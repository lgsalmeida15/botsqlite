# -*- coding: utf-8 -*-
"""Carga por analista (aba Carga) e fechamento do formulario por AUSENCIA no TOA.

O TOA e a fonte autoritativa: quando o analista resolve o formulario, ele
desaparece da lista do front. O bot le as listas (`in_process` + `pending`) a
cada `load_poll_sec`, grava a medicao por analista e, ao ver o mesmo formulario
ausente por `form_absent_readings` leituras bem sucedidas, fecha o caso
(`form_resolvido_toa`) liberando a vaga do analista.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import MEM, Engine  # noqa: E402
from tests.test_form_route import ANA, TEC, add_member, events, routes  # noqa: E402
from tests.test_submenu import mem_conn, seed_queue_menu  # noqa: E402

BIA = "u:bia@claro.com"
FORM_ID = 99
CHAT_ID = 10


class LoadSock:
    """Socket fake com as listas do TOA controlaveis por teste."""

    own_address = "u:bot@claro.com"

    def __init__(self, in_process=None, pending=None, online=None, timeout=False, fail_tipos=()):
        self.cmds: list[tuple[str, dict]] = []
        self.in_process = list(in_process or [])
        self.pending = list(pending or [])
        self.online = list(online or [])
        self.timeout = bool(timeout)
        self.fail_tipos = {str(t) for t in fail_tipos}

    async def command(self, name, payload=None, **kwargs):
        payload = payload or {}
        self.cmds.append((name, payload))
        if name == "get_online_users":
            if self.timeout:
                return {"timeout": True}
            return {"d": {"result": list(self.online)}}
        if name == "get_active_chats":
            if self.timeout:
                return {"timeout": True}
            return {"d": {"chats": []}}
        if name == "get_helpdesk_chats":
            tipo = str(payload.get("chat_type") or "")
            if self.timeout or tipo in self.fail_tipos:
                return {"timeout": True}
            lista = self.in_process if tipo == "in_process" else self.pending
            return {"d": {"chats": [dict(c) for c in lista]}}
        return {"d": {}}

    def names(self) -> list[str]:
        return [n for n, _ in self.cmds]

    def sent_texts(self) -> list[str]:
        return [p["message"]["text"] for n, p in self.cmds if n == "send_message"]


def chat_toa(cid: int = CHAT_ID, tec: str = TEC, analista: str = ANA) -> dict:
    """Chat de fila em atendimento: o tecnico pediu, o analista atende."""
    return {
        "chat_id": cid,
        "helpdesk_address": "h:q1",
        "participants": {
            tec: {"address": tec, "present": True},
            analista: {"address": analista, "name": "Analista", "present": True, "role_desc": "Moderator"},
        },
    }


def form_toa(cid: int = FORM_ID, tec: str = TEC, analista: str = ANA) -> dict:
    """Formulario de validacao em atendimento (o mesmo caso do chat)."""
    return {
        "chat_id": cid,
        "mode": "message_scenario",
        "person": "Message Scenario Agent",
        "helpdesk_address": "h:q1",
        "participants": {
            tec: {"address": tec, "present": True},
            analista: {"address": analista, "name": "Analista", "present": True},
        },
    }


def base_engine(conn, sock, **cfg) -> Engine:
    eng = Engine(conn, sock)
    for k, v in cfg.items():
        eng.cfg[k] = v
    return eng


class LoadTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _conn(self, *, bia: bool = False):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        if bia:
            add_member(conn, BIA, "Bia")
        return conn

    def _caso_aberto(self, conn, kind: str = "form", cid: int = FORM_ID) -> None:
        db.assign_open(conn, kind, cid, "h:q1", 1, ANA, "a:" + TEC, TEC, "Tec", "address")

    async def test_snapshot_grava_a_leitura_por_analista(self):
        conn = self._conn()
        sock = LoadSock(in_process=[chat_toa(), form_toa()], online=["ana"])
        eng = base_engine(conn, sock)
        total = await eng.snapshot_load("teste")
        self.assertEqual(total, 1)
        linhas = db.load_rows(conn)
        self.assertEqual(len(linhas), 1)
        linha = linhas[0]
        self.assertEqual(linha["address"], ANA)
        self.assertEqual(int(linha["online"]), 1)
        self.assertEqual(int(linha["chats_toa"]), 1)
        self.assertEqual(int(linha["forms_toa"]), 1)
        self.assertEqual(int(linha["toa_ok"]), 1)
        # chat + formulario do MESMO tecnico = 1 caso (a mesma regra da capacidade).
        self.assertEqual(int(linha["casos_total"]), 1)
        self.assertEqual(len(db.load_history(conn, ANA)), 1)
        self.assertIn("analyst_load", events(conn))

    async def test_leitura_conta_so_quem_esta_presente(self):
        conn = self._conn(bia=True)
        sock = LoadSock(in_process=[chat_toa()], online=["ana"])
        eng = base_engine(conn, sock)
        await eng.snapshot_load("teste")
        por_addr = {r["address"]: r for r in db.load_rows(conn)}
        self.assertEqual(int(por_addr[ANA]["chats_toa"]), 1)
        self.assertEqual(int(por_addr[BIA]["chats_toa"]), 0)
        self.assertEqual(int(por_addr[ANA]["online"]), 1)
        self.assertEqual(int(por_addr[BIA]["online"]), 0)

    async def test_ledger_e_reservas_aparecem_na_linha(self):
        conn = self._conn()
        self._caso_aberto(conn)
        eng = base_engine(conn, LoadSock(in_process=[form_toa()], online=["ana"]))
        eng._reserve_add(ANA, "chat", "a:" + TEC)
        await eng.snapshot_load("teste")
        linha = db.load_rows(conn)[0]
        self.assertEqual(int(linha["forms_ledger"]), 1)
        self.assertEqual(int(linha["casos_total"]), 1)
        self.assertEqual(int(linha["reservas"]), 1)
        self.assertEqual(int(linha["form_presente_toa"]), 1)

    async def test_ausente_no_toa_fecha_o_caso_na_segunda_leitura(self):
        """A tese: resolvido no TOA = sumiu da lista = fecha e libera a vaga."""
        conn = self._conn()
        self._caso_aberto(conn)
        db.upsert_chat(conn, FORM_ID, queue_address="h:q1", status="awaiting_reply")
        db.enqueue_route(conn, "form", FORM_ID, "h:q1", 1, detail="formulario", delay_sec=0)
        eng = base_engine(conn, LoadSock(in_process=[], pending=[]))
        await eng.snapshot_load("t1")
        self.assertIsNotNone(db.open_assignment(conn, FORM_ID), "1a ausencia nao fecha")
        self.assertNotIn("form_resolvido_toa", events(conn))
        await eng.snapshot_load("t2")
        self.assertIsNone(db.open_assignment(conn, FORM_ID), "2a ausencia fecha o caso")
        fechado = conn.execute(
            "SELECT * FROM route_assignments WHERE chat_id = ?", (FORM_ID,)
        ).fetchone()
        self.assertEqual(fechado["close_reason"], "form_resolvido_toa")
        self.assertIn("form_closed", events(conn))
        self.assertIn("form_resolvido_toa", events(conn))
        self.assertEqual(len(routes(conn, "cancelled")), 1, "rota do formulario encerrada")
        casos, _por_tipo, _info = await eng._capacidade(db.team_members(conn, 1), "form", "h:q1")
        self.assertEqual(casos[ANA], 0, "a vaga do analista volta")

    async def test_presenca_zera_o_contador_de_ausencia(self):
        conn = self._conn()
        self._caso_aberto(conn)
        eng = base_engine(conn, LoadSock(in_process=[], pending=[]))
        await eng.snapshot_load("t1")
        eng.sock = LoadSock(in_process=[form_toa()], pending=[])
        await eng.snapshot_load("t2")
        self.assertEqual(int(eng._form_absent.get(FORM_ID, 0)), 0, "apareceu no TOA: zera")
        eng.sock = LoadSock(in_process=[], pending=[])
        await eng.snapshot_load("t3")
        self.assertIsNotNone(db.open_assignment(conn, FORM_ID), "contador recomecou")
        await eng.snapshot_load("t4")
        self.assertIsNone(db.open_assignment(conn, FORM_ID))

    async def test_leitura_falha_nunca_fecha(self):
        conn = self._conn()
        self._caso_aberto(conn)
        eng = base_engine(conn, LoadSock(timeout=True))
        await eng.snapshot_load("t1")
        await eng.snapshot_load("t2")
        await eng.snapshot_load("t3")
        self.assertIsNotNone(db.open_assignment(conn, FORM_ID), "sem TOA nao se decide ausencia")
        self.assertNotIn("form_resolvido_toa", events(conn))
        self.assertEqual(int(db.load_rows(conn)[0]["toa_ok"]), 0)

    async def test_uma_lista_falhando_nao_fecha(self):
        """Ausencia so decide com as DUAS listas do front respondendo."""
        conn = self._conn()
        self._caso_aberto(conn)
        eng = base_engine(conn, LoadSock(in_process=[], pending=[], fail_tipos=("pending",)))
        await eng.snapshot_load("t1")
        await eng.snapshot_load("t2")
        await eng.snapshot_load("t3")
        self.assertIsNotNone(db.open_assignment(conn, FORM_ID), "pending falhou: nao decide")
        self.assertNotIn("form_resolvido_toa", events(conn))
        self.assertEqual(int(db.load_rows(conn)[0]["toa_ok"]), 0)
        # Sem time com fila nao existe lista do front: tambem nao decide.
        eng2 = base_engine(conn, LoadSock(in_process=[], pending=[]))
        conn.execute("DELETE FROM team_queues")
        await eng2.snapshot_load("t4")
        await eng2.snapshot_load("t5")
        self.assertIsNotNone(db.open_assignment(conn, FORM_ID), "sem fila nao ha lista do front")

    async def test_formulario_no_indice_sem_caso_sai_por_ausencia(self):
        conn = self._conn()
        eng = base_engine(conn, LoadSock(in_process=[], pending=[]))
        eng._open_forms[FORM_ID] = "closure"
        await eng.snapshot_load("t1")
        self.assertIn(FORM_ID, eng._open_forms)
        await eng.snapshot_load("t2")
        self.assertNotIn(FORM_ID, eng._open_forms, "sumiu do TOA: sai do indice")

    async def test_cadencia_e_limites_dos_ajustes(self):
        conn = self._conn()
        eng = base_engine(conn, LoadSock())
        self.assertEqual(eng._load_poll_sec(), 30, "padrao de 30 s")
        eng.cfg["load_poll_sec"] = "1"
        self.assertEqual(eng._load_poll_sec(), 5, "piso de 5 s")
        self.assertEqual(eng._form_absent_readings(), 2)
        eng.cfg["form_absent_readings"] = "0"
        self.assertEqual(eng._form_absent_readings(), 1, "piso de 1 leitura")
        self.assertEqual(eng._load_keep(), 200)

    async def test_historico_e_podado_pelo_ajuste(self):
        conn = self._conn()
        eng = base_engine(conn, LoadSock(), analyst_load_keep="10")
        for i in range(12):
            await eng.snapshot_load(f"t{i}")
        self.assertEqual(len(db.load_history(conn, limit=50)), 10)
        self.assertEqual(len(db.load_rows(conn)), 1, "leitura atual e uma linha por analista")

    async def test_job_load_snapshot_le_na_hora(self):
        """Botao 'Atualizar agora' da aba Carga: bot_jobs -> leitura imediata."""
        conn = self._conn()
        sock = LoadSock(in_process=[chat_toa()], online=["ana"])
        eng = base_engine(conn, sock)
        conn.execute("INSERT INTO bot_jobs (kind) VALUES ('load_snapshot')")
        await eng.process_pending_jobs()
        job = conn.execute("SELECT * FROM bot_jobs").fetchone()
        self.assertEqual(job["status"], "done")
        self.assertEqual(len(db.load_rows(conn)), 1)
        self.assertIn("get_helpdesk_chats", sock.names())


if __name__ == "__main__":
    unittest.main()

