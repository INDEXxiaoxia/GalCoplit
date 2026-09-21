import sqlite3
import datetime
import json
import re
from typing import Optional, List, Dict, Any


def conflict_mark(route_name: str, fact: str) -> str:
    """设定冲突标注：线路名线中原设定。线路名已以「线」结尾时不再重复加「线」。"""
    name = (route_name or "").strip() or "其他"
    fact = re.sub(r"\s+", " ", (fact or "").strip())
    if not fact:
        return ""
    if name.endswith("线"):
        return f"{name}中{fact}"
    return f"{name}线中{fact}"


def _base_name(name: str) -> str:
    return re.split(r"[（(]", name or "")[0].strip()


class Database:
    """管理游戏、线路、会话、剧情总结与关键信息的持久化。

    剧情总结仍按线路保存。剧情线索、世界观设定、角色信息按游戏共享。
    """

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
                    title TEXT DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 0,
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
            self._migrate_shared_knowledge(conn)
            self._migrate_summary_prompts(conn)
            self._migrate_summary_order(conn)
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

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set:
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.OperationalError:
            return set()

    def _migrate_shared_knowledge(self, conn: sqlite3.Connection):
        """旧库：关键信息/角色仍挂在线路上。补 game_id 后按游戏共享，不丢原有内容。"""
        for table in ("key_info", "characters"):
            if "id" not in self._table_columns(conn, table):
                continue
            if "game_id" not in self._table_columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN game_id INTEGER")
        if "sort_order" not in self._table_columns(conn, "characters"):
            conn.execute("ALTER TABLE characters ADD COLUMN sort_order INTEGER DEFAULT 0")
        conn.execute(
            """
            UPDATE key_info
            SET game_id = (SELECT game_id FROM routes WHERE routes.id = key_info.route_id)
            WHERE game_id IS NULL
            """
        )
        conn.execute(
            """
            UPDATE characters
            SET game_id = (SELECT game_id FROM routes WHERE routes.id = characters.route_id)
            WHERE game_id IS NULL
            """
        )
        self._dedupe_shared_key_info(conn)
        self._merge_shared_characters(conn)

    def _dedupe_shared_key_info(self, conn: sqlite3.Connection):
        """同一游戏里完全相同的线索/设定只留最早一条。"""
        rows = conn.execute(
            """
            SELECT id, game_id, category, content
            FROM key_info
            WHERE game_id IS NOT NULL
            ORDER BY id ASC
            """
        ).fetchall()
        seen = set()
        for item_id, game_id, category, content in rows:
            key = (game_id, category or "", (content or "").strip())
            if key in seen:
                conn.execute("DELETE FROM key_info WHERE id=?", (item_id,))
            else:
                seen.add(key)

    def _merge_shared_characters(self, conn: sqlite3.Connection):
        """同一游戏、同一基础名的角色合并为一条；不同描述按来源线路打冲突标注。"""
        rows = conn.execute(
            """
            SELECT c.id, c.game_id, c.name, c.info, COALESCE(r.name, '')
            FROM characters c
            LEFT JOIN routes r ON r.id = c.route_id
            WHERE c.game_id IS NOT NULL
            ORDER BY c.id ASC
            """
        ).fetchall()
        groups: Dict[tuple, list] = {}
        for row in rows:
            groups.setdefault((row[1], _base_name(row[2])), []).append(row)
        for items in groups.values():
            if len(items) < 2 or not items[0][0]:
                continue
            keep_id, _game_id, keep_name, keep_info, _keep_route = items[0]
            merged_name = keep_name or ""
            merged_info = keep_info or ""
            for _cid, _gid, name, info, route_name in items[1:]:
                other_name = name or ""
                if len(other_name) > len(merged_name) and _base_name(other_name) == _base_name(merged_name):
                    merged_name = other_name
                other_info = (info or "").strip()
                if other_info and other_info not in merged_info:
                    mark = conflict_mark(route_name, other_info)
                    merged_info = f"{merged_info}\n{mark}".strip() if merged_info else mark
                conn.execute("DELETE FROM characters WHERE id=?", (_cid,))
            if merged_name != (keep_name or "") or merged_info != (keep_info or ""):
                conn.execute(
                    "UPDATE characters SET name=?, info=? WHERE id=?",
                    (merged_name, merged_info, keep_id),
                )

    def _migrate_summary_prompts(self, conn: sqlite3.Connection):
        """旧库没有提示词表时补上，并写入预置的详细/简略两种系统提示词。"""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS summary_prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                system_prompt TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                selected INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                builtin_key TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        alters = {
            "title": "TEXT DEFAULT ''",
            "system_prompt": "TEXT DEFAULT ''",
            "enabled": "INTEGER DEFAULT 1",
            "selected": "INTEGER DEFAULT 0",
            "sort_order": "INTEGER DEFAULT 0",
            "builtin_key": "TEXT DEFAULT ''",
            "created_at": "TEXT DEFAULT ''",
            "updated_at": "TEXT DEFAULT ''",
        }
        cols = self._table_columns(conn, "summary_prompts")
        for name, ddl in alters.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE summary_prompts ADD COLUMN {name} {ddl}")
        count = conn.execute("SELECT COUNT(*) FROM summary_prompts").fetchone()[0]
        if count:
            return
        from llm_client import BUILTIN_SUMMARY_PROMPTS

        now = datetime.datetime.now().isoformat()
        for index, item in enumerate(BUILTIN_SUMMARY_PROMPTS, start=1):
            conn.execute(
                """
                INSERT INTO summary_prompts
                    (title, system_prompt, enabled, selected, sort_order, builtin_key, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    item["title"],
                    item["system_prompt"],
                    int(item["selected"]),
                    index,
                    item["builtin_key"],
                    now,
                    now,
                ),
            )

    def _migrate_summary_order(self, conn: sqlite3.Connection):
        """旧库补上剧情顺序号，并按原来的时间顺序回填，避免 0 和重号。"""
        if "sort_order" not in self._table_columns(conn, "summaries"):
            conn.execute("ALTER TABLE summaries ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        route_ids = [
            row[0] for row in conn.execute("SELECT DISTINCT route_id FROM summaries").fetchall()
        ]
        for route_id in route_ids:
            rows = conn.execute(
                """
                SELECT id, COALESCE(sort_order, 0)
                FROM summaries
                WHERE route_id=?
                ORDER BY created_at ASC, id ASC
                """,
                (route_id,),
            ).fetchall()
            if not rows:
                continue
            if all(int(order or 0) <= 0 for _sid, order in rows):
                for index, (summary_id, _order) in enumerate(rows, start=1):
                    conn.execute(
                        "UPDATE summaries SET sort_order=? WHERE id=?",
                        (index, summary_id),
                    )
                continue
            used = {int(order) for _sid, order in rows if int(order or 0) > 0}
            next_order = (max(used) + 1) if used else 1
            for summary_id, order in rows:
                if int(order or 0) > 0:
                    continue
                while next_order in used:
                    next_order += 1
                conn.execute(
                    "UPDATE summaries SET sort_order=? WHERE id=?",
                    (next_order, summary_id),
                )
                used.add(next_order)
                next_order += 1

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
            conn.execute(
                """
                DELETE FROM characters
                WHERE game_id=? OR route_id IN (SELECT id FROM routes WHERE game_id=?)
                """,
                (game_id, game_id),
            )
            conn.execute(
                """
                DELETE FROM key_info
                WHERE game_id=? OR route_id IN (SELECT id FROM routes WHERE game_id=?)
                """,
                (game_id, game_id),
            )
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
        """删除线路及其剧情总结。线索、世界观、角色属于游戏，不随单条线路删除。"""
        with sqlite3.connect(self.db_path) as conn:
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
        created_at: str = "",
        insert_at: Optional[int] = None,
    ) -> int:
        stamp = (created_at or "").strip() or datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            if insert_at is None or int(insert_at) <= 0:
                row = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) FROM summaries WHERE route_id=?",
                    (route_id,),
                ).fetchone()
                order = int(row[0] or 0) + 1
            else:
                order = int(insert_at)
                # 从大到小加 1，避免中途撞上还没挪开的号
                shifting = conn.execute(
                    """
                    SELECT id FROM summaries
                    WHERE route_id=? AND sort_order>=?
                    ORDER BY sort_order DESC, id DESC
                    """,
                    (route_id, order),
                ).fetchall()
                for (summary_id,) in shifting:
                    conn.execute(
                        "UPDATE summaries SET sort_order = sort_order + 1 WHERE id=?",
                        (summary_id,),
                    )
            cur = conn.execute(
                """
                INSERT INTO summaries
                    (route_id, session_id, summary_type, content, raw_text, title, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (route_id, session_id, summary_type, content, raw_text, title, order, stamp),
            )
            summary_id = cur.lastrowid
            self._set_last_summary_id(conn, route_id)
            conn.commit()
            return summary_id

    def _set_last_summary_id(self, conn: sqlite3.Connection, route_id: int):
        """上一章按剧情顺序取最后一段，不按写入先后。"""
        last = conn.execute(
            """
            SELECT id FROM summaries
            WHERE route_id=?
            ORDER BY sort_order DESC, id DESC
            LIMIT 1
            """,
            (route_id,),
        ).fetchone()
        conn.execute(
            "UPDATE routes SET last_summary_id=? WHERE id=?",
            (last[0] if last else None, route_id),
        )

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

    def add_manual_chapter(self, route_id: int, title: str, content: str = "") -> int:
        """手动追加一段剧情。不计入游玩时长。线路还没有总结时，这段就是第一章。"""
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (route_id, start_time, end_time, duration_sec, status)
                VALUES (?, ?, ?, 0, 'paused')
                """,
                (route_id, now, now),
            )
            session_id = cur.lastrowid
            conn.commit()
        return self.add_summary(route_id, session_id, "manual", content or "", "", title or "")

    def delete_summary(self, summary_id: int):
        """删除一段剧情总结。共享的线索/角色不删，只解开和这段的关联。"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT route_id FROM summaries WHERE id=?", (summary_id,)
            ).fetchone()
            if not row:
                return
            route_id = row[0]
            conn.execute(
                "UPDATE key_info SET summary_id=NULL WHERE summary_id=?", (summary_id,)
            )
            conn.execute("DELETE FROM summaries WHERE id=?", (summary_id,))
            self._set_last_summary_id(conn, route_id)
            conn.commit()

    def list_summaries(self, route_id: int, limit: int = 999) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM summaries WHERE route_id=? ORDER BY sort_order ASC, id ASC LIMIT ?",
                (route_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def min_summary_sort_order(self, summary_ids: List[int]) -> Optional[int]:
        """这些总结里当前最小的顺序号。已被删掉的 id 忽略。"""
        ids = [int(item) for item in summary_ids if item]
        if not ids:
            return None
        marks = ",".join("?" * len(ids))
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT MIN(sort_order) FROM summaries WHERE id IN ({marks}) AND sort_order>0",
                ids,
            ).fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0])

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
    def game_id_of_route(self, route_id: int) -> Optional[int]:
        route = self.get_route(route_id)
        if not route:
            return None
        game_id = route.get("game_id")
        return int(game_id) if game_id else None

    def add_key_info(
        self,
        route_id: int,
        content: str,
        category: str,
        user_note: str = "",
        summary_id: Optional[int] = None,
    ) -> int:
        content = (content or "").strip()
        now = datetime.datetime.now().isoformat()
        game_id = self.game_id_of_route(route_id)
        with sqlite3.connect(self.db_path) as conn:
            if game_id and content:
                existing = conn.execute(
                    """
                    SELECT id FROM key_info
                    WHERE game_id=? AND category=? AND content=?
                    ORDER BY id ASC LIMIT 1
                    """,
                    (game_id, category, content),
                ).fetchone()
                if existing:
                    return int(existing[0])
            cur = conn.execute(
                """
                INSERT INTO key_info (route_id, summary_id, content, category, user_note, created_at, game_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (route_id, summary_id, content, category, user_note, now, game_id),
            )
            conn.commit()
            return cur.lastrowid

    def list_key_info(self, route_id: int, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """返回该线路所属游戏的共享关键信息。找不到游戏时退回只查本线路。"""
        game_id = self.game_id_of_route(route_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if game_id:
                sql = (
                    "SELECT k.*, COALESCE(r.name, '') AS source_route "
                    "FROM key_info k LEFT JOIN routes r ON r.id = k.route_id "
                    "WHERE k.game_id=?"
                )
                params: list = [game_id]
            else:
                sql = (
                    "SELECT k.*, COALESCE(r.name, '') AS source_route "
                    "FROM key_info k LEFT JOIN routes r ON r.id = k.route_id "
                    "WHERE k.route_id=?"
                )
                params = [route_id]
            if category:
                sql += " AND k.category=?"
                params.append(category)
            sql += " ORDER BY k.created_at ASC"
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def get_key_info(self, key_info_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM key_info WHERE id=?", (key_info_id,)).fetchone()
            return dict(row) if row else None

    def key_info_in_game(self, key_info_id: int, game_id: Optional[int]) -> bool:
        if not game_id:
            return False
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM key_info WHERE id=? AND game_id=?",
                (key_info_id, game_id),
            ).fetchone()
            return row is not None

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
        now = datetime.datetime.now().isoformat()
        new_base = _base_name(name)
        game_id = self.game_id_of_route(route_id)
        with sqlite3.connect(self.db_path) as conn:
            if game_id:
                rows = conn.execute(
                    "SELECT id, name, info FROM characters WHERE game_id=?",
                    (game_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, name, info FROM characters WHERE route_id=?",
                    (route_id,),
                ).fetchall()
            for char_id, existing_name, existing_info in rows:
                if _base_name(existing_name) == new_base:
                    updated_name = existing_name
                    if existing_name != name and name not in existing_name:
                        updated_name = name
                    new_info = self._merge_character_info(existing_info or "", info or "")
                    conn.execute(
                        "UPDATE characters SET name=?, info=?, updated_at=? WHERE id=?",
                        (updated_name, new_info, now, char_id),
                    )
                    conn.commit()
                    return char_id
            cur = conn.execute(
                """
                INSERT INTO characters (route_id, name, info, created_at, updated_at, game_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (route_id, name, info, now, now, game_id),
            )
            conn.commit()
            return cur.lastrowid

    @staticmethod
    def _merge_character_info(existing_info: str, new_info: str) -> str:
        existing_info = (existing_info or "").strip()
        new_info = (new_info or "").strip()
        if not new_info or new_info in existing_info:
            return existing_info
        # 新文本已包含旧描述（通常带了冲突标注）时，用新文本替换，避免越拼越长
        if existing_info and existing_info in new_info:
            return new_info
        if not existing_info:
            return new_info
        return f"{existing_info}\n{new_info}".strip()

    def list_characters(self, route_id: int) -> List[Dict[str, Any]]:
        """返回该线路所属游戏的共享角色。找不到游戏时退回只查本线路。"""
        game_id = self.game_id_of_route(route_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if game_id:
                rows = conn.execute(
                    """
                    SELECT c.*, COALESCE(r.name, '') AS source_route
                    FROM characters c
                    LEFT JOIN routes r ON r.id = c.route_id
                    WHERE c.game_id=?
                    ORDER BY c.sort_order ASC, c.updated_at DESC
                    """,
                    (game_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT c.*, COALESCE(r.name, '') AS source_route
                    FROM characters c
                    LEFT JOIN routes r ON r.id = c.route_id
                    WHERE c.route_id=?
                    ORDER BY c.sort_order ASC, c.updated_at DESC
                    """,
                    (route_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    def get_character(self, char_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM characters WHERE id=?", (char_id,)).fetchone()
            return dict(row) if row else None

    def character_in_game(self, char_id: int, game_id: Optional[int]) -> bool:
        if not game_id:
            return False
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM characters WHERE id=? AND game_id=?",
                (char_id, game_id),
            ).fetchone()
            return row is not None

    def format_shared_context(self, route_id: int, limit: int = 7000) -> str:
        """给总结/关键信息提示词用的共享设定。剧情总结本身不包含在内。"""
        parts = []
        for item in self.list_key_info(route_id):
            source = (item.get("source_route") or "").strip() or "未知线路"
            note = (item.get("user_note") or "").strip()
            line = f"- id={item['id']} [{item.get('category') or '其他'}] {item.get('content') or ''}"
            if note:
                line += f"（备注：{note}）"
            line += f"（来源：{source}）"
            parts.append(line)
        for char in self.list_characters(route_id):
            source = (char.get("source_route") or "").strip() or "未知线路"
            info = re.sub(r"\s+", " ", (char.get("info") or "").strip())
            line = f"- id={char['id']} [角色信息] {char.get('name') or ''}"
            if info:
                line += f"：{info}"
            line += f"（来源：{source}）"
            parts.append(line)
        text = "\n".join(parts).strip()
        if len(text) > limit:
            text = text[:limit].rstrip() + "\n…（其余共享信息已省略）"
        return text

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
    # 剧情总结提示词
    # ------------------------------------------------------------------
    def list_summary_prompts(self) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM summary_prompts ORDER BY sort_order ASC, id ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_active_summary_prompt(self) -> Optional[Dict[str, Any]]:
        prompts = self.list_summary_prompts()
        if not prompts:
            return None
        for item in prompts:
            if item.get("selected") and item.get("enabled"):
                return item
        for item in prompts:
            if item.get("enabled"):
                return item
        return prompts[0]

    def add_summary_prompt(self, title: str, system_prompt: str, enabled: bool = True) -> int:
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM summary_prompts"
            ).fetchone()[0]
            selected = 0
            if not conn.execute("SELECT 1 FROM summary_prompts LIMIT 1").fetchone():
                selected = 1
                enabled = True
            cur = conn.execute(
                """
                INSERT INTO summary_prompts
                    (title, system_prompt, enabled, selected, sort_order, builtin_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, '', ?, ?)
                """,
                (title.strip(), system_prompt.strip(), 1 if enabled else 0, selected, order, now, now),
            )
            conn.commit()
            return cur.lastrowid

    def update_summary_prompt(self, prompt_id: int, title: str, system_prompt: str):
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE summary_prompts SET title=?, system_prompt=?, updated_at=? WHERE id=?",
                (title.strip(), system_prompt.strip(), now, prompt_id),
            )
            conn.commit()

    def set_summary_prompt_enabled(self, prompt_id: int, enabled: bool) -> str:
        """关闭时至少留一个启用项。若关掉的是当前项，改选另一个已启用的。"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT selected FROM summary_prompts WHERE id=?", (prompt_id,)
            ).fetchone()
            if not row:
                return "提示词不存在"
            if not enabled:
                others = conn.execute(
                    "SELECT COUNT(*) FROM summary_prompts WHERE enabled=1 AND id<>?",
                    (prompt_id,),
                ).fetchone()[0]
                if not others:
                    return "至少保留一个启用的提示词"
            now = datetime.datetime.now().isoformat()
            conn.execute(
                "UPDATE summary_prompts SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, now, prompt_id),
            )
            if not enabled and row[0]:
                nxt = conn.execute(
                    "SELECT id FROM summary_prompts WHERE enabled=1 AND id<>? ORDER BY sort_order ASC, id ASC LIMIT 1",
                    (prompt_id,),
                ).fetchone()
                conn.execute("UPDATE summary_prompts SET selected=0")
                if nxt:
                    conn.execute(
                        "UPDATE summary_prompts SET selected=1, updated_at=? WHERE id=?",
                        (now, nxt[0]),
                    )
            conn.commit()
        return ""

    def select_summary_prompt(self, prompt_id: int) -> str:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT enabled FROM summary_prompts WHERE id=?", (prompt_id,)
            ).fetchone()
            if not row:
                return "提示词不存在"
            if not row[0]:
                return "请先启用该提示词"
            now = datetime.datetime.now().isoformat()
            conn.execute("UPDATE summary_prompts SET selected=0")
            conn.execute(
                "UPDATE summary_prompts SET selected=1, updated_at=? WHERE id=?",
                (now, prompt_id),
            )
            conn.commit()
        return ""

    def delete_summary_prompt(self, prompt_id: int) -> str:
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM summary_prompts").fetchone()[0]
            if total <= 1:
                return "至少保留一个提示词，最后一个不能删除"
            row = conn.execute(
                "SELECT selected, enabled FROM summary_prompts WHERE id=?", (prompt_id,)
            ).fetchone()
            if not row:
                return "提示词不存在"
            conn.execute("DELETE FROM summary_prompts WHERE id=?", (prompt_id,))
            if row[0]:
                nxt = conn.execute(
                    """
                    SELECT id FROM summary_prompts
                    ORDER BY enabled DESC, sort_order ASC, id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if nxt:
                    conn.execute("UPDATE summary_prompts SET selected=0")
                    conn.execute(
                        "UPDATE summary_prompts SET selected=1, enabled=1 WHERE id=?",
                        (nxt[0],),
                    )
            conn.commit()
        return ""

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

    def set_summary_order(self, ids: List[int]):
        """按给定顺序重写同一线路的剧情序号，并把最后一段记为上一章。"""
        if not ids:
            return
        with sqlite3.connect(self.db_path) as conn:
            route_row = conn.execute(
                "SELECT route_id FROM summaries WHERE id=?", (ids[0],)
            ).fetchone()
            for index, item_id in enumerate(ids, start=1):
                conn.execute(
                    "UPDATE summaries SET sort_order=? WHERE id=?",
                    (index, item_id),
                )
            if route_row:
                self._set_last_summary_id(conn, route_row[0])
            conn.commit()

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
