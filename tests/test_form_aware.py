# -*- coding: utf-8 -*-
"""Abertura form-aware: vinculação, escolha 1/2 e caso único (chat + formulário)."""
from __future__ import annotations

import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python import form_open  # noqa: E402
from python.engine import MEM, Engine  # noqa: E402
from tests.test_form_open import OWN, QUEUE, form_chat, queue_chat  # noqa: E402
from tests.test_submenu import insert_option, mem_conn, seed_queue_menu  # noqa: E402

ANA = "u:ana@claro.com"
TEC = "u:tec@claro.com"
CASO_TEC = "a:u:tec@claro.com"


def _ms_atras(minutos: float) -> int:
    """Epoch em MILISSEGUNDOS de N minutos atrás (`time` como o TOA manda)."""
    return int((time.time() - float(minutos) * 60.0) * 1000)


def _envelhece_caso(conn, cid: int = 99, horas: float = 5) -> None:
    """Fixture: caso aberto há horas.

    O relógio de vida considera também a abertura do caso (`assigned_at`): uma
    entrega feita agora mantém o formulário vivo, então o cenário de "parado há
    horas" precisa envelhecer a atribuição.
    """
    conn.execute(
        "UPDATE route_assignments SET assigned_at = datetime('now', ?) WHERE chat_id = ?",
        ("-%d hours" % int(horas), int(cid)),
    )


def form_agente(cid: int = 99, **over) -> dict:
    """Formulário como o TOA manda de verdade: SEM o address do técnico.

    É o caso do formulário real (o ledger gravou `case_key='n:message scenario
    agent'`): a identidade do dono não vem no payload, então a correlação cai no
    fallback por fila + recência.
    """
    c = {
        "chat_id": cid,
        "mode": "message_scenario",
        "lifecycle": "pending",
        "ended_time": -1,
        "person": "Message Scenario Agent",
        "helpdesk_address": QUEUE,
        "participants": {
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


class AwareSock:
    """Socket fake: o join marca presença e o online é controlável."""

    own_address = OWN

    def __init__(self, online=("ana",)):
        self.cmds: list[tuple[str, dict]] = []
        self.online = list(online)
        self.msgs: dict[int, list] = {}

    async def command(self, name, payload=None, **kwargs):
        payload = payload or {}
        self.cmds.append((name, payload))
        if name == "get_online_users":
            return {"d": {"result": list(self.online)}}
        if name == "get_active_chats":
            return {"d": {"chats": []}}
        if name == "join_chat":
            return {
                "d": {
                    "present": True,
                    "address": self.own_address,
                    "chat_id": payload.get("chat_id"),
                }
            }
        if name == "get_chat_messages":
            cid = payload.get("chat_id")
            return {"d": {"messages": list(self.msgs.get(int(cid), []))}}
        return {"d": {}}

    def names(self) -> list[str]:
        return [n for n, _ in self.cmds]

    def last(self, name: str) -> dict:
        for n, p in reversed(self.cmds):
            if n == name:
                return p
        raise AssertionError(f"comando {name} não foi enviado")

    def sent_texts(self) -> list[str]:
        return [p["message"]["text"] for n, p in self.cmds if n == "send_message"]

    def moved_chats(self) -> list[int]:
        return [int(p["chat_id"]) for n, p in self.cmds if n == "move_chat"]


def events(conn) -> list[str]:
    return [r["reason"] for r in conn.execute("SELECT reason FROM bot_events ORDER BY id").fetchall()]


def chat_row(conn, cid: int = 10) -> dict:
    return conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone()


def assignment(conn, cid: int) -> dict:
    return (
        conn.execute(
            "SELECT * FROM route_assignments WHERE chat_id = ? AND closed_at IS NULL", (cid,)
        ).fetchone()
        or {}
    )


def routes(conn, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM pending_routes"
    args: list = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    return conn.execute(sql + " ORDER BY id", args).fetchall()


class FormAwareSchemaTest(unittest.TestCase):
    """Colunas legadas do vinculo chat<->formulario (a abertura foi REMOVIDA)."""

    def test_schema_mantem_colunas_e_limpa_os_textos(self):
        conn = mem_conn()
        self.assertIn("linked_form_chat_id", db._table_columns(conn, "chats"))
        self.assertIn("expects_owner", db._table_columns(conn, "pending_routes"))
        textos = db.templates(conn)
        for slug in ("form_pending", "form_pending_wait", "form_waiting"):
            self.assertNotIn(slug, textos, "texto da abertura removida nao pode ficar no banco")

    def test_migracao_em_base_antiga_remove_textos_e_e_idempotente(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = db._dict_factory
        conn.isolation_level = None
        conn.executescript(
            "CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, status TEXT NOT NULL);"
            "CREATE TABLE pending_routes (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, chat_id INTEGER);"
            "CREATE TABLE templates (slug TEXT PRIMARY KEY, body TEXT NOT NULL DEFAULT '');"
        )
        # Banco antigo: os textos da abertura estao gravados (com o literal antigo).
        conn.execute("INSERT INTO templates (slug, body) VALUES ('form_pending', 'linha1\\nlinha2')")
        conn.execute("INSERT INTO templates (slug, body) VALUES ('greeting', 'Ola')")
        db._migrate_form_aware(conn)
        self.assertIn("linked_form_chat_id", db._table_columns(conn, "chats"))
        self.assertIn("expects_owner", db._table_columns(conn, "pending_routes"))
        restantes = [r["slug"] for r in conn.execute("SELECT slug FROM templates").fetchall()]
        self.assertEqual(restantes, ["greeting"], "so os textos da abertura saem do banco")
        db._migrate_form_aware(conn)
        self.assertEqual(len(conn.execute("SELECT 1 FROM templates").fetchall()), 1)


class CorrelationTest(unittest.TestCase):
    """form_open.form_by_technician: address → nome → fila + recência."""

    def test_match_por_address(self):
        form, regra = form_open.form_by_technician(queue_chat(), [form_chat()], OWN)
        self.assertEqual(regra, "address")
        self.assertEqual(int(form["chat_id"]), 99)

    def test_sem_address_no_form_casa_por_fila(self):
        form, regra = form_open.form_by_technician(queue_chat(), [form_agente()], OWN)
        self.assertEqual(regra, "fila")
        self.assertEqual(int(form["chat_id"]), 99)

    def test_dois_candidatos_sem_identidade_nao_casam(self):
        """Sem identidade, dois candidatos livres = ambíguo: não adivinha por recência.

        Era esse palpite (`candidatos[-1]`) que juntava técnicos diferentes no
        mesmo caso: `regra=fila` + `relink_case` reescrevendo o dono do formulário
        alheio.
        """
        antigo = form_agente(cid=90, started_time=1000)
        novo = form_agente(cid=98, started_time=2000)
        form, regra = form_open.form_by_technician(queue_chat(), [antigo, novo], OWN)
        self.assertIsNone(form)
        self.assertEqual(regra, "ambiguo:2")

    def test_autor_das_mensagens_casa(self):
        """Formulário sem address no payload: o AUTOR das mensagens identifica."""
        form, regra = form_open.form_by_technician(
            queue_chat(), [form_agente()], OWN, authors={99: [TEC]},
        )
        self.assertEqual(regra, "autor")
        self.assertEqual(int(form["chat_id"]), 99)

    def test_autor_nao_casa_quando_e_de_outro_tecnico(self):
        form, regra = form_open.form_by_technician(
            queue_chat(), [form_agente()], OWN, authors={99: ["u:outro@claro.com"]},
        )
        self.assertEqual(regra, "fila", "sem o autor do tecnico sobra o palpite por fila")

    def test_palpite_de_fila_prefere_o_sem_responsavel(self):
        """Com responsável e livre na fila, o palpite usa o LIVRE."""
        dono = form_agente(cid=98, started_time=1000)
        livre = form_agente(cid=99, started_time=2000)
        form, regra = form_open.form_by_technician(
            queue_chat(), [dono, livre], OWN, sem_dono={99},
        )
        self.assertEqual(regra, "fila")
        self.assertEqual(int(form["chat_id"]), 99)

    def test_sem_candidato_livre_nao_casa(self):
        """Formulários já em atendimento não entram no palpite (só por identidade)."""
        a = form_agente(cid=98, started_time=1000)
        b = form_agente(cid=99, started_time=2000)
        form, regra = form_open.form_by_technician(queue_chat(), [a, b], OWN, sem_dono=set())
        self.assertIsNone(form)
        self.assertEqual(regra, "em_atendimento:2")

    def test_unico_candidato_com_dono_ainda_casa(self):
        """Um só formulário na fila e já em atendimento: é o chat falando dele."""
        form, regra = form_open.form_by_technician(
            queue_chat(), [form_agente()], OWN, sem_dono=set(),
        )
        self.assertEqual(regra, "fila")
        self.assertEqual(int(form["chat_id"]), 99)

    def test_form_ja_vinculado_nao_e_reusado(self):
        form, regra = form_open.form_by_technician(queue_chat(), [form_agente()], OWN, linked=[99])
        self.assertIsNone(form)
        self.assertEqual(regra, "")

    def test_fila_diferente_nao_casa(self):
        outro = form_agente(helpdesk_address="h:outra")
        self.assertIsNone(form_open.form_by_technician(queue_chat(), [outro], OWN)[0])

    def test_nome_tem_prioridade_sobre_recencia(self):
        meu = form_agente(cid=90, started_time=1000, person="Tecnico Silva")
        outro = form_agente(cid=99, started_time=2000, person="Outro Agente")
        achado, regra = form_open.form_by_technician(queue_chat(), [meu, outro], OWN, match_name=True)
        self.assertEqual(regra, "name")
        self.assertEqual(int(achado["chat_id"]), 90)

    def test_sem_match_name_o_vinculo_acontece_pela_fila(self):
        form = form_agente(person="Tecnico Silva")
        _, regra = form_open.form_by_technician(queue_chat(), [form], OWN, match_name=False)
        self.assertEqual(regra, "fila")

    def test_form_encerrado_nao_casa(self):
        self.assertIsNone(form_open.form_by_technician(queue_chat(), [form_agente(lifecycle="removed")], OWN)[0])
        self.assertIsNone(
            form_open.form_by_technician(queue_chat(), [form_agente(ended_time=1700000000)], OWN)[0]
        )

    def test_sem_fila_no_chat_nao_casa(self):
        chat = queue_chat(helpdesk_address=None, channel_addr=None)
        self.assertIsNone(form_open.form_by_technician(chat, [form_agente()], OWN)[0])


class FormAwareEngineTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _eng(self, online=("ana",), with_team=True):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=with_team)
        # Menu minimo: sem opcao o join_and_greet falha com "menu vazio".
        insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Validação",
            classification="TESTE",
            action="reply_and_close",
            reply_text="Validação somente via formulário.",
        )
        insert_option(
            conn,
            menu_id=1,
            sort_order=2,
            label="Infra",
            classification="Infra",
            action="transfer_only",
            reply_text="",
        )
        sock = AwareSock(online=online)
        eng = Engine(conn, sock)
        eng.cfg["transfer_retry_interval_sec"] = "0"
        eng.cfg["form_route_retry_interval_sec"] = "0"
        return eng, conn, sock

    async def test_sem_formulario_o_menu_normal_continua(self):
        eng, conn, sock = self._eng()
        await eng.consider(queue_chat())
        row = chat_row(conn)
        self.assertEqual(row["status"], "awaiting_reply")
        self.assertIn("menu_sent", events(conn))
        self.assertNotIn("form_aware_greeting", events(conn))
        self.assertIn("Digite o número da opção:", sock.sent_texts()[-1])

    async def test_form_ocioso_fecha_o_caso_no_tick(self):
        """Ocioso além da janela de fechamento: sai do índice e o caso fecha."""
        eng, conn, sock = self._eng()
        await eng.consider(form_agente())
        eng.cfg["form_idle_close_min"] = "60"
        _envelhece_caso(conn)
        sock.msgs[99] = [
            {"author": "CREATEDBYMESSAGESCENARIO", "text": "cenario", "time": _ms_atras(240)},
        ]
        eng._form_checked[99] = 0.0  # força a varredura a visitar este formulário
        await eng._sweep_form_states()
        self.assertNotIn(99, eng._open_forms)
        self.assertEqual(db.form_case_state(conn, 99), "done")
        detalhe = conn.execute(
            "SELECT detail FROM bot_events WHERE reason = 'form_closed' ORDER BY id DESC LIMIT 1"
        ).fetchone()["detail"]
        self.assertIn("form_ocioso", detalhe)

    async def test_form_ocioso_com_analista_fecha_no_tick(self):
        """Parado há muito tempo COM analista dentro: fecha (o TOA não avisa a saída)."""
        eng, conn, sock = self._eng()
        await eng.consider(form_agente())
        eng.cfg["form_idle_owner_close_min"] = "60"
        _envelhece_caso(conn)
        sock.msgs[99] = [
            {"author": "CREATEDBYMESSAGESCENARIO", "text": "cenario", "time": _ms_atras(300)},
            {"author": ANA, "system_message": "user_joined", "time": _ms_atras(300)},
        ]
        eng._form_checked[99] = 0.0
        await eng._sweep_form_states()
        self.assertNotIn(99, eng._open_forms)
        self.assertEqual(db.form_case_state(conn, 99), "done")
        detalhe = conn.execute(
            "SELECT detail FROM bot_events WHERE reason = 'form_closed' ORDER BY id DESC LIMIT 1"
        ).fetchone()["detail"]
        self.assertIn("form_ocioso_analista", detalhe)

    async def test_indice_descarta_formulario_de_caso_encerrado(self):
        """Caso encerrado de verdade: sai do índice (a varredura para de ler no TOA)."""
        eng, conn, _sock = self._eng()
        await eng.consider(form_agente())
        db.assign_close(conn, 99, "form_done")
        self.assertEqual(eng._prune_open_forms(), 1)
        self.assertNotIn(99, eng._open_forms)
        detalhe = conn.execute(
            "SELECT detail FROM bot_events WHERE reason = 'form_forget' ORDER BY id DESC LIMIT 1"
        ).fetchone()["detail"]
        self.assertIn("caso_encerrado", detalhe)

    async def test_indice_mantem_formulario_sem_caso_encerrado(self):
        """Caso expirado por TTL não descarta o formulário: ele pode estar aberto no TOA."""
        eng, conn, _sock = self._eng()
        await eng.consider(form_agente())
        db.assign_close(conn, 99, "ttl")
        self.assertEqual(eng._prune_open_forms(), 0)
        self.assertIn(99, eng._open_forms)

    async def test_ociosidade_usa_started_time_do_payload(self):
        """Sem `time` nas mensagens, o relógio de vida vem do `started_time` do closure."""
        eng, conn, sock = self._eng()
        await eng.consider(form_agente(started_time=_ms_atras(300)))
        eng.cfg["form_idle_close_min"] = "60"
        _envelhece_caso(conn)
        sock.msgs[99] = [{"author": "CREATEDBYMESSAGESCENARIO", "text": "cenario", "time": 1}]
        eng._form_checked[99] = 0.0
        await eng._sweep_form_states()
        self.assertNotIn(99, eng._open_forms)
        detalhe = conn.execute(
            "SELECT detail FROM bot_events WHERE reason = 'form_closed' ORDER BY id DESC LIMIT 1"
        ).fetchone()["detail"]
        self.assertIn("form_ocioso", detalhe)

    async def test_ociosidade_usa_assigned_at_apos_restart(self):
        """Reidratado do ledger: o relógio de vida é a abertura do caso (`assigned_at`)."""
        eng, conn, _sock = self._eng()
        await eng.consider(form_agente())
        conn.execute(
            "UPDATE route_assignments SET assigned_at = datetime('now','-5 hours') WHERE chat_id = 99"
        )
        eng._open_forms.clear()
        eng._form_seen.clear()
        eng._form_last_msg.clear()
        eng._form_started.clear()
        eng._form_case_at.clear()
        self.assertEqual(eng._hydrate_open_forms(), 1)
        eng.cfg["form_idle_owner_close_min"] = "240"
        eng._form_checked[99] = 0.0
        await eng._sweep_form_states()
        self.assertNotIn(99, eng._open_forms)

class FormIndexRobustnessTest(unittest.IsolatedAsyncioTestCase):
    """P1/P2: o índice não pode perder (nem nunca receber) formulário vivo."""

    def tearDown(self):
        MEM.clear()

    def _eng(self, online=("ana",)):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        insert_option(
            conn, menu_id=1, sort_order=1, label="Validação",
            classification="TESTE", action="reply_and_close", reply_text="ok",
        )
        sock = AwareSock(online=online)
        eng = Engine(conn, sock)
        eng.cfg["transfer_retry_interval_sec"] = "0"
        eng.cfg["form_route_retry_interval_sec"] = "0"
        return eng, conn, sock

    async def test_ended_time_epoch_nao_indexa_e_avisa(self):
        """ended_time epoch = fim explícito: evento form_not_open (antes silencioso)."""
        eng, conn, _sock = self._eng()
        await eng.consider(form_agente(ended_time=1758553200))
        self.assertNotIn(99, eng._open_forms)
        self.assertIn("form_not_open", events(conn))
        detalhe = conn.execute(
            "SELECT detail FROM bot_events WHERE reason = 'form_not_open' ORDER BY id DESC LIMIT 1"
        ).fetchone()["detail"]
        self.assertIn("ended_time=1758553200", detalhe)
        self.assertIn("mode=message_scenario", detalhe)

    async def test_ended_time_pequeno_nao_derruba_o_formulario(self):
        """0/-1/1/2 não são datas: o formulário continua acompanhado."""
        eng, conn, _sock = self._eng()
        for valor in (0, -1, 1, 2, "0"):
            await eng.consider(form_agente(ended_time=valor))
            self.assertIn(99, eng._open_forms, f"ended_time={valor!r}")
        self.assertIn("form_indexed", events(conn))

    async def test_payload_pobre_nao_derruba_o_indice(self):
        """Payload do mesmo chat sem sinal de formulário: índice mantido + evento."""
        eng, conn, _sock = self._eng()
        await eng.consider(form_agente())
        self.assertIn(99, eng._open_forms)
        eng._note_open_form({"chat_id": 99, "helpdesk_address": QUEUE}, "active")
        self.assertIn(99, eng._open_forms, "payload pobre nao pode descartar o formulario")
        self.assertIn("form_ignored_payload", events(conn))

    async def test_chat_removed_tira_do_indice_com_motivo(self):
        eng, conn, _sock = self._eng()
        await eng.consider(form_agente())
        await eng.on_note("chat_removed", {"chat_id": 99})
        self.assertNotIn(99, eng._open_forms)
        detalhe = conn.execute(
            "SELECT detail FROM bot_events WHERE reason = 'form_forget' ORDER BY id DESC LIMIT 1"
        ).fetchone()["detail"]
        self.assertIn("chat_removed", detalhe)

    async def test_varredura_de_boot_nao_fecha_caso_de_formulario(self):
        eng, conn, _sock = self._eng()
        db.assign_open(conn, "form", 99, QUEUE, 1, ANA, "n:message scenario agent")
        eng._sweep_stale_cases({})
        self.assertEqual(db.form_case_state(conn, 99), "open", "formulario vivo nao e stale_boot")

    async def test_bootstrap_hidrata_formulario_antes_da_varredura(self):
        """Restart: o formulário do ledger entra no índice e a varredura não o fecha."""
        eng, conn, _sock = self._eng()
        db.assign_open(conn, "form", 99, QUEUE, 1, ANA, "n:message scenario agent")
        await eng.bootstrap()
        self.assertIn(99, eng._open_forms)
        self.assertEqual(db.form_case_state(conn, 99), "open")


class FormStateTest(unittest.IsolatedAsyncioTestCase):
    """Estados do formulário (pendente / em atendimento / passado) e o gate do '1'."""

    def tearDown(self):
        MEM.clear()

    def _eng(self, online=("ana",)):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        insert_option(
            conn, menu_id=1, sort_order=1, label="Validação",
            classification="TESTE", action="reply_and_close", reply_text="ok",
        )
        sock = AwareSock(online=online)
        eng = Engine(conn, sock)
        eng.cfg["transfer_retry_interval_sec"] = "0"
        eng.cfg["form_route_retry_interval_sec"] = "0"
        return eng, conn, sock

    def _msg(self, text: str = "1", autor: str = TEC, cid: int = 10) -> dict:
        return {"chat_id": cid, "author": autor, "text": text, "time": 1700000000}

    # --- passos 1 e 2: estados do formulario -----------------------------

    async def test_boot_descarta_formulario_passado(self):
        eng, conn, sock = self._eng()
        db.assign_open(conn, "form", 99, QUEUE, 1, ANA, "a:u:tec@claro.com")
        sock.msgs[99] = [{"author": ANA, "system_message": "user_left", "time": 3}]
        await eng.bootstrap()
        self.assertNotIn(99, eng._open_forms)
        self.assertEqual(db.form_case_state(conn, 99), "done")

    async def test_varredura_revalida_um_formulario_por_tick(self):
        eng, conn, sock = self._eng()
        await eng.consider(form_agente())
        sock.msgs[99] = [{"author": ANA, "system_message": "user_left", "time": 3}]
        eng._form_checked.clear()  # estado "velho"
        await eng.tick_timeouts()
        self.assertNotIn(99, eng._open_forms)
        self.assertIn("form_closed", events(conn))


    # --- passo 3: nunca oferecer duas vezes ------------------------------

    # --- passo 5: a resposta do tecnico chega pelo caminho real ----------

    async def test_mensagem_em_status_nao_tratado_registra_evento(self):
        eng, conn, _sock = self._eng()
        db.upsert_chat(conn, 10, queue_address=QUEUE, status="monitoring")
        MEM[10] = {"started_addr": TEC, "queue_address": QUEUE}
        await eng.on_note("message_received", self._msg("oi"))
        self.assertIn("message_ignored", events(conn))
        detalhe = conn.execute(
            "SELECT detail FROM bot_events WHERE reason = 'message_ignored' ORDER BY id DESC LIMIT 1"
        ).fetchone()["detail"]
        self.assertIn("status=monitoring", detalhe)


class TemplateRepairTest(unittest.TestCase):
    """Os textos do fluxo normal nao podem ter barra+n literal (seed com char(10))."""

    def test_sementes_nao_tem_newline_literal(self):
        conn = mem_conn()
        for slug, corpo in db.templates(conn).items():
            self.assertNotIn("\\n", corpo or "", f"{slug} com literal")


class CaseIdentityTest(unittest.TestCase):
    """Identidade do caso: nome generico de agente nunca vira caso (o "balde")."""

    def test_formulario_sem_identidade_tem_chave_por_formulario(self):
        chave, addr, _nome, origem = form_open.requester_key(form_agente(), OWN)
        self.assertEqual(chave, "chat:99")
        self.assertEqual(origem, "chat")
        self.assertEqual(addr, "")

    def test_dois_formularios_sem_identidade_nao_se_misturam(self):
        a = form_open.requester_key(form_agente(cid=99), OWN)[0]
        b = form_open.requester_key(form_agente(cid=98), OWN)[0]
        self.assertNotEqual(a, b)
        self.assertNotIn("scenario agent", (a + b).lower())

    def test_nome_de_pessoa_continua_por_nome(self):
        chave, _a, _n, origem = form_open.requester_key(form_agente(person="JOSE DA SILVA"), OWN)
        self.assertEqual(chave, "n:jose da silva")
        self.assertEqual(origem, "name")

    def test_address_continua_mandando(self):
        chave, _a, _n, origem = form_open.requester_key(form_chat(), OWN)
        self.assertEqual(chave, "a:u:tec@claro.com")
        self.assertEqual(origem, "address")

    def test_reparo_do_balde_no_banco(self):
        conn = mem_conn()
        db.assign_open(conn, "form", 99, QUEUE, 1, ANA, "n:message scenario agent")
        db.assign_open(conn, "form", 98, QUEUE, 1, ANA, "n:message scenario agent")
        db.assign_open(conn, "form", 97, QUEUE, 1, ANA, "a:u:tec@claro.com")
        db._migrate_case_keys(conn)
        chaves = {
            r["chat_id"]: r["case_key"]
            for r in conn.execute("SELECT chat_id, case_key FROM route_assignments").fetchall()
        }
        self.assertEqual(chaves[99], "chat:99")
        self.assertEqual(chaves[98], "chat:98")
        self.assertEqual(chaves[97], "a:u:tec@claro.com", "quem tem identidade nao muda")

    def test_dialeto_legado_do_case_key_e_normalizado(self):
        """Chave de address sem o prefixo `a:` (legado) passa a casar na afinidade."""
        conn = mem_conn()
        db.assign_open(conn, "form", 991, QUEUE, 1, ANA, "u:tec@claro.com")
        conn.execute(
            "INSERT INTO pending_routes (kind, chat_id, status, case_key)"
            " VALUES ('form', 992, 'waiting', 'u:outro@claro.com')"
        )
        db._migrate_case_keys(conn)
        self.assertEqual(
            conn.execute("SELECT case_key FROM route_assignments WHERE chat_id = 991").fetchone()["case_key"],
            "a:u:tec@claro.com",
        )
        self.assertEqual(
            conn.execute("SELECT case_key FROM pending_routes WHERE chat_id = 992").fetchone()["case_key"],
            "a:u:outro@claro.com",
        )
        # afinidade enxerga o dono do caso nos dois dialetos (antes o caso aberto
        # no dialeto antigo ficava invisivel e virava caso duplicado)
        self.assertEqual(db.case_owner(conn, "a:u:tec@claro.com"), ANA)
        self.assertEqual(db.case_owner(conn, "u:tec@claro.com"), ANA)


class DistribuicaoECapTest(unittest.IsolatedAsyncioTestCase):
    """Cap no rodizio, afinidade direta e a fila unica (chat + formulario)."""

    def tearDown(self):
        MEM.clear()

    def _eng(self, *, cap_cases=1, online=("ana", "bia"), com_bia=True):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)  # T1 + Ana
        if com_bia:
            conn.execute(
                "INSERT INTO team_members (team_id, address, name, active)"
                " VALUES (1, 'u:bia@claro.com', 'Bia', 1)"
            )
        insert_option(
            conn, menu_id=1, sort_order=1, label="Validação",
            classification="TESTE", action="reply_and_close",
        )
        conn.execute(
            "UPDATE bot_settings SET value = ? WHERE key = 'max_cases_per_agent'", (str(cap_cases),)
        )
        sock = AwareSock(online=online)
        eng = Engine(conn, sock)
        eng.cfg["max_cases_per_agent"] = str(cap_cases)
        eng.cfg["transfer_retry_interval_sec"] = "0"
        eng.cfg["form_route_retry_interval_sec"] = "0"
        return eng, conn, sock

    @staticmethod
    def _form(cid: int, tec: str) -> dict:
        return form_agente(
            cid=cid, started_addr=tec, participants={tec: {"address": tec, "present": True}}
        )

    async def test_rajada_distribui_e_respeita_o_cap(self):
        eng, conn, _sock = self._eng(cap_cases=1)
        for i, tec in enumerate(["u:t1@x", "u:t2@x", "u:t3@x", "u:t4@x"]):
            await eng.consider(self._form(900 + i, tec))
        abertos = conn.execute(
            "SELECT assignee FROM route_assignments WHERE closed_at IS NULL"
        ).fetchall()
        por_analista: dict[str, int] = {}
        for r in abertos:
            por_analista[r["assignee"]] = por_analista.get(r["assignee"], 0) + 1
        self.assertEqual(por_analista.get(ANA), 1, "Ana no cap de 1")
        self.assertEqual(por_analista.get("u:bia@claro.com"), 1, "Bia no cap de 1")
        self.assertEqual(len(routes(conn, "waiting")), 2, "as outras 2 esperam na fila")

    async def test_libera_para_quem_esvaziou_o_volume(self):
        eng, conn, _sock = self._eng(cap_cases=1)
        for i, tec in enumerate(["u:t1@x", "u:t2@x"]):
            await eng.consider(self._form(900 + i, tec))
        await eng.consider(self._form(910, "u:t3@x"))
        self.assertEqual(len(routes(conn, "waiting")), 1, "terceiro espera")
        ana = conn.execute(
            "SELECT chat_id FROM route_assignments WHERE assignee = ? AND closed_at IS NULL", (ANA,)
        ).fetchone()
        # Fechar PELO ENGINE (devolve a reserva) é o caminho real de "atendeu e liberou".
        eng._close_case(int(ana["chat_id"]), "atendido")
        await eng.tick_routes()
        self.assertEqual(routes(conn, "waiting"), [], "a fila andou")
        novo = conn.execute(
            "SELECT assignee FROM route_assignments WHERE closed_at IS NULL AND chat_id = 910"
        ).fetchone()
        self.assertEqual(novo["assignee"], ANA, "foi para quem liberou vaga")

    async def test_afinidade_entrega_direto_mesmo_no_cap(self):
        eng, conn, _sock = self._eng(cap_cases=1)
        await eng.consider(self._form(900, "u:t1@x"))  # rodizio: Ana
        await eng.consider(self._form(901, "u:t1@x"))  # mesmo tecnico: afinidade
        abertos = conn.execute(
            "SELECT assignee, case_key FROM route_assignments WHERE closed_at IS NULL"
        ).fetchall()
        self.assertEqual(len(abertos), 2)
        self.assertTrue(all(r["assignee"] == ANA for r in abertos), "afinidade entrega ao dono")
        self.assertEqual({r["case_key"] for r in abertos}, {"a:u:t1@x"}, "mesmo caso")

    async def test_formulario_sem_elegivel_espera_sem_esgotar_tentativas(self):
        # Um analista so, com a vaga cheia: nao ha para quem entregar.
        eng, conn, _sock = self._eng(cap_cases=1, online=("ana",), com_bia=False)
        eng.cfg["form_route_attempts"] = "1"
        await eng.consider(self._form(900, "u:t1@x"))
        await eng.consider(self._form(901, "u:t2@x"))
        for _ in range(3):
            await eng.tick_routes()
        self.assertEqual(len(routes(conn, "waiting")), 1, "com a fila ligada o formulario espera")
        detalhes = [
            (r["detail"] or "")
            for r in conn.execute("SELECT detail FROM bot_events WHERE reason = 'form_no_agent'").fetchall()
        ]
        self.assertFalse(
            any("tentativas esgotadas" in d for d in detalhes),
            "a fila ligada nao deixa o formulario morrer nas tentativas",
        )

    def test_posicao_conta_chat_e_formulario_na_mesma_fila(self):
        conn = mem_conn()
        rid_chat = db.enqueue_route(conn, "chat", 10, "h:q1", 1, delay_sec=0)
        rid_form = db.enqueue_route(conn, "form", 99, "h:q1", 1, delay_sec=0)
        self.assertEqual(db.queue_position(conn, rid_chat), 1)
        self.assertEqual(db.queue_position(conn, rid_form), 2, "fila normal para os dois tipos")


if __name__ == "__main__":
    unittest.main()

