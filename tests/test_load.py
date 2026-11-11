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


class LoadFila(LoadSock):
    """Listas por FILA: permite ter carga numa fila diferente da do item.

    `por_fila` vale para `in_process`; `pending_fila` (opcional) alimenta a
    lista `pending` por fila — e e a fonte do card FILA FORMS.
    """

    def __init__(self, por_fila=None, pending_fila=None, **kw):
        super().__init__(**kw)
        self.por_fila = {str(k): list(v) for k, v in (por_fila or {}).items()}
        self.pending_fila = {str(k): list(v) for k, v in (pending_fila or {}).items()}

    async def command(self, name, payload=None, **kwargs):
        if name == "get_helpdesk_chats":
            q = str((payload or {}).get("helpdesk_address") or "")
            tipo = str((payload or {}).get("chat_type") or "")
            if q in self.por_fila or q in self.pending_fila:
                self.cmds.append((name, payload or {}))
                if tipo == "in_process":
                    lista = self.por_fila.get(q, [])
                else:
                    lista = self.pending_fila.get(q, [])
                return {"d": {"chats": [dict(c) for c in lista]}}
        return await super().command(name, payload, **kwargs)


def conn_dois_filas():
    """Time 1 com DUAS filas (h:q1 do item e h:q2 onde mora a carga)."""
    conn = mem_conn()
    seed_queue_menu(conn, with_team=True)
    conn.execute("INSERT INTO queues (address, name, enabled) VALUES ('h:q2', 'F2', 1)")
    conn.execute("INSERT INTO team_queues (team_id, queue_address) VALUES (1, 'h:q2')")
    return conn


class ChatResolvidoTest(unittest.IsolatedAsyncioTestCase):
    """Caso de CHAT tambem morre quando o item sai das listas do TOA."""

    def tearDown(self):
        MEM.clear()

    def _caso(self, conn, cid: int = CHAT_ID, fila: str = "h:q1") -> None:
        db.assign_open(conn, "chat", cid, fila, 1, ANA, "a:" + TEC, TEC, "Tec", "address")
        db.upsert_chat(conn, cid, queue_address=fila, status="closed")

    async def test_chat_ausente_no_toa_fecha_o_caso(self):
        """Status local `closed` nao decide nada: quem decide e a ausencia no TOA."""
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        self._caso(conn)
        eng = base_engine(conn, LoadSock(in_process=[], pending=[]))
        await eng.snapshot_load("t1")
        self.assertIsNotNone(db.open_assignment(conn, CHAT_ID), "1a ausencia nao fecha")
        await eng.snapshot_load("t2")
        self.assertIsNone(db.open_assignment(conn, CHAT_ID), "2a ausencia fecha o caso")
        fechado = conn.execute(
            "SELECT close_reason FROM route_assignments WHERE chat_id = ?", (CHAT_ID,)
        ).fetchone()
        self.assertEqual(fechado["close_reason"], "chat_resolvido_toa")
        self.assertIn("chat_resolvido_toa", events(conn))
        casos, _por_tipo, _info = await eng._capacidade(db.team_members(conn, 1), "chat", "h:q1")
        self.assertEqual(casos[ANA], 0, "a vaga do analista volta")

    async def test_chat_presente_no_toa_mantem_o_caso(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        self._caso(conn)
        eng = base_engine(conn, LoadSock(in_process=[chat_toa()], pending=[]))
        await eng.snapshot_load("t1")
        await eng.snapshot_load("t2")
        await eng.snapshot_load("t3")
        self.assertIsNotNone(db.open_assignment(conn, CHAT_ID), "presente: o caso segue aberto")
        self.assertNotIn("chat_resolvido_toa", events(conn))

    async def test_fila_fora_do_time_nao_e_fechada_por_ausencia(self):
        """Sem a fila na leitura nao da para afirmar que o item sumiu."""
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        conn.execute("INSERT INTO queues (address, name, enabled) VALUES ('h:fora', 'Fora', 1)")
        self._caso(conn, 77, "h:fora")
        eng = base_engine(conn, LoadSock(in_process=[], pending=[]))
        await eng.snapshot_load("t1")
        await eng.snapshot_load("t2")
        await eng.snapshot_load("t3")
        self.assertIsNotNone(db.open_assignment(conn, 77), "fila fora do time: nao fecha")

    async def test_leitura_falha_nao_fecha_chat(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        self._caso(conn)
        eng = base_engine(conn, LoadSock(timeout=True))
        await eng.snapshot_load("t1")
        await eng.snapshot_load("t2")
        await eng.snapshot_load("t3")
        self.assertIsNotNone(db.open_assignment(conn, CHAT_ID), "sem TOA nao se decide ausencia")


def item_sem_autor(cid: int, analista: str = ANA) -> dict:
    """Formulario sem autor identificavel: a leitura cai em `chat:<id>`."""
    return {
        "chat_id": cid,
        "mode": "message_scenario",
        "person": "Message Scenario Agent",
        "helpdesk_address": "h:q1",
        "participants": {
            analista: {"address": analista, "name": "Analista", "present": True, "role_desc": "Moderator"},
        },
    }


class CapPeloFisicoTest(unittest.IsolatedAsyncioTestCase):
    """O cap usa a leitura completa do TOA (todas as filas), nao so a fila do item."""

    def tearDown(self):
        MEM.clear()

    async def test_carga_de_outra_fila_do_time_entra_no_cap(self):
        conn = conn_dois_filas()
        chats = []
        for i in range(4):
            c = chat_toa(200 + i, tec="u:tec%d@claro.com" % i, analista=ANA)
            c["helpdesk_address"] = "h:q2"
            chats.append(c)
        eng = base_engine(conn, LoadFila(por_fila={"h:q2": chats}, online=["ana"]))
        await eng.snapshot_load("teste")
        lido = db.load_reading(conn, ANA)
        self.assertIsNotNone(lido, "leitura recente e boa serve para decidir")
        self.assertEqual(len(db.load_case_keys(lido)), 4, "4 autores vistos na leitura")
        # A fila do item (h:q1) esta VAZIA, mas o TOA mostra o analista com 4 casos.
        pick, detail = await eng.pick_assignee("h:q1", kind="form", retry_online=False)
        self.assertIsNone(pick, detail)
        self.assertIn("no_cap=ana", detail)
        # Sem a leitura (leitura velha/restart) volta ao calculo por fila: a fila do
        # item esta vazia, entao o analista volta a ser elegivel.
        conn.execute("DELETE FROM analyst_load")
        pick2, _ = await eng.pick_assignee("h:q1", kind="form", retry_online=False)
        self.assertIsNotNone(pick2, "sem a leitura vale so a fila do item")

    async def test_ledger_nao_infla_o_cap_quando_a_leitura_existe(self):
        """Caso real (Andressa): ledger `a:u:...` e leitura `chat:<id>` = MESMO caso."""
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        # O bot entregou o formulario identificando o autor (chave por autor)...
        db.assign_open(conn, "form", 99, "h:q1", 1, ANA, "a:u:z648031@clarosa", "u:Z648031@clarosa", "Tec", "address")
        # ...e a lista do TOA nao traz o autor: a leitura ve o mesmo item como `chat:99`.
        eng = base_engine(conn, LoadSock(in_process=[item_sem_autor(99)], online=["ana"]))
        await eng.snapshot_load("teste")
        lido = db.load_reading(conn, ANA)
        self.assertEqual(db.load_case_keys(lido), {"chat:99"}, "leitura sem autor: caso por item")
        self.assertEqual(int(db.load_rows(conn)[0]["casos_total"]), 1, "o mesmo item nao conta 2x")
        casos, _por_tipo, _info = await eng._capacidade(db.team_members(conn, 1), "form", "h:q1")
        self.assertEqual(casos[ANA], 1, "cap conta o fisico: 1 caso, nao 2")

    async def test_reserva_em_voo_conta_e_nao_duplica(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        eng = base_engine(conn, LoadSock(in_process=[item_sem_autor(99)], online=["ana"]))
        await eng.snapshot_load("teste")
        # Reserva de um caso NOVO (entrega em voo, o TOA ainda nao refletiu): soma.
        eng._reserve_add(ANA, "form", "a:u:novo@clarosa")
        casos, por_tipo, _info = await eng._capacidade(db.team_members(conn, 1), "form", "h:q1")
        self.assertEqual(casos[ANA], 2, "fisico 1 + reserva em voo 1")
        self.assertEqual(por_tipo[ANA], 2, "por tipo tambem soma a reserva em voo")
        # Reserva do MESMO caso que a leitura ja viu: nao duplica.
        eng._reserve_add(ANA, "form", "chat:99")
        casos, por_tipo, _info = await eng._capacidade(db.team_members(conn, 1), "form", "h:q1")
        self.assertEqual(casos[ANA], 2, "o item do TOA e a reserva dele sao o mesmo caso")
        self.assertEqual(por_tipo[ANA], 2, "por tipo nao repete o item que ja esta no TOA")

    async def test_sem_leitura_vale_o_calculo_antigo(self):
        """Sem leitura boa (restart/leitura velha) o ledger volta a valer."""
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        db.assign_open(conn, "form", 99, "h:q1", 1, ANA, "a:u:z648031@clarosa", "u:Z648031@clarosa", "Tec", "address")
        eng = base_engine(conn, LoadSock(in_process=[], online=["ana"]))
        casos, _por_tipo, _info = await eng._capacidade(db.team_members(conn, 1), "form", "h:q1")
        self.assertEqual(casos[ANA], 1, "sem leitura o ledger conta (fallback)")

    async def test_leitura_velha_ou_com_falha_nao_serve(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        db.load_upsert(conn, ANA, toa_ok=1, chats_toa=4, casos_toa_keys='["a:u:x@y"]')
        self.assertIsNotNone(db.load_reading(conn, ANA, fresh_sec=90))
        conn.execute(
            "UPDATE analyst_load SET at = datetime('now', '-10 minutes') WHERE address = ?", (ANA,)
        )
        self.assertIsNone(db.load_reading(conn, ANA, fresh_sec=90), "leitura velha nao decide")
        conn.execute(
            "UPDATE analyst_load SET at = datetime('now'), toa_ok = 0 WHERE address = ?", (ANA,)
        )
        self.assertIsNone(db.load_reading(conn, ANA, fresh_sec=90), "leitura falha nao decide")

    async def test_snapshot_grava_as_chaves_do_toa(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        eng = base_engine(conn, LoadSock(in_process=[chat_toa(), form_toa()], online=["ana"]))
        await eng.snapshot_load("teste")
        chaves = db.load_case_keys(db.load_reading(conn, ANA))
        # chat + formulario do MESMO autor: um caso so (a regra do cap).
        self.assertEqual(len(chaves), 1, chaves)
        self.assertEqual(len(chaves), int(db.load_rows(conn)[0]["casos_total"]))


def form_na_fila(cid: int = 301, tec: str = TEC) -> dict:
    """Formulario de validacao na lista `pending` (na fila do TOA, sem analista)."""
    return {
        "chat_id": cid,
        "mode": "message_scenario",
        "person": "Message Scenario Agent",
        "helpdesk_address": "h:q1",
        "participants": {tec: {"address": tec, "present": True}},
    }


def chat_na_fila(cid: int = 302, tec: str = TEC) -> dict:
    """Chat simples na lista `pending` (conta como chat, nao form)."""
    return {
        "chat_id": cid,
        "helpdesk_address": "h:q1",
        "participants": {tec: {"address": tec, "present": True}},
    }


class QueueLoadTest(unittest.IsolatedAsyncioTestCase):
    """queue_load: a fonte do card FILA FORMS do dashboard.

    O card precisa contar os forms presentes na lista `pending` do TOA por
    fila — nao as rotas em `pending_routes` (rota orfa de form inflava o
    numero). Uma linha por fila, atualizada a cada snapshot.
    """

    def tearDown(self):
        MEM.clear()

    def _conn(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        return conn

    def _fila(self, conn, address: str, name: str = "") -> None:
        conn.execute("INSERT INTO queues (address, name, enabled) VALUES (?, ?, 1)", (address, name or address))
        conn.execute("INSERT INTO team_queues (team_id, queue_address) VALUES (1, ?)", (address,))

    async def test_snapshot_grava_queue_load_por_fila(self):
        conn = self._conn()
        eng = base_engine(
            conn,
            LoadSock(in_process=[], pending=[form_na_fila(301), form_na_fila(303), chat_na_fila(302)], online=["ana"]),
        )
        await eng.snapshot_load("teste")
        linhas = conn.execute("SELECT * FROM queue_load WHERE queue_address = 'h:q1'").fetchall()
        self.assertEqual(len(linhas), 1, "uma linha por fila")
        self.assertEqual(int(linhas[0]["pending_forms"]), 2)
        self.assertEqual(int(linhas[0]["pending_chats"]), 1)
        self.assertEqual(int(linhas[0]["toa_ok"]), 1)

    async def test_pending_e_deduplicado_por_chat(self):
        """O MESMO chat repetido na lista `pending` conta uma vez."""
        conn = self._conn()
        eng = base_engine(
            conn,
            LoadSock(in_process=[], pending=[form_na_fila(301), form_na_fila(301)], online=["ana"]),
        )
        await eng.snapshot_load("teste")
        linha = conn.execute("SELECT * FROM queue_load WHERE queue_address = 'h:q1'").fetchone()
        self.assertEqual(int(linha["pending_forms"]), 1)

    async def test_form_em_duas_filas_conta_uma_vez_no_total(self):
        """Mesmo form listado em duas filas: dedup entre filas do snapshot."""
        conn = self._conn()
        self._fila(conn, "h:q2")
        form_q1 = form_na_fila(301)
        form_q2 = form_na_fila(301)
        form_q2["helpdesk_address"] = "h:q2"
        eng = base_engine(
            conn,
            LoadFila(pending_fila={"h:q1": [form_q1], "h:q2": [form_q2]}, online=["ana"]),
        )
        await eng.snapshot_load("teste")
        q1 = conn.execute("SELECT * FROM queue_load WHERE queue_address = 'h:q1'").fetchone()
        q2 = conn.execute("SELECT * FROM queue_load WHERE queue_address = 'h:q2'").fetchone()
        self.assertEqual(int(q1["pending_forms"]), 1)
        self.assertEqual(int(q2["pending_forms"]), 0, "o form ja contou na primeira fila")
        self.assertEqual(int(q1["pending_forms"]) + int(q2["pending_forms"]), 1)

    async def test_fila_com_falha_grava_toa_ok_zero(self):
        """Leitura falha nao vira numero no card: toa_ok=0 e o painel mostra '-'."""
        conn = self._conn()
        eng = base_engine(conn, LoadSock(in_process=[], pending=[form_na_fila()], online=["ana"], fail_tipos=("pending",)))
        await eng.snapshot_load("teste")
        linha = conn.execute("SELECT * FROM queue_load WHERE queue_address = 'h:q1'").fetchone()
        self.assertEqual(int(linha["toa_ok"]), 0)

    async def test_upsert_e_idempotente_por_fila(self):
        conn = self._conn()
        eng = base_engine(conn, LoadSock(in_process=[], pending=[form_na_fila()], online=["ana"]))
        await eng.snapshot_load("t1")
        eng.sock = LoadSock(in_process=[], pending=[], online=["ana"])
        await eng.snapshot_load("t2")
        linhas = conn.execute("SELECT * FROM queue_load WHERE queue_address = 'h:q1'").fetchall()
        self.assertEqual(len(linhas), 1, "upsert: uma linha por fila, nao acumula")
        self.assertEqual(int(linhas[0]["pending_forms"]), 0, "segunda leitura atualiza o valor")

    def test_queue_load_frescas_filtra_velha_e_falha(self):
        conn = self._conn()
        db.queue_load_upsert(conn, "h:q1", pending_forms=3, toa_ok=1)
        self.assertEqual(len(db.queue_load_frescas(conn, fresh_sec=90)), 1)
        conn.execute("UPDATE queue_load SET at = datetime('now', '-10 minutes')")
        self.assertEqual(db.queue_load_frescas(conn, fresh_sec=90), [], "leitura velha nao serve")
        conn.execute("UPDATE queue_load SET at = datetime('now'), toa_ok = 0")
        self.assertEqual(db.queue_load_frescas(conn, fresh_sec=90), [], "leitura falha nao serve")

    def test_tabela_existe_no_schema(self):
        conn = mem_conn()
        self.assertIn("queue_load", db._table_names(conn))


class DisponibilidadeWakeTest(unittest.IsolatedAsyncioTestCase):
    """Mudanca 2 (docs/plano-correcao-fila-esquecida.md): o `snapshot_load`
    acorda a fila do time assim que detecta a TRANSICAO para disponivel —
    sem depender da detecao (fragil) de fechamento de caso."""

    def tearDown(self):
        MEM.clear()

    def _conn(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        return conn

    async def test_analista_disponivel_acorda_rota_em_backoff(self):
        conn = self._conn()
        eng = base_engine(
            conn, LoadSock(in_process=[chat_toa()], online=["ana"]),
            max_cases_per_agent="1",
        )
        rid = db.enqueue_route(conn, "chat", 55, "h:q1", 1, delay_sec=120)
        await eng.snapshot_load("t1")
        # Ana no limite (1 caso, cap=1): a rota continua esperando o backoff.
        self.assertEqual(db.due_routes(conn)["chat"], [], "ainda em backoff; ninguem ficou disponivel")
        self.assertNotIn("analyst_available", events(conn))
        eng.sock.in_process = []  # caso resolvido no TOA: ana libera a vaga
        await eng.snapshot_load("t2")
        due = db.due_routes(conn)["chat"]
        self.assertEqual(
            [int(r["id"]) for r in due], [rid],
            "liberacao acordou a rota na hora, sem esperar o backoff vencer sozinho",
        )
        self.assertIn("analyst_available", events(conn))

    async def test_analista_fica_indisponivel_nao_acorda(self):
        conn = self._conn()
        eng = base_engine(
            conn, LoadSock(in_process=[], online=["ana"]),
            max_cases_per_agent="1",
        )
        db.enqueue_route(conn, "chat", 56, "h:q1", 1, delay_sec=120)
        await eng.snapshot_load("t1")  # ana ja disponivel desde a 1a leitura
        eng.sock.in_process = [chat_toa()]  # agora ela pega um caso: fica indisponivel
        await eng.snapshot_load("t2")
        self.assertEqual(db.due_routes(conn)["chat"], [], "ficou indisponivel: nao acorda a fila")
        self.assertNotIn("analyst_available", events(conn))

    async def test_leitura_degradada_alerta_uma_vez(self):
        conn = self._conn()
        eng = base_engine(
            conn, LoadSock(online=["ana"], timeout=True),
            queue_read_degraded_sec="0",
        )
        await eng.snapshot_load("t1")
        self.assertEqual(events(conn).count("queue_read_degraded"), 1)
        await eng.snapshot_load("t2")  # continua ruim: nao repete o alerta
        self.assertEqual(events(conn).count("queue_read_degraded"), 1)
        eng.sock.timeout = False
        await eng.snapshot_load("t3")  # volta a ler bem: limpa o estado de degradacao
        await eng.snapshot_load("t4")
        self.assertEqual(events(conn).count("queue_read_degraded"), 1, "sem nova falha, sem novo alerta")


if __name__ == "__main__":
    unittest.main()

