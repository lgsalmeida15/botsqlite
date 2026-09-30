# -*- coding: utf-8 -*-
"""Contrato dos scripts, criar_apoio com HTTP fake e manifest = registry."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python.scripts import criar_acesso_hub, criar_apoio, criar_infra_rede, registry  # noqa: E402
from tests.test_submenu import insert_option, mem_conn, seed_queue_menu  # noqa: E402

ENV = {
    "APOIO_API_URL": "http://example.test/api/v1/toa/criar_apoio?ambiente=PRODUCAO",
    "APOIO_STATUS_URL": "http://example.test/api/v1/toa",
    "APOIO_API_KEY": "sekret",
}
ANSWERS = {"atividade": "42", "login_apoio": "tec.apoio"}
ENV_INFRA = {
    "INFRA_REDE_API_URL": "http://example.test/api/v1/toa/criar_infra_rede?ambiente=PRODUCAO",
    "APOIO_STATUS_URL": "http://example.test/api/v1/toa",
    "APOIO_API_KEY": "sekret",
}
ANSWERS_INFRA = {
    "motivo": "Troca de Poste",
    "cidade": "Sao Paulo",
    "endereco": "Rua da Gloria, 54",
    "equipamento": "LIBAC",
    "rede": "HFC",
    "tecnico": "Z679436",
}


class NormalizeResultTest(unittest.TestCase):
    def test_acao_desconhecida_vira_stay(self):
        n = registry.normalize_result(
            {"ok": True, "message": "ok", "action": "explode", "detail": "http 200"}
        )
        self.assertEqual(n["action"], "stay")
        self.assertTrue(n["ok"])
        self.assertEqual(n["message"], "ok")

    def test_nao_dict_vira_fallback(self):
        n = registry.normalize_result("boom")
        self.assertFalse(n["ok"])
        self.assertEqual(n["action"], "ask_and_transfer")
        self.assertEqual(n["message"], registry.FALLBACK_MESSAGE)
        self.assertEqual(n["detail"], "bad_result")

    def test_detail_cortado(self):
        n = registry.normalize_result({"ok": False, "action": "stay", "detail": "x" * 900})
        self.assertEqual(len(n["detail"]), 800)


class RegistryRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_excecao_nao_manda_stack_no_resultado(self):
        async def boom(_ctx):
            raise RuntimeError("APIKEY=secret-nao-vazar")

        with patch.object(registry, "_load_module", return_value=SimpleNamespace(run=boom)):
            r = await registry.run("criar_apoio", {"answers": {}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["action"], "ask_and_transfer")
        self.assertEqual(r["message"], registry.FALLBACK_MESSAGE)
        self.assertEqual(r["detail"], "script_exception")
        blob = str(r)
        self.assertNotIn("secret-nao-vazar", blob)
        self.assertNotIn("APIKEY", blob)


class ManifestRegistryTest(unittest.TestCase):
    def test_manifest_bate_com_registry(self):
        file_data = registry.load_manifest_file()
        live = registry.manifest_payload()
        self.assertEqual(file_data, live)
        slugs = [s["slug"] for s in live["scripts"]]
        self.assertIn("criar_apoio", slugs)
        self.assertIn("criar_infra_rede", slugs)
        self.assertIn("criar_acesso_hub", slugs)


class RegistryFieldsTest(unittest.TestCase):
    def test_script_estatico_pergunta_o_fields(self):
        self.assertEqual(
            [f["key"] for f in registry.fields_for("criar_infra_rede", {})],
            ["motivo", "cidade", "endereco", "equipamento", "rede", "tecnico"],
        )

    def test_script_dinamico_usa_fields_for(self):
        self.assertEqual(
            [f["key"] for f in registry.fields_for("criar_acesso_hub", {})], ["hub"]
        )

    def test_fields_for_com_excecao_cai_no_fields_estatico(self):
        with patch.object(criar_acesso_hub, "fields_for", side_effect=RuntimeError("boom")):
            keys = [f["key"] for f in registry.fields_for("criar_acesso_hub", {})]
        self.assertEqual(keys, ["hub", "incidente", "qtd_tecnicos", "motivo"])

    def test_script_sem_hook_devolve_none(self):
        self.assertIsNone(
            registry.normalize_answer("criar_infra_rede", {"key": "motivo"}, "x", {})
        )

    def test_hook_normaliza_contrato(self):
        norm = registry.normalize_answer(
            "criar_acesso_hub", {"key": "qtd_tecnicos"}, "2", {}
        )
        self.assertEqual(norm["value"], "2")
        self.assertFalse(norm["retry"])
        self.assertEqual(norm["state"], {})
        ruim = registry.normalize_answer(
            "criar_acesso_hub", {"key": "qtd_tecnicos"}, "9", {}
        )
        self.assertIsNone(ruim["value"])
        self.assertTrue(ruim["retry"])

    def test_hook_com_excecao_vira_recusa(self):
        with patch.object(criar_acesso_hub, "normalize_answer", side_effect=RuntimeError("boom")):
            norm = registry.normalize_answer(
                "criar_acesso_hub", {"key": "hub"}, "HUBSAM", {}
            )
        self.assertIsNone(norm["value"])
        self.assertTrue(norm["retry"])

    def test_confirm_note(self):
        self.assertIn("recusa", registry.confirm_note("criar_acesso_hub"))
        self.assertEqual(registry.confirm_note("criar_infra_rede"), "")


class CriarApoioTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_200_sucesso_encerra(self):
        order = []
        captured = {}

        def persist(uid):
            order.append("persist")
            captured["uid"] = uid

        def fake_post(url, payload, headers, *a, **k):
            order.append("post")
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = headers
            return 200, {"status": "SUCESSO"}

        with patch.dict(os.environ, ENV, clear=False):
            r = await criar_apoio.run(
                {"answers": ANSWERS, "persist_job": persist},
                http_post=fake_post,
            )
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "reply_and_close")
        self.assertEqual(r["message"], criar_apoio.SUCCESS_MESSAGE)
        self.assertEqual(order[0], "persist")
        self.assertEqual(order[1], "post")
        self.assertEqual(captured["payload"], {"tarefa_id": 42, "tecnico": "tec.apoio"})
        self.assertEqual(captured["headers"]["x-api-key"], "sekret")
        self.assertEqual(captured["headers"]["x-modo"], "sync")
        self.assertEqual(captured["headers"]["x-uuid"], captured["uid"])
        self.assertNotIn("sekret", r["message"])
        self.assertNotIn("sekret", r["detail"])

    async def test_409_gera_novo_uuid_e_repete(self):
        uuids = []
        persisted = []

        def persist(uid):
            persisted.append(uid)

        def fake_post(url, payload, headers, *a, **k):
            uuids.append(headers.get("x-uuid"))
            if len(uuids) == 1:
                return 409, {"detail": "uuid ja utilizado"}
            return 200, {"status": "SUCESSO"}

        with patch.dict(os.environ, ENV, clear=False):
            r = await criar_apoio.run(
                {"answers": ANSWERS, "persist_job": persist},
                http_post=fake_post,
            )
        self.assertTrue(r["ok"])
        self.assertEqual(len(uuids), 2)
        self.assertNotEqual(uuids[0], uuids[1])
        self.assertEqual(persisted[0], uuids[0])
        self.assertEqual(persisted[1], uuids[1])

    async def test_http_500_transfere(self):
        def fake_post(url, payload, headers, *a, **k):
            return 500, {}

        with patch.dict(os.environ, ENV, clear=False):
            r = await criar_apoio.run({"answers": ANSWERS}, http_post=fake_post)
        self.assertFalse(r["ok"])
        self.assertEqual(r["action"], "ask_and_transfer")
        self.assertEqual(r["message"], criar_apoio.ERROR_MESSAGE)
        self.assertEqual(r["detail"], "http 500")

    async def test_http_500_body_no_detail(self):
        def fake_post(url, payload, headers, *a, **k):
            return 500, {"detail": "falha xyz"}

        with patch.dict(os.environ, ENV, clear=False):
            r = await criar_apoio.run({"answers": ANSWERS}, http_post=fake_post)
        self.assertFalse(r["ok"])
        self.assertEqual(r["action"], "ask_and_transfer")
        self.assertIn("http 500", r["detail"])
        self.assertIn("falha xyz", r["detail"])
        self.assertNotIn("sekret", r["detail"])
        self.assertNotIn("x-api-key", r["detail"])

    async def test_rede_transfere(self):
        def fake_post(url, payload, headers, *a, **k):
            return None, {}

        with patch.dict(os.environ, ENV, clear=False):
            r = await criar_apoio.run({"answers": ANSWERS}, http_post=fake_post)
        self.assertEqual(r["action"], "ask_and_transfer")
        self.assertEqual(r["detail"], "http_error")

    async def test_body_erro_transfere(self):
        def fake_post(url, payload, headers, *a, **k):
            return 200, {
                "status": "ERRO",
                "detail": "uuid ja utilizado",
                "response": "nao criado",
            }

        with patch.dict(os.environ, ENV, clear=False):
            r = await criar_apoio.run({"answers": ANSWERS}, http_post=fake_post)
        self.assertEqual(r["action"], "ask_and_transfer")
        self.assertIn("http 200", r["detail"])
        self.assertIn("ERRO", r["detail"])
        self.assertIn("uuid ja utilizado", r["detail"])
        self.assertIn("nao criado", r["detail"])
        self.assertNotIn("sekret", r["detail"])

    async def test_409_esgotado_leva_ultimo_body(self):
        bodies = [
            {"detail": "uuid 'aaa' ja utilizado"},
            {"detail": "uuid 'bbb' ja utilizado"},
            {"detail": "uuid 'ccc' ja utilizado"},
        ]

        def fake_post(url, payload, headers, *a, **k):
            return 409, bodies.pop(0)

        with patch.dict(os.environ, ENV, clear=False):
            r = await criar_apoio.run({"answers": ANSWERS}, http_post=fake_post)
        self.assertFalse(r["ok"])
        self.assertEqual(r["action"], "ask_and_transfer")
        self.assertIn("http 409", r["detail"])
        self.assertIn("uuid 'ccc' ja utilizado", r["detail"])
        self.assertNotIn("sekret", r["detail"])

    async def test_pendente_depois_get_sucesso(self):
        gets = []

        def fake_post(url, payload, headers, *a, **k):
            return 200, {"status": "PENDENTE"}

        def fake_get(url, headers, *a, **k):
            gets.append(headers.get("x-uuid"))
            return 200, {"status": "SUCESSO"}

        with patch.dict(os.environ, ENV, clear=False), patch(
            "python.scripts.criar_apoio.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            r = await criar_apoio.run(
                {"answers": ANSWERS},
                http_post=fake_post,
                http_get=fake_get,
            )
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "reply_and_close")
        self.assertEqual(len(gets), 1)
        sleep.assert_not_called()

    async def test_pendente_get_erro_transfere(self):
        def fake_post(url, payload, headers, *a, **k):
            return 200, {"status": "PENDENTE"}

        def fake_get(url, headers, *a, **k):
            return 200, {"status": "ERRO"}

        with patch.dict(os.environ, ENV, clear=False):
            r = await criar_apoio.run(
                {"answers": ANSWERS},
                http_post=fake_post,
                http_get=fake_get,
            )
        self.assertEqual(r["action"], "ask_and_transfer")
        self.assertEqual(r["message"], criar_apoio.ERROR_MESSAGE)
        self.assertIn("http 200", r["detail"])
        self.assertIn("ERRO", r["detail"])
        self.assertNotIn("sekret", r["detail"])

    async def test_resume_so_get_sem_post(self):
        posts = []

        def fake_post(url, payload, headers, *a, **k):
            posts.append(1)
            raise AssertionError("POST no resume")

        def fake_get(url, headers, *a, **k):
            self.assertEqual(headers.get("x-uuid"), "uid-salvo")
            return 200, {"status": "SUCESSO"}

        with patch.dict(os.environ, ENV, clear=False):
            r = await criar_apoio.run(
                {"answers": ANSWERS, "resume_uuid": "uid-salvo"},
                http_post=fake_post,
                http_get=fake_get,
            )
        self.assertTrue(r["ok"])
        self.assertEqual(posts, [])

    async def test_orcamento_estourado_sem_http(self):
        posts = []

        def fake_post(url, payload, headers, *a, **k):
            posts.append(1)
            return 200, {"status": "SUCESSO"}

        old = datetime.now(timezone.utc) - timedelta(seconds=181)
        with patch.dict(os.environ, ENV, clear=False):
            r = await criar_apoio.run(
                {"answers": ANSWERS, "started_at": old},
                http_post=fake_post,
            )
        self.assertEqual(r["action"], "ask_and_transfer")
        self.assertEqual(r["detail"], "timeout")
        self.assertEqual(posts, [])

    async def test_pendente_no_maximo_4_gets(self):
        gets = []

        def fake_post(url, payload, headers, *a, **k):
            return 200, {"status": "PENDENTE"}

        def fake_get(url, headers, *a, **k):
            gets.append(headers.get("x-uuid"))
            return 200, {"status": "PENDENTE"}

        with patch.dict(os.environ, ENV, clear=False), patch(
            "python.scripts.criar_apoio.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            r = await criar_apoio.run(
                {"answers": ANSWERS},
                http_post=fake_post,
                http_get=fake_get,
            )
        self.assertEqual(r["action"], "ask_and_transfer")
        self.assertEqual(r["message"], criar_apoio.ERROR_MESSAGE)
        self.assertEqual(r["detail"], "poll_exhausted")
        self.assertEqual(len(gets), 4)
        self.assertEqual(sleep.call_count, 3)
        sleep.assert_called_with(30.0)


class CriarInfraRedeTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_200_payload_completo(self):
        captured = {}

        def fake_post(url, payload, headers, *a, **k):
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = headers
            return 200, {"status": "SUCESSO"}

        with patch.dict(os.environ, {**ENV, **ENV_INFRA}, clear=False):
            r = await criar_infra_rede.run({"answers": ANSWERS_INFRA}, http_post=fake_post)
        self.assertTrue(r["ok"])
        self.assertEqual(r["action"], "reply_and_close")
        self.assertEqual(r["message"], criar_infra_rede.SUCCESS_MESSAGE)
        self.assertEqual(captured["url"], ENV_INFRA["INFRA_REDE_API_URL"])
        self.assertEqual(captured["payload"], ANSWERS_INFRA)
        self.assertEqual(captured["headers"]["x-api-key"], "sekret")
        self.assertNotIn("sekret", r["detail"])

    async def test_http_500_body_sem_chave(self):
        def fake_post(url, payload, headers, *a, **k):
            return 500, {"detail": "falha xyz"}

        with patch.dict(os.environ, {**ENV, **ENV_INFRA}, clear=False):
            r = await criar_infra_rede.run({"answers": ANSWERS_INFRA}, http_post=fake_post)
        self.assertFalse(r["ok"])
        self.assertEqual(r["action"], "ask_and_transfer")
        self.assertEqual(r["message"], criar_infra_rede.ERROR_MESSAGE)
        self.assertIn("http 500", r["detail"])
        self.assertIn("falha xyz", r["detail"])
        self.assertNotIn("sekret", r["detail"])

    async def test_resume_so_get_sem_post(self):
        posts = []

        def fake_post(url, payload, headers, *a, **k):
            posts.append(1)
            raise AssertionError("POST no resume")

        def fake_get(url, headers, *a, **k):
            self.assertEqual(headers.get("x-uuid"), "uid-infra")
            return 200, {"status": "SUCESSO"}

        with patch.dict(os.environ, {**ENV, **ENV_INFRA}, clear=False):
            r = await criar_infra_rede.run(
                {"answers": ANSWERS_INFRA, "resume_uuid": "uid-infra"},
                http_post=fake_post,
                http_get=fake_get,
            )
        self.assertTrue(r["ok"])
        self.assertEqual(posts, [])


class SchemaRunScriptTest(unittest.TestCase):
    def test_check_aceita_run_script(self):
        conn = mem_conn()
        seed_queue_menu(conn)
        oid = insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Apoio",
            classification="apoio",
            action="run_script",
            script_slug="criar_apoio",
        )
        self.assertGreater(oid, 0)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'menu_options'"
        ).fetchone()["sql"]
        self.assertIn("run_script", sql)
        chat_cols = {r["name"] for r in conn.execute("PRAGMA table_info(chats)").fetchall()}
        self.assertIn("script_slug", chat_cols)
        self.assertIn("script_field_index", chat_cols)
        self.assertIn("script_answers", chat_cols)
        self.assertIn("script_uuid", chat_cols)
        self.assertIn("script_started_at", chat_cols)


if __name__ == "__main__":
    unittest.main()
