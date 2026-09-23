# -*- coding: utf-8 -*-
"""Resposta do tecnico que nao pode se perder: race do envio, silencio e replay no resume."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import MEM, Engine, _silence_expired  # noqa: E402
from tests.test_form_route import events  # noqa: E402
from tests.test_submenu import awaiting, insert_option, mem_conn, seed_queue_menu  # noqa: E402

TEC = "u:tec@claro.com"
BOT = "u:bot@claro.com"


def epoch_ms(*, seconds_ago: int = 0) -> int:
    return int((datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).timestamp() * 1000)


class ReplySock:
    """Socket fake: consegue responder DURANTE o envio e devolver mensagens do chat."""

    own_address = BOT

    def __init__(self, *, reply_on_send: str | None = None, messages=None, online=None):
        self.cmds: list[tuple[str, dict]] = []
        self.eng: Engine | None = None
        self.reply_on_send = reply_on_send
        self.messages = list(messages or [])
        self.online = list(online if online is not None else ["ana"])

    async def command(self, name, payload=None, **kwargs):
        payload = payload or {}
        self.cmds.append((name, payload))
        if name == "get_online_users":
            return {"d": {"result": list(self.online)}}
        if name == "get_active_chats":
            return {"d": {"chats": []}}
        if name == "join_chat":
            return {"d": {"present": True, "address": BOT, "chat_id": payload.get("chat_id")}}
        if name == "get_chat_messages":
            return {"d": {"messages": list(self.messages)}}
        if name == "send_message" and self.reply_on_send and self.eng:
            # O tecnico responde no instante em que o bot ainda espera o ACK do envio.
            await self.eng.on_message(
                {"chat_id": payload["chat_id"], "author": self.reply_on_send, "text": "1"}
            )
        return {"d": {}}

    def names(self) -> list[str]:
        return [n for n, _ in self.cmds]

    def texts(self) -> list[str]:
        return [p["message"]["text"] for n, p in self.cmds if n == "send_message"]


def chat_row(conn, cid: int = 10) -> dict:
    return conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone()


def seed_leaf(conn, **over) -> int:
    campos = {
        "menu_id": 1,
        "sort_order": 1,
        "label": "Apoio",
        "classification": "apoio",
        "action": "reply_and_close",
        "reply_text": "Encerrando conforme solicitado.",
    }
    campos.update(over)
    return insert_option(conn, **campos)


class BaseTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _eng(self, *, reply_on_send=None, messages=None, online=None, **opt):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        seed_leaf(conn, **opt)
        sock = ReplySock(reply_on_send=reply_on_send, messages=messages, online=online)
        eng = Engine(conn, sock)
        sock.eng = eng
        eng.cfg["transfer_retry_interval_sec"] = "0"
        return eng, conn, sock

    def _mem(self, cid: int = 10, addr: str = TEC) -> None:
        MEM[cid] = {
            "started_addr": addr,
            "queue_address": "h:q1",
            "originator_spoke": False,
            "bot_present": True,
            "raw": {"chat_id": cid, "started_addr": addr},
        }


class RespostaDuranteOEnvioTest(BaseTest):
    """A resposta que chega enquanto o bot envia o menu nao pode ser descartada."""

    async def test_resposta_durante_o_envio_do_menu_e_processada(self):
        eng, conn, sock = self._eng(reply_on_send=TEC)
        self._mem()
        await eng.join_and_greet(10, "h:q1")
        row = chat_row(conn)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["classification"], "apoio")
        self.assertIn("classified", events(conn))
        self.assertIn("menu_sent", events(conn))

    async def test_resposta_durante_o_envio_do_submenu_e_processada(self):
        eng, conn, sock = self._eng(reply_on_send=TEC)
        pai = insert_option(
            conn, menu_id=1, sort_order=2, label="Grupo", classification="", action="submenu"
        )
        insert_option(
            conn,
            menu_id=1,
            parent_id=pai,
            sort_order=1,
            label="Filho",
            classification="filho",
            action="reply_and_close",
        )
        self._mem()
        await eng.join_and_greet(10, "h:q1")
        await eng.handle_reply(10, chat_row(conn), "2")
        row = chat_row(conn)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["classification"], "filho")

    async def test_autor_com_formato_diferente_e_aceito(self):
        eng, conn, sock = self._eng()
        self._mem(addr="TEC@CLARO.COM")
        awaiting(conn)
        await eng.on_message({"chat_id": 10, "author": TEC, "text": "1"})
        self.assertEqual(chat_row(conn)["classification"], "apoio")
        self.assertIn("classified", events(conn))

    async def test_mensagem_de_outro_participante_nao_e_tratada(self):
        eng, conn, sock = self._eng()
        self._mem()
        awaiting(conn)
        await eng.on_message({"chat_id": 10, "author": "u:outro@claro.com", "text": "1"})
        self.assertEqual(events(conn), [])
        self.assertIsNone(chat_row(conn)["last_reply_at"])
        self.assertEqual(chat_row(conn)["status"], "awaiting_reply")


class SilencioDoTecnicoTest(BaseTest):
    """O timeout mede silencio do tecnico, nao 'tempo desde o ultimo prompt'."""

    async def test_atividade_recente_impede_o_timeout(self):
        eng, conn, sock = self._eng()
        awaiting(conn)                     # last_prompt_at = 2000-01-01 (muito antigo)
        db.mark_reply(conn, 10)            # tecnico acabou de falar
        await eng.tick_timeouts()
        self.assertEqual(chat_row(conn)["status"], "awaiting_reply")
        self.assertNotIn("leave_chat", sock.names())

    async def test_resposta_invalida_conta_como_atividade(self):
        eng, conn, sock = self._eng()
        awaiting(conn)
        await eng.on_message({"chat_id": 10, "author": TEC, "text": "bom dia"})
        self.assertIsNotNone(chat_row(conn)["last_reply_at"])
        await eng.tick_timeouts()
        self.assertEqual(chat_row(conn)["status"], "awaiting_reply")

    async def test_silencio_de_verdade_encerra_com_o_closing(self):
        eng, conn, sock = self._eng()
        awaiting(conn)
        await eng.tick_timeouts()
        self.assertEqual(chat_row(conn)["status"], "closed_timeout")
        self.assertIn("leave_chat", sock.names())
        self.assertIn("Chat está sendo encerrado", sock.texts())
        self.assertIn("closed_timeout", events(conn))

    def test_silencio_expira_pela_marca_mais_recente(self):
        agora = datetime.now(timezone.utc)
        velho = (agora - timedelta(seconds=400)).isoformat(sep=" ")
        novo = (agora - timedelta(seconds=10)).isoformat(sep=" ")
        self.assertFalse(_silence_expired({"last_prompt_at": velho, "last_reply_at": novo}, 300, now=agora))
        self.assertTrue(_silence_expired({"last_prompt_at": velho, "last_reply_at": None}, 300, now=agora))
        self.assertFalse(_silence_expired({"last_prompt_at": None, "last_reply_at": None}, 300, now=agora))


class ReplayResumeTest(BaseTest):
    """Resposta que chegou com o bot fora do chat (restart/reciclagem) e reprocessada."""

    async def test_resposta_pendente_e_processada(self):
        msgs = [{"author": TEC, "text": "1", "time": epoch_ms(seconds_ago=5)}]
        eng, conn, sock = self._eng(messages=msgs)
        self._mem()
        self.assertTrue(await eng.replay_pending_reply(10, awaiting(conn)))
        self.assertEqual(chat_row(conn)["classification"], "apoio")
        self.assertIn("reply_replayed", events(conn))

    async def test_replay_nao_reprocessa_a_mesma_mensagem(self):
        msgs = [{"author": TEC, "text": "1", "time": epoch_ms(seconds_ago=5)}]
        eng, conn, sock = self._eng(messages=msgs)
        self._mem()
        await eng.replay_pending_reply(10, awaiting(conn))
        conn.execute("UPDATE chats SET status = 'awaiting_reply' WHERE chat_id = 10")
        self.assertFalse(await eng.replay_pending_reply(10, chat_row(conn)))
        self.assertEqual(events(conn).count("reply_replayed"), 1)

    async def test_mensagem_anterior_ao_menu_nao_e_reprocessada(self):
        msgs = [{"author": TEC, "text": "1", "time": epoch_ms(seconds_ago=600)}]
        eng, conn, sock = self._eng(messages=msgs)
        self._mem()
        awaiting(conn)
        db.upsert_chat(conn, 10, status="awaiting_reply", menu_sent_at=datetime.now(timezone.utc))
        self.assertFalse(await eng.replay_pending_reply(10, chat_row(conn)))
        self.assertNotIn("reply_replayed", events(conn))

    async def test_replay_usa_a_mensagem_mais_recente(self):
        msgs = [
            {"author": TEC, "text": "9", "time": epoch_ms(seconds_ago=90)},   # antiga (invalida)
            {"author": TEC, "text": "1", "time": epoch_ms(seconds_ago=5)},    # mais recente
        ]
        eng, conn, sock = self._eng(messages=msgs)
        self._mem()
        await eng.replay_pending_reply(10, awaiting(conn))
        self.assertEqual(chat_row(conn)["classification"], "apoio")

    async def test_autor_de_sistema_nao_e_reprocessado(self):
        msgs = [{"author": "u:CREATEDBYMESSAGESCENARIO", "text": "1", "time": epoch_ms(seconds_ago=5)}]
        eng, conn, sock = self._eng(messages=msgs)
        MEM[10] = {"queue_address": "h:q1", "originator_spoke": False, "raw": {}}
        self.assertFalse(await eng.replay_pending_reply(10, awaiting(conn)))
        self.assertNotIn("reply_replayed", events(conn))

    async def test_mensagem_do_bot_nao_e_reprocessada(self):
        msgs = [{"author": BOT, "text": "1", "time": epoch_ms(seconds_ago=5)}]
        eng, conn, sock = self._eng(messages=msgs)
        self._mem()
        self.assertFalse(await eng.replay_pending_reply(10, awaiting(conn)))
        self.assertNotIn("reply_replayed", events(conn))

    async def test_sem_mensagem_no_toa_nao_faz_nada(self):
        eng, conn, sock = self._eng(messages=[])
        self._mem()
        self.assertFalse(await eng.replay_pending_reply(10, awaiting(conn)))
        self.assertEqual(chat_row(conn)["status"], "awaiting_reply")

    async def test_status_fora_de_espera_nao_reprocessa(self):
        msgs = [{"author": TEC, "text": "1", "time": epoch_ms(seconds_ago=5)}]
        eng, conn, sock = self._eng(messages=msgs)
        self._mem()
        await eng.handle_reply(10, awaiting(conn), "1")      # encerra em closed
        self.assertFalse(await eng.replay_pending_reply(10, chat_row(conn)))
        self.assertNotIn("reply_replayed", events(conn))


if __name__ == "__main__":
    unittest.main()
