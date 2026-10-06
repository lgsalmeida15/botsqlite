from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from .auth import SessionNotReady, close_browser, ensure_logged_in, launch_browser, page_on_origin
from .config import ROOT, db_backend, env, load_env

# O `.env` entra ANTES do `db`: o modulo decide as excecoes do backend no import
# (`db.IntegrityError`) e o `run.py` importa este arquivo - com o `db` dentro -
# antes de qualquer `load_env()`. Sem isto o `DB_BACKEND=mysql` do `.env` nao
# valeria para o DDL do `ensure_schema` nem para o `except` do bot.
load_env()

from . import db  # noqa: E402
from .engine import Engine
from .resume import BrowserStale, should_recycle
from .toa import ToaSocket

log = logging.getLogger("helptoa")
LOCK = ROOT / ".bot.lock"


def _browser_alive(page) -> bool:
    try:
        return page is not None and not page.is_closed()
    except Exception:
        return False


def acquire_lock() -> int:
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("outra instância do bot já está rodando")
    return fd


def _bind_stop(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _fire(*_args):
        if not stop.is_set():
            log.info("sinal de parada recebido")
            stop.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _fire)
        except (NotImplementedError, RuntimeError, AttributeError):
            try:
                signal.signal(sig, lambda *_a, _f=_fire: loop.call_soon_threadsafe(_f))
            except (ValueError, OSError):
                pass


async def run_once(conn, page, state: dict):
    origin = env("TOA_ORIGIN") or "https://clarosa.etadirect.com"
    state["ws"] = False
    engine_holder: dict = {}

    async def on_action(a, d):
        if engine_holder.get("e"):
            await engine_holder["e"].on_action(a, d)

    async def on_note(n, d):
        if engine_holder.get("e"):
            await engine_holder["e"].on_note(n, d)

    sock = ToaSocket(page, on_action, on_note)
    await sock.attach()
    await ensure_logged_in(page, origin)
    page = await page_on_origin(page.context, origin, page) or page
    await sock.rebind_page(page)

    engine = None
    run_task = asyncio.create_task(sock.run())
    try:
        await sock.connect()
        state["ws"] = True
        engine = Engine(conn, sock)
        engine_holder["e"] = engine

        async def timers():
            n = 0
            version_fails = 0
            while True:
                await asyncio.sleep(5)
                n += 5
                try:
                    if db.conn_closed(engine.conn):
                        engine.conn = db.connect()
                        log.info("%s reconectado (timer)", db_backend())
                    await engine.tick_timeouts()
                    await engine.tick_routes()
                    await engine.process_pending_jobs()
                    poll_load = max(5, int(engine.cfg.get("load_poll_sec") or 30))
                    if n % poll_load == 0:
                        # Carga por analista: le as listas do TOA e grava a medicao
                        # (aba Carga). E a mesma fonte da capacidade.
                        await engine.snapshot_load("timer")
                    poll = int(engine.cfg.get("config_poll_sec") or 15)
                    if n % max(poll, 5) == 0:
                        engine.reload_config()
                    if n % 60 == 0:
                        try:
                            ack = await sock.command("get_version")
                            if isinstance(ack, dict) and ack.get("timeout"):
                                version_fails += 1
                                log.warning("get_version timeout consecutivos=%s", version_fails)
                            else:
                                version_fails = 0
                        except Exception:
                            version_fails += 1
                            log.exception("get_version falhou consecutivos=%s", version_fails)
                    stale_sec = int(engine.cfg.get("ws_stale_sec") or 180)
                    age = time.monotonic() - sock.last_frame_at
                    if should_recycle(version_fails, age, fail_limit=3, stale_sec=stale_sec):
                        raise BrowserStale(
                            f"ws stale fails={version_fails} age={age:.0f}s limit={stale_sec}s"
                        )
                except BrowserStale:
                    raise
                except Exception:
                    log.exception("timer")

        await engine.bootstrap()
        await asyncio.gather(run_task, timers())
    finally:
        await sock.close()
        if not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass


async def _run_until_stop(conn, page, state: dict, stop: asyncio.Event) -> bool:
    task = asyncio.create_task(run_once(conn, page, state))
    stopper = asyncio.create_task(stop.wait())
    _done, pending = await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
    for p in pending:
        p.cancel()
        try:
            await p
        except (asyncio.CancelledError, Exception):
            pass
    if stop.is_set():
        return True
    await task
    return False


async def amain() -> None:
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    acquire_lock()
    stop = asyncio.Event()
    _bind_stop(stop)
    conn = db.connect()
    log.info("banco pronto (%s)", db_backend())
    backoff = 2
    pw = ctx = page = None
    state = {"ws": False}
    origin = env("TOA_ORIGIN") or "https://clarosa.etadirect.com"
    try:
        while not stop.is_set():
            try:
                if page is None:
                    pw, ctx, page = await launch_browser()
                stopped = await _run_until_stop(conn, page, state, stop)
                if stopped:
                    break
                backoff = 2
            except SessionNotReady as e:
                log.info("%s; Chromium permanece aberto", e)
                if ctx is not None:
                    page = await page_on_origin(ctx, origin, page) or page
                await asyncio.sleep(2)
                backoff = 2
            except BrowserStale as e:
                log.warning("%s; reciclando Chromium", e)
                await close_browser(pw, ctx)
                pw = ctx = page = None
                state["ws"] = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception:
                log.exception("ciclo do bot caiu; reconecta em %ss", backoff)
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    conn = db.connect()
                    log.info("%s reconectado", db_backend())
                except Exception:
                    log.warning("falha ao reconectar %s", db_backend(), exc_info=True)
                if _browser_alive(page):
                    if ctx is not None:
                        page = await page_on_origin(ctx, origin, page) or page
                    log.info("Chromium permanece aberto")
                else:
                    await close_browser(pw, ctx)
                    pw = ctx = page = None
                    state["ws"] = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    finally:
        await close_browser(pw, ctx)
        log.info("Chromium fechado; saída limpa")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
