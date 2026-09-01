from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager

from .config import sqlite_path, ROOT

log = logging.getLogger("helptoa")

SCHEMA_PATH = ROOT / "schema.sql"


def _dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def connect():
    path = sqlite_path()
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    conn.row_factory = _dict_factory
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    ensure_schema(conn)
    return conn


def conn_closed(conn) -> bool:
    try:
        if conn is None:
            return True
        conn.execute("SELECT 1")
        return False
    except Exception:
        return True


def _table_names(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def ensure_schema(conn) -> None:
    if "menu_options" in _table_names(conn):
        _migrate_submenu(conn)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    _migrate_submenu(conn)


def _table_columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_submenu(conn) -> None:
    cols = _table_columns(conn, "menu_options")
    if "parent_id" not in cols:
        conn.execute(
            "ALTER TABLE menu_options ADD COLUMN parent_id INTEGER REFERENCES menu_options(id) ON DELETE CASCADE"
        )
    chat_cols = _table_columns(conn, "chats")
    if "menu_node_id" not in chat_cols:
        conn.execute(
            "ALTER TABLE chats ADD COLUMN menu_node_id INTEGER REFERENCES menu_options(id) ON DELETE SET NULL"
        )
    if "reply_text_if_form" not in cols:
        conn.execute("ALTER TABLE menu_options ADD COLUMN reply_text_if_form TEXT NOT NULL DEFAULT ''")
    if "action_if_form" not in cols:
        conn.execute("ALTER TABLE menu_options ADD COLUMN action_if_form TEXT NOT NULL DEFAULT ''")
    if "script_slug" not in cols:
        conn.execute("ALTER TABLE menu_options ADD COLUMN script_slug TEXT NOT NULL DEFAULT ''")
    chat_cols = _table_columns(conn, "chats")
    if "script_slug" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN script_slug TEXT NOT NULL DEFAULT ''")
    if "script_field_index" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN script_field_index INTEGER NOT NULL DEFAULT 0")
    if "script_answers" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN script_answers TEXT NOT NULL DEFAULT '{}'")
    if "script_uuid" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN script_uuid TEXT NOT NULL DEFAULT ''")
    if "script_started_at" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN script_started_at TEXT")
    conn.execute("DROP INDEX IF EXISTS menu_options_per_menu")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_root
           ON menu_options (menu_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_child
           ON menu_options (menu_id, parent_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NOT NULL"""
    )
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'menu_options'"
    ).fetchone()
    sql = (row or {}).get("sql") or ""
    if sql and "run_script" not in sql:
        _rebuild_menu_options_check(conn)


def _rebuild_menu_options_check(conn) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    conn.execute(
        """CREATE TABLE menu_options_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id INTEGER REFERENCES menus(id) ON DELETE CASCADE,
            queue_address TEXT REFERENCES queues(address) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL,
            label TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL CHECK (action IN ('reply_and_close', 'ask_and_transfer', 'transfer_only', 'submenu', 'run_script')),
            reply_text TEXT NOT NULL DEFAULT '',
            reply_text_if_form TEXT NOT NULL DEFAULT '',
            action_if_form TEXT NOT NULL DEFAULT '',
            transfer_target_id INTEGER REFERENCES transfer_targets(id),
            parent_id INTEGER REFERENCES menu_options(id) ON DELETE CASCADE,
            script_slug TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        """INSERT INTO menu_options_new (id, menu_id, queue_address, sort_order, label, classification, action, reply_text, reply_text_if_form, action_if_form, transfer_target_id, parent_id, script_slug)
           SELECT id, menu_id, queue_address, sort_order, label, classification, action, reply_text,
                  COALESCE(reply_text_if_form, ''), COALESCE(action_if_form, ''), transfer_target_id, parent_id,
                  COALESCE(script_slug, '') FROM menu_options"""
    )
    conn.execute("DROP TABLE menu_options")
    conn.execute("ALTER TABLE menu_options_new RENAME TO menu_options")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_queue
           ON menu_options (queue_address, sort_order) WHERE queue_address IS NOT NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_root
           ON menu_options (menu_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_child
           ON menu_options (menu_id, parent_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NOT NULL"""
    )
    conn.execute("COMMIT")
    conn.execute("PRAGMA foreign_keys = ON")


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def upsert_queue(conn, address: str, name: str) -> None:
    conn.execute(
        """INSERT INTO queues (address, name) VALUES (?, ?)
           ON CONFLICT (address) DO UPDATE
           SET name = COALESCE(NULLIF(excluded.name, ''), queues.name),
               last_seen = datetime('now')""",
        (address, name or ""),
    )


def enabled_queues(conn) -> set[str]:
    rows = conn.execute("SELECT address FROM queues WHERE enabled = 1").fetchall()
    return {r["address"] for r in rows}


def settings(conn) -> dict:
    rows = conn.execute("SELECT key, value FROM bot_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def templates(conn) -> dict:
    rows = conn.execute("SELECT slug, body FROM templates").fetchall()
    return {r["slug"]: r["body"] for r in rows}


def menu_option(conn, option_id: int | None) -> dict | None:
    if not option_id:
        return None
    return conn.execute("SELECT * FROM menu_options WHERE id = ?", (int(option_id),)).fetchone()


def menu_options(conn, queue_address: str | None, parent_id: int | None = None) -> list[dict]:
    if parent_id is not None:
        return conn.execute(
            "SELECT * FROM menu_options WHERE parent_id = ? ORDER BY sort_order",
            (int(parent_id),),
        ).fetchall()
    if queue_address:
        rows = conn.execute(
            """SELECT o.*
               FROM menu_options o
               JOIN menus m ON m.id = o.menu_id
               JOIN menu_queues mq ON mq.menu_id = m.id
               WHERE mq.queue_address = ? AND m.active = 1 AND o.parent_id IS NULL
               ORDER BY o.sort_order""",
            (queue_address,),
        ).fetchall()
        if rows:
            return rows
        own = conn.execute(
            """SELECT * FROM menu_options
               WHERE queue_address = ? AND parent_id IS NULL
               ORDER BY sort_order""",
            (queue_address,),
        ).fetchall()
        if own:
            return own
    return conn.execute(
        """SELECT o.*
           FROM menu_options o
           JOIN menus m ON m.id = o.menu_id
           WHERE m.active = 1 AND o.menu_id IS NOT NULL
             AND o.parent_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM menu_queues mq WHERE mq.menu_id = m.id)
             AND o.queue_address IS NULL
           ORDER BY o.sort_order"""
    ).fetchall() or conn.execute(
        """SELECT * FROM menu_options
           WHERE queue_address IS NULL AND parent_id IS NULL
           ORDER BY sort_order"""
    ).fetchall()


def team_for_queue(conn, queue_address: str | None) -> dict | None:
    if not queue_address:
        return None
    return conn.execute(
        """SELECT t.*
           FROM teams t
           JOIN team_queues tq ON tq.team_id = t.id
           WHERE tq.queue_address = ? AND t.active = 1""",
        (queue_address,),
    ).fetchone()


def team_members(conn, team_id: int) -> list[dict]:
    return conn.execute(
        """SELECT * FROM team_members
           WHERE team_id = ? AND active = 1
           ORDER BY sort_order, name, address""",
        (team_id,),
    ).fetchall()


def set_team_last_assigned(conn, team_id: int, address: str) -> None:
    conn.execute(
        "UPDATE teams SET last_assigned = ? WHERE id = ?",
        (address, team_id),
    )


def upsert_queue_operator(conn, queue_address: str, address: str, name: str) -> None:
    conn.execute(
        """INSERT INTO queue_operators (queue_address, address, name) VALUES (?, ?, ?)
           ON CONFLICT (queue_address, address) DO UPDATE
           SET name = COALESCE(NULLIF(excluded.name, ''), queue_operators.name)""",
        (queue_address, address, name or ""),
    )


def ingest_search_contact(raw: dict) -> tuple[int, int]:
    """Grava filas/operadores em lote numa conexão própria (thread-safe)."""
    queues: list[tuple[str, str]] = []
    operators: list[tuple[str, str, str]] = []
    seen_ops: set[str] = set()
    seen_q: set[str] = set()
    for qa, qd in raw.items():
        qa = str(qa or "")
        if not qa.startswith("h:") or not isinstance(qd, dict):
            continue
        if qa not in seen_q:
            queues.append((qa, qd.get("name") or ""))
            seen_q.add(qa)
        for op in qd.get("operators") or []:
            if not isinstance(op, dict):
                continue
            addr = str(op.get("address") or "")
            if not addr.startswith("u:"):
                continue
            operators.append((qa, addr, op.get("name") or ""))
            seen_ops.add(addr)
    conn = connect()
    try:
        if queues:
            conn.executemany(
                """INSERT INTO queues (address, name) VALUES (?, ?)
                   ON CONFLICT (address) DO UPDATE
                   SET name = COALESCE(NULLIF(excluded.name, ''), queues.name),
                       last_seen = datetime('now')""",
                queues,
            )
        if operators:
            conn.executemany(
                """INSERT INTO queue_operators (queue_address, address, name) VALUES (?, ?, ?)
                   ON CONFLICT (queue_address, address) DO UPDATE
                   SET name = COALESCE(NULLIF(excluded.name, ''), queue_operators.name)""",
                operators,
            )
    finally:
        conn.close()
    return len(seen_ops), len(seen_q)


def transfer_target(conn, tid: int | None) -> dict | None:
    if not tid:
        return None
    return conn.execute(
        "SELECT * FROM transfer_targets WHERE id = ? AND active = 1", (tid,)
    ).fetchone()


def option_targets(conn, option_id: int | None) -> list[dict]:
    if not option_id:
        return []
    rows = conn.execute(
        "SELECT address, name FROM menu_option_targets WHERE option_id = ? ORDER BY name, address",
        (option_id,),
    ).fetchall()
    if rows:
        return rows
    opt = conn.execute(
        "SELECT transfer_target_id FROM menu_options WHERE id = ?",
        (option_id,),
    ).fetchone()
    if not opt or not opt.get("transfer_target_id"):
        return []
    tgt = transfer_target(conn, opt["transfer_target_id"])
    if not tgt:
        return []
    addr = str(tgt.get("address") or "")
    if not addr.startswith("u:"):
        return []
    return [{"address": addr, "name": tgt.get("name") or ""}]


def lock_chat(conn, chat_id: int) -> dict | None:
    return conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()


def upsert_chat(conn, chat_id: int, **fields) -> None:
    cols = ["chat_id"] + list(fields)
    vals = [chat_id] + list(fields.values())
    placeholders = ", ".join(["?"] * len(vals))
    assignments = ", ".join(f"{k} = excluded.{k}" for k in fields)
    conn.execute(
        f"""INSERT INTO chats ({", ".join(cols)}, updated_at)
            VALUES ({placeholders}, datetime('now'))
            ON CONFLICT (chat_id) DO UPDATE SET {assignments}, updated_at = datetime('now')""",
        vals,
    )


def awaiting_timeouts(conn, seconds: int) -> list[dict]:
    return conn.execute(
        """SELECT * FROM chats
           WHERE status IN ('awaiting_reply', 'awaiting_script_answer', 'awaiting_script_confirm')
             AND last_prompt_at IS NOT NULL
             AND datetime(last_prompt_at) < datetime('now', '-' || ? || ' seconds')""",
        (str(seconds),),
    ).fetchall()


def resume_chats(conn) -> list[dict]:
    return conn.execute(
        "SELECT * FROM chats WHERE status IN ('joined', 'menu_sent', 'awaiting_reply', 'classified', 'monitoring', 'awaiting_script_answer', 'awaiting_script_confirm', 'awaiting_script_run')"
    ).fetchall()


def script_run_timeouts(conn, seconds: int = 60) -> list[dict]:
    return conn.execute(
        """SELECT * FROM chats
           WHERE status = 'awaiting_script_run'
             AND script_started_at IS NOT NULL
             AND datetime(script_started_at) < datetime('now', '-' || ? || ' seconds')""",
        (str(seconds),),
    ).fetchall()


def log_event(
    conn,
    reason: str,
    chat_id: int | None = None,
    queue_address: str | None = None,
    queue_enabled: bool | None = None,
    detail: str = "",
) -> None:
    enabled = None if queue_enabled is None else (1 if queue_enabled else 0)
    try:
        conn.execute(
            """INSERT INTO bot_events (chat_id, queue_address, queue_enabled, reason, detail)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, queue_address, enabled, reason, detail or ""),
        )
    except Exception:
        log.warning("bot_events: insert falhou", exc_info=True)
