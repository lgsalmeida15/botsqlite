# -*- coding: utf-8 -*-
"""FASE A - dupla entrega: uma unica operacao ativa por (kind, chat_id).

Falhas comprovadas no banco real: dois `move_chat` no mesmo formulario, convites
para dois analistas diferentes no mesmo chat e ledger sobrescrito depois da
confirmacao.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import MEM  # noqa: E402
from tests.test_delivery_guard import prepara_chat_classificado  # noqa: E402
from tests.test_form_open import form_chat  # noqa: E402
from tests.test_form_route import RouteSock, add_member, engine_with, events, routes  # noqa: E402
from tests.test_submenu import mem_conn, seed_queue_menu  # noqa: E402

ANA = "u:ana@claro.com"
BIA = "u:bia@claro.com"


class DuplaEntregaTest(unittest.IsolatedAsyncioTestCase):
    """A2: claim atomico + alvo imutavel + guarda de caso ja atribuido."""

    def tearDown(self):
        MEM.clear()

    async def test_dois_eventos_do_mesmo_form_entregam_uma_vez(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        add_member(conn, BIA, "Bia", sort_order=1)

        class SlowSock(RouteSock):
            async def command(self, name, payload=None, **kwargs):
                if name in ("move_chat", "invite_to_chat"):
                    await asyncio.sleep(0.05)
                return await super().command(name, payload, **kwargs)

        sock = SlowSock(online=["ana", "bia"])
        eng = engine_with(conn, sock)
        eng.cfg["form_route_retry_interval_sec"] = "0"
        await asyncio.gather(eng.consider(form_chat()), eng.consider(form_chat()))
        await eng.tick_routes()
        self.assertEqual(sock.names().count("move_chat"), 1)
        self.assertEqual(sock.names().count("invite_to_chat"), 1)
        self.assertEqual(len(db.open_assignments(conn)), 1)
        self.assertEqual(len(routes(conn, "waiting")), 0)

    async def test_form_nao_move_duas_vezes_quando_o_convite_falha(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        add_member(conn, BIA, "Bia", sort_order=1)

        class InviteFailSock(RouteSock):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.invites = 0

            async def command(self, name, payload=None, **kwargs):
                if name == "invite_to_chat":
                    self.invites += 1
                    self.cmds.append((name, payload or {}))
                    if self.invites == 1:
                        raise RuntimeError("invite recusado")
                return await super().command(name, payload, **kwargs)

        sock = InviteFailSock(online=["ana", "bia"])
        eng = engine_with(conn, sock)
        eng.cfg["form_route_retry_interval_sec"] = "0"
        await eng.consider(form_chat())
        await eng.tick_routes()  # 1a tentativa: move + convite (falha, agenda retry)
        conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()  # 2a tentativa: so reconvida, nao move de novo
        self.assertEqual(sock.names().count("move_chat"), 1)
        self.assertEqual(sock.invites, 2)
        self.assertEqual(routes(conn, "done")[0]["invite_target"], ANA)

    async def test_entrega_de_chat_nao_repete_apos_confirmacao(self):
        conn = mem_conn()
        prepara_chat_classificado(conn)
        sock = RouteSock(online=["ana"])
        eng = engine_with(conn, sock)
        await eng.tick_routes()
        # Simula a queda entre a confirmacao (ledger gravado) e o `leave`: a rota
        # volta a `waiting` e o chat continua `classified`.
        conn.execute(
            "UPDATE pending_routes SET status = 'waiting', next_try_at = datetime('now', '-1 seconds')"
        )
        conn.execute("UPDATE chats SET status = 'classified' WHERE chat_id = 10")
        await eng.tick_routes()
        self.assertEqual(sock.names().count("move_chat"), 1)
        self.assertEqual(sock.names().count("invite_to_chat"), 1)
        self.assertIn("delivery_duplicate_blocked", events(conn))
        self.assertEqual(len(db.open_assignments(conn)), 1)
        # E o bot termina o que faltou: sai do chat entregue.
        chat = conn.execute("SELECT status FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["status"], "closed")
        self.assertEqual(sock.names().count("leave_chat"), 2)

    async def test_crash_antes_da_confirmacao_mantem_o_mesmo_alvo(self):
        """Queda entre o convite e o ledger: a retentativa NAO troca de analista."""
        conn = mem_conn()
        prepara_chat_classificado(conn, moved=True)
        conn.execute("INSERT INTO team_routing (team_id, last_assigned_chat) VALUES (1, ?)", (ANA,))
        add_member(conn, BIA, "Bia", sort_order=1)
        sock = RouteSock(online=["ana", "bia"])
        eng = engine_with(conn, sock)
        await eng.tick_routes()
        conn.execute("DELETE FROM route_assignments")
        conn.execute(
            "UPDATE pending_routes SET status = 'waiting', next_try_at = datetime('now', '-1 seconds')"
        )
        conn.execute("UPDATE chats SET status = 'classified' WHERE chat_id = 10")
        await eng.tick_routes()
        convidados = [p["address"] for n, p in sock.cmds if n == "invite_to_chat"]
        self.assertEqual(len(convidados), 2)
        self.assertEqual(convidados[0], convidados[1])
        self.assertEqual(convidados[0], routes(conn)[0]["invite_target"])
        self.assertEqual(sock.names().count("move_chat"), 0)


if __name__ == "__main__":
    unittest.main()
