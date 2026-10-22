"""旧数据转换窗口。"""

import json
import os
import sys
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCursor, QDrag
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import theme
from old_data_import_app.bundle import build_bundle
from old_data_import_app.canvas import MIME, TreeCanvas
from old_data_import_app.graph import GraphError, StoryGraph
from old_data_import_app.legacy import game_routes, load_legacy, route_summaries, summary_by_id


class SummaryList(QListWidget):
    def mimeTypes(self):
        return [MIME]

    def mimeData(self, items):
        from PyQt6.QtCore import QMimeData
        mime = QMimeData()
        if items:
            summary_id = items[0].data(Qt.ItemDataRole.UserRole)
            mime.setData(MIME, str(summary_id).encode("utf-8"))
        return mime

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if item is None:
            return
        drag = QDrag(self)
        drag.setMimeData(self.mimeData([item]))
        drag.exec(Qt.DropAction.CopyAction)


class ConverterWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setObjectName("ConverterWindow")
        self.setWindowTitle("GalCoplit 旧数据转换")
        self.data: Optional[dict] = None
        self.game_id: Optional[int] = None
        self.work: Dict[int, List[StoryGraph]] = {}
        self.graph: Optional[StoryGraph] = None
        self.selected_id: Optional[int] = None
        self._filling = False
        self._build_ui()

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("ConverterRoot")
        self.setCentralWidget(root)
        page = QVBoxLayout(root)
        page.setContentsMargins(16, 14, 16, 14)
        page.setSpacing(10)

        bar = QHBoxLayout()
        btn_open = QPushButton("打开旧数据库")
        btn_open.clicked.connect(self._choose_db)
        self.cmb_game = QComboBox()
        self.cmb_game.setMinimumWidth(220)
        self.cmb_game.currentIndexChanged.connect(self._on_game_changed)
        self.cmb_tree = QComboBox()
        self.cmb_tree.setMinimumWidth(140)
        self.cmb_tree.currentIndexChanged.connect(self._on_tree_changed)
        btn_tree = QPushButton("新建剧情树")
        btn_tree.clicked.connect(self._add_tree)
        btn_export = QPushButton("导出数据")
        btn_export.clicked.connect(self._export)
        bar.addWidget(btn_open)
        bar.addWidget(self.cmb_game, 1)
        bar.addWidget(self.cmb_tree)
        bar.addWidget(btn_tree)
        bar.addWidget(btn_export)
        page.addLayout(bar)

        self.lbl_status = QLabel("先打开 1.x 的 records 数据库。")
        self.lbl_status.setProperty("cls", "dim")
        page.addWidget(self.lbl_status)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("ConverterSplit")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)

        left = QFrame()
        left.setProperty("card", True)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.addWidget(QLabel("旧线路"))
        self.list_routes = QListWidget()
        self.list_routes.currentRowChanged.connect(self._on_route_changed)
        left_layout.addWidget(self.list_routes, 1)
        left_layout.addWidget(QLabel("旧章节"))
        self.txt_filter = QLineEdit()
        self.txt_filter.setPlaceholderText("筛选标题或正文")
        self.txt_filter.textChanged.connect(self._fill_summaries)
        left_layout.addWidget(self.txt_filter)
        check_row = QHBoxLayout()
        btn_all = QPushButton("全选")
        btn_all.clicked.connect(lambda: self._set_all_checks(True))
        btn_none = QPushButton("取消全选")
        btn_none.clicked.connect(lambda: self._set_all_checks(False))
        self.lbl_checked = QLabel("已勾选 0")
        self.lbl_checked.setProperty("cls", "dim")
        check_row.addWidget(btn_all)
        check_row.addWidget(btn_none)
        check_row.addWidget(self.lbl_checked, 1)
        left_layout.addLayout(check_row)
        self.list_summaries = SummaryList()
        self.list_summaries.setDragEnabled(True)
        self.list_summaries.setDragDropMode(QListWidget.DragDropMode.DragOnly)
        self.list_summaries.currentRowChanged.connect(self._preview_summary)
        self.list_summaries.itemChanged.connect(lambda _item: self._update_checked_count())
        left_layout.addWidget(self.list_summaries, 2)
        btn_batch = QPushButton("添加到时点后")
        btn_batch.clicked.connect(self._add_checked)
        left_layout.addWidget(btn_batch)
        self.lbl_cursor = QLabel("未设置总结时点")
        self.lbl_cursor.setWordWrap(True)
        self.lbl_cursor.setProperty("cls", "dim")
        left_layout.addWidget(self.lbl_cursor)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("选中一章可以在这里预览")
        left_layout.addWidget(self.preview, 1)
        splitter.addWidget(left)

        center = QFrame()
        center.setProperty("card", True)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(8, 8, 8, 8)
        hint = QLabel("勾选左侧章节后点「添加到时点后」，按列表顺序接在总结时点后面。全选只作用于当前筛出来的章节。空格拖动，Ctrl+滚轮缩放。")
        hint.setWordWrap(True)
        hint.setProperty("cls", "dim")
        center_layout.addWidget(hint)
        self.canvas = TreeCanvas()
        self.canvas.node_clicked.connect(self._select_node)
        self.canvas.summary_dropped.connect(self._on_summary_dropped)
        center_layout.addWidget(self.canvas, 1)
        actions = QHBoxLayout()
        btn_blank = QPushButton("空白节点")
        btn_blank.clicked.connect(self._add_blank)
        btn_branch = QPushButton("新建分支")
        btn_branch.clicked.connect(self._add_branch)
        btn_cursor = QPushButton("设为总结时点")
        btn_cursor.clicked.connect(self._set_cursor)
        btn_delete = QPushButton("删除")
        btn_delete.clicked.connect(self._delete_node)
        actions.addWidget(btn_blank)
        actions.addWidget(btn_branch)
        actions.addWidget(btn_cursor)
        actions.addWidget(btn_delete)
        actions.addStretch()
        center_layout.addLayout(actions)
        splitter.addWidget(center)

        side = QFrame()
        side.setProperty("card", True)
        side.setMinimumWidth(280)
        form = QVBoxLayout(side)
        form.setContentsMargins(12, 12, 12, 12)
        self.lbl_kind = QLabel("未选中")
        form.addWidget(self.lbl_kind)
        form.addWidget(QLabel("剧情树提示词"))
        self.txt_prompt = QLineEdit()
        self.txt_prompt.setPlaceholderText("只对当前这棵树生效，可以留空")
        self.txt_prompt.editingFinished.connect(self._save_prompt)
        form.addWidget(self.txt_prompt)
        form.addWidget(QLabel("标题"))
        self.txt_title = QLineEdit()
        form.addWidget(self.txt_title)
        form.addWidget(QLabel("正文"))
        self.txt_content = QTextEdit()
        form.addWidget(self.txt_content, 1)
        self.lbl_condition = QLabel("条件")
        form.addWidget(self.lbl_condition)
        self.txt_condition = QLineEdit()
        self.txt_condition.setPlaceholderText("只用于追加和差分")
        form.addWidget(self.txt_condition)
        btn_save = QPushButton("保存节点")
        btn_save.clicked.connect(self._save_node)
        form.addWidget(btn_save)
        splitter.addWidget(side)
        splitter.setSizes([320, 680, 320])
        page.addWidget(splitter, 1)
        self._set_form_enabled(False)

    def open_path(self, path: str):
        try:
            data = load_legacy(path)
        except Exception as exc:
            QMessageBox.critical(self, "无法打开", str(exc))
            return
        if not data["games"]:
            QMessageBox.information(self, "没有游戏", "这个数据库里没有游戏。")
            return
        self.data = data
        self.work = {}
        self.graph = None
        self.selected_id = None
        self.cmb_game.blockSignals(True)
        self.cmb_game.clear()
        for game in data["games"]:
            self.cmb_game.addItem(game.get("name") or "未命名游戏", game["id"])
        self.cmb_game.blockSignals(False)
        self.cmb_game.setCurrentIndex(0)
        self._on_game_changed()
        self.lbl_status.setText(path)

    def _choose_db(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开旧数据库", "", "数据库 (*.db)")
        if path:
            self.open_path(path)

    def _on_game_changed(self):
        self._apply_form()
        if not self.data:
            return
        game_id = self.cmb_game.currentData()
        if game_id is None:
            return
        self.game_id = int(game_id)
        if self.game_id not in self.work:
            self.work[self.game_id] = [StoryGraph("本篇")]
        self.list_routes.clear()
        for route in game_routes(self.data, self.game_id):
            count = len(route_summaries(self.data, route["id"]))
            item = QListWidgetItem(f"{route.get('name') or '未命名线路'}  ({count})")
            item.setData(Qt.ItemDataRole.UserRole, route["id"])
            self.list_routes.addItem(item)
        if self.list_routes.count():
            self.list_routes.setCurrentRow(0)
        self._reload_tree_combo()
        self._update_status()

    def _reload_tree_combo(self):
        trees = self.work.get(self.game_id) or []
        current = self.cmb_tree.currentIndex()
        self.cmb_tree.blockSignals(True)
        self.cmb_tree.clear()
        for tree in trees:
            self.cmb_tree.addItem(tree.name)
        if trees:
            self.cmb_tree.setCurrentIndex(current if 0 <= current < len(trees) else 0)
        self.cmb_tree.blockSignals(False)
        self._show_tree()

    def _on_tree_changed(self):
        self._apply_form()
        self._show_tree()

    def _show_tree(self):
        trees = self.work.get(self.game_id) or []
        index = self.cmb_tree.currentIndex()
        self.graph = trees[index] if 0 <= index < len(trees) else None
        self.selected_id = None
        self._filling = True
        self.txt_prompt.setText(self.graph.prompt if self.graph else "")
        self._filling = False
        self._refresh_canvas()
        self._fill_form()
        self._fill_summaries()

    def _add_tree(self):
        if self.game_id is None:
            return
        name, ok = QInputDialog.getText(self, "新建剧情树", "名称：")
        if not ok or not name.strip():
            return
        self.work[self.game_id].append(StoryGraph(name.strip()))
        self._reload_tree_combo()
        self.cmb_tree.setCurrentIndex(len(self.work[self.game_id]) - 1)

    def _on_route_changed(self):
        self._fill_summaries()
        self.preview.clear()

    def _current_route_id(self) -> Optional[int]:
        item = self.list_routes.currentItem()
        if item is None:
            return None
        return int(item.data(Qt.ItemDataRole.UserRole))

    def _fill_summaries(self):
        self.list_summaries.blockSignals(True)
        self.list_summaries.clear()
        if not self.data:
            self.list_summaries.blockSignals(False)
            self._update_checked_count()
            return
        route_id = self._current_route_id()
        if route_id is None:
            self.list_summaries.blockSignals(False)
            self._update_checked_count()
            return
        keyword = self.txt_filter.text().strip()
        used = set()
        for tree in self.work.get(self.game_id) or []:
            used |= tree.used_summary_ids()
        for summary in route_summaries(self.data, route_id):
            title = (summary.get("title") or "").strip() or "未命名"
            content = summary.get("content") or ""
            if keyword and keyword not in title and keyword not in content:
                continue
            mark = "已用  " if summary["id"] in used else ""
            item = QListWidgetItem(f"{mark}{title}")
            item.setData(Qt.ItemDataRole.UserRole, summary["id"])
            item.setToolTip(content[:240])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.list_summaries.addItem(item)
        self.list_summaries.blockSignals(False)
        self._update_checked_count()

    def _preview_summary(self):
        item = self.list_summaries.currentItem()
        if item is None or not self.data:
            self.preview.clear()
            return
        summary = summary_by_id(self.data, int(item.data(Qt.ItemDataRole.UserRole)))
        if not summary:
            self.preview.clear()
            return
        title = (summary.get("title") or "").strip() or "未命名"
        self.preview.setPlainText(f"{title}\n\n{summary.get('content') or ''}")

    def _select_node(self, node_id: int):
        self._apply_form()
        self.selected_id = node_id
        self._refresh_canvas()
        self._fill_form()

    def _on_summary_dropped(self, node_id: int, summary_id: int):
        if not self.graph or not self.data:
            return
        self._apply_form()
        summary = summary_by_id(self.data, summary_id)
        if not summary:
            return
        title = (summary.get("title") or "").strip() or "未命名"
        content = summary.get("content") or ""
        try:
            if node_id and node_id in self.graph.nodes:
                node = self.graph.nodes[node_id]
                if self.graph.is_spine_common(node_id):
                    self._drop_on_common(node_id, summary_id, title, content)
                    return
                if node["kind"] == "branch":
                    self._drop_on_branch(node_id, summary_id, title, content)
                    return
                self.graph.add_after(node_id, title, content, source_id=summary_id)
                self.selected_id = self.graph.cursor_id
            else:
                self.graph.append_after_cursor(title, content, source_id=summary_id)
                self.selected_id = self.graph.cursor_id
        except GraphError as exc:
            QMessageBox.warning(self, "无法放入", str(exc))
            return
        self._refresh_canvas()
        self._fill_form()
        self._fill_summaries()

    def _drop_on_common(self, node_id: int, summary_id: int, title: str, content: str):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.addAction("接到主链后面", lambda _checked=False: self._run_drop(lambda: self.graph.add_common(title, content, after_id=node_id, source_id=summary_id)))
        menu.addAction("作为追加", lambda _checked=False: self._run_drop(lambda: self._add_side_from_drop(node_id, "append", summary_id, title, content)))
        menu.addAction("作为差分", lambda _checked=False: self._run_drop(lambda: self._add_side_from_drop(node_id, "variant", summary_id, title, content)))
        menu.exec(QCursor.pos())

    def _add_side_from_drop(self, node_id: int, role: str, summary_id: int, title: str, content: str):
        label = "追加" if role == "append" else "差分"
        condition, ok = QInputDialog.getText(self, f"新建{label}", "条件（可留空）：")
        if not ok:
            return
        self.graph.add_side(node_id, role, title, content, condition.strip(), source_id=summary_id)

    def _drop_on_branch(self, node_id: int, summary_id: int, title: str, content: str):
        name, note = self._branch_dialog("在这个分支上再开一条线路", title, content)
        if not name:
            return
        self._run_drop(lambda: self.graph.add_branch(node_id, name, note, source_id=summary_id))

    def _run_drop(self, action):
        try:
            action()
        except GraphError as exc:
            QMessageBox.warning(self, "无法放入", str(exc))
            return
        if self.graph and self.graph.cursor_id:
            self.selected_id = self.graph.cursor_id
        self._refresh_canvas()
        self._fill_form()
        self._fill_summaries()

    def _add_blank(self):
        if not self.graph:
            return
        self._apply_form()
        try:
            self.graph.append_after_cursor("未命名", "")
        except GraphError as exc:
            QMessageBox.warning(self, "无法新增", str(exc))
            return
        self.selected_id = self.graph.cursor_id
        self._refresh_canvas()
        self._fill_form()

    def _add_branch(self):
        if not self.graph:
            return
        self._apply_form()
        anchor = self.selected_id if self.selected_id in (self.graph.nodes if self.graph else {}) else None
        where = "在主链末端新建分支"
        if anchor and self.graph.nodes[anchor]["kind"] == "branch":
            where = "在这个分支上再开一条线路"
        elif anchor and self.graph.continues_after(anchor):
            where = "从这里插入分支。后面的剧情会接到新线路第一段的后面"
        elif anchor and self.graph.nodes[anchor].get("role") == "route":
            where = "在这里新建子分支"
        name, note = self._branch_dialog(where, "", "")
        if not name:
            return
        try:
            self.graph.add_branch(anchor, name, note)
        except GraphError as exc:
            QMessageBox.warning(self, "无法新建分支", str(exc))
            return
        self.selected_id = self.graph.cursor_id
        self._refresh_canvas()
        self._fill_form()

    def _branch_dialog(self, prompt: str, name: str, note: str):
        from PyQt6.QtWidgets import QDialog
        dialog = QDialog(self)
        dialog.setWindowTitle("新建分支")
        dialog.resize(420, 300)
        layout = QVBoxLayout(dialog)
        hint = QLabel(prompt)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addWidget(QLabel("子线路名称"))
        txt_name = QLineEdit(name)
        layout.addWidget(txt_name)
        layout.addWidget(QLabel("备注"))
        txt_note = QTextEdit()
        txt_note.setPlainText(note)
        txt_note.setPlaceholderText("会成为这条线路第一段的正文")
        layout.addWidget(txt_note, 1)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(dialog.reject)
        ok = QPushButton("创建")
        ok.clicked.connect(dialog.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        layout.addLayout(row)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return "", ""
        return txt_name.text().strip(), txt_note.toPlainText().strip()

    def _set_cursor(self):
        if not self.graph or not self.selected_id:
            return
        try:
            self.graph.set_cursor(self.selected_id)
        except GraphError as exc:
            QMessageBox.warning(self, "无法设置时点", str(exc))
            return
        self._refresh_canvas()

    def _delete_node(self):
        if not self.graph or not self.selected_id:
            return
        try:
            self.graph.delete(self.selected_id)
        except GraphError as exc:
            QMessageBox.warning(self, "无法删除", str(exc))
            return
        self.selected_id = None
        self._refresh_canvas()
        self._fill_form()
        self._fill_summaries()

    def _save_prompt(self):
        if self._filling or not self.graph:
            return
        self.graph.prompt = self.txt_prompt.text().strip()

    def _save_node(self):
        self._apply_form()
        self._refresh_canvas()

    def _apply_form(self):
        if self._filling or not self.graph or not self.selected_id:
            return
        node = self.graph.nodes.get(self.selected_id)
        if not node or node["kind"] != "beat":
            return
        condition = self.txt_condition.text().strip() if node.get("role") in ("append", "variant") else None
        self.graph.update_beat(
            node["id"],
            self.txt_title.text().strip(),
            self.txt_content.toPlainText(),
            condition,
        )
        self.graph.prompt = self.txt_prompt.text().strip()

    def _fill_form(self):
        node = self.graph.nodes.get(self.selected_id) if self.graph and self.selected_id else None
        self._filling = True
        if not node:
            self.lbl_kind.setText("未选中")
            self.txt_title.clear()
            self.txt_content.clear()
            self.txt_condition.clear()
            self._set_condition_visible(False)
            self._set_form_enabled(False)
            self._filling = False
            return
        self._set_form_enabled(node["kind"] == "beat")
        if node["kind"] == "branch":
            self.lbl_kind.setText("分支")
            self.txt_title.setText("分支")
            self.txt_content.clear()
            self._set_condition_visible(False)
            self._filling = False
            return
        names = {"common": "共通", "append": "追加", "variant": "差分", "route": "线路"}
        self.lbl_kind.setText(names.get(node.get("role"), "剧情"))
        self.txt_title.setText(node.get("title") or "")
        self.txt_content.setPlainText(node.get("content") or "")
        show_condition = node.get("role") in ("append", "variant")
        self._set_condition_visible(show_condition)
        self.txt_condition.setText(node.get("condition_label") or "" if show_condition else "")
        self._filling = False

    def _set_condition_visible(self, visible: bool):
        self.lbl_condition.setVisible(visible)
        self.txt_condition.setVisible(visible)

    def _set_form_enabled(self, enabled: bool):
        self.txt_title.setReadOnly(not enabled)
        self.txt_content.setReadOnly(not enabled)
        self.txt_condition.setReadOnly(not enabled)

    def _refresh_canvas(self):
        if hasattr(self, "lbl_cursor"):
            self.lbl_cursor.setText(self._cursor_label())
        if not self.graph or not self.graph.nodes:
            self.canvas.set_message("勾选左侧章节，添加到时点后。空树会从第一段开始。")
            return
        self.canvas.set_graph(self.graph.as_layout_graph(), self.selected_id)

    def _export(self):
        if not self.data or self.game_id is None:
            QMessageBox.information(self, "还没有数据", "请先打开旧数据库并选择游戏。")
            return
        self._apply_form()
        trees = self.work.get(self.game_id) or []
        if not any(tree.nodes for tree in trees):
            reply = QMessageBox.question(
                self,
                "还没有新剧情树",
                "当前游戏还没有拖入任何章节。导出的文件只包含旧线路，导入后不会出现新的剧情树。仍要导出吗？",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        game = next(row for row in self.data["games"] if row["id"] == self.game_id)
        safe = "".join(ch for ch in (game.get("name") or "") if ch not in '\\/:*?"<>|').strip() or "游戏"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出游戏数据",
            f"{safe}.galcoplit.json",
            "GalCoplit 游戏数据 (*.galcoplit.json)",
        )
        if not path:
            return
        if not path.lower().endswith(".galcoplit.json"):
            path += ".galcoplit.json"
        try:
            payload = build_bundle(self.data, self.game_id, trees)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(
            self,
            "导出成功",
            f"已导出到：\n{path}\n\n在 2.0 的游戏管理里选择「导入游戏」。",
        )

    def _set_all_checks(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.list_summaries.blockSignals(True)
        for index in range(self.list_summaries.count()):
            self.list_summaries.item(index).setCheckState(state)
        self.list_summaries.blockSignals(False)
        self._update_checked_count()

    def _update_checked_count(self):
        count = sum(
            1
            for index in range(self.list_summaries.count())
            if self.list_summaries.item(index).checkState() == Qt.CheckState.Checked
        )
        self.lbl_checked.setText(f"已勾选 {count}")

    def _checked_summaries(self) -> list:
        chosen = []
        if not self.data:
            return chosen
        for index in range(self.list_summaries.count()):
            item = self.list_summaries.item(index)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            summary = summary_by_id(self.data, int(item.data(Qt.ItemDataRole.UserRole)))
            if summary:
                chosen.append(summary)
        return chosen

    def _add_checked(self):
        if not self.graph or not self.data:
            QMessageBox.information(self, "还没有数据", "请先打开旧数据库并选择游戏。")
            return
        chosen = self._checked_summaries()
        if not chosen:
            QMessageBox.information(self, "没有勾选", "请先勾选要添加的章节。全选只包含当前列表里看得到的章节。")
            return
        self._apply_form()
        added = 0
        try:
            for summary in chosen:
                title = (summary.get("title") or "").strip() or "未命名"
                self.graph.append_after_cursor(title, summary.get("content") or "", source_id=summary["id"])
                added += 1
        except GraphError as exc:
            self.selected_id = self.graph.cursor_id
            self._refresh_canvas()
            self._fill_form()
            self._fill_summaries()
            if added:
                QMessageBox.warning(self, "只添加了一部分", f"已添加 {added} 章，后面的没有接上。\n{exc}")
            else:
                QMessageBox.warning(self, "无法添加", str(exc))
            return
        self.selected_id = self.graph.cursor_id
        self._refresh_canvas()
        self._fill_form()
        self._fill_summaries()

    def _cursor_label(self) -> str:
        if not self.graph or not self.graph.cursor_id:
            return "未设置总结时点。点树上的段落，再点「设为总结时点」。"
        node = self.graph.nodes.get(self.graph.cursor_id)
        if not node:
            return "未设置总结时点。点树上的段落，再点「设为总结时点」。"
        title = (node.get("title") or "").strip() or "未命名"
        return f"接下来会接到时点「{title}」后面。"

    def _update_status(self):
        if not self.data or self.game_id is None:
            return
        routes = game_routes(self.data, self.game_id)
        route_ids = {row["id"] for row in routes}
        summaries = sum(1 for row in self.data["summaries"] if row.get("route_id") in route_ids)
        keys = sum(1 for row in self.data["key_info"] if row.get("game_id") == self.game_id or row.get("route_id") in route_ids)
        chars = sum(1 for row in self.data["characters"] if row.get("game_id") == self.game_id or row.get("route_id") in route_ids)
        self.lbl_status.setText(
            f"旧线路 {len(routes)} 条，章节 {summaries}，关键信息 {keys}，角色 {chars}。这些会随导出一起带走。"
        )


def main():
    app = QApplication(sys.argv)
    theme.apply_theme(app, "light")
    window = ConverterWindow()
    window.resize(1280, 820)
    window.show()
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        window.open_path(sys.argv[1])
    sys.exit(app.exec())
