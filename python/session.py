from __future__ import annotations

import secrets


def _dig(obj, *path):
    cur = obj
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def extract_ws_params(manage: dict, supervisor: dict) -> dict:
    # Replica extractWsParams de app/app.js
    cs = _dig(manage, "companySettings", "community") or {}
    k = cs.get("connectionSessionKey")
    raw_host = cs.get("connectionManager")
    if isinstance(raw_host, list):
        raw_host = raw_host[0] if raw_host else None
    host = None
    if raw_host:
        host = str(raw_host).replace("wss://", "").replace("ws://", "").split("/")[0]
    v = cs.get("connectionVersion") or cs.get("protocolVersion") or "01"
    u = (
        _dig(manage, "currentUser", "login_address")
        or _dig(manage, "currentUser", "address")
        or _dig(manage, "user", "login_address")
        or _dig(manage, "user", "address")
        or ""
    )
    sup = supervisor or {}
    s = _dig(sup, "settings", "s") or _dig(sup, "community", "s") or _dig(sup, "connection", "s") or sup.get("s")
    a = _dig(sup, "settings", "a") or _dig(sup, "community", "a") or _dig(sup, "connection", "a") or sup.get("a")
    c = _dig(sup, "settings", "c") or _dig(sup, "community", "c") or _dig(sup, "connection", "c") or sup.get("c")
    o = _dig(sup, "settings", "o") or _dig(sup, "community", "o") or _dig(sup, "connection", "o") or sup.get("o")
    if not all([k, host, s, a, c, o]):
        raise RuntimeError(f"parâmetros WS incompletos k={bool(k)} host={bool(host)} s={bool(s)} a={bool(a)} c={bool(c)} o={bool(o)}")
    return {"u": u, "k": k, "s": s, "a": a, "c": c, "o": o, "v": v, "host": host}


def build_ws_url(p: dict) -> str:
    r = str(secrets.randbelow(90_000_000) + 10_000_000)
    from urllib.parse import quote
    # Formato capturado no dump: a=bind&u=...&s=...&v=10&k=...&c=connect&r=...&o=mobility
    return (
        f"wss://{p['host']}/websocket/?a={quote(str(p['a']))}"
        f"&u={quote(str(p['u']))}&s={quote(str(p['s']))}&v={quote(str(p['v']))}"
        f"&k={quote(str(p['k']))}&c={quote(str(p['c']))}&r={r}&o={quote(str(p['o']))}"
    )
