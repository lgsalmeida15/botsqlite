# -*- coding: utf-8 -*-
"""Autor do chat/form no evento: bot_events grava author_addr/author_name.

A coluna AUTOR da aba Atividade nasce aqui: o `_emit` resolve a identidade na
ordem ledger aberto -> rota pendente -> indice de formularios e grava o par
(endereco, nome). Sem identidade confiavel o par fica vazio (o painel mostra
endereco ou "-") — nunca inventa nome.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import Engine  # noqa: E402
from tests.test_form_route import ANA, TEC  # noqa: E402
from tests.test_submenu import FakeSock, mem_conn, seed_queue_menu  # noqa: E402

CID = 42


def ultimo_evento(conn) -> dict:
    return conn.execute(
        "SELECT reason, author_addr, author_name FROM bot_events ORDER BY id DESC LIMIT 1"
    ).fetchone()


class EventAuthorTest(unittest.TestCase):
    def _eng(self, conn) -> Engine:
        return Engine(conn, FakeSock())

    def test_autor_vem_do_ledger_aberto(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        db.assign_open(
            conn, "form", CID, "h:q1", 1, ANA,
            "a:" + TEC, TEC, "Tec um", "address",
        )
        self._eng(conn)._emit("form_state", CID, "h:q1", "estado=aberto")
        ev = ultimo_evento(conn)
        self.assertEqual(ev["author_addr"], TEC)
        self.assertEqual(ev["author_name"], "Tec um")

    def test_ledger_fechado_nao_conta(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        db.assign_open(
            conn, "form", CID, "h:q1", 1, ANA,
            "a:" + TEC, TEC, "Tec um", "address",
        )
        db.assign_close(conn, CID, "form_done")
        self._eng(conn)._emit("form_closed", CID, "h:q1", "motivo=x")
        ev = ultimo_evento(conn)
        self.assertEqual(ev["author_addr"], "")
        self.assertEqual(ev["author_name"], "")

    def test_autor_vem_da_rota_pendente(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        db.enqueue_route(
            conn, "chat", CID, "h:q1", 1, detail="", delay_sec=0,
        )
        conn.execute(
            "UPDATE pending_routes SET requester_addr = ? WHERE chat_id = ?",
            (TEC, CID),
        )
        self._eng(conn)._emit("queued", CID, "h:q1", "sem analista")
        ev = ultimo_evento(conn)
        self.assertEqual(ev["author_addr"], TEC)

    def test_autor_vem_do_indice_de_forms(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        eng = self._eng(conn)
        eng._open_forms[CID] = {
            "chat_id": CID,
            "mode": "message_scenario",
            "helpdesk_address": "h:q1",
            "started_addr": TEC,
            "started_name": "Tec Dois",
        }
        eng._emit("form_indexed", CID, "h:q1", "origem=closure")
        ev = ultimo_evento(conn)
        self.assertEqual(ev["author_addr"], TEC)
        self.assertEqual(ev["author_name"], "Tec Dois")

    def test_sem_identidade_grava_vazio(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        self._eng(conn)._emit("left", CID, "h:q1", "chat_removed")
        ev = ultimo_evento(conn)
        self.assertEqual(ev["author_addr"], "")
        self.assertEqual(ev["author_name"], "")

    def test_evento_sem_chat_nao_explode(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        self._eng(conn)._emit("analyst_load", None, None, "motivo=timer")
        ev = ultimo_evento(conn)
        self.assertEqual(ev["reason"], "analyst_load")
        self.assertEqual(ev["author_addr"], "")

    def test_migracao_adiciona_colunas_em_base_antiga(self):
        conn = mem_conn()
        conn.execute("DROP TABLE bot_events")
        conn.execute(
            """CREATE TABLE bot_events (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 at VARCHAR(191) NOT NULL DEFAULT (datetime('now')),
                 chat_id INTEGER,
                 queue_address VARCHAR(191),
                 queue_enabled INTEGER,
                 reason VARCHAR(191) NOT NULL,
                 detail TEXT NOT NULL DEFAULT ''
               )"""
        )
        db.ensure_schema(conn)
        cols = db._table_columns(conn, "bot_events")
        self.assertIn("author_addr", cols)
        self.assertIn("author_name", cols)
        # Idempotente: rodar de novo nao quebra.
        db.ensure_schema(conn)
