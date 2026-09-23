# -*- coding: utf-8 -*-
"""Decisão de retomar chats após restart e de reciclar o Chromium."""
from __future__ import annotations

OPEN_STATUSES = frozenset({
    "joined", "menu_sent", "awaiting_reply", "classified", "monitoring",
    "awaiting_script_answer", "awaiting_script_confirm", "awaiting_script_run",
    "awaiting_form_choice",
})


class BrowserStale(Exception):
    """WebSocket/TOA travado; o Chromium deve ser reciclado."""


def _own_present(chat: dict | None, own: str) -> bool:
    if not chat:
        return False
    from .engine import _own_present_in
    return _own_present_in(chat, own)


def resume_action(row: dict | None, active_chat: dict | None, own: str) -> str:
    if not row:
        return "skip"
    status = str(row.get("status") or "")
    if status not in OPEN_STATUSES:
        return "skip"
    if active_chat is None:
        return "stale"
    if _own_present(active_chat, own):
        return "rehydrate"
    return "rejoin_silent"


def should_recycle(
    consecutive_version_fails: int,
    last_frame_age_sec: float | None,
    *,
    fail_limit: int = 3,
    stale_sec: int = 180,
) -> bool:
    if consecutive_version_fails >= fail_limit:
        return True
    if last_frame_age_sec is not None and last_frame_age_sec > stale_sec:
        return True
    return False
