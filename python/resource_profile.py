# -*- coding: utf-8 -*-
"""Perfil TOA (Gestor) via resourcemanagement, na sessão Playwright."""
from __future__ import annotations

import asyncio
import logging
import time
import unicodedata
from typing import Any, Awaitable, Callable

log = logging.getLogger("helptoa.profile")

DENIED_MESSAGE = "Usuário não habilitado para esta solicitação; por gentileza, solicite ao seu gestor que efetue a ação."
CACHE_TTL_SEC = 600.0
FETCH_TIMEOUT_SEC = 12.0
LOGIN_TITLE = "efetuar login"
TIPO_TITLE = "tipo de usuario"

_cache: dict[str, dict] = {}
_lock = asyncio.Lock()

FetchPage = Callable[[Any, str], Awaitable[dict]]

_FETCH_JS = """async (login) => {
  function csrfToken() {
    if (window.CSRFSecureToken) return String(window.CSRFSecureToken);
    try {
      const ls = localStorage.getItem('CSRFSecureToken');
      if (ls) return ls;
    } catch (e) {}
    return '';
  }
  function foldTitle(s) {
    return String(s || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
  }
  function loginTipo(m) {
    if (m && m.static && m.static.is_group) return {login: '', tipo: ''};
    const dyn = (m && m.dynamic) || [];
    let user = '';
    let tipo = '';
    for (const item of dyn) {
      if (!item || typeof item !== 'object') continue;
      const title = foldTitle(item.title);
      const val = item.value == null ? '' : String(item.value).trim();
      if (title === 'efetuar login') user = val;
      else if (title === 'tipo de usuario') tipo = val;
    }
    return {login: user, tipo};
  }
  const csrf = csrfToken();
  if (!csrf) return {ok: false, error: 'no_csrf', rows: []};
  const body = new URLSearchParams();
  body.set('data[filter][organization_id]', '');
  body.set('data[filter][organization_unit]', '');
  body.set('data[filter][types]', '');
  body.set('data[filter][user_types]', '');
  body.set('data[filter][collaboration_groups]', '');
  body.set('data[filter][statuses]', '1');
  body.set('data[filter][search]', String(login || ''));
  body.set('data[filter][last_login]', '');
  body.set('data[hash]', '');
  body.set('data[selectFrom]', '0');
  body.set('data[selectCount]', '45');
  body.set('data[includeActions]', 'false');
  const headers = {
    'Content-Type': 'application/x-www-form-urlencoded',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'X-Requested-With': 'XMLHttpRequest',
    'X-OA': '2',
    'X-PLATFORM': '1',
    'X-OFS-CSRF-SECURE': csrf,
  };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 10000);
  try {
    const r = await fetch(location.origin + '/?m=resourcemanagement&a=get', {
      method: 'POST',
      credentials: 'include',
      headers,
      body: body.toString(),
      signal: ctrl.signal,
    });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) {
      return {ok: false, status: r.status, error: 'bad_json', rows: []};
    }
    if (json && json.errorNo != null) {
      return {ok: false, status: r.status, error: 'errorNo', rows: []};
    }
    if (r.status !== 200) {
      return {ok: false, status: r.status, error: 'http', rows: []};
    }
    const members = (json && json.data && json.data.members) || [];
    const rows = [];
    for (const m of members) {
      const p = loginTipo(m);
      if (p.login) rows.push(p);
    }
    return {ok: true, status: r.status, count: rows.length, rows};
  } catch (e) {
    const aborted = e && e.name === 'AbortError';
    return {ok: false, status: 0, error: aborted ? 'timeout' : String(e), rows: []};
  } finally {
    clearTimeout(timer);
  }
}"""


def reset_cache() -> None:
    global _cache
    _cache = {}


def _fold_title(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).casefold()


def _login_tipo(member: dict) -> tuple[str, str]:
    if not isinstance(member, dict):
        return "", ""
    st = member.get("static")
    if isinstance(st, dict) and st.get("is_group"):
        return "", ""
    dyn = member.get("dynamic")
    if not isinstance(dyn, list):
        return "", ""
    login, tipo = "", ""
    for item in dyn:
        if not isinstance(item, dict):
            continue
        title = _fold_title(str(item.get("title") or ""))
        raw = item.get("value")
        val = "" if raw is None else str(raw).strip()
        if title == LOGIN_TITLE:
            login = val
        elif title == TIPO_TITLE:
            tipo = val
    return login, tipo


def extract_rows(obj) -> list[dict]:
    rows: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        mem = node.get("members")
        if isinstance(mem, list):
            for m in mem:
                if not isinstance(m, dict):
                    continue
                login, tipo = _login_tipo(m)
                if login:
                    rows.append({"login": login, "tipo": tipo})
        for k, v in node.items():
            if k == "members":
                continue
            walk(v)

    walk(obj)
    return rows


def is_gestor(tipo: str) -> bool:
    return "gestor" in (tipo or "").casefold()


def _cache_get(key: str) -> dict | None:
    hit = _cache.get(key)
    if not hit:
        return None
    if float(hit.get("exp") or 0) <= time.monotonic():
        _cache.pop(key, None)
        return None
    return hit


def _cache_put(key: str, *, found: bool, tipo: str) -> None:
    _cache[key] = {
        "found": found,
        "tipo": tipo,
        "exp": time.monotonic() + CACHE_TTL_SEC,
    }


def _pick_tipo(rows: list, login: str) -> str | None:
    want = login.casefold()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        got = str(row.get("login") or "").strip()
        if got.casefold() == want:
            return str(row.get("tipo") or "").strip()
    return None


async def _fetch_via_page(page, login: str) -> dict:
    if page is None or not hasattr(page, "evaluate"):
        return {"ok": False, "error": "no_page", "rows": []}
    try:
        raw = await asyncio.wait_for(
            page.evaluate(_FETCH_JS, str(login)),
            timeout=FETCH_TIMEOUT_SEC + 2.0,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timeout", "rows": []}
    except Exception as e:
        log.warning("resource_profile fetch falhou: %s", e)
        return {"ok": False, "error": "eval", "rows": []}
    return raw if isinstance(raw, dict) else {"ok": False, "error": "bad_result", "rows": []}


def _rows_from_raw(raw: dict) -> list:
    rows = raw.get("rows")
    if isinstance(rows, list):
        return rows
    return extract_rows(raw.get("data") or raw)


async def _fetch_tipo(page, login: str, fetch_page: FetchPage | None) -> tuple[str, str | None]:
    fetch = fetch_page or _fetch_via_page
    raw = await fetch(page, login)
    if not isinstance(raw, dict):
        return "api_error", None
    err = str(raw.get("error") or "")
    if err == "timeout":
        return "api_timeout", None
    if err in ("no_csrf", "errorNo", "no_page", "eval", "bad_json", "http", "bad_result"):
        return "api_error", None
    if not raw.get("ok"):
        return "api_timeout" if err == "timeout" else "api_error", None
    tipo = _pick_tipo(_rows_from_raw(raw), login)
    if tipo is None:
        return "not_found", None
    return "ok", tipo


async def lookup(page, login: str, *, fetch_page: FetchPage | None = None) -> dict:
    login = str(login or "").strip()
    if not login:
        return {"allowed": False, "login": "", "perfil": "", "reason": "no_login"}
    key = login.casefold()
    hit = _cache_get(key)
    if hit is not None:
        return _result(login, found=bool(hit.get("found")), tipo=str(hit.get("tipo") or ""))
    async with _lock:
        hit = _cache_get(key)
        if hit is not None:
            return _result(login, found=bool(hit.get("found")), tipo=str(hit.get("tipo") or ""))
        status, tipo = await _fetch_tipo(page, login, fetch_page)
        if status == "ok":
            _cache_put(key, found=True, tipo=tipo or "")
            return _result(login, found=True, tipo=tipo or "")
        if status == "not_found":
            _cache_put(key, found=False, tipo="")
            return _result(login, found=False, tipo="")
        return {"allowed": False, "login": login, "perfil": "", "reason": status}


def _result(login: str, *, found: bool, tipo: str) -> dict:
    if not found:
        return {"allowed": False, "login": login, "perfil": "", "reason": "not_found"}
    if not is_gestor(tipo):
        return {"allowed": False, "login": login, "perfil": tipo, "reason": "not_gestor"}
    return {"allowed": True, "login": login, "perfil": tipo, "reason": "ok"}


def deny_detail(slug: str, login: str, result: dict) -> str:
    slug = str(slug or "")
    login = str(login or result.get("login") or "?") or "?"
    perfil = str(result.get("perfil") or "").strip()
    reason = str(result.get("reason") or "not_found")
    if perfil:
        text = f"slug={slug} login={login} perfil={perfil}"
    else:
        text = f"slug={slug} login={login} {reason}"
    return text[:800]
