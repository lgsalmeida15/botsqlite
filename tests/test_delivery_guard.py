# -*- coding: utf-8 -*-
"""FASE A - instrumentacao da entrega e travas no banco.

Cada teste corresponde a uma falha comprovada no banco real (data/helptoa.sqlite,
13 dias de operacao): timeout de ACK invisivel, dois `transferred` no mesmo chat
no mesmo segundo e duas rotas ativas para o mesmo formulario.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python import engine as engine_mod  # noqa: E402
from python.engine import MEM  # noqa: E402
from tests.test_form_route import RouteSock, engine_with  # noqa: E402
from tests.test_submenu import insert_option, mem_conn, seed_queue_menu  # noqa: E402

ANA = "u:ana@claro.com"
BIA = "u:bia@claro.com"
TEC = "u:tec@claro.com"


def lembra(cid: int, tec: str = TEC) -> None:
    """Repovoa a memoria do chat (o que o TOA entrega em get_active_chats)."""
    engine_mod.remember(
        {
            "chat_id": cid,
            "started_addr": tec,
            "helpdesk_address": "h:q1",
            "participants": {tec: {"address": tec, "name": "Tec", "present": True}},
        }
    )


def ops(conn) -> list[tuple]:
    return [
        (r["op"], r["result"], r["target"])
        for r in conn.execute("SELECT * FROM delivery_attempts ORDER BY id").fetchall()
    ]


def detalhe_evento(conn, reason: str) -> str:
    row = conn.execute(
        "SELECT detail FROM bot_events WHERE reason = ? ORDER BY id DESC LIMIT 1", (reason,)
    ).fetchone()
    return str((row or {}).get("detail") or "")


def prepara_chat_classificado(conn, cid: int = 10, com_rota: bool = True, moved: bool = False) -> None:
    seed_queue_menu(conn, with_team=True)
    insert_option(
        conn, menu_id=1, sort_order=1, label="Transferir", classification="tec", action="transfer_only"
    )
    db.upsert_chat(conn, cid, queue_address="h:q1", status="classified", last_prompt_at="2000-01-01 00:00:00")
    if moved:
        conn.execute("UPDATE chats SET moved_at = datetime('now') WHERE chat_id = ?", (cid,))
    if com_rota:
        conn.execute(
            "INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, detail, next_try_at)"
            " VALUES (?, ?, ?, ?, ?, datetime(?, ?))",
            ("chat", cid, "h:q1", 1, "sem analista", "now", "-1 seconds"),
        )
    lembra(cid)


class InstrumentacaoTest(unittest.IsolatedAsyncioTestCase):
    """A6: toda operacao externa de entrega fica registrada (mede o timeout)."""

    def tearDown(self):
        MEM.clear()

    async def test_entrega_grava_move_invite_e_leave(self):
        conn = mem_conn()
        prepara_chat_classificado(conn)
        sock = RouteSock(online=["ana"])
        eng = engine_with(conn, sock)
        await eng.tick_routes()
        self.assertEqual([o[0] for o in ops(conn)], ["move_chat", "invite_to_chat", "leave_chat"])
        self.assertEqual({o[1] for o in ops(conn)}, {"ok"})
        self.assertEqual(ops(conn)[1][2], ANA)

    async def test_timeout_do_toa_fica_medido(self):
        """Timeout nao pode ser invisivel: fica gravado (a decisao muda na FASE B)."""
        conn = mem_conn()
        prepara_chat_classificado(conn)

        class TimeoutSock(RouteSock):
            async def command(self, name, payload=None, **kwargs):
                if name in ("move_chat", "invite_to_chat"):
                    self.cmds.append((name, payload or {}))
                    return {"ok": True, "timeout": True, "r": "123"}
                return await super().command(name, payload, **kwargs)

        sock = TimeoutSock(online=["ana"])
        eng = engine_with(conn, sock)
        await eng.tick_routes()
        self.assertEqual([o[1] for o in ops(conn) if o[0] != "leave_chat"], ["timeout", "timeout"])
        self.assertIn("move=timeout", detalhe_evento(conn, "transferred"))
        self.assertIn("invite=timeout", detalhe_evento(conn, "transferred"))


class TravaNoBancoTest(unittest.TestCase):
    """A2: a trava de dupla entrega existe no banco, nao so na memoria."""

    def test_indice_unico_ativo_bloqueia_segunda_rota(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        conn.execute(
            "INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status)"
            " VALUES ('chat', 77, 'h:q1', 1, 'waiting')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status)"
                " VALUES ('chat', 77, 'h:q1', 1, 'waiting')"
            )

    def test_migracao_saneia_duplicatas_antigas(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        conn.execute("DROP INDEX IF EXISTS pending_routes_active")
        for _ in range(2):
            conn.execute(
                "INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status)"
                " VALUES ('form', 88, 'h:q1', 1, 'waiting')"
            )
        db.ensure_schema(conn)
        ativas = conn.execute(
            "SELECT count(*) AS n FROM pending_routes WHERE status NOT IN ('done','failed','cancelled')"
        ).fetchone()["n"]
        self.assertEqual(ativas, 1)

    def test_claim_devolve_posse_quando_outra_execucao_esta_entregando(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        primeiro = db.claim_delivery(conn, "form", 99, "h:q1", 1, "token-a")
        segundo = db.claim_delivery(conn, "form", 99, "h:q1", 1, "token-b")
        self.assertIsNotNone(primeiro)
        self.assertIsNone(segundo)
        db.release_delivery(conn, int(primeiro["id"]), "token-a")
        self.assertIsNotNone(db.claim_delivery(conn, "form", 99, "h:q1", 1, "token-b"))
