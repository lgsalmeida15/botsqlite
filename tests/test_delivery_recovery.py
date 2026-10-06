# -*- coding: utf-8 -*-
"""FASE A - ledger fechado com motivo e recuperacao de caso parado.

Falhas comprovadas no banco real: 3 de 3 entregas com `closed_at IS NULL` por
16-20h, rota encerrada em silencio ("fila sem time ativo" sem evento) e chat
`classified` sem nenhuma rota, sem timeout e sem recuperacao.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import MEM  # noqa: E402
from tests.test_delivery_guard import ANA, TEC, lembra, prepara_chat_classificado  # noqa: E402
from tests.test_form_open import form_chat  # noqa: E402
from tests.test_form_route import RouteSock, engine_with, events, routes  # noqa: E402
from tests.test_submenu import mem_conn, seed_queue_menu  # noqa: E402

BOT = "u:bot@claro.com"


class SockAtivo(RouteSock):
    """Socket com a lista de chats ativos controlada pelo teste."""

    def __init__(self, chats, **kw):
        super().__init__(**kw)
        self.ativos = list(chats)

    async def command(self, name, payload=None, **kwargs):
        if name == "get_active_chats":
            self.cmds.append((name, payload or {}))
            return {"d": {"chats": list(self.ativos)}}
        return await super().command(name, payload, **kwargs)


def chat_ativo(cid: int) -> dict:
    return {
        "chat_id": cid,
        "started_addr": TEC,
        "helpdesk_address": "h:q1",
        "participants": {
            TEC: {"address": TEC, "present": True},
            BOT: {"address": BOT, "present": True},
        },
    }


class FechamentoDoLedgerTest(unittest.IsolatedAsyncioTestCase):
    """A1: o caso aberto nunca mais fica pendurado sem motivo."""

    def tearDown(self):
        MEM.clear()

    def _caso_aberto(self, conn, cid: int = 60):
        seed_queue_menu(conn, with_team=True)
        db.upsert_chat(conn, cid, queue_address="h:q1", status="classified", last_prompt_at="2000-01-01 00:00:00")
        db.assign_open(conn, "chat", cid, "h:q1", 1, ANA, "a:" + TEC, TEC, "Tec", "address")
        lembra(cid)

    async def test_chat_ausente_no_boot_fecha_com_motivo(self):
        conn = mem_conn()
        self._caso_aberto(conn)
        eng = engine_with(conn, SockAtivo([]))
        eng.cfg["case_stale_confirm_sec"] = "0"
        await eng.resume_open_chats()
        linha = conn.execute("SELECT * FROM route_assignments WHERE chat_id = 60").fetchone()
        self.assertIsNotNone(linha["closed_at"])
        self.assertEqual(linha["close_reason"], "stale_boot")
        self.assertIn("case_closed", events(conn))

    async def test_caso_ainda_vivo_no_toa_nao_e_fechado(self):
        conn = mem_conn()
        self._caso_aberto(conn)
        eng = engine_with(conn, SockAtivo([chat_ativo(60)]))
        eng.cfg["case_stale_confirm_sec"] = "0"
        await eng.resume_open_chats()
        self.assertEqual(len(db.open_assignments(conn)), 1)

    async def test_entrega_recente_nao_e_fechada_pelo_boot(self):
        conn = mem_conn()
        self._caso_aberto(conn)
        eng = engine_with(conn, SockAtivo([]))
        eng.cfg["case_stale_confirm_sec"] = "3600"
        await eng.resume_open_chats()
        self.assertEqual(len(db.open_assignments(conn)), 1)

    async def test_chat_removido_fecha_com_motivo(self):
        conn = mem_conn()
        self._caso_aberto(conn)
        eng = engine_with(conn, RouteSock())
        await eng.on_note("chat_removed", {"chat_id": 60})
        linha = conn.execute("SELECT * FROM route_assignments WHERE chat_id = 60").fetchone()
        self.assertEqual(linha["close_reason"], "chat_removed")


class RecuperacaoTest(unittest.IsolatedAsyncioTestCase):
    """A4: nada morre em silencio (rota sem time, classificado sem rota)."""

    def tearDown(self):
        MEM.clear()

    async def test_rota_sem_time_emite_evento_e_aplica_fallback(self):
        conn = mem_conn()
        prepara_chat_classificado(conn)
        conn.execute("DELETE FROM teams WHERE id = 1")
        eng = engine_with(conn, RouteSock(online=["ana"]))
        await eng.tick_routes()
        self.assertIn("delivery_failed_no_team", events(conn))
        self.assertIn("closed_no_agent", events(conn))
        chat = conn.execute("SELECT status FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["status"], "closed_no_agent")

    async def test_classificado_sem_rota_e_recuperado_e_entregue(self):
        conn = mem_conn()
        prepara_chat_classificado(conn, com_rota=False)
        sock = RouteSock(online=["ana"])
        eng = engine_with(conn, sock)
        self.assertEqual(routes(conn), [])
        await eng.tick_routes()
        self.assertIn("delivery_recovered", events(conn))
        self.assertEqual(sock.names().count("invite_to_chat"), 1)
        self.assertEqual(len(db.open_assignments(conn)), 1)

    async def test_rota_falha_nao_e_reciclada_pela_recuperacao(self):
        conn = mem_conn()
        prepara_chat_classificado(conn)
        conn.execute("UPDATE pending_routes SET status = 'failed', detail = 'tentativas esgotadas'")
        sock = RouteSock(online=["ana"])
        eng = engine_with(conn, sock)
        await eng.tick_routes()
        self.assertNotIn("delivery_recovered", events(conn))
        self.assertNotIn("move_chat", sock.names())
        self.assertEqual(len(routes(conn, "waiting")), 0)

    async def test_orfao_reprocessado_pelo_painel_volta_a_entregar(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        sock = RouteSock(online=[])
        eng = engine_with(conn, sock)
        # Rota parada de verdade: com a FILA desligada o formulario esgota as
        # tentativas e fica em 'failed' (com a fila ligada ele so espera a vez).
        eng.cfg["queue_wait_enabled"] = "false"
        eng.cfg["form_route_attempts"] = "1"
        eng.cfg["form_route_retry_interval_sec"] = "0"
        await eng.consider(form_chat())
        for _ in range(3):
            conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
            await eng.tick_routes()
        paradas = db.stuck_routes(conn)
        self.assertEqual(len(paradas), 1)
        self.assertTrue(db.reopen_route(conn, int(paradas[0]["id"])))
        sock.online = ["ana"]
        conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        self.assertEqual(sock.names().count("invite_to_chat"), 1)
        self.assertEqual(len(db.open_assignments(conn)), 1)


if __name__ == "__main__":
    unittest.main()

