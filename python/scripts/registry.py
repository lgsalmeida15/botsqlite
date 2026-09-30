# -*- coding: utf-8 -*-
"""Descobre scripts na pasta e executa com contrato único."""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
from pathlib import Path

log = logging.getLogger("helptoa.scripts")

ALLOWED_ACTIONS = frozenset({
    "reply_and_close",
    "ask_and_transfer",
    "transfer_only",
    "retry_form",
    "stay",
})
FALLBACK_MESSAGE = "Não foi possível concluir a solicitação. Um coordenador vai te atender."
MANIFEST_PATH = Path(__file__).with_name("manifest.json")
RUN_TIMEOUT_SEC = 180.0


def _load_module(slug: str):
    if not slug or not slug.replace("_", "").isalnum() or slug.startswith("_"):
        return None
    try:
        return importlib.import_module(f"python.scripts.{slug}")
    except Exception:
        log.warning("script %s não carregou", slug, exc_info=True)
        return None


def _spec_field(f: dict) -> dict:
    item = {
        "key": str(f.get("key") or ""),
        "type": str(f.get("type") or "text"),
        "prompt": str(f.get("prompt") or ""),
    }
    if item["type"] == "choice":
        item["choices"] = [
            str(x).strip() for x in (f.get("choices") or []) if str(x).strip()
        ]
    return item


# Chaves extras que um campo do wizard pode declarar e que NAO entram no
# manifest (que continua so com key/type/prompt/choices):
# - `label`: texto curto usado no resumo da confirmacao (no lugar do prompt);
# - `confirm`: False omite o campo do resumo (campo de busca interno).
FIELD_FLAGS = ("label", "confirm")


def _runtime_field(f: dict) -> dict:
    """Campo do wizard em tempo de execucao (mantem `label` e `confirm`)."""
    item = _spec_field(f)
    for flag in FIELD_FLAGS:
        if flag in f:
            item[flag] = f[flag]
    return item


def _normalized_fields(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [_runtime_field(f) for f in raw if isinstance(f, dict) and f.get("key")]


def list_specs() -> list[dict]:
    specs = []
    folder = Path(__file__).parent
    for path in sorted(folder.glob("*.py")):
        name = path.stem
        if name in ("registry",) or name.startswith("_"):
            continue
        mod = _load_module(name)
        if mod is None:
            continue
        slug = str(getattr(mod, "SLUG", "") or "")
        fields = getattr(mod, "FIELDS", None)
        if slug != name or not isinstance(fields, list) or not fields:
            continue
        if not callable(getattr(mod, "run", None)):
            continue
        specs.append({
            "slug": slug,
            "label": str(getattr(mod, "LABEL", slug) or slug),
            "fields": [_spec_field(f) for f in fields if isinstance(f, dict) and f.get("key")],
        })
    return specs


def get_spec(slug: str) -> dict | None:
    slug = str(slug or "")
    for spec in list_specs():
        if spec["slug"] == slug:
            return spec
    return None


def fields_for(slug: str, answers: dict | None = None) -> list[dict]:
    """Campos que o wizard deve perguntar agora.

    Scripts sem o hook `fields_for(answers)` continuam com a lista estatica
    `FIELDS` (comportamento antigo, campo a campo). Com o hook, a lista pode
    depender do que ja foi coletado: e assim que `criar_acesso_hub` pergunta os
    tecnicos 1..N e troca o texto livre do hub por uma lista numerada de
    sugestoes quando o nome nao e localizado.
    """
    mod = _load_module(str(slug or ""))
    if mod is None:
        return []
    raw = None
    fn = getattr(mod, "fields_for", None)
    if callable(fn):
        try:
            raw = fn(answers if isinstance(answers, dict) else {})
        except Exception:
            log.warning("fields_for de %s falhou", slug, exc_info=True)
            raw = None
    if not isinstance(raw, list):
        raw = getattr(mod, "FIELDS", None)
    return _normalized_fields(raw)


def normalize_answer(
    slug: str, field: dict, text: str, answers: dict | None = None
) -> dict | None:
    """Valida/normaliza a resposta pelo hook opcional do modulo.

    Devolve `None` quando o modulo nao define `normalize_answer`: nesse caso o
    engine usa a validacao padrao (`text`/`int`/`choice`), como sempre. Contrato
    do retorno:

    - `value`: valor normalizado (str) ou `None` quando a resposta nao serve;
    - `state`: chaves internas (prefixo `_`) a persistir junto das respostas,
      valido **inclusive na recusa** (e como a lista de sugestoes viaja);
    - `retry`: `True` conta tentativa; `False` apenas re-pergunta (sugestao);
    - `message`: aviso enviado antes de re-perguntar.
    """
    mod = _load_module(str(slug or ""))
    fn = getattr(mod, "normalize_answer", None) if mod else None
    if not callable(fn):
        return None
    key = str((field or {}).get("key") or "")
    try:
        raw = fn(key, str(text or ""), answers if isinstance(answers, dict) else {})
    except Exception:
        log.warning("normalize_answer de %s falhou", slug, exc_info=True)
        return {"value": None, "state": {}, "retry": True, "message": ""}
    data = raw if isinstance(raw, dict) else {}
    value = data.get("value")
    value = str(value).strip() if value is not None else ""
    if not value:
        value = None
    state: dict = {}
    if isinstance(data.get("state"), dict):
        for k, v in data["state"].items():
            name = str(k or "")
            if name:
                state[name] = v
    retry = data.get("retry")
    return {
        "value": value,
        "state": state,
        "retry": True if retry is None else bool(retry),
        "message": str(data.get("message") or "")[:800],
    }


def confirm_note(slug: str) -> str:
    """Aviso extra do script no resumo da confirmacao (`CONFIRM_NOTE`)."""
    mod = _load_module(str(slug or ""))
    return str(getattr(mod, "CONFIRM_NOTE", "") or "") if mod else ""


def manifest_payload() -> dict:
    return {"scripts": list_specs()}


def load_manifest_file() -> dict:
    if not MANIFEST_PATH.is_file():
        return {"scripts": []}
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"scripts": []}
    if not isinstance(data, dict):
        return {"scripts": []}
    scripts = data.get("scripts")
    if not isinstance(scripts, list):
        return {"scripts": []}
    return {"scripts": scripts}


def normalize_result(raw) -> dict:
    if not isinstance(raw, dict):
        return {
            "ok": False,
            "message": FALLBACK_MESSAGE,
            "action": "ask_and_transfer",
            "detail": "bad_result",
        }
    action = str(raw.get("action") or "stay")
    if action not in ALLOWED_ACTIONS:
        action = "stay"
    message = str(raw.get("message") or "")
    detail = str(raw.get("detail") or "")[:800]
    ok = bool(raw.get("ok"))
    return {"ok": ok, "message": message, "action": action, "detail": detail}


async def run(slug: str, ctx: dict) -> dict:
    mod = _load_module(str(slug or ""))
    if mod is None or not callable(getattr(mod, "run", None)):
        return {
            "ok": False,
            "message": FALLBACK_MESSAGE,
            "action": "ask_and_transfer",
            "detail": "script_missing",
        }
    try:
        raw = await asyncio.wait_for(mod.run(ctx), timeout=RUN_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        log.warning("script %s estourou o tempo", slug)
        return {
            "ok": False,
            "message": FALLBACK_MESSAGE,
            "action": "ask_and_transfer",
            "detail": "script_timeout",
        }
    except Exception:
        log.exception("script %s falhou", slug)
        return {
            "ok": False,
            "message": FALLBACK_MESSAGE,
            "action": "ask_and_transfer",
            "detail": "script_exception",
        }
    return normalize_result(raw)


def error_message(slug: str) -> str:
    mod = _load_module(str(slug or ""))
    msg = str(getattr(mod, "ERROR_MESSAGE", "") or "") if mod else ""
    return msg or FALLBACK_MESSAGE
