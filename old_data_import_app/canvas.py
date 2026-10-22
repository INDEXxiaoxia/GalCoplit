"""剧情树画布：位置算法沿用 2.0 主程序，保证导出去的树能按同样的方式画出来。"""

import os
import sys
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QFrame, QGraphicsPolygonItem, QGraphicsRectItem, QGraphicsScene, QGraphicsView

import theme
from recorder import StoryTreeError
from story_tree_page import CARD_H, CARD_W, _layout_graph

MIME = "application/x-galcoplit-summary"

ROLE_COLOR = {
    "common": "#7AA2F7",
    "append": "#E0AF68",
    "variant": "#BB9AF7",
    "route": "#7DCFFF",
}
ROLE_NAME = {"common": "共通", "append": "追加", "variant": "差分", "route": "线路"}
BRANCH_COLOR = "#F7768E"


class _Card(QGraphicsRectItem):
    def __init__(self, node: dict, selected: bool, is_cursor: bool):
        super().__init__(0, 0, CARD_W, CARD_H)
        self.node = node
        self.selected = selected
        self.is_cursor = is_cursor
        color = QColor(BRANCH_COLOR if node.get("kind") == "branch" else ROLE_COLOR.get(node.get("role"), ROLE_COLOR["common"]))
        self.setBrush(QBrush(QColor(theme.col("surface"))))
        pen = QPen(color, 2.4 if selected else 1.4)
        self.setPen(pen)
        self.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable, False)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        super().paint(painter, option, widget)
        color = BRANCH_COLOR if self.node.get("kind") == "branch" else ROLE_COLOR.get(self.node.get("role"), ROLE_COLOR["common"])
        painter.setPen(QColor(color))
        font = QFont()
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        label = "分支" if self.node.get("kind") == "branch" else ROLE_NAME.get(self.node.get("role"), "剧情")
        painter.drawText(QRectF(10, 6, CARD_W - 20, 16), Qt.AlignmentFlag.AlignLeft, label)
        painter.setPen(QColor(theme.col("text")))
        font.setPointSize(10)
        painter.setFont(font)
        title = "分支" if self.node.get("kind") == "branch" else ((self.node.get("title") or "").strip() or "未命名")
        painter.drawText(QRectF(10, 26, CARD_W - 20, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title[:16])
        if self.is_cursor:
            painter.setBrush(QBrush(QColor(theme.col("accent"))))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(CARD_W - 18, 6, 10, 10))


class _Diamond(QGraphicsPolygonItem):
    def __init__(self, selected: bool):
        diamond = QPolygonF([
            QPointF(CARD_W / 2, 10),
            QPointF(CARD_W - 36, CARD_H / 2),
            QPointF(CARD_W / 2, CARD_H - 10),
            QPointF(36, CARD_H / 2),
        ])
        super().__init__(diamond)
        self.selected = selected
        self.setBrush(QBrush(QColor(theme.col("surface"))))
        self.setPen(QPen(QColor(BRANCH_COLOR), 2.4 if selected else 1.4))

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        super().paint(painter, option, widget)
        painter.setPen(QColor(BRANCH_COLOR))
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, CARD_W, CARD_H), Qt.AlignmentFlag.AlignCenter, "分支")


class TreeCanvas(QGraphicsView):
    node_clicked = pyqtSignal(int)
    summary_dropped = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QBrush(QColor(theme.col("bg"))))
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._panning = False
        self._message = "从左侧把旧章节拖到这里，成为主链上的第一段。"

    def set_message(self, text: str):
        self._message = text
        self.set_graph({"nodes": [], "edges": [], "cursor": None}, None)

    def set_graph(self, graph: dict, selected_id: Optional[int]):
        horizontal = self.horizontalScrollBar().value()
        vertical = self.verticalScrollBar().value()
        self.setBackgroundBrush(QBrush(QColor(theme.col("bg"))))
        scene = self.scene()
        scene.clear()
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        if not nodes:
            text = scene.addText(self._message)
            text.setDefaultTextColor(QColor(theme.col("text_dim")))
            text.setTextWidth(420)
            scene.setSceneRect(-20, -20, 480, 120)
            self._repaint_soon()
            return
        try:
            positions, _path_ids, cursor_id = _layout_graph(graph)
        except StoryTreeError as exc:
            text = scene.addText(str(exc))
            text.setDefaultTextColor(QColor(theme.col("text_dim")))
            text.setTextWidth(420)
            self._repaint_soon()
            return
        node_map = {node["id"]: node for node in nodes}
        pen = QPen(QColor(theme.col("text_faint")))
        pen.setWidthF(1.4)
        for edge in edges:
            start = positions.get(edge["from_node_id"])
            end = positions.get(edge.get("to_node_id"))
            if not start or not end:
                continue
            path = scene.addLine(start[0] + CARD_W / 2, start[1] + CARD_H, end[0] + CARD_W / 2, end[1], pen)
            path.setZValue(-1)
        for node_id, (x, y) in positions.items():
            node = node_map.get(node_id)
            if not node:
                continue
            selected = node_id == selected_id
            if node.get("kind") == "branch":
                item = _Diamond(selected)
            else:
                item = _Card(node, selected, node_id == cursor_id)
            item.setPos(x, y)
            item.setData(0, node_id)
            scene.addItem(item)
        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-48, -48, 80, 80))
        self.horizontalScrollBar().setValue(horizontal)
        self.verticalScrollBar().setValue(vertical)
        self._repaint_soon()

    def _repaint_soon(self):
        self.viewport().update()
        QTimer.singleShot(0, self.viewport().update)

    def begin_pan(self):
        self._panning = True
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)

    def end_pan(self):
        self._panning = False
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.viewport().unsetCursor()

    def mousePressEvent(self, event):
        if not self._panning and event.button() == Qt.MouseButton.LeftButton:
            node_id = self._node_at(event.position().toPoint())
            if node_id is not None:
                self.node_clicked.emit(node_id)
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self.begin_pan()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self.end_pan()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.12 if event.angleDelta().y() > 0 else 1 / 1.12
            self.scale(factor, factor)
            return
        super().wheelEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasFormat(MIME):
            event.ignore()
            return
        raw = bytes(event.mimeData().data(MIME)).decode("utf-8")
        try:
            summary_id = int(raw)
        except ValueError:
            event.ignore()
            return
        node_id = self._node_at(event.position().toPoint()) or 0
        self.summary_dropped.emit(node_id, summary_id)
        event.acceptProposedAction()

    def _node_at(self, view_pos) -> Optional[int]:
        item = self.scene().itemAt(self.mapToScene(view_pos), self.transform())
        while item is not None and item.data(0) is None:
            item = item.parentItem()
        if item is None or item.data(0) is None:
            return None
        return int(item.data(0))
