# -*- coding: utf-8 -*-
"""Script criar_acesso_hub: base de hubs, wizard dinamico e texto do webhook."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python.scripts import criar_acesso_hub as hub  # noqa: E402

ENV = {"WEBHOOK_ACESSO_HUB": "http://example.test/hook/acesso-hub"}

ANSWERS = {
    "hub": "HUBSAM - SANTO AMARO",
    "incidente": "RAL-569474/2026",
    "qtd_tecnicos": "2",
    "tec_1_empresa": "Claro",
    "tec_1_nome": "José de Jesus Barros",
    "tec_1_rg": "14253236-X",
    "tec_1_cpf": "025.555.652-85",
    "tec_2_empresa": "Claro",
    "tec_2_nome": "Marcos Romeu Gerardi",
    "tec_2_rg": "12653236-X",
    "tec_2_cpf": "021.563.652-90",
    "motivo": "TESTES COM OTDR RAL-569474/2026",
}

ESPERADO = (
    "HUB DE ACESSO:  HUBSAM - SANTO AMARO\n"
    "\n"
    "Incidente: RAL-569474/2026\n"
    "\n"
    "Motivo: TESTES COM OTDR RAL-569474/2026\n"
    "\n"
    "Técnico 1:  \n"
    "Empresa: Claro\n"
    "Nome Completo: José de Jesus Barros\n"
    "RG: 14253236-X\n"
    "CPF: 025.555.652-85\n"
    "\n"
    "Técnico 2:  \n"
    "Empresa: Claro\n"
    "Nome Completo: Marcos Romeu Gerardi\n"
    "RG: 12653236-X\n"
    "CPF: 021.563.652-90"
)


class BaseHubTest(unittest.TestCase):
    def test_base_tem_32_registros(self):
        self.assertEqual(len(hub.load_hubs()), 32)

    def test_hub_e_distrito_exatos(self):
        self.assertEqual(hub.buscar_hub("HUBSAM"), ("HUBSAM - SANTO AMARO", []))
        self.assertEqual(hub.buscar_hub("santo amaro"), ("HUBSAM - SANTO AMARO", []))
        self.assertEqual(hub.buscar_hub("VILA MARIANA"), ("HEVMN - VILA MARIANA", []))

    def test_vmn_acha_por_trecho_do_codigo(self):
        exato, sugestoes = hub.buscar_hub("VMN")
        self.assertEqual(exato, "")
        self.assertIn("HEVMN - VILA MARIANA", sugestoes)
        self.assertIn("HEDVMN - CAMBUCI", sugestoes)

    def test_distrito_ambiguo_vira_lista(self):
        exato, sugestoes = hub.buscar_hub("BUTANTA")
        self.assertEqual(exato, "")
        self.assertEqual(sorted(sugestoes), ["HEBUT - BUTANTA", "HEDBUT - BUTANTA"])

    def test_typo_aproxima(self):
        exato, sugestoes = hub.buscar_hub("vila mariiana")
        self.assertEqual(exato, "")
        self.assertEqual(sugestoes[0], "HEVMN - VILA MARIANA")

    def test_sugestoes_limitadas_a_10(self):
        exato, sugestoes = hub.buscar_hub("HUB")
        self.assertEqual(exato, "")
        self.assertTrue(sugestoes)
        self.assertLessEqual(len(sugestoes), hub.MAX_SUGESTOES)

    def test_nada_parecido(self):
        self.assertEqual(hub.buscar_hub("ZZZQQQ"), ("", []))
        self.assertEqual(hub.buscar_hub("A"), ("", []))


class FieldsForTest(unittest.TestCase):
    def test_sequencia_hub_incidente_qtd_blocos_motivo(self):
        self.assertEqual([f["key"] for f in hub.fields_for({})], ["hub"])
        com_hub = {"hub": "HUBSAM - SANTO AMARO"}
        self.assertEqual(
            [f["key"] for f in hub.fields_for(com_hub)],
            ["hub", "incidente", "qtd_tecnicos"],
        )
        com_inc = {**com_hub, "incidente": "RAL-569474/2026"}
        self.assertEqual(
            [f["key"] for f in hub.fields_for(com_inc)],
            ["hub", "incidente", "qtd_tecnicos"],
        )
        com_qtd = {**com_inc, "qtd_tecnicos": "2"}
        self.assertEqual(
            [f["key"] for f in hub.fields_for(com_qtd)],
            [
                "hub", "incidente", "qtd_tecnicos",
                "tec_1_empresa", "tec_1_nome", "tec_1_rg", "tec_1_cpf",
                "tec_2_empresa", "tec_2_nome", "tec_2_rg", "tec_2_cpf",
                "motivo",
            ],
        )

    def test_limite_de_4_tecnicos(self):
        base = {"hub": "HUBSAM - SANTO AMARO", "incidente": "RAL-569474/2026"}
        acima = {**base, "qtd_tecnicos": "9"}
        self.assertEqual(
            [f["key"] for f in hub.fields_for(acima)],
            ["hub", "incidente", "qtd_tecnicos"],
        )
        quatro = {**base, "qtd_tecnicos": "4"}
        self.assertEqual(len(hub.fields_for(quatro)), 3 + 4 * 4 + 1)

    def test_prompt_do_bloco_tem_cabecalho(self):
        base = {"hub": "HUBSAM - SANTO AMARO", "incidente": "RAL-569474/2026"}
        fields = hub.fields_for({**base, "qtd_tecnicos": "1"})
        self.assertTrue(fields[3]["prompt"].startswith("Técnico 1:"))
        self.assertEqual(fields[4]["prompt"], "Nome Completo")
        self.assertEqual(fields[3]["label"], "Técnico 1 — Empresa")

    def test_sugestoes_viram_lista_numerada_sem_consumir_tentativa(self):
        answers: dict = {}
        norm = hub.normalize_answer("hub", "VMN", answers)
        self.assertIsNone(norm["value"])
        self.assertFalse(norm["retry"])
        answers.update(norm["state"])
        field = hub.fields_for(answers)[0]
        self.assertEqual(field["type"], "choice")
        self.assertTrue(field["prompt"].startswith('Não localizei "VMN"'))
        self.assertEqual(field["label"], "Hub/site")
        escolhido = hub.normalize_answer("hub", "1", answers)
        self.assertEqual(escolhido["value"], field["choices"][0])


class NormalizeAnswerTest(unittest.TestCase):
    def test_hub_exato(self):
        norm = hub.normalize_answer("hub", "HUBSAM", {})
        self.assertEqual(norm["value"], "HUBSAM - SANTO AMARO")
        self.assertFalse(norm["retry"])

    def test_hub_desconhecido_conta_tentativa_e_avisa_na_pergunta(self):
        norm = hub.normalize_answer("hub", "ZZZQQQ", {})
        self.assertIsNone(norm["value"])
        self.assertTrue(norm["retry"])
        prompt = hub.fields_for(dict(norm["state"]))[0]["prompt"]
        self.assertIn('Não localizei "ZZZQQQ"', prompt)

    def test_opcao_fora_da_lista(self):
        answers = {"_hub_opcoes": ["HEVMN - VILA MARIANA"], "_hub_raw": "VMN"}
        norm = hub.normalize_answer("hub", "9", answers)
        self.assertIsNone(norm["value"])
        self.assertTrue(norm["retry"])
        self.assertIn("1 a 1", norm["message"])

    def test_nome_digitado_com_a_lista_na_tela_volta_a_casar(self):
        answers = {"_hub_opcoes": ["HEVMN - VILA MARIANA"], "_hub_raw": "VMN"}
        norm = hub.normalize_answer("hub", "HUBSAM", answers)
        self.assertEqual(norm["value"], "HUBSAM - SANTO AMARO")
        self.assertFalse(norm["retry"])

    def test_qtd_aceita_1_a_4(self):
        self.assertEqual(hub.normalize_answer("qtd_tecnicos", "2", {})["value"], "2")
        for ruim in ("0", "5", "dois", ""):
            norm = hub.normalize_answer("qtd_tecnicos", ruim, {})
            self.assertIsNone(norm["value"])
            self.assertTrue(norm["retry"])
            self.assertTrue(norm["message"])

    def test_cpf_valido_e_formatado(self):
        norm = hub.normalize_answer("tec_1_cpf", "52998224725", {})
        self.assertEqual(norm["value"], "529.982.247-25")
        self.assertFalse(norm["retry"])

    def test_cpf_repetido_ou_curto_recusa(self):
        for ruim in ("111.111.111-11", "00000000000", "123", "abc"):
            norm = hub.normalize_answer("tec_1_cpf", ruim, {})
            self.assertIsNone(norm["value"])
            self.assertTrue(norm["retry"])
            self.assertIn("CPF inválido", norm["message"])

    def test_incidente_rg_nome_empresa_e_motivo(self):
        self.assertEqual(
            hub.normalize_answer("incidente", "RAL-569474/2026", {})["value"],
            "RAL-569474/2026",
        )
        self.assertEqual(
            hub.normalize_answer("incidente", "inc 1234", {})["value"], "inc 1234"
        )
        self.assertIsNone(hub.normalize_answer("incidente", "", {})["value"])
        self.assertIsNone(hub.normalize_answer("incidente", "x" * 16, {})["value"])
        self.assertEqual(
            hub.normalize_answer("tec_1_rg", "14253 236-x", {})["value"], "14253236-X"
        )
        self.assertIsNone(hub.normalize_answer("tec_1_rg", "12", {})["value"])
        self.assertEqual(
            hub.normalize_answer("tec_2_nome", "José  de Jesus Barros", {})["value"],
            "José de Jesus Barros",
        )
        self.assertIsNone(hub.normalize_answer("tec_2_nome", "Abc", {})["value"])
        self.assertIsNone(hub.normalize_answer("tec_2_empresa", "C", {})["value"])
        self.assertEqual(
            hub.normalize_answer("motivo", "Teste  com OTDR", {})["value"],
            "Teste com OTDR",
        )
        self.assertIsNone(hub.normalize_answer("motivo", "abc", {})["value"])

    def test_chave_desconhecida_recusa(self):
        self.assertIsNone(hub.normalize_answer("qualquer", "x", {})["value"])


class BuildMessageTest(unittest.TestCase):
    def test_layout_esperado(self):
        self.assertEqual(hub.build_message(ANSWERS), ESPERADO)

    def test_um_tecnico_nao_inventa_bloco(self):
        um = {
            k: v
            for k, v in ANSWERS.items()
            if k in ("hub", "incidente", "motivo") or k.startswith("tec_1_")
        }
        um["qtd_tecnicos"] = "1"
        texto = hub.build_message(um)
        self.assertIn("Técnico 1:  ", texto)
        self.assertNotIn("Técnico 2", texto)


class RunWebhookTest(unittest.IsolatedAsyncioTestCase):
    async def test_200_encerra_e_posta_o_texto(self):
        captured = {}

        def fake_post(url, payload, headers, *a, **k):
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = headers
            return 200, {"ok": True}

        with patch.dict(os.environ, ENV, clear=False):
            r = await hub.run({"answers": ANSWERS}, http_post=fake_post)
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "reply_and_close")
        self.assertEqual(r["message"], hub.SUCCESS_MESSAGE)
        self.assertEqual(captured["url"], ENV["WEBHOOK_ACESSO_HUB"])
        self.assertEqual(captured["payload"], {"text": ESPERADO})
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertIn("http 200", r["detail"])

    async def test_500_transfere_sem_vazar_dados_do_tecnico(self):
        def fake_post(url, payload, headers, *a, **k):
            return 500, {"erro": "falha xyz"}

        with patch.dict(os.environ, ENV, clear=False):
            r = await hub.run({"answers": ANSWERS}, http_post=fake_post)
        self.assertFalse(r["ok"])
        self.assertEqual(r["action"], "ask_and_transfer")
        self.assertEqual(r["message"], hub.ERROR_MESSAGE)
        self.assertIn("http 500", r["detail"])
        self.assertIn("falha xyz", r["detail"])
        self.assertNotIn("025.555.652-85", r["detail"])
        self.assertNotIn("José", r["detail"])

    async def test_sem_url_falha_config(self):
        limpo = {k: v for k, v in os.environ.items() if k != "WEBHOOK_ACESSO_HUB"}
        with patch.dict(os.environ, limpo, clear=True):
            r = await hub.run({"answers": ANSWERS}, http_post=lambda *a: (200, {}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["detail"], "config")

    async def test_respostas_incompletas(self):
        with patch.dict(os.environ, ENV, clear=False):
            r = await hub.run(
                {"answers": {"hub": "HUBSAM - SANTO AMARO"}},
                http_post=lambda *a: (200, {}),
            )
        self.assertFalse(r["ok"])
        self.assertEqual(r["detail"], "bad_answers")

    async def test_falha_de_transporte(self):
        def boom(*a, **k):
            raise OSError("sem rota")

        with patch.dict(os.environ, ENV, clear=False):
            r = await hub.run({"answers": ANSWERS}, http_post=boom)
        self.assertFalse(r["ok"])
        self.assertEqual(r["detail"], "http_error")

    async def test_sem_resposta_http(self):
        with patch.dict(os.environ, ENV, clear=False):
            r = await hub.run({"answers": ANSWERS}, http_post=lambda *a: (None, {}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["detail"], "http_error")


class ErroSemUrlTest(unittest.TestCase):
    def test_url_do_webhook_nao_vaza_no_log(self):
        texto = hub._erro_sem_url(OSError("falhou em https://x.test/invoke?sig=SEGREDO"))
        self.assertNotIn("SEGREDO", texto)
        self.assertIn("<url>", texto)

    def test_erro_de_transporte_nao_grava_url_no_detail(self):
        def boom(*a, **k):
            raise OSError("sem rota em " + ENV["WEBHOOK_ACESSO_HUB"])

        with patch.dict(os.environ, ENV, clear=False):
            r = asyncio.run(hub.run({"answers": ANSWERS}, http_post=boom))
        self.assertEqual(r["detail"], "http_error")
        self.assertNotIn("example.test", r["detail"])


if __name__ == "__main__":
    unittest.main()
