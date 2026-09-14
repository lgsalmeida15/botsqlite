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
