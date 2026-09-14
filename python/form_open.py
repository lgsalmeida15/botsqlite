# -*- coding: utf-8 -*-
"""Formulário de validação aberto: detecção e resolução da folha do menu."""
from __future__ import annotations

import unicodedata


def _deaccent(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).casefold()


def _parts(chat: dict | None) -> list[dict]:
    if not chat:
        return []
    p = chat.get("participants") or {}
    if isinstance(p, dict):
        return [v for v in p.values() if isinstance(v, dict)]
    return [x for x in p if isinstance(x, dict)] if isinstance(p, list) else []


def _addr(p) -> str:
    if isinstance(p, dict):
        return str(p.get("address") or "")
    return str(p or "")


def is_system_or_staff(p, chat: dict | None = None, own: str | None = None) -> bool:
    addr = _addr(p)
    if not addr:
        return True
    if own and addr == own:
        return True
    if "CREATEDBY" in addr:
        return True
    role = ""
    if isinstance(p, dict):
        role = str(p.get("role_desc") or "")
    if "Moderator" in role:
        return True
    if chat:
        helpdesk = chat.get("helpdesk_address") or chat.get("channel_addr")
        if helpdesk and addr == str(helpdesk):
            return True
    return False


def is_validation_form(c: dict | None) -> bool:
    """Equivalente a closure_chat: formulário de encerramento/validação, não chat de fila."""
    if not c:
        return False
    if c.get("mode") == "message_scenario":
        return True
    fm = c.get("first_message")
    if not fm:
        return False
    if (fm.get("subject") or "") != "Solicitação de Encerramento":
        return False
    if "CREATEDBYMESSAGESCENARIO" in str(fm.get("author") or ""):
        return True
    if c.get("lifecycle") == "removed":
        return False
    ended = c.get("ended_time")
    if ended is not None:
        try:
            if float(ended) > 0:
                return False
        except (TypeError, ValueError):
            pass
    try:
        open_ended = ended is not None and float(ended) == -1
    except (TypeError, ValueError):
        open_ended = False
    return open_ended or c.get("lifecycle") in ("pending", "active")


def is_form_done(c: dict | None) -> bool:
    if not c:
        return True
    if c.get("lifecycle") == "removed":
        return True
    ended = c.get("ended_time")
    if ended is None:
        return False
    try:
        return float(ended) > 0
    except (TypeError, ValueError):
        return False


def is_open_validation_form(chat: dict | None) -> bool:
    return bool(chat) and is_validation_form(chat) and not is_form_done(chat)


def _display_name(chat: dict | None) -> str:
    if not chat:
        return ""
    for key in ("person", "name", "started_name"):
        v = str(chat.get(key) or "").strip()
        if v:
            return v
    started = str(chat.get("started_addr") or "")
    for p in _parts(chat):
        if _addr(p) == started:
            n = str(p.get("name") or "").strip()
            if n:
                return n
    for p in _parts(chat):
        n = str(p.get("name") or "").strip()
        if n:
            return n
    return ""


def chat_opener(chat: dict | None, own: str | None = None) -> dict:
    started = str((chat or {}).get("started_addr") or "")
    address = None
    if started and not is_system_or_staff({"address": started}, chat, own):
        address = started
    name = _display_name(chat)
    channel = str((chat or {}).get("channel_name") or "")
    helpdesk = str((chat or {}).get("helpdesk_address") or (chat or {}).get("channel_addr") or "")
    fallback = (
        not name
        or name.startswith("Sem identificação")
        or name == channel
        or name == helpdesk
    )
    return {"address": address, "name": "" if fallback else name}


def form_requester(form: dict | None, own: str | None = None) -> dict:
    addrs: list[str] = []
    seen: set[str] = set()
    for p in _parts(form):
        if not p or is_system_or_staff(p, form, own):
            continue
        a = _addr(p)
        if a and a not in seen:
            addrs.append(a)
            seen.add(a)
    started = str((form or {}).get("started_addr") or "")
    if started and not is_system_or_staff({"address": started}, form, own) and started not in seen:
        addrs.append(started)
    name = _display_name(form)
    if name in ("", "Solicitação de Encerramento") or name.startswith("Sem identificação"):
        name = ""
    return {"addrs": addrs, "name": name}


def has_open_validation_form(queue_chat: dict | None, forms, own: str | None = None) -> bool:
    if not queue_chat:
        return False
    opener = chat_opener(queue_chat, own)
    addrs: set[str] = set()
    names: set[str] = set()
    for form in forms or []:
        if not is_open_validation_form(form):
            continue
        req = form_requester(form, own)
        addrs.update(req["addrs"])
        if req["name"]:
            names.add(_deaccent(req["name"]))
    if opener.get("address"):
        return opener["address"] in addrs
    if opener.get("name") and _deaccent(opener["name"]) in names:
        return True
    return False


def resolve_option(opt: dict, form_open: bool) -> tuple[str, str]:
    action = str(opt.get("action") or "")
    reply = str(opt.get("reply_text") or "")
    if not form_open:
        return action, reply
    alt_action = str(opt.get("action_if_form") or "").strip()
    alt_reply = str(opt.get("reply_text_if_form") or "")
    if alt_action:
        action = alt_action
    if alt_reply.strip():
        reply = alt_reply
    return action, reply
