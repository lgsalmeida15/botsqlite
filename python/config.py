from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# URLs cujos hosts (API interna na rede Claro) nunca podem passar por proxy.
NO_PROXY_URL_KEYS = (
    "APOIO_API_URL",
    "APOIO_STATUS_URL",
    "INFRA_REDE_API_URL",
    "REMANEJAMENTO_API_URL",
)


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


MYSQL_KEYS = ("IP_MYSQL", "PORTA_MYSQL", "USUARIO_MYSQL", "SENHA_MYSQL", "DATABASE_MYSQL")


def db_backend() -> str:
    """Backend do banco: `sqlite` (padrao) ou `mysql`.

    O bot continua escrito em dialeto SQLite; com `DB_BACKEND=mysql` o
    `db.connect()` entrega a conexao traduzida por `sqlcompat`. O rollback e
    trocar a linha do `.env` de volta para `sqlite` (ou remover a chave).
    """
    valor = (env("DB_BACKEND") or "sqlite").strip().lower()
    return "mysql" if valor == "mysql" else "sqlite"


def mysql_missing_keys() -> list[str]:
    """Chaves do MySQL declaradas no `.env` mas ainda sem valor."""
    return [k for k in MYSQL_KEYS if not (env(k) or "").strip()]


def mysql_dsn() -> dict:
    """Credenciais do MySQL do `.env` (nomes iguais aos que o painel PHP usa).

    Levanta `RuntimeError` quando falta valor: o erro aparece no boot com a lista
    exata das chaves, em vez de um "access denied" generico do driver.
    """
    faltando = mysql_missing_keys()
    if faltando:
        raise RuntimeError(
            "DB_BACKEND=mysql mas o .env esta sem valor em: " + ", ".join(faltando)
        )
    porta = (env("PORTA_MYSQL") or "3306").strip() or "3306"
    return {
        "host": (env("IP_MYSQL") or "").strip(),
        "port": int(porta),
        "user": (env("USUARIO_MYSQL") or "").strip(),
        "password": env("SENHA_MYSQL") or "",
        "database": (env("DATABASE_MYSQL") or "").strip(),
        "charset": "utf8mb4",
    }


def sqlite_path() -> Path:
    rel = env("SQLITE_PATH", "data/helptoa.sqlite") or "data/helptoa.sqlite"
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
