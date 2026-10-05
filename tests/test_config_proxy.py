# -*- coding: utf-8 -*-
"""Proxy de ambiente: API interna nao pode passar pelo proxy (evita 503)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import proxy_bypass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import config  # noqa: E402

WEBHOOK = "https://default55247.5f.environment.api.powerplatform.com:443/powerautomate/x"
ENV = {
    "APOIO_API_URL": "http://10.70.107.15:8100/api/v1/toa/criar_apoio?ambiente=PRODUCAO",
    "APOIO_STATUS_URL": "http://10.70.107.15:8100/api/v1/toa",
    "INFRA_REDE_API_URL": "http://10.70.107.15:8100/api/v1/toa/criar_infra_rede?ambiente=PRODUCAO",
    "HTTP_PROXY": "http://127.0.0.1:8088",
    "HTTPS_PROXY": "http://127.0.0.1:8088",
    "NO_PROXY": "localhost,127.0.0.1,::1",
}


class BypassProxyTest(unittest.TestCase):
    def test_hosts_internos_vem_das_urls_do_env(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(config.internal_api_hosts(), ["10.70.107.15"])

    def test_is_internal_api_reconhece_api_e_ignora_webhook(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertTrue(config.is_internal_api(ENV["APOIO_STATUS_URL"] + "?tarefa_id=1"))
            self.assertFalse(config.is_internal_api(WEBHOOK))

    def test_bypass_acrescenta_host_sem_remover_os_atuais(self):
        with patch.dict(os.environ, ENV, clear=True):
            self.assertEqual(config.bypass_proxy_for_internal_apis(), ["10.70.107.15"])
            for var in ("NO_PROXY", "no_proxy"):
                valor = os.environ[var]
                self.assertIn("10.70.107.15", valor)
                self.assertIn("localhost", valor)
                self.assertIn("127.0.0.1", valor)

    def test_urllib_ignora_proxy_para_api_interna_e_mantem_no_webhook(self):
        with patch.dict(os.environ, ENV, clear=True):
            config.bypass_proxy_for_internal_apis()
            self.assertTrue(proxy_bypass("10.70.107.15"))
            self.assertFalse(proxy_bypass("default55247.5f.environment.api.powerplatform.com"))

    def test_sem_urls_internas_nao_mexe_no_no_proxy(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:8088"}, clear=True):
            self.assertEqual(config.bypass_proxy_for_internal_apis(), [])
            self.assertNotIn("NO_PROXY", os.environ)

    def test_load_env_aplica_bypass_do_arquivo(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "APOIO_STATUS_URL=http://10.70.107.15:8100/api/v1/toa\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:8088", "NO_PROXY": "localhost"}, clear=True):
                with patch.object(config, "ENV_PATH", env_file):
                    config.load_env()
                self.assertEqual(os.environ["NO_PROXY"], "localhost,10.70.107.15")
                self.assertEqual(os.environ["no_proxy"], "localhost,10.70.107.15")
                self.assertTrue(proxy_bypass("10.70.107.15"))


if __name__ == "__main__":
    unittest.main()
