# -*- coding: utf-8 -*-
"""Remaneja rota de tecnicos via API interna. 200/SUCESSO encerra; falha transfere.

Move uma atividade entre tecnicos ou para o proprio tecnico mudando a posicao:

1. `fields_for(answers)` pede id da atividade, login do tecnico e a posicao. So
   quando a posicao e "Apos outra atividade" a pergunta `apos_tarefa_id` entra.
2. `normalize_answer(key, text, answers)` valida os ids (inteiros positivos) e
   recusa a atividade como anterior de si mesma.
3. `run(ctx)` monta o payload (sem `posicao`/`apos_tarefa_id` na posicao "proxima
   da rota"; com `"posicao": "apos_tarefa"` e `apos_tarefa_id` na posicao "apos
   outra atividade") e posta na API interna com idempotencia por `x-uuid`, igual
   ao `criar_apoio`.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from python.config import env, is_internal_api
from python.scripts.criar_apoio import parse_started_at

SLUG = "remanejamento_rota"
LABEL = "Remanejar rota de tecnicos"
POS_APOS = "Após outra atividade"
# Valor mandado no corpo quando a posicao e "Apos outra atividade".
POS_API_APOS = "apos_tarefa"
# Espelho estatico das perguntas fixas (usado pelo `manifest.json`/aba Menus).
# A pergunta `apos_tarefa_id` e condicional e vem de `fields_for`.
FIELDS = [
    {
        "key": "tarefa_id",
        "type": "int",
        "prompt": "Informe Id da atividade a ser remanejada",
        "label": "Atividade",
    },
    {
        "key": "tecnico",
        "type": "text",
        "prompt": "Informe Login do técnico a receber atividade",
        "label": "Técnico",
    },
    {
        "key": "posicao",
        "type": "choice",
        "prompt": "Posição da atividade:",
        "label": "Posição",
        "choices": ["Próxima da rota", "Após outra atividade"],
    },
]
SUCCESS_MESSAGE = "Remanejamento efetuado com sucesso! Para mais solicitações inicie um novo chat."
ERROR_MESSAGE = "Erro ao remanejar a atividade automaticamente, direcionando para um atendente."
BUDGET_SEC = 180.0
POLL_SEC = 30.0
HTTP_TIMEOUT = 60.0
RETRY_SLEEP = 3.0
MAX_CONFLICT = 3
MAX_STATUS_GETS = 4
DETAIL_MAX = 800
_SECRET_HEADER_KEYS = frozenset({"x-api-key", "api-key", "apikey", "api_key"})
# API interna nao usa proxy do ambiente (proxy local do VS Code devolvia 503).
_OPENER_DIRETO = build_opener(ProxyHandler({}))


def _open_http(req: Request, timeout: float):
    if is_internal_api(req.full_url):
        return _OPENER_DIRETO.open(req, timeout=timeout)
    return urlopen(req, timeout=timeout)


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


def _join_tries(tries: list, suffix: str = "") -> str:
    """Resposta de cada tentativa de POST, na ordem, cortada no limite do detail."""
    out = "; ".join(tries)
    if suffix:
        out = f"{out} | {suffix}" if out else suffix
    return out[:DETAIL_MAX] or "http_error"


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
        with _open_http(req, timeout) as resp:
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
        with _open_http(req, timeout) as resp:
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


# --------------------------------------------------------------------------- #
# Perguntas do wizard (`fields_for`)
# --------------------------------------------------------------------------- #
def fields_for(answers: dict) -> list[dict]:
    """Perguntas do momento: id da atividade, tecnico, posicao e (se preciso) o id anterior.

    A pergunta `apos_tarefa_id` so entra quando a posicao e "Apos outra atividade" —
    depois da resposta de `posicao`, o que mantem os indices guardados pelo engine.
    """
    respostas = answers if isinstance(answers, dict) else {}
    fields = [dict(f) for f in FIELDS]
    if str(respostas.get("posicao") or "").strip() == POS_APOS:
        fields.append({
            "key": "apos_tarefa_id",
            "type": "int",
            "prompt": "Informe Id da atividade anterior do técnico",
            "label": "Após a atividade",
        })
    return fields


# --------------------------------------------------------------------------- #
# Validacao das respostas (`normalize_answer`)
# --------------------------------------------------------------------------- #
def _aceita(value: str) -> dict:
    return {"value": value, "state": {}, "retry": False, "message": ""}


def _recusa(message: str) -> dict:
    return {"value": None, "state": {}, "retry": True, "message": message}


def _positivo(text: str) -> str | None:
    raw = str(text or "").strip()
    if not raw.isdigit():
        return None
    if int(raw) <= 0:
        return None
    return str(int(raw))


def _posicoes() -> list[str]:
    return [str(x).strip() for x in (FIELDS[2].get("choices") or []) if str(x).strip()]


def normalize_answer(key: str, text: str, answers: dict) -> dict:
    respostas = answers if isinstance(answers, dict) else {}
    if key == "tarefa_id":
        value = _positivo(text)
        if value is None:
            return _recusa("Id da atividade inválido. Informe apenas o número.")
        return _aceita(value)
    if key == "apos_tarefa_id":
        value = _positivo(text)
        if value is None:
            return _recusa("Id da atividade anterior inválido. Informe apenas o número.")
        if value == str(respostas.get("tarefa_id") or "").strip():
            return _recusa("A atividade não pode ser a anterior de si mesma. Informe outro Id.")
        return _aceita(value)
    if key == "posicao":
        escolhas = _posicoes()
        raw = str(text or "").strip()
        if not raw.isdigit() or not (1 <= int(raw) <= len(escolhas)):
            return _recusa(f"Opção inválida. Escolha de 1 a {len(escolhas)}.")
        return _aceita(escolhas[int(raw) - 1])
    value = str(text or "").strip()
    if not value:
        return _recusa("Informe um valor válido.")
    return _aceita(value)


def _payload(answers: dict) -> dict | None:
    respostas = answers if isinstance(answers, dict) else {}
    tarefa = _positivo(respostas.get("tarefa_id"))
    tecnico = str(respostas.get("tecnico") or "").strip()
    if tarefa is None or not tecnico:
        return None
    payload = {"tarefa_id": int(tarefa), "tecnico": tecnico}
    if str(respostas.get("posicao") or "").strip() == POS_APOS:
        apos = _positivo(respostas.get("apos_tarefa_id"))
        if apos is None or apos == tarefa:
            return None
        payload["posicao"] = POS_API_APOS
        payload["apos_tarefa_id"] = int(apos)
    return payload


async def run(ctx: dict, http_post=None, http_get=None) -> dict:
    answers = ctx.get("answers") if isinstance(ctx.get("answers"), dict) else {}
    payload = _payload(answers)
    if payload is None:
        return _fail("bad_answers")
    post_url = (env("REMANEJAMENTO_API_URL") or "").strip()
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

    if not resume_uid:
        tries: list = []
        for attempt in range(1, MAX_CONFLICT + 1):
            timeout = _http_timeout(started)
            if timeout <= 0:
                return _fail(_join_tries(tries, "timeout"))
            headers = _headers(key, uid, post=True)
            if post is _post_json:
                code, body = await asyncio.to_thread(_post_json, post_url, payload, headers, timeout)
            else:
                code, body = _unpack(await _call_post(post, post_url, payload, headers, timeout))
            tries.append(f"#{attempt} {_detail_api(code, body)}")
            if code == 409:
                # UUID ja utilizado: gera outro e repete imediatamente.
                uid = str(uuid.uuid4())
                await _persist(ctx, uid)
                continue
            st = _api_status(body)
            if st == "SUCESSO":
                return _ok("http 200")
            if st == "ERRO":
                return _fail(_join_tries(tries))
            if st == "PENDENTE" or (code == 200 and not st):
                return await _poll_status(get, status_url, key, uid, started)
            transient = code is None or (isinstance(code, int) and code >= 500)
            if not transient:
                return _fail(_join_tries(tries))
            if attempt < MAX_CONFLICT:
                if _remaining(started) <= RETRY_SLEEP:
                    return _fail(_join_tries(tries, "timeout"))
                await asyncio.sleep(RETRY_SLEEP)
        return _fail(_join_tries(tries))

    return await _poll_status(get, status_url, key, uid, started)



