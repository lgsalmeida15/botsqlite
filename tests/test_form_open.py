# -*- coding: utf-8 -*-
"""Detecção de formulário de validação aberto e resolução da folha."""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import Engine, MEM, remember  # noqa: E402
from python.form_open import is_open_validation_form  # noqa: E402
from tests.test_submenu import FakeSock, awaiting, insert_option, mem_conn, seed_queue_menu  # noqa: E402

OWN = "u:bot@claro.com"
QUEUE = "h:q1"
TEC = "u:tec@claro.com"
OTHER = "u:outro@claro.com"


def queue_chat(**over):
    c = {
        "chat_id": 10,
        "started_addr": TEC,
        "helpdesk_address": QUEUE,
        "participants": {TEC: {"address": TEC, "name": "Técnico Silva", "present": True}},
    }
    c.update(over)
    return c


def form_chat(**over):
    c = {
        "chat_id": 99,
        "mode": "message_scenario",
        "lifecycle": "pending",
        "ended_time": -1,
        "started_addr": TEC,
        "helpdesk_address": QUEUE,
        "participants": {
            TEC: {"address": TEC, "name": "Técnico Silva", "present": True},
            "CREATEDBYMESSAGESCENARIO": {
                "address": "CREATEDBYMESSAGESCENARIO",
                "role_desc": "Moderator",
            },
        },
        "first_message": {
            "subject": "Solicitação de Encerramento",
            "author": "CREATEDBYMESSAGESCENARIO",
        },
    }
    c.update(over)
    return c


class OpenFormDetectionTest(unittest.TestCase):
    def test_message_scenario_pending_e_aberto(self):
        self.assertTrue(is_open_validation_form(form_chat()))

    def test_ended_time_positivo_nao_e_aberto(self):
        self.assertFalse(is_open_validation_form(form_chat(ended_time=1700000000, lifecycle="removed")))

    def test_lifecycle_removed_nao_e_aberto(self):
        self.assertFalse(is_open_validation_form(form_chat(lifecycle="removed", ended_time=None)))

    def test_chat_de_fila_nao_e_formulario(self):
        self.assertFalse(is_open_validation_form(queue_chat()))




class SchemaFormFieldsTest(unittest.TestCase):
    def test_novas_colunas_no_schema(self):
        conn = mem_conn()
        cols = db._table_columns(conn, "menu_options")
        self.assertIn("reply_text_if_form", cols)
        self.assertIn("action_if_form", cols)

    def test_ensure_schema_banco_antigo_sem_parent_id(self):
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
        db.ensure_schema(conn)
        cols = db._table_columns(conn, "menu_options")
        self.assertIn("parent_id", cols)
        self.assertIn("reply_text_if_form", cols)
        self.assertIn("action_if_form", cols)
        self.assertIn("menu_node_id", db._table_columns(conn, "chats"))


class EngineFormOpenTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _eng(self, conn) -> tuple[Engine, FakeSock]:
        sock = FakeSock()
        return Engine(conn, sock), sock

    def _leaf(self, conn) -> int:
        return insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Transferir",
            classification="tec",
            action="transfer_only",
            reply_text="",
        )

    async def test_consider_indexa_form_aberto(self):
        conn = mem_conn()
        eng, _ = self._eng(conn)
        await eng.consider(form_chat())
        self.assertIn(99, eng._open_forms)

    async def test_com_form_aberto_vale_a_acao_normal(self):
        """O override action_if_form foi removido: com formulario aberto vale a acao da folha."""
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        self._leaf(conn)
        eng, sock = self._eng(conn)
        remember(queue_chat())
        eng._open_forms[99] = form_chat()
        row = awaiting(conn)
        await eng.handle_reply(10, row, "1")
        cmds = [n for n, _ in sock.cmds]
        self.assertIn("move_chat", cmds)
        self.assertNotIn("Você já tem formulário de validação aberto.", sock.sent_texts())
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["classification"], "tec")
        details = [
            e["detail"]
            for e in conn.execute("SELECT detail FROM bot_events WHERE reason = 'classified'").fetchall()
        ]
        self.assertFalse(any("form_aberto" in (d or "") for d in details))

    async def test_sem_form_transfere(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        self._leaf(conn)
        eng, sock = self._eng(conn)
        remember(queue_chat())
        row = awaiting(conn)
        await eng.handle_reply(10, row, "1")
        cmds = [n for n, _ in sock.cmds]
        self.assertIn("move_chat", cmds)
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["status"], "closed")
        self.assertEqual(chat["classification"], "tec")


if __name__ == "__main__":
    unittest.main()

