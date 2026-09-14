from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db
from . import form_open
from . import resource_profile
from .resume import resume_action
from .scripts import registry
from .scripts.criar_apoio import BUDGET_SEC, parse_started_at
from .toa import ToaSocket

log = logging.getLogger("helptoa.engine")
MEM: dict[int, dict] = {}
try:
    TZ_BRT = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:
    TZ_BRT = timezone(timedelta(hours=-3))


def _parts(chat: dict) -> list[dict]:
    p = chat.get("participants") or {}
    if isinstance(p, dict):
        return list(p.values())
    return list(p) if isinstance(p, list) else []


def is_new(chat: dict, own: str) -> bool:
    if not (chat.get("helpdesk_address") or chat.get("channel_addr")):
        return False
    for p in _parts(chat):
        if "Moderator" in str(p.get("role_desc") or "") and p.get("present"):
            return False
        if p.get("address") == own and p.get("present"):
            return False
    return True


def _member_login_raw(addr) -> str:
    x = str(addr or "").strip()
    if x.lower().startswith("u:"):
        x = x[2:]
    at = x.find("@")
    return x[:at] if at >= 0 else x


def _member_login(addr) -> str:
    return _member_login_raw(addr).lower()


def _online_logins_from_ack(ack: dict) -> list[str] | None:
    """Lista que o TOA devolveu, ou None se timeout/resposta inútil."""
    if not isinstance(ack, dict) or ack.get("timeout"):
        return None
    d = ack.get("d") if isinstance(ack.get("d"), dict) else ack
    raw = d.get("result") if isinstance(d, dict) else None
    if not isinstance(raw, list):
        return None
    return [str(x).strip() for x in raw if x]


def _addr_eq(a, b) -> bool:
    sa, sb = str(a or "").strip().lower(), str(b or "").strip().lower()
    if sa and sa == sb:
        return True
    return bool(sa and sb and _member_login(sa) == _member_login(sb))


_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?")


def _as_clock(v) -> dt_time | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.time().replace(microsecond=0)
    if isinstance(v, dt_time):
        return v.replace(tzinfo=None, microsecond=0)
    if isinstance(v, timedelta):
        total = int(v.total_seconds()) % 86400
        if total < 0:
            total += 86400
        h, rem = divmod(total, 3600)
        m, sec = divmod(rem, 60)
        try:
            return dt_time(h, m, sec)
        except ValueError:
            return None
    m = _CLOCK_RE.search(str(v).strip())
    if not m:
        return None
    try:
        return dt_time(int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    except ValueError:
        return None


def _clock_minutes(t: dt_time) -> int:
    return t.hour * 60 + t.minute


def _window_label(member: dict) -> str:
    start = _as_clock(member.get("route_start"))
    end = _as_clock(member.get("route_end"))
    if start is None or end is None:
        return "24h"
    return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"


def _in_route_window(member: dict, now: datetime | None = None) -> bool:
    start = _as_clock(member.get("route_start"))
    end = _as_clock(member.get("route_end"))
    if start is None or end is None:
        return True
    start_m = _clock_minutes(start)
    end_m = _clock_minutes(end)
    if start_m == end_m:
        return True
    now = now or datetime.now(TZ_BRT)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ_BRT)
    now_m = _clock_minutes(now.astimezone(TZ_BRT).time().replace(microsecond=0))
    # Mesmo dia: 08:00–18:00. Overnight: 22:00–06:00 → agora >= início OU agora < fim.
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m


def queue_of(chat: dict) -> str | None:
    return chat.get("helpdesk_address") or chat.get("channel_addr")


def closure_chat(c: dict | None) -> bool:
    """Replica closureChat de app/app.js — formulário de encerramento, não chat de fila."""
    return form_open.is_validation_form(c)


def remember(chat: dict) -> int | None:
    cid = chat.get("chat_id") or chat.get("chatId")
    if cid is None:
        return None
    cid = int(cid)
    prev = MEM.get(cid, {})
    raw = dict(prev.get("raw") or {})
    raw.update(chat)
    MEM[cid] = {
        "started_addr": chat.get("started_addr") or prev.get("started_addr") or raw.get("started_addr"),
        "queue_address": queue_of(chat) or prev.get("queue_address"),
        "originator_spoke": prev.get("originator_spoke", False),
        "bot_present": prev.get("bot_present", False),
        "raw": raw,
    }
    return cid


def _msg_epoch(v) -> float | None:
    if v is None:
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n > 1e12:
        n /= 1000.0
    return n


def _before_menu(m: dict, row: dict) -> bool:
    sent = row.get("menu_sent_at")
    if not sent:
        return False
    t = _msg_epoch(m.get("time"))
    if t is None:
        return False
    sent_ts = sent.timestamp() if hasattr(sent, "timestamp") else _msg_epoch(sent)
    if sent_ts is None:
        return False
    return t < sent_ts - 0.5


def _script_answers(row: dict) -> dict:
    try:
        data = json.loads(row.get("script_answers") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _field_choices(field: dict) -> list[str]:
    raw = field.get("choices")
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _field_prompt(field: dict) -> str:
    prompt = str(field.get("prompt") or "")
    if str(field.get("type") or "text") != "choice":
        return prompt
    choices = _field_choices(field)
    if not choices:
        return prompt
    lines = [prompt] if prompt else []
    for i, label in enumerate(choices, start=1):
        lines.append(f"{i}) {label}")
    return "\n".join(lines)


def _validate_script_value(field: dict, text: str) -> str | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    typ = str(field.get("type") or "text")
    if typ == "int":
        if not raw.isdigit():
            return None
        return str(int(raw))
    if typ == "choice":
        choices = _field_choices(field)
        if not choices or not raw.isdigit():
            return None
        n = int(raw)
        if n < 1 or n > len(choices):
            return None
        return choices[n - 1]
    return raw


def _confirm_text(spec: dict, answers: dict) -> str:
    lines = ["Confirme as informações:", ""]
    for f in spec.get("fields") or []:
        key = str(f.get("key") or "")
        prompt = str(f.get("prompt") or key).rstrip(" ?")
        lines.append(f"{prompt}: {answers.get(key, '')}.")
    lines.append("")
    lines.append("Digite:")
    lines.append("1) Confirmar")
    lines.append("2) Corrigir")
    return "\n".join(lines)


def _own_present_in(chat: dict, own: str) -> bool:
    """Replica precisaAssumir invertido: participants[own].present."""
    if not own:
        return False
    parts = chat.get("participants") or {}
    if isinstance(parts, dict):
        items = list(parts.items())
    elif isinstance(parts, list):
        items = [(p.get("address"), p) for p in parts if isinstance(p, dict)]
    else:
        return False
    for addr, p in items:
        if not isinstance(p, dict):
            p = {"address": addr, "present": True}
        if not p.get("present"):
            continue
        if _addr_eq(p.get("address") or addr, own):
            return True
    return False


class Engine:
    def __init__(self, conn, sock: ToaSocket):
        self.conn = conn
        self.sock = sock
        self.cfg = db.settings(conn)
        self.tpl = db.templates(conn)
        self.enabled = db.enabled_queues(conn)
        self._joining: int | None = None
        self._open_forms: dict[int, dict] = {}
        self._script_inflight: set[int] = set()

    def reload_config(self) -> None:
        self.cfg = db.settings(self.conn)
        self.tpl = db.templates(self.conn)
        self.enabled = db.enabled_queues(self.conn)

    @property
    def own(self) -> str:
        return self.sock.own_address

    @property
    def bot_on(self) -> bool:
        return (self.cfg.get("bot_enabled") or "true") == "true"

    def _mark_present(self, cid: int | None) -> None:
        if cid is None:
            cid = self._joining
        if cid is None:
            return
        MEM.setdefault(int(cid), {})["bot_present"] = True

    def _join_payload_is_self(self, d: dict | None) -> bool:
        if not isinstance(d, dict):
            return False
        if not d.get("present"):
            return False
        return _addr_eq(d.get("address"), self.own)

    def _note_join_response(self, d: dict | None) -> None:
        if not self._join_payload_is_self(d):
            return
        cid = d.get("chat_id") or d.get("chatId") or self._joining
        self._mark_present(int(cid) if cid is not None else None)

    async def on_action(self, a: str, d: dict) -> None:
        if a == "get_helpdesk_details":
            self._ingest_queues(d)
        elif a in ("connect", "reconnect") and d.get("helpdesks"):
            self._ingest_queues(d["helpdesks"])
        elif a == "join_chat":
            self._note_join_response(d)
        elif a == "get_helpdesk_chats":
            for c in d.get("chats") or d.get("result") or []:
                await self.consider(c)
        elif a == "get_active_chats":
            for c in d.get("chats") or d.get("result") or []:
                if not isinstance(c, dict):
                    continue
                remember(c)
                self._note_open_form(c)
                if _own_present_in(c, self.own):
                    kid = c.get("chat_id") or c.get("chatId")
                    if kid is not None:
                        self._mark_present(int(kid))
        elif a == "get_contact_list":
            raw = d.get("contacts") or d.get("contact_list") or d.get("result") or d
            items = raw if isinstance(raw, list) else list(raw.values()) if isinstance(raw, dict) else []
            for c in items:
                if not isinstance(c, dict):
                    continue
                addr = str(c.get("address") or "")
                if addr.startswith("h:"):
                    db.upsert_queue(self.conn, addr, c.get("name") or c.get("description") or "")
            self.enabled = db.enabled_queues(self.conn)
        elif a == "search_contact":
            await self._ingest_search_contact(d)

    async def on_note(self, n: str, d: dict) -> None:
        if n == "helpdesk_chat_received" and d.get("chat"):
            await self.consider(d["chat"])
        elif n == "message_received":
            await self.on_message(d)
        elif n == "helpdesk_chat_removed" and d.get("chat_id"):
            cid = int(d["chat_id"])
            if self._joining is not None and cid == self._joining:
                self._mark_present(cid)
        elif n == "chat_removed" and d.get("chat_id"):
            cid = int(d["chat_id"])
            q = MEM.get(cid, {}).get("queue_address")
            db.upsert_chat(self.conn, cid, status="left", left_at=datetime.now(timezone.utc))
            db.log_event(self.conn, "left", chat_id=cid, queue_address=q, queue_enabled=q in self.enabled if q else None, detail="chat_removed")

    def _ingest_queues(self, raw) -> None:
        items = []
        if isinstance(raw, list):
            items = [(q.get("address") or q.get("helpdesk_address"), q) for q in raw]
        elif isinstance(raw, dict):
            if raw.get("address"):
                items = [(raw.get("address"), raw)]
            else:
                items = list(raw.items())
        for addr, q in items:
            if addr and "@" in str(addr):
                db.upsert_queue(self.conn, str(addr), q.get("name") or q.get("description") or "")
        self.enabled = db.enabled_queues(self.conn)

    async def _ingest_search_contact(self, d: dict) -> None:
        raw = d.get("result") if isinstance(d.get("result"), dict) else d
        if not isinstance(raw, dict):
            return
        n_ops, n_queues = await asyncio.to_thread(db.ingest_search_contact, raw)
        self.enabled = db.enabled_queues(self.conn)
        log.info("search_contact: %s operadores em %s filas", n_ops, n_queues)

    def _emit(self, reason: str, cid: int | None, q: str | None, detail: str = "") -> None:
        on = bool(q and q in self.enabled)
        db.log_event(self.conn, reason, chat_id=cid, queue_address=q, queue_enabled=on if q else None, detail=detail)

    def _note_open_form(self, chat: dict | None) -> None:
        if not chat:
            return
        kid = chat.get("chat_id") or chat.get("chatId")
        if kid is None:
            return
        cid = int(kid)
        if form_open.is_open_validation_form(chat):
            self._open_forms[cid] = dict(chat)
        elif cid in self._open_forms:
            self._open_forms.pop(cid, None)

    def _queue_chat_snapshot(self, cid: int, row: dict) -> dict:
        mem = MEM.get(cid, {})
        snap = dict(mem.get("raw") or {})
        if mem.get("started_addr") and not snap.get("started_addr"):
            snap["started_addr"] = mem["started_addr"]
        if row.get("queue_address") and not snap.get("helpdesk_address"):
            snap["helpdesk_address"] = row.get("queue_address")
        return snap

    async def consider(self, chat: dict) -> None:
        cid = remember(chat)
        q = queue_of(chat)
        if not self.bot_on:
            if cid is not None:
                self._emit("bot_off", cid, q, "bot desligado")
            return
        if cid is None:
            return
        if closure_chat(chat):
            self._note_open_form(chat)
            self._emit("closure", cid, q, "formulário de encerramento")
            return
        if not q:
            self._emit("queue_unknown", cid, None, "chat sem fila")
            return
        if q not in self.enabled:
            if q.startswith("h:"):
                db.upsert_queue(self.conn, q, "")
            self._emit("queue_off", cid, q, "fila desabilitada")
            return
        if not is_new(chat, self.own):
            self._emit("not_new", cid, q, "já tem analista ou bot")
            return
        with db.tx(self.conn) as cx:
            row = db.lock_chat(cx, cid)
            if row and row["status"] not in ("detected", "skipped"):
                return
            db.upsert_chat(cx, cid, queue_address=q, status="detected")
        try:
            await self.join_and_greet(cid, q)
        except Exception as e:
            log.exception("join_and_greet %s", cid)
            self._emit("join_failed", cid, q, str(e))

    async def _join_until_present(self, cid: int) -> bool:
        prev = self._joining
        self._joining = cid
        try:
            try:
                await self.sock.command(
                    "get_chat_messages",
                    {"chat_id": cid, "count": 10, "direction": "desc"},
                )
            except Exception as e:
                log.warning("get_chat_messages pré-join %s: %s", cid, e)
            ack = await self.sock.command("join_chat", {"chat_id": cid})
            blob = ack.get("d") if isinstance(ack.get("d"), dict) else ack
            log.info(
                "join_chat %s timeout=%s e=%s present=%s address=%s",
                cid,
                ack.get("timeout"),
                ack.get("e"),
                blob.get("present") if isinstance(blob, dict) else None,
                blob.get("address") if isinstance(blob, dict) else None,
            )
            self._note_join_response(blob)
            for _ in range(40):
                if MEM.get(cid, {}).get("bot_present"):
                    break
                await asyncio.sleep(0.25)
            if not MEM.get(cid, {}).get("bot_present"):
                try:
                    ack2 = await self.sock.command("get_active_chats", {"contact_status": False})
                    d2 = ack2.get("d") if isinstance(ack2.get("d"), dict) else ack2
                    for c in (d2.get("chats") or d2.get("result") or []):
                        if not isinstance(c, dict):
                            continue
                        remember(c)
                        kid = c.get("chat_id") or c.get("chatId")
                        if kid is not None and int(kid) == cid and _own_present_in(c, self.own):
                            self._mark_present(cid)
                except Exception as e:
                    log.warning("get_active_chats pós-join %s: %s", cid, e)
            return bool(MEM.get(cid, {}).get("bot_present"))
        finally:
            self._joining = prev

    async def join_and_greet(self, cid: int, queue_address: str) -> None:
        if not await self._join_until_present(cid):
            self._emit("join_failed", cid, queue_address, "sem presença no chat")
            return
        db.upsert_chat(self.conn, cid, queue_address=queue_address, status="joined")
        self._emit("assumed", cid, queue_address, "chat assumido")
        opts = db.menu_options(self.conn, queue_address, None)
        if not opts:
            self._emit("join_failed", cid, queue_address, "menu vazio")
            return
        await self.send(cid, self._menu_text(opts, root=True))
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            queue_address=queue_address,
            status="awaiting_reply",
            retries=0,
            menu_node_id=None,
            menu_sent_at=now,
            last_prompt_at=now,
        )
        self._emit("menu_sent", cid, queue_address, "menu enviado")

    async def rejoin_silent(self, cid: int, queue_address: str) -> None:
        if not await self._join_until_present(cid):
            self._emit("join_failed", cid, queue_address, "resume sem presença")
            return
        log.info("rejoin_silent %s fila=%s", cid, queue_address)

    async def resume_open_chats(self) -> None:
        active_by_id: dict[int, dict] = {}
        fetched_ok = False
        try:
            ack = await self.sock.command("get_active_chats", {"contact_status": False})
            if not (isinstance(ack, dict) and ack.get("timeout")):
                d = ack.get("d") if isinstance(ack.get("d"), dict) else ack
                for c in (d.get("chats") or d.get("result") or []):
                    if not isinstance(c, dict):
                        continue
                    remember(c)
                    self._note_open_form(c)
                    kid = c.get("chat_id") or c.get("chatId")
                    if kid is None:
                        continue
                    cid = int(kid)
                    active_by_id[cid] = c
                    if _own_present_in(c, self.own):
                        self._mark_present(cid)
                fetched_ok = True
        except Exception as e:
            log.warning("resume get_active_chats: %s", e)
        if not fetched_ok:
            log.warning("resume_open_chats: sem lista ativa; adia")
            return
        now = datetime.now(timezone.utc)
        for row in db.resume_chats(self.conn):
            cid = int(row["chat_id"])
            q = row.get("queue_address")
            action = resume_action(row, active_by_id.get(cid), self.own)
            if action == "rehydrate":
                remember(active_by_id[cid])
                self._note_open_form(active_by_id[cid])
                self._mark_present(cid)
                log.info("resume rehydrate chat=%s", cid)
                await self._resume_script_run(row)
            elif action == "rejoin_silent":
                try:
                    await self.rejoin_silent(cid, q or "")
                    fresh = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone() or row
                    await self._resume_script_run(fresh)
                except Exception as e:
                    log.exception("rejoin_silent %s", cid)
                    self._emit("join_failed", cid, q, str(e))
            elif action == "stale":
                db.upsert_chat(self.conn, cid, status="left", left_at=now)
                self._emit("resume_stale", cid, q, "chat sumiu do TOA")

    def _menu_text(self, opts: list[dict], *, root: bool = True, title: str | None = None) -> str:
        if root:
            lines = [self.tpl.get("greeting") or "", self.tpl.get("menu") or ""]
        else:
            lines = [title or ""]
        for o in opts:
            lines.append(f"{o['sort_order']}) {o['label']}")
        if not root:
            lines.append("0) Voltar")
        return "\n".join(x for x in lines if x)

    def _menu_node_id(self, row: dict) -> int | None:
        node = row.get("menu_node_id")
        if node is None or node == "":
            return None
        return int(node)

    async def _prompt_menu(self, cid: int, row: dict, parent_id: int | None, title: str | None = None) -> None:
        q = row.get("queue_address")
        opts = db.menu_options(self.conn, q, parent_id)
        if parent_id is not None and title is None:
            parent = db.menu_option(self.conn, parent_id)
            title = (parent or {}).get("label") or ""
        await self.send(cid, self._menu_text(opts, root=parent_id is None, title=title))
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            queue_address=q,
            status="awaiting_reply",
            retries=0,
            menu_node_id=parent_id,
            last_prompt_at=now,
        )

    async def send(self, cid: int, text: str) -> None:
        await self.sock.command(
            "send_message",
            {"chat_id": cid, "mark_read": False, "message": {"delivery_id": int(time.time() * 1000), "text": text}},
        )

    async def on_message(self, m: dict) -> None:
        cid = m.get("chat_id")
        if cid is None:
            return
        cid = int(cid)
        remember({"chat_id": cid, "started_addr": MEM.get(cid, {}).get("started_addr")})
        sysm = m.get("system_message")
        if sysm == "user_joined":
            part = m.get("participant")
            addr = part.get("address") if isinstance(part, dict) else None
            auth = m.get("author")
            if isinstance(auth, dict):
                auth = auth.get("address")
            if _addr_eq(addr or auth, self.own):
                self._mark_present(cid)
            return
        if sysm == "user_left" and m.get("author"):
            await self.on_left(cid, m["author"])
            return
        if sysm:
            return
        if m.get("author") == self.own or _addr_eq(m.get("author"), self.own):
            return
        mem = MEM.get(cid, {})
        origin = mem.get("started_addr")
        if origin and m.get("author") != origin:
            return
        if m.get("author") and m.get("author") == origin:
            mem["originator_spoke"] = True
        row = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone()
        if not row:
            return
        st = row.get("status")
        if st not in ("awaiting_reply", "awaiting_script_answer", "awaiting_script_confirm"):
            return
        if st == "awaiting_reply" and _before_menu(m, row):
            return
        text = str(m.get("text") or "").strip()
        if not text:
            return
        await self.handle_reply(cid, row, text)

    async def handle_reply(self, cid: int, row: dict, text: str) -> None:
        st = row.get("status")
        if st == "awaiting_script_answer":
            await self.handle_script_answer(cid, row, text)
            return
        if st == "awaiting_script_confirm":
            await self.handle_script_confirm(cid, row, text)
            return
        q = row.get("queue_address")
        parent_id = self._menu_node_id(row)
        if parent_id is not None and text.strip() == "0":
            await self._prompt_menu(cid, row, None)
            return
        opts = db.menu_options(self.conn, q, parent_id)
        chosen = None
        if text.isdigit():
            n = int(text)
            chosen = next((o for o in opts if int(o["sort_order"]) == n), None)
        if chosen:
            if chosen.get("action") == "submenu":
                kids = db.menu_options(self.conn, q, int(chosen["id"]))
                if not kids:
                    await self._invalid_reply(cid, row)
                    return
                await self._prompt_menu(cid, row, int(chosen["id"]), title=chosen.get("label") or "")
                return
            await self.apply_option(cid, row, chosen)
            return
        await self._invalid_reply(cid, row)

    async def _invalid_reply(self, cid: int, row: dict) -> None:
        max_r = int(self.cfg.get("max_invalid_retries") or 3)
        retries = int(row["retries"] or 0)
        q = row.get("queue_address")
        if retries < max_r:
            await self.send(cid, self.tpl.get("retry") or "")
            node = self._menu_node_id(row)
            if node is not None:
                parent = db.menu_option(self.conn, node)
                opts = db.menu_options(self.conn, q, node)
                await self.send(
                    cid,
                    self._menu_text(opts, root=False, title=(parent or {}).get("label") or ""),
                )
            now = datetime.now(timezone.utc)
            db.upsert_chat(self.conn, cid, status="awaiting_reply", retries=retries + 1, last_prompt_at=now)
            self._emit("ignored_reply", cid, q, "opção inválida")
            return
        await self.close_out(cid, "closed_invalid")

    async def _deny_script(self, cid: int, row: dict, slug: str, result: dict) -> None:
        login = str(result.get("login") or "")
        detail = resource_profile.deny_detail(slug, login, result)
        q = row.get("queue_address")
        await self.send(cid, resource_profile.DENIED_MESSAGE)
        self._emit("script_denied", cid, q, detail)
        await self.leave(cid, "closed")

    async def start_script(self, cid: int, row: dict, opt: dict) -> None:
        slug = str(opt.get("script_slug") or "")
        spec = registry.get_spec(slug)
        fields = spec.get("fields") if spec else None
        if not spec or not fields:
            await self._invalid_reply(cid, row)
            return
        if slug == "criar_apoio":
            addr = (MEM.get(cid) or {}).get("started_addr") or ""
            login = _member_login_raw(addr)
            page = getattr(self.sock, "page", None)
            result = await resource_profile.lookup(page, login)
            if not result.get("allowed"):
                await self._deny_script(cid, row, slug, result)
                return
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            queue_address=row.get("queue_address"),
            status="awaiting_script_answer",
            option_id=opt.get("id"),
            script_slug=slug,
            script_field_index=0,
            script_answers="{}",
            retries=0,
            last_prompt_at=now,
        )
        prompt = _field_prompt(fields[0])
        if prompt:
            await self.send(cid, prompt)

    async def _restart_script(self, cid: int, row: dict) -> None:
        spec = registry.get_spec(row.get("script_slug"))
        fields = spec.get("fields") if spec else None
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            status="awaiting_script_answer",
            script_field_index=0,
            script_answers="{}",
            retries=0,
            last_prompt_at=now,
        )
        if fields:
            prompt = _field_prompt(fields[0])
            if prompt:
                await self.send(cid, prompt)

    async def handle_script_answer(self, cid: int, row: dict, text: str) -> None:
        spec = registry.get_spec(row.get("script_slug"))
        fields = spec.get("fields") if spec else []
        try:
            idx = int(row.get("script_field_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if not fields or idx < 0 or idx >= len(fields):
            await self._restart_script(cid, row)
            return
        field = fields[idx]
        value = _validate_script_value(field, text)
        now = datetime.now(timezone.utc)
        if value is None:
            prompt = _field_prompt(field)
            if prompt:
                await self.send(cid, prompt)
            retries = int(row.get("retries") or 0)
            max_r = int(self.cfg.get("max_invalid_retries") or 3)
            if retries < max_r:
                db.upsert_chat(self.conn, cid, status="awaiting_script_answer", retries=retries + 1, last_prompt_at=now)
                self._emit("ignored_reply", cid, row.get("queue_address"), "campo inválido")
                return
            await self.close_out(cid, "closed_invalid")
            return
        answers = _script_answers(row)
        answers[str(field.get("key") or "")] = value
        nxt = idx + 1
        payload = json.dumps(answers, ensure_ascii=False)
        if nxt >= len(fields):
            db.upsert_chat(
                self.conn, cid,
                status="awaiting_script_confirm",
                script_answers=payload,
                retries=0,
                last_prompt_at=now,
            )
            await self.send(cid, _confirm_text(spec, answers))
            return
        db.upsert_chat(
            self.conn, cid,
            status="awaiting_script_answer",
            script_field_index=nxt,
            script_answers=payload,
            retries=0,
            last_prompt_at=now,
        )
        next_prompt = _field_prompt(fields[nxt])
        if next_prompt:
            await self.send(cid, next_prompt)

    async def handle_script_confirm(self, cid: int, row: dict, text: str) -> None:
        choice = text.strip()
        if choice == "2":
            await self._restart_script(cid, row)
            return
        if choice == "1":
            await self._execute_script(cid, row, resume=False)
            return
        spec = registry.get_spec(row.get("script_slug"))
        answers = _script_answers(row)
        retries = int(row.get("retries") or 0)
        max_r = int(self.cfg.get("max_invalid_retries") or 3)
        if spec and retries < max_r:
            await self.send(cid, _confirm_text(spec, answers))
            now = datetime.now(timezone.utc)
            db.upsert_chat(self.conn, cid, status="awaiting_script_confirm", retries=retries + 1, last_prompt_at=now)
            self._emit("ignored_reply", cid, row.get("queue_address"), "confirmação inválida")
            return
        await self.close_out(cid, "closed_invalid")

    def _persist_script_uuid(self, cid: int, uid: str) -> None:
        db.upsert_chat(self.conn, cid, script_uuid=str(uid or ""), status="awaiting_script_run")

    async def _execute_script(self, cid: int, row: dict, *, resume: bool) -> None:
        slug = str(row.get("script_slug") or "")
        opt = db.menu_option(self.conn, row.get("option_id")) or {}
        now = datetime.now(timezone.utc)
        started = row.get("script_started_at") or now.strftime("%Y-%m-%d %H:%M:%S")
        if not resume:
            db.upsert_chat(
                self.conn, cid,
                status="awaiting_script_run",
                script_started_at=started,
                last_prompt_at=now,
            )
            row = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone() or row
        ctx = {
            "chat_id": cid,
            "queue_address": row.get("queue_address"),
            "answers": _script_answers(row),
            "option": opt,
            "persist_job": lambda uid: self._persist_script_uuid(cid, uid),
            "started_at": row.get("script_started_at") or started,
        }
        if resume:
            uid = str(row.get("script_uuid") or "").strip()
            if uid:
                ctx["resume_uuid"] = uid
        self._script_inflight.add(cid)
        try:
            result = await registry.run(slug, ctx)
        finally:
            self._script_inflight.discard(cid)
        fresh = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone() or row
        await self.apply_script_result(cid, fresh, result)

    async def _resume_script_run(self, row: dict) -> None:
        if str(row.get("status") or "") != "awaiting_script_run":
            return
        cid = int(row["chat_id"])
        if cid in self._script_inflight:
            return
        uid = str(row.get("script_uuid") or "").strip()
        started = parse_started_at(row.get("script_started_at"))
        age = (datetime.now(timezone.utc) - started).total_seconds()
        if not uid or age >= BUDGET_SEC:
            await self.apply_script_result(cid, row, {
                "ok": False,
                "message": registry.error_message(str(row.get("script_slug") or "")),
                "action": "ask_and_transfer",
                "detail": "resume_timeout" if uid else "resume_no_uuid",
            })
            return
        await self._execute_script(cid, row, resume=True)

    async def apply_script_result(self, cid: int, row: dict, result: dict) -> None:
        action = str(result.get("action") or "stay")
        message = str(result.get("message") or "")
        q = row.get("queue_address")
        reason = "script_ok" if result.get("ok") else "script_error"
        self._emit(reason, cid, q, str(result.get("detail") or "")[:800])
        if message:
            await self.send(cid, message)
        if action == "retry_form":
            await self._restart_script(cid, row)
            return
        if action == "stay":
            db.upsert_chat(self.conn, cid, status="monitoring")
            return
        if action in ("reply_and_close", "ask_and_transfer", "transfer_only"):
            opt = db.menu_option(self.conn, row.get("option_id")) or {}
            await self._finish_leaf_action(cid, row, opt, action, reply="", form_open_flag=False)
            return
        db.upsert_chat(self.conn, cid, status="monitoring")

    async def apply_option(self, cid: int, row: dict, opt: dict) -> None:
        if opt.get("action") == "submenu":
            await self._invalid_reply(cid, row)
            return
        if str(opt.get("action") or "") == "run_script":
            await self.start_script(cid, row, opt)
            return
        snap = self._queue_chat_snapshot(cid, row)
        form_open_flag = form_open.has_open_validation_form(
            snap, self._open_forms.values(), own=self.own
        )
        action, reply = form_open.resolve_option(opt, form_open_flag)
        await self._finish_leaf_action(cid, row, opt, action, reply=reply, form_open_flag=form_open_flag)

    async def _finish_leaf_action(
        self, cid: int, row: dict, opt: dict, action: str, reply: str = "", form_open_flag: bool = False
    ) -> None:
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            status="classified",
            classification=opt.get("classification") or "",
            option_id=opt.get("id"),
            classified_at=now,
            auto_close=action == "reply_and_close",
        )
        if action in ("reply_and_close", "ask_and_transfer") and reply:
            await self.send(cid, reply)
        q = row.get("queue_address")
        detail = opt.get("classification") or ""
        pid = opt.get("parent_id")
        if pid:
            parent = db.menu_option(self.conn, int(pid))
            if parent:
                detail = f"{parent.get('label') or ''} > {opt.get('label') or ''}".strip(" >")
        if form_open_flag:
            detail = f"{detail}; form_aberto" if detail else "form_aberto"
        self._emit("classified", cid, q, detail)
        if action == "reply_and_close":
            await self.leave(cid, "closed")
            return
        if action in ("ask_and_transfer", "transfer_only"):
            picked, route = await self.pick_assignee(q)
            if picked:
                addr = str(picked.get("address") or "")
                label = picked.get("name") or addr
                if not q:
                    route = f"{route}; move_falhou=sem fila"
                else:
                    try:
                        await self.sock.command(
                            "move_chat",
                            {
                                "chat_id": cid,
                                "channel_address": q,
                                "reason": f"Transferido para {label}",
                                "to_operator": True,
                            },
                        )
                    except Exception as e:
                        log.warning("move_chat %s %s: %s", cid, q, e)
                        route = f"{route}; move_falhou={e}"
                    else:
                        try:
                            await self.sock.command("invite_to_chat", {"chat_id": cid, "address": addr})
                            tid = picked.get("team_id")
                            if tid is not None:
                                db.set_team_last_assigned(self.conn, int(tid), addr)
                            self._emit("transferred", cid, q, f"{label}; {route}")
                            await self.leave(cid, "closed")
                            return
                        except Exception as e:
                            log.warning("invite_to_chat %s %s: %s", cid, addr, e)
                            route = f"{route}; invite_falhou={e}"
            log.warning("transferência %s sem analista disponível; bot sai do chat", cid)
            self._emit("no_agent", cid, q, route or "nenhum analista disponível no time")
            await self.close_out(cid, "closed_no_agent")

    async def pick_assignee(self, queue_address: str | None) -> tuple[dict | None, str]:
        team = db.team_for_queue(self.conn, queue_address)
        if not team:
            log.warning("fila %s sem time ativo", queue_address)
            return None, "fila sem time ativo"
        members = db.team_members(self.conn, team["id"])
        if not members:
            log.warning("time %s sem membros", team.get("name") or team["id"])
            return None, "time sem membros"
        now_brt = datetime.now(TZ_BRT)
        off_shift: list[dict] = []
        on_shift: list[dict] = []
        for m in members:
            if _in_route_window(m, now_brt):
                on_shift.append(m)
            else:
                off_shift.append(m)
        fora_s = ",".join(
            f"{_member_login_raw(m['address'])}:{_window_label(m)}" for m in off_shift
        ) or "—"
        janela_s = ",".join(
            f"{_member_login_raw(m['address'])}:{_window_label(m)}" for m in on_shift
        ) or "—"
        if not on_shift:
            log.warning("time %s sem membros na janela", team.get("name") or team["id"])
            return None, f"fora_horario; janelas={fora_s}"
        members = on_shift
        cap = int(self.cfg.get("max_open_chats_per_agent") or 8)
        max_r = int(self.cfg.get("transfer_attempts") or 5)
        interval = float(self.cfg.get("transfer_retry_interval_sec") or 60.0)
        logins = [_member_login_raw(m["address"]) for m in members]
        members_s = ",".join(logins) or "vazio"
        raw_online: list[str] = []
        attempts = 0
        for attempt in range(1, max_r + 1):
            attempts = attempt
            try:
                ack = await self.sock.command(
                    "get_online_users", {"users": logins}, timeout=12.0
                )
                parsed = _online_logins_from_ack(ack)
                if parsed is not None:
                    raw_online = parsed
                    if parsed:
                        break
            except Exception as e:
                log.warning("get_online_users: %s", e)
                raw_online = []
            if attempt < max_r:
                await asyncio.sleep(interval)
        online = {_member_login(x) for x in raw_online}
        toa_s = ",".join(raw_online) if raw_online else "vazio"
        counts: dict[str, int] = {str(m["address"]): 0 for m in members}
        try:
            ack2 = await self.sock.command("get_active_chats", {"contact_status": False})
            d2 = ack2.get("d") if isinstance(ack2.get("d"), dict) else ack2
            for c in d2.get("chats") or d2.get("result") or []:
                if not isinstance(c, dict):
                    continue
                for p in _parts(c):
                    if not isinstance(p, dict) or not p.get("present"):
                        continue
                    pa = p.get("address") or ""
                    for m in members:
                        if _addr_eq(pa, m["address"]):
                            key = str(m["address"])
                            counts[key] = counts.get(key, 0) + 1
        except Exception as e:
            log.warning("get_active_chats carga: %s", e)
        eligible: list[dict] = []
        for m in members:
            if _member_login(m["address"]) not in online:
                continue
            n = counts.get(str(m["address"]), 0)
            if n >= cap:
                continue
            eligible.append(m)
        base = (
            f"membros={members_s}; janela={janela_s}; fora_horario={fora_s}; "
            f"toa_online={toa_s}; tentativas={attempts}"
        )
        if not eligible:
            return None, base
        last = str(team.get("last_assigned") or "")
        idx = -1
        last_name = last or "—"
        for i, m in enumerate(eligible):
            if last and _addr_eq(m["address"], last):
                idx = i
                last_name = m.get("name") or last
                break
        if idx < 0 and last:
            for m in members:
                if _addr_eq(m["address"], last):
                    last_name = m.get("name") or last
                    break
        chosen = eligible[(idx + 1) % len(eligible)]
        label = chosen.get("name") or chosen.get("address") or ""
        return chosen, f"{base}; rr={last_name}→{label}; last={last or '—'}"

    async def on_left(self, cid: int, author: str) -> None:
        mem = MEM.get(cid, {})
        origin = mem.get("started_addr")
        row = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone()
        if not row:
            return
        if author == origin and mem.get("originator_spoke"):
            await self.leave(cid, "closed_originator_left")
            return
        if row["status"] == "monitoring" and author != self.own and author != origin:
            await self.leave(cid, "left")

    async def close_out(self, cid: int, status: str) -> None:
        try:
            await self.send(cid, self.tpl.get("closing") or "Chat está sendo encerrado")
        except Exception as e:
            log.warning("close_out send %s: %s", cid, e)
        await self.leave(cid, status)

    async def leave(self, cid: int, status: str) -> None:
        try:
            await self.sock.command("leave_chat", {"chat_id": cid})
        except Exception as e:
            log.warning("leave_chat %s: %s", cid, e)
        now = datetime.now(timezone.utc)
        db.upsert_chat(self.conn, cid, status=status, auto_close=True, left_at=now)
        q = MEM.get(cid, {}).get("queue_address")
        reason = "closed_timeout" if status == "closed_timeout" else "closed_invalid" if status == "closed_invalid" else "left"
        self._emit(reason, cid, q, status)

    async def tick_timeouts(self) -> None:
        sec = int(self.cfg.get("reply_timeout_sec") or 300)
        for row in db.awaiting_timeouts(self.conn, sec):
            await self.close_out(int(row["chat_id"]), "closed_timeout")
        for row in db.script_run_timeouts(self.conn, int(BUDGET_SEC)):
            cid = int(row["chat_id"])
            if cid in self._script_inflight:
                continue
            await self._resume_script_run(row)

    async def bootstrap(self) -> None:
        MEM.clear()
        log.info("bootstrap início")
        await self.discover_catalog()
        await self.process_pending_jobs()
        for addr in self.enabled:
            try:
                await self.sock.command(
                    "get_helpdesk_chats",
                    {"chat_type": "pending", "count": 50, "from": 0, "helpdesk_address": addr},
                )
            except Exception as e:
                log.warning("pending %s: %s", addr, e)
        try:
            await self.sock.command("get_active_chats", {"contact_status": False})
        except Exception:
            pass
        try:
            await self.resume_open_chats()
        except Exception:
            log.exception("resume_open_chats")
        log.info("bootstrap fim filas_habilitadas=%s", len(self.enabled))

    async def discover_catalog(self) -> None:
        """Coleta o catálogo do TOA: filas e operadores (append-only)."""
        searches = (
            ("get_helpdesk_details", {}, 8.0),
            (
                "get_contact_list",
                {"count": 500, "from": 0, "load_all_helpdesks": True},
                8.0,
            ),
            ("search_contact", {"count": -1, "from": 0, "substring": "", "where": "ho"}, 30.0),
        )
        for a, payload, timeout in searches:
            try:
                await self.sock.command(a, payload, timeout=timeout)
            except Exception as e:
                log.warning("%s: %s", a, e)

    async def process_pending_jobs(self) -> None:
        """Consome um pedido do painel (bot_jobs) por chamada."""
        job = db.claim_pending_job(self.conn)
        if not job:
            return
        jid = int(job["id"])
        kind = str(job.get("kind") or "")
        try:
            if kind == "catalog_sync":
                await self.discover_catalog()
            else:
                raise ValueError(f"job kind desconhecido: {kind}")
            db.finish_job(self.conn, jid, "done")
            db.log_event(self.conn, "catalog_synced", detail=f"job={jid}; kind={kind}")
        except Exception as e:
            log.warning("bot_jobs %s falhou: %s", jid, e)
            db.finish_job(self.conn, jid, "error", str(e)[:500])
            db.log_event(self.conn, "sync_error", detail=f"job={jid}; kind={kind}: {e}")
