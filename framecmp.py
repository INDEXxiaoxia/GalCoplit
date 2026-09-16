"""帧差检测（dHash）、文本相似度与有界帧队列 —— 移植自 Golang 版本 framecmp.go / ocrqueue.go。

用于 OCR 采样管线：画面未变化跳过识别（跳帧）、识别慢时用有界队列解耦截图与识别、
以及识别文本相似度去重，避免重复文本反复进入剧情缓冲。
"""
import threading
from collections import deque

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