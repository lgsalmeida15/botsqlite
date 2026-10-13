# -*- coding: utf-8 -*-
"""Wizard de script no chat: perguntas, confirmar, corrigir, fechar sem join extra."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import Engine  # noqa: E402
from tests.test_submenu import FakeSock, awaiting, insert_option, mem_conn, seed_queue_menu  # noqa: E402

ENV_APOIO = {
    "APOIO_API_URL": "http://example.test/api/v1/toa/criar_apoio?ambiente=PRODUCAO",
    "APOIO_STATUS_URL": "http://example.test/api/v1/toa",
    "APOIO_API_KEY": "sekret",
}
ENV_HUB = {"WEBHOOK_ACESSO_HUB": "http://example.test/hook/acesso-hub"}
ENV_ROTA = {
    "REMANEJAMENTO_API_URL": "http://example.test:8101/api/v1/toa/realocar_tarefa?ambiente=PRODUCAO",
    "APOIO_STATUS_URL": "http://example.test/api/v1/toa",
    "APOIO_API_KEY": "sekret",
}


def seed_apoio_leaf(conn) -> int:
    parent = insert_option(
        conn,
        menu_id=1,
        sort_order=1,
        label="Coord.",
        classification="",
        action="submenu",
    )
    insert_option(
        conn,
        menu_id=1,
        parent_id=parent,
        sort_order=1,
        label="Apoio",
        classification="apoio",
        action="run_script",
        script_slug="criar_apoio",
    )
    return parent


def seed_infra_leaf(conn) -> int:
    parent = insert_option(
        conn,
        menu_id=1,
        sort_order=1,
        label="Coord. Infra",
        classification="",
        action="submenu",
    )
    insert_option(
        conn,
        menu_id=1,
        parent_id=parent,
        sort_order=1,
        label="Infra",
        classification="infra",
        action="run_script",
        script_slug="criar_infra_rede",
    )
    return parent


def seed_hub_leaf(conn) -> int:
    parent = insert_option(
        conn,
        menu_id=1,
        sort_order=1,
        label="Acesso Hub",
        classification="",
        action="submenu",
    )
    insert_option(
        conn,
        menu_id=1,
        parent_id=parent,
        sort_order=1,
        label="Acesso Hub",
        classification="hub",
        action="run_script",
        script_slug="criar_acesso_hub",
    )
    return parent


def seed_rota_leaf(conn) -> int:
    parent = insert_option(
        conn,
        menu_id=1,
        sort_order=1,
        label="Coord. Rota",
        classification="",
        action="submenu",
    )
    insert_option(
        conn,
        menu_id=1,
        parent_id=parent,
        sort_order=1,
        label="Remanejar rota",
        classification="apoio",
        action="run_script",
        script_slug="remanejamento_rota",
    )
    return parent


def row_of(conn, cid: int = 10) -> dict:
    return conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone()


class ScriptWizardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from python.engine import MEM

        MEM.clear()
        self._gate = patch(
            "python.engine.resource_profile.lookup",
            new_callable=AsyncMock,
            return_value={"allowed": True, "login": "tec", "perfil": "Gestor RE3", "reason": "ok"},
        )
        self._gate.start()
        self.addCleanup(self._gate.stop)
        self.addCleanup(MEM.clear)

    def _eng(self, conn) -> tuple[Engine, FakeSock]:
        sock = FakeSock()
        return Engine(conn, sock), sock

    async def _answer_form(self, eng: Engine, conn) -> None:
        await eng.handle_reply(10, row_of(conn), "123")
        await eng.handle_reply(10, row_of(conn), "tec.apoio")

    async def test_perguntas_confirma_e_encerra_sem_join(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        parent = seed_apoio_leaf(conn)
        eng, sock = self._eng(conn)
        await eng.handle_reply(10, awaiting(conn), "1")
        chat = row_of(conn)
        self.assertEqual(int(chat["menu_node_id"]), parent)
        await eng.handle_reply(10, chat, "1")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_answer")
        self.assertEqual(chat["script_slug"], "criar_apoio")
        self.assertIn("Qual ID da atividade?", sock.sent_texts()[-1])

        await eng.handle_reply(10, chat, "abc")
        self.assertEqual(row_of(conn)["status"], "awaiting_script_answer")
        self.assertIn("Qual ID da atividade?", sock.sent_texts()[-1])

        await self._answer_form(eng, conn)
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_confirm")
        answers = json.loads(chat["script_answers"])
        self.assertEqual(answers["atividade"], "123")
        self.assertEqual(answers["login_apoio"], "tec.apoio")
        conf = sock.sent_texts()[-1]
        self.assertIn("Confirme as informações:", conf)
        self.assertIn("Qual ID da atividade: 123.", conf)
        self.assertIn("Qual Login do tec. Apoio: tec.apoio.", conf)
        self.assertIn("Digite:", conf)
        self.assertIn("1) Confirmar", conf)
        self.assertIn("2) Corrigir", conf)

        with patch.dict(os.environ, ENV_APOIO, clear=False), patch(
            "python.scripts.criar_apoio._post_json", return_value=(200, {"status": "SUCESSO"})
        ):
            await eng.handle_reply(10, chat, "1")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "closed")
        self.assertEqual(chat["classification"], "apoio")
        self.assertTrue(chat["script_uuid"])
        texts = sock.sent_texts()
        self.assertTrue(any("Apoio criado com sucesso" in t for t in texts))
        cmds = [n for n, _ in sock.cmds]
        self.assertIn("leave_chat", cmds)
        self.assertNotIn("join_chat", cmds)
        reasons = [e["reason"] for e in conn.execute("SELECT reason FROM bot_events").fetchall()]
        self.assertIn("script_ok", reasons)
        self.assertIn("classified", reasons)
        details = [
            e["detail"]
            for e in conn.execute("SELECT detail FROM bot_events WHERE reason = 'script_ok'").fetchall()
        ]
        self.assertTrue(any("http 200" in (d or "") for d in details))
        # O detail leva o slug do script: sem isso a aba Atividade so mosta
        # "http 200" e fica impossivel saber qual script rodou.
        self.assertTrue(any("slug=criar_apoio" in (d or "") for d in details))
        blob = " ".join(
            (e["detail"] or "") + (e["reason"] or "")
            for e in conn.execute("SELECT reason, detail FROM bot_events").fetchall()
        )
        self.assertNotIn("sekret", blob)

    async def test_corrigir_volta_a_pergunta_1(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_apoio_leaf(conn)
        eng, sock = self._eng(conn)
        await eng.handle_reply(10, awaiting(conn), "1")
        await eng.handle_reply(10, row_of(conn), "1")
        await self._answer_form(eng, conn)
        self.assertEqual(row_of(conn)["status"], "awaiting_script_confirm")
        await eng.handle_reply(10, row_of(conn), "2")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_answer")
        self.assertEqual(int(chat["script_field_index"] or 0), 0)
        self.assertEqual(json.loads(chat["script_answers"] or "{}"), {})
        self.assertIn("Qual ID da atividade?", sock.sent_texts()[-1])

    async def test_nome_do_autor_vai_no_payload(self):
        from python.engine import MEM

        conn = mem_conn()
        seed_queue_menu(conn)
        seed_apoio_leaf(conn)
        eng, sock = self._eng(conn)
        MEM[10] = {
            "started_addr": "u:tec@claro.com",
            "queue_address": "h:q1",
            "raw": {
                "chat_id": 10,
                "person": "Maria Gestora",
                "started_addr": "u:tec@claro.com",
                "helpdesk_address": "h:q1",
            },
        }
        await eng.handle_reply(10, awaiting(conn), "1")
        await eng.handle_reply(10, row_of(conn), "1")
        chat = row_of(conn)
        # O nome do autor fica guardado no estado interno do wizard.
        self.assertEqual(
            json.loads(chat["script_answers"])["_nome_solicitante"], "Maria Gestora"
        )
        await self._answer_form(eng, conn)
        conf = sock.sent_texts()[-1]
        self.assertIn("Confirme as informações:", conf)
        self.assertNotIn("nome_solicitante", conf)
        captured = {}

        def fake_post(url, payload, headers, *a, **k):
            captured["payload"] = payload
            return 200, {"status": "SUCESSO"}

        with patch.dict(os.environ, ENV_APOIO, clear=False), patch(
            "python.scripts.criar_apoio._post_json", side_effect=fake_post
        ):
            await eng.handle_reply(10, row_of(conn), "1")
        self.assertEqual(captured["payload"]["nome_solicitante"], "Maria Gestora")

    async def test_digito_invalido_na_confirmacao(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_apoio_leaf(conn)
        eng, sock = self._eng(conn)
        await eng.handle_reply(10, awaiting(conn), "1")
        await eng.handle_reply(10, row_of(conn), "1")
        await self._answer_form(eng, conn)
        await eng.handle_reply(10, row_of(conn), "9")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_confirm")
        self.assertEqual(int(chat["retries"]), 1)
        self.assertIn("1) Confirmar", sock.sent_texts()[-1])
        cmds = [n for n, _ in sock.cmds]
        self.assertNotIn("leave_chat", cmds)

    async def test_http_erro_transfere(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        seed_apoio_leaf(conn)
        eng, sock = self._eng(conn)
        await eng.handle_reply(10, awaiting(conn), "1")
        await eng.handle_reply(10, row_of(conn), "1")
        await self._answer_form(eng, conn)
        with patch.dict(os.environ, ENV_APOIO, clear=False), patch(
            "python.scripts.criar_apoio._post_json", return_value=(500, {})
        ):
            await eng.handle_reply(10, row_of(conn), "1")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "closed")
        texts = sock.sent_texts()
        self.assertTrue(any("Erro ao criar apoio automático" in t for t in texts))
        cmds = [n for n, _ in sock.cmds]
        self.assertIn("move_chat", cmds)
        self.assertIn("invite_to_chat", cmds)
        reasons = [e["reason"] for e in conn.execute("SELECT reason FROM bot_events").fetchall()]
        self.assertIn("script_error", reasons)
        det_err = [
            e["detail"]
            for e in conn.execute("SELECT detail FROM bot_events WHERE reason = 'script_error'").fetchall()
        ]
        self.assertTrue(any("slug=criar_apoio" in (d or "") for d in det_err))

    async def test_timeout_no_wizard_encerra(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_apoio_leaf(conn)
        eng, sock = self._eng(conn)
        await eng.handle_reply(10, awaiting(conn), "1")
        await eng.handle_reply(10, row_of(conn), "1")
        conn.execute(
            "UPDATE chats SET last_prompt_at = datetime('now', '-400 seconds') WHERE chat_id = 10"
        )
        await eng.tick_timeouts()
        self.assertEqual(row_of(conn)["status"], "closed_timeout")
        self.assertIn("leave_chat", [n for n, _ in sock.cmds])

    async def test_on_message_nao_trata_wizard_como_menu(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_apoio_leaf(conn)
        eng, sock = self._eng(conn)
        awaiting(conn)
        await eng.handle_reply(10, row_of(conn), "1")
        await eng.handle_reply(10, row_of(conn), "1")
        await eng.on_message({"chat_id": 10, "author": "u:tec@claro.com", "text": "nao-e-numero"})
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_answer")
        self.assertEqual(int(chat["retries"]), 1)
        self.assertIn("Qual ID da atividade?", sock.sent_texts()[-1])

    async def test_resume_run_consulta_uuid_sem_post(self):
        from datetime import datetime, timezone

        from python.engine import MEM
        from tests.test_resume import ResumeSock, active

        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        seed_apoio_leaf(conn)
        child = conn.execute(
            "SELECT id FROM menu_options WHERE action = 'run_script'"
        ).fetchone()["id"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db.upsert_chat(
            conn,
            10,
            queue_address="h:q1",
            status="awaiting_script_run",
            option_id=child,
            script_slug="criar_apoio",
            script_uuid="uid-salvo",
            script_started_at=now,
            script_answers=json.dumps({"atividade": "1", "login_apoio": "tec"}),
        )
        sock = ResumeSock(chats=[active(present=True)])
        eng = Engine(conn, sock)
        posts = []

        def boom_post(*a, **k):
            posts.append(1)
            raise AssertionError("POST no resume")

        with patch.dict(os.environ, ENV_APOIO, clear=False), patch(
            "python.scripts.criar_apoio._post_json", side_effect=boom_post
        ), patch(
            "python.scripts.criar_apoio._get_json",
            return_value=(200, {"status": "SUCESSO"}),
        ):
            await eng.resume_open_chats()
        self.assertEqual(posts, [])
        self.assertEqual(row_of(conn)["status"], "closed")
        self.assertTrue(any("Apoio criado com sucesso" in t for t in sock.sent_texts()))
        MEM.clear()


class ApoioProfileGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from python.engine import MEM
        from python import resource_profile

        resource_profile.reset_cache()
        MEM.clear()
        self.addCleanup(MEM.clear)
        self.addCleanup(resource_profile.reset_cache)

    def _eng(self, conn) -> tuple[Engine, FakeSock]:
        sock = FakeSock()
        return Engine(conn, sock), sock

    async def _choose_apoio(self, eng: Engine, conn) -> None:
        await eng.handle_reply(10, awaiting(conn), "1")
        await eng.handle_reply(10, row_of(conn), "1")

    async def test_nao_gestor_responde_e_encerra(self):
        from python.engine import MEM

        conn = mem_conn()
        seed_queue_menu(conn)
        seed_apoio_leaf(conn)
        MEM[10] = {"started_addr": "u:Z469299@claro.com", "queue_address": "h:q1"}
        eng, sock = self._eng(conn)
        denied = {
            "allowed": False,
            "login": "Z469299",
            "perfil": "Tecnico",
            "reason": "not_gestor",
        }
        with patch("python.engine.resource_profile.lookup", AsyncMock(return_value=denied)):
            await self._choose_apoio(eng, conn)
        chat = row_of(conn)
        self.assertEqual(chat["status"], "closed")
        self.assertIn("Usuário não habilitado para esta solicitação", sock.sent_texts()[-1])
        self.assertFalse(any("Qual ID da atividade?" in t for t in sock.sent_texts()))
        cmds = [n for n, _ in sock.cmds]
        self.assertIn("leave_chat", cmds)
        self.assertNotIn("move_chat", cmds)
        reasons = [e["reason"] for e in conn.execute("SELECT reason FROM bot_events").fetchall()]
        self.assertIn("script_denied", reasons)
        details = [
            e["detail"]
            for e in conn.execute("SELECT detail FROM bot_events WHERE reason = 'script_denied'").fetchall()
        ]
        self.assertTrue(any("login=Z469299" in (d or "") and "perfil=Tecnico" in (d or "") for d in details))

    async def test_not_found_encerra_com_detail(self):
        from python.engine import MEM

        conn = mem_conn()
        seed_queue_menu(conn)
        seed_apoio_leaf(conn)
        MEM[10] = {"started_addr": "u:Z000001@claro.com", "queue_address": "h:q1"}
        eng, sock = self._eng(conn)
        denied = {"allowed": False, "login": "Z000001", "perfil": "", "reason": "not_found"}
        with patch("python.engine.resource_profile.lookup", AsyncMock(return_value=denied)):
            await self._choose_apoio(eng, conn)
        self.assertEqual(row_of(conn)["status"], "closed")
        details = [
            e["detail"]
            for e in conn.execute("SELECT detail FROM bot_events WHERE reason = 'script_denied'").fetchall()
        ]
        self.assertTrue(any("login=Z000001" in (d or "") and "not_found" in (d or "") for d in details))

    async def test_timeout_encerra_sem_wizard(self):
        from python.engine import MEM

        conn = mem_conn()
        seed_queue_menu(conn)
        seed_apoio_leaf(conn)
        MEM[10] = {"started_addr": "u:Z469299@claro.com", "queue_address": "h:q1"}
        eng, sock = self._eng(conn)
        denied = {"allowed": False, "login": "Z469299", "perfil": "", "reason": "api_timeout"}
        with patch("python.engine.resource_profile.lookup", AsyncMock(return_value=denied)):
            await self._choose_apoio(eng, conn)
        self.assertEqual(row_of(conn)["status"], "closed")
        self.assertIn("Usuário não habilitado para esta solicitação", sock.sent_texts()[-1])
        details = [
            e["detail"]
            for e in conn.execute("SELECT detail FROM bot_events WHERE reason = 'script_denied'").fetchall()
        ]
        self.assertTrue(any("api_timeout" in (d or "") for d in details))


class InfraRedeWizardTest(unittest.IsolatedAsyncioTestCase):
    def _eng(self, conn) -> tuple[Engine, FakeSock]:
        sock = FakeSock()
        return Engine(conn, sock), sock

    async def test_rede_lista_opcoes_e_digito_vira_hfc(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_infra_leaf(conn)
        eng, sock = self._eng(conn)
        await eng.handle_reply(10, awaiting(conn), "1")
        await eng.handle_reply(10, row_of(conn), "1")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_answer")
        self.assertEqual(chat["script_slug"], "criar_infra_rede")
        self.assertIn("Qual o motivo?", sock.sent_texts()[-1])

        for ans in ("Troca de Poste", "São Paulo", "Rua da Gloria, 54", "LIBAC"):
            await eng.handle_reply(10, row_of(conn), ans)
        rede_prompt = sock.sent_texts()[-1]
        self.assertIn("Qual a rede?", rede_prompt)
        self.assertIn("1) HFC", rede_prompt)
        self.assertIn("2) GPON", rede_prompt)

        await eng.handle_reply(10, row_of(conn), "1")
        self.assertIn("Qual o Login do Técnico?", sock.sent_texts()[-1])
        await eng.handle_reply(10, row_of(conn), "Z679436")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_confirm")
        answers = json.loads(chat["script_answers"])
        self.assertEqual(answers["rede"], "HFC")
        conf = sock.sent_texts()[-1]
        self.assertIn("Qual a rede: HFC.", conf)


class AcessoHubWizardTest(unittest.IsolatedAsyncioTestCase):
    def _eng(self, conn) -> tuple[Engine, FakeSock]:
        sock = FakeSock()
        return Engine(conn, sock), sock

    async def _abre(self, eng: Engine, conn) -> None:
        await eng.handle_reply(10, awaiting(conn), "1")
        await eng.handle_reply(10, row_of(conn), "1")

    async def _informa_um_tecnico(self, eng: Engine, conn) -> None:
        await eng.handle_reply(10, row_of(conn), "HUBSAM")
        await eng.handle_reply(10, row_of(conn), "RAL-569474/2026")
        await eng.handle_reply(10, row_of(conn), "1")
        for ans in ("Claro", "José de Jesus Barros", "14253236-x", "529.982.247-25"):
            await eng.handle_reply(10, row_of(conn), ans)
        await eng.handle_reply(10, row_of(conn), "TESTES COM OTDR")

    async def test_fluxo_completo_com_sugestao_e_webhook(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_hub_leaf(conn)
        eng, sock = self._eng(conn)
        await self._abre(eng, conn)
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_answer")
        self.assertEqual(chat["script_slug"], "criar_acesso_hub")
        self.assertIn("Qual hub/site? Ex: VMN ou VILA MARIANA", sock.sent_texts()[-1])

        # Hub desconhecido vira lista numerada e NAO consome tentativa.
        await eng.handle_reply(10, chat, "vila mariiana")
        chat = row_of(conn)
        self.assertEqual(int(chat["retries"] or 0), 0)
        sugestao = sock.sent_texts()[-1]
        self.assertIn('Não localizei "vila mariiana"', sugestao)
        self.assertIn("1) HEVMN - VILA MARIANA", sugestao)

        await eng.handle_reply(10, chat, "1")
        self.assertIn("Qual número do Incidente? (RAL/REC/INM)", sock.sent_texts()[-1])

        await eng.handle_reply(10, row_of(conn), "RAL-569474/2026")
        self.assertIn("Quantos técnicos acessarão o local?", sock.sent_texts()[-1])

        await eng.handle_reply(10, row_of(conn), "2")
        self.assertIn("Técnico 1:", sock.sent_texts()[-1])

        await eng.handle_reply(10, row_of(conn), "Claro")
        self.assertEqual(sock.sent_texts()[-1], "Nome Completo")
        await eng.handle_reply(10, row_of(conn), "José de Jesus Barros")
        await eng.handle_reply(10, row_of(conn), "14253236-x")
        await eng.handle_reply(10, row_of(conn), "529.982.247-25")
        self.assertIn("Técnico 2:", sock.sent_texts()[-1])
        await eng.handle_reply(10, row_of(conn), "Claro")
        await eng.handle_reply(10, row_of(conn), "Marcos Romeu Gerardi")
        await eng.handle_reply(10, row_of(conn), "12653236-X")
        await eng.handle_reply(10, row_of(conn), "529.982.247-25")
        self.assertEqual(sock.sent_texts()[-1], "Informe o motivo do acesso")
        await eng.handle_reply(10, row_of(conn), "TESTES COM OTDR RAL-569474/2026")

        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_confirm")
        answers = json.loads(chat["script_answers"])
        self.assertEqual(answers["hub"], "HEVMN - VILA MARIANA")
        self.assertEqual(answers["qtd_tecnicos"], "2")
        self.assertEqual(answers["tec_1_rg"], "14253236-X")
        self.assertEqual(answers["tec_1_cpf"], "529.982.247-25")
        conf = "\n".join(sock.sent_texts()[-2:])  # resumo longo pode vir em 2 blocos
        self.assertIn("Confirme as informações:", conf)
        self.assertIn("Hub/site: HEVMN - VILA MARIANA.", conf)
        self.assertIn("Incidente: RAL-569474/2026.", conf)
        self.assertIn("Técnicos: 2.", conf)
        self.assertIn("Técnico 1 — Empresa: Claro.", conf)
        self.assertIn("Técnico 2 — CPF: 529.982.247-25.", conf)
        self.assertIn("Motivo: TESTES COM OTDR RAL-569474/2026.", conf)
        self.assertIn("Qualquer inconsistência pode ocasionar na recusa.", conf)
        self.assertIn("1) Confirmar", conf)
        self.assertIn("2) Corrigir", conf)

        capturado = {}

        def fake_post(url, payload, headers, *a, **k):
            capturado["url"] = url
            capturado["payload"] = payload
            return 200, {"ok": True}

        with patch.dict(os.environ, ENV_HUB, clear=False), patch(
            "python.scripts.criar_acesso_hub._post_json", side_effect=fake_post
        ):
            await eng.handle_reply(10, chat, "1")

        chat = row_of(conn)
        self.assertEqual(chat["status"], "closed")
        self.assertEqual(chat["classification"], "hub")
        self.assertTrue(
            any("Solicitação de acesso ao HUB enviada" in t for t in sock.sent_texts())
        )
        self.assertEqual(capturado["url"], ENV_HUB["WEBHOOK_ACESSO_HUB"])
        texto = capturado["payload"]["text"]
        self.assertIn("HUB DE ACESSO:  HEVMN - VILA MARIANA", texto)
        self.assertIn("Incidente: RAL-569474/2026", texto)
        self.assertIn("Motivo: TESTES COM OTDR RAL-569474/2026", texto)
        self.assertIn("Técnico 1:  \nEmpresa: Claro", texto)
        self.assertIn("Técnico 2:  \nEmpresa: Claro", texto)

    async def test_qtd_acima_do_limite_conta_tentativa(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_hub_leaf(conn)
        eng, sock = self._eng(conn)
        await self._abre(eng, conn)
        await eng.handle_reply(10, row_of(conn), "HUBSAM")
        await eng.handle_reply(10, row_of(conn), "RAL-0001/2026")
        self.assertIn("Quantos técnicos", sock.sent_texts()[-1])

        await eng.handle_reply(10, row_of(conn), "5")
        chat = row_of(conn)
        self.assertEqual(int(chat["retries"] or 0), 1)
        self.assertIn("O limite é de 4 técnicos", sock.sent_texts()[-2])
        self.assertIn("Quantos técnicos", sock.sent_texts()[-1])
        self.assertNotIn("qtd_tecnicos", json.loads(chat["script_answers"] or "{}"))

    async def test_cpf_invalido_avisa_e_nao_avanca(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_hub_leaf(conn)
        eng, sock = self._eng(conn)
        await self._abre(eng, conn)
        for ans in ("HUBSAM", "RAL-0001/2026", "1", "Claro", "José de Jesus Barros", "14253236-x"):
            await eng.handle_reply(10, row_of(conn), ans)
        await eng.handle_reply(10, row_of(conn), "123")
        chat = row_of(conn)
        self.assertEqual(int(chat["retries"] or 0), 1)
        self.assertIn("CPF inválido", sock.sent_texts()[-2])
        self.assertEqual(sock.sent_texts()[-1], "CPF")
        self.assertNotIn("tec_1_cpf", json.loads(chat["script_answers"] or "{}"))

    async def test_corrigir_volta_ao_hub(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_hub_leaf(conn)
        eng, sock = self._eng(conn)
        await self._abre(eng, conn)
        await self._informa_um_tecnico(eng, conn)
        self.assertEqual(row_of(conn)["status"], "awaiting_script_confirm")

        await eng.handle_reply(10, row_of(conn), "2")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_answer")
        self.assertEqual(int(chat["script_field_index"] or 0), 0)
        self.assertEqual(json.loads(chat["script_answers"] or "{}"), {})
        self.assertIn("Qual hub/site?", sock.sent_texts()[-1])


class RemanejamentoWizardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from python.engine import MEM

        MEM.clear()
        self._gate = patch(
            "python.engine.resource_profile.lookup",
            new_callable=AsyncMock,
            return_value={"allowed": True, "login": "tec", "perfil": "Gestor RE3", "reason": "ok"},
        )
        self._gate.start()
        self.addCleanup(self._gate.stop)
        self.addCleanup(MEM.clear)

    def _eng(self, conn) -> tuple[Engine, FakeSock]:
        sock = FakeSock()
        return Engine(conn, sock), sock

    async def _abre(self, eng: Engine, conn) -> None:
        await eng.handle_reply(10, awaiting(conn), "1")
        await eng.handle_reply(10, row_of(conn), "1")

    async def test_posicao_proxima_envia_payload_sem_apos(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_rota_leaf(conn)
        eng, sock = self._eng(conn)
        await self._abre(eng, conn)
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_answer")
        self.assertEqual(chat["script_slug"], "remanejamento_rota")
        self.assertIn("Informe Id da atividade", sock.sent_texts()[-1])

        await eng.handle_reply(10, chat, "42")
        self.assertIn("Informe Login do técnico", sock.sent_texts()[-1])
        await eng.handle_reply(10, row_of(conn), "Z679436")
        pos = sock.sent_texts()[-1]
        self.assertIn("Posição da atividade", pos)
        self.assertIn("1) Próxima da rota", pos)
        self.assertIn("2) Após outra atividade", pos)

        await eng.handle_reply(10, row_of(conn), "1")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_confirm")
        conf = sock.sent_texts()[-1]
        self.assertIn("Confirme as informações:", conf)
        self.assertIn("Atividade: 42.", conf)
        self.assertIn("Técnico: Z679436.", conf)
        self.assertIn("Posição: Próxima da rota.", conf)
        self.assertNotIn("Após a atividade", conf)

        captured = {}

        def fake_post(url, payload, headers, *a, **k):
            captured["url"] = url
            captured["payload"] = payload
            return 200, {"status": "SUCESSO"}

        with patch.dict(os.environ, ENV_ROTA, clear=False), patch(
            "python.scripts.remanejamento_rota._post_json", side_effect=fake_post
        ):
            await eng.handle_reply(10, chat, "1")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "closed")
        self.assertEqual(captured["payload"], {"tarefa_id": 42, "tecnico": "Z679436"})
        self.assertNotIn("posicao", captured["payload"])
        self.assertTrue(any("Remanejamento efetuado" in t for t in sock.sent_texts()))

    async def test_posicao_apos_pergunta_apos_e_envia(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_rota_leaf(conn)
        eng, sock = self._eng(conn)
        await self._abre(eng, conn)
        await eng.handle_reply(10, row_of(conn), "42")
        await eng.handle_reply(10, row_of(conn), "Z679436")
        await eng.handle_reply(10, row_of(conn), "2")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_answer")
        self.assertEqual(int(chat["script_field_index"]), 3)
        self.assertIn("Informe Id da atividade anterior", sock.sent_texts()[-1])

        await eng.handle_reply(10, row_of(conn), "41")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_confirm")
        conf = sock.sent_texts()[-1]
        self.assertIn("Posição: Após outra atividade.", conf)
        self.assertIn("Após a atividade: 41.", conf)

        captured = {}

        def fake_post(url, payload, headers, *a, **k):
            captured["payload"] = payload
            return 200, {"status": "SUCESSO"}

        with patch.dict(os.environ, ENV_ROTA, clear=False), patch(
            "python.scripts.remanejamento_rota._post_json", side_effect=fake_post
        ):
            await eng.handle_reply(10, row_of(conn), "1")
        self.assertEqual(
            captured["payload"],
            {
                "tarefa_id": 42,
                "tecnico": "Z679436",
                "posicao": "apos_tarefa",
                "apos_tarefa_id": 41,
            },
        )

    async def test_id_invalido_avisa_e_nao_avanca(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_rota_leaf(conn)
        eng, sock = self._eng(conn)
        await self._abre(eng, conn)
        await eng.handle_reply(10, row_of(conn), "0")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_answer")
        self.assertEqual(int(chat["retries"] or 0), 1)
        self.assertIn("Id da atividade inválido", sock.sent_texts()[-2])
        self.assertEqual(sock.sent_texts()[-1], "Informe Id da atividade a ser remanejada")
        self.assertNotIn("tarefa_id", json.loads(chat["script_answers"] or "{}"))

    async def test_apos_igual_a_tarefa_avisa(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        seed_rota_leaf(conn)
        eng, sock = self._eng(conn)
        await self._abre(eng, conn)
        await eng.handle_reply(10, row_of(conn), "42")
        await eng.handle_reply(10, row_of(conn), "Z679436")
        await eng.handle_reply(10, row_of(conn), "2")
        await eng.handle_reply(10, row_of(conn), "42")
        chat = row_of(conn)
        self.assertEqual(chat["status"], "awaiting_script_answer")
        self.assertIn("não pode ser a anterior de si mesma", sock.sent_texts()[-2])
        self.assertNotIn("apos_tarefa_id", json.loads(chat["script_answers"] or "{}"))


class ResumeScriptStatusTest(unittest.TestCase):
    def test_timeouts_incluem_wizard(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        db.upsert_chat(
            conn,
            11,
            queue_address="h:q1",
            status="awaiting_script_answer",
            last_prompt_at="2000-01-01 00:00:00",
        )
        rows = db.awaiting_timeouts(conn, 300)
        self.assertTrue(any(int(r["chat_id"]) == 11 for r in rows))
        statuses = {r["status"] for r in db.resume_chats(conn)}
        self.assertIn("awaiting_script_answer", statuses)

    def test_run_nao_entra_no_timeout_de_5min(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        db.upsert_chat(
            conn,
            12,
            queue_address="h:q1",
            status="awaiting_script_run",
            script_uuid="abc",
            script_started_at="2000-01-01 00:00:00",
            last_prompt_at="2000-01-01 00:00:00",
        )
        ids = {int(r["chat_id"]) for r in db.awaiting_timeouts(conn, 300)}
        self.assertNotIn(12, ids)
        statuses = {r["status"] for r in db.resume_chats(conn)}
        self.assertIn("awaiting_script_run", statuses)
        timed = db.script_run_timeouts(conn, 60)
        self.assertTrue(any(int(r["chat_id"]) == 12 for r in timed))


if __name__ == "__main__":
    unittest.main()
