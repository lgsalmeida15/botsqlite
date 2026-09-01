# -*- coding: utf-8 -*-
"""Cria apoio via API interna. 200/SUCESSO encerra; falha transfere."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from python.config import env

SLUG = "criar_apoio"
LABEL = "Criar apoio"
FIELDS = [
    {"key": "atividade", "type": "int", "prompt": "Qual ID da atividade?"},
    {"key": "login_apoio", "type": "text", "prompt": "Qual Login do tec. Apoio?"},
]
SUCCESS_MESSAGE = "Apoio criado com sucesso! Para novas solicitações inicie um novo chat."
ERROR_MESSAGE = "Erro ao criar apoio automático, direcionando para um atendente."
BUDGET_SEC = 180.0
POLL_SEC = 30.0
HTTP_TIMEOUT = 15.0
MAX_CONFLICT = 3
MAX_STATUS_GETS = 4
DETAIL_MAX = 800
_SECRET_HEADER_KEYS = frozenset({"x-api-key", "api-key", "apikey", "api_key"})


def _ok(detail: str) -> dict:
    return {
        "ok": True,
        "message": SUCCESS_MESSAGE,
        "action": "reply_and_close",
        "detail": detail[:200],
    }


def _fail(detail: str) -> dict:
    return {
        "ok": False,
        "message": ERROR_MESSAGE,
        "action": "ask_and_transfer",
        "detail": str(detail or "")[:DETAIL_MAX],
    }


def _scrub_body(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = str(k).strip().lower().replace("_", "-")
            if key in _SECRET_HEADER_KEYS or "api-key" in key:
                continue
            out[k] = _scrub_body(v)
        return out
    if isinstance(obj, list):
        return [_scrub_body(x) for x in obj]
    return obj


def _detail_api(code, body) -> str:
    if code is None and not body:
        return "http_error"
    prefix = f"http {code}" if code is not None else "http_error"
    if not body:
        return prefix
    try:
        snippet = json.dumps(_scrub_body(body), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        snippet = ""
    if not snippet or snippet in ("{}", "[]", "null"):
        return prefix
    return f"{prefix} {snippet}"[:DETAIL_MAX]


def _as_body(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _api_status(body: dict) -> str:
    return str((body or {}).get("status") or "").strip().upper()


def parse_started_at(raw) -> datetime:
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc)
    s = str(raw or "").strip()
    if not s:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _remaining(started: datetime) -> float:
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    return BUDGET_SEC - elapsed


def _http_timeout(started: datetime) -> float:
    left = _remaining(started)
    if left <= 0:
        return 0.0
    return min(HTTP_TIMEOUT, left)


def _read_http(resp) -> tuple[int, dict]:
    status = int(getattr(resp, "status", 0) or 0)
    try:
        raw = resp.read()
    except Exception:
        raw = b""
    return status, _as_body(raw)


def _post_json(url: str, payload: dict, headers: dict, timeout: float = HTTP_TIMEOUT) -> tuple[int | None, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return _read_http(resp)
    except HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        return int(e.code or 0), _as_body(raw)
    except (URLError, TimeoutError, OSError, ValueError):
        return None, {}


def _get_json(url: str, headers: dict, timeout: float = HTTP_TIMEOUT) -> tuple[int | None, dict]:
    req = Request(url, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return _read_http(resp)
    except HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        return int(e.code or 0), _as_body(raw)
    except (URLError, TimeoutError, OSError, ValueError):
        return None, {}


def _headers(key: str, uid: str, *, post: bool) -> dict:
    h = {
        "accept": "application/json",
        "x-uuid": uid,
    }
    if post:
        h["Content-Type"] = "application/json"
        h["x-modo"] = "sync"
    if key:
        h["x-api-key"] = key
    return h


async def _persist(ctx: dict, uid: str) -> None:
    persist = ctx.get("persist_job")
    if not callable(persist):
        return
    out = persist(uid)
    if asyncio.iscoroutine(out):
        await out


async def _call_post(post, url: str, payload: dict, headers: dict, timeout: float):
    try:
        result = post(url, payload, headers, timeout)
    except TypeError:
        result = post(url, payload, headers)
    if asyncio.iscoroutine(result):
        return await result
    return result


async def _call_get(get, url: str, headers: dict, timeout: float):
    try:
        result = get(url, headers, timeout)
    except TypeError:
        result = get(url, headers)
    if asyncio.iscoroutine(result):
        return await result
    return result


def _unpack(raw) -> tuple[int | None, dict]:
    if not isinstance(raw, tuple) or len(raw) < 2:
        return None, {}
    code, body = raw[0], raw[1]
    if code is not None:
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
    return code, _as_body(body)


async def _poll_status(get, url: str, key: str, uid: str, started: datetime) -> dict:
    for attempt in range(MAX_STATUS_GETS):
        left = _remaining(started)
        if left <= 0:
            return _fail("timeout")
        timeout = _http_timeout(started)
        if timeout <= 0:
            return _fail("timeout")
        headers = _headers(key, uid, post=False)
        if get is _get_json:
            code, body = await asyncio.to_thread(_get_json, url, headers, timeout)
        else:
            code, body = _unpack(await _call_get(get, url, headers, timeout))
        st = _api_status(body)
        if st == "SUCESSO":
            return _ok("http get sucesso")
        if st == "ERRO":
            return _fail(_detail_api(code, body))
        if st == "PENDENTE" or (code == 200 and not st):
            if attempt + 1 >= MAX_STATUS_GETS:
                return _fail("poll_exhausted")
            await asyncio.sleep(POLL_SEC)
            continue
        if code is None:
            return _fail(_detail_api(None, body))
        return _fail(_detail_api(code, body))
    return _fail("poll_exhausted")


async def run(ctx: dict, http_post=None, http_get=None) -> dict:
    answers = ctx.get("answers") if isinstance(ctx.get("answers"), dict) else {}
    raw_id = str(answers.get("atividade") or "").strip()
    tecnico = str(answers.get("login_apoio") or "").strip()
    try:
        tarefa_id = int(raw_id)
    except ValueError:
        return _fail("bad_answers")
    post_url = (env("APOIO_API_URL") or "").strip()
    status_url = (env("APOIO_STATUS_URL") or "").strip()
    key = (env("APOIO_API_KEY") or "").strip()
    if not post_url or not status_url:
        return _fail("config")
    started = parse_started_at(ctx.get("started_at"))
    resume_uid = str(ctx.get("resume_uuid") or "").strip()
    uid = resume_uid or str(uuid.uuid4())
    await _persist(ctx, uid)
    if _remaining(started) <= 0:
        return _fail("timeout")

    post = http_post or _post_json
    get = http_get or _get_json
    payload = {"tarefa_id": tarefa_id, "tecnico": tecnico}

    if not resume_uid:
        posted = False
        last_fail = ""
        for _ in range(MAX_CONFLICT):
            timeout = _http_timeout(started)
            if timeout <= 0:
                return _fail("timeout")
            headers = _headers(key, uid, post=True)
            if post is _post_json:
                code, body = await asyncio.to_thread(_post_json, post_url, payload, headers, timeout)
            else:
                code, body = _unpack(await _call_post(post, post_url, payload, headers, timeout))
            posted = True
            if code == 409:
                last_fail = _detail_api(code, body)
                uid = str(uuid.uuid4())
                await _persist(ctx, uid)
                continue
            st = _api_status(body)
            if st == "SUCESSO" or (code == 200 and st == "SUCESSO"):
                return _ok("http 200")
            if st == "ERRO":
                return _fail(_detail_api(code, body))
            if st == "PENDENTE" or (code == 200 and not st):
                return await _poll_status(get, status_url, key, uid, started)
            if code is None:
                return _fail(_detail_api(None, body))
            return _fail(_detail_api(code, body))
        if posted:
            return _fail(last_fail or "http 409")
        return _fail("http_error")

    return await _poll_status(get, status_url, key, uid, started)
