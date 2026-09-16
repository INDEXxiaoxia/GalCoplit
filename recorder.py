import sqlite3
import datetime
import json
import re
from typing import Optional, List, Dict, Any


class Database:
    """管理游戏、线路、会话、剧情总结与关键信息的持久化"""

    def __init__(self, db_path: str = "records.db"):
        self.db_path = db_path
        self._init_db()

    # ------------------------------------------------------------------
    # 初始化与迁移
    # ------------------------------------------------------------------
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            self._migrate_legacy(conn)

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    notes TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    notes TEXT DEFAULT '',
                    last_summary_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (game_id) REFERENCES games(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_id INTEGER NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    duration_sec INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active',
                    FOREIGN KEY (route_id) REFERENCES routes(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_id INTEGER NOT NULL,
                    session_id INTEGER NOT NULL,
                    summary_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    raw_text TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (route_id) REFERENCES routes(id),
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
                """
            )
            # 旧库迁移：summaries 表补充 title 列
            try:
                conn.execute("ALTER TABLE summaries ADD COLUMN title TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            # 旧库迁移：games 表补充 prompt 列（追加提示词）
            try:
                conn.execute("ALTER TABLE games ADD COLUMN prompt TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            # 旧库迁移：games/routes/characters 表补充 sort_order 列（拖动排序）
            for table in ("games", "routes", "characters"):
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN sort_order INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS key_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_id INTEGER NOT NULL,
                    summary_id INTEGER,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL,
                    user_note TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (route_id) REFERENCES routes(id),
                    FOREIGN KEY (summary_id) REFERENCES summaries(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS characters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    info TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (route_id) REFERENCES routes(id)
                )
                """
            )
            conn.commit()

    def _migrate_legacy(self, conn: sqlite3.Connection):
        """如果存在旧版 sessions/summaries 表，则重命名保留，避免冲突"""
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sessions', 'summaries')")
        legacy_tables = {row[0] for row in cursor.fetchall()}

        # 检查是否是旧表结构：是否存在 route_id 列
        def is_new(table: str) -> bool:
            try:
                info = conn.execute(f"PRAGMA table_info({table})").fetchall()
                return any(col[1] == "route_id" for col in info)
            except Exception:
                return False

        for table in legacy_tables:
            if not is_new(table):
                new_name = f"legacy_{table}"
                try:
                    conn.execute(f"ALTER TABLE {table} RENAME TO {new_name}")
                except sqlite3.OperationalError:
                    pass
        conn.commit()

    # ------------------------------------------------------------------
    # 游戏
    # ------------------------------------------------------------------
    def list_games(self) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM games ORDER BY sort_order ASC, created_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_game(self, game_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
            return dict(row) if row else None

    def add_game(self, name: str, notes: str = "") -> int:
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO games (name, notes, created_at) VALUES (?, ?, ?)",
                (name, notes, now),
            )
            conn.commit()
            return cur.lastrowid

    def rename_game(self, game_id: int, name: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE games SET name=? WHERE id=?", (name, game_id))
            conn.commit()

    def update_game_prompt(self, game_id: int, prompt: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE games SET prompt=? WHERE id=?", (prompt, game_id))
            conn.commit()

    def delete_game(self, game_id: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM characters WHERE route_id IN (SELECT id FROM routes WHERE game_id=?)", (game_id,))
            conn.execute("DELETE FROM key_info WHERE route_id IN (SELECT id FROM routes WHERE game_id=?)", (game_id,))
            conn.execute("DELETE FROM summaries WHERE route_id IN (SELECT id FROM routes WHERE game_id=?)", (game_id,))
            conn.execute("DELETE FROM sessions WHERE route_id IN (SELECT id FROM routes WHERE game_id=?)", (game_id,))
            conn.execute("DELETE FROM routes WHERE game_id=?", (game_id,))
            conn.execute("DELETE FROM games WHERE id=?", (game_id,))
            conn.commit()

    # ------------------------------------------------------------------
    # 线路
    # ------------------------------------------------------------------
    def list_routes(self, game_id: int) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM routes WHERE game_id=? ORDER BY sort_order ASC, created_at DESC",
                (game_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_route(self, route_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
            return dict(row) if row else None

    def add_route(self, game_id: int, name: str, notes: str = "") -> int:
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO routes (game_id, name, notes, created_at) VALUES (?, ?, ?, ?)",
                (game_id, name, notes, now),
            )
            conn.commit()
            return cur.lastrowid

    def rename_route(self, route_id: int, name: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE routes SET name=? WHERE id=?", (name, route_id))
            conn.commit()

    def delete_route(self, route_id: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM characters WHERE route_id=?", (route_id,))
            conn.execute("DELETE FROM key_info WHERE route_id=?", (route_id,))
            conn.execute("DELETE FROM summaries WHERE route_id=?", (route_id,))
            conn.execute("DELETE FROM sessions WHERE route_id=?", (route_id,))
            conn.execute("DELETE FROM routes WHERE id=?", (route_id,))
            conn.commit()

    def update_route_last_summary(self, route_id: int, summary_id: Optional[int]):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE routes SET last_summary_id=? WHERE id=?", (summary_id, route_id))
            conn.commit()

    def get_last_summary_for_route(self, route_id: int) -> Optional[Dict[str, Any]]:
        route = self.get_route(route_id)
        if route and route.get("last_summary_id"):
            return self.get_summary(route["last_summary_id"])
        return None

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def start_session(self, route_id: int) -> int:
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO sessions (route_id, start_time, status) VALUES (?, ?, ?)",
                (route_id, now, "active"),
            )
            conn.commit()
            return cur.lastrowid

    def pause_session(self, session_id: int):
        end_dt = datetime.datetime.now()
        start_row = self._get_session_start(session_id)
        if start_row:
            start_dt = datetime.datetime.fromisoformat(start_row)
            duration = max(0, int((end_dt - start_dt).total_seconds()))
        else:
            duration = 0
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET end_time=?, duration_sec=?, status='paused' WHERE id=?",
                (end_dt.isoformat(), duration, session_id),
            )
            conn.commit()

    def _get_session_start(self, session_id: int) -> Optional[str]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT start_time FROM sessions WHERE id=?", (session_id,)).fetchone()
            return row[0] if row else None

    def list_sessions(self, route_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM sessions WHERE route_id=? ORDER BY start_time DESC LIMIT ?",
                (route_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def fix_active_session(self, session_id: int, duration: int):
        """异常退出后修复会话状态"""
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET end_time=?, duration_sec=?, status='paused' WHERE id=?",
                (now, max(0, duration), session_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # 剧情总结
    # ------------------------------------------------------------------
    def add_summary(
        self,
        route_id: int,
        session_id: int,
        summary_type: str,
        content: str,
        raw_text: str = "",
        title: str = "",
    ) -> int:
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO summaries (route_id, session_id, summary_type, content, raw_text, title, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (route_id, session_id, summary_type, content, raw_text, title, now),
            )
            summary_id = cur.lastrowid
            conn.execute("UPDATE routes SET last_summary_id=? WHERE id=?", (summary_id, route_id))
            conn.commit()
            return summary_id

    def get_summary(self, summary_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM summaries WHERE id=?", (summary_id,)).fetchone()
            return dict(row) if row else None

    def update_summary(self, summary_id: int, content: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE summaries SET content=? WHERE id=?", (content, summary_id))
            conn.commit()

    def update_summary_title(self, summary_id: int, title: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE summaries SET title=? WHERE id=?", (title, summary_id))
            conn.commit()

    def list_summaries(self, route_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM summaries WHERE route_id=? ORDER BY created_at ASC LIMIT ?",
                (route_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def search_summaries(self, route_id: int, keyword: str) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM summaries WHERE route_id=? AND content LIKE ? ORDER BY created_at DESC",
                (route_id, f"%{keyword}%"),
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 关键信息
    # ------------------------------------------------------------------
    def add_key_info(
        self,
        route_id: int,
        content: str,
        category: str,
        user_note: str = "",
        summary_id: Optional[int] = None,
    ) -> int:
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO key_info (route_id, summary_id, content, category, user_note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (route_id, summary_id, content, category, user_note, now),
            )
            conn.commit()
            return cur.lastrowid

    def list_key_info(self, route_id: int, category: Optional[str] = None) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if category:
                rows = conn.execute(
                    "SELECT * FROM key_info WHERE route_id=? AND category=? ORDER BY created_at ASC",
                    (route_id, category),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM key_info WHERE route_id=? ORDER BY created_at ASC",
                    (route_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    def update_key_info(self, key_info_id: int, content: str, category: str, user_note: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE key_info SET content=?, category=?, user_note=? WHERE id=?",
                (content, category, user_note, key_info_id),
            )
            conn.commit()

    def delete_key_info(self, key_info_id: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM key_info WHERE id=?", (key_info_id,))
            conn.commit()

    # ------------------------------------------------------------------
    # 角色信息
    # ------------------------------------------------------------------
    def add_or_update_character(self, route_id: int, name: str, info: str = "") -> int:
        def _base_name(n: str) -> str:
            return re.split(r"[（(]", n)[0].strip()

        now = datetime.datetime.now().isoformat()
        new_base = _base_name(name)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, name, info FROM characters WHERE route_id=?",
                (route_id,),
            ).fetchall()
            for char_id, existing_name, existing_info in rows:
                if _base_name(existing_name) == new_base:
                    updated_name = existing_name
                    if existing_name != name and name not in existing_name:
                        updated_name = name
                    new_info = f"{existing_info}\n{info}".strip() if info else existing_info
                    conn.execute(
                        "UPDATE characters SET name=?, info=?, updated_at=? WHERE id=?",
                        (updated_name, new_info, now, char_id),
                    )
                    conn.commit()
                    return char_id
            cur = conn.execute(
                "INSERT INTO characters (route_id, name, info, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (route_id, name, info, now, now),
            )
            conn.commit()
            return cur.lastrowid

    def list_characters(self, route_id: int) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM characters WHERE route_id=? ORDER BY sort_order ASC, updated_at DESC",
                (route_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_character(self, char_id: int, name: str, info: str):
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE characters SET name=?, info=?, updated_at=? WHERE id=?",
                (name, info, now, char_id),
            )
            conn.commit()

    def delete_character(self, char_id: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM characters WHERE id=?", (char_id,))
            conn.commit()

    # ------------------------------------------------------------------
    # 拖动排序
    # ------------------------------------------------------------------
    def _set_order(self, table: str, ids: List[int]):
        with sqlite3.connect(self.db_path) as conn:
            for index, item_id in enumerate(ids, start=1):
                conn.execute(f"UPDATE {table} SET sort_order=? WHERE id=?", (index, item_id))
            conn.commit()

    def set_game_order(self, ids: List[int]):
        self._set_order("games", ids)

    def set_route_order(self, ids: List[int]):
        self._set_order("routes", ids)

    def set_character_order(self, ids: List[int]):
        self._set_order("characters", ids)

    # ------------------------------------------------------------------
    # 时长统计
    # ------------------------------------------------------------------
    def today_seconds(self, route_id: Optional[int] = None) -> int:
        today = datetime.date.today().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            if route_id is not None:
                row = conn.execute(
                    "SELECT COALESCE(SUM(duration_sec), 0) FROM sessions WHERE route_id=? AND date(start_time)=?",
                    (route_id, today),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(duration_sec), 0) FROM sessions WHERE date(start_time)=?",
                    (today,),
                ).fetchone()
            return int(row[0]) if row and row[0] else 0
