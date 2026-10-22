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


def _window_label(member: dict, rows: list[dict] | None = None) -> str:
    if rows:
        shifts = [r for r in rows if str(r.get("kind") or "shift") == "shift"]
        breaks = [r for r in rows if str(r.get("kind") or "shift") == "break"]

        def fmt(r: dict) -> str:
            s, e = _as_clock(r.get("start_time")), _as_clock(r.get("end_time"))
            if s is None or e is None:
                return "?"
            return f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"

        label = ",".join(fmt(r) for r in shifts) or "24h"
        if breaks:
            label += "[descanso " + ",".join(fmt(r) for r in breaks) + "]"
        return label
    start = _as_clock(member.get("route_start"))
    end = _as_clock(member.get("route_end"))
    if start is None or end is None:
        return "24h"
    return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"


def _now_minutes(now: datetime | None) -> int:
    now = now or datetime.now(TZ_BRT)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ_BRT)
    return _clock_minutes(now.astimezone(TZ_BRT).time().replace(microsecond=0))


def _weekday_bit(now: datetime | None) -> int:
    now = now or datetime.now(TZ_BRT)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ_BRT)
    return 1 << now.astimezone(TZ_BRT).weekday()


def _in_range(start_m: int, end_m: int, now_m: int) -> bool:
    """Mesmo dia: 08:00-18:00. Overnight: 22:00-06:00 -> agora >= inicio OU agora < fim."""
    if start_m == end_m:
        return True
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m


def _minutes_to_end(start_m: int, end_m: int, now_m: int) -> int | None:
    if start_m == end_m:
        return None
    if start_m < end_m:
        if not (start_m <= now_m < end_m):
            return 0
        return end_m - now_m
    if now_m >= start_m:
        return (24 * 60 - now_m) + end_m
    if now_m < end_m:
        return end_m - now_m
    return 0


def _row_active(row: dict, wd_bit: int, now_m: int) -> bool:
    if not (int(row.get("days_mask") or 127) & wd_bit):
        return False
    start = _as_clock(row.get("start_time"))
    end = _as_clock(row.get("end_time"))
    if start is None or end is None:
        return False
    return _in_range(_clock_minutes(start), _clock_minutes(end), now_m)


def resolve_member_schedule(schedules: dict | None, address: str) -> list[dict]:
    """Herança de janelas: override total do analista, senão default do time.

    `schedules` vem de `db.team_schedules()`: chave `""` = default do time,
    demais chaves = override por analista (não soma com o default — substitui).
    """
    schedules = schedules or {}
    own = schedules.get(str(address or "")) or []
    if own:
        return own
    return schedules.get("") or []


def _in_route_window(member: dict, now: datetime | None = None, rows: list[dict] | None = None) -> bool:
    if rows:
        wd_bit = _weekday_bit(now)
        now_m = _now_minutes(now)
        shifts = [r for r in rows if str(r.get("kind") or "shift") == "shift"]
        breaks = [r for r in rows if str(r.get("kind") or "shift") == "break"]
        em_turno = True if not shifts else any(_row_active(r, wd_bit, now_m) for r in shifts)
        em_descanso = any(_row_active(r, wd_bit, now_m) for r in breaks)
        return em_turno and not em_descanso
    start = _as_clock(member.get("route_start"))
    end = _as_clock(member.get("route_end"))
    if start is None or end is None:
        return True
    return _in_range(_clock_minutes(start), _clock_minutes(end), _now_minutes(now))


def _minutes_to_window_end(
    member: dict, now: datetime | None = None, rows: list[dict] | None = None
) -> int | None:
    """Quantos minutos faltam para o fim da disponibilidade (None = 24h).

    A janela e a ESCALA, nao a presenca: quem sai as 18:00 nao deve receber um
    formulario novo as 17:57. Overnight (22:00-06:00) conta a virada do dia;
    leitura fora da janela devolve 0. Com `rows`, tambem conta o inicio do
    proximo descanso dentro do turno ativo (nao entregar em cima do descanso).
    """
    if rows:
        wd_bit = _weekday_bit(now)
        now_m = _now_minutes(now)
        shifts = [r for r in rows if str(r.get("kind") or "shift") == "shift"]
        breaks = [r for r in rows if str(r.get("kind") or "shift") == "break"]
        restos: list[int] = []
        if shifts:
            ativos = [r for r in shifts if _row_active(r, wd_bit, now_m)]
            if not ativos:
                return 0
            for r in ativos:
                falta = _minutes_to_end(_clock_minutes(_as_clock(r["start_time"])), _clock_minutes(_as_clock(r["end_time"])), now_m)
                if falta is not None:
                    restos.append(falta)
        # Chamada so chega aqui se o membro NAO esta em descanso agora (ver
        # `_in_route_window`, checado antes pelo `decide`): o inicio de cada
        # descanso de hoje esta sempre adiante, por isso o modulo e seguro.
        for r in breaks:
            if not (int(r.get("days_mask") or 127) & wd_bit):
                continue
            start = _as_clock(r.get("start_time"))
            if start is None:
                continue
            restos.append((_clock_minutes(start) - now_m) % (24 * 60))
        return min(restos) if restos else None
    start = _as_clock(member.get("route_start"))
    end = _as_clock(member.get("route_end"))
    if start is None or end is None:
        return None
    return _minutes_to_end(_clock_minutes(start), _clock_minutes(end), _now_minutes(now))


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


def _names(members: list[dict], *, windows: bool = False, rows_by_addr: dict | None = None) -> str:
    parts: list[str] = []
    for m in members:
        login = member_login_raw(_addr(m))
        if windows:
            rows = (rows_by_addr or {}).get(_addr(m))
            parts.append(f"{login}:{_window_label(m, rows)}")
        else:
            parts.append(login)
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
        # Afinidade NAO e excecao ao online: dono do caso offline nao recebe nada
        # (a chave `affinity_require_online` foi removida — permitia prender caso
        # em quem estava fora). `require_online` vale para todo mundo.
        "affinity_match_name": _to_bool(cfg.get("affinity_match_name"), False),
        # Formulario: para de ser OFERECIDO N minutos antes do fim da janela do
        # operador (a escala, nao a presenca). NULL no time = herda o global.
        "form_window_margin_min": max(
            _to_int(
                row.get("form_window_margin_min"),
                _to_int(cfg.get("form_window_margin_min"), 10),
            ),
            0,
        ),
        # Resgate por ausencia do operador: o time sobrescreve os segundos
        # (NULL = herda o global; 0 = este time nao devolve caso de analista
        # offline para a fila). O liga/desliga e global.
        "offline_requeue_sec": max(
            _to_int(row.get("offline_requeue_sec"), _to_int(cfg.get("offline_requeue_sec"), 120)), 0
        ),
        "offline_requeue_enabled": _to_bool(cfg.get("offline_requeue_enabled"), True),
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
    exclude: set[str] | None = None,
    schedules: dict | None = None,
) -> tuple[dict | None, str]:
    """Filtra e escolhe. Devolve (membro|None, detalhe legível para o log).

    `exclude` e a lista de enderecos que NUNCA podem receber: o proprio bot
    (`self.own`) e, no resgate, o analista que acabou de ficar offline.
    `schedules` e o resultado de `db.team_schedules()` (janelas de turno e
    descanso); sem ele, cai no legado `route_start`/`route_end` do membro.
    """
    kind = kind_of(kind)
    mrouting = mrouting or {}
    loads = loads or {}
    online = online or set()
    now = now or datetime.now(TZ_BRT)
    excluidos = [str(x).strip() for x in (exclude or set()) if str(x or "").strip()]

    props: list[dict] = []
    off_window: list[dict] = []
    fim_janela: list[dict] = []
    offline: list[dict] = []
    only_chat: list[dict] = []
    only_form: list[dict] = []
    over_cap: list[dict] = []
    cand: list[dict] = []
    weights: dict[str, int] = {}
    rows_by_addr: dict[str, list[dict]] = {}

    for m in members:
        addr = _addr(m)
        rows_by_addr[addr] = resolve_member_schedule(schedules, addr)
        # Alvo proibido (bot da casa): nao e elegivel por decisao nenhuma.
        if excluidos and any(addr_eq(addr, x) for x in excluidos):
            props.append(m)
            continue
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
        if settings.get("respect_window", True) and not _in_route_window(m, now, rows=rows_by_addr[addr]):
            off_window.append(m)
            continue
        # Fim de janela: nao entrega formulario novo a quem esta saindo (o caso
        # ficaria preso com um operador que ja foi embora, ou que ja vai entrar
        # em descanso).
        if kind == "form":
            margem = int(settings.get("form_window_margin_min") or 0)
            if margem > 0:
                falta = _minutes_to_window_end(m, now, rows=rows_by_addr[addr])
                if falta is not None and falta < margem:
                    fim_janela.append(m)
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
    fim = ",".join(
        f"{member_login_raw(_addr(m))}:{int(_minutes_to_window_end(m, now, rows=rows_by_addr.get(_addr(m))) or 0)}min"
        for m in fim_janela
    )
    detail = (
        f"time={team_name or '—'}; estrategia={strategy}; cap={settings.get('cap')}; "
        f"membros={_names(members)}; janela={_names(eligible, windows=True, rows_by_addr=rows_by_addr)}; "
        f"fora_horario={_names(off_window, windows=True, rows_by_addr=rows_by_addr)}; fim_janela={fim or '—'}; "
        f"sem_online={_names(offline)}; proprio_bot={_names(props)}; "
        f"so_chat={_names(only_chat)}; so_form={_names(only_form)}; no_cap={_names(over_cap)}; "
        f"casos={casos or '—'}; carga={carga or '—'}; tentativas={int(attempts_used)}"
    )
    if pick:
        return pick, f"{detail}; rr={last_name or '—'}→{pick_label}; {where}; last={last_assigned or '—'}"
    return None, detail

