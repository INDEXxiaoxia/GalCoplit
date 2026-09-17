"""帧差检测（dHash）、文本相似度与有界帧队列 —— 移植自 Golang 版本 framecmp.go / ocrqueue.go。

用于 OCR 采样管线：画面未变化跳过识别（跳帧）、识别慢时用有界队列解耦截图与识别、
以及识别文本相似度去重，避免重复文本反复进入剧情缓冲。
"""
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

# frameDiffThreshold：dHash 汉明距离阈值，≤ 视为同一画面（跳过 OCR）
FRAME_DIFF_THRESHOLD = 3

# textSimilarityThreshold：文本相似度阈值（0..1），≥ 视为重复文本（不加入剧情）
TEXT_SIM_THRESHOLD = 0.85


# ---------------------------------------------------------------------------
# 帧差异（dHash）
# ---------------------------------------------------------------------------
def frame_hash(img: Image.Image) -> int:
    """计算 9x8 采样灰度 dHash（64 位），抗轻微亮度变化。"""
    gray = img.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    px = list(gray.getdata())
    hash_val = 0
    for y in range(8):
        row = px[y * 9:(y + 1) * 9]
        for x in range(8):
            if row[x] > row[x + 1]:
                hash_val |= 1 << (y * 8 + x)
    return hash_val


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ---------------------------------------------------------------------------
# 文本相似度（归一化编辑距离，0..1，越大越像）
# ---------------------------------------------------------------------------
def _normalize_text(s: str) -> str:
    return "".join(ch for ch in s if not ch.isspace())


def _levenshtein(a: str, b: str) -> int:
    if len(a) == 0:
        return len(b)
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[len(b)]


def text_similarity(a: str, b: str) -> float:
    """归一化编辑距离相似度（0..1，越大越像）。"""
    na, nb = _normalize_text(a), _normalize_text(b)
    if not na and not nb:
        return 1.0
    m = max(len(na), len(nb))
    if m == 0:
        return 1.0
    return 1.0 - _levenshtein(na, nb) / float(m)


# ---------------------------------------------------------------------------
# 有界帧队列（满则丢最旧、留最新）
# ---------------------------------------------------------------------------
class FrameQueue:
    """线程安全有界 FIFO 帧队列，get 阻塞取帧，close 后禁止再入队并唤醒。"""

    def __init__(self, maxlen: int):
        self._max = max(1, maxlen)
        self._buf: deque = deque()
        self._cond = threading.Condition()
        self._closed = False

    def put(self, img) -> None:
        if img is None:
            return
        with self._cond:
            if self._closed:
                return
            if len(self._buf) == self._max:
                self._buf.popleft()  # 丢最旧
            self._buf.append(img)
            self._cond.notify()

    def get(self):
        """阻塞取一帧；队列关闭且已空时返回 None。"""
        with self._cond:
            while not self._buf and not self._closed:
                self._cond.wait()
            if self._buf:
                return self._buf.popleft()
            return None

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def __len__(self) -> int:
        with self._cond:
            return len(self._buf)


# ---------------------------------------------------------------------------
# 带帧编号的识别任务队列（v1.1 队列模式）
# ---------------------------------------------------------------------------
@dataclass
class FrameItem:
    """一个待识别帧：帧编号 + 图像 + 入队时间戳。

    frame_id 由生产者单调递增分配，用于剧情总结的锚点定位。
    """

    frame_id: int
    img: Image.Image
    ts: float = field(default_factory=time.monotonic)


class FrameTaskQueue:
    """线程安全有界 FIFO 帧任务队列（v1.1）。

    与 FrameQueue 的区别：
    - 元素是携带 frame_id 的 FrameItem，可用于锚点定位；
    - 统计已入队总数与因积压溢出而丢弃的帧数，便于 UI 显式提示；
    - 除 close（立即结束）外提供 seal（禁止入队但允许消费者排空剩余帧）。
    """

    def __init__(self, maxlen: int):
        self._max = max(1, maxlen)
        self._buf: deque = deque()
        self._cond = threading.Condition()
        self._closed = False
        self._sealed = False
        self._enqueued = 0
        self._dropped = 0

    def put(self, item: FrameItem) -> tuple:
        """入队一帧。

        返回 (是否入队成功, 因积压溢出被丢弃的帧编号)。队列已封口/关闭时返回 (False, 0)。
        被丢弃的帧编号必须上报，否则锚点的连续完成水位会永久停在该帧。
        """
        if item is None:
            return False, 0
        with self._cond:
            if self._closed or self._sealed:
                return False, 0
            dropped_id = 0
            if len(self._buf) >= self._max:
                dropped_id = self._buf.popleft().frame_id  # 积压溢出：丢最旧
                self._dropped += 1
            self._buf.append(item)
            self._enqueued += 1
            self._cond.notify()
            return True, dropped_id

    def get(self) -> Optional[FrameItem]:
        """阻塞取一帧；关闭时立即返回 None，封口且已排空时返回 None。"""
        with self._cond:
            while not self._buf and not self._closed and not self._sealed:
                self._cond.wait()
            if self._closed:
                return None
            if self._buf:
                return self._buf.popleft()
            return None

    def seal(self) -> None:
        """封口：不再接受新帧，但消费者仍可把已入队的帧全部取完。"""
        with self._cond:
            self._sealed = True
            self._cond.notify_all()

    def close(self) -> None:
        """立即关闭：丢弃未消费帧并唤醒消费者。"""
        with self._cond:
            self._closed = True
            self._buf.clear()
            self._cond.notify_all()

    def stats(self) -> tuple:
        """返回 (待识别帧数, 已入队总数, 已丢弃帧数)。"""
        with self._cond:
            return len(self._buf), self._enqueued, self._dropped

    @property
    def dropped(self) -> int:
        with self._cond:
            return self._dropped

    def __len__(self) -> int:
        with self._cond:
            return len(self._buf)