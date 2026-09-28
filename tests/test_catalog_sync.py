# -*- coding: utf-8 -*-
"""Catálogo sob demanda: bot_jobs, discover_catalog, cadastro de fila desconhecida."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import Engine  # noqa: E402
from tests.test_submenu import FakeSock, mem_conn, seed_queue_menu  # noqa: E402


class CatalogSyncTest(unittest.IsolatedAsyncioTestCase):
    def test_request_claim_finish_fluxo(self):
        conn = mem_conn()
        jid = db.request_catalog_sync(conn)
        self.assertGreater(jid, 0)
        job = db.claim_pending_job(conn)
        self.assertIsNotNone(job)
        self.assertEqual(int(job["id"]), jid)
        self.assertEqual(job["status"], "running")
        # segundo claim não devolve nada (só um job)
        self.assertIsNone(db.claim_pending_job(conn))
        db.finish_job(conn, jid, "done", "ok")
        row = conn.execute("SELECT * FROM bot_jobs WHERE id = ?", (jid,)).fetchone()
        self.assertEqual(row["status"], "done")

    async def test_process_pending_jobs_dispara_catalog(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        sock = FakeSock()
        eng = Engine(conn, sock)
        db.request_catalog_sync(conn)
        await eng.process_pending_jobs()
        job = conn.execute("SELECT * FROM bot_jobs").fetchone()
        self.assertEqual(job["status"], "done")
        names = [n for n, _ in sock.cmds]
        self.assertIn("get_helpdesk_details", names)
        self.assertIn("get_contact_list", names)
        self.assertIn("search_contact", names)
        reasons = [e["reason"] for e in conn.execute("SELECT reason FROM bot_events").fetchall()]
        self.assertIn("catalog_synced", reasons)

    async def test_consider_fila_desconhecida_cadastra(self):
        conn = mem_conn()
        seed_queue_menu(conn)  # apenas h:q1 habilitada
        sock = FakeSock()
        eng = Engine(conn, sock)
        chat = {"chat_id": 500, "helpdesk_address": "h:nova", "participants": []}
        await eng.consider(chat)
        row = conn.execute("SELECT * FROM queues WHERE address = 'h:nova'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["enabled"], 0)
        reasons = [e["reason"] for e in conn.execute("SELECT reason FROM bot_events").fetchall()]
        self.assertIn("queue_off", reasons)

    async def test_consider_fila_sem_time_ativo_nao_assume(self):
        """Fila habilitada sem time ATIVO: nao entra (era o closed_no_agent).

        Antes o bot assumia, mandava o menu e so descobria na entrega que nao
        havia time elegivel — o chat acabava encerrado sem transferir ninguem.
        """
        conn = mem_conn()
        seed_queue_menu(conn, with_team=False)  # fila habilitada + menu, sem time
        conn.execute(
            "INSERT INTO menu_options (menu_id, sort_order, label, classification, action)"
            " VALUES (1, 1, 'A', 'a', 'reply_and_close')"
        )
        sock = FakeSock()
        eng = Engine(conn, sock)
        await eng.consider({"chat_id": 501, "helpdesk_address": "h:q1", "participants": []})
        reasons = [e["reason"] for e in conn.execute("SELECT reason FROM bot_events").fetchall()]
        self.assertIn("queue_no_team", reasons)
        self.assertNotIn("assumed", reasons)
        self.assertNotIn("menu_sent", reasons)

    async def test_consider_fila_sem_menu_ativo_nao_assume(self):
        """Menu desativado no painel = fila habilitada sem opcoes: nao entra."""
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        conn.execute("UPDATE menus SET active = 0")
        conn.execute(
            "INSERT INTO menu_options (menu_id, sort_order, label, classification, action)"
            " VALUES (1, 1, 'A', 'a', 'reply_and_close')"
        )
        sock = FakeSock()
        eng = Engine(conn, sock)
        await eng.consider({"chat_id": 502, "helpdesk_address": "h:q1", "participants": []})
        reasons = [e["reason"] for e in conn.execute("SELECT reason FROM bot_events").fetchall()]
        self.assertIn("queue_no_menu", reasons)
        self.assertNotIn("assumed", reasons)

    async def test_consider_menu_sem_opcoes_nao_assume(self):
        """Menu ativo porem sem nenhuma opcao: mesmo portao (nao entra)."""
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)  # sem menu_options
        sock = FakeSock()
        eng = Engine(conn, sock)
        await eng.consider({"chat_id": 503, "helpdesk_address": "h:q1", "participants": []})
        reasons = [e["reason"] for e in conn.execute("SELECT reason FROM bot_events").fetchall()]
        self.assertIn("queue_no_menu", reasons)
        self.assertNotIn("assumed", reasons)

    def test_upsert_preserva_origem_manual(self):
        conn = mem_conn()
        db.upsert_queue(conn, "h:q1", "Q1")
        db.upsert_queue_operator(conn, "h:q1", "u:ana@claro.com", "Ana Manual", "manual")
        db.upsert_queue_operator(conn, "h:q1", "u:ana@claro.com", "Ana TOA", "toa")
        row = conn.execute(
            "SELECT name, source FROM queue_operators WHERE queue_address = 'h:q1' AND address = 'u:ana@claro.com'"
        ).fetchone()
        self.assertEqual(row["source"], "manual")

    def test_ingest_search_contact_grava_source_toa(self):
        with tempfile.TemporaryDirectory() as td:
            dbp = str(Path(td) / "sync.sqlite")
            with patch.dict(os.environ, {"SQLITE_PATH": dbp}, clear=False):
                conn = db.connect()
                db.upsert_queue(conn, "h:q9", "Q9")
                db.upsert_queue_operator(conn, "h:q9", "u:bob@claro.com", "Bob", "manual")
                conn.close()
                raw = {
                    "h:q9": {
                        "name": "Q9",
                        "operators": [
                            {"address": "u:bob@claro.com", "name": "Bob TOA"},
                            {"address": "u:novo@claro.com", "name": "Novo"},
                        ],
                    }
                }
                n_ops, n_q = db.ingest_search_contact(raw)
                self.assertGreaterEqual(n_ops, 1)
                conn = db.connect()
                row = conn.execute(
                    "SELECT name, source FROM queue_operators WHERE queue_address = 'h:q9' AND address = 'u:bob@claro.com'"
                ).fetchone()
                self.assertEqual(row["source"], "manual")  # manual preservado
                row2 = conn.execute(
                    "SELECT source FROM queue_operators WHERE queue_address = 'h:q9' AND address = 'u:novo@claro.com'"
                ).fetchone()
                self.assertEqual(row2["source"], "toa")
                conn.close()


if __name__ == "__main__":
    unittest.main()
