# -*- coding: utf-8 -*-
"""Analista offline: o caso volta para a fila (resgate) e nao volta para ele."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import MEM, Engine  # noqa: E402
from tests.test_form_open import form_chat  # noqa: E402
from tests.test_form_route import ANA, RouteSock, events, routes  # noqa: E402
from tests.test_submenu import insert_option, mem_conn, seed_queue_menu  # noqa: E402

TEC = "u:tec@claro.com"


async def _sem_join(self, cid: int) -> bool:
    """Nos testes nao ha socket real: o alvo aqui e a devolucao do caso."""
    return True


def chat_row(conn, cid: int = 10) -> dict:
    return conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone()


def engine_with(conn, sock, **cfg) -> Engine:
    eng = Engine(conn, sock)
    eng.cfg["offline_requeue_enabled"] = "true"
    eng.cfg["offline_requeue_sec"] = "120"
    for k, v in cfg.items():
        eng.cfg[k] = v
    return eng


def _presenca_offline(conn, addr: str, segundos: int = 200) -> None:
    db.load_upsert(conn, addr, name="Ana", team_id=1)
    conn.execute(
        "UPDATE analyst_load SET online = 0, online_ok = 1,"
        " offline_since = datetime('now', '-' || ? || ' seconds') WHERE address = ?",
        (str(int(segundos)), addr),
    )


def abre_caso_chat(conn, cid: int = 10, addr: str = ANA, segundos: int = 200) -> None:
    """Chat entregue a um analista (ledger aberto) + presenca offline."""
    db.upsert_chat(
        conn, cid, queue_address="h:q1", status="closed",
        moved_at=datetime.now(timezone.utc),
    )
    db.assign_open(conn, "chat", cid, "h:q1", 1, addr, "a:" + TEC, TEC, "Tec")
    _presenca_offline(conn, addr, segundos)


def abre_caso_form(conn, fid: int = 99, addr: str = ANA, segundos: int = 200) -> None:
    db.assign_open(conn, "form", fid, "h:q1", 1, addr, "a:" + TEC, TEC, "Tec")
    _presenca_offline(conn, addr, segundos)


class ResgateOfflineTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _eng(self, **cfg):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        insert_option(
            conn, menu_id=1, sort_order=1, label="Apoio", classification="apoio",
            action="transfer_only",
        )
        sock = RouteSock(online=[ANA])
        return engine_with(conn, sock, **cfg), conn, sock

    async def test_chat_offline_volta_para_a_fila(self):
        eng, conn, sock = self._eng()
        abre_caso_chat(conn)
        with mock.patch.object(Engine, "_join_until_present", _sem_join):
            devolvidos = await eng._reclaim_offline()
        self.assertEqual(devolvidos, 1)
        caso = conn.execute("SELECT * FROM route_assignments WHERE chat_id = 10").fetchone()
        self.assertIsNotNone(caso["closed_at"], "o caso sai do ledger do offline")
        self.assertEqual(caso["close_reason"], "analista_offline")
        self.assertIn("case_reclaimed", events(conn))
        self.assertIn("case_closed", events(conn))
        self.assertIn("Operador ficou offline", " ".join(sock.sent_texts()))
        linha = chat_row(conn, 10)
        self.assertEqual(linha["status"], "classified")
        self.assertIsNone(linha["moved_at"], "o proximo move e liberado")
        espera = routes(conn, "waiting")
        self.assertEqual(len(espera), 1)
        self.assertEqual(espera[0]["kind"], "chat")
        self.assertEqual(espera[0]["exclude_assignee"], ANA)
        self.assertIn("resgatado", espera[0]["detail"])

    async def test_formulario_offline_volta_sem_mensagem(self):
        eng, conn, sock = self._eng()
        abre_caso_form(conn)
        devolvidos = await eng._reclaim_offline()
        self.assertEqual(devolvidos, 1)
        self.assertIn("case_reclaimed", events(conn))
        self.assertEqual(sock.sent_texts(), [], "formulario nao tem conversa")
        espera = routes(conn, "waiting")
        self.assertEqual(len(espera), 1)
        self.assertEqual(espera[0]["kind"], "form")
        self.assertEqual(espera[0]["exclude_assignee"], ANA)
        self.assertEqual(espera[0]["case_key"], "a:" + TEC)

    async def test_offline_recente_nao_devolve(self):
        eng, conn, _sock = self._eng()
        abre_caso_chat(conn, segundos=30)  # < offline_requeue_sec (120)
        self.assertEqual(await eng._reclaim_offline(), 0)
        caso = conn.execute("SELECT * FROM route_assignments WHERE chat_id = 10").fetchone()
        self.assertIsNone(caso["closed_at"])
        self.assertNotIn("case_reclaimed", events(conn))
        self.assertEqual(routes(conn, "waiting"), [])

    async def test_leitura_de_online_ruim_nao_devolve(self):
        eng, conn, _sock = self._eng()
        abre_caso_chat(conn)
        # A leitura do `get_online_users` falhou: nao da para afirmar ausencia.
        conn.execute("UPDATE analyst_load SET online_ok = 0")
        self.assertEqual(await eng._reclaim_offline(), 0)
        self.assertNotIn("case_reclaimed", events(conn))

    async def test_desligado_ou_zero_no_time_nao_devolve(self):
        eng, conn, _sock = self._eng(offline_requeue_enabled="false")
        abre_caso_chat(conn)
        self.assertEqual(await eng._reclaim_offline(), 0)
        eng.cfg["offline_requeue_enabled"] = "true"
        conn.execute("INSERT INTO team_routing (team_id, offline_requeue_sec) VALUES (1, 0)")
        self.assertEqual(await eng._reclaim_offline(), 0)
        self.assertNotIn("case_reclaimed", events(conn))

    async def test_limite_de_cinco_por_tick(self):
        eng, conn, _sock = self._eng()
        for i in range(7):
            cid = 100 + i
            db.upsert_chat(conn, cid, queue_address="h:q1", status="closed")
            db.assign_open(conn, "chat", cid, "h:q1", 1, ANA, "a:%s%d" % (TEC, i), TEC, "Tec")
        _presenca_offline(conn, ANA)
        with mock.patch.object(Engine, "_join_until_present", _sem_join):
            self.assertEqual(await eng._reclaim_offline(), 5)
        self.assertEqual(len(routes(conn, "waiting")), 5)

    async def test_forca_do_painel_ignora_limite_e_presenca(self):
        eng, conn, _sock = self._eng()
        for i in range(3):
            cid = 200 + i
            db.upsert_chat(conn, cid, queue_address="h:q1", status="closed")
            db.assign_open(conn, "chat", cid, "h:q1", 1, ANA, "a:%s%d" % (TEC, i), TEC, "Tec")
        # Analista ONLINE: o botao do painel ainda devolve (decisao humana).
        db.load_upsert(conn, ANA, name="Ana", team_id=1, online=1, online_ok=1)
        with mock.patch.object(Engine, "_join_until_present", _sem_join):
            self.assertEqual(await eng._reclaim_offline(force_address=ANA), 3)
        self.assertEqual(len(routes(conn, "waiting")), 3)

    async def test_exclude_assignee_barra_o_mesmo_analista(self):
        eng, conn, _sock = self._eng()
        # Mesmo com `require_online=false`, o excluido nao pode receber de volta.
        eng.cfg["require_online"] = "false"
        conn.execute("INSERT INTO team_routing (team_id, require_online) VALUES (1, 0)")
        picked, detalhe = await eng.pick_assignee(
            "h:q1", kind="chat", retry_online=False, exclude_address=ANA
        )
        self.assertIsNone(picked)
        self.assertIn("excluido", detalhe)

    async def test_afinidade_nao_puxa_o_analista_offline(self):
        eng, conn, _sock = self._eng()
        db.assign_open(conn, "form", 99, "h:q1", 1, ANA, "a:" + TEC, TEC, "Tec")
        picked, detalhe = await eng.pick_assignee(
            "h:q1", kind="chat", retry_online=False, case_key="a:" + TEC, exclude_address=ANA
        )
        self.assertIsNone(picked, "so ha Ana no time e ela esta excluida")
        self.assertIn("excluido", detalhe)

    def test_presence_carimba_e_limpa(self):
        conn = mem_conn()
        db.load_upsert(conn, ANA, name="Ana", team_id=1)
        db.set_presence(conn, ANA, online=False, online_ok=True)
        linha = conn.execute("SELECT * FROM analyst_load WHERE address = ?", (ANA,)).fetchone()
        self.assertIsNotNone(linha["offline_since"])
        self.assertEqual(int(linha["online_ok"]), 1)
        primeiro = linha["offline_since"]
        # Nova leitura offline: o carimbo NAO anda (conta desde a primeira).
        db.set_presence(conn, ANA, online=False, online_ok=True)
        linha = conn.execute("SELECT * FROM analyst_load WHERE address = ?", (ANA,)).fetchone()
        self.assertEqual(linha["offline_since"], primeiro)
        # Voltou: limpa o carimbo.
        db.set_presence(conn, ANA, online=True, online_ok=True)
        linha = conn.execute("SELECT * FROM analyst_load WHERE address = ?", (ANA,)).fetchone()
        self.assertIsNone(linha["offline_since"])
        # Leitura ruim nao cria carimbo nem mente sobre presenca.
        db.set_presence(conn, ANA, online=False, online_ok=False)
        linha = conn.execute("SELECT * FROM analyst_load WHERE address = ?", (ANA,)).fetchone()
        self.assertIsNone(linha["offline_since"])
        self.assertEqual(int(linha["online_ok"]), 0)

    def test_schema_tem_as_colunas_novas(self):
        conn = mem_conn()
        self.assertIn("offline_since", db._table_columns(conn, "analyst_load"))
        self.assertIn("online_ok", db._table_columns(conn, "analyst_load"))
        self.assertIn("exclude_assignee", db._table_columns(conn, "pending_routes"))
        self.assertIn("offline_requeue_sec", db._table_columns(conn, "team_routing"))
        self.assertIn("payload", db._table_columns(conn, "bot_jobs"))
        self.assertIn("requeue_notice", db.templates(conn))
        self.assertEqual(db.settings(conn).get("offline_requeue_sec"), "120")


if __name__ == "__main__":
    unittest.main()

