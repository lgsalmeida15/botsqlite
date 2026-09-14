from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from playwright.async_api import async_playwright

from .config import ROOT, env

log = logging.getLogger("helptoa")
PROFILE = ROOT / ".chrome-profile"

_ERROR_MARKERS = (
    "Solicitação incorreta",
    "Solicitacao incorreta",
    "Incorrect request",
)

_SEL_USERNAME = "#username"
_SEL_PASSWORD = "#password"
_SEL_RESTORE = 'input[data-label="restore"]'
_LOGIN_RETRY_SEC = 10.0
_FIELD_WAIT_MS = 15000
_DEFAULT_NAV_TIMEOUT_MS = 60000


def _nav_timeout_ms() -> int:
    raw = (env("TOA_NAV_TIMEOUT_MS") or "").strip()
    if not raw:
        return _DEFAULT_NAV_TIMEOUT_MS
    try:
        val = int(float(raw))
    except ValueError:
        log.warning(
            "TOA_NAV_TIMEOUT_MS inválido (%r); usando %s", raw, _DEFAULT_NAV_TIMEOUT_MS
        )
        return _DEFAULT_NAV_TIMEOUT_MS
    return val if val > 0 else _DEFAULT_NAV_TIMEOUT_MS

_SESSION_JS = """async () => {
    const r = await fetch(location.origin + '/index.php?m=manage&a=get', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'}
    });
    const text = await r.text();
    if (!text) return {ok: false, status: r.status, len: 0};
    try {
        const j = JSON.parse(text);
        const keys = j && typeof j === 'object' && !Array.isArray(j) ? Object.keys(j) : [];
        return {ok: keys.length > 0, status: r.status, keys: keys.slice(0, 16)};
    } catch (e) {
        return {ok: false, status: r.status, preview: String(text).slice(0, 80)};
    }
}"""


class SessionNotReady(Exception):
    """Login/SSO ainda não entregou JSON válido de manage/supervisor."""


def _landing(origin: str) -> str:
    return origin.rstrip("/") + "/"


def _on_origin(page, origin: str) -> bool:
    return (page.url or "").startswith(origin)


async def page_on_origin(ctx, origin: str, fallback=None):
    for p in ctx.pages:
        if _on_origin(p, origin) and not p.is_closed():
            return p
    return fallback


async def _body_text(page) -> str:
    try:
        return await page.inner_text("body")
    except Exception:
        return ""


async def _is_error_page(page) -> bool:
    text = await _body_text(page)
    return any(m.lower() in text.lower() for m in _ERROR_MARKERS)


async def _has_login_form(page) -> bool:
    found = await _locate(page, 'input[type="password"]', quiet=True)
    return found is not None


async def _can_probe(page, origin: str) -> bool:
    if page is None or page.is_closed() or not _on_origin(page, origin):
        return False
    if await _is_error_page(page) or await _has_login_form(page):
        return False
    return True


async def _open_landing(page, origin: str) -> None:
    await page.goto(
        _landing(origin), wait_until="domcontentloaded", timeout=_nav_timeout_ms()
    )


async def _probe_session(page, origin: str) -> bool:
    if not await _can_probe(page, origin):
        return False
    try:
        info = await page.evaluate(_SESSION_JS)
    except Exception as e:
        log.debug("probe sessão falhou url=%s: %s", page.url, e)
        return False
    if info and info.get("ok"):
        log.info("sessão TOA ok keys=%s", info.get("keys"))
        return True
    log.debug("sessão ainda inválida url=%s info=%s", page.url, info)
    return False


async def _any_session_ok(ctx, origin: str) -> bool:
    for p in list(ctx.pages):
        if await _probe_session(p, origin):
            return True
    return False


def _page_closed(page) -> bool:
    try:
        return page is None or page.is_closed()
    except Exception:
        return True


def _frame_count(page) -> int:
    try:
        return len(page.frames)
    except Exception:
        return 0


async def _locate(page, selector: str, *, quiet: bool = False):
    if _page_closed(page):
        return None
    try:
        loc = page.locator(selector)
        if await loc.count() > 0:
            if not quiet:
                log.info("seletor encontrado selector=%s frame=main url=%s", selector, page.url)
            return loc.first, "main"
        for fr in page.frames:
            try:
                if fr == page.main_frame:
                    continue
                loc = fr.locator(selector)
                if await loc.count() > 0:
                    label = fr.url or "iframe"
                    if not quiet:
                        log.info("seletor encontrado selector=%s frame=%s", selector, label)
                    return loc.first, label
            except Exception:
                continue
    except Exception as e:
        log.warning("busca seletor falhou selector=%s: %s", selector, e)
        return None
    return None


async def _is_restore_login(page) -> bool:
    return await _locate(page, _SEL_RESTORE, quiet=True) is not None


async def _is_full_login(page) -> bool:
    if await _is_restore_login(page):
        return False
    user = await _locate(page, _SEL_USERNAME, quiet=True)
    password = await _locate(page, _SEL_PASSWORD, quiet=True)
    return user is not None and password is not None


async def _fill_field(loc, value: str, selector: str, *, submit: bool) -> None:
    await loc.wait_for(state="attached", timeout=_FIELD_WAIT_MS)
    try:
        await loc.wait_for(state="visible", timeout=5000)
    except Exception:
        log.info("campo attached mas não visible selector=%s; fill mesmo assim", selector)
    log.info("digitando campo selector=%s", selector)
    await loc.click()
    await loc.fill("")
    await loc.press_sequentially(value, delay=50)
    await loc.evaluate(
        """el => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }"""
    )
    if submit:
        await loc.press("Enter")


async def _try_auto_login(page) -> bool:
    password = env("TOA_PASS")
    if not password:
        log.warning("TOA_PASS não definido; auto-login desativado")
        return False
    if _page_closed(page):
        log.warning("auto-login abortado: aba fechada")
        return False
    try:
        restore = await _locate(page, _SEL_RESTORE)
        if restore:
            loc, frame = restore
            log.info("detectado restore selector=%s frame=%s", _SEL_RESTORE, frame)
            log.info("preenchendo senha restore")
            await _fill_field(loc, password, _SEL_RESTORE, submit=True)
            log.info("login restore (só senha)")
            try:
                await page.wait_for_load_state(
                    "domcontentloaded", timeout=_nav_timeout_ms()
                )
            except Exception:
                pass
            return True
        user_hit = await _locate(page, _SEL_USERNAME)
        pass_hit = await _locate(page, _SEL_PASSWORD)
        if user_hit and pass_hit:
            user = env("TOA_USER")
            if not user:
                log.warning("login completo visível, mas TOA_USER não está definido")
                return False
            user_loc, user_frame = user_hit
            pass_loc, pass_frame = pass_hit
            log.info(
                "detectado login completo user_frame=%s pass_frame=%s",
                user_frame,
                pass_frame,
            )
            log.info("preenchendo login completo")
            await _fill_field(user_loc, user, _SEL_USERNAME, submit=False)
            await _fill_field(pass_loc, password, _SEL_PASSWORD, submit=True)
            log.info("login completo")
            try:
                await page.wait_for_load_state(
                    "domcontentloaded", timeout=_nav_timeout_ms()
                )
            except Exception:
                pass
            return True
    except Exception as e:
        log.warning("auto-login falhou: %s", e)
        return False
    log.info(
        "auto-login: nenhum formulário url=%s frames=%s restore=nao full=nao",
        page.url,
        _frame_count(page),
    )
    return False


async def ensure_logged_in(page, origin: str) -> None:
    ctx = page.context
    user = env("TOA_USER") or ""
    has_pass = bool(env("TOA_PASS"))
    log.info(
        "ensure_logged_in url=%s closed=%s frames=%s TOA_USER=%s TOA_PASS=%s",
        getattr(page, "url", ""),
        _page_closed(page),
        _frame_count(page),
        user or "(vazio)",
        "sim" if has_pass else "nao",
    )
    # Uma sessão só de cookie (sem reenviar login) deixa o TOA "logado" mas
    # não inicializa o módulo de chat — o WebSocket nativo nunca é criado e
    # o bot fica preso esperando por até 120s. Limpar os cookies e recarregar
    # a landing força sempre um login de verdade (completo ou "restore"),
    # que é o que efetivamente dispara o bootstrap do chat.
    await ctx.clear_cookies()
    log.info("cookies limpos; abrindo landing origin=%s", origin)
    await _open_landing(page, origin)

    last_attempt = 0.0
    if await _try_auto_login(page):
        last_attempt = time.monotonic()

    recovered = False
    for i in range(180):
        if _page_closed(page):
            log.warning("aba fechada durante espera de login")
            raise SessionNotReady("aba do TOA fechou")
        if await _is_error_page(page) and not recovered:
            log.warning("TOA mostrou solicitação incorreta; reabrindo a raiz do site")
            await _open_landing(page, origin)
            recovered = True
            last_attempt = 0.0
        if await _any_session_ok(ctx, origin):
            log.info("sessão TOA pronta")
            return
        now = time.monotonic()
        if now - last_attempt >= _LOGIN_RETRY_SEC:
            log.info(
                "tentando auto-login novamente url=%s frames=%s",
                page.url,
                _frame_count(page),
            )
            await _try_auto_login(page)
            last_attempt = now
        if i % 5 == 0:
            restore = await _is_restore_login(page)
            full = await _is_full_login(page)
            log.info(
                "aguardando login url=%s closed=%s restore=%s full=%s frames=%s",
                page.url,
                _page_closed(page),
                restore,
                full,
                _frame_count(page),
            )
        await asyncio.sleep(2)
    raise SessionNotReady("sessão TOA não ficou pronta em 6 minutos (login/SSO)")


def _headless() -> bool:
    return (env("TOA_HEADLESS") or "").strip().lower() in ("1", "true", "yes", "on")


_LOCK_MARKERS = (
    "singleton",
    "lock file",
    "lockfile",
    "profile is already in use",
    "user data directory is already in use",
    "processsingleton",
)


def _looks_like_profile_lock(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _LOCK_MARKERS)


def _clear_profile_locks(profile: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie"):
        p = profile / name
        try:
            if p.is_file() or p.is_symlink():
                p.unlink()
                log.info("removeu lock do perfil %s", name)
        except OSError as e:
            log.warning("não removeu %s: %s", name, e)


async def _launch_persistent(pw, kwargs):
    try:
        ctx = await pw.chromium.launch_persistent_context(str(PROFILE), channel="chrome", **kwargs)
        log.info("Chromium via Chrome instalado headless=%s", kwargs.get("headless"))
        return ctx
    except Exception as e:
        if _looks_like_profile_lock(e):
            raise
        ctx = await pw.chromium.launch_persistent_context(str(PROFILE), **kwargs)
        log.info("Chromium do Playwright headless=%s", kwargs.get("headless"))
        return ctx


async def launch_browser():
    PROFILE.mkdir(parents=True, exist_ok=True)
    pw = await async_playwright().start()
    headless = _headless()
    kwargs = {
        "headless": headless,
        "locale": "pt-BR",
        "args": ["--disable-dev-shm-usage"],
    }
    proxy = env("TOA_PROXY")
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    try:
        ctx = await _launch_persistent(pw, kwargs)
    except Exception as e:
        if not _looks_like_profile_lock(e):
            raise
        log.warning("perfil Chrome com lock; removendo SingletonLock/SingletonCookie e tentando de novo")
        _clear_profile_locks(PROFILE)
        ctx = await _launch_persistent(pw, kwargs)
    ctx.set_default_timeout(_nav_timeout_ms())
    ctx.set_default_navigation_timeout(_nav_timeout_ms())
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return pw, ctx, page


async def close_browser(pw, ctx) -> None:
    if ctx is not None:
        try:
            await ctx.close()
        except Exception:
            pass
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass
