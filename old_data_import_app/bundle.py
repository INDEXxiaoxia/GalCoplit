"""把旧库里的一个游戏，加上正在编辑的剧情树，收成 2.0 可导入的数据包。"""

import datetime
from typing import Any, Dict, List

from old_data_import_app.graph import StoryGraph
from old_data_import_app.legacy import game_routes


def build_bundle(data: Dict[str, Any], game_id: int, trees: List[StoryGraph]) -> Dict[str, Any]:
    game = next((row for row in data["games"] if row["id"] == game_id), None)
    if not game:
        raise ValueError("游戏不存在")
    now = datetime.datetime.now().isoformat(timespec="seconds")
    routes = game_routes(data, game_id)
    route_ids = {row["id"] for row in routes}
    sessions = [row for row in data["sessions"] if row.get("route_id") in route_ids]
    session_ids = {row["id"] for row in sessions}
    summaries = [
        row for row in data["summaries"]
        if row.get("route_id") in route_ids and row.get("session_id") in session_ids
    ]
    summary_ids = {row["id"] for row in summaries}
    key_info = []
    seen_keys = set()
    for row in data["key_info"]:
        if row["id"] in seen_keys:
            continue
        if row.get("game_id") == game_id or row.get("route_id") in route_ids:
            seen_keys.add(row["id"])
            item = dict(row)
            if item.get("summary_id") not in summary_ids:
                item["summary_id"] = None
            key_info.append(item)
    characters = []
    seen_chars = set()
    for row in data["characters"]:
        if row["id"] in seen_chars:
            continue
        if row.get("game_id") == game_id or row.get("route_id") in route_ids:
            seen_chars.add(row["id"])
            characters.append(row)
    exported_trees = []
    for index, tree in enumerate(trees, start=1):
        if tree.nodes:
            exported_trees.append(tree.export_tree(index, now))
    return {
        "format": "galcoplit-game",
        "version": 1,
        "game": {
            "name": game.get("name") or "未命名游戏",
            "notes": game.get("notes") or "",
            "prompt": game.get("prompt") or "",
            "sort_order": int(game.get("sort_order") or 0),
            "created_at": game.get("created_at") or now,
        },
        "routes": [
            {
                "id": row["id"],
                "name": row.get("name") or "未命名线路",
                "notes": row.get("notes") or "",
                "last_summary_id": row.get("last_summary_id"),
                "created_at": row.get("created_at") or now,
                "hidden": 0,
                "sort_order": int(row.get("sort_order") or 0),
            }
            for row in routes
        ],
        "sessions": sessions,
        "summaries": summaries,
        "key_info": key_info,
        "characters": characters,
        "trees": exported_trees,
    }
