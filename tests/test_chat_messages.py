# -*- coding: utf-8 -*-
"""Reproducao do chat: toda mensagem (chat e formulario, qualquer autor) vira
uma linha em `chat_messages` -- ver schema.sql e o comentario de `Engine._record_message`."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python.engine import MEM, Engine  # noqa: E402
from tests.test_submenu import mem_conn  # noqa: E402

TEC = "u:tec@claro.com"
ANA = "u:ana@claro.com"
BOT = "u:bot@claro.com"


class FakeResponse:
    def __init__(self, *, ok: bool = True, body: bytes = b"", content_type: str = "application/octet-stream"):
        self.ok = ok
        self._body = body
        self.headers = {"content-type": content_type}

    async def body(self) -> bytes:
        return self._body


class FakeRequest:
    def __init__(self, *, response: FakeResponse | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.urls: list[str] = []

    async def get(self, url: str, timeout: float | None = None):
        self.urls.append(url)
        if self._error:
            raise self._error
        return self._response


class FakeContext:
    def __init__(self, request: FakeRequest):
        self.request = request


class FakePage:
    def __init__(self, url: str, request: FakeRequest):
        self.url = url
        self.context = FakeContext(request)


class FakeSock:
    own_address = BOT

    def __init__(self, *, messages=None, page=None):
        self.cmds: list[tuple[str, dict]] = []
        self.messages = list(messages or [])
        self.page = page

    async def command(self, name, payload=None, **kwargs):
        payload = payload or {}
        self.cmds.append((name, payload))
        if name == "get_chat_messages":
            return {"d": {"messages": list(self.messages)}}
        return {"d": {}}


def chat_messages(conn, cid: int = 10) -> list[dict]:
    return conn.execute(
        "SELECT * FROM chat_messages WHERE chat_id = ? ORDER BY id", (cid,)
    ).fetchall()


class BaseTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _eng(self, *, messages=None, page=None) -> tuple[Engine, object, FakeSock]:
        conn = mem_conn()
        sock = FakeSock(messages=messages, page=page)
        eng = Engine(conn, sock)
        return eng, conn, sock

    def _mem(self, cid: int = 10, addr: str = TEC) -> None:
        MEM[cid] = {
            "started_addr": addr,
            "queue_address": "h:q1",
            "originator_spoke": False,
            "bot_present": True,
            "raw": {"chat_id": cid, "started_addr": addr},
        }


class ChatMessageTest(BaseTest):
    """Mensagem de chat: solicitante, analista e nota de sistema, em qualquer status."""

    async def test_mensagem_do_solicitante_e_gravada(self):
        eng, conn, sock = self._eng()
        self._mem()
        await eng.on_message({"chat_id": 10, "author": TEC, "text": "bom dia", "time": 1700000000})
        rows = chat_messages(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["author_addr"], TEC)
        self.assertEqual(rows[0]["text"], "bom dia")
        self.assertEqual(rows[0]["kind"], "chat")
        self.assertEqual(rows[0]["direction"], "in")
        self.assertEqual(rows[0]["queue_address"], "h:q1")

    async def test_mensagem_do_analista_e_gravada_mesmo_chat_monitorando(self):
        """Hoje `on_message` descarta mensagem de terceiro quando ha `started_addr`
        (engine.py `_addr_eq` != origin) -- a gravacao tem que acontecer ANTES desse filtro."""
        eng, conn, sock = self._eng()
        self._mem(addr=TEC)
        await eng.on_message({"chat_id": 10, "author": ANA, "text": "Pode confirmar o chamado?"})
        rows = chat_messages(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["author_addr"], ANA)

    async def test_mensagem_de_status_nao_tratavel_e_gravada(self):
        """Hoje `on_message` loga so `message_ignored` (sem texto) fora de `_REPLY_STATUSES`."""
        from python import db
        from tests.test_submenu import seed_queue_menu

        eng, conn, sock = self._eng()
        seed_queue_menu(conn)
        self._mem()
        db.upsert_chat(conn, 10, queue_address="h:q1", status="monitoring")
        await eng.on_message({"chat_id": 10, "author": TEC, "text": "obrigado"})
        rows = chat_messages(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "obrigado")

    async def test_nota_de_sistema_e_gravada_como_system(self):
        eng, conn, sock = self._eng()
        self._mem()
        await eng.on_message(
            {"chat_id": 10, "system_message": "user_joined", "author": ANA, "participant": {"address": ANA}}
        )
        rows = chat_messages(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "system")
        self.assertEqual(rows[0]["system_message"], "user_joined")
        self.assertEqual(rows[0]["author_addr"], ANA)

    async def test_mensagem_do_bot_no_eco_do_ws_nao_duplica_o_send(self):
        """`send()` ja grava a saida do bot; o eco do mesmo autor via WS nao pode duplicar."""
        eng, conn, sock = self._eng()
        self._mem()
        await eng.send(10, "Olá, sou o assistente")
        await eng.on_message({"chat_id": 10, "author": BOT, "text": "Olá, sou o assistente"})
        rows = chat_messages(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "out")
        self.assertEqual(rows[0]["is_bot"], 1)

    async def test_idempotencia_mesma_mensagem_nao_duplica(self):
        eng, conn, sock = self._eng()
        self._mem()
        msg = {"chat_id": 10, "author": TEC, "text": "1", "time": 1700000000}
        await eng.on_message(dict(msg))
        await eng.on_message(dict(msg))
        self.assertEqual(len(chat_messages(conn)), 1)


class FormMessageTest(BaseTest):
    """Formulario: o bot nunca entra na sala, so le por polling (`get_chat_messages`)."""

    async def test_mensagens_do_formulario_sao_gravadas_no_refresh(self):
        msgs = [
            {"author": TEC, "text": "preciso de ajuda", "time": 1},
            {"author": ANA, "system_message": "user_joined", "time": 2},
        ]
        eng, conn, sock = self._eng(messages=msgs)
        await eng._refresh_form_state(99, forcar=True)
        rows = chat_messages(conn, 99)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["kind"] == "form" for r in rows))

    async def test_refresh_repetido_nao_duplica(self):
        msgs = [{"author": TEC, "text": "preciso de ajuda", "time": 1}]
        eng, conn, sock = self._eng(messages=msgs)
        await eng._refresh_form_state(99, forcar=True)
        await eng._refresh_form_state(99, forcar=True)
        self.assertEqual(len(chat_messages(conn, 99)), 1)


class AttachmentTest(BaseTest):
    """Anexo: metadado vem na mensagem, binario e baixado via pagina autenticada do bot."""

    def _attachment(self) -> dict:
        return {
            "attachment_id": 1,
            "type": "image",
            "data": {"displayName": "print.png", "fileName": "abc/2026-01-01/x_print.png", "fileType": "image"},
        }

    async def test_anexo_baixado_com_sucesso_grava_binario(self):
        resp = FakeResponse(ok=True, body=b"bytes-da-imagem", content_type="application/octet-stream")
        page = FakePage("https://clarosa.etadirect.com/chat", FakeRequest(response=resp))
        eng, conn, sock = self._eng(page=page)
        self._mem()
        await eng.on_message({"chat_id": 10, "author": TEC, "text": "", "attachment": self._attachment()})
        rows = chat_messages(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["has_attachment"], 1)
        self.assertEqual(bytes(rows[0]["attachment_data"]), b"bytes-da-imagem")
        self.assertEqual(rows[0]["attachment_fetch_status"], "ok")
        self.assertEqual(rows[0]["attachment_mime"], "image/png")
        self.assertEqual(rows[0]["attachment_name"], "print.png")
        self.assertIn("m=filestorage", sock.page.context.request.urls[0])

    async def test_download_com_falha_nao_impede_gravar_a_mensagem(self):
        page = FakePage("https://clarosa.etadirect.com/chat", FakeRequest(error=RuntimeError("timeout")))
        eng, conn, sock = self._eng(page=page)
        self._mem()
        await eng.on_message({"chat_id": 10, "author": TEC, "text": "segue o print", "attachment": self._attachment()})
        rows = chat_messages(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "segue o print")
        self.assertEqual(rows[0]["has_attachment"], 1)
        self.assertIsNone(rows[0]["attachment_data"])
        self.assertEqual(rows[0]["attachment_fetch_status"], "failed")

    async def test_anexo_acima_do_teto_nao_grava_binario(self):
        resp = FakeResponse(ok=True, body=b"x" * (9 * 1024 * 1024))
        page = FakePage("https://clarosa.etadirect.com/chat", FakeRequest(response=resp))
        eng, conn, sock = self._eng(page=page)
        self._mem()
        await eng.on_message({"chat_id": 10, "author": TEC, "text": "arquivo grande", "attachment": self._attachment()})
        rows = chat_messages(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "arquivo grande")
        self.assertIsNone(rows[0]["attachment_data"])
        self.assertEqual(rows[0]["attachment_fetch_status"], "too_large")

    async def test_sem_pagina_autenticada_nao_quebra_o_fluxo(self):
        eng, conn, sock = self._eng(page=None)
        self._mem()
        await eng.on_message({"chat_id": 10, "author": TEC, "text": "", "attachment": self._attachment()})
        rows = chat_messages(conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attachment_fetch_status"], "failed")


if __name__ == "__main__":
    unittest.main()
