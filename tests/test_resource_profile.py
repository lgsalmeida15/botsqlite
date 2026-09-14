# -*- coding: utf-8 -*-
"""Lookup de perfil TOA por search (Gestor)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import resource_profile  # noqa: E402


def _member(login: str, tipo: str, *, grupo: bool = False) -> dict:
    return {
        "static": {"pname": login or "bucket", "is_group": grupo},
        "dynamic": [
            {"value": None if grupo else login, "title": "Efetuar Login", "type": "field"},
            {"value": "ativo", "title": "Status", "type": "field"},
            {"value": None if grupo else tipo, "title": "Tipo de Usuário", "type": "field"},
        ],
    }


class ExtractRowsTest(unittest.TestCase):
    def test_members_aninhados_em_data(self):
        payload = {
            "data": {
                "filters": {"organizations": [{"id": 1}]},
                "members": [
                    _member("", "", grupo=True),
                    _member("Z469299", "Gestor RE3"),
                    _member("N0236128", "Gestor RE"),
                ],
            }
        }
        rows = resource_profile.extract_rows(payload)
        by_login = {r["login"]: r["tipo"] for r in rows}
        self.assertNotIn("", by_login)
        self.assertEqual(by_login["Z469299"], "Gestor RE3")
        self.assertEqual(by_login["N0236128"], "Gestor RE")

    def test_is_gestor(self):
        self.assertTrue(resource_profile.is_gestor("Gestor RE3"))
        self.assertTrue(resource_profile.is_gestor("gestor re"))
        self.assertFalse(resource_profile.is_gestor("Tecnico"))
        self.assertFalse(resource_profile.is_gestor("Analista COP REDE"))


class LookupSearchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        resource_profile.reset_cache()

    async def test_gestor_habilitado(self):
        calls = []

        async def fetch(page, login):
            calls.append(login)
            return {"ok": True, "rows": [{"login": "Z469299", "tipo": "Gestor RE3"}]}

        r = await resource_profile.lookup(None, "Z469299", fetch_page=fetch)
        self.assertTrue(r["allowed"])
        self.assertEqual(r["perfil"], "Gestor RE3")
        self.assertEqual(r["reason"], "ok")
        self.assertEqual(calls, ["Z469299"])

    async def test_login_casefold_e_cache(self):
        calls = []

        async def fetch(page, login):
            calls.append(login)
            return {"ok": True, "rows": [{"login": "Z469299", "tipo": "Gestor RE3"}]}

        r = await resource_profile.lookup(None, "z469299", fetch_page=fetch)
        self.assertTrue(r["allowed"])
        r2 = await resource_profile.lookup(None, "Z469299", fetch_page=fetch)
        self.assertTrue(r2["allowed"])
        self.assertEqual(calls, ["z469299"])

    async def test_analista_nao_gestor(self):
        async def fetch(page, login):
            return {"ok": True, "rows": [{"login": "F104751", "tipo": "Analista COP REDE"}]}

        r = await resource_profile.lookup(None, "f104751", fetch_page=fetch)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason"], "not_gestor")
        self.assertEqual(r["perfil"], "Analista COP REDE")

    async def test_not_found(self):
        async def fetch(page, login):
            return {"ok": True, "rows": []}

        r = await resource_profile.lookup(None, "Z000", fetch_page=fetch)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason"], "not_found")

    async def test_error_no_e_sem_csrf(self):
        async def err(page, login):
            return {"ok": False, "error": "errorNo", "rows": []}

        async def nocsrf(page, login):
            return {"ok": False, "error": "no_csrf", "rows": []}

        r = await resource_profile.lookup(None, "Z469299", fetch_page=err)
        self.assertEqual(r["reason"], "api_error")
        resource_profile.reset_cache()
        r2 = await resource_profile.lookup(None, "Z469299", fetch_page=nocsrf)
        self.assertEqual(r2["reason"], "api_error")

    async def test_timeout_e_sem_login(self):
        async def fetch(page, login):
            return {"ok": False, "error": "timeout", "rows": []}

        r = await resource_profile.lookup(None, "Z469299", fetch_page=fetch)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason"], "api_timeout")
        empty = await resource_profile.lookup(None, "", fetch_page=fetch)
        self.assertEqual(empty["reason"], "no_login")

    def test_deny_detail(self):
        d = resource_profile.deny_detail(
            "criar_apoio",
            "F104751",
            {"perfil": "Analista COP REDE", "reason": "not_gestor"},
        )
        self.assertIn("slug=criar_apoio", d)
        self.assertIn("login=F104751", d)
        self.assertIn("perfil=Analista COP REDE", d)


if __name__ == "__main__":
    unittest.main()
