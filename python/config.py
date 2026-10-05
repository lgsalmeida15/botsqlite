from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# URLs cujos hosts (API interna na rede Claro) nunca podem passar por proxy.
NO_PROXY_URL_KEYS = ("APOIO_API_URL", "APOIO_STATUS_URL", "INFRA_REDE_API_URL")


def load_env() -> None:
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    bypass_proxy_for_internal_apis()


def internal_api_hosts() -> list[str]:
    """Hosts das APIs internas declaradas no .env (sem duplicatas)."""
    hosts: list[str] = []
    for key in NO_PROXY_URL_KEYS:
        raw = (env(key) or "").strip()
        if not raw:
            continue
        host = urlsplit(raw).hostname
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def is_internal_api(url: str) -> bool:
    """True quando a URL aponta para uma das APIs internas do .env."""
    host = urlsplit(str(url or "")).hostname
    return bool(host) and host in internal_api_hosts()


def bypass_proxy_for_internal_apis() -> list[str]:
    """Tira os hosts da API interna do proxy de ambiente (HTTP_PROXY/NO_PROXY).

    O VS Code injeta HTTP_PROXY/HTTPS_PROXY/NO_PROXY no terminal integrado
    (settings http.proxy + http.proxySupport=override) e o bot roda nesse
    terminal. Como o urllib honra essas variaveis, as chamadas para a API
    interna eram roteadas ao proxy local, que respondia 503 sem corpo depois
    de ~15s. Aqui os hosts internos sao acrescentados a NO_PROXY, sem remover
    o que ja existia (o webhook externo segue podendo usar o proxy).
    """
    hosts = internal_api_hosts()
    if not hosts:
        return []
    atuais = [
        p.strip()
        for p in (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",")
        if p.strip()
    ]
    for host in hosts:
        if host not in atuais:
            atuais.append(host)
    valor = ",".join(atuais)
    os.environ["NO_PROXY"] = valor
    os.environ["no_proxy"] = valor
    return hosts


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def sqlite_path() -> Path:
    rel = env("SQLITE_PATH", "data/helptoa.sqlite") or "data/helptoa.sqlite"
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
