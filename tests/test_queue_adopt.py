# -*- coding: utf-8 -*-
"""Analista entra sozinho no chat que esta na fila: o caso e dele e o bot sai."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import MEM, Engine, remember  # noqa: E402
from tests.test_form_route import ANA, TEC, RouteSock, events, routes  # noqa: E402
from tests.test_queue import chat_row, engine_cfg, seed_ask_leaf  # noqa: E402
from tests.test_submenu import awaiting, mem_conn, seed_queue_menu  # noqa: E402


class ChatAdotadoNaFilaTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _eng(self, online=None):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        seed_ask_leaf(conn)
        sock = RouteSock(online=online)
        return engine_cfg(conn, sock), conn, sock

    async def _entra_na_fila(self, eng, conn, cid: int = 10):
        await eng.handle_reply(cid, awaiting(conn, cid=cid), "1")
        await eng.handle_reply(cid, chat_row(conn, cid), "protocolo")
        await eng.handle_reply(cid, chat_row(conn, cid), "1")

    def _marca_participantes(self, cid: int, presente: str) -> None:
        remember({
            "chat_id": cid,
            "helpdesk_address": "h:q1",
            "started_addr": TEC,
            "participants": {presente: {"present": True}},
        })

    async def test_analista_que_entra_na_fila_adota_o_chat(self):
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        self.assertEqual(len(routes(conn, "waiting")), 1)

        await eng.on_message({
            "chat_id": 10,
            "system_message": "user_joined",
            "author": ANA,
            "participant": {"address": ANA},
        })

        self.assertIn("chat_adopted", events(conn))
        self.assertEqual(routes(conn, "waiting"), [], "a rota sai da fila")
        self.assertEqual(routes(conn, "done")[0]["detail"][:8], "adotado:")
        caso = conn.execute("SELECT * FROM route_assignments WHERE chat_id = 10").fetchone()
        self.assertEqual(caso["assignee"], ANA)
        self.assertIsNone(caso["closed_at"], "o caso passa a contar para ele")
        self.assertEqual(caso["identity_source"], "adocao")
        self.assertEqual(chat_row(conn, 10)["status"], "closed")
        self.assertIn("leave_chat", sock.names(), "o bot sai do chat")

    async def test_aviso_de_posicao_confere_quem_esta_dentro(self):
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        rid = int(routes(conn, "waiting")[0]["id"])
        enviados = len(sock.sent_texts())
        # O payload mais recente do chat ja mostra a analista presente na fila.
        self._marca_participantes(10, ANA)

        await eng._queue_notify(10, rid, "h:q1")

        self.assertIn("chat_adopted", events(conn))
        self.assertNotIn("queue_position", events(conn))
        self.assertEqual(len(sock.sent_texts()), enviados, "nao manda aviso de fila")
        self.assertEqual(len(routes(conn, "waiting")), 0)
        self.assertIn("leave_chat", sock.names())

    async def test_tecnico_entrando_nao_adota(self):
        eng, conn, _sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        await eng.on_message({
            "chat_id": 10,
            "system_message": "user_joined",
            "author": TEC,
            "participant": {"address": TEC},
        })
        self.assertNotIn("chat_adopted", events(conn))
        self.assertEqual(len(routes(conn, "waiting")), 1, "segue na fila")

    async def test_terceiro_fora_do_time_nao_adota(self):
        eng, conn, _sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        await eng.on_message({
            "chat_id": 10,
            "system_message": "user_joined",
            "author": "u:supervisor@claro.com",
            "participant": {"address": "u:supervisor@claro.com"},
        })
        self.assertNotIn("chat_adopted", events(conn))
        self.assertEqual(len(routes(conn, "waiting")), 1)

    async def test_chat_fora_da_fila_nao_adota(self):
        """Chat que nao esta `classified` (ex.: ja entregue) nao muda nada."""
        eng, conn, _sock = self._eng(online=[ANA])
        db.upsert_chat(conn, 10, queue_address="h:q1", status="closed")
        await eng.on_message({
            "chat_id": 10,
            "system_message": "user_joined",
            "author": ANA,
            "participant": {"address": ANA},
        })
        self.assertNotIn("chat_adopted", events(conn))

    async def test_formulario_na_fila_nao_adota(self):
        eng, conn, sock = self._eng(online=[])
        db.assign_open(conn, "form", 99, "h:q1", 1, TEC, "a:" + TEC, TEC, "Tec")
        rid = db.enqueue_route(conn, "form", 99, "h:q1", 1, detail="na fila", delay_sec=0)
        enviados = len(sock.sent_texts())
        await eng._queue_notify(99, rid, "h:q1")
        self.assertNotIn("chat_adopted", events(conn))
        self.assertEqual(len(sock.sent_texts()), enviados, "formulario nunca recebe texto")


if __name__ == "__main__":
    unittest.main()
