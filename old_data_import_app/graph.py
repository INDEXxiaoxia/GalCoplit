"""内存里的剧情树。连线规则和 2.0 主程序一致，导出后可以直接导入。"""

from typing import Dict, List, Optional


class GraphError(Exception):
    pass


class StoryGraph:
    def __init__(self, name: str = "本篇"):
        self.name = name
        self.prompt = ""
        self.nodes: Dict[int, dict] = {}
        self.edges: List[dict] = []
        self.cursor_id: Optional[int] = None
        self._seq = 1

    def as_layout_graph(self) -> dict:
        cursor = {"after_node_id": self.cursor_id} if self.cursor_id else None
        return {"nodes": list(self.nodes.values()), "edges": list(self.edges), "cursor": cursor}

    def export_tree(self, tree_id: int, created_at: str) -> dict:
        nodes = []
        for node in self.nodes.values():
            nodes.append({
                "id": node["id"],
                "kind": node["kind"],
                "role": node["role"],
                "title": node.get("title") or "",
                "content": node.get("content") or "",
                "raw_text": "",
                "variant_of": node.get("variant_of"),
                "condition_label": node.get("condition_label") or "",
                "include_in_context": 0,
                "created_at": created_at,
            })
        edges = [
            {
                "id": edge["id"],
                "from_node_id": edge["from_node_id"],
                "to_node_id": edge.get("to_node_id"),
                "kind": edge["kind"],
                "label": edge.get("label") or "",
                "sort_order": int(edge.get("sort_order") or 0),
            }
            for edge in self.edges
        ]
        cursor = None
        if self.cursor_id and self.cursor_id in self.nodes and self.nodes[self.cursor_id]["kind"] == "beat":
            cursor = {"after_node_id": self.cursor_id, "via_edge_id": None}
        return {
            "id": tree_id,
            "name": self.name,
            "prompt": self.prompt or "",
            "anchor_route_id": None,
            "sort_order": tree_id,
            "created_at": created_at,
            "nodes": nodes,
            "edges": edges,
            "cursor": cursor,
        }

    def add_common(self, title: str, content: str, after_id: Optional[int] = None, source_id: Optional[int] = None) -> int:
        if not self.nodes:
            node_id = self._new_node("beat", "common", title, content, source_id=source_id)
            self.cursor_id = node_id
            return node_id
        if after_id and self.is_spine_common(after_id):
            place = ("after", after_id)
        else:
            end_id = self._spine_end()
            end = self.nodes[end_id]
            if end["kind"] == "branch":
                place = ("before", end_id)
            elif self.is_spine_common(end_id):
                place = ("after", end_id)
            else:
                raise GraphError("主链末端不是共通剧情，不能直接接上")
        node_id = self._new_node("beat", "common", title, content, source_id=source_id)
        if place[0] == "after":
            self._splice_after(place[1], node_id, "spine")
        else:
            self._splice_before(place[1], node_id, "spine")
        self.cursor_id = node_id
        return node_id

    def add_side(
        self,
        anchor_id: int,
        role: str,
        title: str,
        content: str,
        condition: str = "",
        source_id: Optional[int] = None,
    ) -> int:
        if role not in ("append", "variant"):
            raise GraphError("这里只能新建追加或差分")
        if not self.is_spine_common(anchor_id):
            raise GraphError("追加和差分只能挂在主链的共通剧情上")
        variant_of = anchor_id if role == "variant" else None
        node_id = self._new_node(
            "beat", role, title, content,
            condition=condition, variant_of=variant_of, source_id=source_id,
        )
        self._link(anchor_id, node_id, "insert", "")
        self.cursor_id = node_id
        return node_id

    def add_after(self, node_id: int, title: str, content: str, source_id: Optional[int] = None) -> int:
        node = self._node(node_id)
        if node["kind"] != "beat":
            raise GraphError("分支点上请新建分支，不能直接接剧情")
        incoming = self._incoming(node_id)
        if incoming is None or incoming["kind"] == "spine":
            return self.add_common(title, content, after_id=node_id, source_id=source_id)
        if incoming["kind"] == "insert":
            role = node["role"] if node["role"] in ("append", "variant") else "append"
            variant_of = node.get("variant_of") if role == "variant" else None
            new_id = self._new_node("beat", role, title, content, variant_of=variant_of, source_id=source_id)
            self._splice_after(node_id, new_id, "insert")
        elif incoming["kind"] == "branch":
            new_id = self._new_node("beat", "route", title, content, source_id=source_id)
            self._splice_after(node_id, new_id, "branch")
        else:
            raise GraphError("不能接在这个段落后面")
        self.cursor_id = new_id
        return new_id

    def append_after_cursor(self, title: str, content: str, source_id: Optional[int] = None) -> int:
        """空树时成为第一段。已有剧情时只接在总结时点后面，并把时点移到新段落。"""
        if not self.nodes or not self.cursor_id or self.cursor_id not in self.nodes:
            if not self.nodes:
                return self.add_common(title, content, source_id=source_id)
            raise GraphError("请先设置总结时点。勾选的章节会按列表顺序接到时点后面。")
        if self.nodes[self.cursor_id]["kind"] != "beat":
            raise GraphError("总结时点要放在剧情上")
        return self.add_after(self.cursor_id, title, content, source_id=source_id)

    def add_branch(self, anchor_id: Optional[int], label: str, content: str, source_id: Optional[int] = None) -> int:
        title = (label or "").strip()
        if not title:
            raise GraphError("请填写子线路名称")
        if not self.nodes:
            raise GraphError("主链还没有剧情，不能建分支")
        if anchor_id and anchor_id in self.nodes and self.nodes[anchor_id]["kind"] == "beat":
            tail_id = self._detach_successor(anchor_id)
            branch_id = self._new_node("branch", "common")
            self._link(anchor_id, branch_id, self._chain_kind(anchor_id), "")
            beat_id = self._new_node("beat", "route", title, content, source_id=source_id)
            self._link(branch_id, beat_id, "branch", title)
            if tail_id:
                self._link(beat_id, tail_id, "branch", "")
                self._convert_tail(tail_id)
            self.cursor_id = beat_id
            return beat_id
        target = self._branch_target(anchor_id) if anchor_id else None
        if target is None:
            end_id = self._spine_end()
            target = self.nodes[end_id]
            if target["kind"] != "branch":
                branch_id = self._new_node("branch", "common")
                self._link(end_id, branch_id, "spine", "")
                target = self.nodes[branch_id]
        if target["kind"] != "branch":
            branch_id = self._new_node("branch", "common")
            self._link(target["id"], branch_id, self._chain_kind(target["id"]), "")
            target = self.nodes[branch_id]
        beat_id = self._new_node("beat", "route", title, content, source_id=source_id)
        self._link(target["id"], beat_id, "branch", title)
        self.cursor_id = beat_id
        return beat_id

    def update_beat(self, node_id: int, title: str, content: str, condition: Optional[str] = None):
        node = self._node(node_id)
        if node["kind"] != "beat":
            return
        old_title = node.get("title") or ""
        node["title"] = title
        node["content"] = content
        if node["role"] in ("append", "variant") and condition is not None:
            node["condition_label"] = condition
        incoming = self._incoming(node_id)
        if (
            node["role"] == "route"
            and incoming
            and incoming["kind"] == "branch"
            and self.nodes.get(incoming["from_node_id"], {}).get("kind") == "branch"
        ):
            if incoming.get("label") == old_title or not (incoming.get("label") or "").strip():
                incoming["label"] = title

    def set_cursor(self, node_id: int):
        node = self._node(node_id)
        if node["kind"] != "beat":
            raise GraphError("总结时点要放在剧情上")
        self.cursor_id = node_id

    def delete(self, node_id: int):
        node = self._node(node_id)
        outs = [edge for edge in self.edges if edge["from_node_id"] == node_id]
        linked = [edge for edge in outs if edge.get("to_node_id")]
        if len(linked) > 1:
            raise GraphError("请先删除旁边和后面的剧情")
        incoming = self._incoming(node_id)
        if len(linked) == 1:
            outgoing = linked[0]
            same_kind = bool(incoming and incoming["kind"] == outgoing["kind"])
            if node["kind"] == "branch" and not same_kind:
                self._dissolve_branch(node_id, incoming, outgoing)
            elif not same_kind:
                raise GraphError("请先删除旁边和后面的剧情")
            else:
                incoming["to_node_id"] = outgoing["to_node_id"]
                self.edges = [edge for edge in self.edges if edge["id"] != outgoing["id"]]
                self.nodes.pop(node_id, None)
        elif incoming and incoming["kind"] == "branch" and node["kind"] == "beat":
            self.edges = [
                edge for edge in self.edges
                if edge["id"] != incoming["id"] and edge["from_node_id"] != node_id
            ]
            self.nodes.pop(node_id, None)
        else:
            self.edges = [
                edge for edge in self.edges
                if edge["from_node_id"] != node_id and edge.get("to_node_id") != node_id
            ]
            self.nodes.pop(node_id, None)
        if self.cursor_id == node_id:
            self.cursor_id = None

    def is_spine_common(self, node_id: int) -> bool:
        node = self.nodes.get(node_id)
        if not node or node["kind"] != "beat" or node.get("role") != "common":
            return False
        incoming = self._incoming(node_id)
        return incoming is None or incoming["kind"] == "spine"

    def used_summary_ids(self) -> set:
        return {node["source_summary_id"] for node in self.nodes.values() if node.get("source_summary_id")}

    def _node(self, node_id: int) -> dict:
        node = self.nodes.get(node_id)
        if not node:
            raise GraphError("这个段落不在当前剧情树上")
        return node

    def _new_node(
        self,
        kind: str,
        role: str,
        title: str = "",
        content: str = "",
        condition: str = "",
        variant_of: Optional[int] = None,
        source_id: Optional[int] = None,
    ) -> int:
        node_id = self._new_id()
        self.nodes[node_id] = {
            "id": node_id,
            "kind": kind,
            "role": role,
            "title": title or "",
            "content": content or "",
            "condition_label": condition or "",
            "variant_of": variant_of,
            "source_summary_id": source_id,
        }
        return node_id

    def _link(self, source: int, target: int, kind: str, label: str, order: Optional[int] = None) -> int:
        if order is None:
            order = self._next_order(source, kind)
        edge_id = self._new_id()
        self.edges.append({
            "id": edge_id,
            "from_node_id": source,
            "to_node_id": target,
            "kind": kind,
            "label": label or "",
            "sort_order": order,
        })
        return edge_id

    def _splice_after(self, anchor_id: int, new_id: int, kind: str):
        outs = [
            edge for edge in self.edges
            if edge["from_node_id"] == anchor_id and edge["kind"] == kind and edge.get("to_node_id")
        ]
        if len(outs) > 1:
            raise GraphError("这段后面有多条后续，请先整理")
        if not outs:
            self._link(anchor_id, new_id, kind, "")
            return
        edge = outs[0]
        old_to = edge["to_node_id"]
        edge["to_node_id"] = new_id
        self._link(new_id, old_to, kind, "")

    def _splice_before(self, target_id: int, new_id: int, kind: str):
        incoming = self._incoming(target_id)
        if not incoming or incoming["kind"] != kind:
            raise GraphError("不能插在这个位置")
        source = incoming["from_node_id"]
        incoming["to_node_id"] = new_id
        self._link(new_id, target_id, kind, "")
        if source is None:
            raise GraphError("不能插在这个位置")

    def _spine_end(self) -> int:
        if not self.nodes:
            raise GraphError("主链还没有剧情")
        pointed = {edge["to_node_id"] for edge in self.edges if edge.get("to_node_id")}
        roots = [node_id for node_id in self.nodes if node_id not in pointed]
        if len(roots) != 1:
            raise GraphError("剧情树结构不完整")
        current = roots[0]
        seen = set()
        while current not in seen:
            seen.add(current)
            nxt = self._first(current, "spine")
            if not nxt:
                return current
            current = nxt
        raise GraphError("主链出现了循环")

    def _dissolve_branch(self, node_id: int, incoming: Optional[dict], outgoing: dict):
        child_id = outgoing["to_node_id"]
        if incoming:
            incoming["to_node_id"] = child_id
            new_kind = incoming["kind"]
            parent = self.nodes.get(incoming["from_node_id"])
        else:
            new_kind = "spine"
            parent = None
        if outgoing["kind"] != new_kind:
            if new_kind == "spine":
                new_role = "common"
            elif new_kind == "insert":
                new_role = parent.get("role") if parent and parent.get("role") in ("append", "variant") else "append"
            else:
                new_role = "route"
            self._fold_chain(child_id, outgoing["kind"], new_kind, new_role)
        self.edges = [edge for edge in self.edges if edge["from_node_id"] != node_id]
        self.nodes.pop(node_id, None)

    def _fold_chain(self, start_id: int, old_kind: str, new_kind: str, new_role: str):
        current = start_id
        seen = set()
        while current and current not in seen:
            seen.add(current)
            node = self.nodes.get(current)
            if not node or node["kind"] == "branch":
                return
            if node.get("role") == "route":
                node["role"] = new_role
            cont = [
                edge for edge in self.edges
                if edge["from_node_id"] == current and edge["kind"] == old_kind and edge.get("to_node_id")
            ]
            if len(cont) > 1:
                raise GraphError("这段后面有多条后续，请先整理")
            if not cont:
                return
            cont.sort(key=lambda edge: (int(edge.get("sort_order") or 0), int(edge["id"])))
            cont[0]["kind"] = new_kind
            nxt = self.nodes.get(cont[0]["to_node_id"])
            if not nxt or nxt["kind"] == "branch":
                return
            current = nxt["id"]

    def continues_after(self, node_id: Optional[int]) -> bool:
        if not node_id or node_id not in self.nodes or self.nodes[node_id]["kind"] != "beat":
            return False
        kind = self._chain_kind(node_id)
        return any(
            edge["from_node_id"] == node_id and edge["kind"] == kind and edge.get("to_node_id")
            for edge in self.edges
        )

    def _detach_successor(self, node_id: int) -> Optional[int]:
        kind = self._chain_kind(node_id)
        outs = [
            edge for edge in self.edges
            if edge["from_node_id"] == node_id and edge["kind"] == kind and edge.get("to_node_id")
        ]
        if len(outs) > 1:
            raise GraphError("这段后面有多条后续，请先整理")
        if not outs:
            return None
        edge = outs[0]
        tail_id = edge["to_node_id"]
        self.edges = [item for item in self.edges if item["id"] != edge["id"]]
        return tail_id

    def _convert_tail(self, start_id: int):
        current = start_id
        seen = set()
        while current and current not in seen:
            seen.add(current)
            node = self.nodes.get(current)
            if not node or node["kind"] == "branch":
                return
            if node.get("role") == "common":
                node["role"] = "route"
            spine = [
                edge for edge in self.edges
                if edge["from_node_id"] == current and edge["kind"] == "spine" and edge.get("to_node_id")
            ]
            if len(spine) > 1:
                raise GraphError("这段后面有多条后续，请先整理")
            if spine:
                spine[0]["kind"] = "branch"
                current = spine[0]["to_node_id"]
                continue
            nxt = [
                edge for edge in self.edges
                if edge["from_node_id"] == current and edge["kind"] == "branch" and edge.get("to_node_id")
            ]
            if len(nxt) == 1:
                current = nxt[0]["to_node_id"]
                continue
            return

    def _branch_target(self, anchor_id: int) -> Optional[dict]:
        anchor = self._node(anchor_id)
        if anchor["kind"] == "branch":
            return anchor
        incoming = self._incoming(anchor_id)
        if not incoming or incoming["kind"] == "spine":
            return None
        kind = incoming["kind"]
        current = anchor
        seen = set()
        while current and current["id"] not in seen:
            seen.add(current["id"])
            if current["kind"] == "branch":
                return current
            nxt = self._first(current["id"], kind)
            if not nxt:
                return current
            current = self.nodes.get(nxt)
        return None

    def _chain_kind(self, node_id: int) -> str:
        incoming = self._incoming(node_id)
        if incoming and incoming["kind"] in ("spine", "branch", "insert"):
            return incoming["kind"]
        if self.nodes[node_id].get("role") == "route":
            return "branch"
        return "spine"

    def _incoming(self, node_id: int) -> Optional[dict]:
        hits = [edge for edge in self.edges if edge.get("to_node_id") == node_id]
        if len(hits) > 1:
            raise GraphError("有段落被多处连接，当前版本不能处理")
        return hits[0] if hits else None

    def _first(self, node_id: int, kind: str) -> Optional[int]:
        found = [
            edge for edge in self.edges
            if edge["from_node_id"] == node_id and edge["kind"] == kind and edge.get("to_node_id")
        ]
        found.sort(key=lambda edge: (int(edge.get("sort_order") or 0), int(edge["id"])))
        return found[0]["to_node_id"] if found else None

    def _next_order(self, node_id: int, kind: str) -> int:
        orders = [
            int(edge.get("sort_order") or 0)
            for edge in self.edges
            if edge["from_node_id"] == node_id and edge["kind"] == kind
        ]
        return (max(orders) if orders else 0) + 1

    def _new_id(self) -> int:
        value = self._seq
        self._seq += 1
        return value
