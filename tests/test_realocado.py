# -*- coding: utf-8 -*-
"""Checagem TEMPORÁRIA do formulário "TAREFA REALOCADA" contra o robô de realocação.

Ver python/realocado.py e Engine._realocado_primeiro_contato/_realocado_segurar
(python/engine.py). Este arquivo some junto quando a feature for removida.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python import realocado  # noqa: E402
from python.engine import MEM  # noqa: E402
from python.routing import TZ_BRT  # noqa: E402
from tests.test_form_open import form_chat  # noqa: E402
from tests.test_form_route import RouteSock, engine_with, events, routes  # noqa: E402
from tests.test_submenu import mem_conn, seed_queue_menu  # noqa: E402

# Texto real de um formulário de produção (chat_id 17773835), confirmado direto
# no banco (`chat_messages.text`) durante o levantamento desta feature.
TEXTO_REALOCADO = (
    "FO - RJO - Procisa\n"
    "JULIO CEZAR CARDOSO DE OLIVEIRA\n\n"
    "Validar Ação:\n"
    "Não Concluir \n\n"
    "Atividade:\n"
    "Emergencial Fibra EMP\n"
    "REC-705279/2026\n"
    "INC000007076767\n"
    "TASK00006669092\n"
    "RJ / RIO_DE_JANEIRO\n"
    "Equipamento: CZS \n"
    "TAREFA REALOCADA\n"
    "N/A\n"
    "N/A\n\n"
    "Tarefa realocada \n"
    "https://clarosa.etadirect.com/mobility/#screen=activity_by_id"
    "&activityId=24740480&date=2026-11-09&resourceInternalId=13251"
)

TEXTO_NORMAL = (
    "FO - RJO - OESTE\n"
    "VAGNER LUIS DE OLIVEIRA VIEIRA\n\n"
    "Validar Ação:\n"
    "Não Concluir \n\n"
    "Atividade:\n"
    "Emergencial Fibra EMP\n"
    "RAL-707513/2026\n"
    "REDE EXTERNA IMPROCEDENTE\n"
    "N/A\n"
    "N/A\n\n"
    "Após análise CGR sistema já normalizado.\n"
    "https://clarosa.etadirect.com/mobility/#screen=activity_by_id"
    "&activityId=24752582&date=2026-11-09&resourceInternalId=2242"
)


class RealocadoParsingTest(unittest.TestCase):
    def test_is_realocado_detecta_a_classificacao(self):
        self.assertTrue(realocado.is_realocado([TEXTO_REALOCADO]))

    def test_is_realocado_ignora_outros_templates(self):
        self.assertFalse(realocado.is_realocado([TEXTO_NORMAL]))

    def test_extract_activity_id(self):
        self.assertEqual(realocado.extract_activity_id([TEXTO_REALOCADO]), 24740480)

    def test_extract_activity_id_sem_url(self):
        self.assertIsNone(realocado.extract_activity_id(["sem url nenhuma aqui"]))


class RealocadoEngineTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _eng(self, msgs=None, online=None):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        sock = RouteSock(online=online, msgs=msgs)
        return engine_with(conn, sock), conn, sock

    async def test_primeiro_contato_so_enfileira_nao_tenta_mover(self):
        """Classificado: nao tenta `move_chat` de cara -- so cria a rota em espera."""
        eng, conn, sock = self._eng(msgs={99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_REALOCADO, "time": 1}]})
        await eng.consider(form_chat())
        self.assertNotIn("move_chat", sock.names())
        rota = routes(conn)
        self.assertEqual(len(rota), 1)
        self.assertEqual(rota[0]["status"], "waiting")
        self.assertEqual(int(rota[0]["attempts"] or 0), 0)
        self.assertIn("form_realocado_aguardando", events(conn))

    async def test_sem_classificacao_mantem_entrega_imediata(self):
        """Regressao: formulario comum continua indo direto (sem a checagem)."""
        eng, conn, sock = self._eng(msgs={99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_NORMAL, "time": 1}]})
        await eng.consider(form_chat())
        self.assertIn("move_chat", sock.names())

    async def test_config_desligada_mantem_entrega_imediata(self):
        """`realocado_check_enabled=false`: feature inteira sai do caminho."""
        eng, conn, sock = self._eng(msgs={99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_REALOCADO, "time": 1}]})
        eng.cfg["realocado_check_enabled"] = "false"
        await eng.consider(form_chat())
        self.assertIn("move_chat", sock.names())

    async def test_tick_segura_dentro_dos_5_min(self):
        """Ainda dentro do atraso: o tick nao tenta entregar nem conta tentativa."""
        eng, conn, sock = self._eng(msgs={99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_REALOCADO, "time": 1}]})
        await eng.consider(form_chat())
        await eng.tick_routes()
        self.assertNotIn("move_chat", sock.names())
        rota = routes(conn)[0]
        self.assertEqual(rota["status"], "waiting")
        self.assertEqual(int(rota["attempts"] or 0), 0)
        self.assertIn("aguardando checagem de realocacao automatica", str(rota["detail"] or ""))

    def _torna_elegivel_para_checagem(self, conn):
        """Simula que os 5 min ja passaram (created_at antigo o suficiente)."""
        conn.execute("UPDATE pending_routes SET created_at = datetime('now', '-301 seconds')")

    async def test_tick_resolvido_pelo_robo_encerra_sem_mover(self):
        eng, conn, sock = self._eng(msgs={99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_REALOCADO, "time": 1}]})
        await eng.consider(form_chat())
        self._torna_elegivel_para_checagem(conn)
        with mock.patch.object(db, "realocado_resolvido", return_value=True) as fake:
            await eng.tick_routes()
        fake.assert_called_once()
        self.assertEqual(fake.call_args.args[1], 24740480)
        self.assertNotIn("move_chat", sock.names())
        rota = routes(conn)[0]
        self.assertEqual(rota["status"], "done")
        self.assertIn("resolvido pela automacao de realocacao", str(rota["detail"] or ""))
        self.assertIn("form_realocado_resolvido", events(conn))

    async def test_tick_aplica_margem_de_tolerancia_antes_do_inicio(self):
        """Formulario duplicado: robo pode ter resolvido poucos segundos ANTES
        deste formulario especifico (o robo sempre roda primeiro) -- o limite
        passado para a consulta precisa recuar a margem de tolerancia, senao
        esse sucesso real seria descartado por engano."""
        eng, conn, sock = self._eng(msgs={99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_REALOCADO, "time": 1}]})
        await eng.consider(form_chat())
        self._torna_elegivel_para_checagem(conn)
        rota_antes = routes(conn)[0]
        inicio_brt = datetime.fromisoformat(str(rota_antes["created_at"]).replace(" ", "T")).replace(
            tzinfo=None
        ) - timedelta(hours=3)
        with mock.patch.object(db, "realocado_resolvido", return_value=False) as fake:
            await eng.tick_routes()
        fake.assert_called_once()
        limite_usado = fake.call_args.args[2]
        self.assertAlmostEqual(
            (inicio_brt - limite_usado).total_seconds(), 300.0, delta=2.0
        )

    async def test_tick_nao_resolvido_segue_fluxo_normal(self):
        eng, conn, sock = self._eng(msgs={99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_REALOCADO, "time": 1}]})
        await eng.consider(form_chat())
        self._torna_elegivel_para_checagem(conn)
        with mock.patch.object(db, "realocado_resolvido", return_value=False):
            await eng.tick_routes()
        self.assertIn("move_chat", sock.names())
        rota = routes(conn)[0]
        self.assertEqual(rota["status"], "done")
        self.assertNotIn("resolvido pela automacao de realocacao", str(rota["detail"] or ""))

    async def test_falha_na_consulta_nao_trava_a_fila(self):
        """Schema `robo` fora do ar: nao bloqueia, segue o fluxo normal."""
        eng, conn, sock = self._eng(msgs={99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_REALOCADO, "time": 1}]})
        await eng.consider(form_chat())
        self._torna_elegivel_para_checagem(conn)
        with mock.patch.object(db, "realocado_resolvido", side_effect=RuntimeError("schema indisponivel")):
            await eng.tick_routes()
        self.assertIn("move_chat", sock.names())
        rota = routes(conn)[0]
        self.assertEqual(rota["status"], "done")

    async def test_sem_activity_id_libera_sem_consultar_o_robo(self):
        texto_sem_url = "TAREFA REALOCADA\nsem link nenhum aqui"
        eng, conn, sock = self._eng(msgs={99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": texto_sem_url, "time": 1}]})
        await eng.consider(form_chat())
        self._torna_elegivel_para_checagem(conn)
        with mock.patch.object(db, "realocado_resolvido") as fake:
            await eng.tick_routes()
        fake.assert_not_called()
        self.assertIn("move_chat", sock.names())


if __name__ == "__main__":
    unittest.main()
