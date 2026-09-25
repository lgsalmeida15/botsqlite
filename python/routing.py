# -*- coding: utf-8 -*-
"""Motor de roteamento: chat livre e formulário de validação.

Aqui ficam só as regras (janela de plantão, online, cap, prioridade e
estratégia de escolha). Nada de socket nem de SQL: o engine resolve time,
membros e a situação no TOA e entrega tudo pronto para `decide`.
"""
from __future__ import annotations

import re
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    TZ_BRT = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:
    TZ_BRT = timezone(timedelta(hours=-3))

KINDS = ("chat", "form")
# Tipo de atendimento do OPERADOR (nao confundir com o tipo da rota): `both` e o
# padrao — o operador recebe chat e formulario.
MEMBER_KINDS = ("both", "chat", "form")
STRATEGIES = ("round_robin", "priority", "least_load")
# 'close' não faz sentido no formulário (o bot não é participante do form).
CHAT_FALLBACKS = ("close", "monitor")
FORM_FALLBACKS = ("retry", "monitor")

# Chaves de bot_settings e de team_routing por tipo de tráfego.
_CAP_KEY = {"chat": "max_open_chats_per_agent", "form": "max_open_forms_per_agent"}
_ATTEMPTS_KEY = {"chat": "transfer_attempts", "form": "form_route_attempts"}
_INTERVAL_KEY = {"chat": "transfer_retry_interval_sec", "form": "form_route_retry_interval_sec"}
_FALLBACK_KEY = {"chat": "", "form": "form_route_fallback"}
_ROW_CAP = {"chat": "cap_chat", "form": "cap_form"}

_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?")


def _to_int(v, default: int) -> int:
    try:
        if v is None or v == "":
            return int(default)
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return int(default)


def _to_float(v, default: float) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(str(v).strip())
    except (TypeError, ValueError):
        return float(default)


def _to_bool(v, default: bool) -> bool:
    if v is None or v == "":
        return bool(default)
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("1", "true", "t", "sim", "yes", "on")



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


def member_login_raw(addr) -> str:
    x = str(addr or "").strip()
    if x.lower().startswith("u:"):
        x = x[2:]
    at = x.find("@")
    return x[:at] if at >= 0 else x


def member_login(addr) -> str:
    return member_login_raw(addr).lower()


def addr_eq(a, b) -> bool:
    sa, sb = str(a or "").strip().lower(), str(b or "").strip().lower()
    if sa and sa == sb:
        return True
    return bool(sa and sb and member_login(sa) == member_login(sb))


# Aliases com o nome histórico do engine (usado pelos testes e por _own_present_in).
_addr_eq = addr_eq
_member_login = member_login
_member_login_raw = member_login_raw


def kind_of(value) -> str:
    v = str(value or "").strip().lower()
    return v if v in KINDS else "chat"


def kinds_of(value) -> str:
    """Tipo de atendimento do operador: `both` (padrao), `chat` ou `form`."""
    v = str(value or "").strip().lower()
    return v if v in MEMBER_KINDS else "both"


def _addr(member: dict | None) -> str:
    return str((member or {}).get("address") or "")


def _names(members: list[dict], *, windows: bool = False) -> str:
    parts: list[str] = []
    for m in members:
        login = member_login_raw(_addr(m))
        parts.append(f"{login}:{_window_label(m)}" if windows else login)
    return ",".join(parts) or "—"


def resolve_settings(cfg: dict, routing_row: dict | None, kind: str) -> dict:
    """Herança: team_routing → bot_settings → padrão. `routing_row` é 1:1 com o time."""
    kind = kind_of(kind)
    row = routing_row or {}
    cfg = cfg or {}
    strategy = str(row.get("strategy") or cfg.get("routing_strategy") or "round_robin").strip()
    if strategy not in STRATEGIES:
        strategy = "round_robin"
    cap = row.get(_ROW_CAP[kind])
    if cap is None or cap == "":
        cap = cfg.get(_CAP_KEY[kind])
    attempts = row.get("attempts")
    if attempts is None or attempts == "":
        attempts = cfg.get(_ATTEMPTS_KEY[kind])
    interval = row.get("retry_interval_sec")
    if interval is None or interval == "":
        interval = cfg.get(_INTERVAL_KEY[kind])
    fallback = str(row.get("fallback") or "").strip()
    if not fallback and _FALLBACK_KEY[kind]:
        fallback = str(cfg.get(_FALLBACK_KEY[kind]) or "").strip()
    allowed = FORM_FALLBACKS if kind == "form" else CHAT_FALLBACKS
    if fallback not in allowed:
        fallback = allowed[0]
    casos = row.get("cap_cases")
    if casos is None or casos == "":
        casos = cfg.get("max_cases_per_agent")
    fonte = str(cfg.get("route_capacity_source") or "both").strip().lower()
    if fonte not in ("active", "helpdesk", "both"):
        fonte = "both"
    return {
        "kind": kind,
        "strategy": strategy,
        "cap": max(_to_int(cap, 8), 0),
        "require_online": _to_bool(row.get("require_online"), True),
        "respect_window": _to_bool(row.get("respect_window"), True),
        "attempts": max(_to_int(attempts, 5), 1),
        "retry_interval_sec": max(_to_float(interval, 60.0), 0.0),
        "fallback": fallback,
        "form_enabled": _to_bool(row.get("form_enabled"), True),
        "cap_cases": max(_to_int(casos, 4), 0),
        # Tipo de atendimento padrao dos operadores do time (membro sem linha e
        # sem `forms_enabled` herda isto): `both` (padrao), `chat` ou `form`.
        "member_kinds": kinds_of(row.get("member_kinds")),
        "capacity_source": fonte,
        "affinity": _to_bool(cfg.get("affinity_enabled"), True),
        "affinity_require_online": _to_bool(cfg.get("affinity_require_online"), False),
        "affinity_match_name": _to_bool(cfg.get("affinity_match_name"), False),
    }


def member_rules(member_row: dict | None, settings: dict, kind: str) -> dict:
    """Herança por membro: team_member_routing → cap do time → global."""
    kind = kind_of(kind)
    row = member_row or {}
    cap = row.get(_ROW_CAP[kind])
    if cap is None or cap == "":
        cap = settings.get("cap")
    casos = row.get("cap_cases")
    if casos is None or casos == "":
        casos = settings.get("cap_cases")
    # Tipo de atendimento: `kinds` do membro → `member_kinds` do time → ambos.
    # `forms_enabled` do legado (0 = so chat) e respeitado quando `kinds` nao veio.
    kinds = str(row.get("kinds") or "").strip().lower()
    if kinds not in MEMBER_KINDS:
        if row.get("forms_enabled") is not None:
            kinds = "both" if _to_bool(row.get("forms_enabled"), True) else "chat"
        else:
            kinds = kinds_of(settings.get("member_kinds"))
    return {
        "cap": max(_to_int(cap, 8), 0),
        "cap_cases": max(_to_int(casos, 4), 0),
        "weight": _to_int(row.get("weight"), 0),
        "kinds": kinds,
        # Espelho de leitura: `forms_enabled` continua sendo o campo do legado.
        "forms_enabled": kinds != "chat",
    }


def choose(
    eligible: list[dict],
    *,
    strategy: str,
    last_assigned: str = "",
    loads: dict | None = None,
    weights: dict | None = None,
) -> tuple[dict | None, str]:
    """Escolhe o próximo. `round_robin` = comportamento histórico do chat.

    Em `priority` o maior weight vence (cascata titular → apoio) e, no empate,
    o próximo do rodízio. Em `least_load` vence a menor carga, com desempate
    por weight e depois rodízio — sempre determinístico.
    """
    if not eligible:
        return None, ""
    loads = loads or {}
    weights = weights or {}
    n = len(eligible)
    idx_last = -1
    if last_assigned:
        for i, m in enumerate(eligible):
            if addr_eq(_addr(m), last_assigned):
                idx_last = i
                break

    def rr(i: int) -> int:
        return (i - idx_last - 1) % n

    if strategy == "priority":
        order = sorted(range(n), key=lambda i: (-int(weights.get(_addr(eligible[i]), 0)), rr(i)))
    elif strategy == "least_load":
        order = sorted(
            range(n),
            key=lambda i: (
                int(loads.get(_addr(eligible[i]), 0)),
                -int(weights.get(_addr(eligible[i]), 0)),
                rr(i),
            ),
        )
    else:
        order = sorted(range(n), key=rr)
    win = order[0]
    return eligible[win], f"pos={win + 1}/{n}"


def decide(
    members: list[dict],
    *,
    settings: dict,
    kind: str = "chat",
    mrouting: dict | None = None,
    loads: dict | None = None,
    loads_kind: dict | None = None,
    online: set[str] | None = None,
    last_assigned: str = "",
    now: datetime | None = None,
    team_name: str = "",
    attempts_used: int = 0,
) -> tuple[dict | None, str]:
    """Filtra e escolhe. Devolve (membro|None, detalhe legível para o log)."""
    kind = kind_of(kind)
    mrouting = mrouting or {}
    loads = loads or {}
    online = online or set()
    now = now or datetime.now(TZ_BRT)

    off_window: list[dict] = []
    offline: list[dict] = []
    only_chat: list[dict] = []
    only_form: list[dict] = []
    over_cap: list[dict] = []
    cand: list[dict] = []
    weights: dict[str, int] = {}

    for m in members:
        addr = _addr(m)
        rules = member_rules(mrouting.get(addr), settings, kind)
        # Tipo de atendimento do operador: quem so atende chat nao recebe
        # formulario e quem so atende formulario nao recebe chat.
        if kind == "form" and rules["kinds"] == "chat":
            only_chat.append(m)
            continue
        if kind == "chat" and rules["kinds"] == "form":
            only_form.append(m)
            continue
        weights[addr] = rules["weight"]
        if settings.get("respect_window", True) and not _in_route_window(m, now):
            off_window.append(m)
            continue
        if settings.get("require_online", True) and member_login(addr) not in online:
            offline.append(m)
            continue
        cand.append(m)

    por_tipo = loads_kind if loads_kind is not None else loads
    eligible: list[dict] = []
    for m in cand:
        addr = _addr(m)
        rules = member_rules(mrouting.get(addr), settings, kind)
        if int(loads.get(addr, 0)) >= rules["cap_cases"]:
            over_cap.append(m)
            continue
        if int(por_tipo.get(addr, 0)) >= rules["cap"]:
            over_cap.append(m)
            continue
        eligible.append(m)

    last_name = ""
    for m in members:
        if last_assigned and addr_eq(_addr(m), last_assigned):
            last_name = str(m.get("name") or last_assigned)
            break

    strategy = str(settings.get("strategy") or "round_robin")
    pick, where = choose(
        eligible,
        strategy=strategy,
        last_assigned=last_assigned,
        loads=loads,
        weights=weights,
    )
    pick_label = str((pick or {}).get("name") or _addr(pick)) if pick else ""
    casos = ",".join(f"{member_login_raw(_addr(m))}={int(loads.get(_addr(m), 0))}" for m in members)
    carga = ",".join(f"{member_login_raw(_addr(m))}={int(por_tipo.get(_addr(m), 0))}" for m in members)
    detail = (
        f"time={team_name or '—'}; estrategia={strategy}; cap={settings.get('cap')}; "
        f"membros={_names(members)}; janela={_names(eligible, windows=True)}; "
        f"fora_horario={_names(off_window, windows=True)}; sem_online={_names(offline)}; "
        f"so_chat={_names(only_chat)}; so_form={_names(only_form)}; no_cap={_names(over_cap)}; "
        f"casos={casos or '—'}; carga={carga or '—'}; tentativas={int(attempts_used)}"
    )
    if pick:
        return pick, f"{detail}; rr={last_name or '—'}→{pick_label}; {where}; last={last_assigned or '—'}"
    return None, detail

