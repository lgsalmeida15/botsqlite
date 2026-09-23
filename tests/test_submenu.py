# -*- coding: utf-8 -*-
"""Regressão do menu plano e fluxo Menu → Submenu → Ação."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import Engine  # noqa: E402


class FakeSock:
    own_address = "u:bot@claro.com"

    def __init__(self) -> None:
        self.cmds: list[tuple[str, dict]] = []

    async def command(self, name, payload=None, **kwargs):
        payload = payload or {}
        self.cmds.append((name, payload))
        if name == "get_online_users":
            return {"d": {"result": ["ana"]}}
        if name == "get_active_chats":
            return {"d": {"chats": []}}
        return {"d": {}}

    def sent_texts(self) -> list[str]:
        return [p["message"]["text"] for n, p in self.cmds if n == "send_message"]


class FakeSockNoOnline(FakeSock):
    def __init__(self) -> None:
        super().__init__()
        self.online_calls = 0

    async def command(self, name, payload=None, **kwargs):
        if name == "get_online_users":
            self.online_calls += 1
            return {"d": {"result": []}}
        return await super().command(name, payload, **kwargs)


def mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = db._dict_factory
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    db.ensure_schema(conn)
    return conn


def seed_queue_menu(conn, *, with_team: bool = False) -> None:
    conn.execute("INSERT INTO queues (address, name, enabled) VALUES ('h:q1', 'Q1', 1)")
    conn.execute("INSERT INTO menus (name, active) VALUES ('M1', 1)")
    conn.execute("INSERT INTO menu_queues (menu_id, queue_address) VALUES (1, 'h:q1')")
    if with_team:
        conn.execute("INSERT INTO teams (name, active) VALUES ('T1', 1)")
        conn.execute("INSERT INTO team_queues (team_id, queue_address) VALUES (1, 'h:q1')")
        conn.execute(
            "INSERT INTO team_members (team_id, address, name, active) VALUES (1, 'u:ana@claro.com', 'Ana', 1)"
        )


def insert_option(conn, **fields) -> int:
    cols = ", ".join(fields)
    placeholders = ", ".join(["?"] * len(fields))
    cur = conn.execute(
        f"INSERT INTO menu_options ({cols}) VALUES ({placeholders})",
        list(fields.values()),
    )
    return int(cur.lastrowid)


def awaiting(conn, cid: int = 10, node=None, retries: int = 0) -> dict:
    db.upsert_chat(
        conn,
        cid,
        queue_address="h:q1",
        status="awaiting_reply",
        retries=retries,
        menu_node_id=node,
        last_prompt_at="2000-01-01 00:00:00",
    )
    return conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone()


class SchemaSubmenuTest(unittest.TestCase):
    def test_new_schema_has_parent_and_menu_node(self):
        conn = mem_conn()
        self.assertIn("parent_id", db._table_columns(conn, "menu_options"))
        self.assertIn("menu_node_id", db._table_columns(conn, "chats"))
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'menu_options'"
        ).fetchone()["sql"]
        self.assertIn("submenu", sql)

    def test_migrate_old_table_adds_columns_and_allows_submenu(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = db._dict_factory
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE queues (address TEXT PRIMARY KEY, name TEXT, enabled INTEGER DEFAULT 0);
            CREATE TABLE menus (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, active INTEGER DEFAULT 1);
            CREATE TABLE menu_options (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              menu_id INTEGER REFERENCES menus(id),
              queue_address TEXT,
              sort_order INTEGER NOT NULL,
              label TEXT NOT NULL,
              classification TEXT NOT NULL,
              action TEXT NOT NULL CHECK (action IN ('reply_and_close', 'ask_and_transfer', 'transfer_only')),
              reply_text TEXT NOT NULL DEFAULT '',
              transfer_target_id INTEGER
            );
            CREATE UNIQUE INDEX menu_options_per_menu
              ON menu_options (menu_id, sort_order) WHERE menu_id IS NOT NULL;
            CREATE TABLE chats (
              chat_id INTEGER PRIMARY KEY,
              queue_address TEXT,
              status TEXT NOT NULL,
              classification TEXT,
              option_id INTEGER,
              retries INTEGER NOT NULL DEFAULT 0,
              auto_close INTEGER NOT NULL DEFAULT 0,
              menu_sent_at TEXT,
              last_prompt_at TEXT,
              classified_at TEXT,
              left_at TEXT,
              updated_at TEXT
            );
            """
        )
        conn.execute("INSERT INTO menus (name) VALUES ('old')")
        conn.execute(
            """INSERT INTO menu_options (menu_id, sort_order, label, classification, action)
               VALUES (1, 1, 'A', 'cls', 'reply_and_close')"""
        )
        db._migrate_submenu(conn)
        self.assertIn("parent_id", db._table_columns(conn, "menu_options"))
        self.assertIn("menu_node_id", db._table_columns(conn, "chats"))
        db._migrate_ask(conn)
        self.assertIn("ask_option_id", db._table_columns(conn, "chats"))
        self.assertIn("last_reply_at", db._table_columns(conn, "chats"))
        conn.execute("UPDATE menu_options SET action = 'submenu', classification = '' WHERE id = 1")
        row = conn.execute("SELECT action, parent_id FROM menu_options WHERE id = 1").fetchone()
        self.assertEqual(row["action"], "submenu")
        self.assertIsNone(row["parent_id"])

    def test_unicidade_por_nivel(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Raiz A",
            classification="a",
            action="reply_and_close",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            insert_option(
                conn,
                menu_id=1,
                sort_order=1,
                label="Raiz B",
                classification="b",
                action="reply_and_close",
            )
        parent = insert_option(
            conn,
            menu_id=1,
            sort_order=2,
            label="Grupo",
            classification="",
            action="submenu",
        )
        insert_option(
            conn,
            menu_id=1,
            parent_id=parent,
            sort_order=1,
            label="Filho A",
            classification="fa",
            action="reply_and_close",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            insert_option(
                conn,
                menu_id=1,
                parent_id=parent,
                sort_order=1,
                label="Filho dup",
                classification="fd",
                action="reply_and_close",
            )

    def test_menu_options_filtra_nivel(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Encerrar",
            classification="fim",
            action="reply_and_close",
        )
        parent = insert_option(
            conn,
            menu_id=1,
            sort_order=2,
            label="Outros",
            classification="",
            action="submenu",
        )
        insert_option(
            conn,
            menu_id=1,
            parent_id=parent,
            sort_order=1,
            label="Filho",
            classification="filho",
            action="transfer_only",
        )
        roots = db.menu_options(conn, "h:q1")
        self.assertEqual([o["label"] for o in roots], ["Encerrar", "Outros"])
        kids = db.menu_options(conn, "h:q1", parent)
        self.assertEqual([o["label"] for o in kids], ["Filho"])


class EngineSubmenuTest(unittest.IsolatedAsyncioTestCase):
    def _eng(self, conn) -> tuple[Engine, FakeSock]:
        sock = FakeSock()
        return Engine(conn, sock), sock

    async def test_menu_plano_classifica_e_encerra(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Encerrar",
            classification="plano",
            action="reply_and_close",
            reply_text="ok",
        )
        eng, sock = self._eng(conn)
        row = awaiting(conn)
        await eng.handle_reply(10, row, "1")
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["classification"], "plano")
        self.assertEqual(chat["status"], "closed")
        self.assertIn("ok", sock.sent_texts())
        reasons = [e["reason"] for e in conn.execute("SELECT reason FROM bot_events").fetchall()]
        self.assertIn("classified", reasons)

    async def test_submenu_voltar_folha_classifica_e_transfere(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Encerrar",
            classification="fim",
            action="reply_and_close",
        )
        parent = insert_option(
            conn,
            menu_id=1,
            sort_order=2,
            label="Suporte",
            classification="",
            action="submenu",
        )
        insert_option(
            conn,
            menu_id=1,
            parent_id=parent,
            sort_order=1,
            label="Técnico",
            classification="tec",
            action="transfer_only",
        )
        eng, sock = self._eng(conn)
        row = awaiting(conn)
        await eng.handle_reply(10, row, "2")
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(int(chat["menu_node_id"]), parent)
        self.assertEqual(chat["status"], "awaiting_reply")
        self.assertEqual(int(chat["retries"] or 0), 0)
        sub = sock.sent_texts()[-1]
        self.assertIn("Suporte", sub)
        self.assertIn("1) Técnico", sub)
        self.assertIn("0) Voltar", sub)
        self.assertNotIn("Olá", sub)

        row = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        await eng.handle_reply(10, row, "0")
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertIsNone(chat["menu_node_id"])
        root = sock.sent_texts()[-1]
        self.assertIn("1) Encerrar", root)
        self.assertIn("2) Suporte", root)

        row = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        await eng.handle_reply(10, row, "2")
        row = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        await eng.handle_reply(10, row, "1")
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["classification"], "tec")
        self.assertEqual(chat["status"], "closed")
        cmds = [n for n, _ in sock.cmds]
        self.assertIn("move_chat", cmds)
        self.assertIn("invite_to_chat", cmds)
        details = [
            e["detail"]
            for e in conn.execute("SELECT detail FROM bot_events WHERE reason = 'classified'").fetchall()
        ]
        self.assertTrue(any("Suporte > Técnico" in d for d in details))

    async def test_retry_no_submenu_reenvia_lista(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        parent = insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Grupo",
            classification="",
            action="submenu",
        )
        insert_option(
            conn,
            menu_id=1,
            parent_id=parent,
            sort_order=1,
            label="Filho",
            classification="f",
            action="reply_and_close",
        )
        insert_option(
            conn,
            menu_id=1,
            sort_order=2,
            label="Folha raiz",
            classification="raiz",
            action="reply_and_close",
        )
        eng, sock = self._eng(conn)
        row = awaiting(conn, node=parent)
        await eng.handle_reply(10, row, "9")
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["status"], "awaiting_reply")
        self.assertEqual(int(chat["menu_node_id"]), parent)
        self.assertEqual(int(chat["retries"]), 1)
        texts = sock.sent_texts()
        self.assertEqual(texts[0], "Por gentileza, digite o numero que melhor corresponde a solicitação")
        self.assertIn("0) Voltar", texts[1])
        self.assertIn("1) Filho", texts[1])
        self.assertNotIn("2) Folha raiz", texts[1])

    async def test_submenu_vazio_e_invalido(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Vazio",
            classification="",
            action="submenu",
        )
        eng, sock = self._eng(conn)
        row = awaiting(conn)
        await eng.handle_reply(10, row, "1")
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertIsNone(chat["menu_node_id"])
        self.assertEqual(int(chat["retries"]), 1)
        self.assertTrue(sock.sent_texts())

    async def test_timeout_no_submenu_encerra(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        parent = insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Grupo",
            classification="",
            action="submenu",
        )
        insert_option(
            conn,
            menu_id=1,
            parent_id=parent,
            sort_order=1,
            label="Filho",
            classification="f",
            action="reply_and_close",
        )
        eng, sock = self._eng(conn)
        awaiting(conn, node=parent)
        conn.execute(
            "UPDATE chats SET last_prompt_at = datetime('now', '-400 seconds') WHERE chat_id = 10"
        )
        await eng.tick_timeouts()
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["status"], "closed_timeout")

    def test_menu_text_raiz_e_submenu(self):
        conn = mem_conn()
        eng, _ = self._eng(conn)
        opts = [{"sort_order": 1, "label": "A"}, {"sort_order": 2, "label": "B"}]
        root = eng._menu_text(opts, root=True)
        self.assertIn("Olá. Sou o assistente de triagem.", root)
        self.assertIn("1) A", root)
        self.assertNotIn("0) Voltar", root)
        sub = eng._menu_text(opts, root=False, title="Grupo")
        self.assertTrue(sub.startswith("Grupo"))
        self.assertIn("0) Voltar", sub)
        self.assertNotIn("Olá", sub)

    async def test_pick_assignee_sem_online_tenta_5(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        sock = FakeSockNoOnline()
        eng = Engine(conn, sock)
        eng.cfg["transfer_attempts"] = "5"
        eng.cfg["transfer_retry_interval_sec"] = "0"
        picked, reason = await eng.pick_assignee("h:q1")
        self.assertIsNone(picked)
        self.assertEqual(sock.online_calls, 5)
        self.assertIn("tentativas=5", reason)

    async def test_transfer_sem_analista_fica_na_fila(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Transferir",
            classification="tec",
            action="transfer_only",
        )
        sock = FakeSockNoOnline()
        eng = Engine(conn, sock)
        eng.cfg["transfer_attempts"] = "5"
        eng.cfg["transfer_retry_interval_sec"] = "0"
        row = awaiting(conn)
        await eng.handle_reply(10, row, "1")
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["status"], "classified")
        cmds = [n for n, _ in sock.cmds]
        self.assertNotIn("leave_chat", cmds)
        self.assertNotIn("move_chat", cmds)
        wait = conn.execute("SELECT * FROM pending_routes WHERE status = 'waiting'").fetchall()
        self.assertEqual(len(wait), 1)
        self.assertIsNone(wait[0]["expires_at"])
        reasons = [
            e["reason"]
            for e in conn.execute("SELECT reason FROM bot_events").fetchall()
        ]
        self.assertIn("no_agent", reasons)
        self.assertIn("queued", reasons)

    async def test_transfer_sem_analista_com_fila_desligada_sai_do_chat(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Transferir",
            classification="tec",
            action="transfer_only",
        )
        sock = FakeSockNoOnline()
        eng = Engine(conn, sock)
        eng.cfg["transfer_attempts"] = "1"
        eng.cfg["transfer_retry_interval_sec"] = "0"
        eng.cfg["queue_wait_enabled"] = "false"
        row = awaiting(conn)
        await eng.handle_reply(10, row, "1")
        conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["status"], "closed_no_agent")
        cmds = [n for n, _ in sock.cmds]
        self.assertIn("leave_chat", cmds)
        self.assertNotIn("move_chat", cmds)
        reasons = [
            e["reason"]
            for e in conn.execute("SELECT reason FROM bot_events").fetchall()
        ]
        self.assertIn("no_agent", reasons)
        self.assertNotIn("monitoring", chat["status"])


class UnicidadeFilhoOkTest(unittest.TestCase):
    def test_mesmo_numero_em_niveis_diferentes(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Raiz",
            classification="r",
            action="reply_and_close",
        )
        parent = insert_option(
            conn,
            menu_id=1,
            sort_order=2,
            label="Grupo",
            classification="",
            action="submenu",
        )
        child = insert_option(
            conn,
            menu_id=1,
            parent_id=parent,
            sort_order=1,
            label="Filho",
            classification="f",
            action="reply_and_close",
        )
        self.assertGreater(child, 0)


if __name__ == "__main__":
    unittest.main()
