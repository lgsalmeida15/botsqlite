# -*- coding: utf-8 -*-
"""Perguntas antes de transferir (ask_and_transfer): wizard, confirmacao, fila e restart."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import MEM, Engine  # noqa: E402
from tests.test_form_route import ANA, RouteSock, events, routes  # noqa: E402
from tests.test_submenu import awaiting, insert_option, mem_conn, seed_queue_menu  # noqa: E402

PERGUNTAS = [
    ("Qual o ID da atividade?", "int", ""),
    ("Qual a rede?", "choice", "HFC|GPON"),
    ("Observacao", "text", ""),
]


def seed_ask_leaf(conn, reply_text="Vou precisar de algumas informacoes.", perguntas=None) -> int:
    opt = insert_option(
        conn,
        menu_id=1,
        sort_order=1,
        label="Apoio",
        classification="apoio",
        action="ask_and_transfer",
        reply_text=reply_text,
    )
    for i, (prompt, kind, choices) in enumerate(perguntas or [], start=1):
        conn.execute(
            """INSERT INTO menu_option_questions (option_id, sort_order, prompt, kind, choices)
               VALUES (?, ?, ?, ?, ?)""",
            (opt, i, prompt, kind, choices),
        )
    return opt


def chat_row(conn, cid: int = 10) -> dict:
    return conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone()


def engine_cfg(conn, sock, attempts="5", interval="0") -> Engine:
    eng = Engine(conn, sock)
    eng.cfg["transfer_attempts"] = attempts
    eng.cfg["transfer_retry_interval_sec"] = interval
    return eng


class InviteFailSock(RouteSock):
    """TOA recusando o convite: o caso real do analista que nao e operador da fila.

    O `move_chat` passa e o `invite_to_chat` volta com erro (`{e: 1}` no TOA real, que o
    `toa.command` transforma em RuntimeError).
    """

    def __init__(self, online=None, chats=None, falhas: int = 1):
        super().__init__(online=online, chats=chats)
        self.falhas = int(falhas)

    async def command(self, name, payload=None, **kwargs):
        if name == "invite_to_chat" and self.falhas > 0:
            self.falhas -= 1
            self.cmds.append((name, payload or {}))
            raise RuntimeError("1: erro do TOA sem mensagem")
        return await super().command(name, payload, **kwargs)


class AskFlowTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _eng(self, online=None, perguntas=None):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        seed_ask_leaf(conn, perguntas=PERGUNTAS if perguntas is None else perguntas)
        sock = RouteSock(online=online)
        return engine_cfg(conn, sock), conn, sock

    async def _responde_tudo(self, eng, conn, primeiro="123"):
        await eng.handle_reply(10, chat_row(conn), primeiro)
        await eng.handle_reply(10, chat_row(conn), "1")
        await eng.handle_reply(10, chat_row(conn), "ok")

    async def test_fluxo_completo_confirma_e_transfere(self):
        eng, conn, sock = self._eng()
        await eng.handle_reply(10, awaiting(conn), "1")
        row = chat_row(conn)
        self.assertEqual(row["status"], "awaiting_script_answer")
        self.assertEqual(int(row["ask_option_id"]), 1)
        self.assertIn("Vou precisar", sock.sent_texts()[0])
        self.assertIn("Qual o ID da atividade?", sock.sent_texts()[-1])

        await eng.handle_reply(10, chat_row(conn), "abc")
        self.assertEqual(chat_row(conn)["status"], "awaiting_script_answer")
        self.assertEqual(int(chat_row(conn)["retries"]), 1)
        self.assertIn("Qual o ID da atividade?", sock.sent_texts()[-1])

        await eng.handle_reply(10, chat_row(conn), "123")
        self.assertIn("1) HFC", sock.sent_texts()[-1])
        await eng.handle_reply(10, chat_row(conn), "9")
        self.assertIn("Qual a rede?", sock.sent_texts()[-1])
        await eng.handle_reply(10, chat_row(conn), "2")
        self.assertIn("Observacao", sock.sent_texts()[-1])
        await eng.handle_reply(10, chat_row(conn), "sem acesso")

        row = chat_row(conn)
        self.assertEqual(row["status"], "awaiting_script_confirm")
        conf = sock.sent_texts()[-1]
        self.assertIn("Confirme", conf)
        self.assertIn("GPON", conf)
        self.assertIn("1) Confirmar", conf)
        answers = json.loads(row["script_answers"])
        self.assertEqual(answers["q1"], "123")
        self.assertEqual(answers["q2"], "GPON")
        self.assertEqual(answers["q3"], "sem acesso")

        await eng.handle_reply(10, chat_row(conn), "2")
        self.assertEqual(chat_row(conn)["status"], "awaiting_script_answer")
        self.assertEqual(int(chat_row(conn)["script_field_index"]), 0)
        self.assertEqual(json.loads(chat_row(conn)["script_answers"]), {})
        self.assertIn("Qual o ID da atividade?", sock.sent_texts()[-1])

        await self._responde_tudo(eng, conn)
        await eng.handle_reply(10, chat_row(conn), "1")
        self.assertEqual(chat_row(conn)["status"], "closed")
        resumo = " ".join(sock.sent_texts())
        self.assertIn("Registrei", resumo)
        self.assertEqual(sock.last("invite_to_chat")["address"], ANA)
        self.assertEqual(sock.last("move_chat")["channel_address"], "h:q1")
        evs = events(conn)
        self.assertIn("ask_collected", evs)
        self.assertIn("transferred", evs)
        self.assertEqual(len(routes(conn, "done")), 1)
    async def test_sem_perguntas_mantem_fluxo_antigo(self):
        eng, conn, sock = self._eng(perguntas=[])
        await eng.handle_reply(10, awaiting(conn), "1")
        row = chat_row(conn)
        self.assertEqual(row["status"], "closed")
        self.assertIsNone(row.get("ask_option_id"))
        self.assertEqual(sock.sent_texts()[0], "Vou precisar de algumas informacoes.")
        self.assertEqual(sock.last("invite_to_chat")["address"], ANA)
        self.assertNotIn("ask_collected", events(conn))
        # A entrega inline agora deixa UM registro de entrega concluido (com o alvo
        # gravado): e o que impede mover/convidar de novo depois de um restart.
        todas = routes(conn)
        self.assertEqual(len(todas), 1)
        self.assertEqual(todas[0]["status"], "done")
        self.assertEqual(todas[0]["invite_target"], ANA)

    async def test_sem_analista_fica_na_fila_e_entrega_depois(self):
        eng, conn, sock = self._eng(online=[])
        await eng.handle_reply(10, awaiting(conn), "1")
        await self._responde_tudo(eng, conn)
        await eng.handle_reply(10, chat_row(conn), "1")
        evs = events(conn)
        self.assertIn("ask_collected", evs)
        self.assertIn("no_agent", evs)
        self.assertNotIn("move_chat", sock.names())
        wait = routes(conn, "waiting")
        self.assertEqual(len(wait), 1)
        self.assertEqual(wait[0]["kind"], "chat")
        self.assertEqual(int(wait[0]["attempts"]), 1)
        self.assertNotIn("leave_chat", sock.names())

        sock2 = RouteSock(online=["ana"])
        eng2 = engine_cfg(conn, sock2)
        conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
        await eng2.tick_routes()
        self.assertEqual(sock2.last("invite_to_chat")["address"], ANA)
        self.assertEqual(len(routes(conn, "done")), 1)
        self.assertEqual(len(routes(conn, "waiting")), 0)

    async def test_fila_avisa_posicao_sem_ask_transferring(self):
        """Trilho 'sem analista': o resumo nao promete transferencia e o aviso e de fila."""
        eng, conn, sock = self._eng(online=[])
        await eng.handle_reply(10, awaiting(conn), "1")
        await self._responde_tudo(eng, conn)
        await eng.handle_reply(10, chat_row(conn), "1")
        textos = " | ".join(sock.sent_texts())
        self.assertIn("Registrei", textos)
        self.assertNotIn("transferindo", textos.lower())
        self.assertIn("Nº 1", textos)
        self.assertNotIn("move_chat", sock.names())
        self.assertFalse(db.chat_moved(conn, 10))
        self.assertIn("queued", events(conn))

    async def test_entrega_imediata_envia_ask_transferring(self):
        """Trilho 'com analista': o texto de transferencia vai junto do move/invite."""
        eng, conn, sock = self._eng()          # ana online
        await eng.handle_reply(10, awaiting(conn), "1")
        await self._responde_tudo(eng, conn)
        await eng.handle_reply(10, chat_row(conn), "1")
        textos = sock.sent_texts()
        self.assertIn("transferindo", textos[-1].lower())
        self.assertNotIn("Nº 1", " | ".join(textos))
        self.assertEqual(sock.names().count("move_chat"), 1)
        self.assertEqual(chat_row(conn)["status"], "closed")
        self.assertTrue(db.chat_moved(conn, 10))

    async def test_invite_falha_apos_move_nao_move_de_novo_nem_avisa_fila(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        seed_ask_leaf(conn, perguntas=PERGUNTAS)
        sock = InviteFailSock(online=["ana"], falhas=1)
        eng = engine_cfg(conn, sock)
        await eng.handle_reply(10, awaiting(conn), "1")
        await self._responde_tudo(eng, conn)
        await eng.handle_reply(10, chat_row(conn), "1")

        evs = events(conn)
        self.assertIn("transferred_partial", evs)
        self.assertNotIn("queued", evs)                  # nada de aviso de fila depois do move
        self.assertNotIn("Nº 1", " | ".join(sock.sent_texts()))
        self.assertEqual(sock.names().count("move_chat"), 1)
        self.assertTrue(db.chat_moved(conn, 10))
        self.assertEqual(chat_row(conn)["status"], "classified")   # ainda nao saiu do chat
        self.assertEqual(len(routes(conn, "waiting")), 1)

        # Proxima rodada: o convite e retentado, mas o chat NAO e movido de novo.
        sock.falhas = 0
        conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        self.assertEqual(sock.names().count("move_chat"), 1)
        self.assertEqual(sock.names().count("invite_to_chat"), 2)
        self.assertEqual(len(routes(conn, "done")), 1)
        self.assertEqual(chat_row(conn)["status"], "closed")
        self.assertIn("leave_chat", sock.names())

    async def test_convite_falha_esgota_tentativas_e_aplica_o_fallback(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        seed_ask_leaf(conn, perguntas=PERGUNTAS)
        sock = InviteFailSock(online=["ana"], falhas=3)
        eng = engine_cfg(conn, sock, attempts="1")
        await eng.handle_reply(10, awaiting(conn), "1")
        await self._responde_tudo(eng, conn)
        await eng.handle_reply(10, chat_row(conn), "1")
        self.assertTrue(db.chat_moved(conn, 10))

        conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        self.assertEqual(len(routes(conn, "failed")), 1)
        self.assertIn("no_agent", events(conn))
        self.assertEqual(chat_row(conn)["status"], "closed_no_agent")
        self.assertEqual(sock.names().count("move_chat"), 1)     # nao ficou pulando o chat
        self.assertIn("leave_chat", sock.names())

    async def test_sem_fila_esgotar_tentativas_aplica_o_fallback_do_time(self):
        eng, conn, sock = self._eng(online=[])
        eng.cfg["queue_wait_enabled"] = "false"
        eng.cfg["transfer_attempts"] = "1"
        await eng.handle_reply(10, awaiting(conn), "1")
        await self._responde_tudo(eng, conn)
        await eng.handle_reply(10, chat_row(conn), "1")
        conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        self.assertEqual(len(routes(conn, "failed")), 1)
        self.assertEqual(chat_row(conn)["status"], "closed_no_agent")
        self.assertIn("leave_chat", sock.names())

    async def test_restart_no_meio_das_perguntas_reenvia_a_atual(self):
        eng, conn, sock = self._eng()
        await eng.handle_reply(10, awaiting(conn), "1")
        await eng.handle_reply(10, chat_row(conn), "123")
        self.assertIn("Qual a rede?", sock.sent_texts()[-1])

        sock2 = RouteSock(online=["ana"])
        eng2 = engine_cfg(conn, sock2)
        await eng2._resume_ask(chat_row(conn))
        self.assertIn("Qual a rede?", sock2.sent_texts()[-1])
        await eng2.handle_reply(10, chat_row(conn), "2")
        self.assertIn("Observacao", sock2.sent_texts()[-1])

    async def test_restart_na_confirmacao_reenvia_o_resumo(self):
        eng, conn, sock = self._eng()
        await eng.handle_reply(10, awaiting(conn), "1")
        await self._responde_tudo(eng, conn)
        self.assertEqual(chat_row(conn)["status"], "awaiting_script_confirm")

        sock2 = RouteSock(online=["ana"])
        eng2 = engine_cfg(conn, sock2)
        await eng2._resume_ask(chat_row(conn))
        self.assertIn("Confirme", sock2.sent_texts()[-1])
        self.assertIn("HFC", sock2.sent_texts()[-1])

    async def test_resume_fora_da_janela_nao_reenvia(self):
        eng, conn, sock = self._eng()
        await eng.handle_reply(10, awaiting(conn), "1")
        conn.execute("UPDATE chats SET last_prompt_at = datetime('now', '-7200 seconds') WHERE chat_id = 10")
        sock2 = RouteSock(online=["ana"])
        eng2 = engine_cfg(conn, sock2)
        await eng2._resume_ask(chat_row(conn))
        self.assertEqual(sock2.sent_texts(), [])

    async def test_confirmacao_invalida_fecha_no_limite(self):
        eng, conn, sock = self._eng()
        await eng.handle_reply(10, awaiting(conn), "1")
        await self._responde_tudo(eng, conn)
        for _ in range(4):
            await eng.handle_reply(10, chat_row(conn), "9")
        self.assertEqual(chat_row(conn)["status"], "closed_invalid")


    async def test_dez_perguntas_encadeiam_e_transferem(self):
        perguntas = [("Pergunta %d" % i, "text", "") for i in range(1, 11)]
        eng, conn, sock = self._eng(perguntas=perguntas)
        await eng.handle_reply(10, awaiting(conn), "1")
        for i in range(1, 11):
            await eng.handle_reply(10, chat_row(conn), "v%d" % i)
        row = chat_row(conn)
        self.assertEqual(row["status"], "awaiting_script_confirm")
        answers = json.loads(row["script_answers"])
        self.assertEqual(len(answers), 10)
        self.assertEqual(answers["q10"], "v10")
        await eng.handle_reply(10, chat_row(conn), "1")
        self.assertEqual(chat_row(conn)["status"], "closed")
        self.assertEqual(len(routes(conn, "done")), 1)
        self.assertEqual(sock.last("invite_to_chat")["address"], ANA)

    async def test_muitas_perguntas_vem_em_blocos(self):
        longa = "Pergunta bem longa para forcar a quebra em blocos de mensagem numero %02d"
        perguntas = [(longa % i, "text", "") for i in range(1, 31)]
        eng, conn, sock = self._eng(perguntas=perguntas)
        await eng.handle_reply(10, awaiting(conn), "1")
        for _ in range(29):
            await eng.handle_reply(10, chat_row(conn), "ok")
        antes = len(sock.sent_texts())
        await eng.handle_reply(10, chat_row(conn), "ok")
        blocos = sock.sent_texts()[antes:]
        self.assertGreaterEqual(len(blocos), 2)
        self.assertIn("Confirme", blocos[0])
        self.assertIn("1) Confirmar", blocos[-1])

        antesResumo = len(sock.sent_texts())
        await eng.handle_reply(10, chat_row(conn), "1")
        resumo = sock.sent_texts()[antesResumo:]
        self.assertGreaterEqual(len(resumo), 2)
        self.assertIn("Registrei", resumo[0])
        self.assertEqual(sock.last("invite_to_chat")["address"], ANA)
        self.assertEqual(chat_row(conn)["status"], "closed")


class AskQuestionsDbTest(unittest.TestCase):
    def test_questions_no_formato_do_wizard(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        opt = seed_ask_leaf(conn, perguntas=PERGUNTAS)
        fields = db.option_questions(conn, opt)
        self.assertEqual([f["key"] for f in fields], ["q1", "q2", "q3"])
        self.assertEqual([f["type"] for f in fields], ["int", "choice", "text"])
        self.assertEqual(fields[1]["choices"], ["HFC", "GPON"])
        self.assertEqual(fields[0]["prompt"], "Qual o ID da atividade?")
        self.assertEqual(db.option_questions(conn, None), [])
        self.assertEqual(db.option_questions(conn, 999), [])

    def test_excluir_opcao_remove_perguntas(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        opt = seed_ask_leaf(conn, perguntas=PERGUNTAS)
        self.assertEqual(len(db.option_questions(conn, opt)), 3)
        conn.execute("DELETE FROM menu_options WHERE id = ?", (opt,))
        self.assertEqual(len(db.option_questions(conn, opt)), 0)
if __name__ == "__main__":
    unittest.main()