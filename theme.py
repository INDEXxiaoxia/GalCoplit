"""全局主题：支持深色（暮夜）/ 浅色（晨雾）两套配色，运行时可切换"""
from string import Template

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import QApplication, QGraphicsDropShadowEffect

# ---------------------------------------------------------------------------
# 两套调色板
# ---------------------------------------------------------------------------
DARK = {
    "bg": "#1C1F2A",
    "titlebar": "#171922",
    "header": "#20232F",
    "surface": "#242837",
    "surface2": "#2B3042",
    "input_bg": "#191C26",
    "live_bg": "#161923",
    "popup": "#262A3A",
    "border": "#333950",
    "win_border": "#3B425C",
    "text": "#E9EBF5",
    "text_dim": "#A0A7BD",
    "text_faint": "#7A8199",
    "btn_bg": "#2C3145",
    "btn_hover": "#353B54",
    "btn_press": "#262B3D",
    "btn_border": "#3D4460",
    "btn_text": "#D9DDEC",
    "accent": "#7AA2F7",
    "accent_text": "#131622",
    "accent_soft": "rgba(122, 162, 247, 0.16)",
    "g1": "#7AA2F7", "g2": "#BB9AF7",
    "g1_hover": "#8FB3FF", "g2_hover": "#CBAEFF",
    "g1_press": "#6890E5", "g2_press": "#A988E5",
    "primary_dis_bg": "#2A3350",
    "primary_dis_text": "#5A6690",
    "chip_bg": "#2A3147",
    "chip_border": "#3E4866",
    "chip_text": "#A9BDF5",
    "danger": "#F7768E",
    "danger_soft": "rgba(247, 118, 142, 0.14)",
    "danger_border": "#5C3A4A",
    "warn": "#E0AF68",
    "ok": "#9ECE6A",
    "dot_off": "#454C63",
    "dis_bg": "#232734",
    "dis_text": "#5A6078",
    "dis_border": "#2D3242",
    "scroll": "#3A4157",
    "scroll_hover": "#4A5473",
    "sel_text": "#131622",
    "avatar_text": "#FFFFFF",
}

LIGHT = {
    "bg": "#F2F4F9",
    "titlebar": "#FAFBFE",
    "header": "#F7F8FC",
    "surface": "#FFFFFF",
    "surface2": "#F5F6FB",
    "input_bg": "#F4F5FA",
    "live_bg": "#F4F6FC",
    "popup": "#FFFFFF",
    "border": "#E3E6F0",
    "win_border": "#D7DBE8",
    "text": "#252A3D",
    "text_dim": "#646B82",
    "text_faint": "#9298AC",
    "btn_bg": "#EEF0F7",
    "btn_hover": "#E4E7F2",
    "btn_press": "#DCDFEC",
    "btn_border": "#D9DDEA",
    "btn_text": "#3A4056",
    "accent": "#5B6EF5",
    "accent_text": "#FFFFFF",
    "accent_soft": "rgba(91, 110, 245, 0.12)",
    "g1": "#5B6EF5", "g2": "#8B5CF6",
    "g1_hover": "#6F80FF", "g2_hover": "#9D73FF",
    "g1_press": "#4C5DDA", "g2_press": "#7A4BE0",
    "primary_dis_bg": "#C9D0F5",
    "primary_dis_text": "#8E97D8",
    "chip_bg": "#EDF0FE",
    "chip_border": "#D5DCFA",
    "chip_text": "#4A5BD8",
    "danger": "#E5484D",
    "danger_soft": "rgba(229, 72, 77, 0.10)",
    "danger_border": "#F3C6C8",
    "warn": "#B45309",
    "ok": "#15803D",
    "dot_off": "#C6CBDB",
    "dis_bg": "#EFF1F6",
    "dis_text": "#A9AEC0",
    "dis_border": "#E5E8F0",
    "scroll": "#CDD2E0",
    "scroll_hover": "#B8BDD2",
    "sel_text": "#FFFFFF",
    "avatar_text": "#FFFFFF",
}

_SHADOWS = {
    "dark": QColor(0, 0, 0, 170),
    "light": QColor(45, 55, 90, 55),
}

MODE = "dark"
CURRENT = dict(DARK)


def col(key: str) -> str:
    return CURRENT[key]


def shadow_color() -> QColor:
    return _SHADOWS.get(MODE, _SHADOWS["dark"])


def make_shadow(widget, blur: int = 36, dy: int = 8) -> QGraphicsDropShadowEffect:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    effect.setColor(shadow_color())
    widget.setGraphicsEffect(effect)
    return effect


def polish_combo_popup(combo) -> None:
    """修复下拉弹层的黑边：弹层窗口设为无边框半透明，让 QSS 圆角生效。

    全局 QSS 中 QWidget 背景为 transparent，弹层是独立顶层窗口，
    不设置 WA_TranslucentBackground 时透明区域会被渲染成黑色。
    """
    popup = combo.view().window()
    popup.setWindowFlags(
        Qt.WindowType.Popup
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.NoDropShadowWindowHint
    )
    popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


# ---------------------------------------------------------------------------
# QSS 模板
# ---------------------------------------------------------------------------
_QSS_TMPL = Template("""
* { outline: none; }

QWidget {
    color: $text;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
    background: transparent;
}

QMainWindow { background: transparent; }
QDialog { background-color: $bg; }
QDialog#GlassDialog { background: transparent; }
QMessageBox, QInputDialog { background-color: $surface; }

/* ---------- 窗口容器（带边框圆角，配合投影） ---------- */
#RootFrame { background-color: $bg; border: 1px solid $win_border; border-radius: 14px; }
#DialogFrame { background-color: $bg; border: 1px solid $win_border; border-radius: 14px; }
#FloatFrame { background-color: $surface; border: 1px solid $win_border; border-radius: 0px; }

/* ---------- 消息提示（Toast） ---------- */
#ToastFrame { background-color: $popup; border: 1px solid $win_border; border-radius: 10px; }
QLabel[cls="toastText"] { color: $text; font-size: 12px; font-weight: 600; }
QLabel[cls="toastSub"] { color: $text_dim; font-size: 11px; }
QPushButton#ToastClose {
    background: transparent; border: none; border-radius: 4px;
    color: $text_faint; font-size: 13px; padding: 0;
    min-width: 18px; max-width: 18px; min-height: 18px; max-height: 18px;
}
QPushButton#ToastClose:hover { background: $btn_hover; color: $text; }

/* ---------- 进度条 ---------- */
QProgressBar {
    background-color: $input_bg; border: 1px solid $border;
    border-radius: 3px; text-align: center;
}
QProgressBar::chunk { background-color: $accent; border-radius: 2px; }

/* ---------- 标题栏 ---------- */
#TitleBar {
    background-color: $titlebar;
    border-bottom: 1px solid $border;
    border-top-left-radius: 13px; border-top-right-radius: 13px;
}
#TitleText { font-size: 13px; font-weight: 700; letter-spacing: 3px; color: $text; }
#TitleMark { color: $accent; font-size: 13px; }

QPushButton#WinBtn, QPushButton#WinBtnClose {
    background: transparent; border: none; border-radius: 6px;
    min-width: 34px; max-width: 34px; min-height: 28px; max-height: 28px;
    color: $text_faint; font-size: 12px; padding: 0;
}
QPushButton#WinBtn:hover { background: $btn_hover; color: $text; }
QPushButton#WinBtnClose:hover { background: #E5484D; color: #FFFFFF; }

/* ---------- 侧边栏 ---------- */
#Sidebar {
    background-color: $titlebar;
    border-right: 1px solid $border;
    border-bottom-left-radius: 13px;
}
QPushButton#NavBtn {
    background: transparent; border: 1px solid transparent; border-radius: 8px;
    padding: 0;
}
QPushButton#NavBtn:hover { background: $btn_bg; }
QPushButton#NavBtn:checked { background: $accent_soft; }
QLabel#NavGlyph { color: $text_faint; font-size: 14px; }
QLabel#NavText { color: $text_dim; font-weight: 600; font-size: 13px; }
QLabel#NavGlyph[active="true"] { color: $accent; }
QLabel#NavText[active="true"] { color: $accent; }

/* ---------- 对话框头部 ---------- */
#DialogHeader {
    background-color: $header;
    border-bottom: 1px solid $border;
    border-top-left-radius: 13px; border-top-right-radius: 13px;
}
#DialogTitle { font-size: 14px; font-weight: 700; letter-spacing: 1px; }

/* ---------- 卡片与文字层级 ---------- */
QFrame[card="true"] {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 12px;
}
QLabel[cls="caption"] { color: $text_faint; font-size: 10px; font-weight: 700; letter-spacing: 2px; }
QLabel[cls="h1"] { font-size: 19px; font-weight: 700; }
QLabel[cls="statValue"] { font-size: 17px; font-weight: 700; }
QLabel[cls="statAccent"] { font-size: 15px; font-weight: 700; color: $accent; }
QLabel[cls="dim"] { color: $text_dim; }
QLabel[cls="chip"] {
    background-color: $chip_bg; color: $chip_text;
    border: 1px solid $chip_border; border-radius: 10px;
    padding: 3px 10px; font-size: 11px;
}
QLabel[cls="floatTitle"] { font-weight: 700; font-size: 13px; }
QLabel[cls="floatSub"] { color: $text_dim; font-size: 12px; }
QLabel[cls="floatMeta"] { color: $text_faint; font-size: 10px; }
QLabel[cls="cardName"] { font-size: 14px; font-weight: 700; }
QLabel[cls="tlTitle"] { font-size: 13px; font-weight: 700; color: $text; }
QLabel[cls="tlMeta"] { font-size: 11px; color: $text_faint; }

/* 剧情总结右侧：段落标题（平时像标题，编辑时出现底部强调线） */
QLineEdit#DetailTitle {
    background: transparent;
    border: none;
    border-radius: 0;
    padding: 0 2px 4px 2px;
    font-size: 17px;
    font-weight: 700;
    color: $text;
}
QLineEdit#DetailTitle:!read-only {
    border-bottom: 2px solid $accent;
    background: transparent;
}

/* 阅读区：剧情总结正文 */
QTextEdit#ReadingArea {
    font-size: 15px;
    padding: 14px;
    letter-spacing: 0.4px;
}
/* ---------- 角色卡片 ---------- */
QFrame[charCard="true"] {
    background-color: $surface2;
    border: 1px solid $border;
    border-radius: 12px;
}
QFrame[charCard="true"]:hover { border-color: $accent; }
QFrame[cls="cardDivider"] {
    border: none;
    background-color: $border;
    max-height: 1px;
}

/* ---------- 按钮 ---------- */
QPushButton {
    background-color: $btn_bg;
    color: $btn_text;
    border: 1px solid $btn_border;
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton:hover { background-color: $btn_hover; border-color: $accent; }
QPushButton:pressed { background-color: $btn_press; }
QPushButton:disabled { background-color: $dis_bg; color: $dis_text; border-color: $dis_border; }
QPushButton:checked {
    background-color: $accent_soft;
    border-color: $accent;
    color: $accent;
}

QPushButton[variant="primary"] {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 $g1, stop:1 $g2);
    color: $accent_text; border: none; font-weight: 700;
}
QPushButton[variant="primary"]:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 $g1_hover, stop:1 $g2_hover);
}
QPushButton[variant="primary"]:pressed {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 $g1_press, stop:1 $g2_press);
}
QPushButton[variant="primary"]:disabled { background-color: $primary_dis_bg; color: $primary_dis_text; }

QPushButton[variant="danger"] {
    background-color: $danger_soft; color: $danger; border: 1px solid $danger_border;
}
QPushButton[variant="danger"]:hover { background-color: $danger_soft; border-color: $danger; }
QPushButton[variant="danger"]:disabled { background-color: $dis_bg; color: $dis_text; border-color: $dis_border; }

QPushButton[variant="ghost"] { background-color: transparent; border: 1px solid transparent; color: $text_dim; }
QPushButton[variant="ghost"]:hover { background-color: $btn_bg; color: $text; }

QPushButton#PillBtn { padding: 3px 12px; border-radius: 6px; font-size: 12px; }

/* ---------- 输入控件 ---------- */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: $input_bg;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 6px 10px;
    selection-background-color: $accent;
    selection-color: $sel_text;
}
QLineEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: $accent; }
QTextEdit { padding: 8px; }
QLineEdit:read-only { color: $text_dim; }

QLineEdit#LiveLine {
    border: 1px solid $border;
    border-left: 3px solid $accent;
    background-color: $live_bg;
    font-size: 14px;
    padding: 9px 12px;
    color: $text;
}

QComboBox::drop-down { border: none; width: 26px; }
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $text_faint;
    margin-right: 8px;
}
/* 可编辑下拉：内部输入框去掉边框与自身 padding，避免与 Combo 的 padding 叠加裁切文字 */
QComboBox QLineEdit {
    border: none;
    background: transparent;
    padding: 0px;
    margin: 0px;
    min-height: 18px;
    selection-background-color: $accent;
    selection-color: $sel_text;
}

/* 下拉弹层：窗口本身由 polish_combo_popup 设为半透明，圆角由视图绘制 */
QComboBoxPrivateContainer {
    background: transparent;
    border: none;
}
QComboBox QAbstractItemView {
    background-color: $popup;
    border: 1px solid $win_border;
    border-radius: 10px;
    padding: 4px;
    selection-background-color: $accent_soft;
    selection-color: $text;
}

/* 通用列表视图（含可编辑下拉框的补全弹层） */
QListView {
    background-color: $popup;
    color: $text;
    border: 1px solid $win_border;
    padding: 4px;
}
QListView::item { padding: 6px 8px; border-radius: 4px; }
QListView::item:hover { background-color: $btn_bg; }
QListView::item:selected { background-color: $accent_soft; color: $text; }

QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    background: $btn_bg; border: none; width: 18px;
}
QSpinBox::up-button, QDoubleSpinBox::up-button { border-top-right-radius: 8px; }
QSpinBox::down-button, QDoubleSpinBox::down-button { border-bottom-right-radius: 8px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover { background: $btn_hover; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: none;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 4px solid $text_faint;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: none;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 4px solid $text_faint;
}

QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 18px; height: 18px; border-radius: 5px;
    border: 1px solid $btn_border; background: $input_bg;
}
QCheckBox::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked { background-color: $accent; border-color: $accent; }

/* ---------- 列表 / 标签页 / 分割条 / 滚动区 ---------- */
QListWidget {
    background-color: $input_bg;
    border: 1px solid $border;
    border-radius: 10px;
    padding: 6px;
}
QListWidget::item { padding: 9px 10px; border-radius: 6px; color: $text_dim; margin: 1px 0; }
QListWidget::item:hover { background-color: $btn_bg; color: $text; }
QListWidget::item:selected { background-color: $accent_soft; color: $text; }

QTabWidget::pane { border: none; }
QTabBar { background: transparent; }
QTabBar::tab {
    background: transparent; color: $text_faint;
    padding: 8px 16px; margin-right: 6px;
    border-bottom: 2px solid transparent; font-weight: 600;
}
QTabBar::tab:hover { color: $text_dim; }
QTabBar::tab:selected { color: $accent; border-bottom: 2px solid $accent; }

QSplitter::handle { background-color: $border; }
QSplitter::handle:horizontal { width: 2px; }
QSplitter::handle:vertical { height: 2px; }

QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }

/* ---------- 滚动条 ---------- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scroll; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: $scroll_hover; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: $scroll; border-radius: 4px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: $scroll_hover; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ---------- 其他 ---------- */
QToolTip {
    background-color: $popup; color: $text;
    border: 1px solid $border; padding: 6px 8px; border-radius: 6px;
}
QMessageBox QPushButton, QInputDialog QPushButton { min-width: 72px; }
QSizeGrip { background: transparent; width: 14px; height: 14px; }
""")


def apply_theme(app: QApplication, mode: str = "dark"):
    global MODE, CURRENT
    MODE = mode if mode in ("dark", "light") else "dark"
    CURRENT = dict(DARK if MODE == "dark" else LIGHT)

    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(CURRENT["bg"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(CURRENT["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(CURRENT["input_bg"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(CURRENT["surface"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(CURRENT["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(CURRENT["btn_bg"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(CURRENT["btn_text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(CURRENT["accent"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(CURRENT["sel_text"]))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(CURRENT["popup"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(CURRENT["text"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(CURRENT["text_faint"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(CURRENT["dis_text"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(CURRENT["dis_text"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(CURRENT["dis_text"]))
    app.setPalette(palette)

    font = QFont("Microsoft YaHei UI")
    font.setPointSize(9)
    app.setFont(font)

    app.setStyleSheet(_QSS_TMPL.substitute(CURRENT))


def status_visual(text: str) -> tuple:
    """根据状态文本返回 (指示灯颜色, 是否呼吸闪烁)"""
    if "总结中" in text:
        return CURRENT["warn"], True
    if "识别中" in text or "测试中" in text or "排空" in text:
        return CURRENT["accent"], True
    if "暂停" in text:
        return CURRENT["danger"], False
    if "就绪" in text or "已选择" in text:
        return CURRENT["ok"], False
    return CURRENT["text_faint"], False
