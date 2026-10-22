"""剧情树：主链、追加、差分、分支，以及总结时点。"""

from typing import Dict, List, Optional

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPalette, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
)

import theme
from recorder import Database, StoryTreeError


CARD_W = 176
CARD_H = 76
GAP_X = 64
GAP_Y = 56
GAP_CHAIN = 10
SIDE_BUS_LIFT = 20
SIDE_BUS_CLEARANCE = SIDE_BUS_LIFT + 12

ROLE_COLOR = {
    "common": "#7AA2F7",
    "append": "#E0AF68",
    "variant": "#BB9AF7",
    "route": "#7DCFFF",
}
LINE_COLOR = {
    "common": "#5B6EF5",
    "append": "#C48A2A",
    "variant": "#8D62D6",
    "route": "#2B8CB8",
}
BRANCH_COLOR = "#F7768E"


def _caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("cls", "caption")
    return label


def _variant(button: QPushButton, name: str):
    button.setProperty("variant", name)


class SplitBeatDialog(QDialog):
    """左右两栏手动剪开一段总结。"""

    def __init__(self, original: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("拆分剧情")
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        hint = QLabel("左边留下前半，右边写下后半。程序不会自动切句。")
        hint.setWordWrap(True)
        hint.setProperty("cls", "dim")
        layout.addWidget(hint)
        columns = QHBoxLayout()
        self.left = QTextEdit()
        self.left.setPlainText(original or "")
        self.right = QTextEdit()
        self.right.setPlaceholderText("后半段剧情")
        columns.addWidget(self.left, 1)
        columns.addWidget(self.right, 1)
        layout.addLayout(columns, 1)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("拆开")
        _variant(ok, "primary")
        ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    def halves(self):
        return self.left.toPlainText(), self.right.toPlainText()


class _NodeItem(QGraphicsRectItem):
    def __init__(self, node: dict, selected: bool, on_path: bool, is_cursor: bool, continued: bool = False):
        super().__init__(0, 0, CARD_W, CARD_H)
        self.node = node
        self.selected = selected
        self.on_path = on_path
        self.is_cursor = is_cursor
        self.continued = continued
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setAcceptHoverEvents(False)

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        role = self.node.get("role") or "common"
        border = QColor(ROLE_COLOR.get(role, ROLE_COLOR["common"]))
        if self.selected:
            border = QColor(theme.col("accent"))
        fill = QColor(theme.col("surface2" if self.selected or self.on_path else "surface"))
        pen = QPen(border, 2.4 if self.selected or self.on_path else 1.4)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)

        painter.setPen(QColor(border))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        role_name = {"common": "共通", "append": "追加", "variant": "差分", "route": "线路"}.get(role, "剧情")
        if self.continued:
            role_name = f"{role_name} · 续"
        painter.drawText(QRectF(10, 6, CARD_W - 36, 16), Qt.AlignmentFlag.AlignLeft, role_name)

        painter.setPen(QColor(theme.col("text")))
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        title = (self.node.get("title") or "").strip() or "未命名"
        painter.drawText(QRectF(10, 24, CARD_W - 20, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)

        painter.setPen(QColor(theme.col("text_dim")))
        font.setBold(False)
        font.setPointSize(8)
        painter.setFont(font)
        condition = (self.node.get("condition_label") or "").strip()
        if condition and self.node.get("role") in ("append", "variant"):
            painter.drawText(QRectF(10, 48, CARD_W - 20, 18), Qt.AlignmentFlag.AlignLeft, condition[:18])

        if self.is_cursor:
            painter.setBrush(QBrush(QColor(theme.col("accent"))))
            painter.setPen(QPen(QColor("#ffffff"), 1.2))
            painter.drawEllipse(QRectF(CARD_W - 22, 4, 14, 14))


class _BranchItem(QGraphicsPolygonItem):
    def __init__(self, node: dict, selected: bool, on_path: bool, is_cursor: bool):
        diamond = QPolygonF([
            QPointF(CARD_W / 2, 8),
            QPointF(CARD_W - 28, CARD_H / 2),
            QPointF(CARD_W / 2, CARD_H - 8),
            QPointF(28, CARD_H / 2),
        ])
        super().__init__(diamond)
        self.node = node
        self.selected = selected
        self.on_path = on_path
        self.is_cursor = is_cursor

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        border = QColor(theme.col("accent") if self.selected else BRANCH_COLOR)
        fill = QColor(theme.col("surface2" if self.selected or self.on_path else "surface"))
        painter.setPen(QPen(border, 2.4 if self.selected or self.on_path else 1.4))
        painter.setBrush(QBrush(fill))
        painter.drawPolygon(self.polygon())
        painter.setPen(QColor(theme.col("text")))
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(36, 28, CARD_W - 72, 22), Qt.AlignmentFlag.AlignCenter, "分支")
        if self.is_cursor:
            painter.setBrush(QBrush(QColor(theme.col("accent"))))
            painter.setPen(QPen(QColor("#ffffff"), 1.2))
            painter.drawEllipse(QRectF(CARD_W - 20, CARD_H / 2 - 7, 14, 14))


class StoryCanvas(QGraphicsView):
    node_clicked = pyqtSignal(int)
    node_menu = pyqtSignal(int, object)
    node_double_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setBackgroundBrush(QBrush(QColor(theme.col("bg"))))
        self._press_pos = None
        self._panning = False

    def begin_pan(self):
        self._panning = True
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)

    def end_pan(self):
        self._panning = False
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.viewport().unsetCursor()

    def set_graph(self, graph: dict, selected_id: Optional[int]):
        horizontal = self.horizontalScrollBar().value()
        vertical = self.verticalScrollBar().value()
        self.setBackgroundBrush(QBrush(QColor(theme.col("bg"))))
        scene = self.scene()
        scene.clear()
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        if not nodes:
            text = scene.addText("这棵树还没有剧情。开始总结后，第一段会成为主链起点。")
            text.setDefaultTextColor(QColor(theme.col("text_dim")))
            scene.setSceneRect(-20, -20, 460, 80)
            self.horizontalScrollBar().setValue(horizontal)
            self.verticalScrollBar().setValue(vertical)
            self._repaint_soon()
            return

        node_map = {node["id"]: node for node in nodes}
        try:
            positions, path_ids, cursor_id = _layout_graph(graph)
        except StoryTreeError as exc:
            text = scene.addText(str(exc))
            text.setDefaultTextColor(QColor(theme.col("text_dim")))
            self.horizontalScrollBar().setValue(horizontal)
            self.verticalScrollBar().setValue(vertical)
            self._repaint_soon()
            return

        continued = _continued_ids(nodes, edges)
        _draw_edges(scene, edges, positions, node_map, path_ids)

        for node_id, (x, y) in positions.items():
            node = node_map[node_id]
            selected = node_id == selected_id
            on_path = node_id in path_ids
            is_cursor = node_id == cursor_id
            if node["kind"] == "branch":
                item = _BranchItem(node, selected, on_path, is_cursor)
            else:
                item = _NodeItem(node, selected, on_path, is_cursor, node_id in continued)
            item.setPos(x, y)
            item.setData(0, node_id)
            scene.addItem(item)

        rect = scene.itemsBoundingRect().adjusted(-40, -40, 80, 80)
        scene.setSceneRect(rect)
        self.horizontalScrollBar().setValue(horizontal)
        self.verticalScrollBar().setValue(vertical)
        self._repaint_soon()

    def _repaint_soon(self):
        self.viewport().update()
        QTimer.singleShot(0, self.viewport().update)

    def mousePressEvent(self, event):
        self._press_pos = event.position()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self._press_pos is None:
            return
        moved = (event.position() - self._press_pos).manhattanLength()
        self._press_pos = None
        if self._panning or moved > 6 or event.button() != Qt.MouseButton.LeftButton:
            return
        scene_pos = self.mapToScene(event.position().toPoint())
        node_id = self._node_at_scene(scene_pos)
        if node_id is not None:
            self.node_clicked.emit(node_id)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            node_id = self._node_at_scene(self.mapToScene(event.position().toPoint()))
            if node_id is not None:
                self.node_double_clicked.emit(node_id)
                return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):
        node_id = self._node_at_scene(self.mapToScene(event.pos()))
        if node_id is None:
            return
        if hasattr(event, "globalPos"):
            global_pos = event.globalPos()
        else:
            global_pos = event.globalPosition().toPoint()
        self.node_menu.emit(node_id, global_pos)

    def _node_at_scene(self, scene_pos) -> Optional[int]:
        item = self.scene().itemAt(scene_pos, self.transform())
        while item is not None and item.data(0) is None:
            item = item.parentItem()
        if item is None or item.data(0) is None:
            return None
        return int(item.data(0))

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.12 if event.angleDelta().y() > 0 else 1 / 1.12
            self.scale(factor, factor)
            return
        super().wheelEvent(event)


def _layout_graph(graph: dict):
    """主链从上到下。差分在当前轴左边，追加在右边。分支在当前线路下方再展开，子分支同理。"""
    nodes = graph["nodes"]
    edges = graph["edges"]
    node_map = {node["id"]: node for node in nodes}
    pointed = {edge["to_node_id"] for edge in edges if edge.get("to_node_id")}
    roots = [node["id"] for node in nodes if node["id"] not in pointed]
    if len(roots) != 1:
        raise StoryTreeError("剧情树结构不完整，暂时无法绘制")
    positions: Dict[int, tuple] = {}
    node_id = roots[0]
    x = 0
    y = 0
    seen = set()
    while node_id and node_id not in seen:
        seen.add(node_id)
        node = node_map[node_id]
        if node["kind"] == "branch":
            _place_branch(node_id, x, y, edges, node_map, positions)
            break
        positions[node_id] = (x, y)
        depth = _place_sides(node_id, x, y, "spine", edges, node_map, positions)
        nxt = _first_target(edges, node_id, "spine")
        y += _row_step(depth, tight=False, gap=_gap_before(nxt, "spine", edges, node_map, tight=False))
        node_id = nxt

    cursor = graph.get("cursor") or {}
    cursor_id = cursor.get("after_node_id")
    path_ids = set(_path_ids(cursor_id, edges)) if cursor_id else set()
    return positions, path_ids, cursor_id


def _row_step(depth: int, tight: bool, gap: Optional[float] = None) -> float:
    if gap is None:
        gap = GAP_CHAIN if tight else GAP_Y
    if depth <= 1:
        return CARD_H + gap
    span = depth * CARD_H + (depth - 1) * GAP_CHAIN
    return span + gap


def _gap_before(node_id, chain_kind, edges, node_map, tight: bool) -> float:
    """下一块自己的追加或差分要从上方绕线时，给这段空隙留出总线的高度。"""
    base = GAP_CHAIN if tight else GAP_Y
    if not node_id:
        return base
    variants, appends = _side_groups(
        node_id, edges, node_map, _side_skip(node_id, chain_kind, edges)
    )
    if len(variants) >= 2 or len(appends) >= 2:
        return max(base, SIDE_BUS_CLEARANCE)
    return base


def _place_branch(branch_id, x, y, edges, node_map, positions):
    if branch_id in positions:
        return
    positions[branch_id] = (x, y)
    lanes = _direct_targets(edges, branch_id, "branch")
    if not lanes:
        return
    metrics = [_column_metrics(lane, "branch", edges, node_map) for lane in lanes]
    total = sum(item[0] for item in metrics) + GAP_X * (len(metrics) - 1)
    origin = x + CARD_W / 2 - total / 2
    lane_y = y + CARD_H + GAP_Y
    cursor = origin
    for lane, (width, axis) in zip(lanes, metrics):
        _place_chain(lane, cursor + axis, lane_y, "branch", edges, node_map, positions)
        cursor += width + GAP_X


def _place_chain(start_id, x, y, kind, edges, node_map, positions):
    node_id = start_id
    seen = set()
    while node_id and node_id not in seen and node_id not in positions:
        seen.add(node_id)
        node = node_map[node_id]
        if node["kind"] == "branch":
            _place_branch(node_id, x, y, edges, node_map, positions)
            return
        positions[node_id] = (x, y)
        depth = _place_sides(node_id, x, y, kind, edges, node_map, positions)
        nxt = _first_target(edges, node_id, kind)
        if not nxt:
            return
        nxt_node = node_map.get(nxt)
        if nxt_node and nxt_node["kind"] == "branch":
            _place_branch(nxt, x, y + _row_step(depth, tight=False), edges, node_map, positions)
            return
        y += _row_step(depth, tight=True, gap=_gap_before(nxt, kind, edges, node_map, tight=True))
        node_id = nxt


def _place_sides(anchor_id, x, y, chain_kind, edges, node_map, positions) -> int:
    variants, appends = _side_groups(anchor_id, edges, node_map, _side_skip(anchor_id, chain_kind, edges))
    depth = 1
    cursor = x
    for start_id in variants:
        width, axis = _column_metrics(start_id, "insert", edges, node_map)
        cursor -= GAP_X + width
        _place_chain(start_id, cursor + axis, y, "insert", edges, node_map, positions)
        depth = max(depth, _chain_rows(start_id, "insert", edges, node_map))
    cursor = x + CARD_W
    for start_id in appends:
        width, axis = _column_metrics(start_id, "insert", edges, node_map)
        _place_chain(start_id, cursor + GAP_X + axis, y, "insert", edges, node_map, positions)
        cursor += GAP_X + width
        depth = max(depth, _chain_rows(start_id, "insert", edges, node_map))
    return depth


def _side_skip(anchor_id, chain_kind, edges) -> Optional[int]:
    if chain_kind != "insert":
        return None
    return _first_target(edges, anchor_id, "insert")


def _side_groups(anchor_id, edges, node_map, skip_id=None):
    variants = []
    appends = []
    for start_id in _direct_targets(edges, anchor_id, "insert"):
        if start_id == skip_id:
            continue
        role = (node_map.get(start_id) or {}).get("role")
        if role == "variant":
            variants.append(start_id)
        else:
            appends.append(start_id)
    return variants, appends


def _column_metrics(start_id, kind, edges, node_map, seen=None):
    """返回这一列占用的宽度，以及剧情卡相对槽位左边的偏移。"""
    if seen is None:
        seen = set()
    left_need = 0.0
    right_need = 0.0
    fork = float(CARD_W)
    node_id = start_id
    while node_id and node_id not in seen:
        seen.add(node_id)
        node = node_map.get(node_id)
        if not node:
            break
        if node["kind"] == "branch":
            lanes = _direct_targets(edges, node_id, "branch")
            if lanes:
                widths = [
                    _column_metrics(lane, "branch", edges, node_map, seen)[0]
                    for lane in lanes
                ]
                fork = max(fork, sum(widths) + GAP_X * (len(widths) - 1))
            break
        nxt = _first_target(edges, node_id, kind)
        variants, appends = _side_groups(node_id, edges, node_map, nxt if kind == "insert" else None)
        left = 0.0
        for start in variants:
            left += _column_metrics(start, "insert", edges, node_map, seen)[0] + GAP_X
        right = 0.0
        for start in appends:
            right += GAP_X + _column_metrics(start, "insert", edges, node_map, seen)[0]
        left_need = max(left_need, left)
        right_need = max(right_need, right)
        nxt = _first_target(edges, node_id, kind)
        nxt_node = node_map.get(nxt) if nxt else None
        if not nxt_node:
            break
        node_id = nxt
    fork_side = max(0.0, (fork - CARD_W) / 2)
    left_need = max(left_need, fork_side)
    right_need = max(right_need, fork_side)
    return left_need + CARD_W + right_need, left_need


def _chain_rows(start_id, kind, edges, node_map) -> int:
    rows = 0
    node_id = start_id
    seen = set()
    while node_id and node_id not in seen:
        seen.add(node_id)
        node = node_map.get(node_id)
        if not node or node["kind"] == "branch":
            break
        rows += 1
        nxt = _first_target(edges, node_id, kind)
        nxt_node = node_map.get(nxt) if nxt else None
        if not nxt_node or nxt_node["kind"] == "branch":
            break
        node_id = nxt
    return rows


def _direct_targets(edges, node_id, kind) -> List[int]:
    found = [
        edge for edge in edges
        if edge["from_node_id"] == node_id and edge["kind"] == kind and edge.get("to_node_id")
    ]
    found.sort(key=lambda edge: (int(edge.get("sort_order") or 0), int(edge["id"])))
    return [edge["to_node_id"] for edge in found]


def _continued_ids(nodes, edges) -> set:
    node_map = {node["id"]: node for node in nodes}
    parent = {}
    for edge in edges:
        if edge.get("to_node_id"):
            parent[edge["to_node_id"]] = edge
    continued = set()
    for node in nodes:
        edge = parent.get(node["id"])
        if not edge or edge["kind"] not in ("insert", "branch"):
            continue
        previous = node_map.get(edge["from_node_id"])
        if previous and previous.get("kind") == "beat" and previous.get("role") == node.get("role"):
            continued.add(node["id"])
    return continued


def _first_target(edges, node_id, kind) -> Optional[int]:
    found = [
        edge for edge in edges
        if edge["from_node_id"] == node_id and edge["kind"] == kind and edge.get("to_node_id")
    ]
    found.sort(key=lambda edge: (int(edge.get("sort_order") or 0), int(edge["id"])))
    return found[0]["to_node_id"] if found else None


def _path_ids(anchor_id, edges) -> List[int]:
    parent = {}
    for edge in edges:
        if edge.get("to_node_id"):
            parent[edge["to_node_id"]] = edge["from_node_id"]
    path = []
    seen = set()
    current = anchor_id
    while current and current not in seen:
        seen.add(current)
        path.append(current)
        current = parent.get(current)
    path.reverse()
    return path


def _draw_edges(scene, edges, positions, node_map, path_ids):
    forks = {}
    side_heads = {}
    continued = _continued_ids(list(node_map.values()), edges)
    for edge in edges:
        start = positions.get(edge["from_node_id"])
        end = positions.get(edge.get("to_node_id"))
        if not start or not end:
            continue
        start_node = node_map[edge["from_node_id"]]
        end_node = node_map[edge["to_node_id"]]
        if edge["kind"] == "branch" and start_node["kind"] == "branch":
            forks.setdefault(edge["from_node_id"], []).append(edge)
            continue
        if edge["kind"] == "insert" and edge["to_node_id"] not in continued:
            side = "left" if end_node.get("role") == "variant" else "right"
            side_heads.setdefault((edge["from_node_id"], side), []).append(edge)
            continue
        on_path = edge["from_node_id"] in path_ids and edge["to_node_id"] in path_ids
        _add_path(scene, _edge_path(edge["kind"], start, end, start_node["kind"], end_node["kind"]), _line_color(on_path, end_node.get("role")), on_path)

    for branch_id, branch_edges in forks.items():
        _draw_fork(scene, positions[branch_id], branch_edges, positions, path_ids, branch_id)
    for (anchor_id, side), group in side_heads.items():
        _draw_side_links(scene, positions[anchor_id], group, positions, path_ids, node_map, side == "left")


def _draw_fork(scene, origin, branch_edges, positions, path_ids, branch_id):
    tip_x = origin[0] + CARD_W / 2
    tip_y = origin[1] + CARD_H - 8
    drops = []
    for edge in sorted(branch_edges, key=lambda item: (int(item.get("sort_order") or 0), int(item["id"]))):
        end = positions.get(edge.get("to_node_id"))
        if not end:
            continue
        drops.append((end[0] + CARD_W / 2, end[1] + 2, edge["to_node_id"] in path_ids and branch_id in path_ids))
    if not drops:
        return
    if len(drops) == 1 and abs(drops[0][0] - tip_x) < 1:
        path = QPainterPath()
        path.moveTo(tip_x, tip_y)
        path.lineTo(tip_x, drops[0][1])
        _add_path(scene, path, _line_color(drops[0][2], "route"), drops[0][2])
        return
    bus_y = tip_y + 22
    stem = QPainterPath()
    stem.moveTo(tip_x, tip_y)
    stem.lineTo(tip_x, bus_y)
    stem_hot = any(item[2] for item in drops)
    _add_path(scene, stem, _line_color(stem_hot, "route"), stem_hot)
    hot_x = next((item[0] for item in drops if item[2]), None)
    xs = sorted({tip_x, *(item[0] for item in drops)})
    for left, right in zip(xs, xs[1:]):
        mid = (left + right) / 2
        hot = hot_x is not None and min(tip_x, hot_x) - 0.1 <= mid <= max(tip_x, hot_x) + 0.1
        bus = QPainterPath()
        bus.moveTo(left, bus_y)
        bus.lineTo(right, bus_y)
        _add_path(scene, bus, _line_color(hot, "route"), hot)
    for drop_x, drop_y, hot in drops:
        path = QPainterPath()
        path.moveTo(drop_x, bus_y)
        path.lineTo(drop_x, drop_y)
        _add_path(scene, path, _line_color(hot, "route"), hot)


def _draw_side_links(scene, origin, group, positions, path_ids, node_map, left: bool):
    """多条差分或追加并排时，线从卡片上方绕开，不再横穿中间那一张。"""
    heads = []
    for edge in sorted(group, key=lambda item: (int(item.get("sort_order") or 0), int(item["id"]))):
        end = positions.get(edge.get("to_node_id"))
        if not end:
            continue
        hot = edge["from_node_id"] in path_ids and edge["to_node_id"] in path_ids
        role = node_map[edge["to_node_id"]].get("role") or "common"
        heads.append((end[0] + CARD_W / 2, end[1] + 3, hot, role, end))
    if not heads:
        return
    if len(heads) == 1:
        end = heads[0][4]
        role = heads[0][3]
        _add_path(
            scene,
            _side_connector(origin, end, left),
            _line_color(heads[0][2], role),
            heads[0][2],
        )
        return
    bus_y = min(origin[1], *(item[4][1] for item in heads)) - SIDE_BUS_LIFT
    stem_x = origin[0] + (36 if left else CARD_W - 36)
    stem_hot = any(item[2] for item in heads)
    stem_role = next((item[3] for item in heads if item[2]), heads[0][3])
    stem = QPainterPath()
    stem.moveTo(stem_x, origin[1])
    stem.lineTo(stem_x, bus_y)
    _add_path(scene, stem, _line_color(stem_hot, stem_role), stem_hot)
    hot_x = next((item[0] for item in heads if item[2]), None)
    xs = sorted({stem_x, *(item[0] for item in heads)})
    for left_x, right_x in zip(xs, xs[1:]):
        mid = (left_x + right_x) / 2
        hot = hot_x is not None and min(stem_x, hot_x) - 0.1 <= mid <= max(stem_x, hot_x) + 0.1
        bus = QPainterPath()
        bus.moveTo(left_x, bus_y)
        bus.lineTo(right_x, bus_y)
        _add_path(scene, bus, _line_color(hot, stem_role), hot)
    for drop_x, drop_y, hot, role, _end in heads:
        path = QPainterPath()
        path.moveTo(drop_x, bus_y)
        path.lineTo(drop_x, drop_y)
        _add_path(scene, path, _line_color(hot, role), hot)


def _line_color(on_path: bool, role: Optional[str]) -> QColor:
    color = QColor(theme.col("accent") if on_path else LINE_COLOR.get(role or "common", LINE_COLOR["common"]))
    color.setAlpha(255)
    return color


def _add_path(scene, path: QPainterPath, color: QColor, strong: bool):
    item = QGraphicsPathItem(path)
    pen = QPen(color, 2.8 if strong else 2.4)
    pen.setCapStyle(Qt.PenCapStyle.SquareCap)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    item.setPen(pen)
    item.setZValue(2)
    scene.addItem(item)


def _side_connector(origin: tuple, end: tuple, left: bool) -> QPainterPath:
    """差分从卡片左边接出，追加从右边接出，横线只走两张卡之间的空隙。"""
    path = QPainterPath()
    inset = 3
    if left:
        x1 = origin[0] + inset
        x2 = end[0] + CARD_W - inset
    else:
        x1 = origin[0] + CARD_W - inset
        x2 = end[0] + inset
    y1 = origin[1] + CARD_H / 2
    y2 = end[1] + CARD_H / 2
    path.moveTo(x1, y1)
    path.lineTo(x2, y1)
    if abs(y1 - y2) >= 1:
        path.lineTo(x2, y2)
    return path


def _edge_path(kind: str, start: tuple, end: tuple, start_kind: str, end_kind: str) -> QPainterPath:
    """线段伸进卡片边缘一截，接头盖住边框，避免叠色发灰。"""
    path = QPainterPath()
    same_column = abs(start[0] - end[0]) < 1
    inset = 3
    if same_column or kind == "spine":
        x1 = start[0] + CARD_W / 2
        y1 = start[1] + (CARD_H - 8 if start_kind == "branch" else CARD_H - inset)
        x2 = end[0] + CARD_W / 2
        y2 = end[1] + (8 if end_kind == "branch" else inset)
        path.moveTo(x1, y1)
        path.lineTo(x2, y2)
        return path
    x1 = start[0] + CARD_W - inset
    y1 = start[1] + CARD_H / 2
    x2 = end[0] + inset
    y2 = end[1] + CARD_H / 2
    path.moveTo(x1, y1)
    path.lineTo(x2, y1)
    path.lineTo(x2, y2)
    return path


ROLE_NAME = {
    "common": "共通",
    "append": "追加",
    "variant": "差分",
    "route": "线路",
}


def _type_of(node: dict) -> tuple:
    if node.get("kind") == "branch":
        return "分支", BRANCH_COLOR
    role = node.get("role") or "common"
    return ROLE_NAME.get(role, "剧情"), ROLE_COLOR.get(role, ROLE_COLOR["common"])


def _apply_chip(label: QLabel, text: str, color: str):
    label.setText(text)
    label.setStyleSheet(
        "QLabel {"
        f"color: {color};"
        "background: transparent;"
        f"border: 1px solid {color};"
        "border-radius: 9px;"
        "padding: 1px 8px;"
        "font-size: 11px;"
        "font-weight: 600;"
        "}"
    )
    label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    label.adjustSize()


class _ReadDialog(QDialog):
    """双击节点后的阅读页，只展示不编辑。"""

    def __init__(self, node: dict, parent=None):
        super().__init__(parent)
        kind, color = _type_of(node)
        title = (node.get("title") or "").strip() or "未命名"
        condition = (node.get("condition_label") or "").strip()
        if node.get("role") not in ("append", "variant"):
            condition = ""
        content = (node.get("content") or "").strip()
        self.setWindowTitle(title)
        self.resize(680, 760)
        bg = theme.col("bg")
        fg = theme.col("text")
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(bg))
        palette.setColor(QPalette.ColorRole.Base, QColor(bg))
        palette.setColor(QPalette.ColorRole.Text, QColor(fg))
        self.setPalette(palette)
        self.setStyleSheet(
            "QDialog, QTextEdit, QTextEdit::viewport {"
            f"background: {bg};"
            f"color: {fg};"
            "border: none;"
            "}"
        )

        page = QVBoxLayout(self)
        page.setContentsMargins(36, 28, 36, 28)
        page.setSpacing(0)

        chip = QLabel(kind)
        _apply_chip(chip, kind, color)
        page.addWidget(chip, 0, Qt.AlignmentFlag.AlignLeft)
        page.addSpacing(14)

        heading = QLabel(title)
        heading.setWordWrap(True)
        heading.setStyleSheet(
            f"color: {theme.col('text')}; font-size: 22px; font-weight: 700; background: transparent;"
        )
        page.addWidget(heading)
        if condition:
            page.addSpacing(8)
            note = QLabel(condition)
            note.setWordWrap(True)
            note.setStyleSheet(
                f"color: {theme.col('text_dim')}; font-size: 13px; background: transparent;"
            )
            page.addWidget(note)

        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {theme.col('border')}; border: none;")
        page.addSpacing(18)
        page.addWidget(rule)
        page.addSpacing(18)

        body = QTextEdit()
        body.setReadOnly(True)
        body.setFrameShape(QFrame.Shape.NoFrame)
        body.setAutoFillBackground(True)
        body_palette = body.palette()
        body_palette.setColor(QPalette.ColorRole.Base, QColor(bg))
        body_palette.setColor(QPalette.ColorRole.Text, QColor(fg))
        body.setPalette(body_palette)
        body.viewport().setAutoFillBackground(True)
        body.viewport().setPalette(body_palette)
        shown = content or "这段还没有正文。"
        escaped = (
            shown.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        ink = theme.col("text_dim") if not content else fg
        body.document().setDefaultStyleSheet(
            f"body {{ background-color: {bg}; color: {ink}; }}"
        )
        body.setHtml(
            f"<body style='background-color: {bg}; color: {ink};'>"
            f"<div style='line-height: 175%; font-size: 15px;'>{escaped}</div></body>"
        )
        body.setPalette(body_palette)
        body.viewport().setPalette(body_palette)
        page.addWidget(body, 1)


class _BranchDialog(QDialog):
    """新建分支：子线路名称成为第一段标题，备注成为第一段正文。"""

    def __init__(self, prompt: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建分支")
        self.resize(440, 320)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        hint = QLabel(prompt)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(QLabel("子线路名称"))
        self.txt_name = QLineEdit()
        self.txt_name.setPlaceholderText("这条线路的名字")
        layout.addWidget(self.txt_name)
        layout.addWidget(QLabel("备注"))
        self.txt_note = QTextEdit()
        self.txt_note.setPlaceholderText("写在这条线路第一段的正文里，可以留空")
        layout.addWidget(self.txt_note, 1)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("创建")
        _variant(ok, "primary")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        layout.addLayout(row)

    def values(self) -> tuple:
        return self.txt_name.text().strip(), self.txt_note.toPlainText().strip()


class StoryTreePage(QWidget):
    cursor_changed = pyqtSignal()

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.tree_id: Optional[int] = None
        self.selected_id: Optional[int] = None
        self._graph = {"tree": None, "nodes": [], "edges": [], "cursor": None}
        self._pan_filter = False
        self._init_ui()

    def set_tree(self, tree_id: Optional[int]):
        self.tree_id = tree_id
        self.selected_id = None
        self.reload()

    def reload(self):
        if self.tree_id:
            self.db.ensure_branch_beats(self.tree_id)
            self._graph = self.db.story_graph(self.tree_id)
        else:
            self._graph = {"tree": None, "nodes": [], "edges": [], "cursor": None}
        self.canvas.set_graph(self._graph, self.selected_id)
        self._fill_editor()
        self._fill_cursor_label()

    def _init_ui(self):
        page = QVBoxLayout(self)
        page.setContentsMargins(16, 14, 16, 14)
        page.setSpacing(10)
        page.addWidget(_caption("剧情树 · STORY"))

        head = QHBoxLayout()
        self.lbl_cursor = QLabel("未选择剧情树")
        self.lbl_cursor.setProperty("cls", "dim")
        head.addWidget(self.lbl_cursor, 1)
        legend = QLabel("主链向下　差分在左　追加在右　按住空格拖动　Ctrl+滚轮缩放")
        legend.setProperty("cls", "dim")
        head.addWidget(legend)
        page.addLayout(head)

        self.canvas = StoryCanvas()
        self.canvas.node_clicked.connect(self._select_node)
        self.canvas.node_menu.connect(self._show_node_menu)
        self.canvas.node_double_clicked.connect(self._open_reader)
        frame = QFrame()
        frame.setProperty("card", True)
        frame.setMinimumWidth(280)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(8, 8, 8, 8)
        frame_layout.addWidget(self.canvas)

        side = QFrame()
        side.setProperty("card", True)
        side.setMinimumWidth(240)
        form = QVBoxLayout(side)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)
        self.lbl_kind = QLabel("")
        self.lbl_kind.hide()
        form.addWidget(self.lbl_kind, 0, Qt.AlignmentFlag.AlignLeft)
        form.addWidget(QLabel("标题"))
        self.txt_title = QLineEdit()
        form.addWidget(self.txt_title)
        form.addWidget(QLabel("正文"))
        self.txt_content = QTextEdit()
        form.addWidget(self.txt_content, 1)
        self.lbl_condition = QLabel("条件")
        form.addWidget(self.lbl_condition)
        self.txt_condition = QLineEdit()
        self.txt_condition.setPlaceholderText("例如：选择安慰后出现")
        form.addWidget(self.txt_condition)
        self.lbl_condition.hide()
        self.txt_condition.hide()

        self.btn_save = QPushButton("保存")
        _variant(self.btn_save, "primary")
        self.btn_save.clicked.connect(self._save_node)
        form.addWidget(self.btn_save)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)
        splitter.addWidget(frame)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([760, 320])
        page.addWidget(splitter, 1)
        self._set_editor_enabled(False)

    def showEvent(self, event):
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None and not self._pan_filter:
            app.installEventFilter(self)
            self._pan_filter = True

    def hideEvent(self, event):
        self.canvas.end_pan()
        app = QApplication.instance()
        if app is not None and self._pan_filter:
            app.removeEventFilter(self)
            self._pan_filter = False
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
                focus = QApplication.focusWidget()
                if self._space_pan_allowed(focus):
                    if event.type() == QEvent.Type.KeyPress:
                        self.canvas.begin_pan()
                    else:
                        self.canvas.end_pan()
                    return True
        return super().eventFilter(obj, event)

    def _space_pan_allowed(self, focus) -> bool:
        if focus is None:
            return False
        if focus is not self and not self.isAncestorOf(focus):
            return False
        widget = focus
        while widget is not None and widget is not self:
            if isinstance(widget, (QLineEdit, QTextEdit, QDialog)):
                return False
            widget = widget.parentWidget()
        return widget is self

    def _nodes(self) -> Dict[int, dict]:
        return {node["id"]: node for node in self._graph.get("nodes") or []}

    def _selected(self) -> Optional[dict]:
        return self._nodes().get(self.selected_id)

    def _select_node(self, node_id: int):
        changed = self.selected_id != node_id
        self.selected_id = node_id
        if changed:
            self.canvas.set_graph(self._graph, self.selected_id)
        self._fill_editor()

    def _fill_cursor_label(self):
        if not self.tree_id:
            self.lbl_cursor.setText("未选择剧情树")
            return
        tree = self._graph.get("tree") or {}
        name = tree.get("name") or "剧情树"
        where = self.db.story_cursor_label(self.tree_id)
        self.lbl_cursor.setText(f"{name}　·　时点：{where}")

    def _fill_editor(self):
        node = self._selected()
        if not node:
            self.lbl_kind.hide()
            self._set_condition_visible(False)
            self.txt_title.clear()
            self.txt_content.clear()
            self.txt_condition.clear()
            self._set_editor_enabled(False)
            return
        self._set_editor_enabled(True)
        kind, color = _type_of(node)
        _apply_chip(self.lbl_kind, kind, color)
        self.lbl_kind.show()
        show_condition = node.get("kind") == "beat" and node.get("role") in ("append", "variant")
        self._set_condition_visible(show_condition)
        if node["kind"] == "branch":
            self.txt_title.setText("分支")
            self.txt_content.clear()
            self.txt_condition.clear()
            self.txt_title.setReadOnly(True)
            self.txt_content.setReadOnly(True)
            self.btn_save.setEnabled(False)
        else:
            self.txt_title.setReadOnly(False)
            self.txt_content.setReadOnly(False)
            self.txt_title.setText(node.get("title") or "")
            self.txt_content.setPlainText(node.get("content") or "")
            self.txt_condition.setText(node.get("condition_label") or "" if show_condition else "")
            self.btn_save.setEnabled(True)

    def _set_condition_visible(self, visible: bool):
        self.lbl_condition.setVisible(visible)
        self.txt_condition.setVisible(visible)

    def _set_editor_enabled(self, enabled: bool):
        for widget in (self.txt_title, self.txt_content, self.txt_condition, self.btn_save):
            widget.setEnabled(enabled)

    def _show_node_menu(self, node_id: int, global_pos):
        self._select_node(node_id)
        node = self._selected()
        if not node:
            return
        menu = QMenu(self)
        for label, handler in self._menu_actions(node):
            if label == "删除" and menu.actions():
                menu.addSeparator()
            action = menu.addAction(label)
            action.triggered.connect(lambda _checked=False, fn=handler: fn())
        menu.exec(global_pos)

    def _menu_actions(self, node: dict):
        actions = [("设为总结时点", self._set_cursor)]
        if node["kind"] == "beat":
            actions.append(("拆分", self._split))
            if self._is_spine_common(node):
                actions.append(("追加", lambda: self._add_side("append")))
                actions.append(("差分", lambda: self._add_side("variant")))
        if self._can_branch_from(node):
            actions.append(("新建分支", self._add_branch))
        actions.append(("删除", self._delete))
        return actions

    def _incoming_edge(self, node_id: int) -> Optional[dict]:
        hits = [
            edge for edge in self._graph.get("edges") or []
            if edge.get("to_node_id") == node_id
        ]
        return hits[0] if len(hits) == 1 else None

    def _is_spine_common(self, node: dict) -> bool:
        if node.get("kind") != "beat" or node.get("role") != "common":
            return False
        incoming = self._incoming_edge(node["id"])
        return incoming is None or incoming.get("kind") == "spine"

    def _can_branch_from(self, node: dict) -> bool:
        if node.get("kind") == "branch" or node.get("role") == "route":
            return True
        return self._is_spine_common(node)

    def _open_reader(self, node_id: int):
        self._select_node(node_id)
        node = self._selected()
        if not node:
            return
        _ReadDialog(node, self).exec()

    def _save_node(self):
        node = self._selected()
        if not node or node["kind"] != "beat":
            return
        fields = {
            "title": self.txt_title.text().strip(),
            "content": self.txt_content.toPlainText(),
        }
        if node.get("role") in ("append", "variant"):
            fields["condition_label"] = self.txt_condition.text().strip()
        try:
            self.db.update_story_node(node["id"], **fields)
        except StoryTreeError as exc:
            QMessageBox.warning(self, "无法保存", str(exc))
            return
        self.reload()
        self.cursor_changed.emit()

    def _set_cursor(self):
        node = self._selected()
        if not node or not self.tree_id:
            return
        via = None
        if node["kind"] == "branch":
            edges = [
                edge for edge in self._graph.get("edges") or []
                if edge["from_node_id"] == node["id"] and edge["kind"] == "branch"
            ]
            if len(edges) > 1:
                labels = [(edge.get("label") or "").strip() or "未命名线路" for edge in edges]
                label, ok = QInputDialog.getItem(self, "选择线路", "总结写到哪一条线路后面：", labels, 0, False)
                if not ok:
                    return
                via = edges[labels.index(label)]["id"]
            elif len(edges) == 1:
                via = edges[0]["id"]
        try:
            self.db.set_story_cursor(self.tree_id, node["id"], via)
        except StoryTreeError as exc:
            QMessageBox.warning(self, "无法设置时点", str(exc))
            return
        self.reload()
        self.cursor_changed.emit()

    def _add_side(self, role: str):
        node = self._selected()
        if not node or not self.tree_id:
            return
        title = "新建追加" if role == "append" else "新建差分"
        condition, ok = QInputDialog.getText(self, title, "条件（可留空，只作展示）：")
        if not ok:
            return
        try:
            new_id = self.db.add_side_beat(self.tree_id, node["id"], role, condition)
        except StoryTreeError as exc:
            QMessageBox.warning(self, "无法新建", str(exc))
            return
        self.selected_id = new_id
        self.reload()

    def _has_chain_successor(self, node: dict) -> bool:
        if node.get("kind") != "beat":
            return False
        incoming = self._incoming_edge(node["id"])
        if not incoming or incoming.get("kind") == "spine":
            kind = "spine"
        elif incoming.get("kind") in ("branch", "insert"):
            kind = incoming["kind"]
        else:
            kind = "spine"
        return any(
            edge.get("from_node_id") == node["id"] and edge.get("kind") == kind and edge.get("to_node_id")
            for edge in self._graph.get("edges") or []
        )

    def _branch_prompt(self) -> str:
        node = self._selected()
        if node and node["kind"] == "branch":
            return "在这个分支上再开一条线路："
        if node and node["kind"] == "beat" and self._has_chain_successor(node):
            return "从这里插入分支。后面的剧情会接到新线路第一段的后面："
        if node and node.get("role") == "route":
            return "在这里新建子分支，填写子线路名称："
        return "线路名称："

    def _add_branch(self):
        if not self.tree_id:
            return
        dialog = _BranchDialog(self._branch_prompt(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        label, note = dialog.values()
        if not label:
            QMessageBox.information(self, "新建分支", "请填写子线路名称。")
            return
        try:
            created = self.db.add_branch(self.tree_id, label, self.selected_id, note)
            self.db.set_story_cursor(self.tree_id, created["beat_id"])
        except StoryTreeError as exc:
            QMessageBox.warning(self, "无法新建分支", str(exc))
            return
        self.selected_id = created["beat_id"]
        self.reload()
        self.cursor_changed.emit()

    def _split(self):
        node = self._selected()
        if not node or not self.tree_id or node["kind"] != "beat":
            return
        try:
            new_id = self.db.split_beat_after(self.tree_id, node["id"])
        except StoryTreeError as exc:
            QMessageBox.warning(self, "无法拆分", str(exc))
            return
        dialog = SplitBeatDialog(node.get("content") or "", self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            try:
                self.db.delete_story_node(new_id)
            except StoryTreeError:
                pass
            self.reload()
            return
        left, right = dialog.halves()
        self.db.update_story_node(node["id"], content=left)
        self.db.update_story_node(new_id, content=right, title=self.txt_title.text().strip())
        self.selected_id = new_id
        self.reload()
        self.cursor_changed.emit()

    def _delete(self):
        node = self._selected()
        if not node:
            return
        title = (node.get("title") or "").strip() or ("分支" if node["kind"] == "branch" else "未命名")
        if node["kind"] == "branch":
            prompt = "确定删除这个分支点吗？后面有多条线路时要先删掉；只剩一条时，这条线路会接回上面的剧情。"
        else:
            prompt = f"确定删除「{title}」吗？后面还有剧情时需要先删后面的。"
        reply = QMessageBox.question(self, "确认删除", prompt)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.db.delete_story_node(node["id"])
        except StoryTreeError as exc:
            QMessageBox.warning(self, "无法删除", str(exc))
            return
        self.selected_id = None
        self.reload()
        self.cursor_changed.emit()
