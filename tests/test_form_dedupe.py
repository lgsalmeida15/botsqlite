# -*- coding: utf-8 -*-
"""Formulario duplicado (mesmo activityId do TOA): regra PERMANENTE do motor de
fila -- ver `Engine._descarta_form_duplicado` e `db.form_duplicate_of`
(python/engine.py, python/db.py). Ao contrario de `tests/test_realocado.py`,
este arquivo NAO e removido junto com a checagem do robo.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python.engine import MEM  # noqa: E402
from tests.test_form_open import form_chat  # noqa: E402
from tests.test_form_route import RouteSock, engine_with, events, routes  # noqa: E402
from tests.test_submenu import mem_conn, seed_queue_menu  # noqa: E402

URL_A = "https://clarosa.etadirect.com/mobility/#screen=activity_by_id&activityId=24740480&date=2026-11-09&resourceInternalId=13251"
URL_B = "https://clarosa.etadirect.com/mobility/#screen=activity_by_id&activityId=24752582&date=2026-11-09&resourceInternalId=2242"

# De proposito SEM a classificacao "TAREFA REALOCADA": o descarte de
# duplicado e permanente e independe da checagem (temporaria) do robo.
TEXTO_A = f"FO - RJO - Procisa\nValidar Ação:\nNão Concluir\n\nREDE EXTERNA IMPROCEDENTE\n{URL_A}"
# Conteudo DIFERENTE do TEXTO_A (outra classificacao) de proposito: o descarte
# e por activityId, nao por o texto bater igual.
TEXTO_A_REENVIO = f"FO - RJO - Procisa\nValidar Ação:\nConcluir\n\nServico finalizado\n{URL_A}"
TEXTO_B = f"FO - RJO - OESTE\nValidar Ação:\nNão Concluir\n\nCAIXA DE EMENDA DANIFICADA\n{URL_B}"


class FormDedupeEngineTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _eng(self, msgs):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        sock = RouteSock(msgs=msgs)
        return engine_with(conn, sock), conn, sock

    async def test_segundo_formulario_mesmo_activity_e_descartado(self):
        eng, conn, sock = self._eng(msgs={
            99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_A, "time": 1}],
            100: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_A_REENVIO, "time": 1}],
        })
        await eng.consider(form_chat())
        self.assertIn("move_chat", sock.names())
        sock.cmds.clear()
        await eng.consider(form_chat(chat_id=100))
        self.assertNotIn("move_chat", sock.names())
        rota = [r for r in routes(conn) if r["chat_id"] == 100][0]
        self.assertEqual(rota["status"], "done")
        self.assertIn("activityId=24740480", str(rota["detail"] or ""))
        self.assertIn("chat 99", str(rota["detail"] or ""))
        self.assertIn("form_duplicado_descartado", events(conn))

    async def test_activity_diferente_nao_e_descartado(self):
        eng, conn, sock = self._eng(msgs={
            99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_A, "time": 1}],
            100: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_B, "time": 1}],
        })
        await eng.consider(form_chat())
        sock.cmds.clear()
        await eng.consider(form_chat(chat_id=100))
        self.assertIn("move_chat", sock.names())

    async def test_fora_da_janela_nao_e_descartado(self):
        eng, conn, sock = self._eng(msgs={
            99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_A, "time": 1}],
            100: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_A_REENVIO, "time": 1}],
        })
        await eng.consider(form_chat())
        conn.execute("UPDATE chat_messages SET at = datetime('now', '-7 hours') WHERE chat_id = 99")
        sock.cmds.clear()
        await eng.consider(form_chat(chat_id=100))
        self.assertIn("move_chat", sock.names())

    async def test_config_desligada_mantem_entrega_imediata(self):
        eng, conn, sock = self._eng(msgs={
            99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_A, "time": 1}],
            100: [{"author": "CREATEDBYMESSAGESCENARIO", "text": TEXTO_A_REENVIO, "time": 1}],
        })
        eng.cfg["form_dedupe_enabled"] = "false"
        await eng.consider(form_chat())
        sock.cmds.clear()
        await eng.consider(form_chat(chat_id=100))
        self.assertIn("move_chat", sock.names())

    async def test_sem_url_nao_quebra_fluxo_normal(self):
        eng, conn, sock = self._eng(msgs={
            99: [{"author": "CREATEDBYMESSAGESCENARIO", "text": "formulario sem link nenhum aqui", "time": 1}],
        })
        await eng.consider(form_chat())
        self.assertIn("move_chat", sock.names())


if __name__ == "__main__":
    unittest.main()
