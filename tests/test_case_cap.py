# -*- coding: utf-8 -*-
"""Cap real por CASO: rajada, afinidade, ledger, reserva e ciclo de vida."""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import MEM, Engine  # noqa: E402
from tests.test_form_open import form_chat  # noqa: E402
from tests.test_form_route import RouteSock, engine_with  # noqa: E402
from tests.test_submenu import insert_option, mem_conn, seed_queue_menu  # noqa: E402

TEC = "u:tec1@claro.com"
TEC2 = "u:tec2@claro.com"
ANA = "u:ana@claro.com"


class LoadFailSock(RouteSock):
    """get_active_chats e get_helpdesk_chats sem resposta."""

    async def command(self, name, payload=None, **kwargs):
        if name in ("get_active_chats", "get_helpdesk_chats"):
            self.cmds.append((name, payload or {}))
            return {"ok": True, "timeout": True}
        return await super().command(name, payload, **kwargs)


class FailMoveSock(RouteSock):
    """move_chat recusado pelo TOA."""

    async def command(self, name, payload=None, **kwargs):
        if name == "move_chat":
            self.cmds.append((name, payload or {}))
            raise RuntimeError("move_chat recusado")
        return await super().command(name, payload, **kwargs)


def monta(cap_casos=None, cap_form=None, sock=None):
    conn = mem_conn()
    seed_queue_menu(conn, with_team=True)
    if cap_casos is not None:
        conn.execute("INSERT INTO team_routing (team_id, cap_cases) VALUES (1, ?)", (cap_casos,))
    if cap_form is not None:
        conn.execute("INSERT INTO team_routing (team_id, cap_form) VALUES (1, ?)", (cap_form,))
    return conn, engine_with(conn, sock or RouteSock(online=["ana"]))


def form_de(tec, cid):
    return form_chat(
        chat_id=cid,
        started_addr=tec,
        participants={tec: {"address": tec, "name": tec, "present": True}},
    )


def eventos(conn):
    return [r["reason"] for r in conn.execute("SELECT reason FROM bot_events").fetchall()]


class RajadaTest(unittest.IsolatedAsyncioTestCase):
    """O cap do painel precisa valer em rajada (antes: N formularios = N entregas)."""

    def tearDown(self):
        MEM.clear()

    async def _rajada(self, n, **caps):
        conn, eng = monta(**caps)
        forms = [form_de("u:tec%d@claro.com" % (i + 1), 90 + i) for i in range(n)]
        await asyncio.gather(*[eng.consider(f) for f in forms])
        # Formulario nao entrega mais na hora: entra na fila e so e tentado no tick,
        # apos a janela minima (zerada pelo engine_with nos testes).
        await eng.tick_routes()
        return conn, eng

    async def test_um_formulario_por_vez_respeita_o_cap(self):
        _conn, eng = await self._rajada(1, cap_form=1)
        self.assertEqual(eng.sock.names().count("move_chat"), 1)

    async def test_rajada_de_tres_com_cap_form_um_entrega_um(self):
        conn, eng = await self._rajada(3, cap_form=1)
        self.assertEqual(eng.sock.names().count("move_chat"), 1)
        # Todo formulario emite queued ao chegar (3); quem nao e escolhido no
        # tick ganha mais um queued da 1a notificacao de posicao (2) = 5.
        self.assertEqual(eventos(conn).count("queued"), 5)

    async def test_rajada_de_seis_com_cap_form_um_entrega_um(self):
        conn, eng = await self._rajada(6, cap_form=1)
        self.assertEqual(eng.sock.names().count("move_chat"), 1)
        # 6 chegadas + 4 notificacoes de posicao (per_line=5 tenta so 5 no tick;
        # 1 entregue, 4 ficam sem analista e notificam a 1a posicao).
        self.assertEqual(eventos(conn).count("queued"), 10)

    async def test_rajada_de_quatro_com_cap_form_dois_entrega_dois(self):
        _conn, eng = await self._rajada(4, cap_form=2)
        self.assertEqual(eng.sock.names().count("move_chat"), 2)

    async def test_cap_de_casos_limita_solicitantes_distintos(self):
        conn, eng = await self._rajada(3, cap_casos=2)
        self.assertEqual(eng.sock.names().count("move_chat"), 2)
        self.assertEqual(len(db.open_assignments(conn)), 2)


class AfinidadeTest(unittest.IsolatedAsyncioTestCase):
    """Formulario e chat do MESMO solicitante sao um caso: mesmo operador."""

    def tearDown(self):
        MEM.clear()

    async def test_chat_do_mesmo_solicitante_vai_para_quem_tem_o_formulario(self):
        conn, eng = monta(cap_casos=1)
        await eng.consider(form_de(TEC, 99))
        await eng.tick_routes()
        self.assertEqual(len(db.open_assignments(conn)), 1)
        MEM.clear()
        MEM[10] = {
            "started_addr": TEC,
            "queue_address": "h:q1",
            "raw": {
                "chat_id": 10,
                "started_addr": TEC,
                "helpdesk_address": "h:q1",
                "participants": {TEC: {"address": TEC, "name": "Tec Um", "present": True}},
            },
        }
        insert_option(conn, menu_id=1, sort_order=1, label="Transferir", classification="tec", action="transfer_only")
        db.upsert_chat(conn, 10, queue_address="h:q1", status="awaiting_reply", last_prompt_at="2000-01-01 00:00:00")
        row = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        opt = db.menu_options(conn, "h:q1", None)[0]
        await eng.apply_option(10, row, opt)
        self.assertEqual(eng.sock.last("invite_to_chat")["address"], ANA)
        self.assertIn("transferred", eventos(conn))

    async def test_solicitante_novo_respeita_o_cap(self):
        conn, eng = monta(cap_casos=1)
        await eng.consider(form_de(TEC, 99))
        await eng.consider(form_de(TEC2, 98))
        await eng.tick_routes()
        self.assertEqual(eng.sock.names().count("move_chat"), 1)
        # 2 chegadas + 1 notificacao de posicao do que nao foi escolhido no tick.
        self.assertEqual(eventos(conn).count("queued"), 3)


class CasoUnicoTest(unittest.IsolatedAsyncioTestCase):
    """Form e chat do mesmo tecnico contam UM caso na carga."""

    def tearDown(self):
        MEM.clear()

    async def test_dois_objetos_do_mesmo_solicitante_contam_um_caso(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        db.assign_open(conn, "form", 99, "h:q1", 1, ANA, "a:" + TEC, TEC, "Tec", "address")
        db.assign_open(conn, "chat", 10, "h:q1", 1, ANA, "a:" + TEC, TEC, "Tec", "address")
        eng = Engine(conn, RouteSock())
        casos, por_tipo, _info = await eng._capacidade(db.team_members(conn, 1), "form", "h:q1")
        self.assertEqual(casos[ANA], 1)
        self.assertEqual(por_tipo[ANA], 1)


class FalhaToaTest(unittest.IsolatedAsyncioTestCase):
    """TOA sem resposta nao pode liberar geral: ledger e reserva seguram."""

    def tearDown(self):
        MEM.clear()

    async def test_ledger_segura_o_cap_com_toa_mudo(self):
        _conn, eng = monta(cap_form=1, sock=LoadFailSock(online=["ana"]))
        await eng.consider(form_de(TEC, 99))
        await eng.consider(form_de(TEC2, 98))
        await eng.tick_routes()
        self.assertEqual(eng.sock.names().count("move_chat"), 1)


class ReservaTest(unittest.IsolatedAsyncioTestCase):
    """Falha de entrega devolve a reserva; caso encerrado libera a vaga na hora."""

    def tearDown(self):
        MEM.clear()

    async def test_entrega_que_falha_devolve_a_reserva(self):
        _conn, eng = monta(cap_form=1, sock=FailMoveSock(online=["ana"]))
        await eng.consider(form_de(TEC, 99))
        await eng.tick_routes()
        self.assertEqual(len(eng._reserved()), 0)
        await eng.consider(form_de(TEC2, 98))
        await eng.tick_routes()
        self.assertEqual(eng.sock.names().count("move_chat"), 2)

    async def test_caso_encerrado_libera_a_vaga_na_hora(self):
        conn, eng = monta(cap_casos=1)
        await eng.consider(form_de(TEC, 99))
        await eng.tick_routes()
        await eng.on_note("chat_removed", {"chat_id": 99})
        self.assertEqual(len(db.open_assignments(conn)), 0)
        self.assertEqual(len(eng._reserved()), 0)
        await eng.consider(form_de(TEC2, 98))
        await eng.tick_routes()
        self.assertEqual(eng.sock.names().count("move_chat"), 2)

    async def test_ttl_fecha_caso_orfao(self):
        conn, eng = monta(cap_casos=1)
        db.assign_open(conn, "form", 99, "h:q1", 1, ANA, "a:" + TEC, TEC, "Tec", "address")
        eng.cfg["route_assign_ttl_sec"] = "3600"
        conn.execute("UPDATE route_assignments SET assigned_at = datetime('now', '-2 hours')")
        await eng.tick_timeouts()
        self.assertEqual(len(db.open_assignments(conn)), 0)


if __name__ == "__main__":
    unittest.main()
