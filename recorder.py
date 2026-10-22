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


class StoryTreeError(Exception):
    """剧情树操作无法完成时抛出，消息可直接展示给用户。"""


class Database:
    """管理游戏、线路、会话、剧情总结与关键信息的持久化。

    旧的剧情总结仍按线路保存。新的游玩记录写在剧情树上。
    剧情线索、世界观设定、角色信息按游戏共享。
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
            self._migrate_story_graph(conn)
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

    def _migrate_story_graph(self, conn: sqlite3.Connection):
        """剧情树、总结时点。锚点线路用 hidden 隐藏，不出现在旧线路列表里。"""
        if "hidden" not in self._table_columns(conn, "routes"):
            conn.execute("ALTER TABLE routes ADD COLUMN hidden INTEGER DEFAULT 0")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS story_trees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                prompt TEXT DEFAULT '',
                anchor_route_id INTEGER,
                sort_order INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (game_id) REFERENCES games(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS story_nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tree_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'common',
                title TEXT DEFAULT '',
                content TEXT DEFAULT '',
                raw_text TEXT DEFAULT '',
                variant_of INTEGER,
                condition_label TEXT DEFAULT '',
                include_in_context INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (tree_id) REFERENCES story_trees(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS story_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tree_id INTEGER NOT NULL,
                from_node_id INTEGER NOT NULL,
                to_node_id INTEGER,
                kind TEXT NOT NULL,
                label TEXT DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (tree_id) REFERENCES story_trees(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS story_cursors (
                tree_id INTEGER PRIMARY KEY,
                after_node_id INTEGER,
                via_edge_id INTEGER,
                FOREIGN KEY (tree_id) REFERENCES story_trees(id)
            )
            """
        )

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
            tree_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM story_trees WHERE game_id=?", (game_id,)
                ).fetchall()
            ]
            if tree_ids:
                marks = ",".join("?" * len(tree_ids))
                conn.execute(f"DELETE FROM story_cursors WHERE tree_id IN ({marks})", tree_ids)
                conn.execute(f"DELETE FROM story_edges WHERE tree_id IN ({marks})", tree_ids)
                conn.execute(f"DELETE FROM story_nodes WHERE tree_id IN ({marks})", tree_ids)
                conn.execute("DELETE FROM story_trees WHERE game_id=?", (game_id,))
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

    def export_game_bundle(self, game_id: int) -> Dict[str, Any]:
        """导出一份本软件可以再导入的游戏数据。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            game = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
            if not game:
                raise StoryTreeError("游戏不存在")
            routes = self._bundle_rows(
                conn, "SELECT * FROM routes WHERE game_id=? ORDER BY sort_order, id", (game_id,)
            )
            route_ids = [row["id"] for row in routes]
            sessions = self._bundle_rows_in(conn, "sessions", "route_id", route_ids)
            summaries = self._bundle_rows_in(conn, "summaries", "route_id", route_ids)
            key_info = self._bundle_rows(
                conn,
                """
                SELECT * FROM key_info
                WHERE game_id=? OR route_id IN (SELECT id FROM routes WHERE game_id=?)
                ORDER BY id
                """,
                (game_id, game_id),
            )
            characters = self._bundle_rows(
                conn,
                """
                SELECT * FROM characters
                WHERE game_id=? OR route_id IN (SELECT id FROM routes WHERE game_id=?)
                ORDER BY sort_order, id
                """,
                (game_id, game_id),
            )
            trees = self._bundle_rows(
                conn,
                "SELECT * FROM story_trees WHERE game_id=? ORDER BY sort_order, id",
                (game_id,),
            )
            for tree in trees:
                tree_id = tree["id"]
                tree["nodes"] = self._bundle_rows(
                    conn, "SELECT * FROM story_nodes WHERE tree_id=? ORDER BY id", (tree_id,)
                )
                tree["edges"] = self._bundle_rows(
                    conn, "SELECT * FROM story_edges WHERE tree_id=? ORDER BY id", (tree_id,)
                )
                cursor = conn.execute(
                    "SELECT after_node_id, via_edge_id FROM story_cursors WHERE tree_id=?",
                    (tree_id,),
                ).fetchone()
                tree["cursor"] = dict(cursor) if cursor else None
                tree.pop("game_id", None)
            return {
                "format": "galcoplit-game",
                "version": 1,
                "game": {key: game[key] for key in game.keys() if key != "id"},
                "routes": routes,
                "sessions": sessions,
                "summaries": summaries,
                "key_info": key_info,
                "characters": characters,
                "trees": trees,
            }

    def import_game_bundle(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """导入游戏数据，始终新建一个游戏，不覆盖现有内容。"""
        if not isinstance(data, dict) or data.get("format") != "galcoplit-game":
            raise StoryTreeError("这不是本软件导出的游戏数据")
        if data.get("version") != 1:
            raise StoryTreeError("无法识别这个版本的游戏数据")
        game = data.get("game") if isinstance(data.get("game"), dict) else None
        if not game or not str(game.get("name") or "").strip():
            raise StoryTreeError("游戏数据里没有名称")
        with sqlite3.connect(self.db_path) as conn:
            try:
                game_id, name = self._write_game_bundle(conn, data, game)
                conn.commit()
            except StoryTreeError:
                conn.rollback()
                raise
            except Exception as exc:
                conn.rollback()
                raise StoryTreeError(f"导入失败：{exc}") from exc
        return {"id": game_id, "name": name}

    def _bundle_rows(self, conn: sqlite3.Connection, sql: str, params: tuple) -> List[Dict[str, Any]]:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def _bundle_rows_in(
        self, conn: sqlite3.Connection, table: str, column: str, ids: List[int]
    ) -> List[Dict[str, Any]]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self._bundle_rows(
            conn, f"SELECT * FROM {table} WHERE {column} IN ({marks}) ORDER BY id", tuple(ids)
        )

    def _write_game_bundle(self, conn: sqlite3.Connection, data: dict, game: dict) -> tuple:
        name = self._unused_game_name(conn, str(game.get("name") or "").strip())
        now = datetime.datetime.now().isoformat()
        cur = conn.execute(
            """
            INSERT INTO games (name, notes, created_at, prompt, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                game.get("notes") or "",
                game.get("created_at") or now,
                game.get("prompt") or "",
                int(game.get("sort_order") or 0),
            ),
        )
        game_id = int(cur.lastrowid)
        route_map: Dict[int, int] = {}
        for route in data.get("routes") or []:
            if not isinstance(route, dict) or route.get("id") is None:
                continue
            cur = conn.execute(
                """
                INSERT INTO routes (game_id, name, notes, last_summary_id, created_at, hidden, sort_order)
                VALUES (?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    game_id,
                    route.get("name") or "未命名线路",
                    route.get("notes") or "",
                    route.get("created_at") or now,
                    int(route.get("hidden") or 0),
                    int(route.get("sort_order") or 0),
                ),
            )
            route_map[int(route["id"])] = int(cur.lastrowid)
        session_map: Dict[int, int] = {}
        for session in data.get("sessions") or []:
            if not isinstance(session, dict) or session.get("id") is None:
                continue
            route_id = route_map.get(int(session.get("route_id") or 0))
            if not route_id:
                continue
            cur = conn.execute(
                """
                INSERT INTO sessions (route_id, start_time, end_time, duration_sec, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    route_id,
                    session.get("start_time") or now,
                    session.get("end_time"),
                    int(session.get("duration_sec") or 0),
                    session.get("status") or "finished",
                ),
            )
            session_map[int(session["id"])] = int(cur.lastrowid)
        summary_map: Dict[int, int] = {}
        for summary in data.get("summaries") or []:
            if not isinstance(summary, dict) or summary.get("id") is None:
                continue
            route_id = route_map.get(int(summary.get("route_id") or 0))
            session_id = session_map.get(int(summary.get("session_id") or 0))
            if not route_id or not session_id:
                continue
            cur = conn.execute(
                """
                INSERT INTO summaries (
                    route_id, session_id, summary_type, content, raw_text,
                    title, sort_order, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route_id,
                    session_id,
                    summary.get("summary_type") or "manual",
                    summary.get("content") or "",
                    summary.get("raw_text") or "",
                    summary.get("title") or "",
                    int(summary.get("sort_order") or 0),
                    summary.get("created_at") or now,
                ),
            )
            summary_map[int(summary["id"])] = int(cur.lastrowid)
        for route in data.get("routes") or []:
            if not isinstance(route, dict) or not route.get("last_summary_id"):
                continue
            new_route = route_map.get(int(route.get("id") or 0))
            new_summary = summary_map.get(int(route["last_summary_id"]))
            if new_route and new_summary:
                conn.execute(
                    "UPDATE routes SET last_summary_id=? WHERE id=?",
                    (new_summary, new_route),
                )
        for item in data.get("key_info") or []:
            if not isinstance(item, dict):
                continue
            route_id = route_map.get(int(item.get("route_id") or 0))
            if not route_id:
                continue
            summary_id = summary_map.get(int(item.get("summary_id") or 0)) if item.get("summary_id") else None
            conn.execute(
                """
                INSERT INTO key_info (
                    route_id, summary_id, content, category, user_note, created_at, game_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route_id,
                    summary_id,
                    item.get("content") or "",
                    item.get("category") or "",
                    item.get("user_note") or "",
                    item.get("created_at") or now,
                    game_id,
                ),
            )
        for item in data.get("characters") or []:
            if not isinstance(item, dict):
                continue
            route_id = route_map.get(int(item.get("route_id") or 0))
            if not route_id:
                continue
            conn.execute(
                """
                INSERT INTO characters (
                    route_id, name, info, created_at, updated_at, game_id, sort_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    route_id,
                    item.get("name") or "未命名",
                    item.get("info") or "",
                    item.get("created_at") or now,
                    item.get("updated_at") or now,
                    game_id,
                    int(item.get("sort_order") or 0),
                ),
            )
        for tree in data.get("trees") or []:
            if not isinstance(tree, dict):
                continue
            anchor_id = route_map.get(int(tree.get("anchor_route_id") or 0))
            if not anchor_id:
                cur = conn.execute(
                    """
                    INSERT INTO routes (game_id, name, notes, created_at, hidden, sort_order)
                    VALUES (?, ?, '', ?, 1, 0)
                    """,
                    (game_id, tree.get("name") or "剧情树", now),
                )
                anchor_id = int(cur.lastrowid)
            cur = conn.execute(
                """
                INSERT INTO story_trees (
                    game_id, name, prompt, anchor_route_id, sort_order, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    game_id,
                    tree.get("name") or "剧情树",
                    tree.get("prompt") or "",
                    anchor_id,
                    int(tree.get("sort_order") or 0),
                    tree.get("created_at") or now,
                ),
            )
            tree_id = int(cur.lastrowid)
            node_map: Dict[int, int] = {}
            for node in tree.get("nodes") or []:
                if not isinstance(node, dict) or node.get("id") is None:
                    continue
                cur = conn.execute(
                    """
                    INSERT INTO story_nodes (
                        tree_id, kind, role, title, content, raw_text, variant_of,
                        condition_label, include_in_context, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        tree_id,
                        node.get("kind") or "beat",
                        node.get("role") or "common",
                        node.get("title") or "",
                        node.get("content") or "",
                        node.get("raw_text") or "",
                        node.get("condition_label") or "",
                        int(node.get("include_in_context") or 0),
                        node.get("created_at") or now,
                    ),
                )
                node_map[int(node["id"])] = int(cur.lastrowid)
            for node in tree.get("nodes") or []:
                if not isinstance(node, dict) or not node.get("variant_of"):
                    continue
                new_id = node_map.get(int(node.get("id") or 0))
                variant_of = node_map.get(int(node["variant_of"]))
                if new_id and variant_of:
                    conn.execute(
                        "UPDATE story_nodes SET variant_of=? WHERE id=?",
                        (variant_of, new_id),
                    )
            edge_map: Dict[int, int] = {}
            for edge in tree.get("edges") or []:
                if not isinstance(edge, dict) or edge.get("id") is None:
                    continue
                source = node_map.get(int(edge.get("from_node_id") or 0))
                if not source:
                    continue
                target = node_map.get(int(edge["to_node_id"])) if edge.get("to_node_id") else None
                cur = conn.execute(
                    """
                    INSERT INTO story_edges (
                        tree_id, from_node_id, to_node_id, kind, label, sort_order
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tree_id,
                        source,
                        target,
                        edge.get("kind") or "spine",
                        edge.get("label") or "",
                        int(edge.get("sort_order") or 0),
                    ),
                )
                edge_map[int(edge["id"])] = int(cur.lastrowid)
            cursor = tree.get("cursor") if isinstance(tree.get("cursor"), dict) else None
            if cursor and cursor.get("after_node_id"):
                after_id = node_map.get(int(cursor["after_node_id"]))
                via_id = edge_map.get(int(cursor["via_edge_id"])) if cursor.get("via_edge_id") else None
                if after_id:
                    conn.execute(
                        """
                        INSERT INTO story_cursors (tree_id, after_node_id, via_edge_id)
                        VALUES (?, ?, ?)
                        """,
                        (tree_id, after_id, via_id),
                    )
        return game_id, name

    def _unused_game_name(self, conn: sqlite3.Connection, name: str) -> str:
        taken = {row[0] for row in conn.execute("SELECT name FROM games").fetchall()}
        if name not in taken:
            return name
        index = 2
        while f"{name} ({index})" in taken:
            index += 1
        return f"{name} ({index})"

    # ------------------------------------------------------------------
    # 线路
    # ------------------------------------------------------------------
    def list_routes(self, game_id: int, include_hidden: bool = False) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM routes WHERE game_id=?"
            params: List[Any] = [game_id]
            if not include_hidden:
                sql += " AND COALESCE(hidden, 0)=0"
            sql += " ORDER BY sort_order ASC, created_at DESC"
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def get_route(self, route_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
            return dict(row) if row else None

    def add_route(self, game_id: int, name: str, notes: str = "", hidden: bool = False) -> int:
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO routes (game_id, name, notes, created_at, hidden)
                VALUES (?, ?, ?, ?, ?)
                """,
                (game_id, name, notes, now, 1 if hidden else 0),
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

    # ------------------------------------------------------------------
    # 剧情树
    # ------------------------------------------------------------------
    def list_story_trees(self, game_id: int) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM story_trees
                WHERE game_id=?
                ORDER BY sort_order ASC, id ASC
                """,
                (game_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_story_tree(self, tree_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM story_trees WHERE id=?", (tree_id,)).fetchone()
            return dict(row) if row else None

    def add_story_tree(self, game_id: int, name: str, prompt: str = "") -> int:
        title = (name or "").strip()
        if not title:
            raise StoryTreeError("请填写剧情树名称")
        if not self.get_game(game_id):
            raise StoryTreeError("游戏不存在")
        now = datetime.datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            route = conn.execute(
                """
                INSERT INTO routes (game_id, name, notes, created_at, hidden)
                VALUES (?, ?, '', ?, 1)
                """,
                (game_id, title, now),
            )
            order_row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM story_trees WHERE game_id=?",
                (game_id,),
            ).fetchone()
            order = int(order_row[0] or 0) + 1
            cur = conn.execute(
                """
                INSERT INTO story_trees
                    (game_id, name, prompt, anchor_route_id, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (game_id, title, (prompt or "").strip(), route.lastrowid, order, now),
            )
            conn.commit()
            return int(cur.lastrowid)

    def rename_story_tree(self, tree_id: int, name: str):
        title = (name or "").strip()
        if not title:
            raise StoryTreeError("请填写剧情树名称")
        tree = self.get_story_tree(tree_id)
        if not tree:
            raise StoryTreeError("剧情树不存在")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE story_trees SET name=? WHERE id=?", (title, tree_id))
            if tree.get("anchor_route_id"):
                conn.execute(
                    "UPDATE routes SET name=? WHERE id=?",
                    (title, tree["anchor_route_id"]),
                )
            conn.commit()

    def update_story_tree_prompt(self, tree_id: int, prompt: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE story_trees SET prompt=? WHERE id=?",
                ((prompt or "").strip(), tree_id),
            )
            conn.commit()

    def delete_story_tree(self, tree_id: int):
        tree = self.get_story_tree(tree_id)
        if not tree:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM story_cursors WHERE tree_id=?", (tree_id,))
            conn.execute("DELETE FROM story_edges WHERE tree_id=?", (tree_id,))
            conn.execute("DELETE FROM story_nodes WHERE tree_id=?", (tree_id,))
            conn.execute("DELETE FROM story_trees WHERE id=?", (tree_id,))
            conn.commit()
        if tree.get("anchor_route_id"):
            self.delete_route(tree["anchor_route_id"])

    def story_graph(self, tree_id: int) -> Dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            tree = conn.execute("SELECT * FROM story_trees WHERE id=?", (tree_id,)).fetchone()
            if not tree:
                return {"tree": None, "nodes": [], "edges": [], "cursor": None}
            nodes = conn.execute(
                "SELECT * FROM story_nodes WHERE tree_id=? ORDER BY id ASC",
                (tree_id,),
            ).fetchall()
            edges = conn.execute(
                """
                SELECT * FROM story_edges
                WHERE tree_id=?
                ORDER BY sort_order ASC, id ASC
                """,
                (tree_id,),
            ).fetchall()
            cursor = conn.execute(
                "SELECT * FROM story_cursors WHERE tree_id=?",
                (tree_id,),
            ).fetchone()
            return {
                "tree": dict(tree),
                "nodes": [dict(row) for row in nodes],
                "edges": [dict(row) for row in edges],
                "cursor": dict(cursor) if cursor else None,
            }

    def get_story_node(self, node_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM story_nodes WHERE id=?", (node_id,)).fetchone()
            return dict(row) if row else None

    def update_story_node(
        self,
        node_id: int,
        title: Optional[str] = None,
        content: Optional[str] = None,
        condition_label: Optional[str] = None,
    ):
        sets = []
        params: List[Any] = []
        if title is not None:
            sets.append("title=?")
            params.append(title)
        if content is not None:
            sets.append("content=?")
            params.append(content)
        if condition_label is not None:
            sets.append("condition_label=?")
            params.append(condition_label)
        if not sets:
            return
        params.append(node_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"UPDATE story_nodes SET {', '.join(sets)} WHERE id=?",
                params,
            )
            conn.commit()

    def story_can_record(self, tree_id: Optional[int]) -> bool:
        """空树可以写下第一段。已有剧情时必须有可用的总结时点。"""
        if not tree_id:
            return False
        graph = self.story_graph(tree_id)
        if not graph["tree"]:
            return False
        if not graph["nodes"]:
            return True
        cursor = graph["cursor"] or {}
        anchor = self._node_by_id(graph["nodes"], cursor.get("after_node_id"))
        if not anchor:
            return False
        try:
            self._placement(anchor, graph["edges"], cursor.get("via_edge_id"))
            return True
        except StoryTreeError:
            return False

    def story_cursor_label(self, tree_id: int) -> str:
        graph = self.story_graph(tree_id)
        if not graph["tree"]:
            return "未选择剧情树"
        if not graph["nodes"]:
            return "空树，下一次总结会写成第一段"
        cursor = graph["cursor"] or {}
        node = self._node_by_id(graph["nodes"], cursor.get("after_node_id"))
        if not node:
            return "未设置总结时点"
        if node["kind"] == "branch":
            label = self._edge_label(graph["edges"], cursor.get("via_edge_id"))
            return f"分支「{label or '未命名线路'}」之后"
        return (node.get("title") or "").strip() or "未命名"

    def story_route_label(self, tree_id: int) -> str:
        """时点所在线路枝的名称。还在主链上时用剧情树名。"""
        graph = self.story_graph(tree_id)
        tree = graph["tree"] or {}
        fallback = (tree.get("name") or "").strip()
        cursor = graph["cursor"] or {}
        anchor_id = cursor.get("after_node_id")
        if not anchor_id:
            return fallback
        node_map = {node["id"]: node for node in graph["nodes"]}
        path = self._path_to(anchor_id, graph["edges"])
        label = ""
        for index, node_id in enumerate(path):
            node = node_map.get(node_id)
            if node and node["kind"] == "branch":
                got = self._branch_label_on_path(node, path, index, graph["edges"], cursor)
                if got:
                    label = got
        return label or fallback

    def build_plot_context(self, tree_id: int, limit: int = 12000) -> str:
        """从树根走到总结时点，只带这条链路上已经写好的剧情。

        同一条差分、追加或线路上前后相接的节点都在这条路上，会按顺序计入。
        不在时点这条路上的其他差分、追加和线路不进入上下文。
        """
        graph = self.story_graph(tree_id)
        cursor = graph["cursor"] or {}
        anchor_id = cursor.get("after_node_id")
        if not anchor_id:
            return ""
        node_map = {node["id"]: node for node in graph["nodes"]}
        if anchor_id not in node_map:
            return ""
        edges = graph["edges"]
        path = self._path_to(anchor_id, edges)
        lines: List[str] = []
        for index, node_id in enumerate(path):
            node = node_map[node_id]
            if node["kind"] == "branch":
                label = self._branch_label_on_path(node, path, index, edges, cursor)
                if label:
                    lines.append(f"【分支】{label}")
                continue
            lines.append(self._format_story_beat(node))
        text = "\n\n".join(line for line in lines if line.strip())
        if len(text) > limit:
            text = "（更早的剧情已省略）\n\n" + text[-limit:]
        return text

    def insert_beat_at_cursor(
        self,
        tree_id: int,
        content: str,
        title: str = "",
        raw_text: str = "",
    ) -> int:
        """把新剧情块插到时点与旧后继之间，并把时点移到新块上。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self._require_tree(conn, tree_id)
            nodes = self._load_nodes(conn, tree_id)
            edges = self._load_edges(conn, tree_id)
            if not nodes:
                node_id = self._insert_node(
                    conn, tree_id, "beat", "common", title, content, raw_text
                )
                self._write_cursor(conn, tree_id, node_id, None)
                conn.commit()
                return node_id
            cursor = self._load_cursor(conn, tree_id)
            if not cursor or not cursor.get("after_node_id"):
                raise StoryTreeError("请先在剧情树上设置总结时点")
            anchor = self._node_by_id(nodes, cursor.get("after_node_id"))
            if not anchor:
                raise StoryTreeError("总结时点已经失效，请重新设置")
            kind, role, edge, variant_of = self._placement(
                anchor, edges, cursor.get("via_edge_id")
            )
            node_id = self._insert_node(
                conn, tree_id, "beat", role, title, content, raw_text,
                variant_of=variant_of,
            )
            self._splice(conn, tree_id, anchor["id"], edge, node_id, kind)
            self._write_cursor(conn, tree_id, node_id, None)
            conn.commit()
            return node_id

    def insert_beat_before(
        self,
        tree_id: int,
        before_node_id: int,
        content: str,
        title: str = "",
        raw_text: str = "",
    ) -> int:
        """插到指定块之前，不移动总结时点。供总结队列重试时补回更早的段落。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self._require_tree(conn, tree_id)
            nodes = self._load_nodes(conn, tree_id)
            edges = self._load_edges(conn, tree_id)
            target = self._node_by_id(nodes, before_node_id)
            if not target or target["tree_id"] != tree_id:
                raise StoryTreeError("要插入的位置不存在")
            incoming = self._incoming(edges, target["id"])
            if target["kind"] == "branch":
                role = "common"
                variant_of = None
            else:
                role = target["role"] if target["role"] in ("common", "append", "variant", "route") else "common"
                variant_of = target.get("variant_of") if role == "variant" else None
            kind = incoming["kind"] if incoming else "spine"
            node_id = self._insert_node(
                conn, tree_id, "beat", role, title, content, raw_text,
                variant_of=variant_of,
            )
            if incoming is None:
                self._insert_edge(conn, tree_id, node_id, target["id"], kind, "", 1)
            else:
                self._splice(conn, tree_id, incoming["from_node_id"], incoming, node_id, kind)
            conn.commit()
            return node_id

    def earliest_story_node(self, node_ids: List[int]) -> Optional[int]:
        ids = [int(item) for item in node_ids if item]
        if not ids:
            return None
        first = self.get_story_node(ids[0])
        if not first:
            return None
        graph = self.story_graph(first["tree_id"])
        wanted = set(ids)
        node_map = {node["id"]: node for node in graph["nodes"]}
        if not node_map:
            return None
        root = self._root_id(graph["nodes"], graph["edges"])
        order: List[int] = []
        self._walk_story(root, node_map, graph["edges"], order, set())
        for node_id in order:
            if node_id in wanted:
                return node_id
        return None

    def set_story_cursor(self, tree_id: int, after_node_id: int, via_edge_id: Optional[int] = None):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self._require_tree(conn, tree_id)
            nodes = self._load_nodes(conn, tree_id)
            edges = self._load_edges(conn, tree_id)
            node = self._node_by_id(nodes, after_node_id)
            if not node or node["tree_id"] != tree_id:
                raise StoryTreeError("这个段落不在当前剧情树上")
            chosen = None
            if node["kind"] == "branch":
                outs = self._outs(edges, node["id"], "branch")
                if via_edge_id is None:
                    if len(outs) == 1:
                        chosen = outs[0]["id"]
                    elif not outs:
                        raise StoryTreeError("这个分支点还没有线路")
                    else:
                        raise StoryTreeError("请选择要记录的线路")
                else:
                    match = next((edge for edge in outs if edge["id"] == int(via_edge_id)), None)
                    if not match:
                        raise StoryTreeError("这条线路不在当前分支上")
                    chosen = match["id"]
            elif node["kind"] != "beat":
                raise StoryTreeError("不能把总结时点放在这里")
            self._write_cursor(conn, tree_id, node["id"], chosen)
            conn.commit()

    def add_branch(
        self,
        tree_id: int,
        label: str,
        anchor_id: Optional[int] = None,
        content: str = "",
    ) -> Dict[str, int]:
        """新建一条线路，并放上第一块剧情。

        选中分支点时，在这个分支上再开一条同级线路。
        选中某条线路上的剧情时，在这条线路末尾再建一层子分支。
        没选中，或选中的是主链时，仍在主链末端开分支。
        线路名称是第一段标题，备注是第一段正文。
        """
        title = (label or "").strip()
        if not title:
            raise StoryTreeError("请填写线路名称")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self._require_tree(conn, tree_id)
            nodes = self._load_nodes(conn, tree_id)
            edges = self._load_edges(conn, tree_id)
            if not nodes:
                raise StoryTreeError("主链还没有剧情，不能建分支")
            node_map = {node["id"]: node for node in nodes}
            target = None
            if anchor_id:
                anchor = self._node_by_id(nodes, anchor_id)
                if not anchor or anchor["tree_id"] != tree_id:
                    raise StoryTreeError("这个段落不在当前剧情树上")
                target = self._branch_target(anchor, node_map, edges)
            if target is None:
                target = self._node_by_id(nodes, self._spine_end_id(nodes, edges))
                if not target:
                    raise StoryTreeError("主链不完整")
                if target["kind"] != "branch":
                    branch_id = self._insert_node(conn, tree_id, "branch", "common")
                    order = self._next_edge_order(conn, target["id"], "spine")
                    self._insert_edge(conn, tree_id, target["id"], branch_id, "spine", "", order)
                    target = {"id": branch_id, "kind": "branch"}
            if target["kind"] != "branch":
                branch_id = self._insert_node(conn, tree_id, "branch", "common")
                link_kind = self._chain_kind(target, edges)
                order = self._next_edge_order(conn, target["id"], link_kind)
                self._insert_edge(conn, tree_id, target["id"], branch_id, link_kind, "", order)
            else:
                branch_id = target["id"]
            edge_order = self._next_edge_order(conn, branch_id, "branch")
            beat_id = self._insert_node(conn, tree_id, "beat", "route", title, content or "")
            edge_id = self._insert_edge(conn, tree_id, branch_id, beat_id, "branch", title, edge_order)
            conn.commit()
            return {"branch_node_id": branch_id, "edge_id": edge_id, "beat_id": beat_id}

    def ensure_branch_beats(self, tree_id: int) -> int:
        """给还没有剧情块的分支补一块，方便选中并设置时点。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self._require_tree(conn, tree_id)
            edges = self._load_edges(conn, tree_id)
            created = 0
            for edge in edges:
                if edge["kind"] != "branch" or edge.get("to_node_id"):
                    continue
                title = (edge.get("label") or "").strip() or "未命名线路"
                beat_id = self._insert_node(conn, tree_id, "beat", "route", title, "")
                conn.execute(
                    "UPDATE story_edges SET to_node_id=? WHERE id=?",
                    (beat_id, edge["id"]),
                )
                created += 1
            if created:
                conn.commit()
            return created

    def add_side_beat(
        self,
        tree_id: int,
        anchor_id: int,
        role: str,
        condition_label: str = "",
    ) -> int:
        """在主链共通块旁边新开一条追加或差分。继续写下去要靠总结时点，不会并进已有的那一条。"""
        if role not in ("append", "variant"):
            raise StoryTreeError("这里只能新建追加或差分")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self._require_tree(conn, tree_id)
            nodes = self._load_nodes(conn, tree_id)
            edges = self._load_edges(conn, tree_id)
            anchor = self._node_by_id(nodes, anchor_id)
            if not anchor or anchor["tree_id"] != tree_id:
                raise StoryTreeError("这个段落不在当前剧情树上")
            if not self._is_spine_common(anchor, edges):
                raise StoryTreeError("追加和差分只能挂在主链的共通剧情上")
            variant_of = anchor["id"] if role == "variant" else None
            condition = (condition_label or "").strip()
            node_id = self._insert_node(
                conn, tree_id, "beat", role, "", "",
                variant_of=variant_of,
                condition_label=condition,
            )
            order = self._next_edge_order(conn, anchor["id"], "insert")
            self._insert_edge(conn, tree_id, anchor["id"], node_id, "insert", "", order)
            conn.commit()
            return node_id

    def split_beat_after(self, tree_id: int, node_id: int) -> int:
        """在剧情块后面插入一块空剧情，方便手动把正文剪开。不移动时点。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self._require_tree(conn, tree_id)
            nodes = self._load_nodes(conn, tree_id)
            edges = self._load_edges(conn, tree_id)
            node = self._node_by_id(nodes, node_id)
            if not node or node["tree_id"] != tree_id:
                raise StoryTreeError("这个段落不在当前剧情树上")
            if node["kind"] != "beat":
                raise StoryTreeError("分支点不能拆分，请选择剧情块")
            kind, role, edge, variant_of = self._placement(node, edges, None)
            new_id = self._insert_node(
                conn, tree_id, "beat", role, "", "",
                variant_of=variant_of,
            )
            self._splice(conn, tree_id, node["id"], edge, new_id, kind)
            conn.commit()
            return new_id

    def delete_story_node(self, node_id: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM story_nodes WHERE id=?", (node_id,)).fetchone()
            if not row:
                return
            node = dict(row)
            edges = self._load_edges(conn, node["tree_id"])
            outs = self._outs(edges, node["id"])
            dangling = [edge for edge in outs if not edge.get("to_node_id")]
            linked = [edge for edge in outs if edge.get("to_node_id")]
            if len(linked) > 1 or (dangling and node["kind"] != "branch"):
                raise StoryTreeError("请先删除后面的剧情")
            incoming = self._incoming(edges, node["id"])
            parent_id = incoming["from_node_id"] if incoming else None
            removed_edge_ids = {edge["id"] for edge in dangling}
            if len(linked) == 1:
                outgoing = linked[0]
                if not incoming or incoming["kind"] != outgoing["kind"]:
                    raise StoryTreeError("请先删除后面的剧情")
                conn.execute(
                    "UPDATE story_edges SET to_node_id=? WHERE id=?",
                    (outgoing["to_node_id"], incoming["id"]),
                )
                conn.execute("DELETE FROM story_edges WHERE id=?", (outgoing["id"],))
                conn.execute("DELETE FROM story_edges WHERE from_node_id=?", (node_id,))
                conn.execute("DELETE FROM story_nodes WHERE id=?", (node_id,))
            elif incoming and incoming["kind"] == "branch" and node["kind"] == "beat":
                removed_edge_ids.add(incoming["id"])
                conn.execute("DELETE FROM story_edges WHERE id=?", (incoming["id"],))
                conn.execute("DELETE FROM story_nodes WHERE id=?", (node_id,))
            else:
                conn.execute(
                    "DELETE FROM story_edges WHERE from_node_id=? OR to_node_id=?",
                    (node_id, node_id),
                )
                conn.execute("DELETE FROM story_nodes WHERE id=?", (node_id,))
            removed_route = bool(
                incoming and incoming["kind"] == "branch" and node["kind"] == "beat" and len(linked) != 1
            )
            cursor = self._load_cursor(conn, node["tree_id"])
            if cursor and (
                cursor.get("after_node_id") == node_id
                or cursor.get("via_edge_id") in removed_edge_ids
            ):
                if parent_id and not removed_route:
                    self._write_cursor(conn, node["tree_id"], parent_id, None)
                else:
                    conn.execute("DELETE FROM story_cursors WHERE tree_id=?", (node["tree_id"],))
            conn.commit()

    def render_story_tree_markdown(self, tree_id: int) -> str:
        graph = self.story_graph(tree_id)
        tree = graph["tree"]
        if not tree:
            raise StoryTreeError("剧情树不存在")
        game = self.get_game(tree["game_id"]) or {}
        lines = [
            f"# {game.get('name') or '未命名游戏'} - {tree.get('name') or '未命名剧情树'}",
            "",
            f"- 导出时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        nodes = graph["nodes"]
        edges = graph["edges"]
        if not nodes:
            lines.append("（暂无剧情）")
            lines.append("")
            return "\n".join(lines)
        node_map = {node["id"]: node for node in nodes}
        root = self._root_id(nodes, edges)
        lines.append("## 主链")
        lines.append("")
        self._render_story_chain(
            lines, root, node_map, edges, "spine", 3, with_sides=True, branch_heading=2
        )
        return "\n".join(lines).rstrip() + "\n"

    def _require_tree(self, conn: sqlite3.Connection, tree_id: int):
        row = conn.execute("SELECT id FROM story_trees WHERE id=?", (tree_id,)).fetchone()
        if not row:
            raise StoryTreeError("剧情树不存在")

    def _load_nodes(self, conn: sqlite3.Connection, tree_id: int) -> List[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM story_nodes WHERE tree_id=? ORDER BY id ASC",
            (tree_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _load_edges(self, conn: sqlite3.Connection, tree_id: int) -> List[Dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT * FROM story_edges
            WHERE tree_id=?
            ORDER BY sort_order ASC, id ASC
            """,
            (tree_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _load_cursor(self, conn: sqlite3.Connection, tree_id: int) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM story_cursors WHERE tree_id=?",
            (tree_id,),
        ).fetchone()
        return dict(row) if row else None

    def _insert_node(
        self,
        conn: sqlite3.Connection,
        tree_id: int,
        kind: str,
        role: str,
        title: str = "",
        content: str = "",
        raw_text: str = "",
        variant_of: Optional[int] = None,
        condition_label: str = "",
        include_in_context: int = 0,
    ) -> int:
        now = datetime.datetime.now().isoformat()
        cur = conn.execute(
            """
            INSERT INTO story_nodes (
                tree_id, kind, role, title, content, raw_text, variant_of,
                condition_label, include_in_context, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tree_id, kind, role, title or "", content or "", raw_text or "",
                variant_of, condition_label or "", int(include_in_context), now,
            ),
        )
        return int(cur.lastrowid)

    def _insert_edge(
        self,
        conn: sqlite3.Connection,
        tree_id: int,
        from_node_id: int,
        to_node_id: Optional[int],
        kind: str,
        label: str,
        sort_order: int,
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO story_edges
                (tree_id, from_node_id, to_node_id, kind, label, sort_order)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tree_id, from_node_id, to_node_id, kind, label or "", sort_order),
        )
        return int(cur.lastrowid)

    def _next_edge_order(self, conn: sqlite3.Connection, from_node_id: int, kind: str) -> int:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(sort_order), 0)
            FROM story_edges
            WHERE from_node_id=? AND kind=?
            """,
            (from_node_id, kind),
        ).fetchone()
        return int(row[0] or 0) + 1

    def _write_cursor(
        self,
        conn: sqlite3.Connection,
        tree_id: int,
        after_node_id: Optional[int],
        via_edge_id: Optional[int],
    ):
        conn.execute(
            """
            INSERT INTO story_cursors (tree_id, after_node_id, via_edge_id)
            VALUES (?, ?, ?)
            ON CONFLICT(tree_id) DO UPDATE SET
                after_node_id=excluded.after_node_id,
                via_edge_id=excluded.via_edge_id
            """,
            (tree_id, after_node_id, via_edge_id),
        )

    def _splice(
        self,
        conn: sqlite3.Connection,
        tree_id: int,
        from_node_id: int,
        edge: Optional[Dict[str, Any]],
        new_node_id: int,
        kind: str,
    ):
        if edge is None:
            order = self._next_edge_order(conn, from_node_id, kind)
            self._insert_edge(conn, tree_id, from_node_id, new_node_id, kind, "", order)
            return
        if not edge.get("to_node_id"):
            conn.execute(
                "UPDATE story_edges SET to_node_id=? WHERE id=?",
                (new_node_id, edge["id"]),
            )
            return
        old_to = edge["to_node_id"]
        conn.execute(
            "UPDATE story_edges SET to_node_id=? WHERE id=?",
            (new_node_id, edge["id"]),
        )
        order = self._next_edge_order(conn, new_node_id, edge["kind"])
        self._insert_edge(conn, tree_id, new_node_id, old_to, edge["kind"], "", order)

    def _placement(
        self,
        anchor: Dict[str, Any],
        edges: List[Dict[str, Any]],
        via_edge_id: Optional[int],
    ):
        if via_edge_id:
            edge = next((item for item in edges if item["id"] == int(via_edge_id)), None)
            if not edge or edge["from_node_id"] != anchor["id"]:
                raise StoryTreeError("总结时点指向的分支已经不存在，请重新设置")
            role = {"branch": "route", "spine": "common"}.get(edge["kind"], anchor.get("role") or "append")
            if role not in ("common", "append", "variant", "route"):
                role = "route" if edge["kind"] == "branch" else "common"
            variant_of = anchor.get("variant_of") if role == "variant" else None
            return edge["kind"], role, edge, variant_of
        if anchor["kind"] == "branch":
            outs = self._outs(edges, anchor["id"], "branch")
            if not outs:
                raise StoryTreeError("这个分支点还没有线路，请先新建分支")
            if len(outs) > 1:
                raise StoryTreeError("请选择要记录的线路")
            return "branch", "route", outs[0], None
        incoming = self._incoming(edges, anchor["id"])
        if incoming is None or incoming["kind"] == "spine":
            if anchor["kind"] != "beat":
                raise StoryTreeError("不能在这个位置继续总结")
            outs = self._outs(edges, anchor["id"], "spine")
            if len(outs) > 1:
                raise StoryTreeError("主链在这里分成了多条，请先整理")
            return "spine", "common", outs[0] if outs else None, None
        if incoming["kind"] == "insert":
            role = anchor["role"] if anchor["role"] in ("append", "variant") else "append"
            variant_of = anchor.get("variant_of") if role == "variant" else None
            outs = self._outs(edges, anchor["id"], "insert")
            if len(outs) > 1:
                raise StoryTreeError("这段后面有多条后续，请先整理")
            return "insert", role, outs[0] if outs else None, variant_of
        if incoming["kind"] == "branch":
            outs = self._outs(edges, anchor["id"], "branch")
            if len(outs) > 1:
                raise StoryTreeError("这段后面有多条后续，请先整理")
            return "branch", "route", outs[0] if outs else None, None
        raise StoryTreeError("不能在这个位置继续总结")

    def _outs(
        self,
        edges: List[Dict[str, Any]],
        node_id: int,
        kind: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        found = []
        for edge in edges:
            if edge["from_node_id"] != node_id:
                continue
            if kind and edge["kind"] != kind:
                continue
            found.append(edge)
        found.sort(key=lambda edge: (int(edge.get("sort_order") or 0), int(edge["id"])))
        return found

    def _incoming(self, edges: List[Dict[str, Any]], node_id: int) -> Optional[Dict[str, Any]]:
        hits = [edge for edge in edges if edge.get("to_node_id") == node_id]
        if not hits:
            return None
        if len(hits) > 1:
            raise StoryTreeError("剧情树里有段落被多处连接，当前版本不能处理")
        return hits[0]

    def _node_by_id(self, nodes: List[Dict[str, Any]], node_id: Optional[int]) -> Optional[Dict[str, Any]]:
        if not node_id:
            return None
        for node in nodes:
            if node["id"] == int(node_id):
                return node
        return None

    def _root_id(self, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> int:
        pointed = {edge["to_node_id"] for edge in edges if edge.get("to_node_id")}
        roots = [node["id"] for node in nodes if node["id"] not in pointed]
        if len(roots) != 1:
            raise StoryTreeError("剧情树结构不完整")
        return roots[0]

    def _spine_end_id(self, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> int:
        current = self._root_id(nodes, edges)
        seen = set()
        while current and current not in seen:
            seen.add(current)
            nxt = None
            for edge in self._outs(edges, current, "spine"):
                if edge.get("to_node_id"):
                    nxt = edge["to_node_id"]
                    break
            if not nxt:
                return current
            current = nxt
        return current

    def _chain_kind(self, node: Dict[str, Any], edges: List[Dict[str, Any]]) -> str:
        incoming = self._incoming(edges, node["id"])
        if incoming and incoming["kind"] in ("spine", "branch", "insert"):
            return incoming["kind"]
        if node.get("role") == "route":
            return "branch"
        return "spine"

    def _branch_target(
        self,
        anchor: Dict[str, Any],
        node_map: Dict[int, Dict[str, Any]],
        edges: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """分支点本身返回自己。线路上的剧情走到这条线的尽头。主链上的剧情返回空，交给主链末端。"""
        if anchor["kind"] == "branch":
            return anchor
        incoming = self._incoming(edges, anchor["id"])
        if not incoming or incoming["kind"] == "spine":
            return None
        kind = incoming["kind"]
        current = anchor
        seen = set()
        while current and current["id"] not in seen:
            seen.add(current["id"])
            if current["kind"] == "branch":
                return current
            outs = [
                edge for edge in self._outs(edges, current["id"], kind)
                if edge.get("to_node_id")
            ]
            if len(outs) > 1:
                raise StoryTreeError("这段后面有多条后续，请先选择其中一条")
            if not outs:
                return current
            nxt = node_map.get(outs[0]["to_node_id"])
            if not nxt:
                return current
            current = nxt
        return current

    def _is_spine_common(self, node: Dict[str, Any], edges: List[Dict[str, Any]]) -> bool:
        if node["kind"] != "beat" or node["role"] != "common":
            return False
        incoming = self._incoming(edges, node["id"])
        return incoming is None or incoming["kind"] == "spine"

    def _side_chain_end(
        self,
        anchor: Dict[str, Any],
        node_map: Dict[int, Dict[str, Any]],
        edges: List[Dict[str, Any]],
        role: str,
    ) -> Dict[str, Any]:
        current = anchor
        seen = set()
        while current["id"] not in seen:
            seen.add(current["id"])
            nxt = None
            for edge in self._outs(edges, current["id"], "insert"):
                target = node_map.get(edge.get("to_node_id"))
                if target and target.get("role") == role:
                    nxt = target
                    break
            if not nxt:
                return current
            current = nxt
        return current

    def _path_to(self, anchor_id: int, edges: List[Dict[str, Any]]) -> List[int]:
        parent = {}
        for edge in edges:
            if edge.get("to_node_id"):
                parent[edge["to_node_id"]] = edge
        path = []
        seen = set()
        current = anchor_id
        while current and current not in seen:
            seen.add(current)
            path.append(current)
            edge = parent.get(current)
            current = edge["from_node_id"] if edge else None
        path.reverse()
        return path

    def _edge_label(self, edges: List[Dict[str, Any]], edge_id: Optional[int]) -> str:
        if not edge_id:
            return ""
        for edge in edges:
            if edge["id"] == int(edge_id):
                return (edge.get("label") or "").strip()
        return ""

    def _branch_label_on_path(
        self,
        node: Dict[str, Any],
        path: List[int],
        index: int,
        edges: List[Dict[str, Any]],
        cursor: Dict[str, Any],
    ) -> str:
        if index + 1 < len(path):
            nxt = path[index + 1]
            for edge in edges:
                if (
                    edge["from_node_id"] == node["id"]
                    and edge.get("to_node_id") == nxt
                    and edge["kind"] == "branch"
                ):
                    return (edge.get("label") or "").strip() or "未命名线路"
            return ""
        return self._edge_label(edges, cursor.get("via_edge_id")) or ""

    def _format_story_beat(self, node: Dict[str, Any]) -> str:
        role_name = {
            "common": "共通",
            "append": "追加",
            "variant": "差分",
            "route": "线路",
        }.get(node.get("role") or "", "剧情")
        title = (node.get("title") or "").strip() or "未命名"
        head = f"【{role_name}】{title}"
        parts = [head]
        condition = (node.get("condition_label") or "").strip()
        if condition:
            parts.append(f"（{condition}）")
        body = (node.get("content") or "").strip()
        if body:
            parts.append(body)
        return "\n".join(parts)

    def _walk_story(
        self,
        node_id: Optional[int],
        node_map: Dict[int, Dict[str, Any]],
        edges: List[Dict[str, Any]],
        order: List[int],
        seen: set,
    ):
        if not node_id or node_id in seen or node_id not in node_map:
            return
        seen.add(node_id)
        order.append(node_id)
        for kind in ("insert", "branch", "spine"):
            for edge in self._outs(edges, node_id, kind):
                if edge.get("to_node_id"):
                    self._walk_story(edge["to_node_id"], node_map, edges, order, seen)

    def _render_branch(
        self,
        lines: List[str],
        branch_id: int,
        node_map: Dict[int, Dict[str, Any]],
        edges: List[Dict[str, Any]],
        heading: int,
    ):
        marks = "#" * max(2, min(heading, 6))
        for edge in self._outs(edges, branch_id, "branch"):
            label = (edge.get("label") or "").strip() or "未命名线路"
            lines.append(f"{marks} 分支：{label}")
            lines.append("")
            if edge.get("to_node_id"):
                self._render_story_chain(
                    lines, edge["to_node_id"], node_map, edges, "branch",
                    heading + 1, with_sides=False, branch_heading=heading + 1,
                )
            else:
                lines.append("（尚无剧情）")
                lines.append("")

    def _render_story_chain(
        self,
        lines: List[str],
        start_id: int,
        node_map: Dict[int, Dict[str, Any]],
        edges: List[Dict[str, Any]],
        kind: str,
        heading: int,
        with_sides: bool,
        branch_heading: Optional[int] = None,
    ):
        current = start_id
        seen = set()
        marks = "#" * heading
        while current and current not in seen:
            seen.add(current)
            node = node_map.get(current)
            if not node or node["kind"] == "branch":
                return
            title = (node.get("title") or "").strip() or "未命名"
            lines.append(f"{marks} {title}")
            lines.append("")
            condition = (node.get("condition_label") or "").strip()
            if condition:
                lines.append(f"> {condition}")
                lines.append("")
            lines.append((node.get("content") or "").strip() or "（空）")
            lines.append("")
            if with_sides:
                self._render_story_sides(lines, node, node_map, edges, heading + 1)
            nxt = None
            for edge in self._outs(edges, current, kind):
                if edge.get("to_node_id"):
                    nxt = edge["to_node_id"]
                    break
            nxt_node = node_map.get(nxt) if nxt else None
            if nxt_node and nxt_node["kind"] == "branch":
                self._render_branch(
                    lines, nxt_node["id"], node_map, edges,
                    branch_heading if branch_heading is not None else heading,
                )
                return
            current = nxt

    def _render_story_sides(
        self,
        lines: List[str],
        anchor: Dict[str, Any],
        node_map: Dict[int, Dict[str, Any]],
        edges: List[Dict[str, Any]],
        heading: int,
    ):
        grouped = {"append": [], "variant": []}
        for edge in self._outs(edges, anchor["id"], "insert"):
            target = node_map.get(edge.get("to_node_id"))
            if target and target.get("role") in grouped:
                grouped[target["role"]].append(target["id"])
        sub = "#" * heading
        for role, title in (("append", "追加"), ("variant", "差分")):
            for start_id in grouped[role]:
                lines.append(f"{sub} {title}")
                lines.append("")
                self._render_story_chain(
                    lines, start_id, node_map, edges, "insert", heading + 1, with_sides=False
                )
