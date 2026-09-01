from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote

from .auth import SessionNotReady

log = logging.getLogger("helptoa")
BRIDGE_JS = Path(__file__).with_name("bridge.js")
_BRIDGE_SRC = BRIDGE_JS.read_text(encoding="utf-8")

# Um único binding de contexto. Cada ToaSocket se registra aqui; o retry de
# login não pode expor de novo (Playwright recusa) senão os frames caem numa
# fila morta e o bot não grava nem responde.
_push_target: ToaSocket | None = None


async def _bridge_push(rec) -> None:
    t = _push_target
    if t is None or t._closed.is_set():
        return
    await t.frames.put(rec)


async def bind_context_push(ctx) -> None:
    try:
        await ctx.expose_function("__helptoaPush", _bridge_push)
    except Exception as e:
        log.error("falha ao expor __helptoaPush no contexto: %s", e)
        raise RuntimeError("gancho __helptoaPush não pôde ser ligado") from e


class ToaSocket:
    def __init__(self, page, on_action: Callable, on_note: Callable):
        self.page = page
        self.on_action = on_action
        self.on_note = on_note
        self.pending: dict[str, asyncio.Future] = {}
        self.own_address = ""
        self.frames: asyncio.Queue = asyncio.Queue()
        self._closed = asyncio.Event()
        self.last_frame_at = time.monotonic()

    def _claim(self) -> None:
        global _push_target
        _push_target = self

    async def attach(self) -> None:
        self._claim()
        try:
            await self.page.evaluate(_BRIDGE_SRC)
        except Exception as e:
            log.warning("inject bridge: %s", e)

    async def connect(self) -> None:
        await self.attach()
        started = time.monotonic()
        deadline = started + 120
        last_wait_log = 0.0
        while time.monotonic() < deadline:
            if self.page.is_closed():
                raise SessionNotReady("aba do TOA fechou")
            try:
                info = await self.page.evaluate("() => window.__HELPTOA && window.__HELPTOA.info()")
            except Exception:
                info = None
            if info and info.get("open"):
                self.own_address = unquote(str(info.get("u") or ""))
                log.info("WS da página aberto u=%s", self.own_address or "?")
                return
            now = time.monotonic()
            if now - last_wait_log >= 15:
                log.info("aguardando WebSocket de chat url=%s", getattr(self.page, "url", ""))
                last_wait_log = now
            await asyncio.sleep(0.5)
        raise SessionNotReady("TOA não abriu o WebSocket da página")

    async def run(self) -> None:
        while not self._closed.is_set():
            try:
                rec = await asyncio.wait_for(self.frames.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if self.page.is_closed():
                    break
                continue
            await self._dispatch(rec)
            if _is_close(rec):
                log.info("WS nativo fechou; continuando à espera da reconexão da página")

    def _spawn(self, coro) -> None:
        async def _run():
            try:
                await coro
            except Exception:
                log.exception("handler TOA")
        asyncio.create_task(_run())

    async def _dispatch(self, rec) -> None:
        self.last_frame_at = time.monotonic()
        if not isinstance(rec, dict):
            return
        data = rec.get("data")
        if rec.get("payloadFormat") == "json" and isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return
        if not isinstance(data, dict):
            return
        if data.get("event") == "close":
            log.info("WS close code=%s", data.get("code"))
            return
        if rec.get("direction") == "sent":
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
        r = str(random.randint(10_000_000, 99_999_999))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending[r] = fut
        payload = {"a": a, "d": d or {}, "r": r, "t": str(int(time.time() * 1000))}
        try:
            await self.page.evaluate("(p) => window.__HELPTOA.send(p)", payload)
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
        global _push_target
        if _push_target is self:
            _push_target = None


def _is_close(rec) -> bool:
    if not isinstance(rec, dict):
        return False
    data = rec.get("data")
    return isinstance(data, dict) and data.get("event") == "close"
