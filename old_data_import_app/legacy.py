"""读取 1.x 的 records 数据库。读完即关闭，避免占着文件。"""

import sqlite3
from typing import Any, Dict, List, Optional


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def load_legacy(path: str) -> Dict[str, Any]:
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"games", "routes", "sessions", "summaries"}
        missing = required - tables
        if missing:
            names = "、".join(sorted(missing))
            raise ValueError(f"这不是 1.x 的游戏数据库，缺少：{names}")
        games = _rows(conn, "SELECT * FROM games ORDER BY sort_order, id")
        routes = _rows(conn, "SELECT * FROM routes ORDER BY sort_order, id")
        sessions = _rows(conn, "SELECT * FROM sessions ORDER BY id")
        summaries = _rows(conn, "SELECT * FROM summaries ORDER BY sort_order, id")
        key_info = _rows(conn, "SELECT * FROM key_info ORDER BY id") if "key_info" in tables else []
        characters = _rows(conn, "SELECT * FROM characters ORDER BY sort_order, id") if "characters" in tables else []
    return {
        "path": path,
        "games": games,
        "routes": routes,
        "sessions": sessions,
        "summaries": summaries,
        "key_info": key_info,
        "characters": characters,
    }


def game_routes(data: Dict[str, Any], game_id: int) -> List[Dict[str, Any]]:
    return [row for row in data["routes"] if row.get("game_id") == game_id]


def route_summaries(data: Dict[str, Any], route_id: int) -> List[Dict[str, Any]]:
    rows = [row for row in data["summaries"] if row.get("route_id") == route_id]
    rows.sort(key=lambda row: (int(row.get("sort_order") or 0), int(row["id"])))
    return rows


def summary_by_id(data: Dict[str, Any], summary_id: int) -> Optional[Dict[str, Any]]:
    for row in data["summaries"]:
        if row["id"] == summary_id:
            return row
    return None
