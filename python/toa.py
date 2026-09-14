from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import time
from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlparse

import websockets

from .auth import SessionNotReady
from .config import env

log = logging.getLogger("helptoa")


def _insecure_ssl_context() -> ssl.SSLContext:
    # O proxy corporativo faz interceptação/reescrita de certificado; a
    # validação padrão do TLS quebra a conexão com este proxy específico.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ToaSocket:
    def __init__(self, page, on_action: Callable, on_note: Callable):
        self.page = page
        self.on_action = on_action
        self.on_note = on_note
        self.pending: dict[str, asyncio.Future] = {}
        self.own_address = ""
        self._closed = asyncio.Event()
        self.last_frame_at = time.monotonic()
        self.ws: websockets.ClientConnection | None = None
        self._url_future: asyncio.Future | None = None

    def _on_native_ws(self, native_ws) -> None:
        # Captura a URL que o próprio JS do TOA usa para abrir o WS de chat
        # (com auth ticket/session key corretos) — não decodificamos o
        # communityConfig porque o TOA agora criptografa esse valor no
        # localStorage (prefixo "__ofsc_gcm__:").
        url = getattr(native_ws, "url", "")
        log.info("WebSocket nativo criado pela página url=%s", url)
        if url and "/websocket/" in url and self._url_future is not None and not self._url_future.done():
            self._url_future.set_result(url)

    def _on_console(self, msg) -> None:
        if msg.type in ("error", "warning"):
            log.warning("console navegador [%s] %s", msg.type, msg.text)

    def _on_request_failed(self, request) -> None:
        log.warning("request falhou url=%s failure=%s", request.url, request.failure)

    def _attach_listeners(self) -> None:
        self.page.on("websocket", self._on_native_ws)
        self.page.on("console", self._on_console)
        self.page.on("requestfailed", self._on_request_failed)

    def _detach_listeners(self) -> None:
        for event, handler in (
            ("websocket", self._on_native_ws),
            ("console", self._on_console),
            ("requestfailed", self._on_request_failed),
        ):
            try:
                self.page.remove_listener(event, handler)
            except Exception:
                pass

    async def attach(self) -> None:
        # Registra cedo (antes do login/navegação), para não perder a
        # primeira tentativa de WS que o TOA abre assim que a página carrega.
        self._url_future = asyncio.get_running_loop().create_future()
        self._attach_listeners()

    async def rebind_page(self, page) -> None:
        # Usado quando a aba "ativa" muda (ex.: após ensure_logged_in
        # trocar de página); move os listeners para a nova página.
        if page is self.page:
            return
        self._detach_listeners()
        self.page = page
        self._attach_listeners()

    async def connect(self) -> None:
        proxy = env("TOA_PROXY") or None
        ssl_ctx = _insecure_ssl_context()
        started = time.monotonic()
        deadline = started + 120
        last_wait_log = 0.0
        last_err: Exception | None = None

        if self._url_future is None:
            await self.attach()

        while time.monotonic() < deadline:
            if self.page.is_closed():
                raise SessionNotReady("aba do TOA fechou")
            try:
                url = await asyncio.wait_for(asyncio.shield(self._url_future), timeout=1.0)
            except asyncio.TimeoutError:
                now = time.monotonic()
                if now - last_wait_log >= 15:
                    log.info(
                        "aguardando WebSocket de chat url=%s err=%s",
                        getattr(self.page, "url", ""), last_err,
                    )
                    last_wait_log = now
                continue
            try:
                self.ws = await websockets.connect(
                    url, proxy=proxy, ssl=ssl_ctx, open_timeout=15
                )
                own = unquote(parse_qs(urlparse(url).query).get("u", [""])[0])
                self.own_address = own
                log.info("WS TOA aberto u=%s", own or "?")
                return
            except Exception as e:
                last_err = e
                self._url_future = asyncio.get_running_loop().create_future()
        raise SessionNotReady(f"TOA não abriu o WebSocket: {last_err}")

    async def run(self) -> None:
        while not self._closed.is_set():
            if self.ws is None:
                await asyncio.sleep(1.0)
                continue
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                if self.page.is_closed():
                    break
                continue
            except websockets.exceptions.ConnectionClosed:
                log.info("WS TOA fechou; aguardando reciclagem do navegador")
                self.ws = None
                continue
            await self._dispatch(raw)

    def _spawn(self, coro) -> None:
        async def _run():
            try:
                await coro
            except Exception:
                log.exception("handler TOA")
        asyncio.create_task(_run())

    async def _dispatch(self, raw) -> None:
        self.last_frame_at = time.monotonic()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return
        if data.get("r") is not None:
            key = str(data["r"])
            fut = self.pending.pop(key, None)
            if fut and not fut.done():
                fut.set_result(data)
        if data.get("a"):
            self._spawn(self.on_action(data["a"], data.get("d") or {}))
        for block in (data, data.get("data")):
            if isinstance(block, dict) and block.get("n"):
                self._spawn(self.on_note(block["n"], block.get("d") or {}))

    async def command(self, a: str, d: dict | None = None, timeout: float = 8.0) -> dict:
        if self.ws is None:
            raise RuntimeError("WebSocket do TOA não está aberto")
        r = str(random.randint(10_000_000, 99_999_999))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending[r] = fut
        payload = {"a": a, "d": d or {}, "r": r, "t": str(int(time.time() * 1000))}
        try:
            await self.ws.send(json.dumps(payload))
        except Exception as e:
            self.pending.pop(r, None)
            raise RuntimeError(str(e)) from e
        try:
            ack = await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            self.pending.pop(r, None)
            log.info("TOA command %s sem eco r=%s (timeout %.0fs)", a, r, timeout)
            return {"ok": True, "timeout": True}
        if ack.get("e") not in (None, 0):
            raise RuntimeError(
                ack.get("d", {}).get("message") or ack.get("d", {}).get("error") or str(ack.get("e"))
            )
        return ack

    async def close(self) -> None:
        self._closed.set()
        self._detach_listeners()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
