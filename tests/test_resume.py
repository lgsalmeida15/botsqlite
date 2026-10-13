# -*- coding: utf-8 -*-
"""Decisão de resume de chats e saúde do Chromium/WS."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python.engine import Engine, MEM  # noqa: E402
from python.resume import resume_action, should_recycle  # noqa: E402
from tests.test_submenu import FakeSock, awaiting, insert_option, mem_conn, seed_queue_menu  # noqa: E402

OWN = "u:bot@claro.com"
TEC = "u:tec@claro.com"


def row(status: str, cid: int = 10) -> dict:
    return {"chat_id": cid, "status": status, "queue_address": "h:q1", "menu_node_id": 7, "retries": 1}


def active(*, present: bool) -> dict:
    parts = {
        TEC: {"address": TEC, "present": True},
    }
    if present:
        parts[OWN] = {"address": OWN, "present": True}
    else:
        parts[OWN] = {"address": OWN, "present": False}
    return {"chat_id": 10, "started_addr": TEC, "helpdesk_address": "h:q1", "participants": parts}


class ResumeActionTest(unittest.TestCase):
    def test_awaiting_reply_presente_rehydrate(self):
        self.assertEqual(resume_action(row("awaiting_reply"), active(present=True), OWN), "rehydrate")

    def test_awaiting_reply_ausente_rejoin_silent(self):
        self.assertEqual(resume_action(row("awaiting_reply"), active(present=False), OWN), "rejoin_silent")

    def test_awaiting_reply_sem_chat_ativo_stale(self):
        self.assertEqual(resume_action(row("awaiting_reply"), None, OWN), "stale")

    def test_monitoring_presente_e_ausente(self):
        self.assertEqual(resume_action(row("monitoring"), active(present=True), OWN), "rehydrate")
        self.assertEqual(resume_action(row("monitoring"), active(present=False), OWN), "rejoin_silent")

    def test_sem_row_ou_closed_skip(self):
        self.assertEqual(resume_action(None, active(present=True), OWN), "skip")
        self.assertEqual(resume_action(row("closed"), active(present=True), OWN), "skip")
        self.assertEqual(resume_action(row("left"), None, OWN), "skip")

    def test_wizard_script_e_aberto(self):
        self.assertEqual(resume_action(row("awaiting_script_answer"), active(present=True), OWN), "rehydrate")
        self.assertEqual(resume_action(row("awaiting_script_confirm"), active(present=False), OWN), "rejoin_silent")
        self.assertEqual(resume_action(row("awaiting_script_run"), active(present=True), OWN), "rehydrate")


class ShouldRecycleTest(unittest.TestCase):
    def test_saudavel(self):
        self.assertFalse(should_recycle(0, 10.0, fail_limit=3, stale_sec=180))

    def test_tres_timeouts_get_version(self):
        self.assertTrue(should_recycle(3, 10.0, fail_limit=3, stale_sec=180))

    def test_frame_velho(self):
        self.assertTrue(should_recycle(0, 181.0, fail_limit=3, stale_sec=180))

    def test_duas_falhas_nao_recicla(self):
        self.assertFalse(should_recycle(2, 10.0, fail_limit=3, stale_sec=180))


class ResumeSock(FakeSock):
    def __init__(self, chats=None, join_present: bool = True) -> None:
        super().__init__()
        self.chats = list(chats or [])
        self.join_present = join_present

    async def command(self, name, payload=None, **kwargs):
        payload = payload or {}
        self.cmds.append((name, payload))
        if name == "get_active_chats":
            return {"d": {"chats": self.chats}}
        if name == "join_chat" and self.join_present:
            return {
                "d": {
                    "present": True,
                    "address": self.own_address,
                    "chat_id": payload.get("chat_id"),
                }
            }
        return {"d": {}}


class ResumeOpenChatsTest(unittest.TestCase):
    def setUp(self) -> None:
        MEM.clear()
        self.conn = mem_conn()
        seed_queue_menu(self.conn)
        self.opt_id = insert_option(
            self.conn,
            menu_id=1,
            sort_order=1,
            label="A",
            classification="a",
            action="reply_and_close",
        )

    def tearDown(self) -> None:
        MEM.clear()

    def test_rehydrate_nao_join_nem_menu(self):
        awaiting(self.conn, 10, node=self.opt_id, retries=1)
        chat = active(present=True)
        chat["started_addr"] = TEC
        sock = ResumeSock(chats=[chat])
        eng = Engine(self.conn, sock)
        asyncio.run(eng.resume_open_chats())
        names = [n for n, _ in sock.cmds]
        self.assertNotIn("join_chat", names)
        self.assertEqual(sock.sent_texts(), [])
        self.assertEqual(MEM.get(10, {}).get("started_addr"), TEC)

    def test_rejoin_silent_join_sem_menu(self):
        awaiting(self.conn, 10, node=self.opt_id, retries=1)
        sock = ResumeSock(chats=[active(present=False)], join_present=True)
        eng = Engine(self.conn, sock)
        asyncio.run(eng.resume_open_chats())
        names = [n for n, _ in sock.cmds]
        self.assertIn("join_chat", names)
        self.assertEqual(sock.sent_texts(), [])
        row = self.conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(row["status"], "awaiting_reply")
        self.assertEqual(row["menu_node_id"], self.opt_id)
        self.assertEqual(row["retries"], 1)

    def test_stale_marca_left(self):
        awaiting(self.conn, 10, node=self.opt_id)
        sock = ResumeSock(chats=[])
        eng = Engine(self.conn, sock)
        asyncio.run(eng.resume_open_chats())
        row = self.conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(row["status"], "left")
        reasons = [
            r["reason"]
            for r in self.conn.execute("SELECT reason FROM bot_events WHERE chat_id = 10").fetchall()
        ]
        self.assertIn("resume_stale", reasons)

    def test_stale_encerra_a_rota_da_fila(self):
        """Chat que sumiu do TOA nao pode deixar a rota `waiting` para sempre.

        Sem isto a rota girava `move_chat` (e=10) num chat que o bot ja nao
        conduzia — o loop das rotas orfas.
        """
        awaiting(self.conn, 10, node=self.opt_id)
        self.conn.execute(
            "INSERT INTO pending_routes (kind, chat_id, queue_address, status, detail, next_try_at) "
            "VALUES ('chat', 10, 'h:q1', 'waiting', 'na fila', datetime('now'))"
        )
        eng = Engine(self.conn, ResumeSock(chats=[]))
        asyncio.run(eng.resume_open_chats())
        rota = self.conn.execute(
            "SELECT status, detail, next_try_at FROM pending_routes WHERE chat_id = 10"
        ).fetchone()
        self.assertEqual(rota["status"], "failed")
        self.assertEqual(rota["detail"], "chat sumiu do TOA")
        self.assertIsNone(rota["next_try_at"])


if __name__ == "__main__":
    unittest.main()
