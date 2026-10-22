import sys
import os
import json
import time
import datetime
import threading
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QMessageBox, QRubberBand, QLineEdit,
    QDialog, QFileDialog, QFormLayout, QSpinBox, QDoubleSpinBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
    QInputDialog, QListWidget, QListWidgetItem, QSplitter, QCheckBox,
    QTabWidget, QFrame, QSizeGrip, QScrollArea, QStackedWidget, QButtonGroup,
    QGridLayout, QAbstractItemView, QProgressBar,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QRect, QPoint, QTimer, QSize,
    QPropertyAnimation, QEasingCurve, QMimeData, QEvent, QCoreApplication,
)
from PyQt6.QtGui import QGuiApplication, QTextCursor, QTextBlockFormat, QDrag, QCursor, QIcon
from PIL import Image, ImageGrab

from ocr_engine import (
    set_tesseract_cmd,
    ocr_image,
    ocr_image_extended,
    normalize_backend,
    langs_for_backend,
    resolve_ocr_lang,
    OCR_LANG_LABELS,
    DEFAULT_OCR_LANG,
)
from llm_client import LLMClient, SUMMARY_PROMPT_BRIEF, SUMMARY_USER_TAIL
from recorder import Database, StoryTreeError
from story_tree_page import StoryTreePage
import theme
from theme import apply_theme, status_visual, make_shadow
from framecmp import (
    frame_hash, hamming_distance, text_similarity, FrameQueue,
    FrameItem, FrameTaskQueue,
    FRAME_DIFF_THRESHOLD, TEXT_SIM_THRESHOLD,
)
from paddle_plugin import (
    BACKEND_CPU,
    BACKEND_GPU,
    backend_to_device,
    environment_ready,
    get_manager,
    shutdown_all as shutdown_paddle_workers,
)
from setup_paddle_plugin import install_paddle_plugin


# ---------------------------------------------------------------------------
# 路径与默认配置
# ---------------------------------------------------------------------------
def _app_dir() -> str:
    """返回 exe/script 所在目录，打包后也固定到 exe 目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _default_config() -> dict:
    return {
        "api_key": "",
        "base_url": "https://您的API地址/v1",
        "model": "",
        "ocr_interval_ms": 1000,
        "summary_sentence_count": 100,
        "plot_context_limit": 12000,
        "ocr_lang": "chi_sim",
        "ocr_backend": "rapid",
        "tesseract_path": "",
        "det_limit_side_len": 256,
        "ocr_queue_size": 3,
        "queue_mode": True,
        "queue_backlog_limit": 300,
        "frame_diff_threshold": FRAME_DIFF_THRESHOLD,
        "text_similarity_threshold": TEXT_SIM_THRESHOLD,
        "auto_extract_key_info": True,
        "save_ocr_text": False,
        "summary_mode": "auto",
        "theme": "light",
    }


# OCR 后端：值写入 config，展示文案给用户看
OCR_BACKEND_OPTIONS = [
    ("rapid", "rapid(内置)"),
    (BACKEND_CPU, "paddle-cpu(外置)"),
    (BACKEND_GPU, "paddle-gpu(外置)"),
    ("tesseract", "tesseract"),
    ("windows", "windows"),
]
OCR_BACKEND_IDS = {item[0] for item in OCR_BACKEND_OPTIONS}

OCR_BACKEND_HINTS = {
    "rapid": "无需额外安装，速度适中。内置中文 PP-OCRv4。",
    BACKEND_CPU: "固定 PP-OCRv6_small，通常快于 rapid；首次识别较慢，需额外安装。",
    BACKEND_GPU: "固定 PP-OCRv6_small，速度最快；依赖 GPU/CUDA，首次识别较慢，需额外安装。",
    "tesseract": "需本机安装 Tesseract，并填写下方路径。",
    "windows": "使用系统自带 OCR，依赖 Windows 10/11 与 winrt。",
}

PADDLE_INSTALL_DOWNLOAD_HINT = {
    "cpu": "已打开命令行窗口进行安装，需要下载约900MB文件。",
    "gpu": "已打开命令行窗口进行安装，需要下载约2.33GB文件。",
}


def _normalize_config(config: dict) -> dict:
    """数值/布尔类型归一化 + 非法值兜底。"""
    int_keys = (
        "ocr_interval_ms", "summary_sentence_count", "plot_context_limit", "det_limit_side_len",
        "ocr_queue_size", "queue_backlog_limit", "frame_diff_threshold",
        "last_game_id", "last_route_id", "last_tree_id",
    )
    defaults = _default_config()
    for k in int_keys:
        if k in config:
            try:
                config[k] = int(config[k])
            except (ValueError, TypeError):
                config[k] = defaults.get(k)
    if isinstance(config.get("auto_extract_key_info"), str):
        config["auto_extract_key_info"] = config["auto_extract_key_info"].lower() == "true"
    if isinstance(config.get("queue_mode"), str):
        config["queue_mode"] = config["queue_mode"].lower() == "true"
    config["queue_mode"] = bool(config.get("queue_mode", True))
    if isinstance(config.get("save_ocr_text"), str):
        config["save_ocr_text"] = config["save_ocr_text"].lower() == "true"
    config["save_ocr_text"] = bool(config.get("save_ocr_text", False))
    if not isinstance(config.get("queue_backlog_limit"), int) or config["queue_backlog_limit"] < 20:
        config["queue_backlog_limit"] = defaults["queue_backlog_limit"]
    # 检测分辨率：合法范围 64..800
    det = config.get("det_limit_side_len", defaults["det_limit_side_len"])
    if not isinstance(det, int) or not 64 <= det <= 800:
        det = defaults["det_limit_side_len"]
    config["det_limit_side_len"] = det
    # 画面差异阈值：dHash 汉明距离，合法范围 0..64
    fdt = config.get("frame_diff_threshold", defaults["frame_diff_threshold"])
    if not isinstance(fdt, int) or not 0 <= fdt <= 64:
        fdt = defaults["frame_diff_threshold"]
    config["frame_diff_threshold"] = fdt
    context_limit = config.get("plot_context_limit", defaults["plot_context_limit"])
    if not isinstance(context_limit, int) or not 1000 <= context_limit <= 100000:
        context_limit = defaults["plot_context_limit"]
    config["plot_context_limit"] = context_limit
    # 文本重复阈值：相似度 0.50..1.00
    try:
        tst = float(config.get("text_similarity_threshold", defaults["text_similarity_threshold"]))
    except (ValueError, TypeError):
        tst = defaults["text_similarity_threshold"]
    if not 0.5 <= tst <= 1.0:
        tst = defaults["text_similarity_threshold"]
    config["text_similarity_threshold"] = round(tst, 2)
    if config.get("theme") not in ("dark", "light"):
        config["theme"] = "light"
    config["ocr_backend"] = normalize_backend(config.get("ocr_backend", "rapid"))
    if config.get("ocr_backend") not in OCR_BACKEND_IDS:
        config["ocr_backend"] = "rapid"
    config["ocr_lang"] = resolve_ocr_lang(
        config["ocr_backend"], config.get("ocr_lang", DEFAULT_OCR_LANG)
    )
    return config


APP_DIR = _app_dir()


def _resolve_icon_path() -> str:
    """打包后从 exe 旁或 _MEIPASS 取 icon.ico，供窗口/任务栏显示。"""
    bases = [APP_DIR]
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            bases.append(meipass)
        bases.append(os.path.dirname(sys.executable))
    seen = set()
    for base in bases:
        if not base:
            continue
        path = os.path.normpath(os.path.join(base, "icon.ico"))
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(path):
            return path
    return ""


def _apply_app_icon(app: QApplication, window: Optional[QMainWindow] = None) -> None:
    """设置进程/窗口图标，并固定 Win 任务栏 AppUserModelID，避免显示成 Python 图标。"""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "INDEXxiaoxia.GalCoplit"
            )
        except Exception:
            pass
    icon_path = _resolve_icon_path()
    if not icon_path:
        return
    icon = QIcon(icon_path)
    app.setWindowIcon(icon)
    if window is not None:
        window.setWindowIcon(icon)


# ---------------------------------------------------------------------------
# 导出工具
# ---------------------------------------------------------------------------
def _sanitize_filename(name: str) -> str:
    """去掉 Windows 文件名非法字符"""
    return re.sub(r'[\\\\<>:\"/|?*]', '', name).strip()


_WIN_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _safe_path_part(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip())
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        cleaned = "未命名"
    if cleaned.upper() in _WIN_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:80]


def _format_dt(iso: str) -> str:
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso)


SUMMARY_LIST_LIMIT = 999


def _shared_knowledge(db: Database, route_id: Optional[int]):
    if not route_id:
        return [], [], []
    return (
        db.list_key_info(route_id, category="世界观设定"),
        db.list_characters(route_id),
        db.list_key_info(route_id, category="剧情线索"),
    )


def _append_shared_sections(lines: List[str], world_infos, characters, clues):
    lines.append("## 世界观")
    lines.append("")
    lines.append("> 本游戏各线路共享。设定冲突处标为「线路名线中原设定」。")
    lines.append("")
    if world_infos:
        for item in world_infos:
            note = item.get("user_note") or ""
            if note:
                lines.append(f"- **{item['content']}**：{note}")
            else:
                lines.append(f"- **{item['content']}**")
    else:
        lines.append("（暂无世界观设定）")
    lines.append("")

    lines.append("## 角色")
    lines.append("")
    lines.append("> 本游戏各线路共享。设定冲突处标为「线路名线中原设定」。")
    lines.append("")
    if characters:
        for char in characters:
            info = char.get("info") or ""
            if info:
                lines.append(f"- **{char['name']}**：{info}")
            else:
                lines.append(f"- **{char['name']}**")
    else:
        lines.append("（暂无角色信息）")
    lines.append("")

    lines.append("## 线索")
    lines.append("")
    lines.append("> 本游戏各线路共享。设定冲突处标为「线路名线中原设定」。")
    lines.append("")
    if clues:
        for item in clues:
            note = item.get("user_note") or ""
            if note:
                lines.append(f"- **{item['content']}**：{note}")
            else:
                lines.append(f"- **{item['content']}**")
    else:
        lines.append("（暂无线索）")
    lines.append("")


def _append_summary_blocks(lines: List[str], summaries, heading: str):
    if not summaries:
        lines.append("（暂无剧情总结）")
        lines.append("")
        return
    for summary in summaries:
        title = summary.get("title") or "段落"
        created = _format_dt(summary.get("created_at") or "")
        lines.append(f"{heading} {title}")
        lines.append(f"<span style=\"color:#888\">{created}</span>")
        lines.append("")
        lines.append(summary.get("content", ""))
        lines.append("")


def _write_markdown(output_dir: str, filename: str, lines: List[str]) -> str:
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def export_route_markdown(db: Database, game_id: int, route_id: int, output_dir: str) -> str:
    """将指定线路的世界观、角色、线索、剧情总结导出为 Markdown 文件，返回文件路径"""
    game = db.get_game(game_id)
    route = db.get_route(route_id)
    if not game or not route:
        raise ValueError("游戏或线路不存在")

    summaries = db.list_summaries(route_id, limit=SUMMARY_LIST_LIMIT)
    world_infos, characters, clues = _shared_knowledge(db, route_id)

    lines: List[str] = []
    lines.append(f"# {game['name']} - {route['name']}")
    lines.append("")
    lines.append(f"- 导出时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    _append_shared_sections(lines, world_infos, characters, clues)
    lines.append("## 剧情总结")
    lines.append("")
    lines.append("> 仅包含当前线路。")
    lines.append("")
    _append_summary_blocks(lines, summaries, "###")

    safe_game = _sanitize_filename(game["name"]) or "未命名游戏"
    safe_route = _sanitize_filename(route["name"]) or "未命名线路"
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_game}-{safe_route}-{timestamp}.md"
    return _write_markdown(output_dir, filename, lines)


def export_game_markdown(db: Database, game_id: int, output_dir: str) -> tuple:
    """导出整款游戏。共享设定只写一次，剧情按线路分节。返回 (路径, 达到上限的线路名)。"""
    game = db.get_game(game_id)
    if not game:
        raise ValueError("游戏不存在")
    routes = db.list_routes(game_id)
    probe_id = routes[0]["id"] if routes else None
    world_infos, characters, clues = _shared_knowledge(db, probe_id)

    lines: List[str] = []
    lines.append(f"# {game['name']}")
    lines.append("")
    lines.append(f"- 导出时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    _append_shared_sections(lines, world_infos, characters, clues)
    lines.append("## 剧情总结")
    lines.append("")
    lines.append("> 按线路分开。每条线路最多 999 段，顺序与时间线一致。")
    lines.append("")
    capped: List[str] = []
    if not routes:
        lines.append("（暂无线路）")
        lines.append("")
    for route in routes:
        lines.append(f"### {route.get('name') or '未命名线路'}")
        lines.append("")
        summaries = db.list_summaries(route["id"], limit=SUMMARY_LIST_LIMIT)
        if len(summaries) >= SUMMARY_LIST_LIMIT:
            capped.append(route.get("name") or "未命名线路")
        _append_summary_blocks(lines, summaries, "####")

    safe_game = _sanitize_filename(game["name"]) or "未命名游戏"
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_game}-全部线路-{timestamp}.md"
    return _write_markdown(output_dir, filename, lines), capped


def export_story_tree_markdown(db: Database, tree_id: int, output_dir: str) -> str:
    """导出一棵剧情树。主链、追加、差分和分支分段书写。"""
    text = db.render_story_tree_markdown(tree_id)
    tree = db.get_story_tree(tree_id)
    game = db.get_game(tree["game_id"]) if tree else None
    safe_game = _sanitize_filename((game or {}).get("name") or "未命名游戏") or "未命名游戏"
    safe_tree = _sanitize_filename((tree or {}).get("name") or "剧情树") or "剧情树"
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_game}-{safe_tree}-{timestamp}.md"
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


# ---------------------------------------------------------------------------
# 通用 UI 小部件
# ---------------------------------------------------------------------------
def make_card() -> QFrame:
    frame = QFrame()
    frame.setProperty("card", True)
    return frame


class NoWheelSpinBox(QSpinBox):
    """滚轮不改数值的整数输入框，避免滚动页面时误改配置。"""

    def wheelEvent(self, event):
        event.ignore()  # 交给滚动区处理，页面照常滚动


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """滚轮不改数值的小数输入框，避免滚动页面时误改配置。"""

    def wheelEvent(self, event):
        event.ignore()


class NoWheelComboBox(QComboBox):
    """滚轮不切换选项的下拉框；下拉列表展开时仍可用滚轮浏览选项。"""

    def setEditable(self, editable: bool):
        super().setEditable(editable)
        self._install_line_edit_wheel_filter()

    def showEvent(self, event):
        super().showEvent(event)
        self._install_line_edit_wheel_filter()

    def _install_line_edit_wheel_filter(self):
        # 可编辑时 Qt 会在内部 LineEdit 上拦截滚轮改选项，必须在此过滤
        le = self.lineEdit()
        if le is None:
            return
        # 用 Qt property 标记，避免 sip 包装对象丢弃普通 Python 属性
        if le.property("_nowheel_filtered"):
            return
        le.installEventFilter(self)
        le.setProperty("_nowheel_filtered", True)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel and watched is self.lineEdit():
            if self.view().isVisible():
                return False
            # 不改选项，把滚轮交给外层滚动区
            scroll = self.parentWidget()
            while scroll is not None:
                if isinstance(scroll, QScrollArea):
                    QCoreApplication.sendEvent(scroll.viewport(), event)
                    break
                scroll = scroll.parentWidget()
            return True
        return super().eventFilter(watched, event)

    def wheelEvent(self, event):
        # 列表已展开：允许滚动浏览；收起时忽略，避免滚动页面时误改选项
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoWheelLineEdit(QLineEdit):
    """滚轮不抢事件的输入框，交给外层滚动区处理页面滚动。"""

    def wheelEvent(self, event):
        event.ignore()


def make_hint(text: str) -> QLabel:
    """设置项下方的说明文字（含建议值）"""
    label = QLabel(text)
    set_cls(label, "dim")
    label.setWordWrap(True)
    label.setStyleSheet("font-size: 11px; padding: 0px 0px 2px 2px;")
    return label


def make_caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("cls", "caption")
    return label


def set_cls(widget: QWidget, cls: str):
    widget.setProperty("cls", cls)


def set_variant(button: QPushButton, variant: str):
    button.setProperty("variant", variant)


def make_combo(editable: bool = False) -> QComboBox:
    combo = NoWheelComboBox()
    if editable:
        combo.setEditable(True)
        # 不替换内部 QLineEdit：自定义 LineEdit 会套上全局 padding，文字被裁切。
        combo._install_line_edit_wheel_filter()
    theme.polish_combo_popup(combo)
    return combo


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class NavButton(QPushButton):
    """侧边栏导航按钮：图标占固定宽度，文本严格左对齐"""

    def __init__(self, glyph: str, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("NavBtn")
        self.setCheckable(True)
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 12, 0)
        lay.setSpacing(10)
        self._glyph = QLabel(glyph)
        self._glyph.setObjectName("NavGlyph")
        self._glyph.setFixedWidth(18)
        self._glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._text = QLabel(text)
        self._text.setObjectName("NavText")
        for w in (self._glyph, self._text):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay.addWidget(self._glyph)
        lay.addWidget(self._text)
        lay.addStretch()

        self.toggled.connect(self._sync_state)

    def _sync_state(self, checked: bool):
        for w in (self._glyph, self._text):
            w.setProperty("active", checked)
            w.style().unpolish(w)
            w.style().polish(w)


class PulseDot(QLabel):
    """状态指示灯，可呼吸闪烁"""

    def __init__(self, parent=None):
        super().__init__("●", parent)
        self._color = theme.col("text_faint")
        self._blink = False
        self._on = True
        self._timer = QTimer(self)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self._tick)
        self._apply()

    def set_state(self, color: str, blink: bool):
        self._color = color
        self._blink = blink
        self._on = True
        if blink:
            self._timer.start()
        else:
            self._timer.stop()
        self._apply()

    def _tick(self):
        self._on = not self._on
        self._apply()

    def _apply(self):
        color = self._color if self._on else theme.col("dot_off")
        self.setStyleSheet(f"color: {color}; font-size: 11px; background: transparent;")


class TitleBar(QWidget):
    """无边框窗口的自定义标题栏"""

    def __init__(self, window: QWidget, title: str = "GalCoplit"):
        super().__init__(window)
        self._win = window
        self._drag_offset: Optional[QPoint] = None
        self.setObjectName("TitleBar")
        self.setFixedHeight(46)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 8, 0)
        layout.setSpacing(10)

        mark = QLabel("◆")
        mark.setObjectName("TitleMark")
        layout.addWidget(mark)

        lbl = QLabel(title)
        lbl.setObjectName("TitleText")
        layout.addWidget(lbl)
        layout.addStretch()

        btn_min = QPushButton("—")
        btn_min.setObjectName("WinBtn")
        btn_min.clicked.connect(window.showMinimized)
        layout.addWidget(btn_min)

        btn_close = QPushButton("✕")
        btn_close.setObjectName("WinBtnClose")
        btn_close.clicked.connect(window.close)
        layout.addWidget(btn_close)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._win._title_dragging = True
            self._win._stable_size = self._win.size()
            self._drag_offset = (
                event.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._win.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        self._win._title_dragging = False
        self._win._stable_size = self._win.size()


class BaseDialog(QDialog):
    """统一风格的无边框圆角对话框，带自定义头部、投影与淡入动画"""

    HEADER_HEIGHT = 46
    MARGIN = 14

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setObjectName("GlassDialog")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._drag_offset: Optional[QPoint] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(self.MARGIN, self.MARGIN, self.MARGIN, self.MARGIN)
        outer.setSpacing(0)

        frame = QFrame()
        frame.setObjectName("DialogFrame")
        outer.addWidget(frame)
        self._shadow = make_shadow(frame, blur=32, dy=6)

        inner = QVBoxLayout(frame)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)

        header = QWidget()
        header.setObjectName("DialogHeader")
        header.setFixedHeight(self.HEADER_HEIGHT)
        h = QHBoxLayout(header)
        h.setContentsMargins(18, 0, 10, 0)
        h.setSpacing(10)
        mark = QLabel("◆")
        mark.setObjectName("TitleMark")
        h.addWidget(mark)
        lbl = QLabel(title)
        lbl.setObjectName("DialogTitle")
        h.addWidget(lbl)
        h.addStretch()
        btn_close = QPushButton("✕")
        btn_close.setObjectName("WinBtnClose")
        btn_close.clicked.connect(self.reject)
        h.addWidget(btn_close)
        inner.addWidget(header)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(20, 16, 20, 20)
        self.body.setSpacing(12)
        inner.addLayout(self.body, 1)

    def showEvent(self, event):
        super().showEvent(event)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(180)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.start()

    def mousePressEvent(self, event):
        y = event.position().y()
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.MARGIN <= y < self.MARGIN + self.HEADER_HEIGHT
        ):
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None


def _monitor_physical_rects() -> dict:
    """Windows 显示器设备名到物理像素矩形。Qt 逻辑坐标不能直接乘缩放比。"""
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    rects = {}

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def callback(monitor, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcMonitor
            rects[info.szDevice] = (rect.left, rect.top, rect.right, rect.bottom)
        return True

    user32.EnumDisplayMonitors(None, None, enum_proc(callback), 0)
    return rects


def _screen_physical_origin(screen) -> tuple:
    geo = screen.geometry()
    dpr = screen.devicePixelRatio() or 1.0
    if sys.platform != "win32":
        return geo.x() * dpr, geo.y() * dpr
    rects = _monitor_physical_rects()
    name = (screen.name() or "").casefold()
    for device, rect in rects.items():
        if device.casefold() == name:
            return rect[0], rect[1]
    target_w = round(geo.width() * dpr)
    target_h = round(geo.height() * dpr)
    for left, top, right, bottom in rects.values():
        if abs((right - left) - target_w) <= 2 and abs((bottom - top) - target_h) <= 2:
            return left, top
    return geo.x() * dpr, geo.y() * dpr


def _logical_bbox_to_physical(region: dict) -> tuple:
    """把 Qt 全局逻辑坐标换成虚拟桌面上的物理像素，副屏和不同缩放都能对上。"""
    left = int(region["left"])
    top = int(region["top"])
    width = max(1, int(region["width"]))
    height = max(1, int(region["height"]))
    point = QPoint(left + width // 2, top + height // 2)
    screen = QGuiApplication.screenAt(point) or QGuiApplication.screenAt(QPoint(left, top))
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return left, top, left + width, top + height
    origin_x, origin_y = _screen_physical_origin(screen)
    geo = screen.geometry()
    dpr = screen.devicePixelRatio() or 1.0
    physical_left = int(round(origin_x + (left - geo.x()) * dpr))
    physical_top = int(round(origin_y + (top - geo.y()) * dpr))
    physical_right = physical_left + max(1, int(round(width * dpr)))
    physical_bottom = physical_top + max(1, int(round(height * dpr)))
    return physical_left, physical_top, physical_right, physical_bottom


def _grab_region(bbox: tuple):
    """截取虚拟桌面上的一块。all_screens 才能截到主屏以外的显示器。"""
    try:
        return ImageGrab.grab(bbox=bbox, all_screens=True)
    except TypeError:
        return ImageGrab.grab(bbox=bbox)


# ---------------------------------------------------------------------------
# 区域选择器
# ---------------------------------------------------------------------------
class RegionSelector(QWidget):
    selected = pyqtSignal(QRect)

    def __init__(self, geometry: QRect):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background-color: rgba(0, 0, 0, 80);")
        self.rubber_band = None
        self.origin = QPoint()
        self._selected = False
        self.setGeometry(geometry)

    def paintEvent(self, event):
        from PyQt6.QtGui import QPainter, QColor
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 80))

    def closeEvent(self, event):
        if not self._selected:
            self.selected.emit(QRect())
        event.accept()

    def mousePressEvent(self, event):
        self.origin = event.pos()
        self.rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self.rubber_band.setGeometry(QRect(self.origin, self.origin))
        self.rubber_band.show()

    def mouseMoveEvent(self, event):
        if self.rubber_band:
            self.rubber_band.setGeometry(
                QRect(self.origin, event.pos()).normalized()
            )

    def mouseReleaseEvent(self, event):
        if self.rubber_band:
            rect = self.rubber_band.geometry()
            self.rubber_band.hide()
            self._selected = True
            self.close()
            global_rect = QRect(self.mapToGlobal(rect.topLeft()), rect.size())
            self.selected.emit(global_rect)


# ---------------------------------------------------------------------------
# 工作线程
# ---------------------------------------------------------------------------
class OCRWorker(QThread):
    text_ready = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, region: dict, interval_ms: int, lang: str, tesseract_path: str, backend: str = "rapid", queue_size: int = 3, det_limit: int = 0,
                 frame_diff_threshold: int = FRAME_DIFF_THRESHOLD,
                 text_sim_threshold: float = TEXT_SIM_THRESHOLD):
        super().__init__()
        self.region = region
        self.interval = interval_ms / 1000.0
        self.lang = lang
        self.tesseract_path = tesseract_path
        self.backend = backend
        self.queue_size = queue_size
        self.det_limit = det_limit
        self.frame_diff_threshold = frame_diff_threshold
        self.text_sim_threshold = text_sim_threshold
        self._running = False
        self._bbox_lock = threading.Lock()
        self._physical_bbox = _logical_bbox_to_physical(region)
        self._queue = FrameQueue(queue_size)

    def update_region(self, region: dict):
        """运行时更新识别区域"""
        with self._bbox_lock:
            self.region = region
            self._physical_bbox = _logical_bbox_to_physical(region)

    def _produce(self):
        """生产线程：固定间隔截图，画面 dHash 变化才入队（跳帧）"""
        last_hash = 0
        have_hash = False
        while self._running:
            try:
                with self._bbox_lock:
                    bbox = self._physical_bbox
                pil_img = _grab_region(bbox)
                h = frame_hash(pil_img)
                if not have_hash or hamming_distance(h, last_hash) > self.frame_diff_threshold:
                    last_hash = h
                    have_hash = True
                    self._queue.put(pil_img)
            except Exception as e:
                self.error.emit(str(e))
            time.sleep(self.interval)

    def run(self):
        set_tesseract_cmd(self.tesseract_path)
        producer = threading.Thread(target=self._produce, daemon=True)
        producer.start()

        last_text = ""
        while self._running:
            pil_img = self._queue.get()
            if pil_img is None:
                break
            try:
                text = ocr_image(pil_img, self.lang, backend=self.backend, det_limit=self.det_limit)
                # 文本相似度去重：≥ 阈值视为重复，不重复进入剧情缓冲
                if text and (last_text == "" or text_similarity(text, last_text) < self.text_sim_threshold):
                    last_text = text
                    self.text_ready.emit(text)
            except Exception as e:
                self.error.emit(str(e))

    def start(self):
        self._running = True
        super().start()

    def stop(self):
        self._running = False
        self._queue.close()
        self.wait(2000)


class QueuedOCRWorker(QThread):
    """队列模式（v1.1）识别线程：截图比对与 OCR 彻底解耦。

    生产线程只做「截图 + dHash 比对」，画面变化即分配单调递增的 frame_id 入队；
    消费循环持续追赶 OCR，每帧无论识别结果是否被去重都上报完成，
    使剧情总结可以用 frame_id 作为锚点等待某一段全部识别完成。
    """

    # frame_id, 已入队总数
    frame_enqueued = pyqtSignal(int, int)
    # frame_id, 识别文本（被去重时为空）, 已完成总数, 已入队总数, 待识别数
    frame_recognized = pyqtSignal(int, str, int, int, int)
    # 因积压溢出被丢弃的 frame_id, 累计丢弃数
    frame_dropped = pyqtSignal(int, int)
    error = pyqtSignal(str)

    def __init__(self, region: dict, interval_ms: int, lang: str, tesseract_path: str,
                 backend: str = "rapid", backlog_limit: int = 300, det_limit: int = 0,
                 start_seq: int = 0,
                 frame_diff_threshold: int = FRAME_DIFF_THRESHOLD,
                 text_sim_threshold: float = TEXT_SIM_THRESHOLD):
        super().__init__()
        self.region = region
        self.interval = interval_ms / 1000.0
        self.lang = lang
        self.tesseract_path = tesseract_path
        self.backend = backend
        self.backlog_limit = backlog_limit
        self.det_limit = det_limit
        self.frame_diff_threshold = frame_diff_threshold
        self.text_sim_threshold = text_sim_threshold
        self._running = False
        self._bbox_lock = threading.Lock()
        self._physical_bbox = _logical_bbox_to_physical(region)
        self._queue = FrameTaskQueue(backlog_limit)

        # 帧编号与完成水位；start_seq 让暂停后重新启动的帧号继续递增，不与旧帧冲突
        self._stat_lock = threading.Lock()
        self._start_seq = max(0, start_seq)
        self._frame_seq = self._start_seq
        self._last_enqueued_id = self._start_seq
        self._prev_enqueued_id = self._start_seq
        self._done_upto = 0
        self._done_total = 0

    def update_region(self, region: dict):
        """运行时更新识别区域"""
        with self._bbox_lock:
            self.region = region
            self._physical_bbox = _logical_bbox_to_physical(region)

    # ------------------------------------------------------------------
    # 进度与锚点查询（供 UI 线程调用，均加锁）
    # ------------------------------------------------------------------
    def latest_enqueued_frame_id(self) -> int:
        """最新已入队帧编号；尚无帧时为 0。"""
        with self._stat_lock:
            return self._last_enqueued_id

    def prev_enqueued_frame_id(self) -> int:
        """最新已入队帧的前一帧编号，用作总结锚点。

        只有一帧时退化为该帧本身，避免锚点为 0 导致空区间。
        """
        with self._stat_lock:
            if self._prev_enqueued_id:
                return self._prev_enqueued_id
            return self._last_enqueued_id

    def recognized_upto(self) -> int:
        """已连续识别完成到的帧编号。"""
        with self._stat_lock:
            return self._done_upto

    def frame_seq_high_water(self) -> int:
        """本次运行分配到的最大帧编号，供下一个 worker 续号。"""
        with self._stat_lock:
            return self._frame_seq

    def progress(self) -> tuple:
        """返回本次运行的 (已识别, 已累计, 待识别, 已丢弃)。"""
        pending, _enqueued, dropped = self._queue.stats()
        with self._stat_lock:
            return self._done_total, self._frame_seq - self._start_seq, pending, dropped

    # ------------------------------------------------------------------
    # 生产 / 消费
    # ------------------------------------------------------------------
    def _produce(self):
        """生产线程：固定间隔截图，画面 dHash 变化才分配帧号入队（跳帧）"""
        last_hash = 0
        have_hash = False
        while self._running:
            try:
                with self._bbox_lock:
                    bbox = self._physical_bbox
                pil_img = _grab_region(bbox)
                h = frame_hash(pil_img)
                if not have_hash or hamming_distance(h, last_hash) > self.frame_diff_threshold:
                    last_hash = h
                    have_hash = True
                    with self._stat_lock:
                        self._frame_seq += 1
                        frame_id = self._frame_seq
                        self._prev_enqueued_id = self._last_enqueued_id
                        self._last_enqueued_id = frame_id
                        enqueued_total = self._frame_seq - self._start_seq
                    accepted, dropped_id = self._queue.put(FrameItem(frame_id=frame_id, img=pil_img))
                    if dropped_id:
                        # 被丢弃的帧永远不会被识别，需推进完成水位，避免锚点永久等待
                        with self._stat_lock:
                            if dropped_id > self._done_upto:
                                self._done_upto = dropped_id
                        self.frame_dropped.emit(dropped_id, self._queue.dropped)
                    if accepted:
                        self.frame_enqueued.emit(frame_id, enqueued_total)
            except Exception as e:
                self.error.emit(str(e))
            time.sleep(self.interval)

    def run(self):
        set_tesseract_cmd(self.tesseract_path)
        producer = threading.Thread(target=self._produce, daemon=True)
        producer.start()

        last_text = ""
        while True:
            item = self._queue.get()
            if item is None:
                break
            text = ""
            try:
                raw = ocr_image(item.img, self.lang, backend=self.backend, det_limit=self.det_limit)
                # 文本相似度去重：≥ 阈值视为重复，不重复进入剧情缓冲
                if raw and (last_text == "" or text_similarity(raw, last_text) < self.text_sim_threshold):
                    last_text = raw
                    text = raw
            except Exception as e:
                self.error.emit(str(e))
            # 无论识别结果是否被去重/失败，都必须上报完成，否则锚点水位会永久卡住
            with self._stat_lock:
                # 只前进不后退：生产者可能已因丢帧把水位推到更靠后的帧
                if item.frame_id > self._done_upto:
                    self._done_upto = item.frame_id
                self._done_total += 1
                done_total = self._done_total
                enqueued_total = self._frame_seq - self._start_seq
            self.frame_recognized.emit(
                item.frame_id, text, done_total, enqueued_total, len(self._queue)
            )

    def start(self):
        self._running = True
        super().start()

    def stop(self, drain: bool = False):
        """停止识别。

        drain=True：停止截图但让消费者把已入队帧全部识别完（暂停按钮），避免丢剧情。
        drain=False：立即结束并丢弃未识别帧（切换区域、退出程序）。
        """
        self._running = False
        if drain:
            self._queue.seal()
        else:
            self._queue.close()
            self.wait(2000)


@dataclass
class SummaryTask:
    """队列模式（v1.1）的一次总结任务。

    以 anchor_frame_id 为锚点，覆盖 (start_frame_id, anchor_frame_id] 这段帧的识别文本；
    锚点及之前所有帧识别完成后才能送 LLM，用户连续阅读时多个任务按顺序排队。
    """

    anchor_frame_id: int
    start_frame_id: int
    session_id: Optional[int] = None
    state: str = "等待识别"
    created_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    raw_text: str = ""
    error: str = ""
    queue_seq: int = 0
    summary_id: Optional[int] = None
    node_id: Optional[int] = None


class SummaryThread(QThread):
    # summary, raw_text, key_info_list, title
    summary_ready = pyqtSignal(str, str, str, str)
    error = pyqtSignal(str)

    def __init__(
        self,
        client: LLMClient,
        text: str,
        previous_summary: str,
        auto_extract: bool = True,
        extra_prompt: str = "",
        shared_context: str = "",
        current_route: str = "",
        system_prompt: str = "",
    ):
        super().__init__()
        self.client = client
        self.text = text
        self.previous_summary = previous_summary
        self.auto_extract = auto_extract
        self.extra_prompt = extra_prompt
        self.shared_context = shared_context
        self.current_route = current_route
        self.system_prompt = system_prompt

    def run(self):
        last_error = None
        summary = None
        max_attempts = 4  # 首次 + 最多 3 次重试
        for attempt in range(max_attempts):
            try:
                summary = self.client.summarize(
                    self.text,
                    self.previous_summary,
                    extra_prompt=self.extra_prompt,
                    shared_context=self.shared_context,
                    current_route=self.current_route,
                    system_prompt=self.system_prompt,
                )
                break
            except Exception as e:
                last_error = e
                # 最后一次失败由下方统一处理
        if summary is None:
            self.error.emit(str(last_error))
            return

        title = ""
        try:
            title = self.client.generate_title(summary)
        except Exception:
            pass
        key_info = []
        if self.auto_extract:
            try:
                key_info = self.client.extract_key_info(
                    summary,
                    self.text,
                    extra_prompt=self.extra_prompt,
                    shared_context=self.shared_context,
                    current_route=self.current_route,
                )
            except Exception:
                pass
        self.summary_ready.emit(summary, self.text, json.dumps(key_info, ensure_ascii=False), title)


class TestOCRThread(QThread):
    """单次截图 OCR 测试，返回文本 + 耗时 + 置信度 + 后端"""
    result_ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, region: dict, lang: str, tesseract_path: str, backend: str = "rapid", det_limit: int = 0):
        super().__init__()
        self.region = region
        self.lang = lang
        self.tesseract_path = tesseract_path
        self.backend = backend
        self.det_limit = det_limit

    def run(self):
        try:
            set_tesseract_cmd(self.tesseract_path)
            bbox = _logical_bbox_to_physical(self.region)
            pil_img = _grab_region(bbox)
            start = time.monotonic()
            text, conf = ocr_image_extended(pil_img, self.lang, backend=self.backend, det_limit=self.det_limit)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self.result_ready.emit({
                "text": text,
                "elapsed_ms": elapsed_ms,
                "confidence": round(conf, 4) if conf >= 0 else -1.0,
                "backend": self.backend,
            })
        except Exception as e:
            self.error.emit(str(e))


class TestLLMThread(QThread):
    """用当前填写的地址、密钥和模型发一条很短的请求。"""

    result_ready = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, config: dict):
        super().__init__()
        self._config = config

    def run(self):
        try:
            client = LLMClient(self._config)
            text = client._chat(
                [{"role": "user", "content": "请只回复 OK"}],
                temperature=0,
                timeout=20,
            )
            snippet = (text or "").strip()[:200] or "（模型返回为空）"
            self.result_ready.emit(snippet)
        except Exception as e:
            response = getattr(e, "response", None)
            status = getattr(response, "status_code", None) if response is not None else None
            body = ""
            if response is not None:
                try:
                    body = (response.text or "").strip()[:300]
                except Exception:
                    body = ""
            if status:
                detail = f"HTTP {status}"
                if body:
                    detail += f"\n{body}"
                self.error.emit(detail)
            else:
                self.error.emit(str(e) or "连接失败")


# ---------------------------------------------------------------------------
# OCR 区域指示器
# ---------------------------------------------------------------------------
class RegionIndicator(QWidget):
    """屏幕上显示一个可拖动、可调整大小的区域框"""

    region_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background-color: transparent;")
        self._handle_size = 8
        self._drag_pos: Optional[QPoint] = None
        self._handle: Optional[str] = None
        self._start_pos: Optional[QPoint] = None
        self._start_geo: Optional[QRect] = None
        self.hide()

    def set_region(self, region: Optional[dict]):
        if not region:
            self.hide()
            return
        self.setGeometry(
            region["left"], region["top"], region["width"], region["height"]
        )
        self.show()

    def paintEvent(self, event):
        from PyQt6.QtGui import QPainter, QColor, QPen
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = self.rect()
        pen = QPen(QColor(theme.col("accent")), 2)
        painter.setPen(pen)

        # 只画四角的 L 形边框
        arm = 15  # 角上短线长度
        corners = [
            # 左上角
            [
                QPoint(rect.left(), rect.top()),
                QPoint(rect.left() + arm, rect.top()),
                QPoint(rect.left(), rect.top()),
                QPoint(rect.left(), rect.top() + arm),
            ],
            # 右上角
            [
                QPoint(rect.right() - arm, rect.top()),
                QPoint(rect.right(), rect.top()),
                QPoint(rect.right(), rect.top()),
                QPoint(rect.right(), rect.top() + arm),
            ],
            # 左下角
            [
                QPoint(rect.left(), rect.bottom()),
                QPoint(rect.left() + arm, rect.bottom()),
                QPoint(rect.left(), rect.bottom()),
                QPoint(rect.left(), rect.bottom() - arm),
            ],
            # 右下角
            [
                QPoint(rect.right() - arm, rect.bottom()),
                QPoint(rect.right(), rect.bottom()),
                QPoint(rect.right(), rect.bottom()),
                QPoint(rect.right(), rect.bottom() - arm),
            ],
        ]
        for lines in corners:
            for i in range(0, len(lines), 2):
                painter.drawLine(lines[i], lines[i + 1])

        # 四角定位点小方块
        size = 6
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.col("accent")))
        blocks = [
            QPoint(rect.left(), rect.top()),
            QPoint(rect.right() - size + 1, rect.top()),
            QPoint(rect.left(), rect.bottom() - size + 1),
            QPoint(rect.right() - size + 1, rect.bottom() - size + 1),
        ]
        for pt in blocks:
            painter.drawRect(QRect(pt, QSize(size, size)))

    def _handle_at(self, pos: QPoint) -> str:
        w, h = self.width(), self.height()
        hs = self._handle_size
        x, y = pos.x(), pos.y()

        left = x < hs
        right = x > w - hs
        top = y < hs
        bottom = y > h - hs

        if left and top:
            return "tl"
        if right and top:
            return "tr"
        if left and bottom:
            return "bl"
        if right and bottom:
            return "br"
        if left:
            return "l"
        if right:
            return "r"
        if top:
            return "t"
        if bottom:
            return "b"
        return "move"

    def _cursor_for_handle(self, handle: str):
        cursors = {
            "tl": Qt.CursorShape.SizeFDiagCursor,
            "br": Qt.CursorShape.SizeFDiagCursor,
            "tr": Qt.CursorShape.SizeBDiagCursor,
            "bl": Qt.CursorShape.SizeBDiagCursor,
            "l": Qt.CursorShape.SizeHorCursor,
            "r": Qt.CursorShape.SizeHorCursor,
            "t": Qt.CursorShape.SizeVerCursor,
            "b": Qt.CursorShape.SizeVerCursor,
            "move": Qt.CursorShape.SizeAllCursor,
        }
        return cursors.get(handle, Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._handle = self._handle_at(event.pos())
            self._start_pos = event.globalPosition().toPoint()
            self._start_geo = self.geometry()

    def mouseMoveEvent(self, event):
        pos = event.globalPosition().toPoint()
        if event.buttons() & Qt.MouseButton.LeftButton:
            if not self._start_pos or not self._start_geo:
                return
            delta = pos - self._start_pos
            if self._handle == "move":
                self.move(self._start_geo.x() + delta.x(), self._start_geo.y() + delta.y())
            else:
                self._resize_geometry(delta)
        else:
            handle = self._handle_at(event.pos())
            self.setCursor(self._cursor_for_handle(handle))

    def _resize_geometry(self, delta: QPoint):
        sg = self._start_geo
        min_w, min_h = 20, 20
        handle = self._handle

        # 默认保持左上角不变
        x, y = sg.x(), sg.y()
        w, h = sg.width(), sg.height()

        if "r" in handle:
            w = max(min_w, sg.width() + delta.x())
        if "l" in handle:
            new_w = max(min_w, sg.width() - delta.x())
            x = sg.x() + sg.width() - new_w
            w = new_w
        if "b" in handle:
            h = max(min_h, sg.height() + delta.y())
        if "t" in handle:
            new_h = max(min_h, sg.height() - delta.y())
            y = sg.y() + sg.height() - new_h
            h = new_h

        self.setGeometry(x, y, w, h)

    def mouseReleaseEvent(self, event):
        if self._handle:
            self.region_changed.emit(self._region_dict())
        self._handle = None
        self._drag_pos = None
        self._start_pos = None
        self._start_geo = None

    def _region_dict(self) -> dict:
        return {
            "left": self.x(),
            "top": self.y(),
            "width": self.width(),
            "height": self.height(),
        }


# ---------------------------------------------------------------------------
# 状态浮窗
# ---------------------------------------------------------------------------
class StatusWindow(QWidget):
    """悬浮小窗，实时显示当前状态和最新识别"""

    summarize_requested = pyqtSignal()
    toggle_pause_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(300, 48)
        self._drag_pos = None
        self._ocr_running = False
        self._queue_mode = False
        self._manual_mode = False
        self._summarize_gate = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(0)
        frame = QFrame()
        frame.setObjectName("FloatFrame")
        outer.addWidget(frame)
        self._shadow = make_shadow(frame, blur=10, dy=1)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(2)

        row1 = QHBoxLayout()
        row1.setSpacing(4)

        # 识别状态
        self.dot_ocr = PulseDot()
        row1.addWidget(self.dot_ocr)
        self.lbl_ocr_status = QLabel("等待")
        set_cls(self.lbl_ocr_status, "floatTitle")
        row1.addWidget(self.lbl_ocr_status)

        row1.addSpacing(4)

        # 总结状态
        self.dot_summary = PulseDot()
        row1.addWidget(self.dot_summary)
        self.lbl_summary_status = QLabel("空闲")
        set_cls(self.lbl_summary_status, "floatTitle")
        row1.addWidget(self.lbl_summary_status)

        row1.addStretch()

        self.lbl_count = QLabel("0 句")
        set_cls(self.lbl_count, "floatMeta")
        row1.addWidget(self.lbl_count)
        self.lbl_count.hide()

        self.btn_summarize = QPushButton("总结")
        self.btn_summarize.setObjectName("PillBtn")
        set_variant(self.btn_summarize, "primary")
        self.btn_summarize.setFixedSize(40, 20)
        self.btn_summarize.setStyleSheet("QPushButton { padding: 0px 5px; border-radius: 2px; }")
        self.btn_summarize.clicked.connect(self.summarize_requested.emit)
        row1.addWidget(self.btn_summarize)
        self.btn_summarize.hide()

        self.btn_toggle = QPushButton("▶")
        self.btn_toggle.setObjectName("PillBtn")
        set_variant(self.btn_toggle, "secondary")
        self.btn_toggle.setFixedSize(26, 20)
        self.btn_toggle.setStyleSheet("QPushButton { padding: 0px 2px; border-radius: 2px; }")
        self.btn_toggle.setToolTip("暂停/继续识别")
        self.btn_toggle.clicked.connect(self.toggle_pause_requested.emit)
        row1.addWidget(self.btn_toggle)
        layout.addLayout(row1)

        self.lbl_current = QLabel("--")
        set_cls(self.lbl_current, "floatSub")
        layout.addWidget(self.lbl_current)

        self.hide()

    def refresh_theme(self):
        self._shadow.setColor(theme.shadow_color())
        self.update_state(status=self.lbl_ocr_status.text())

    def showEvent(self, event):
        super().showEvent(event)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(200)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.start()

    def mousePressEvent(self, event):
        self._drag_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self._drag_pos:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.pos() + delta)
            self._drag_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def update_state(self, status=None, current=None):
        if status is not None:
            self.lbl_ocr_status.setText(status)
            color, blink = status_visual(status)
            self.dot_ocr.set_state(color, blink)
        if current is not None and not self._queue_mode:
            # 队列模式下第二行改为显示识别进度，不再显示识别文本
            current = current.replace("\n", " ").replace("\r", " ").strip()
            display = current if len(current) <= 15 else current[:15] + "..."
            self.lbl_current.setText(display)

    def update_summary_state(self, summary_status: str):
        self.lbl_summary_status.setText(summary_status)
        color, blink = status_visual(summary_status)
        self.dot_summary.set_state(color, blink)
        # 总结中时禁用总结按钮；队列模式下可继续追加锚点任务，故保持可用
        allowed = True if self._summarize_gate is None else bool(self._summarize_gate())
        if summary_status == "总结中" and not self._queue_mode:
            self.btn_summarize.setEnabled(False)
        else:
            self.btn_summarize.setEnabled(allowed)

    def set_summarize_gate(self, gate):
        """gate() 为假时，浮窗上的总结按钮不可用。"""
        self._summarize_gate = gate

    def set_ocr_running(self, running: bool):
        """显式同步 OCR 运行状态，控制暂停/继续按钮图标"""
        self._ocr_running = running
        if running:
            self.btn_toggle.setText("暂停")
            self.btn_toggle.setFixedSize(42, 20)
            self.btn_toggle.setToolTip("暂停识别")
        else:
            self.btn_toggle.setText("继续")
            self.btn_toggle.setFixedSize(42, 20)
            self.btn_toggle.setToolTip("继续识别")

    def set_manual_mode(self, enabled: bool):
        self._manual_mode = enabled
        self._sync_manual_widgets()

    def set_queue_mode(self, enabled: bool):
        """队列模式（v1.1）：第二行显示识别进度，总结按钮常驻。"""
        self._queue_mode = enabled
        if enabled:
            self.setFixedSize(340, 48)
            self.lbl_current.setText("已识别/已累计：0/0")
        else:
            self.setFixedSize(300, 48)
            self.lbl_current.setText("--")
        self._sync_manual_widgets()

    def _sync_manual_widgets(self):
        # 队列模式下锚点总结是核心交互，auto/manual 都显示总结按钮与计数
        show = self._manual_mode or self._queue_mode
        self.lbl_count.setVisible(show)
        self.btn_summarize.setVisible(show)

    def update_count(self, count: int):
        self.lbl_count.setText(f"{count} 句")

    def update_queue_progress(self, done: int, total: int, summary_waiting: int):
        """队列模式：显示 已识别/已累计 与待总结任务数。"""
        if not self._queue_mode:
            return
        text = f"已识别/已累计：{done}/{total}"
        if summary_waiting:
            text += f"　待总结 {summary_waiting}"
        self.lbl_current.setText(text)


# ---------------------------------------------------------------------------
# 消息提示（Toast）
# ---------------------------------------------------------------------------
class Toast(QWidget):
    """屏幕角落的浮动提示：几秒后自动淡出，也可手动点关闭。

    独立置顶小窗而非主窗口内的控件，这样全屏玩游戏时也能看到提示。
    """

    dismissed = pyqtSignal(object)

    def __init__(self, text: str, kind: str = "info", duration_ms: int = 4000):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._duration_ms = max(1000, duration_ms)
        self._closing = False
        # 悬停最多延后这么久，保证提示不会因为鼠标恰好停在上面就一直不消失
        self._hover_grace_ms = 6000
        self._hover_waited_ms = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        frame = QFrame()
        frame.setObjectName("ToastFrame")
        outer.addWidget(frame)
        self._shadow = make_shadow(frame, blur=18, dy=3)

        row = QHBoxLayout(frame)
        row.setContentsMargins(12, 9, 8, 9)
        row.setSpacing(8)

        self.dot = PulseDot()
        color_map = {
            "info": theme.CURRENT["accent"],
            "ok": theme.CURRENT["ok"],
            "warn": theme.CURRENT["warn"],
            "error": theme.CURRENT["danger"],
        }
        self.dot.set_state(color_map.get(kind, theme.CURRENT["accent"]), False)
        row.addWidget(self.dot)

        self.lbl = QLabel(text)
        set_cls(self.lbl, "toastText")
        self.lbl.setWordWrap(True)
        self.lbl.setMinimumWidth(230)
        row.addWidget(self.lbl, 1)

        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("ToastClose")
        self.btn_close.setToolTip("关闭提示")
        self.btn_close.clicked.connect(self.dismiss)
        row.addWidget(self.btn_close, 0, Qt.AlignmentFlag.AlignTop)

        self.setFixedWidth(300)
        self.adjustSize()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)

    def show_toast(self):
        self.show()
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(180)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.start()
        self._timer.start(self._duration_ms)

    def _on_timeout(self):
        # 鼠标停在提示上时延后关闭，方便看完长文本。
        # 这里用光标坐标与窗口几何直接判断，不用 underMouse()/enter-leave 事件：
        # 那套状态依赖事件成对送达，鼠标不动时会误判成一直悬停，提示就永远不消失。
        step_ms = 800
        if self._hover_waited_ms < self._hover_grace_ms \
                and self.frameGeometry().contains(QCursor.pos()):
            self._hover_waited_ms += step_ms
            self._timer.start(step_ms)
            return
        self.dismiss()

    def mousePressEvent(self, event):
        self.dismiss()

    def dismiss(self):
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        self._fade_out = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_out.setDuration(180)
        self._fade_out.setStartValue(self.windowOpacity())
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade_out.finished.connect(self._finish)
        self._fade_out.start()

    def _finish(self):
        self.dismissed.emit(self)
        self.close()
        self.deleteLater()


class ToastManager:
    """管理屏幕右下角的提示栈，新提示叠在上方，最多同时显示若干条。"""

    MAX_VISIBLE = 4
    MARGIN = 24
    GAP = 8

    def __init__(self):
        self._toasts: list = []

    def show(self, text: str, kind: str = "info", duration_ms: int = 4000):
        while len(self._toasts) >= self.MAX_VISIBLE:
            self._toasts[0].dismiss()
            if self._toasts and self._toasts[0] in self._toasts:
                self._toasts.pop(0)
        toast = Toast(text, kind=kind, duration_ms=duration_ms)
        toast.dismissed.connect(self._on_dismissed)
        self._toasts.append(toast)
        toast.show_toast()
        self._reposition()
        return toast

    def _on_dismissed(self, toast):
        if toast in self._toasts:
            self._toasts.remove(toast)
        self._reposition()

    def _reposition(self):
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        y = area.bottom() - self.MARGIN
        # 最新的提示贴近屏幕底部，旧的往上堆叠
        for toast in reversed(self._toasts):
            y -= toast.height()
            toast.move(area.right() - self.MARGIN - toast.width(), y)
            y -= self.GAP

    def clear(self):
        for toast in list(self._toasts):
            toast.dismiss()
        self._toasts.clear()


# ---------------------------------------------------------------------------
# 设置页面
# ---------------------------------------------------------------------------
class InstallPaddleEnvThread(QThread):
    """后台安装 paddle-cpu / paddle-gpu 旁路环境。"""

    progress = pyqtSignal(str)
    finished_ok = pyqtSignal()
    finished_err = pyqtSignal(str)

    def __init__(self, device: str, app_dir: str):
        super().__init__()
        self.device = device
        self.app_dir = app_dir

    def run(self):
        try:
            install_paddle_plugin(
                self.device,
                app_dir=self.app_dir,
                log=lambda msg: self.progress.emit(str(msg)),
            )
            self.finished_ok.emit()
        except Exception as e:
            self.finished_err.emit(str(e))


class SettingsPage(QWidget):
    """配置 LLM、OCR、主题等参数"""

    saved = pyqtSignal()

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.config = config
        self._paddle_installing = False
        self._loading = False
        self._init_ui()

    def _init_ui(self):
        page = QVBoxLayout(self)
        page.setContentsMargins(16, 14, 16, 14)
        page.setSpacing(12)
        page.addWidget(make_caption("设置 · SETTINGS"))

        card = make_card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)

        layout = QFormLayout()
        layout.setVerticalSpacing(12)
        layout.setHorizontalSpacing(16)
        card_layout.addLayout(layout)
        self.form_layout = layout

        self.txt_base_url = NoWheelLineEdit(self.config.get("base_url", ""))
        self.txt_api_key = NoWheelLineEdit(self.config.get("api_key", ""))
        self.txt_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_model = NoWheelLineEdit(self.config.get("model", ""))
        self.txt_tesseract_path = NoWheelLineEdit(self.config.get("tesseract_path", ""))

        self.cmb_ocr_lang = make_combo(editable=False)
        self._refresh_ocr_lang_options(
            normalize_backend(self.config.get("ocr_backend", "rapid")),
            preferred=self.config.get("ocr_lang", DEFAULT_OCR_LANG),
        )

        self.spin_ocr_interval = NoWheelSpinBox()
        self.spin_ocr_interval.setRange(200, 30000)
        self.spin_ocr_interval.setSuffix(" ms")
        self.spin_ocr_interval.setValue(self.config.get("ocr_interval_ms", 1500))

        self.spin_det_limit = NoWheelSpinBox()
        self.spin_det_limit.setRange(64, 800)
        self.spin_det_limit.setSingleStep(32)
        self.spin_det_limit.setSuffix(" px")
        self.spin_det_limit.setValue(self.config.get("det_limit_side_len", 256))

        self.spin_ocr_queue = NoWheelSpinBox()
        self.spin_ocr_queue.setRange(1, 20)
        self.spin_ocr_queue.setSuffix(" 帧")
        self.spin_ocr_queue.setValue(self.config.get("ocr_queue_size", 3))

        self.chk_queue_mode = QCheckBox("队列模式：后台持续识别，总结按锚点排队")
        self.chk_queue_mode.setChecked(self.config.get("queue_mode", True))
        self.chk_queue_mode.toggled.connect(self._on_queue_mode_toggled)

        self.spin_queue_backlog = NoWheelSpinBox()
        self.spin_queue_backlog.setRange(20, 2000)
        self.spin_queue_backlog.setSingleStep(20)
        self.spin_queue_backlog.setSuffix(" 帧")
        self.spin_queue_backlog.setValue(self.config.get("queue_backlog_limit", 300))
        self.spin_queue_backlog.setToolTip("识别队列最多积压的帧数，超出后丢弃最旧的帧")

        self.spin_frame_diff = NoWheelSpinBox()
        self.spin_frame_diff.setRange(0, 64)
        self.spin_frame_diff.setValue(
            self.config.get("frame_diff_threshold", FRAME_DIFF_THRESHOLD)
        )
        self.spin_frame_diff.setToolTip("两张截图的 dHash 汉明距离，小于等于该值视为同一画面")

        self.spin_text_sim = NoWheelDoubleSpinBox()
        self.spin_text_sim.setRange(0.50, 1.00)
        self.spin_text_sim.setSingleStep(0.05)
        self.spin_text_sim.setDecimals(2)
        self.spin_text_sim.setValue(
            self.config.get("text_similarity_threshold", TEXT_SIM_THRESHOLD)
        )
        self.spin_text_sim.setToolTip("新识别文本与上一句的相似度达到该值时视为重复并丢弃")

        self.spin_summary_count = NoWheelSpinBox()
        self.spin_summary_count.setRange(1, 999)
        self.spin_summary_count.setSuffix(" 句")
        self.spin_summary_count.setValue(self.config.get("summary_sentence_count", 50))

        self.spin_plot_context = NoWheelSpinBox()
        self.spin_plot_context.setRange(1000, 100000)
        self.spin_plot_context.setSingleStep(1000)
        self.spin_plot_context.setSuffix(" 字")
        self.spin_plot_context.setValue(self.config.get("plot_context_limit", 12000))

        self.cmb_ocr_backend = make_combo()
        current_backend = normalize_backend(self.config.get("ocr_backend", "rapid"))
        for backend_id, label in OCR_BACKEND_OPTIONS:
            self.cmb_ocr_backend.addItem(label, backend_id)
        idx = self.cmb_ocr_backend.findData(current_backend)
        self.cmb_ocr_backend.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_ocr_backend.currentIndexChanged.connect(self._on_backend_changed)

        self.cmb_summary_mode = make_combo()
        self.cmb_summary_mode.addItems(["auto", "manual"])
        self.cmb_summary_mode.setCurrentText(self.config.get("summary_mode", "auto"))
        self.cmb_summary_mode.currentTextChanged.connect(self._on_summary_mode_changed)

        self.chk_auto_key_info = QCheckBox("自动生成关键信息")
        self.chk_auto_key_info.setChecked(self.config.get("auto_extract_key_info", True))

        self.cmb_theme = make_combo()
        self.cmb_theme.addItems(["深色模式", "浅色模式"])
        self.cmb_theme.setCurrentIndex(
            0 if self.config.get("theme", "dark") == "dark" else 1
        )

        layout.addRow("界面主题:", self.cmb_theme)
        layout.addRow("OpenAI URL:", self.txt_base_url)
        layout.addRow("API Key:", self.txt_api_key)
        model_row = QWidget()
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(10)
        model_layout.addWidget(self.txt_model, 1)
        self.btn_test_llm = QPushButton("测试连接")
        self.btn_test_llm.clicked.connect(self._test_llm)
        model_layout.addWidget(self.btn_test_llm)
        layout.addRow("模型:", model_row)
        layout.addRow("OCR 后端:", self.cmb_ocr_backend)
        self.lbl_ocr_backend_hint = make_hint("")
        layout.addRow(self.lbl_ocr_backend_hint)

        # Paddle：仅显示环境检测；未通过时右侧出现「安装环境」
        self.paddle_plugin_container = QWidget()
        paddle_row = QHBoxLayout(self.paddle_plugin_container)
        paddle_row.setContentsMargins(0, 0, 0, 0)
        paddle_row.setSpacing(10)
        self.lbl_paddle_status = QLabel("环境检测：—")
        set_cls(self.lbl_paddle_status, "dim")
        self.lbl_paddle_status.setWordWrap(True)
        paddle_row.addWidget(self.lbl_paddle_status, 1)
        self.btn_install_paddle = QPushButton("安装环境")
        set_variant(self.btn_install_paddle, "primary")
        self.btn_install_paddle.clicked.connect(self._install_paddle_env)
        self.btn_install_paddle.setVisible(False)
        paddle_row.addWidget(self.btn_install_paddle, 0, Qt.AlignmentFlag.AlignRight)
        layout.addRow(self.paddle_plugin_container)

        layout.addRow("OCR 语言:", self.cmb_ocr_lang)

        # Tesseract 路径单独放在一个容器里，方便显示/隐藏（包括前面的标签）
        self.tesseract_container = QWidget()
        tesseract_form = QFormLayout(self.tesseract_container)
        tesseract_form.setContentsMargins(0, 0, 0, 0)
        tesseract_form.addRow("Tesseract 路径:", self.txt_tesseract_path)
        layout.addRow(self.tesseract_container)

        layout.addRow("OCR 间隔:", self.spin_ocr_interval)
        layout.addRow(make_hint(
            "每隔多久截图比对一次。越大越省 CPU 但容易漏句，越小越不容易漏句但无效识别变多。"
            "建议 500-2000 ms，默认 1000。"
        ))
        layout.addRow("检测分辨率:", self.spin_det_limit)
        layout.addRow(make_hint(
            "文字检测时缩放到的边长像素。越大越准但越慢，越小越快但小字容易漏。"
            "建议 128-384 px，默认 256。对 rapid 与 paddle 均生效。"
        ))
        layout.addRow("画面差异阈值:", self.spin_frame_diff)
        layout.addRow(make_hint(
            "判断两张截图是否算同一画面。越大越容易算同一张，跳过更多识别但可能漏句；"
            "越小越敏感，识别更全但同一句可能重复识别。建议 1-8，默认 3。"
        ))
        layout.addRow("文本重复阈值:", self.spin_text_sim)
        layout.addRow(make_hint(
            "判断识别结果与上一句是否算重复。越大越少判为重复，可能重复记录；"
            "越小越易判为重复，可能误删相似台词。建议 0.75-0.95，默认 0.85。"
        ))
        self.chk_save_ocr_text = QCheckBox("保留识别原文")
        self.chk_save_ocr_text.setChecked(bool(self.config.get("save_ocr_text", False)))
        ocr_save_row = QWidget()
        ocr_save_layout = QHBoxLayout(ocr_save_row)
        ocr_save_layout.setContentsMargins(0, 0, 0, 0)
        ocr_save_layout.setSpacing(10)
        ocr_save_layout.addWidget(self.chk_save_ocr_text)
        ocr_save_layout.addStretch()
        btn_open_app_dir = QPushButton("打开软件目录")
        set_variant(btn_open_app_dir, "ghost")
        btn_open_app_dir.clicked.connect(self._open_app_dir)
        ocr_save_layout.addWidget(btn_open_app_dir)
        layout.addRow(ocr_save_row)
        layout.addRow(make_hint(
            "勾选后，开始识别时在程序所在目录的「-游戏原文保存」里，按「游戏名_剧情树名.txt」追加每次识别结果。"
            "同一游戏同一剧情树再次开始识别会继续写入同一文件，不会另建新文件。"
        ))
        layout.addRow(self.chk_queue_mode)

        # 直接放进主表单，和其它输入框共用列宽。
        # 嵌套表单在「先隐藏再显示」时，QSpinBox 会被压成一条，改值后才撑开。
        self._row_queue_backlog = layout.rowCount()
        layout.addRow("队列积压上限:", self.spin_queue_backlog)
        self._row_queue_backlog_hint = layout.rowCount()
        layout.addRow(make_hint(
            "识别队列最多积压多少帧，超出后丢弃最旧的帧。越大越能追赶长时间的积压，"
            "越小越省内存但更容易丢帧。建议 100-500 帧，默认 300。"
        ))

        self._row_legacy_queue = layout.rowCount()
        layout.addRow("OCR 队列上限:", self.spin_ocr_queue)
        self._row_legacy_queue_hint = layout.rowCount()
        layout.addRow(make_hint(
            "旧版管线的待识别帧上限，满了会丢掉最旧的帧。越大越能容忍识别慢，"
            "越小越省内存但更容易丢帧。建议 1-5 帧，默认 3。"
        ))

        layout.addRow("总结模式:", self.cmb_summary_mode)

        # 总结句数只在自动模式下显示
        self.summary_count_container = QWidget()
        summary_count_form = QFormLayout(self.summary_count_container)
        summary_count_form.setContentsMargins(0, 0, 0, 0)
        summary_count_form.addRow("总结句数:", self.spin_summary_count)
        layout.addRow(self.summary_count_container)
        layout.addRow("剧情上下文上限:", self.spin_plot_context)
        layout.addRow(make_hint(
            "总结时带上当前链路里已经写好的剧情，超过该字数就从最早的段落裁掉。"
            "不包括这次要总结的识别原文。默认 12000 字。"
        ))

        layout.addRow(self.chk_auto_key_info)

        self._on_backend_changed()
        self._on_summary_mode_changed(self.cmb_summary_mode.currentText())
        self._on_queue_mode_changed(self.chk_queue_mode.isChecked())
        self._bind_autosave()

        # 设置项带说明后内容较长，放进滚动区避免小窗口下被裁掉
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 6, 0)
        holder_layout.setSpacing(0)
        holder_layout.addWidget(card)
        holder_layout.addStretch()
        scroll.setWidget(holder)
        page.addWidget(scroll, 1)

    def _current_backend(self) -> str:
        data = self.cmb_ocr_backend.currentData()
        if isinstance(data, str) and data:
            return data
        return normalize_backend(self.cmb_ocr_backend.currentText())

    def _refresh_ocr_lang_options(self, backend: str, preferred: str = ""):
        """按 OCR 后端刷新语言下拉；无效语言回退中文简体。"""
        langs = langs_for_backend(backend)
        want = resolve_ocr_lang(backend, preferred or self.config.get("ocr_lang", DEFAULT_OCR_LANG))
        self.cmb_ocr_lang.blockSignals(True)
        self.cmb_ocr_lang.clear()
        for code in langs:
            label = OCR_LANG_LABELS.get(code, code)
            self.cmb_ocr_lang.addItem(f"{label} ({code})", code)
        idx = self.cmb_ocr_lang.findData(want)
        self.cmb_ocr_lang.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_ocr_lang.blockSignals(False)
        # Rapid 仅中文模型时禁用选择，避免误解
        self.cmb_ocr_lang.setEnabled(len(langs) > 1)

    def _current_ocr_lang(self) -> str:
        data = self.cmb_ocr_lang.currentData()
        if isinstance(data, str) and data:
            return data
        text = self.cmb_ocr_lang.currentText().strip()
        # 兼容 "中文简体 (chi_sim)" 展示
        if "(" in text and text.endswith(")"):
            inner = text[text.rfind("(") + 1 : -1].strip()
            if inner:
                return inner
        return text or DEFAULT_OCR_LANG

    def _on_backend_changed(self, *_args):
        backend = self._current_backend()
        self.lbl_ocr_backend_hint.setText(
            OCR_BACKEND_HINTS.get(backend, OCR_BACKEND_HINTS["rapid"])
        )
        self.tesseract_container.setVisible(backend == "tesseract")
        is_paddle = backend in (BACKEND_CPU, BACKEND_GPU)
        self.paddle_plugin_container.setVisible(is_paddle)
        self._refresh_ocr_lang_options(backend, preferred=self._current_ocr_lang())
        if is_paddle:
            self._refresh_paddle_status()
        else:
            self.btn_install_paddle.setVisible(False)

    def _refresh_paddle_status(self):
        backend = self._current_backend()
        if backend not in (BACKEND_CPU, BACKEND_GPU):
            self.lbl_paddle_status.setText("环境检测：—")
            self.btn_install_paddle.setVisible(False)
            return
        if getattr(self, "_paddle_installing", False):
            return
        status = environment_ready(backend_to_device(backend), APP_DIR)
        if status.installed:
            self.lbl_paddle_status.setText("环境检测：已通过")
            self.btn_install_paddle.setVisible(False)
        else:
            self.lbl_paddle_status.setText("环境检测：未通过")
            self.btn_install_paddle.setVisible(True)
            self.btn_install_paddle.setEnabled(True)
            self.btn_install_paddle.setToolTip(status.detail)

    def _install_paddle_env(self):
        backend = self._current_backend()
        if backend not in (BACKEND_CPU, BACKEND_GPU):
            return
        device = backend_to_device(backend)
        tip = (
            f"将安装 {backend} 旁路环境到：\n{os.path.join(APP_DIR, f'paddle_ocr_{device}')}\n\n"
            "将固定安装 paddleocr 3.7 + PP-OCRv6_small。\n"
            "若以前装过旧版，请先删除该目录再装。\n"
            "体积较大，可能需要数分钟，期间请保持网络畅通。"
        )
        if device == "gpu":
            tip += "\nGPU 版需本机已正确安装对应 CUDA。"
        reply = QMessageBox.question(
            self,
            "安装 Paddle 环境",
            tip + "\n\n确定开始安装吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._paddle_installing = True
        self.btn_install_paddle.setEnabled(False)
        self.lbl_paddle_status.setText(
            PADDLE_INSTALL_DOWNLOAD_HINT.get(device, PADDLE_INSTALL_DOWNLOAD_HINT["cpu"])
        )
        self._paddle_install_thread = InstallPaddleEnvThread(device, APP_DIR)
        self._paddle_install_thread.finished_ok.connect(self._on_paddle_install_ok)
        self._paddle_install_thread.finished_err.connect(self._on_paddle_install_err)
        self._paddle_install_thread.start()

    def _on_paddle_install_ok(self):
        self._paddle_installing = False
        self._refresh_paddle_status()
        QMessageBox.information(self, "安装完成", "Paddle 环境已安装，环境检测已通过。")

    def _on_paddle_install_err(self, msg: str):
        self._paddle_installing = False
        self._refresh_paddle_status()
        QMessageBox.critical(self, "安装失败", msg)

    def _on_summary_mode_changed(self, mode: str):
        self.summary_count_container.setVisible(mode == "auto")

    def _on_queue_mode_toggled(self, enabled: bool):
        if enabled:
            self.spin_queue_backlog.setValue(300)
        else:
            self.spin_ocr_queue.setValue(3)
        self._on_queue_mode_changed(enabled)

    def _on_queue_mode_changed(self, enabled: bool):
        form = self.form_layout
        form.setRowVisible(self._row_queue_backlog, enabled)
        form.setRowVisible(self._row_queue_backlog_hint, enabled)
        form.setRowVisible(self._row_legacy_queue, not enabled)
        form.setRowVisible(self._row_legacy_queue_hint, not enabled)

    def _open_app_dir(self):
        try:
            os.startfile(APP_DIR)
        except Exception as e:
            QMessageBox.critical(self, "无法打开目录", f"{APP_DIR}\n\n{e}")

    def _test_llm(self):
        base_url = self.txt_base_url.text().strip()
        api_key = self.txt_api_key.text().strip()
        model = self.txt_model.text().strip()
        if not base_url or not api_key or not model or "您的API地址" in base_url:
            QMessageBox.information(self, "无法测试", "请先填写 OpenAI URL、API Key 和模型。")
            return
        self._save()
        self.btn_test_llm.setEnabled(False)
        self.btn_test_llm.setText("测试中…")
        self._llm_test_thread = TestLLMThread({
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
        })
        self._llm_test_thread.result_ready.connect(self._on_llm_test_ok)
        self._llm_test_thread.error.connect(self._on_llm_test_err)
        self._llm_test_thread.finished.connect(self._on_llm_test_finished)
        self._llm_test_thread.start()

    def _on_llm_test_ok(self, text: str):
        QMessageBox.information(self, "连接成功", f"模型已响应：\n{text}")

    def _on_llm_test_err(self, msg: str):
        QMessageBox.critical(self, "连接失败", msg or "连接失败")

    def _on_llm_test_finished(self):
        self.btn_test_llm.setEnabled(True)
        self.btn_test_llm.setText("测试连接")

    def _bind_autosave(self):
        for edit in (self.txt_base_url, self.txt_api_key, self.txt_model, self.txt_tesseract_path):
            edit.editingFinished.connect(self._save)
        for spin in (
            self.spin_ocr_interval,
            self.spin_det_limit,
            self.spin_ocr_queue,
            self.spin_queue_backlog,
            self.spin_frame_diff,
            self.spin_summary_count,
            self.spin_plot_context,
        ):
            spin.valueChanged.connect(self._save)
        self.spin_text_sim.valueChanged.connect(self._save)
        self.cmb_ocr_backend.currentIndexChanged.connect(self._save)
        self.cmb_ocr_lang.currentIndexChanged.connect(self._save)
        self.cmb_summary_mode.currentTextChanged.connect(self._save)
        self.cmb_theme.currentIndexChanged.connect(self._save)
        self.chk_queue_mode.toggled.connect(self._save)
        self.chk_auto_key_info.toggled.connect(self._save)
        self.chk_save_ocr_text.toggled.connect(self._save)

    def _save(self, *_args):
        if self._loading:
            return
        backend = self._current_backend()
        prev_backend = normalize_backend(self.config.get("ocr_backend", "rapid"))
        if backend != prev_backend and backend in (BACKEND_CPU, BACKEND_GPU):
            status = environment_ready(backend_to_device(backend), APP_DIR)
            if not status.installed:
                reply = QMessageBox.question(
                    self,
                    "环境未通过",
                    f"当前选择了 {backend}，但环境检测未通过。\n\n"
                    f"{status.detail}\n\n"
                    "可先点右侧「安装环境」。仍要使用该设置吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    self._loading = True
                    idx = self.cmb_ocr_backend.findData(prev_backend)
                    self.cmb_ocr_backend.setCurrentIndex(idx if idx >= 0 else 0)
                    self._on_backend_changed()
                    self._loading = False
                    return
        payload = {
            "base_url": self.txt_base_url.text().strip(),
            "api_key": self.txt_api_key.text().strip(),
            "model": self.txt_model.text().strip(),
            "ocr_lang": resolve_ocr_lang(backend, self._current_ocr_lang()),
            "tesseract_path": self.txt_tesseract_path.text().strip(),
            "ocr_interval_ms": self.spin_ocr_interval.value(),
            "det_limit_side_len": self.spin_det_limit.value(),
            "ocr_queue_size": self.spin_ocr_queue.value(),
            "queue_mode": self.chk_queue_mode.isChecked(),
            "queue_backlog_limit": self.spin_queue_backlog.value(),
            "frame_diff_threshold": self.spin_frame_diff.value(),
            "text_similarity_threshold": round(self.spin_text_sim.value(), 2),
            "summary_sentence_count": self.spin_summary_count.value(),
            "plot_context_limit": self.spin_plot_context.value(),
            "ocr_backend": backend,
            "summary_mode": self.cmb_summary_mode.currentText(),
            "auto_extract_key_info": self.chk_auto_key_info.isChecked(),
            "save_ocr_text": self.chk_save_ocr_text.isChecked(),
            "theme": "dark" if self.cmb_theme.currentIndex() == 0 else "light",
        }
        if all(self.config.get(key) == value for key, value in payload.items()):
            return
        self.config.update(payload)
        config_path = os.path.join(APP_DIR, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)
        self.saved.emit()


# ---------------------------------------------------------------------------
# 游戏/线路管理页面
# ---------------------------------------------------------------------------
class GamePromptDialog(BaseDialog):
    """编辑游戏的追加提示词（总结/信息提取时拼入 system prompt）"""

    def __init__(self, game_name: str = "", prompt: str = "", parent=None):
        super().__init__("追加提示词", parent)
        self.resize(520, 360)
        hint = QLabel("总结与信息提取时拼到提示词末尾；可在此要求纠正 OCR 错字、还原专有名词等。")
        set_cls(hint, "dim")
        hint.setWordWrap(True)
        self.body.addWidget(hint)
        self.body.addWidget(make_caption(f"游戏 · {game_name or '未命名游戏'}"))
        self.txt_prompt = QTextEdit()
        self.txt_prompt.setAcceptRichText(False)
        self.txt_prompt.setPlainText(prompt)
        self.txt_prompt.setPlaceholderText("例如：这是《某某》游戏，角色名包含 XX，OCR 常见错字把…识别成…，请纠正。")
        self.body.addWidget(self.txt_prompt, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        set_variant(btn_cancel, "ghost")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        btn_save = QPushButton("保存")
        set_variant(btn_save, "primary")
        btn_save.clicked.connect(self._save)
        btn_row.addWidget(btn_save)
        self.body.addLayout(btn_row)

    def _save(self):
        self.prompt = self.txt_prompt.toPlainText().strip()
        self.accept()


class GameRoutePage(QWidget):
    data_changed = pyqtSignal()          # 任意增删改后发出，用于刷新侧边栏下拉框
    tree_chosen = pyqtSignal(int, int)   # (game_id, tree_id) 设为当前剧情树

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.current_game_id: Optional[int] = None
        self.trees: List[dict] = []
        self._init_ui()
        self._load_games()

    def set_current(self, game_id: Optional[int]):
        self.current_game_id = game_id

    def reload(self):
        self._load_games()

    def _init_ui(self):
        page = QVBoxLayout(self)
        page.setContentsMargins(16, 14, 16, 14)
        page.setSpacing(12)
        page.addWidget(make_caption("游戏 / 剧情树 · MANAGE"))

        layout = QHBoxLayout()
        layout.setSpacing(16)
        page.addLayout(layout, 1)

        # 游戏列表
        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(make_caption("游戏 · GAMES"))
        self.list_games = QListWidget()
        self.list_games.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_games.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_games.model().rowsMoved.connect(self._on_games_reordered)
        self.list_games.currentRowChanged.connect(self._on_game_changed)
        left.addWidget(self.list_games, 1)

        game_btns = QHBoxLayout()
        game_btns.setSpacing(6)
        btn_add_game = QPushButton("新建")
        btn_add_game.clicked.connect(self._add_game)
        btn_rename_game = QPushButton("重命名")
        btn_rename_game.clicked.connect(self._rename_game)
        btn_prompt = QPushButton("提示词")
        btn_prompt.clicked.connect(self._edit_game_prompt)
        btn_del_game = QPushButton("删除")
        set_variant(btn_del_game, "danger")
        btn_del_game.clicked.connect(self._delete_game)
        game_btns.addWidget(btn_add_game)
        game_btns.addWidget(btn_rename_game)
        game_btns.addWidget(btn_prompt)
        game_btns.addWidget(btn_del_game)
        left.addLayout(game_btns)
        io_btns = QHBoxLayout()
        io_btns.setSpacing(6)
        btn_export_data = QPushButton("导出数据")
        btn_export_data.clicked.connect(self._export_bundle)
        btn_import_data = QPushButton("导入游戏")
        btn_import_data.clicked.connect(self._import_bundle)
        io_btns.addWidget(btn_export_data)
        io_btns.addWidget(btn_import_data)
        left.addLayout(io_btns)

        # 剧情树与旧线路
        right = QVBoxLayout()
        right.setSpacing(8)
        self.manage_tabs = QTabWidget()
        right.addWidget(self.manage_tabs, 1)

        tree_tab = QWidget()
        tree_layout = QVBoxLayout(tree_tab)
        tree_layout.setContentsMargins(0, 8, 0, 0)
        tree_layout.setSpacing(8)
        tree_layout.addWidget(make_caption("剧情树 · TREES"))
        self.list_trees = QListWidget()
        tree_layout.addWidget(self.list_trees, 1)
        tree_btns = QHBoxLayout()
        tree_btns.setSpacing(6)
        btn_add_tree = QPushButton("新建")
        btn_add_tree.clicked.connect(self._add_tree)
        btn_rename_tree = QPushButton("重命名")
        btn_rename_tree.clicked.connect(self._rename_tree)
        btn_tree_prompt = QPushButton("提示词")
        btn_tree_prompt.clicked.connect(self._edit_tree_prompt)
        btn_del_tree = QPushButton("删除")
        set_variant(btn_del_tree, "danger")
        btn_del_tree.clicked.connect(self._delete_tree)
        tree_btns.addWidget(btn_add_tree)
        tree_btns.addWidget(btn_rename_tree)
        tree_btns.addWidget(btn_tree_prompt)
        tree_btns.addWidget(btn_del_tree)
        tree_layout.addLayout(tree_btns)
        btn_export_tree = QPushButton("导出这棵剧情树")
        set_variant(btn_export_tree, "primary")
        btn_export_tree.clicked.connect(self._export_tree)
        tree_layout.addWidget(btn_export_tree)
        btn_select_tree = QPushButton("✓  设为当前剧情树")
        set_variant(btn_select_tree, "primary")
        btn_select_tree.clicked.connect(self._choose_tree)
        tree_layout.addWidget(btn_select_tree)
        self.manage_tabs.addTab(tree_tab, "剧情树")

        route_tab = QWidget()
        route_layout = QVBoxLayout(route_tab)
        route_layout.setContentsMargins(0, 8, 0, 0)
        route_layout.setSpacing(8)
        route_layout.addWidget(make_caption("旧线路 · ROUTES"))
        route_layout.addWidget(make_hint("这里是改版前的线路存档，只供查看和导出。新的总结写在剧情树上。"))
        self.list_routes = QListWidget()
        self.list_routes.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_routes.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_routes.model().rowsMoved.connect(self._on_routes_reordered)
        route_layout.addWidget(self.list_routes, 1)
        route_btns = QHBoxLayout()
        route_btns.setSpacing(6)
        btn_rename_route = QPushButton("重命名")
        btn_rename_route.clicked.connect(self._rename_route)
        btn_del_route = QPushButton("删除")
        set_variant(btn_del_route, "danger")
        btn_del_route.clicked.connect(self._delete_route)
        route_btns.addWidget(btn_rename_route)
        route_btns.addWidget(btn_del_route)
        route_layout.addLayout(route_btns)
        btn_export_game = QPushButton("导出所有旧线路")
        btn_export_game.clicked.connect(self._export_game)
        route_layout.addWidget(btn_export_game)
        btn_export = QPushButton("导出选中旧线路")
        btn_export.clicked.connect(self._export_route)
        route_layout.addWidget(btn_export)
        self.manage_tabs.addTab(route_tab, "旧线路")

        layout.addLayout(left, 1)
        layout.addLayout(right, 1)

        self.list_games.itemDoubleClicked.connect(self._on_game_double_clicked)

    def _load_games(self):
        self.list_games.clear()
        self.games = self.db.list_games()
        for game in self.games:
            item = QListWidgetItem(game["name"])
            item.setData(Qt.ItemDataRole.UserRole, game["id"])
            self.list_games.addItem(item)
        if self.games:
            # 默认选中当前游戏或第一个
            for idx, game in enumerate(self.games):
                if game["id"] == self.current_game_id:
                    self.list_games.setCurrentRow(idx)
                    return
            self.list_games.setCurrentRow(0)

    def _on_game_changed(self):
        self.list_routes.clear()
        self.list_trees.clear()
        self.routes = []
        self.trees = []
        row = self.list_games.currentRow()
        if row < 0 or row >= len(self.games):
            return
        game_id = self.games[row]["id"]
        self.routes = self.db.list_routes(game_id)
        for route in self.routes:
            item = QListWidgetItem(route["name"])
            item.setData(Qt.ItemDataRole.UserRole, route["id"])
            self.list_routes.addItem(item)
        self.trees = self.db.list_story_trees(game_id)
        for tree in self.trees:
            item = QListWidgetItem(tree["name"])
            item.setData(Qt.ItemDataRole.UserRole, tree["id"])
            self.list_trees.addItem(item)

    def _on_game_double_clicked(self, item):
        self._on_game_changed()

    def _on_games_reordered(self, *args):
        ids = [
            self.list_games.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list_games.count())
        ]
        self.db.set_game_order(ids)
        self.games = self.db.list_games()
        # 拖动后行号变化，重建线路列表保证与当前选中游戏一致
        self._on_game_changed()
        self.data_changed.emit()

    def _on_routes_reordered(self, *args):
        ids = [
            self.list_routes.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list_routes.count())
        ]
        self.db.set_route_order(ids)
        row = self.list_games.currentRow()
        if 0 <= row < len(self.games):
            self.routes = self.db.list_routes(self.games[row]["id"])
        self.data_changed.emit()

    def _add_game(self):
        name, ok = QInputDialog.getText(self, "新建游戏", "游戏名称：")
        if ok and name.strip():
            game_id = self.db.add_game(name.strip())
            try:
                self.db.add_story_tree(game_id, "本篇")
            except StoryTreeError as exc:
                QMessageBox.warning(self, "未能创建剧情树", str(exc))
            self.current_game_id = game_id
            self._load_games()
            self.data_changed.emit()

    def _add_tree(self):
        row = self.list_games.currentRow()
        if row < 0:
            QMessageBox.warning(self, "未选择游戏", "请先选择一个游戏。")
            return
        name, ok = QInputDialog.getText(self, "新建剧情树", "名称（例如：本篇、外传）：")
        if not ok or not name.strip():
            return
        try:
            self.db.add_story_tree(self.games[row]["id"], name.strip())
        except StoryTreeError as exc:
            QMessageBox.warning(self, "未能创建剧情树", str(exc))
            return
        self._on_game_changed()
        self.data_changed.emit()

    def _rename_tree(self):
        row = self.list_trees.currentRow()
        if row < 0 or row >= len(self.trees):
            return
        tree = self.trees[row]
        name, ok = QInputDialog.getText(self, "重命名剧情树", "名称：", text=tree["name"])
        if not ok or not name.strip():
            return
        try:
            self.db.rename_story_tree(tree["id"], name.strip())
        except StoryTreeError as exc:
            QMessageBox.warning(self, "未能重命名", str(exc))
            return
        self._on_game_changed()
        self.data_changed.emit()

    def _delete_tree(self):
        row = self.list_trees.currentRow()
        if row < 0 or row >= len(self.trees):
            return
        tree = self.trees[row]
        reply = QMessageBox.question(self, "确认删除", f"确定删除剧情树《{tree['name']}》吗？树上的剧情会一起删除。")
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.db.delete_story_tree(tree["id"])
        self._on_game_changed()
        self.data_changed.emit()

    def _edit_tree_prompt(self):
        row = self.list_trees.currentRow()
        if row < 0 or row >= len(self.trees):
            return
        tree = self.trees[row]
        dialog = GamePromptDialog(tree["name"], tree.get("prompt") or "", parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.db.update_story_tree_prompt(tree["id"], dialog.prompt)
            self._on_game_changed()
            self.data_changed.emit()

    def _choose_tree(self):
        g_row = self.list_games.currentRow()
        t_row = self.list_trees.currentRow()
        if g_row < 0 or t_row < 0 or t_row >= len(self.trees):
            QMessageBox.information(self, "未选择剧情树", "请先选中一个游戏和一棵剧情树。")
            return
        self.tree_chosen.emit(self.games[g_row]["id"], self.trees[t_row]["id"])

    def _export_tree(self):
        t_row = self.list_trees.currentRow()
        if t_row < 0 or t_row >= len(getattr(self, "trees", [])):
            QMessageBox.information(self, "未选择剧情树", "请先选中一棵剧情树。")
            return
        try:
            path = export_story_tree_markdown(self.db, self.trees[t_row]["id"], APP_DIR)
            QMessageBox.information(self, "导出成功", f"已导出到：\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", f"导出时出错：{exc}")

    def _rename_game(self):
        row = self.list_games.currentRow()
        if row < 0:
            return
        game = self.games[row]
        name, ok = QInputDialog.getText(self, "重命名游戏", "名称：", text=game["name"])
        if ok and name.strip():
            self.db.rename_game(game["id"], name.strip())
            self._load_games()
            self.data_changed.emit()

    def _delete_game(self):
        row = self.list_games.currentRow()
        if row < 0:
            return
        game = self.games[row]
        reply = QMessageBox.question(self, "确认删除", f"确定删除游戏《{game['name']}》及其剧情树、旧线路和记录吗？")
        if reply == QMessageBox.StandardButton.Yes:
            self.db.delete_game(game["id"])
            self._load_games()
            self.data_changed.emit()

    def _edit_game_prompt(self):
        row = self.list_games.currentRow()
        if row < 0:
            return
        game = self.games[row]
        dialog = GamePromptDialog(game["name"], game.get("prompt", ""), parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.db.update_game_prompt(game["id"], dialog.prompt)
            game["prompt"] = dialog.prompt
            self.data_changed.emit()

    def _rename_route(self):
        row = self.list_routes.currentRow()
        if row < 0:
            return
        route = self.routes[row]
        name, ok = QInputDialog.getText(self, "重命名线路", "名称：", text=route["name"])
        if ok and name.strip():
            self.db.rename_route(route["id"], name.strip())
            self._on_game_changed()
            self.data_changed.emit()

    def _delete_route(self):
        row = self.list_routes.currentRow()
        if row < 0:
            return
        route = self.routes[row]
        reply = QMessageBox.question(self, "确认删除", f"确定删除线路《{route['name']}》吗？")
        if reply == QMessageBox.StandardButton.Yes:
            self.db.delete_route(route["id"])
            self._on_game_changed()
            self.data_changed.emit()

    def _export_route(self):
        g_row = self.list_games.currentRow()
        r_row = self.list_routes.currentRow()
        if g_row < 0 or r_row < 0:
            QMessageBox.information(self, "未选择线路", "请先选中一个游戏和一条线路。")
            return
        game_id = self.games[g_row]["id"]
        route_id = self.routes[r_row]["id"]
        try:
            path = export_route_markdown(self.db, game_id, route_id, APP_DIR)
            QMessageBox.information(self, "导出成功", f"已导出到：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"导出时出错：{e}")

    def _export_game(self):
        g_row = self.list_games.currentRow()
        if g_row < 0:
            QMessageBox.information(self, "未选择游戏", "请先选中一个游戏。")
            return
        game = self.games[g_row]
        try:
            path, capped = export_game_markdown(self.db, game["id"], APP_DIR)
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"导出时出错：{e}")
            return
        message = f"已导出到：\n{path}"
        if capped:
            names = "、".join(capped)
            message += f"\n\n以下线路已达到 999 条上限，更后面的章节未包含：\n{names}"
        QMessageBox.information(self, "导出成功", message)

    def _export_bundle(self):
        g_row = self.list_games.currentRow()
        if g_row < 0:
            QMessageBox.information(self, "未选择游戏", "请先选中一个游戏。")
            return
        game = self.games[g_row]
        safe = "".join(ch for ch in (game.get("name") or "") if ch not in '\\/:*?"<>|').strip() or "游戏"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出游戏数据",
            os.path.join(APP_DIR, f"{safe}.galcoplit.json"),
            "GalCoplit 游戏数据 (*.galcoplit.json)",
        )
        if not path:
            return
        if not path.lower().endswith(".galcoplit.json"):
            path += ".galcoplit.json"
        try:
            data = self.db.export_game_bundle(game["id"])
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", f"导出时出错：{exc}")
            return
        QMessageBox.information(
            self,
            "导出成功",
            f"已导出到：\n{path}\n\n这份数据可以在另一台电脑上用「导入游戏」打开。Markdown 导出仍然保留。",
        )

    def _import_bundle(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入游戏",
            APP_DIR,
            "GalCoplit 游戏数据 (*.galcoplit.json);;JSON (*.json)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            created = self.db.import_game_bundle(data)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        self.current_game_id = created["id"]
        self.reload()
        self.data_changed.emit()
        QMessageBox.information(self, "导入成功", f"已导入游戏《{created['name']}》。")


# ---------------------------------------------------------------------------
# 剧情总结时间线页面
# ---------------------------------------------------------------------------
class CopySummaryDialog(BaseDialog):
    def __init__(self, routes: List[dict], parent=None):
        super().__init__("复制到其他线路", parent)
        self.resize(440, 240)
        hint = QLabel("把当前选中的这一段追加到目标线路末尾。目标线路还没有剧情时，它会成为第一章。")
        hint.setWordWrap(True)
        set_cls(hint, "dim")
        self.body.addWidget(hint)
        self.combo = make_combo()
        for route in routes:
            self.combo.addItem(route.get("name") or "未命名线路", route["id"])
        self.body.addWidget(QLabel("目标线路"))
        self.body.addWidget(self.combo)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        set_variant(btn_cancel, "ghost")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        btn_ok = QPushButton("复制")
        set_variant(btn_ok, "primary")
        btn_ok.clicked.connect(self.accept)
        btn_row.addWidget(btn_ok)
        self.body.addLayout(btn_row)

    def selected_route_id(self) -> Optional[int]:
        value = self.combo.currentData()
        return int(value) if value is not None else None


class TimelineList(QListWidget):
    """按住约 1 秒后才允许拖动，避免和点击选中冲突。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.can_drag = None
        self.on_drag_blocked = None
        self._press_pos: Optional[QPoint] = None
        self._armed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._arm_drag)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)

    def mousePressEvent(self, event):
        self._armed = False
        self._timer.stop()
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            if self.indexAt(self._press_pos).isValid():
                self._timer.start(1000)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self._armed:
            if self._press_pos is not None:
                moved = event.position().toPoint() - self._press_pos
                if moved.manhattanLength() >= QApplication.startDragDistance():
                    self._timer.stop()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._timer.stop()
        self._armed = False
        self._press_pos = None
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def _arm_drag(self):
        if self.can_drag is not None and not self.can_drag():
            if self.on_drag_blocked is not None:
                self.on_drag_blocked()
            return
        self._armed = True
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def dropEvent(self, event):
        super().dropEvent(event)
        self._armed = False
        self.unsetCursor()


class TimelinePage(QWidget):
    reordered = pyqtSignal()
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.route_id: Optional[int] = None
        self.summaries: List[dict] = []
        self._init_ui()

    def set_route(self, route_id: Optional[int]):
        self.route_id = route_id
        self.reload()

    def reload(self):
        self._load_summaries()

    def _sync_action_buttons(self):
        has_route = self.route_id is not None
        has_item = 0 <= self.list_widget.currentRow() < len(self.summaries)
        self.btn_add.setEnabled(has_route)
        self.btn_delete.setEnabled(has_item)
        self.btn_copy.setEnabled(has_item)

    def _editing(self) -> bool:
        return not self.detail.isReadOnly()

    def _block_if_editing(self) -> bool:
        if not self._editing():
            return False
        QMessageBox.information(self, "正在编辑", "请先保存或取消当前编辑。")
        return True

    def _init_ui(self):
        self.list_widget = TimelineList()
        self.list_widget.can_drag = lambda: not self._editing()
        self.list_widget.on_drag_blocked = self._block_if_editing
        self.list_widget.setSpacing(2)
        self.list_widget.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list_widget.currentRowChanged.connect(self._on_selection_changed)
        self.list_widget.model().rowsMoved.connect(self._on_summaries_reordered)

        self.edit_title = QLineEdit()
        self.edit_title.setObjectName("DetailTitle")
        self.edit_title.setReadOnly(True)
        self.edit_title.setPlaceholderText("")

        self.lbl_meta = QLabel("")
        set_cls(self.lbl_meta, "tlMeta")

        self.detail = QTextEdit()
        self.detail.setObjectName("ReadingArea")
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("点击左侧条目查看完整总结")

        self.btn_edit = QPushButton("编辑")
        self.btn_edit.clicked.connect(self._toggle_edit_mode)

        self.btn_save = QPushButton("保存")
        set_variant(self.btn_save, "primary")
        self.btn_save.clicked.connect(self._save_summary)
        self.btn_save.setEnabled(False)

        self.btn_cancel_edit = QPushButton("取消")
        set_variant(self.btn_cancel_edit, "ghost")
        self.btn_cancel_edit.clicked.connect(self._cancel_edit)
        self.btn_cancel_edit.setEnabled(False)

        self.btn_add = QPushButton("新增")
        set_variant(self.btn_add, "ghost")
        self.btn_add.setEnabled(False)
        self.btn_add.clicked.connect(self._add_chapter)
        self.btn_delete = QPushButton("删除")
        set_variant(self.btn_delete, "danger")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._delete_chapter)
        self.btn_copy = QPushButton("复制到其他线路")
        set_variant(self.btn_copy, "ghost")
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self._copy_to_route)

        page = QVBoxLayout(self)
        page.setContentsMargins(16, 14, 16, 14)
        page.setSpacing(12)
        page.addWidget(make_caption("剧情总结 · TIMELINE"))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 12, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(make_caption("时间线"))
        hint = QLabel("按住条目约 1 秒后拖动，可调整顺序")
        set_cls(hint, "dim")
        hint.setWordWrap(True)
        left_layout.addWidget(hint)
        left_layout.addWidget(self.list_widget)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(12, 0, 0, 0)
        right_layout.setSpacing(8)
        caption_row = QHBoxLayout()
        caption_row.setSpacing(8)
        caption_row.addWidget(make_caption("详细内容 · DETAIL"))
        caption_row.addStretch()
        caption_row.addWidget(self.lbl_meta)
        right_layout.addLayout(caption_row)
        right_layout.addWidget(self.edit_title)
        right_layout.addWidget(self.detail)

        detail_btn_layout = QHBoxLayout()
        detail_btn_layout.setSpacing(6)
        detail_btn_layout.addWidget(self.btn_edit)
        detail_btn_layout.addWidget(self.btn_save)
        detail_btn_layout.addWidget(self.btn_cancel_edit)
        detail_btn_layout.addStretch()
        detail_btn_layout.addWidget(self.btn_add)
        detail_btn_layout.addWidget(self.btn_delete)
        detail_btn_layout.addWidget(self.btn_copy)
        right_layout.addLayout(detail_btn_layout)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        page.addWidget(splitter, 1)

    def _load_summaries(self, select_id: Optional[int] = None):
        self._loading_list = True
        self.list_widget.clear()
        self.detail.clear()
        self.detail.setReadOnly(True)
        self.edit_title.clear()
        self.edit_title.setReadOnly(True)
        self.lbl_meta.clear()
        self.btn_edit.setEnabled(True)
        self.btn_save.setEnabled(False)
        self.btn_cancel_edit.setEnabled(False)
        if not self.route_id:
            self.summaries = []
            self.detail.setPlaceholderText("请先在左侧选择游戏与线路")
            self._loading_list = False
            self._sync_action_buttons()
            return
        self.detail.setPlaceholderText("点击左侧条目查看完整总结")
        self.summaries = self.db.list_summaries(self.route_id, limit=999)
        chosen = -1
        for index, summary in enumerate(self.summaries):
            item = QListWidgetItem(self._entry_title(summary))
            item.setData(Qt.ItemDataRole.UserRole, summary["id"])
            self.list_widget.addItem(item)
            if select_id is not None and summary["id"] == select_id:
                chosen = index
        if chosen >= 0:
            self.list_widget.setCurrentRow(chosen)
        self._loading_list = False
        self._sync_action_buttons()

    def _on_summaries_reordered(self, *_args):
        if getattr(self, "_loading_list", False):
            return
        ids = []
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            if item is None:
                continue
            summary_id = item.data(Qt.ItemDataRole.UserRole)
            if summary_id is not None:
                ids.append(int(summary_id))
        if not ids:
            return
        self.db.set_summary_order(ids)
        by_id = {summary["id"]: summary for summary in self.summaries}
        self.summaries = [by_id[summary_id] for summary_id in ids if summary_id in by_id]
        self.reordered.emit()

    @staticmethod
    def _entry_title(s: dict) -> str:
        title = (s.get("title") or "").strip()
        if not title:
            content = (s.get("content") or "").strip().replace("\n", " ")
            title = content[:14] + ("…" if len(content) > 14 else "")
        return title or "（无标题）"

    def _apply_reading_format(self):
        """给正文设置行高与段间距，提升阅读体验"""
        cursor = self.detail.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        fmt = QTextBlockFormat()
        fmt.setLineHeight(160, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        fmt.setBottomMargin(10)
        cursor.mergeBlockFormat(fmt)
        cursor.clearSelection()
        self.detail.moveCursor(QTextCursor.MoveOperation.Start)

    def _on_selection_changed(self):
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self.summaries):
            self.detail.clear()
            self.edit_title.clear()
            self.lbl_meta.clear()
            self._sync_action_buttons()
            return
        s = self.summaries[row]
        self.edit_title.setText(self._entry_title(s))
        created = datetime.datetime.fromisoformat(s["created_at"]).strftime("%Y-%m-%d %H:%M")
        self.lbl_meta.setText(created)
        self.detail.setPlainText(s["content"])
        self._apply_reading_format()
        self.current_summary_id = s["id"]
        self._sync_action_buttons()

    def _toggle_edit_mode(self):
        if self.list_widget.currentRow() < 0:
            return
        self.detail.setReadOnly(False)
        self.edit_title.setReadOnly(False)
        self.btn_edit.setEnabled(False)
        self.btn_save.setEnabled(True)
        self.btn_cancel_edit.setEnabled(True)
        self._original_detail = self.detail.toPlainText()
        self._original_title = self.edit_title.text()

    def _cancel_edit(self):
        self.detail.setPlainText(self._original_detail)
        self._apply_reading_format()
        self.edit_title.setText(self._original_title)
        self._exit_edit_mode()

    def _exit_edit_mode(self):
        self.detail.setReadOnly(True)
        self.edit_title.setReadOnly(True)
        self.btn_edit.setEnabled(True)
        self.btn_save.setEnabled(False)
        self.btn_cancel_edit.setEnabled(False)

    def _save_summary(self):
        row = self.list_widget.currentRow()
        if row < 0:
            return
        s = self.summaries[row]
        new_content = self.detail.toPlainText().strip()
        new_title = self.edit_title.text().strip()
        self.db.update_summary(s["id"], new_content)
        self.db.update_summary_title(s["id"], new_title)
        self.summaries[row]["content"] = new_content
        self.summaries[row]["title"] = new_title
        self.list_widget.item(row).setText(self._entry_title(self.summaries[row]))
        self.edit_title.setText(self._entry_title(self.summaries[row]))
        self._exit_edit_mode()

    def _selected_summary(self) -> Optional[dict]:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self.summaries):
            return None
        return self.summaries[row]

    def _add_chapter(self):
        if not self.route_id:
            QMessageBox.information(self, "未选择线路", "请先在左侧选择一条线路。")
            return
        if self._block_if_editing():
            return
        new_id = self.db.add_manual_chapter(self.route_id, "手动添加待修改", "")
        self._load_summaries(select_id=new_id)

    def _delete_chapter(self):
        if self._block_if_editing():
            return
        summary = self._selected_summary()
        if not summary:
            QMessageBox.information(self, "未选择", "请先选中一段剧情总结。")
            return
        reply = QMessageBox.question(self, "确认删除", "确定删除这段剧情总结吗？此操作不可恢复。")
        if reply != QMessageBox.StandardButton.Yes:
            return
        row = self.list_widget.currentRow()
        next_id = None
        if row + 1 < len(self.summaries):
            next_id = self.summaries[row + 1]["id"]
        elif row - 1 >= 0:
            next_id = self.summaries[row - 1]["id"]
        self.db.delete_summary(summary["id"])
        self._load_summaries(select_id=next_id)

    def _copy_to_route(self):
        if self._block_if_editing():
            return
        summary = self._selected_summary()
        if not summary or not self.route_id:
            QMessageBox.information(self, "未选择", "请先选中一段剧情总结。")
            return
        route = self.db.get_route(self.route_id) or {}
        game_id = route.get("game_id")
        if not game_id:
            QMessageBox.warning(self, "无法复制", "当前线路没有所属游戏。")
            return
        targets = [
            item for item in self.db.list_routes(int(game_id))
            if item["id"] != self.route_id
        ]
        if not targets:
            QMessageBox.information(self, "没有其他线路", "请先为这个游戏创建另一条线路。")
            return
        dialog = CopySummaryDialog(targets, self.window())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target_id = dialog.selected_route_id()
        if not target_id:
            return
        title = (summary.get("title") or "").strip()
        self.db.add_manual_chapter(target_id, title, summary.get("content") or "")
        target = next((item for item in targets if item["id"] == target_id), None)
        name = (target or {}).get("name") or "目标线路"
        QMessageBox.information(self, "已复制", f"已追加到「{name}」。")


# ---------------------------------------------------------------------------
# 关键信息编辑对话框
# ---------------------------------------------------------------------------
class KeyInfoEditDialog(BaseDialog):
    def __init__(self, route_id: int, content: str = "", category: str = "剧情线索", user_note: str = "", summary_id: Optional[int] = None, parent=None):
        super().__init__("关键信息", parent)
        self.route_id = route_id
        self.summary_id = summary_id
        self.resize(480, 380)
        self._init_ui(content, category, user_note)

    def _init_ui(self, content: str, category: str, user_note: str):
        layout = QFormLayout()
        layout.setVerticalSpacing(12)
        layout.setHorizontalSpacing(16)
        self.body.addLayout(layout)

        # 内容可能很长，使用多行文本域
        self.txt_content = QTextEdit()
        self.txt_content.setAcceptRichText(False)
        self.txt_content.setPlainText(content)
        self.txt_content.setMinimumHeight(140)
        # 分类由上下文决定，不允许修改
        self.category = category
        lbl_category = QLabel(category)
        set_cls(lbl_category, "chip")
        category_row = QHBoxLayout()
        category_row.addWidget(lbl_category)
        category_row.addStretch()
        self.txt_note = QLineEdit(user_note)

        layout.addRow("内容：", self.txt_content)
        layout.addRow("分类：", category_row)
        layout.addRow("备注：", self.txt_note)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        set_variant(btn_cancel, "ghost")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        btn_save = QPushButton("保存")
        set_variant(btn_save, "primary")
        btn_save.clicked.connect(self._save)
        btn_row.addWidget(btn_save)
        self.body.addLayout(btn_row)

    def _save(self):
        self.content = self.txt_content.toPlainText().strip()
        self.user_note = self.txt_note.text().strip()
        self.accept()


# ---------------------------------------------------------------------------
# 角色信息编辑对话框
# ---------------------------------------------------------------------------
class CharacterEditDialog(BaseDialog):
    def __init__(self, route_id: int, name: str = "", info: str = "", parent=None):
        super().__init__("角色信息", parent)
        self.route_id = route_id
        self.resize(440, 340)
        self._init_ui(name, info)

    def _init_ui(self, name: str, info: str):
        layout = QFormLayout()
        layout.setVerticalSpacing(12)
        layout.setHorizontalSpacing(16)
        self.body.addLayout(layout)

        self.txt_name = QLineEdit(name)
        self.txt_info = QTextEdit()
        self.txt_info.setAcceptRichText(False)
        self.txt_info.setPlainText(info)
        self.txt_info.setMinimumHeight(120)
        layout.addRow("角色名：", self.txt_name)
        layout.addRow("信息：", self.txt_info)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        set_variant(btn_cancel, "ghost")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        btn_save = QPushButton("保存")
        set_variant(btn_save, "primary")
        btn_save.clicked.connect(self._save)
        btn_row.addWidget(btn_save)
        self.body.addLayout(btn_row)

    def _save(self):
        self.name = self.txt_name.text().strip()
        self.info = self.txt_info.toPlainText().strip()
        self.accept()


# ---------------------------------------------------------------------------
# 角色卡片（支持拖动排序）
# ---------------------------------------------------------------------------
CHARACTER_MIME = "application/x-character-id"


class CharacterCard(QFrame):
    def __init__(self, char: dict, on_edit, on_delete, parent=None):
        super().__init__(parent)
        self.char = char
        self.char_id = char["id"]
        self._on_edit = on_edit
        self._press_pos: Optional[QPoint] = None

        self.setProperty("charCard", True)
        self.setMinimumHeight(150)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(8)

        name = (char["name"] or "?").strip() or "?"
        display_name = name if len(name) <= 20 else name[:20] + "…"
        lbl_name = QLabel(display_name)
        lbl_name.setToolTip(name)
        lbl_name.setWordWrap(True)
        set_cls(lbl_name, "cardName")
        v.addWidget(lbl_name)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setProperty("cls", "cardDivider")
        v.addWidget(divider)

        info = (char["info"] or "").strip().replace("\n", "　")
        if len(info) > 110:
            info = info[:110] + "…"
        lbl_info = QLabel(info or "暂无描述")
        set_cls(lbl_info, "dim")
        lbl_info.setWordWrap(True)
        lbl_info.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        v.addWidget(lbl_info, 1)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        btns.addStretch()
        btn_edit = QPushButton("编辑")
        btn_edit.setObjectName("PillBtn")
        btn_edit.clicked.connect(lambda _checked=None: on_edit(self.char))
        btn_del = QPushButton("删除")
        btn_del.setObjectName("PillBtn")
        set_variant(btn_del, "danger")
        btn_del.clicked.connect(lambda _checked=None: on_delete(self.char))
        btns.addWidget(btn_edit)
        btns.addWidget(btn_del)
        v.addLayout(btns)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self._press_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.position().toPoint() - self._press_pos).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(CHARACTER_MIME, str(self.char_id).encode())
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(self._press_pos)
            self._press_pos = None
            drag.exec(Qt.DropAction.MoveAction)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._on_edit(self.char)


class CharacterCardContainer(QWidget):
    """角色卡片网格容器，接受卡片拖放并给出新的排序"""

    order_changed = pyqtSignal(list)  # 新顺序的角色 id 列表

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(CHARACTER_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(CHARACTER_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if not event.mimeData().hasFormat(CHARACTER_MIME):
            return
        src_id = int(bytes(event.mimeData().data(CHARACTER_MIME)).decode())
        pos = event.position().toPoint()
        layout = self.layout()
        ordered: List[int] = []
        target_id: Optional[int] = None
        for i in range(layout.count()):
            w = layout.itemAt(i).widget()
            if isinstance(w, CharacterCard):
                ordered.append(w.char_id)
                if target_id is None and w.geometry().contains(pos):
                    target_id = w.char_id
        if src_id not in ordered:
            return
        ordered.remove(src_id)
        if target_id is None or target_id == src_id:
            ordered.append(src_id)
        else:
            ordered.insert(ordered.index(target_id), src_id)
        event.acceptProposedAction()
        self.order_changed.emit(ordered)


# ---------------------------------------------------------------------------
# 关键信息页面
# ---------------------------------------------------------------------------
class KeyInfoPage(QWidget):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.route_id: Optional[int] = None
        self._init_ui()
        self._refresh_all()

    def set_route(self, route_id: Optional[int]):
        self.route_id = route_id
        self.reload()

    def reload(self):
        self._refresh_all()

    def _init_ui(self):
        page = QVBoxLayout(self)
        page.setContentsMargins(16, 14, 16, 14)
        page.setSpacing(12)
        page.addWidget(make_caption("关键信息 · KEY INFO"))
        page.addWidget(make_hint(
            "剧情线索、世界观、角色在同一游戏的各剧情树之间共享。"
            "设定冲突时标成「线路名线中原设定」。"
        ))

        self.tabs = QTabWidget()

        self.tab_clues = self._create_key_info_tab("剧情线索")
        self.tab_world = self._create_key_info_tab("世界观设定")
        self.tab_chars = self._create_character_tab()

        self.tabs.addTab(self.tab_clues["widget"], "剧情线索")
        self.tabs.addTab(self.tab_world["widget"], "世界观设定")
        self.tabs.addTab(self.tab_chars["widget"], "角色信息")

        page.addWidget(self.tabs, 1)

    def _create_key_info_tab(self, category: str) -> dict:
        widget = QWidget()
        vlayout = QVBoxLayout(widget)
        vlayout.setContentsMargins(0, 12, 0, 0)
        vlayout.setSpacing(10)
        list_widget = QListWidget()
        list_widget.itemDoubleClicked.connect(lambda _checked=None, cat=category: self._edit_key_info(cat))
        vlayout.addWidget(list_widget)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)
        btn_add = QPushButton("＋ 新增")
        set_variant(btn_add, "primary")
        btn_add.clicked.connect(lambda _checked=None, cat=category: self._add_key_info(cat))
        btn_edit = QPushButton("编辑")
        btn_edit.clicked.connect(lambda _checked=None, cat=category: self._edit_key_info(cat))
        btn_del = QPushButton("删除")
        set_variant(btn_del, "danger")
        btn_del.clicked.connect(lambda _checked=None, cat=category: self._delete_key_info(cat))
        btn_layout.addWidget(btn_add)
        btn_layout.addWidget(btn_edit)
        btn_layout.addWidget(btn_del)
        btn_layout.addStretch()
        vlayout.addLayout(btn_layout)

        return {"widget": widget, "list": list_widget, "category": category, "items": []}

    def _create_character_tab(self) -> dict:
        widget = QWidget()
        vlayout = QVBoxLayout(widget)
        vlayout.setContentsMargins(0, 12, 0, 0)
        vlayout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = CharacterCardContainer()
        container.order_changed.connect(self._on_characters_reordered)
        cards_layout = QGridLayout(container)
        cards_layout.setContentsMargins(2, 2, 6, 2)
        cards_layout.setHorizontalSpacing(12)
        cards_layout.setVerticalSpacing(12)
        cards_layout.setColumnStretch(0, 1)
        cards_layout.setColumnStretch(1, 1)
        scroll.setWidget(container)
        vlayout.addWidget(scroll, 1)

        btn_layout = QHBoxLayout()
        btn_add = QPushButton("＋ 新增角色")
        set_variant(btn_add, "primary")
        btn_add.clicked.connect(self._add_character)
        btn_layout.addWidget(btn_add)
        btn_layout.addStretch()
        vlayout.addLayout(btn_layout)

        return {"widget": widget, "cards_layout": cards_layout, "items": []}

    def _make_character_card(self, char: dict) -> QFrame:
        return CharacterCard(char, self._edit_character, self._delete_character)

    def _on_characters_reordered(self, ids: list):
        if not self.route_id:
            return
        self.db.set_character_order([int(i) for i in ids])
        self._load_characters()

    def _refresh_all(self):
        self._load_key_info(self.tab_clues)
        self._load_key_info(self.tab_world)
        self._load_characters()

    def _load_key_info(self, tab: dict):
        list_widget = tab["list"]
        category = tab["category"]
        list_widget.clear()
        if not self.route_id:
            tab["items"] = []
            return
        tab["items"] = self.db.list_key_info(self.route_id, category=category)
        for info in tab["items"]:
            display = info["content"]
            if info["user_note"]:
                display += f" | {info['user_note']}"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, info["id"])
            list_widget.addItem(item)

    def _load_characters(self):
        layout: QGridLayout = self.tab_chars["cards_layout"]
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.tab_chars["items"] = (
            self.db.list_characters(self.route_id) if self.route_id else []
        )
        if not self.tab_chars["items"]:
            empty = QLabel(
                "暂无角色信息，点击下方“新增角色”添加"
                if self.route_id
                else "请先在左侧选择游戏与线路"
            )
            set_cls(empty, "dim")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(empty, 0, 0, 1, 2)
            layout.setRowStretch(1, 1)
            return
        row = 0
        for i, char in enumerate(self.tab_chars["items"]):
            row, col = divmod(i, 2)
            layout.addWidget(self._make_character_card(char), row, col)
        for r in range(row + 2):
            layout.setRowStretch(r, 0)
        layout.setRowStretch(row + 1, 1)

    def _add_key_info(self, category: str):
        if not self.route_id:
            QMessageBox.information(self, "未选择线路", "请先在左侧选择游戏与线路。")
            return
        dialog = KeyInfoEditDialog(self.route_id, category=category, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.db.add_key_info(
                self.route_id,
                content=dialog.content,
                category=dialog.category,
                user_note=dialog.user_note,
            )
            self._refresh_all()

    def _edit_key_info(self, category: str):
        tab = self.tab_clues if category == "剧情线索" else self.tab_world
        row = tab["list"].currentRow()
        if row < 0 or row >= len(tab["items"]):
            return
        info = tab["items"][row]
        dialog = KeyInfoEditDialog(
            self.route_id,
            content=info["content"],
            category=info["category"],
            user_note=info["user_note"],
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.db.update_key_info(info["id"], dialog.content, dialog.category, dialog.user_note)
            self._refresh_all()

    def _delete_key_info(self, category: str):
        tab = self.tab_clues if category == "剧情线索" else self.tab_world
        row = tab["list"].currentRow()
        if row < 0 or row >= len(tab["items"]):
            return
        info = tab["items"][row]
        reply = QMessageBox.question(self, "确认删除", "确定删除这条关键信息吗？")
        if reply == QMessageBox.StandardButton.Yes:
            self.db.delete_key_info(info["id"])
            self._refresh_all()

    def _add_character(self):
        if not self.route_id:
            QMessageBox.information(self, "未选择线路", "请先在左侧选择游戏与线路。")
            return
        dialog = CharacterEditDialog(self.route_id, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.db.add_or_update_character(self.route_id, dialog.name, dialog.info)
            self._refresh_all()

    def _edit_character(self, char: dict):
        dialog = CharacterEditDialog(self.route_id, name=char["name"], info=char["info"], parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.db.update_character(char["id"], dialog.name, dialog.info)
            self._refresh_all()

    def _delete_character(self, char: dict):
        reply = QMessageBox.question(self, "确认删除", f"确定删除角色 {char['name']} 吗？")
        if reply == QMessageBox.StandardButton.Yes:
            self.db.delete_character(char["id"])
            self._refresh_all()


# ---------------------------------------------------------------------------
# 提示词配置
# ---------------------------------------------------------------------------
class PromptEditDialog(BaseDialog):
    def __init__(self, title_text: str = "", system_prompt: str = "", heading: str = "编辑提示词", parent=None):
        super().__init__(heading, parent)
        self.resize(680, 560)
        self.prompt_title = title_text
        self.system_prompt = system_prompt

        self.txt_title = QLineEdit(title_text)
        self.body.addWidget(QLabel("标题"))
        self.body.addWidget(self.txt_title)

        self.txt_system = QTextEdit()
        self.txt_system.setAcceptRichText(False)
        self.txt_system.setPlainText(system_prompt)
        self.body.addWidget(QLabel("系统提示词"))
        self.body.addWidget(self.txt_system, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("取消")
        set_variant(btn_cancel, "ghost")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        btn_save = QPushButton("保存")
        set_variant(btn_save, "primary")
        btn_save.clicked.connect(self._save)
        btn_row.addWidget(btn_save)
        self.body.addLayout(btn_row)

    def _save(self):
        title = self.txt_title.text().strip()
        system = self.txt_system.toPlainText().strip()
        if not title or not system:
            QMessageBox.warning(self, "不能为空", "标题和系统提示词都不能为空。")
            return
        self.prompt_title = title
        self.system_prompt = system
        self.accept()


class PromptConfigPage(QWidget):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self._loading = False
        self._init_ui()

    def _init_ui(self):
        page = QVBoxLayout(self)
        page.setContentsMargins(16, 14, 16, 14)
        page.setSpacing(12)
        page.addWidget(make_caption("提示词 · PROMPTS"))

        card = make_card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)

        hint = QLabel("勾选启用。设为当前后，剧情总结会使用该系统提示词。至少保留一条，且至少启用一条。")
        set_cls(hint, "dim")
        hint.setWordWrap(True)
        card_layout.addWidget(hint)

        user_line = QLabel(f"用户提示词（固定，不可改）：{SUMMARY_USER_TAIL}")
        set_cls(user_line, "dim")
        user_line.setWordWrap(True)
        card_layout.addWidget(user_line)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.list = QListWidget()
        self.list.setMinimumWidth(220)
        self.list.currentItemChanged.connect(self._show_preview)
        self.list.itemChanged.connect(self._on_item_changed)
        body.addWidget(self.list, 2)

        preview_col = QVBoxLayout()
        preview_col.setSpacing(6)
        preview_title = QLabel("系统提示词预览")
        set_cls(preview_title, "dim")
        preview_col.addWidget(preview_title)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAcceptRichText(False)
        preview_col.addWidget(self.preview, 1)
        body.addLayout(preview_col, 3)
        card_layout.addLayout(body, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_add = QPushButton("新增")
        set_variant(self.btn_add, "primary")
        self.btn_add.clicked.connect(self._add)
        self.btn_edit = QPushButton("编辑")
        set_variant(self.btn_edit, "ghost")
        self.btn_edit.clicked.connect(self._edit)
        self.btn_delete = QPushButton("删除")
        set_variant(self.btn_delete, "ghost")
        self.btn_delete.clicked.connect(self._delete)
        self.btn_select = QPushButton("设为当前")
        set_variant(self.btn_select, "ghost")
        self.btn_select.clicked.connect(self._select)
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_edit)
        btn_row.addWidget(self.btn_delete)
        btn_row.addWidget(self.btn_select)
        btn_row.addStretch()
        card_layout.addLayout(btn_row)

        page.addWidget(card, 1)
        self.reload()

    def _current_id(self) -> Optional[int]:
        item = self.list.currentItem()
        if not item:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def reload(self, keep_id: Optional[int] = None):
        if keep_id is None:
            keep_id = self._current_id()
        prompts = self.db.list_summary_prompts()
        self._loading = True
        self.list.blockSignals(True)
        self.list.clear()
        select_row = 0
        for index, prompt in enumerate(prompts):
            title = (prompt.get("title") or "未命名").strip() or "未命名"
            if prompt.get("selected"):
                title = f"{title}（当前）"
                if keep_id is None:
                    select_row = index
            item = QListWidgetItem(title)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsEnabled
            )
            item.setCheckState(
                Qt.CheckState.Checked if prompt.get("enabled") else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, prompt["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, prompt.get("system_prompt") or "")
            item.setData(Qt.ItemDataRole.UserRole + 2, 1 if prompt.get("enabled") else 0)
            item.setData(Qt.ItemDataRole.UserRole + 3, (prompt.get("title") or "").strip())
            self.list.addItem(item)
            if keep_id is not None and prompt["id"] == keep_id:
                select_row = index
        self.list.blockSignals(False)
        self._loading = False
        if self.list.count():
            self.list.setCurrentRow(select_row)
        else:
            self.preview.clear()
        self.btn_delete.setEnabled(len(prompts) > 1)
        self._show_preview()

    def _show_preview(self, *_args):
        item = self.list.currentItem()
        if not item:
            self.preview.clear()
            return
        self.preview.setPlainText(item.data(Qt.ItemDataRole.UserRole + 1) or "")

    def _on_item_changed(self, item: QListWidgetItem):
        if self._loading:
            return
        prompt_id = item.data(Qt.ItemDataRole.UserRole)
        enabled = item.checkState() == Qt.CheckState.Checked
        was = bool(item.data(Qt.ItemDataRole.UserRole + 2))
        if enabled == was:
            return
        err = self.db.set_summary_prompt_enabled(prompt_id, enabled)
        if err:
            QMessageBox.warning(self, "无法关闭", err)
        self.reload(keep_id=prompt_id)

    def _add(self):
        dialog = PromptEditDialog(heading="新增提示词", parent=self.window())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_id = self.db.add_summary_prompt(dialog.prompt_title, dialog.system_prompt, enabled=True)
        self.reload(keep_id=new_id)

    def _edit(self):
        item = self.list.currentItem()
        if not item:
            QMessageBox.information(self, "未选择", "请先选中一条提示词。")
            return
        dialog = PromptEditDialog(
            title_text=item.data(Qt.ItemDataRole.UserRole + 3) or "",
            system_prompt=item.data(Qt.ItemDataRole.UserRole + 1) or "",
            heading="编辑提示词",
            parent=self.window(),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        prompt_id = item.data(Qt.ItemDataRole.UserRole)
        self.db.update_summary_prompt(prompt_id, dialog.prompt_title, dialog.system_prompt)
        self.reload(keep_id=prompt_id)

    def _delete(self):
        prompt_id = self._current_id()
        if prompt_id is None:
            QMessageBox.information(self, "未选择", "请先选中一条提示词。")
            return
        reply = QMessageBox.question(self, "确认删除", "确定删除这条提示词吗？")
        if reply != QMessageBox.StandardButton.Yes:
            return
        err = self.db.delete_summary_prompt(prompt_id)
        if err:
            QMessageBox.warning(self, "无法删除", err)
            return
        self.reload(keep_id=None)

    def _select(self):
        prompt_id = self._current_id()
        if prompt_id is None:
            QMessageBox.information(self, "未选择", "请先选中一条提示词。")
            return
        err = self.db.select_summary_prompt(prompt_id)
        if err:
            QMessageBox.warning(self, "无法设为当前", err)
            return
        self.reload(keep_id=prompt_id)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GalCoplit")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._title_dragging = False
        self._stable_size = None
        self._fixing_size = False
        self.config = self._load_config()
        self.llm_client = LLMClient(self.config)
        try:
            self.db = Database(os.path.join(APP_DIR, "records.db"))
        except Exception as e:
            QMessageBox.critical(self, "数据库初始化失败", f"无法打开数据库：{e}\n请检查 records.db 是否损坏或无权访问。")
            raise
        self.toasts = ToastManager()
        self.status_window = StatusWindow()
        self.status_window.summarize_requested.connect(self._manual_summarize)
        self.status_window.toggle_pause_requested.connect(self._toggle_ocr)
        self.status_window.set_summarize_gate(lambda: self._summarize_allowed())
        self.region_indicator = RegionIndicator()
        self.region_indicator.region_changed.connect(self._on_region_indicator_changed)

        self.region: Optional[dict] = self.config.get("region")
        self.ocr_worker = None
        self.summary_thread: Optional[SummaryThread] = None

        self.buffer_lock = threading.RLock()
        self.text_buffer: list[str] = []
        self.previous_summary = ""
        self._summarizing = False
        self._current_session_id: Optional[int] = None
        self._last_session_id: Optional[int] = None
        self._pause_summary_pending = False
        self.manual_sentence_count = 0

        # ---- 队列模式（v1.1）状态 ----
        self._queue_mode = bool(self.config.get("queue_mode", True))
        # 帧文本簿：(frame_id, text)，按锚点区间取用
        self.frame_texts: list[tuple[int, str]] = []
        self.summary_queue: deque = deque()
        self._summary_task_log: list = []
        self._summary_task_seq = 0
        self._current_summary_task: Optional[SummaryTask] = None
        self._last_anchor_id = 0
        # 连续完成水位：所有 <= _recognized_upto 的帧都已识别（或已确认丢弃）
        self._recognized_upto = 0
        self._recognized_ids: set = set()
        self._frame_id_high_water = 0
        self._frames_done = 0
        self._frames_enqueued = 0
        self._frames_dropped = 0
        self._frame_seq_base = 0
        self._draining = False
        self._draining_workers: list = []
        self._queue_tree_id: Optional[int] = None
        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(500)
        self._queue_timer.timeout.connect(self._on_queue_tick)
        self._inflight_summary_text = ""
        self._queue_table_sig = None
        self._ocr_transcript_logged = False

        self.current_game_id: Optional[int] = self.config.get("last_game_id")
        self.current_tree_id: Optional[int] = self.config.get("last_tree_id")
        self.current_route_id: Optional[int] = None

        self.session_start: Optional[datetime.datetime] = None

        self._init_ui()
        self._restore_current_route()
        self._update_today_time()
        self._apply_saved_region()

        # 默认显示状态浮窗
        self.status_window.show()
        self.btn_status.setChecked(True)
        self.status_window.set_ocr_running(False)
        self.status_window.set_queue_mode(self._queue_mode)
        self.status_window.set_manual_mode(self.config.get("summary_mode", "auto") == "manual")
        self._set_summary_status("空闲")
        self.status_window.update_summary_state("空闲")

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _load_config(self) -> dict:
        config_path = os.path.join(APP_DIR, "config.json")
        default = _default_config()
        if not os.path.exists(config_path):
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=2)
            return default
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            merged = default.copy()
            merged.update(loaded)
            merged = _normalize_config(merged)
            # 旧版 config.json 直接换新版程序时，把新增配置项补写回文件
            if any(k not in loaded for k in default):
                try:
                    with open(config_path, "w", encoding="utf-8") as f:
                        json.dump(merged, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            return merged
        except Exception as e:
            QMessageBox.warning(
                None,
                "配置读取失败",
                f"config.json 读取失败，将使用默认配置。\n错误：{e}",
            )
            return default

    def _save_config(self):
        config_path = os.path.join(APP_DIR, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=2)

    def _apply_saved_region(self):
        if self.region:
            self._set_region_label(self.region)
            self.region_indicator.set_region(self.region)
            self.btn_region_indicator.setChecked(True)

    # ------------------------------------------------------------------
    # 状态 / 显示辅助
    # ------------------------------------------------------------------
    def _set_status(self, text: str):
        self.lbl_status.setText(text)
        self.lbl_side_status.setText(text)
        color, blink = status_visual(text)
        self.status_dot.set_state(color, blink)
        self.side_dot.set_state(color, blink)

    def _set_summary_status(self, text: str):
        self.lbl_summary_status_main.setText(text)
        color, blink = status_visual(text)
        self.summary_status_dot.set_state(color, blink)

    def _set_region_label(self, region: Optional[dict]):
        if region:
            self.lbl_region.setText(
                f"区域 {region['width']} × {region['height']} @ ({region['left']}, {region['top']})"
            )
        else:
            self.lbl_region.setText("区域未选择")

    # ------------------------------------------------------------------
    # UI 初始化
    # ------------------------------------------------------------------
    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(0)

        self.root_frame = QFrame()
        self.root_frame.setObjectName("RootFrame")
        outer.addWidget(self.root_frame)
        self._shadow = make_shadow(self.root_frame, blur=36, dy=8)

        root = QVBoxLayout(self.root_frame)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 标题栏 ----
        self.title_bar = TitleBar(self)
        root.addWidget(self.title_bar)

        # ---- 侧边栏 + 页面区 ----
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root.addLayout(body, 1)

        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(216)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 16, 14, 14)
        side.setSpacing(8)

        side.addWidget(make_caption("档案 · PROFILE"))
        self.cmb_game = make_combo()
        self.cmb_game.currentIndexChanged.connect(self._on_game_combo_changed)
        side.addWidget(self.cmb_game)
        self.cmb_route = make_combo()
        self.cmb_route.currentIndexChanged.connect(self._on_route_combo_changed)
        side.addWidget(self.cmb_route)

        side.addSpacing(12)
        side.addWidget(make_caption("导航 · MENU"))
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for idx, (glyph, text) in enumerate(
            [("▣", "主界面"), ("≡", "剧情树"), ("✦", "关键信息"), ("▤", "游戏管理"), ("✎", "提示词配置"), ("⚙", "设置")]
        ):
            btn = NavButton(glyph, text)
            self.nav_group.addButton(btn, idx)
            side.addWidget(btn)
        self.nav_group.idClicked.connect(self._switch_page)

        side.addStretch()

        side_status = QHBoxLayout()
        side_status.setSpacing(8)
        self.side_dot = PulseDot()
        side_status.addWidget(self.side_dot)
        self.lbl_side_status = QLabel("等待")
        set_cls(self.lbl_side_status, "floatSub")
        side_status.addWidget(self.lbl_side_status, 1)
        side.addLayout(side_status)

        body.addWidget(sidebar)

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)

        self.dashboard_page = self._build_dashboard_page()
        self.story_page = StoryTreePage(self.db)
        self.story_page.cursor_changed.connect(self._on_story_cursor_changed)
        self.keyinfo_page = KeyInfoPage(self.db)
        self.gameroute_page = GameRoutePage(self.db)
        self.gameroute_page.data_changed.connect(self._on_game_data_changed)
        self.gameroute_page.tree_chosen.connect(self._on_tree_chosen)
        self.prompt_page = PromptConfigPage(self.db)
        self.settings_page = SettingsPage(self.config)
        self.settings_page.saved.connect(self._on_settings_saved)
        self.stack.addWidget(self.dashboard_page)
        self.stack.addWidget(self.story_page)
        self.stack.addWidget(self.keyinfo_page)
        self.stack.addWidget(self.gameroute_page)
        self.stack.addWidget(self.prompt_page)
        self.stack.addWidget(self.settings_page)
        self.nav_group.button(0).setChecked(True)

        # ---- 底部缩放手柄 ----
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 6, 6)
        grip_row.addStretch()
        grip_row.addWidget(QSizeGrip(central))
        root.addLayout(grip_row)

        self._set_status("等待选择区域")

    def _build_dashboard_page(self) -> QWidget:
        page_widget = QWidget()
        content = QVBoxLayout(page_widget)
        content.setContentsMargins(16, 14, 16, 8)
        content.setSpacing(12)

        # ---- 控制卡片 ----
        control_card = make_card()
        cc = QVBoxLayout(control_card)
        cc.setContentsMargins(18, 14, 18, 16)
        cc.setSpacing(10)
        cc.addWidget(make_caption("识别控制 · CONTROL"))

        run_row = QHBoxLayout()
        run_row.setSpacing(8)
        self.btn_start = QPushButton("▶  开始识别")
        set_variant(self.btn_start, "primary")
        self.btn_start.setMinimumHeight(38)
        self.btn_start.clicked.connect(self._start_ocr)
        run_row.addWidget(self.btn_start, 1)

        self.btn_stop = QPushButton("❚❚  暂停识别")
        set_variant(self.btn_stop, "danger")
        self.btn_stop.setMinimumHeight(38)
        self.btn_stop.clicked.connect(self._stop_ocr)
        self.btn_stop.setEnabled(False)
        run_row.addWidget(self.btn_stop, 1)
        cc.addLayout(run_row)

        tool_row = QHBoxLayout()
        tool_row.setSpacing(8)
        self.btn_region = QPushButton("选择区域")
        self.btn_region.clicked.connect(self._select_region)
        tool_row.addWidget(self.btn_region)

        self.btn_region_indicator = QPushButton("区域框")
        self.btn_region_indicator.setCheckable(True)
        self.btn_region_indicator.clicked.connect(self._toggle_region_indicator)
        tool_row.addWidget(self.btn_region_indicator)

        self.btn_test_ocr = QPushButton("测试 OCR")
        self.btn_test_ocr.clicked.connect(self._test_ocr_once)
        tool_row.addWidget(self.btn_test_ocr)

        self.lbl_region = QLabel("区域未选择")
        set_cls(self.lbl_region, "chip")
        tool_row.addWidget(self.lbl_region)

        self.btn_status = QPushButton("状态浮窗")
        self.btn_status.setCheckable(True)
        self.btn_status.clicked.connect(self._toggle_status_window)
        tool_row.addWidget(self.btn_status)
        cc.addLayout(tool_row)

        content.addWidget(control_card)

        # ---- 状态统计卡片 ----
        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)

        status_card = make_card()
        sv = QVBoxLayout(status_card)
        sv.setContentsMargins(16, 12, 16, 12)
        sv.setSpacing(6)
        sv.addWidget(make_caption("识别状态 · OCR"))
        status_line = QHBoxLayout()
        status_line.setSpacing(8)
        self.status_dot = PulseDot()
        status_line.addWidget(self.status_dot)
        self.lbl_status = QLabel("等待选择区域")
        set_cls(self.lbl_status, "statAccent")
        status_line.addWidget(self.lbl_status)
        status_line.addStretch()
        sv.addLayout(status_line)
        stats_row.addWidget(status_card, 1)

        summary_status_card = make_card()
        ssv = QVBoxLayout(summary_status_card)
        ssv.setContentsMargins(16, 12, 16, 12)
        ssv.setSpacing(6)
        ssv.addWidget(make_caption("总结状态 · SUMMARY"))
        summary_status_line = QHBoxLayout()
        summary_status_line.setSpacing(8)
        self.summary_status_dot = PulseDot()
        summary_status_line.addWidget(self.summary_status_dot)
        self.lbl_summary_status_main = QLabel("空闲")
        set_cls(self.lbl_summary_status_main, "statAccent")
        summary_status_line.addWidget(self.lbl_summary_status_main)
        summary_status_line.addStretch()
        ssv.addLayout(summary_status_line)
        stats_row.addWidget(summary_status_card, 1)

        today_card = make_card()
        tv = QVBoxLayout(today_card)
        tv.setContentsMargins(16, 12, 16, 12)
        tv.setSpacing(6)
        tv.addWidget(make_caption("今日游玩 · TODAY"))
        self.lbl_today = QLabel("00:00:00")
        set_cls(self.lbl_today, "statValue")
        tv.addWidget(self.lbl_today)
        stats_row.addWidget(today_card, 1)

        content.addLayout(stats_row)

        # ---- 队列进度卡片（仅队列模式 v1.1） ----
        self.card_queue = make_card()
        qv = QVBoxLayout(self.card_queue)
        qv.setContentsMargins(18, 12, 18, 12)
        qv.setSpacing(6)
        qv.addWidget(make_caption("队列进度 · QUEUE"))

        self.lbl_queue_ocr = QLabel("已识别 / 已累计：0 / 0")
        set_cls(self.lbl_queue_ocr, "floatSub")
        qv.addWidget(self.lbl_queue_ocr)
        self.bar_queue_ocr = QProgressBar()
        self.bar_queue_ocr.setTextVisible(False)
        self.bar_queue_ocr.setFixedHeight(6)
        self.bar_queue_ocr.setRange(0, 1)
        self.bar_queue_ocr.setValue(0)
        qv.addWidget(self.bar_queue_ocr)

        self.lbl_queue_summary = QLabel("总结任务：0 / 0")
        set_cls(self.lbl_queue_summary, "floatSub")
        qv.addWidget(self.lbl_queue_summary)
        self.bar_queue_summary = QProgressBar()
        self.bar_queue_summary.setTextVisible(False)
        self.bar_queue_summary.setFixedHeight(6)
        self.bar_queue_summary.setRange(0, 1)
        self.bar_queue_summary.setValue(0)
        qv.addWidget(self.bar_queue_summary)

        content.addWidget(self.card_queue)
        self.card_queue.setVisible(self._queue_mode)

        # ---- 实时监控卡片 ----
        monitor_card = make_card()
        mv = QVBoxLayout(monitor_card)
        mv.setContentsMargins(18, 14, 18, 14)
        mv.setSpacing(10)
        mv.addWidget(make_caption("实时监控 · LIVE"))

        current_row = QHBoxLayout()
        current_row.setSpacing(8)
        self.txt_current = QLineEdit()
        self.txt_current.setReadOnly(True)
        self.txt_current.setObjectName("LiveLine")
        self.txt_current.setPlaceholderText("等待识别输出…")
        current_row.addWidget(self.txt_current, 1)
        self.btn_summarize_main = QPushButton("✦  总结")
        set_variant(self.btn_summarize_main, "primary")
        self.btn_summarize_main.setMinimumHeight(38)
        self.btn_summarize_main.setMinimumWidth(96)
        self.btn_summarize_main.setToolTip("立即总结当前已累计的文本")
        self.btn_summarize_main.clicked.connect(self._manual_summarize)
        current_row.addWidget(self.btn_summarize_main)
        mv.addLayout(current_row)

        self.tabs = QTabWidget()
        self.txt_summary = QTextEdit()
        self.txt_summary.setReadOnly(True)
        self.txt_summary.setPlaceholderText("剧情总结将在这里实时生成…")
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setPlaceholderText("OCR 识别日志…")
        self.tabs.addTab(self.txt_summary, "实时总结")
        self.tabs.addTab(self.txt_log, "识别日志")

        # 队列 tab：总结任务的锚点、帧区间与状态（仅队列模式）
        self.queue_tab = QWidget()
        queue_tab_layout = QVBoxLayout(self.queue_tab)
        queue_tab_layout.setContentsMargins(0, 8, 0, 0)
        queue_tab_layout.setSpacing(8)
        self.queue_retry_row = QWidget()
        retry_row = QHBoxLayout(self.queue_retry_row)
        retry_row.setContentsMargins(0, 0, 0, 0)
        retry_row.setSpacing(8)
        self.lbl_queue_failed = QLabel("0 个总结失败")
        set_cls(self.lbl_queue_failed, "dim")
        retry_row.addWidget(self.lbl_queue_failed)
        retry_row.addStretch()
        self.btn_retry_failed = QPushButton("批量重试")
        set_variant(self.btn_retry_failed, "primary")
        self.btn_retry_failed.clicked.connect(self._retry_all_failed)
        retry_row.addWidget(self.btn_retry_failed)
        self.queue_retry_row.setVisible(False)
        queue_tab_layout.addWidget(self.queue_retry_row)

        self.tbl_queue = QTableWidget(0, 5)
        self.tbl_queue.setHorizontalHeaderLabels(["锚点帧", "帧区间", "状态", "入队时间", "操作"])
        self.tbl_queue.verticalHeader().setVisible(False)
        self.tbl_queue.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_queue.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tbl_queue.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.tbl_queue.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        self.tbl_queue.verticalHeader().setDefaultSectionSize(36)
        queue_tab_layout.addWidget(self.tbl_queue, 1)
        self._queue_table_sig = None
        self._queue_tab_index = -1
        if self._queue_mode:
            self._queue_tab_index = self.tabs.addTab(self.queue_tab, "总结队列")

        mv.addWidget(self.tabs, 1)

        content.addWidget(monitor_card, 1)

        return page_widget

    def _switch_page(self, index: int):
        self.stack.setCurrentIndex(index)
        if index == 1:
            self.story_page.reload()
        elif index == 2:
            self.keyinfo_page.reload()
        elif index == 3:
            self.gameroute_page.set_current(self.current_game_id)
            self.gameroute_page.reload()
        elif index == 4:
            self.prompt_page.reload()

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_shown_once", False):
            self._shown_once = True
            self._fade = QPropertyAnimation(self, b"windowOpacity", self)
            self._fade.setDuration(280)
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._fade.start()

    # ------------------------------------------------------------------
    # 游戏/线路
    # ------------------------------------------------------------------
    def _populate_game_combo(self, select_game_id: Optional[int] = None):
        self.cmb_game.blockSignals(True)
        self.cmb_game.clear()
        games = self.db.list_games()
        for game in games:
            self.cmb_game.addItem(game["name"], game["id"])
        if games:
            idx = next(
                (i for i, g in enumerate(games) if g["id"] == select_game_id), 0
            )
            self.cmb_game.setCurrentIndex(idx)
        self.cmb_game.blockSignals(False)

    def _populate_tree_combo(self, game_id: Optional[int], select_tree_id: Optional[int] = None):
        self.cmb_route.blockSignals(True)
        self.cmb_route.clear()
        trees = self.db.list_story_trees(game_id) if game_id else []
        for tree in trees:
            self.cmb_route.addItem(tree["name"], tree["id"])
        if trees:
            idx = next((i for i, tree in enumerate(trees) if tree["id"] == select_tree_id), 0)
            self.cmb_route.setCurrentIndex(idx)
        self.cmb_route.blockSignals(False)

    def _restore_current_route(self):
        """启动时恢复上次的游戏和剧情树。"""
        game_id = self.current_game_id
        if not (game_id and self.db.get_game(game_id)):
            game_id = None
        self._populate_game_combo(game_id)

        tree_id = self.current_tree_id
        tree = self.db.get_story_tree(tree_id) if tree_id else None
        if not tree or tree["game_id"] != self.cmb_game.currentData():
            tree_id = None
        self._populate_tree_combo(self.cmb_game.currentData(), tree_id)
        self._commit_route_selection(initial=True)

    def _ensure_game_route(self) -> bool:
        """没有游戏时创建默认游戏和本篇树。已有游戏但没有剧情树时请用户自己新建。"""
        try:
            games = self.db.list_games()
            if not games:
                game_id = self.db.add_game("默认游戏", notes="")
                tree_id = self.db.add_story_tree(game_id, "本篇")
                self.current_game_id = game_id
                self.current_tree_id = tree_id
                self._populate_game_combo(game_id)
                self._populate_tree_combo(game_id, tree_id)
                self._commit_route_selection(initial=True)
                return True
            if not self.current_tree_id:
                game_id = self.cmb_game.currentData() or games[0]["id"]
                trees = self.db.list_story_trees(game_id)
                if not trees:
                    QMessageBox.information(
                        self,
                        "还没有剧情树",
                        "请先在游戏管理里为这个游戏新建一棵剧情树，再开始识别。",
                    )
                    return False
                self.current_game_id = game_id
                self.current_tree_id = trees[0]["id"]
                self._populate_game_combo(game_id)
                self._populate_tree_combo(game_id, trees[0]["id"])
                self._commit_route_selection(initial=True)
                return True
        except Exception as exc:
            QMessageBox.warning(self, "初始化失败", f"准备剧情树时出错：{exc}")
            return False
        return True

    def _check_api_config(self) -> bool:
        """检查 API 是否已配置"""
        base_url = self.config.get("base_url", "").strip()
        api_key = self.config.get("api_key", "").strip()
        model = self.config.get("model", "").strip()

        if not base_url or "您的API地址" in base_url or not api_key or not model:
            QMessageBox.warning(self, "未配置 API", "请先配置 OpenAI URL、API Key 和模型，再开始识别。")
            self._open_settings()
            return False
        return True

    def _open_settings(self):
        """切换到设置页面"""
        self.nav_group.button(5).setChecked(True)
        self._switch_page(5)

    def _on_game_combo_changed(self):
        self._populate_tree_combo(self.cmb_game.currentData())
        self._commit_route_selection()

    def _on_route_combo_changed(self):
        self._commit_route_selection()

    def _commit_route_selection(self, initial: bool = False):
        game_id = self.cmb_game.currentData()
        tree_id = self.cmb_route.currentData()
        tree = self.db.get_story_tree(tree_id) if tree_id else None
        anchor_id = tree.get("anchor_route_id") if tree else None
        changed = tree_id != self.current_tree_id
        self.current_game_id = game_id
        self.current_tree_id = tree_id
        self.current_route_id = anchor_id
        self.config["last_game_id"] = game_id
        self.config["last_tree_id"] = tree_id
        self.config["last_route_id"] = anchor_id
        if not initial:
            self._save_config()
        if changed and not initial:
            self.txt_log.clear()
            self.txt_summary.clear()
            self.txt_current.clear()
            with self.buffer_lock:
                self.text_buffer.clear()
        self._load_previous_summary()
        self.story_page.set_tree(tree_id)
        self.keyinfo_page.set_route(anchor_id)

    def _on_game_data_changed(self):
        """游戏管理页发生增删改后，刷新左上方下拉框并保持当前选择"""
        self._populate_game_combo(self.current_game_id)
        self._populate_tree_combo(self.cmb_game.currentData(), self.current_tree_id)
        self._commit_route_selection()

    def _on_tree_chosen(self, game_id: int, tree_id: int):
        """在游戏管理页点击“设为当前剧情树”"""
        self._populate_game_combo(game_id)
        self._populate_tree_combo(game_id, tree_id)
        self._commit_route_selection()

    def _summarize_allowed(self) -> bool:
        return bool(self.current_tree_id) and self.db.story_can_record(self.current_tree_id)

    def _sync_summarize_buttons(self):
        allowed = self._summarize_allowed()
        self.btn_summarize_main.setEnabled(allowed and not self._summarizing)
        if allowed:
            self.btn_summarize_main.setToolTip("立即总结当前已累计的文本")
        else:
            self.btn_summarize_main.setToolTip("请先在剧情树上设置总结时点")
        if self._summarizing and not self._queue_mode:
            self.status_window.btn_summarize.setEnabled(False)
        else:
            self.status_window.btn_summarize.setEnabled(allowed)

    def _plot_context_limit(self) -> int:
        try:
            limit = int(self.config.get("plot_context_limit", 12000))
        except (TypeError, ValueError):
            return 12000
        return limit if 1000 <= limit <= 100000 else 12000

    def _load_previous_summary(self):
        if self.current_tree_id:
            self.previous_summary = self.db.build_plot_context(
                self.current_tree_id, self._plot_context_limit()
            )
        else:
            self.previous_summary = ""
        if hasattr(self, "btn_summarize_main"):
            self._sync_summarize_buttons()

    def _on_story_cursor_changed(self):
        self._load_previous_summary()

    # ------------------------------------------------------------------
    # 区域选择
    # ------------------------------------------------------------------
    def _select_region(self):
        self.hide()
        self._selectors = []
        for screen in QGuiApplication.screens():
            selector = RegionSelector(screen.geometry())
            selector.selected.connect(self._on_region_selected)
            selector.setScreen(screen)
            selector.setGeometry(screen.geometry())
            selector.showFullScreen()
            self._selectors.append(selector)

    def _on_region_selected(self, rect: QRect):
        for selector in getattr(self, "_selectors", []):
            selector._selected = True
            selector.close()
        self._selectors = []
        self.show()
        if rect.isEmpty():
            self._set_status("取消选择区域")
            return
        self.region = {
            "left": rect.left(),
            "top": rect.top(),
            "width": rect.width(),
            "height": rect.height(),
        }
        self.config["region"] = self.region
        self._save_config()
        self._set_region_label(self.region)
        self._set_status("区域已选择，可开始识别")
        self.region_indicator.set_region(self.region)
        self.btn_region_indicator.setChecked(True)

    def _on_region_indicator_changed(self, region: dict):
        self.region = region
        self.config["region"] = region
        self._save_config()
        self._set_region_label(region)
        if self.ocr_worker and self.ocr_worker.isRunning():
            self.ocr_worker.update_region(region)

    def _toggle_region_indicator(self):
        if self.btn_region_indicator.isChecked():
            if self.region:
                self.region_indicator.set_region(self.region)
            else:
                QMessageBox.information(self, "未选择区域", "请先点击“选择区域”框选一个区域。")
                self.btn_region_indicator.setChecked(False)
        else:
            self.region_indicator.hide()

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------
    def _on_settings_saved(self):
        self.llm_client = LLMClient(self.config)
        new_theme = self.config.get("theme", "dark")
        if new_theme != theme.MODE:
            apply_theme(QApplication.instance(), new_theme)
            self._refresh_theme()
        self._apply_queue_mode_setting()

    def _apply_queue_mode_setting(self):
        """队列模式开关变化后同步界面；正在识别时下次启动才生效。"""
        new_mode = bool(self.config.get("queue_mode", True))
        if new_mode == self._queue_mode:
            self.status_window.set_manual_mode(
                self.config.get("summary_mode", "auto") == "manual"
            )
            return
        if self.ocr_worker is not None or self._draining:
            self.txt_log.append("[队列] 队列模式开关已保存，将在下次开始识别时生效")
            return
        self._queue_mode = new_mode
        self._reset_queue_state()
        self._queue_tree_id = None
        self.card_queue.setVisible(new_mode)
        if new_mode:
            if self._queue_tab_index < 0:
                self._queue_tab_index = self.tabs.addTab(self.queue_tab, "总结队列")
        elif self._queue_tab_index >= 0:
            self.tabs.removeTab(self._queue_tab_index)
            self._queue_tab_index = -1
        self.status_window.set_queue_mode(new_mode)
        self.status_window.set_manual_mode(
            self.config.get("summary_mode", "auto") == "manual"
        )

    def _refresh_theme(self):
        """主题切换后刷新依赖代码着色的部分（QSS 部分自动生效）"""
        self._shadow.setColor(theme.shadow_color())
        self.status_window.refresh_theme()
        self._set_status(self.lbl_status.text())
        self.region_indicator.update()
        if hasattr(self, "story_page"):
            self.story_page.reload()

    # ------------------------------------------------------------------
    # 状态浮窗
    # ------------------------------------------------------------------
    def _toggle_status_window(self):
        if self.status_window.isVisible():
            self.status_window.hide()
            self.btn_status.setChecked(False)
        else:
            self.status_window.show()
            self.status_window.update_state(status=self.lbl_status.text())
            self.status_window.update_summary_state("空闲")
            running = self.ocr_worker is not None and self.ocr_worker.isRunning()
            self.status_window.set_ocr_running(running)
            self.btn_status.setChecked(True)

    # ------------------------------------------------------------------
    # OCR 测试
    # ------------------------------------------------------------------
    def _ensure_paddle_plugin(self) -> bool:
        """选择 paddle 后端时检查旁路插件；未安装则提示并返回 False。"""
        backend = normalize_backend(self.config.get("ocr_backend", "rapid"))
        if backend not in (BACKEND_CPU, BACKEND_GPU):
            return True
        device = backend_to_device(backend)
        # 确保后续 OCR 使用主程序目录作为插件根
        get_manager(APP_DIR)
        status = environment_ready(device, APP_DIR)
        if status.installed:
            return True
        QMessageBox.warning(
            self,
            "环境未通过",
            f"当前 OCR 后端为 {backend}，环境检测未通过。\n\n"
            f"{status.detail}\n\n"
            "请到「设置」页点击「安装环境」，或改回 rapid（内置）。",
        )
        return False

    def _test_ocr_once(self):
        if not self.region:
            QMessageBox.warning(self, "未选择区域", "请先点击“选择区域”框选游戏文字区域。")
            return
        if not self._ensure_paddle_plugin():
            return
        self._set_status("OCR 测试中")
        self.test_thread = TestOCRThread(
            self.region,
            self.config.get("ocr_lang", "chi_sim"),
            self.config.get("tesseract_path", ""),
            self.config.get("ocr_backend", "rapid"),
            int(self.config.get("det_limit_side_len", 256)),
        )
        self.test_thread.result_ready.connect(self._on_test_ocr_result)
        self.test_thread.error.connect(self._on_test_ocr_error)
        self.test_thread.start()

    def _on_test_ocr_result(self, result: dict):
        self._set_status("就绪")
        conf = result.get("confidence", -1.0)
        conf_text = f"{conf * 100:.1f}%" if conf >= 0 else "—"
        msg = (
            f"识别内容：\n{result.get('text', '')}\n\n"
            f"耗时：{result.get('elapsed_ms', 0)} ms\n"
            f"置信度：{conf_text}\n"
            f"后端：{result.get('backend', '')}"
        )
        QMessageBox.information(self, "OCR 测试结果", msg)

    def _on_test_ocr_error(self, msg: str):
        self._set_status("就绪")
        QMessageBox.critical(self, "OCR 测试失败", f"错误：\n{msg}")

    # ------------------------------------------------------------------
    # 开始 / 暂停识别
    # ------------------------------------------------------------------
    def _toggle_ocr(self):
        """浮窗暂停/继续按钮入口"""
        if self.ocr_worker and self.ocr_worker.isRunning():
            self._stop_ocr()
        else:
            self._start_ocr()

    def _start_ocr(self):
        if not self.region:
            QMessageBox.warning(self, "未选择区域", "请先点击“选择区域”框选游戏文字区域。")
            return
        if not self._check_api_config():
            return
        if not self._ensure_game_route():
            return
        if not self._ensure_paddle_plugin():
            return
        if self.ocr_worker and self.ocr_worker.isRunning():
            return

        # 加载上文
        self._load_previous_summary()

        # 启动会话
        self._current_session_id = self.db.start_session(self.current_route_id)
        self.session_start = datetime.datetime.now()
        self.manual_sentence_count = 0
        self.status_window.update_count(0)
        self.status_window.set_manual_mode(self.config.get("summary_mode", "auto") == "manual")

        self.txt_log.append("开始 OCR 识别...")
        self._prepare_ocr_transcript()
        if self._queue_mode:
            # 换线路才清空；同线路暂停后继续时保留未总结的帧文本与排队任务
            if self._queue_tree_id != self.current_tree_id:
                self._reset_queue_state()
            self._queue_tree_id = self.current_tree_id
            self.ocr_worker = QueuedOCRWorker(
                self.region,
                self.config.get("ocr_interval_ms", 1500),
                self.config.get("ocr_lang", "chi_sim"),
                self.config.get("tesseract_path", ""),
                self.config.get("ocr_backend", "rapid"),
                backlog_limit=int(self.config.get("queue_backlog_limit", 300)),
                det_limit=int(self.config.get("det_limit_side_len", 256)),
                start_seq=self._frame_seq_base,
                frame_diff_threshold=int(
                    self.config.get("frame_diff_threshold", FRAME_DIFF_THRESHOLD)
                ),
                text_sim_threshold=float(
                    self.config.get("text_similarity_threshold", TEXT_SIM_THRESHOLD)
                ),
            )
            self.ocr_worker.frame_enqueued.connect(self._on_frame_enqueued)
            self.ocr_worker.frame_recognized.connect(self._on_frame_recognized)
            self.ocr_worker.frame_dropped.connect(self._on_frame_dropped)
            self.ocr_worker.error.connect(self._on_ocr_error)
            self.ocr_worker.start()
            self._draining = False
            self._queue_timer.start()
            self._update_queue_ui()
        else:
            self.ocr_worker = OCRWorker(
                self.region,
                self.config.get("ocr_interval_ms", 1500),
                self.config.get("ocr_lang", "chi_sim"),
                self.config.get("tesseract_path", ""),
                self.config.get("ocr_backend", "rapid"),
                queue_size=int(self.config.get("ocr_queue_size", 3)),
                det_limit=int(self.config.get("det_limit_side_len", 256)),
                frame_diff_threshold=int(
                    self.config.get("frame_diff_threshold", FRAME_DIFF_THRESHOLD)
                ),
                text_sim_threshold=float(
                    self.config.get("text_similarity_threshold", TEXT_SIM_THRESHOLD)
                ),
            )
            self.ocr_worker.text_ready.connect(self._on_ocr_text)
            self.ocr_worker.error.connect(self._on_ocr_error)
            self.ocr_worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        # 识别期间锁定线路切换与管理，避免记录串档
        self.cmb_game.setEnabled(False)
        self.cmb_route.setEnabled(False)
        self.gameroute_page.setEnabled(False)
        self._set_status("识别中")
        self.status_window.update_state(status="识别中")
        self.status_window.set_ocr_running(True)

    def _stop_ocr(self):
        drained = False
        if isinstance(self.ocr_worker, QueuedOCRWorker):
            worker = self.ocr_worker
            self._frame_seq_base = max(self._frame_seq_base, worker.frame_seq_high_water())
            _done, _total, pending, _dropped = worker.progress()
            worker.stop(drain=True)
            self.ocr_worker = None
            if worker.isRunning():
                # 让消费者把已入队的帧识别完，避免暂停时丢剧情
                self._draining_workers.append(worker)
                worker.finished.connect(self._on_drain_worker_finished)
                if pending or self.summary_queue or self._summarizing:
                    drained = True
                    self._draining = True
                    self._queue_timer.start()
                    self.txt_log.append(f"[队列] 暂停截图，正在识别剩余 {pending} 帧…")
            if not drained:
                self._queue_timer.stop()
        elif self.ocr_worker:
            self.ocr_worker.stop()
            self.ocr_worker = None

        if self._current_session_id:
            self.db.pause_session(self._current_session_id)
            self._last_session_id = self._current_session_id
            self._current_session_id = None
        if self.session_start:
            self.session_start = None
        self._update_today_time()

        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.cmb_game.setEnabled(True)
        self.cmb_route.setEnabled(True)
        self.gameroute_page.setEnabled(True)
        status = "排空中" if drained else "已暂停"
        self._set_status(status)
        self.status_window.update_state(status=status)
        self.status_window.set_ocr_running(False)
        if self._queue_mode:
            self._update_queue_ui()

    # ------------------------------------------------------------------
    # OCR 文本处理与总结触发
    # ------------------------------------------------------------------
    def _ocr_transcript_path(self) -> str:
        game = self.db.get_game(self.current_game_id) if self.current_game_id else None
        route = self.db.get_route(self.current_route_id) if self.current_route_id else None
        game_name = _safe_path_part((game or {}).get("name") or "未命名游戏")
        route_name = _safe_path_part((route or {}).get("name") or "未命名线路")
        folder = os.path.join(APP_DIR, "-游戏原文保存")
        return os.path.join(folder, f"{game_name}_{route_name}.txt")

    def _prepare_ocr_transcript(self):
        """开始识别时确保原文文件存在。同一游戏同一线路始终追加，不另建文件。"""
        self._ocr_transcript_logged = False
        if not self.config.get("save_ocr_text"):
            return
        path = self._ocr_transcript_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8-sig", newline="\n") as handle:
                    handle.write("")
            self.txt_log.append(f"[原文] 识别原文追加到 {path}")
            self._ocr_transcript_logged = True
        except Exception as exc:
            self.txt_log.append(f"[原文] 无法创建原文文件：{exc}")

    def _append_ocr_transcript(self, text: str):
        if not self.config.get("save_ocr_text"):
            return
        body = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not body:
            return
        path = self._ocr_transcript_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            new_file = not os.path.exists(path)
            encoding = "utf-8-sig" if new_file else "utf-8"
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding=encoding, newline="\n") as handle:
                handle.write(f"[{stamp}]\n{body}\n\n")
                handle.flush()
            if not self._ocr_transcript_logged:
                self.txt_log.append(f"[原文] 识别原文追加到 {path}")
                self._ocr_transcript_logged = True
        except Exception as exc:
            if not self._ocr_transcript_logged:
                self.txt_log.append(f"[原文] 写入失败：{exc}")
                self._ocr_transcript_logged = True

    def _on_ocr_text(self, text: str):
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt_current.setText(text)
        self.txt_log.append(f"[{now_str}] {text}")
        self._append_ocr_transcript(text)
        self.status_window.update_state(current=text)

        with self.buffer_lock:
            self.text_buffer.append(text)
            if len(self.text_buffer) > 100:
                self.text_buffer = self.text_buffer[-100:]
            sentence_count = self._count_sentences("\n".join(self.text_buffer))

        # 始终更新累计句数，并允许手动总结
        self.manual_sentence_count = sentence_count
        self.status_window.update_count(sentence_count)

        # 自动模式：按句数触发
        if self.config.get("summary_mode", "auto") == "auto":
            if sentence_count >= self.config.get("summary_sentence_count", 50):
                self._trigger_summary()

    def _count_sentences(self, text: str) -> int:
        """按中文标点断句"""
        parts = re.split(r"[。！？…；\n]+", text)
        return len([p for p in parts if p.strip()])

    def _split_last_sentence(self, text: str) -> tuple[str, str]:
        """把 text 的最后一句切分出来，返回 (前面部分, 最后一句)。

        如果只有一句或没有可切分的位置，则返回 (text, '')。
        """
        # 按中文标点/换行切分，保留分隔符
        parts = re.split(r"([。！？…；\n]+)", text)
        # parts 形如: ['s1', 'd1', 's2', 'd2', 's3', 'd3', '']
        # 过滤末尾空字符串
        while parts and parts[-1] == "":
            parts = parts[:-1]
        if not parts:
            return text, ""
        # 现在 parts 最后一个元素：若不是分隔符，说明结尾没有标点
        if parts[-1] in "。！？…；\n":
            # 以标点结尾，最后完整句是 parts[-3] + parts[-2]
            if len(parts) < 4:
                return text, ""
            last_sentence = parts[-3] + parts[-2]
            earlier = "".join(parts[:-3]).rstrip()
        else:
            # 没有结尾标点，最后一句就是 parts[-1]
            if len(parts) < 2:
                return text, ""
            last_sentence = parts[-1]
            earlier = "".join(parts[:-1]).rstrip()
        return earlier, last_sentence.strip()

    def _on_ocr_error(self, msg: str):
        self.txt_log.append(f"[OCR错误] {msg}")

    # ------------------------------------------------------------------
    # 队列模式（v1.1）：帧文本簿、锚点水位与总结任务队列
    # ------------------------------------------------------------------
    def _reset_queue_state(self):
        """切换线路或切换模式时清空帧文本簿、任务队列与进度计数。"""
        with self.buffer_lock:
            self.frame_texts = []
        self.summary_queue.clear()
        self._summary_task_log = []
        self._summary_task_seq = 0
        self._current_summary_task = None
        self._last_anchor_id = 0
        self._recognized_upto = 0
        self._recognized_ids = set()
        self._frame_id_high_water = 0
        self._frames_done = 0
        self._frames_enqueued = 0
        self._frames_dropped = 0
        self._frame_seq_base = 0
        self._queue_table_sig = None

    def _advance_recognized_water(self, frame_id: int):
        """推进连续完成水位：只有 1..N 全部有结果时水位才到 N。"""
        if frame_id <= self._recognized_upto:
            return
        self._recognized_ids.add(frame_id)
        while (self._recognized_upto + 1) in self._recognized_ids:
            self._recognized_upto += 1
            self._recognized_ids.discard(self._recognized_upto)

    def _forget_frames_upto(self, frame_id: int):
        """放弃 frame_id 及之前尚未识别的帧（立即停止时调用），避免锚点永久等待。"""
        if frame_id > self._recognized_upto:
            self._recognized_upto = frame_id
            self._recognized_ids = {f for f in self._recognized_ids if f > frame_id}

    def _on_frame_enqueued(self, frame_id: int, enqueued_total: int):
        self._frames_enqueued += 1
        if frame_id > self._frame_id_high_water:
            self._frame_id_high_water = frame_id
        self._update_queue_ui()

    def _on_frame_dropped(self, frame_id: int, dropped_total: int):
        self._frames_dropped = dropped_total
        self._advance_recognized_water(frame_id)
        if dropped_total == 1 or dropped_total % 20 == 0:
            self.txt_log.append(
                f"[队列] 积压超过上限，已丢弃最旧帧 {frame_id}（累计丢弃 {dropped_total} 帧），"
                f"可提高设置中的「队列积压上限」或放慢阅读速度"
            )
        self._update_queue_ui()
        self._pump_summary_queue()

    def _on_frame_recognized(self, frame_id: int, text: str, done_total: int,
                             enqueued_total: int, pending: int):
        """队列模式下每帧识别完成的回调（替代 _on_ocr_text）。"""
        self._frames_done += 1
        if frame_id > self._frame_id_high_water:
            self._frame_id_high_water = frame_id
        self._advance_recognized_water(frame_id)

        if text:
            now_str = datetime.datetime.now().strftime("%H:%M:%S")
            self.txt_current.setText(text)
            self.txt_log.append(f"[{now_str}] [帧{frame_id}] {text}")
            self._append_ocr_transcript(text)
            with self.buffer_lock:
                self.frame_texts.append((frame_id, text))

        # 未总结区间的句数：既用于显示，也用于 auto 模式触发
        with self.buffer_lock:
            pending_text = "\n".join(
                t for fid, t in self.frame_texts if fid > self._last_anchor_id and t
            )
        self.manual_sentence_count = self._count_sentences(pending_text)
        self.status_window.update_count(self.manual_sentence_count)

        if self.config.get("summary_mode", "auto") == "auto":
            if self.manual_sentence_count >= self.config.get("summary_sentence_count", 50):
                self._enqueue_summary_task(anchor_frame_id=frame_id)

        self._update_queue_ui()
        self._pump_summary_queue()

    def _enqueue_summary_task(self, anchor_frame_id: Optional[int] = None) -> bool:
        """以「点击瞬间最新已入队帧的前一帧」为锚点，把一段剧情加入总结队列。"""
        if not self._summarize_allowed():
            if not getattr(self, "_cursor_warned", False):
                self.txt_log.append("[总结] 未设置总结时点，已跳过。")
                self.toasts.show("请先在剧情树上设置总结时点", kind="warn", duration_ms=3000)
                self._cursor_warned = True
            return False
        self._cursor_warned = False
        if anchor_frame_id is not None:
            anchor = anchor_frame_id
        elif isinstance(self.ocr_worker, QueuedOCRWorker):
            anchor = self.ocr_worker.prev_enqueued_frame_id()
        else:
            # 识别已停止：把已知的所有帧都纳入本次总结
            anchor = self._frame_id_high_water

        if anchor <= self._last_anchor_id:
            self.txt_log.append("[队列] 自上次总结锚点以来没有新画面，忽略本次总结请求")
            self.toasts.show("没有新画面，无需总结", kind="warn", duration_ms=3000)
            return False

        self._summary_task_seq += 1
        task = SummaryTask(
            anchor_frame_id=anchor,
            start_frame_id=self._last_anchor_id,
            session_id=self._current_session_id or self._last_session_id,
            queue_seq=self._summary_task_seq,
        )
        self._last_anchor_id = anchor
        self.summary_queue.append(task)
        self._summary_task_log.append(task)
        self.txt_log.append(
            f"[队列] 已加入总结任务：帧 {task.start_frame_id + 1}-{anchor}"
            f"（当前已识别至帧 {self._recognized_upto}）"
        )

        unread = max(0, anchor - self._recognized_upto)
        waiting_before = len(self.summary_queue) - 1 + (1 if self._summarizing else 0)
        tip = "已加入总结队列"
        if unread:
            tip += f"，还需识别 {unread} 帧"
        else:
            tip += "，本段已识别完成"
        if waiting_before:
            tip += f"；前面还有 {waiting_before} 个任务"
        self.toasts.show(tip, kind="info")

        self._pump_summary_queue()
        self._update_queue_ui()
        return True

    def _take_frame_texts(self, start_frame_id: int, anchor_frame_id: int) -> str:
        """取出 (start, anchor] 区间的识别文本，并裁掉锚点及之前的帧记录。"""
        with self.buffer_lock:
            picked = sorted(
                (fid, t) for fid, t in self.frame_texts
                if start_frame_id < fid <= anchor_frame_id and t
            )
            self.frame_texts = [
                (fid, t) for fid, t in self.frame_texts if fid > anchor_frame_id
            ]
        return "\n".join(t for _fid, t in picked)

    def _pump_summary_queue(self):
        """调度总结队列：锚点及之前全部识别完成后才串行发起总结。"""
        if not self._queue_mode or self._summarizing:
            return
        while self.summary_queue:
            task = self.summary_queue[0]
            if task.state == "待重试":
                self.summary_queue.popleft()
                text = (task.raw_text or "").strip()
                if not text:
                    task.state = "失败"
                    task.error = task.error or "原文已丢失，无法重试"
                    self.txt_log.append(
                        f"[队列] 帧 {task.start_frame_id + 1}-{task.anchor_frame_id} 原文已丢失，无法重试"
                    )
                    continue
                task.state = "总结中"
                task.error = ""
                self._current_summary_task = task
                self._start_queued_summary(text, task.session_id)
                self._update_queue_ui()
                return
            if task.anchor_frame_id > self._recognized_upto:
                task.state = "等待识别"
                self._update_queue_ui()
                return
            self.summary_queue.popleft()
            text = self._take_frame_texts(task.start_frame_id, task.anchor_frame_id)
            if not text.strip():
                task.state = "已跳过"
                task.raw_text = ""
                self.txt_log.append(
                    f"[队列] 帧 {task.start_frame_id + 1}-{task.anchor_frame_id} 无有效文本，跳过总结"
                )
                self.toasts.show("这段没识别到文字，已跳过总结", kind="warn", duration_ms=4000)
                continue
            task.raw_text = text
            task.error = ""
            task.state = "总结中"
            self._current_summary_task = task
            self._start_queued_summary(text, task.session_id)
            self._update_queue_ui()
            return
        self._update_queue_ui()

    def _start_queued_summary(self, text: str, session_id: Optional[int]):
        """队列模式下发起一次总结（锚点即段落边界，不再切分最后一句）。"""
        self._summary_session_id = session_id or self._current_session_id or self._last_session_id
        self._summarizing = True
        # 队列模式下总结按钮保持可用：用户读得快时要能继续追加锚点任务
        self._set_summary_status("总结中")
        self.status_window.update_summary_state("总结中")
        shared_context, route_name = self._shared_knowledge_prompt()
        self.summary_thread = SummaryThread(
            self.llm_client,
            text,
            self.previous_summary,
            auto_extract=self.config.get("auto_extract_key_info", True),
            extra_prompt=self._game_prompt(),
            shared_context=shared_context,
            current_route=route_name,
            system_prompt=self._summary_system_prompt(),
        )
        self.summary_thread.summary_ready.connect(self._on_summary_ready)
        self.summary_thread.error.connect(self._on_summary_error)
        self.summary_thread.finished.connect(self._on_summary_thread_finished)
        self.summary_thread.start()

    def _on_queue_tick(self):
        """定时兜底：刷新进度并推进队列（信号漏触发时也能恢复）。"""
        self._update_queue_ui()
        self._pump_summary_queue()
        if self._draining and not self._queue_pending_frames() and not self.summary_queue \
                and not self._summarizing:
            self._finish_draining()

    def _queue_pending_frames(self) -> int:
        """当前识别线程与排空中线程的待识别帧总数。"""
        total = 0
        workers = list(self._draining_workers)
        if isinstance(self.ocr_worker, QueuedOCRWorker):
            workers.append(self.ocr_worker)
        for worker in workers:
            _done, _enqueued, pending, _dropped = worker.progress()
            total += pending
        return total

    def _summary_queue_waiting(self) -> int:
        """排队中的总结任务数（含正在总结的那个）。"""
        return len(self.summary_queue) + (1 if self._current_summary_task and self._summarizing else 0)

    def _finish_draining(self):
        """暂停后队列排空且总结跑完：恢复为已暂停状态。"""
        self._draining = False
        self._queue_timer.stop()
        self._set_status("已暂停")
        self.status_window.update_state(status="已暂停")
        self.txt_log.append("[队列] 剩余画面已全部识别完成")
        self.toasts.show("剩余画面已全部识别完成", kind="ok", duration_ms=3000)
        self._update_queue_ui()

    def _on_drain_worker_finished(self):
        """排空中的识别线程结束后释放引用。"""
        worker = self.sender()
        if worker in self._draining_workers:
            self._draining_workers.remove(worker)

    def _update_queue_ui(self):
        """刷新浮窗与主界面的识别/总结队列进度。"""
        if not self._queue_mode or not hasattr(self, "lbl_queue_ocr"):
            return
        total = max(0, self._frames_enqueued - self._frames_dropped)
        done = min(self._frames_done, total)
        pending = max(0, total - done)

        self.bar_queue_ocr.setMaximum(max(1, total))
        self.bar_queue_ocr.setValue(done)
        ocr_text = f"已识别 / 已累计：{done} / {total}"
        if pending:
            ocr_text += f"　待识别 {pending}"
        if self._frames_dropped:
            ocr_text += f"　已丢弃 {self._frames_dropped}"
        self.lbl_queue_ocr.setText(ocr_text)

        task_total = len(self._summary_task_log)
        task_done = sum(
            1 for t in self._summary_task_log if t.state in ("已完成", "已跳过")
        )
        waiting = self._summary_queue_waiting()
        self.bar_queue_summary.setMaximum(max(1, task_total))
        self.bar_queue_summary.setValue(task_done)
        summary_text = f"总结任务：{task_done} / {task_total}"
        if waiting:
            head = self.summary_queue[0] if self.summary_queue else None
            if self._summarizing:
                summary_text += f"　排队 {waiting}　当前总结中"
            elif head is not None and head.anchor_frame_id > self._recognized_upto:
                summary_text += (
                    f"　排队 {waiting}　等待识别至帧 {head.anchor_frame_id}"
                    f"（当前 {self._recognized_upto}）"
                )
            else:
                summary_text += f"　排队 {waiting}"
        self.lbl_queue_summary.setText(summary_text)

        self.status_window.update_queue_progress(done, total, waiting)
        failed = [task for task in self._summary_task_log if task.state == "失败"]
        if hasattr(self, "queue_retry_row"):
            self.queue_retry_row.setVisible(bool(failed))
            self.lbl_queue_failed.setText(f"{len(failed)} 个总结失败")
        self._refresh_queue_table()

    def _retry_all_failed(self):
        failed = [
            task for task in self._summary_task_log
            if task.state == "失败" and (task.raw_text or "").strip()
        ]
        if not failed:
            self.toasts.show("没有可重试的失败任务", kind="warn", duration_ms=4000)
            self._update_queue_ui()
            return
        failed.sort(key=lambda task: task.queue_seq)
        for task in reversed(failed):
            task.state = "待重试"
            task.error = ""
            if not any(item is task for item in self.summary_queue):
                self.summary_queue.appendleft(task)
        self.txt_log.append(f"[队列] 批量重试 {len(failed)} 条失败总结")
        self.toasts.show(f"已重新加入 {len(failed)} 条总结", kind="info")
        self._pump_summary_queue()
        self._update_queue_ui()

    def _retry_summary_task(self, task: SummaryTask):
        text = (task.raw_text or "").strip()
        if not text:
            self.toasts.show("这段原文已经不在了，无法重试", kind="warn", duration_ms=4000)
            return
        if task.state in ("总结中", "待重试"):
            return
        task.state = "待重试"
        task.error = ""
        if not any(item is task for item in self.summary_queue):
            self.summary_queue.appendleft(task)
        self.txt_log.append(
            f"[队列] 重试总结：帧 {task.start_frame_id + 1}-{task.anchor_frame_id}"
        )
        self.toasts.show("已重新加入总结队列", kind="info")
        self._pump_summary_queue()
        self._update_queue_ui()

    def _visible_summary_tasks(self) -> list:
        """失败和未完成的任务始终留在表里，方便重试。"""
        pinned = {
            id(task) for task in self._summary_task_log
            if task.state not in ("已完成", "已跳过")
        }
        tail = {id(task) for task in self._summary_task_log[-50:]}
        show = pinned | tail
        return [task for task in self._summary_task_log if id(task) in show]

    def _refresh_queue_table(self):
        if not hasattr(self, "tbl_queue"):
            return
        tasks = self._visible_summary_tasks()
        sig = tuple((id(task), task.state, task.anchor_frame_id) for task in tasks)
        if sig == self._queue_table_sig:
            return
        self._queue_table_sig = sig
        self.tbl_queue.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            values = [
                str(task.anchor_frame_id),
                f"{task.start_frame_id + 1} - {task.anchor_frame_id}",
                task.state,
                task.created_at.strftime("%H:%M:%S"),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col == 2 and task.error:
                    item.setToolTip(task.error)
                self.tbl_queue.setItem(row, col, item)
            old_btn = self.tbl_queue.cellWidget(row, 4)
            if old_btn is not None:
                self.tbl_queue.removeCellWidget(row, 4)
                old_btn.deleteLater()
            if task.state == "失败" and (task.raw_text or "").strip():
                btn = QPushButton("重试")
                set_variant(btn, "ghost")
                btn.setFixedHeight(26)
                btn.setToolTip("用这段已识别原文再总结一次")
                btn.clicked.connect(lambda _checked=False, current=task: self._retry_summary_task(current))
                self.tbl_queue.setCellWidget(row, 4, btn)

    def _game_prompt(self) -> str:
        """游戏提示词，再加上当前剧情树的补充说明。"""
        parts = []
        if self.current_game_id:
            game = self.db.get_game(self.current_game_id)
            text = ((game or {}).get("prompt") or "").strip()
            if text:
                parts.append(text)
        if self.current_tree_id:
            tree = self.db.get_story_tree(self.current_tree_id)
            text = ((tree or {}).get("prompt") or "").strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    def _shared_knowledge_prompt(self) -> tuple:
        """共享设定，以及时点所在线路的名称。还在主链上时用剧情树名。"""
        if not self.current_route_id:
            return "", ""
        label = self.db.story_route_label(self.current_tree_id) if self.current_tree_id else ""
        return self.db.format_shared_context(self.current_route_id), label

    def _summary_system_prompt(self) -> str:
        try:
            row = self.db.get_active_summary_prompt()
            text = ((row or {}).get("system_prompt") or "").strip()
            if text:
                return text
        except Exception:
            pass
        return SUMMARY_PROMPT_BRIEF

    def _trigger_summary(self, session_id: Optional[int] = None):
        if not self._summarize_allowed():
            if not getattr(self, "_cursor_warned", False):
                self.txt_log.append("[总结] 未设置总结时点，已跳过。请在剧情树上设置时点。")
                self._cursor_warned = True
            return
        self._cursor_warned = False
        with self.buffer_lock:
            if not self.text_buffer or self._summarizing:
                return
            full_text = "\n".join(self.text_buffer)
            recent_text, keep = self._split_last_sentence(full_text)
            self.text_buffer.clear()
            if keep:
                self.text_buffer.append(keep)

        self._summary_session_id = session_id or self._current_session_id or self._last_session_id
        self._summarizing = True
        self._inflight_summary_text = recent_text
        self.btn_summarize_main.setEnabled(False)
        self._set_status("识别中")
        self._set_summary_status("总结中")
        self.status_window.update_state(status="识别中")
        self.status_window.update_summary_state("总结中")
        shared_context, route_name = self._shared_knowledge_prompt()
        self.summary_thread = SummaryThread(
            self.llm_client,
            recent_text,
            self.previous_summary,
            auto_extract=self.config.get("auto_extract_key_info", True),
            extra_prompt=self._game_prompt(),
            shared_context=shared_context,
            current_route=route_name,
            system_prompt=self._summary_system_prompt(),
        )
        self.summary_thread.summary_ready.connect(self._on_summary_ready)
        self.summary_thread.error.connect(self._on_summary_error)
        self.summary_thread.finished.connect(self._on_summary_thread_finished)
        self.summary_thread.start()

    def _manual_summarize(self):
        if not self._summarize_allowed():
            QMessageBox.information(
                self,
                "未设置总结时点",
                "请先打开剧情树，选中位置后点「设为总结时点」。空树可以直接总结第一段。",
            )
            return
        if self._queue_mode:
            # 队列模式：以点击瞬间的锚点入队，等该段识别完成后再总结
            self._enqueue_summary_task()
            return
        if not self.text_buffer:
            return
        self._trigger_summary()

    def _apply_extracted_key_info(self, item: dict, summary_id: Optional[int], game_id: Optional[int]):
        """写入或修订共享关键信息。update_id 只接受本游戏已有条目，避免模型乱改。"""
        category = item.get("category", "其他")
        content = (item.get("content") or "").strip()
        detail = (item.get("detail") or "").strip()
        update_id = item.get("update_id")
        if category == "角色信息":
            if update_id and self.db.character_in_game(int(update_id), game_id):
                char = self.db.get_character(int(update_id)) or {}
                name = content or char.get("name") or ""
                info = self.db._merge_character_info(char.get("info") or "", detail)
                if name:
                    self.db.update_character(int(update_id), name, info)
                return
            self._persist_character_info(item)
            return
        if not content:
            return
        if update_id and self.db.key_info_in_game(int(update_id), game_id):
            old = self.db.get_key_info(int(update_id)) or {}
            note = old.get("user_note") or ""
            merged = content
            if detail and detail not in content:
                merged = f"{content}；{detail}"
            self.db.update_key_info(int(update_id), merged, category or old.get("category") or "其他", note)
            return
        self.db.add_key_info(
            self.current_route_id,
            content=content,
            category=category,
            user_note="",
            summary_id=summary_id,
        )

    def _persist_character_info(self, item: dict):
        """保存角色信息，先与已有角色做 AI 对比去重"""
        route_id = self.current_route_id
        name = item.get("content", "").strip()
        info = item.get("detail", "").strip()
        if not name:
            return

        # fast path：基础名完全相同的直接补充
        def _base_name(n: str) -> str:
            import re as _re
            return _re.split(r"[（(]", n)[0].strip()

        existing = self.db.list_characters(route_id)
        for char in existing:
            if _base_name(char["name"]) == _base_name(name):
                new_name = char["name"]
                if name not in new_name and new_name != name:
                    new_name = name
                new_info = self.db._merge_character_info(char.get("info") or "", info)
                self.db.update_character(char["id"], new_name, new_info)
                return

        # AI 对比
        if existing:
            candidates = [{"id": c["id"], "name": c["name"], "info": c["info"]} for c in existing]
            matched_id = self.llm_client.match_character(name, candidates)
            if matched_id:
                for char in existing:
                    if char["id"] == matched_id:
                        new_name = char["name"]
                        if name not in new_name and new_name != name:
                            new_name = name
                        new_info = self.db._merge_character_info(char.get("info") or "", info)
                        self.db.update_character(matched_id, new_name, new_info)
                        return

        # 新建
        self.db.add_or_update_character(route_id, name=name, info=info)

    def _summary_insert_before(self, task: Optional[SummaryTask]) -> Optional[int]:
        """重试时，插到比它更晚、且已经写进剧情树的那段之前。"""
        if task is None:
            return None
        later_ids = [
            other.node_id
            for other in self._summary_task_log
            if other is not task and other.node_id and other.queue_seq > task.queue_seq
        ]
        if not later_ids:
            return None
        return self.db.earliest_story_node(later_ids)

    def _on_summary_ready(self, summary: str, raw_text: str, key_info_json: str, title: str = ""):
        self._summarizing = False
        self._inflight_summary_text = ""
        task = self._current_summary_task
        before_id = self._summary_insert_before(task)

        node_id = None
        try:
            if not self.current_tree_id:
                raise StoryTreeError("请先选择剧情树")
            if before_id:
                node_id = self.db.insert_beat_before(
                    self.current_tree_id, before_id, summary, title=title, raw_text=raw_text
                )
            else:
                node_id = self.db.insert_beat_at_cursor(
                    self.current_tree_id, summary, title=title, raw_text=raw_text
                )
        except StoryTreeError as exc:
            self.txt_log.append(f"[总结] 未能写入剧情树：{exc}")
            self.toasts.show(str(exc), kind="warn", duration_ms=5000)
            if task is not None:
                task.state = "失败"
                task.error = str(exc)
                self._current_summary_task = None
            self._load_previous_summary()
            self._set_summary_status("总结失败")
            self.status_window.update_summary_state("总结失败")
            if self._queue_mode:
                self._update_queue_ui()
                self._pump_summary_queue()
            return

        self.previous_summary = self.db.build_plot_context(
            self.current_tree_id, self._plot_context_limit()
        )
        if task is not None:
            task.node_id = node_id
            task.state = "已完成"
            task.raw_text = ""
            task.error = ""
            self._current_summary_task = None

        try:
            key_info_list = json.loads(key_info_json)
        except Exception:
            key_info_list = []
        if key_info_list and self.current_route_id:
            game_id = self.db.game_id_of_route(self.current_route_id)
            for item in key_info_list:
                try:
                    self._apply_extracted_key_info(item, None, game_id)
                except Exception:
                    pass

        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt_summary.append(f"[{now_str}] {summary}\n")

        chapter = title.strip() if title else ""
        if chapter:
            self.toasts.show(f"「{chapter}」已总结完成", kind="ok", duration_ms=5000)
        else:
            self.toasts.show("本段剧情已总结完成", kind="ok", duration_ms=5000)
        self._show_summary_done()
        self.story_page.reload()
        self._sync_summarize_buttons()
        if self.stack.currentIndex() == 2:
            self.keyinfo_page.reload()

        if self._queue_mode:
            # 锚点即段落边界，剩余未总结区间的句数按帧文本簿重算
            with self.buffer_lock:
                pending_text = "\n".join(
                    t for fid, t in self.frame_texts if fid > self._last_anchor_id and t
                )
            self.manual_sentence_count = self._count_sentences(pending_text)
            self.status_window.update_count(self.manual_sentence_count)
            self._pump_summary_queue()
        else:
            # 保留当前缓冲区中的最后一句供下次总结使用，句数同步更新
            self.manual_sentence_count = self._count_sentences("\n".join(self.text_buffer))
            self.status_window.update_count(self.manual_sentence_count)

    def _show_summary_done(self):
        """在主界面和浮窗中显示总结完成反馈"""
        self._set_summary_status("总结完成")
        self.status_window.update_summary_state("总结完成")
        self.txt_log.append("[总结完成]")
        QTimer.singleShot(2000, self._restore_running_status)

    def _on_summary_error(self, msg: str):
        self._summarizing = False
        self._sync_summarize_buttons()
        if self._current_summary_task is not None:
            self._current_summary_task.state = "失败"
            self._current_summary_task.error = msg
            self._current_summary_task = None
        elif self._inflight_summary_text:
            with self.buffer_lock:
                self.text_buffer.insert(0, self._inflight_summary_text)
            self.manual_sentence_count = self._count_sentences("\n".join(self.text_buffer))
            self.status_window.update_count(self.manual_sentence_count)
            self.txt_log.append("[总结] 失败的原文已放回缓冲区，可再次点击总结")
        self._inflight_summary_text = ""
        self.txt_summary.append(f"[总结错误] {msg}")
        self.toasts.show(f"总结失败：{msg}", kind="error", duration_ms=6000)
        self._set_summary_status("总结失败")
        self.status_window.update_summary_state("总结失败")
        QTimer.singleShot(2000, self._restore_running_status)
        if self._queue_mode:
            self._update_queue_ui()
            self._pump_summary_queue()

    def _on_summary_thread_finished(self):
        """总结线程结束后清理引用，避免对象累积"""
        if self.sender() is self.summary_thread:
            self.summary_thread = None

    def _restore_running_status(self):
        """总结结束后根据 OCR 是否仍在运行恢复状态显示"""
        running = self.ocr_worker is not None and self.ocr_worker.isRunning()
        self.status_window.set_ocr_running(running)
        if self._summarizing:
            # 队列中的下一个任务已经开始，不要把状态改回空闲
            self.status_window.update_summary_state("总结中")
            self._set_summary_status("总结中")
        else:
            self.status_window.update_summary_state("空闲")
            self._set_summary_status("空闲")
        if running:
            self._set_status("识别中")
            self.status_window.update_state(status="识别中")
        elif self._draining:
            self._set_status("排空中")
            self.status_window.update_state(status="排空中")
        else:
            self._set_status("已暂停")
            self.status_window.update_state(status="已暂停")

    # ------------------------------------------------------------------
    # 计时
    # ------------------------------------------------------------------
    def _update_today_time(self):
        seconds = self.db.today_seconds()
        self.lbl_today.setText(fmt_duration(seconds))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fixing_size:
            return
        if (
            self._title_dragging
            and self._stable_size is not None
            and event.size() != self._stable_size
        ):
            self._fixing_size = True
            self.resize(self._stable_size)
            self._fixing_size = False
            return
        if not self._title_dragging:
            self._stable_size = event.size()

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        if self._queue_mode:
            pending_frames = self._queue_pending_frames()
            waiting_tasks = self._summary_queue_waiting()
            if pending_frames or waiting_tasks:
                reply = QMessageBox.question(
                    self,
                    "仍有未完成的任务",
                    f"还有 {pending_frames} 帧待识别、{waiting_tasks} 个总结任务未完成。\n"
                    "现在退出会丢弃这些内容，确定退出吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
        self._queue_timer.stop()
        self.toasts.clear()
        # 退出时立即结束识别线程（含仍在排空的），不再等待队列
        had_worker = self.ocr_worker is not None
        workers = list(self._draining_workers)
        self._draining_workers.clear()
        if isinstance(self.ocr_worker, QueuedOCRWorker):
            workers.append(self.ocr_worker)
            self.ocr_worker = None
        for worker in workers:
            try:
                worker.stop(drain=False)
            except Exception:
                pass
        self._draining = False
        if had_worker:
            # 收尾：结束会话、更新时长、恢复按钮状态（旧模式的 worker 也在此停止）
            self._stop_ocr()
        shutdown_paddle_workers()
        event.accept()


def _global_exception_hook(exc_type, exc_value, tb):
    import traceback
    err_msg = "".join(traceback.format_exception(exc_type, exc_value, tb))
    try:
        QMessageBox.critical(None, "程序出错", f"发生未处理的异常：\n{exc_value}\n\n详情请查看控制台。")
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, tb)


def main():
    sys.excepthook = _global_exception_hook
    app = QApplication(sys.argv)
    try:
        config_path = os.path.join(APP_DIR, "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                mode = json.load(f).get("theme", "light")
        else:
            mode = "light"
    except Exception:
        mode = "light"
    apply_theme(app, mode)
    window = MainWindow()
    _apply_app_icon(app, window)
    window.resize(1000, 720)
    window.setMinimumSize(900, 620)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
