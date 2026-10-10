#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Watchdog de rotas orfas: solta a fila de chats que o bot perdeu.

Contexto (mapeado na base de producao em 2026-10-10):

O boot do bot (`engine.resume_open_chats`) marca como `left` os chats que
sumiram da lista ativa do TOA (`resume_stale`), mas NAO encerra a rota aberta
em `pending_routes`. A rota fica `waiting` para sempre: o tick continua
escolhendo analista e chamando `move_chat` num chat que o bot ja nao conduz, o
TOA recusa com `e=10` e a rota gira em loop (milhares de tentativas por rota).

Como o codigo de producao nao pode ser alterado, este script faz o papel que
falta: fecha as rotas orfas para o bot parar de martelar o TOA e a fila fluir.
Ele NAO apaga nada: so muda o `status` da `pending_routes` (o chat e o historico
seguem intactos) e, com `--report`, grava um arquivo com o que foi encerrado.

Escopo: SOMENTE rotas de `chat`. Rotas de `form` tem ciclo de vida proprio
(formulario de validacao nao e um chat que o bot entra) e ficam fora de
proposito — fechar por engano mataria o resgate por dono offline.

"Orfa" aqui e exatamente: `pending_routes.status='waiting'` + `kind='chat'` e o
chat apontado (a) nao existe na tabela `chats`, ou (b) esta num status fora do
conjunto aberto que o proprio bot usa para retomar
(`db.resume_chats`: joined, menu_sent, awaiting_reply, classified, monitoring,
awaiting_script_answer, awaiting_script_confirm, awaiting_script_run).

Uso (na raiz do projeto):

    python bin/watchdog_rotas_orfas.py                                   # dry-run (nao escreve)
    python bin/watchdog_rotas_orfas.py --apply                           # encerra as orfas (uma vez)
    python bin/watchdog_rotas_orfas.py --apply --watch                   # watchdog continuo (60s)
    python bin/watchdog_rotas_orfas.py --apply --watch --interval 30
    python bin/watchdog_rotas_orfas.py --apply --status cancelled
    python bin/watchdog_rotas_orfas.py --env .env.prod-20261006-1610 --apply
    python bin/watchdog_rotas_orfas.py --apply --report orfas.jsonl

Codigos de saida: 0 = ok, 1 = falha ao falar com o banco, 2 = erro de uso/ambiente.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from python import sqlcompat  # noqa: E402
from python.config import (  # noqa: E402
    db_backend,
    mysql_dsn,
    mysql_missing_keys,
    sqlite_path,
)

# Mesmo conjunto que `db.resume_chats` usa para retomar um chat. Tudo fora disso
# significa "o bot nao conduz mais este chat" — a assinatura da rota orfa.
STATUS_ABERTOS = (
    "joined",
    "menu_sent",
    "awaiting_reply",
    "classified",
    "monitoring",
    "awaiting_script_answer",
    "awaiting_script_confirm",
    "awaiting_script_run",
)

DETALHE_ORFA = "rota orfa: chat fora do TOA (watchdog rotas orfas)"

SQL_ORFAS = """
SELECT pr.id, pr.chat_id, pr.queue_address, pr.team_id, pr.attempts,
       pr.created_at, pr.claimed_by, pr.claim_expires_at, c.status AS chat_status
  FROM pending_routes pr
  LEFT JOIN chats c ON c.chat_id = pr.chat_id
 WHERE pr.status = 'waiting'
   AND pr.kind = 'chat'
   AND (c.chat_id IS NULL
        OR c.status NOT IN ('joined','menu_sent','awaiting_reply','classified',
                            'monitoring','awaiting_script_answer',
                            'awaiting_script_confirm','awaiting_script_run'))
 ORDER BY pr.id
"""

SQL_FECHA = """
UPDATE pending_routes
   SET status = ?, detail = ?, next_try_at = NULL, claimed_by = '', claim_expires_at = NULL
 WHERE id = ? AND status = 'waiting'
"""


class Erro(Exception):
    """Falha que aborta com mensagem propria (sem traceback cru)."""


def _dict_factory(cursor, row):
    cols = [c[0] for c in (cursor.description or [])]
    return {col: row[i] for i, col in enumerate(cols)}


def carrega_env(caminho: Path) -> None:
    """Le um `.env` e SOBREPOE as chaves no ambiente (sem apagar o resto).

    Diferente de `config.load_env` (que usa `setdefault`), aqui o arquivo manda:
    e assim que o operador aponta o script para `.env.prod-*` sem depender do
    que ja estava exportado no shell.
    """
    if not caminho.is_file():
        raise Erro("arquivo de ambiente nao encontrado: %s" % caminho)
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        chave = chave.strip()
        valor = valor.strip().strip("\"'")
        if chave:
            os.environ[chave] = valor


def conecta():
    """Abre a conexao do backend do `.env` (sqlite/mysql) SEM rodar DDL.

    Reusa o `sqlcompat` (mesmo dialeto do bot) mas evita `db.connect()`, que
    executaria o `schema.sql` inteiro a cada rodada do watchdog.
    """
    backend = db_backend()
    if backend == "mysql":
        faltando = mysql_missing_keys()
        if faltando:
            raise Erro("DB_BACKEND=mysql sem valor no .env em: " + ", ".join(faltando))
        conn = sqlcompat.connect(backend="mysql", dsn=mysql_dsn(), row_factory=_dict_factory)
    else:
        conn = sqlcompat.connect(backend="sqlite", sqlite_path=sqlite_path(), row_factory=_dict_factory)
    return conn, backend


def _parse_ts(valor):
    if not valor:
        return None
    texto = str(valor).strip().replace("T", " ")
    try:
        ts = datetime.fromisoformat(str(valor))
    except ValueError:
        try:
            ts = datetime.strptime(texto[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _claim_viva(row) -> bool:
    """A rota esta com uma entrega EM ANDAMENTO (lease valido)? Nao mexer nela."""
    if not str(row.get("claimed_by") or "").strip():
        return False
    expira = _parse_ts(row.get("claim_expires_at"))
    return expira is not None and expira > datetime.now(timezone.utc)


def _idade_seg(row) -> float:
    ts = _parse_ts(row.get("created_at"))
    if ts is None:
        return 0.0
    return (datetime.now(timezone.utc) - ts).total_seconds()


def coleta_orfas(conn, grace_sec: float, limit: int) -> tuple[list[dict], int]:
    """Devolve (orfas_para_fechar, quantas_foram_puladas_por_lease_viva)."""
    linhas = conn.execute(SQL_ORFAS).fetchall()
    selecionadas: list[dict] = []
    com_lease = 0
    for row in linhas:
        if _claim_viva(row):
            com_lease += 1
            continue
        if grace_sec > 0 and _idade_seg(row) < grace_sec:
            continue
        selecionadas.append(row)
        if limit and len(selecionadas) >= limit:
            break
    return selecionadas, com_lease


def roda_uma_vez(conn, args) -> dict:
    orfas, com_lease = coleta_orfas(conn, args.grace_sec, args.limit)
    fechadas: list[dict] = []
    if args.apply:
        for row in orfas:
            cur = conn.execute(
                SQL_FECHA, (args.status, DETALHE_ORFA, int(row["id"]))
            )
            if int(cur.rowcount or 0) == 1:
                registro = {
                    "id": int(row["id"]),
                    "chat_id": int(row["chat_id"]),
                    "queue_address": row.get("queue_address"),
                    "team_id": row.get("team_id"),
                    "attempts": row.get("attempts"),
                    "chat_status": row.get("chat_status"),
                    "created_at": str(row.get("created_at") or ""),
                    "novo_status": args.status,
                    "quando": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                }
                fechadas.append(registro)
                if args.report:
                    with args.report.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
        conn.commit()
    return {
        "orfas": len(orfas),
        "fechadas": len(fechadas) if args.apply else 0,
        "com_lease": com_lease,
        "amostra": (orfas if not args.apply else fechadas)[: args.show],
    }


def _imprime(resumo: dict, args, backend: str) -> None:
    marca = time.strftime("%Y-%m-%d %H:%M:%S")
    acao = "FECHADAS" if args.apply else "detectadas"
    print(
        "[%s] backend=%s  orfas %s=%d (lease viva ignorada=%d)  limite=%s"
        % (marca, backend, acao, resumo["orfas"], resumo["com_lease"], args.limit or "sem"),
        flush=True,
    )
    if args.verbose:
        for r in resumo["amostra"]:
            print(
                "    id=%s chat=%s fila=%s chat_status=%s attempts=%s criada=%s"
                % (
                    r.get("id"),
                    r.get("chat_id"),
                    r.get("queue_address"),
                    r.get("chat_status"),
                    r.get("attempts"),
                    r.get("created_at"),
                ),
                flush=True,
            )
    if not args.apply and resumo["orfas"]:
        print("    (dry-run: nada foi escrito; use --apply para encerrar)", flush=True)
    if args.apply and resumo["fechadas"]:
        print("    %d rota(s) encerrada(s) como '%s'." % (resumo["fechadas"], args.status), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Encerra rotas orfas (chat ja saiu/sumiu) para o roteamento voltar a fluir.",
    )
    ap.add_argument("--env", default=".env", help="arquivo .env a usar (padrao: .env)")
    ap.add_argument("--apply", action="store_true", help="escreve no banco (padrao: dry-run)")
    ap.add_argument(
        "--status", default="failed", choices=("failed", "cancelled"),
        help="status final da rota orfa (padrao: failed)",
    )
    ap.add_argument("--watch", action="store_true", help="roda continuamente (watchdog)")
    ap.add_argument("--interval", type=float, default=60.0, help="segundos entre rodadas no --watch")
    ap.add_argument("--grace-sec", type=float, default=0.0, help="ignora rotas criadas ha menos de N s")
    ap.add_argument("--limit", type=int, default=500, help="maximo de rotas por rodada (0 = sem limite)")
    ap.add_argument("--report", type=Path, default=None, help="grava JSONL do que foi encerrado")
    ap.add_argument("--show", type=int, default=20, help="linhas de amostra no --verbose")
    ap.add_argument("--verbose", action="store_true", help="lista a amostra de rotas")
    args = ap.parse_args(argv)

    try:
        carrega_env(Path(args.env) if Path(args.env).is_absolute() else ROOT / args.env)
    except Erro as e:
        print("erro: %s" % e, file=sys.stderr)
        return 2

    while True:
        try:
            conn, backend = conecta()
        except Erro as e:
            print("erro: %s" % e, file=sys.stderr)
            return 2
        except Exception as e:  # conexao indisponivel
            print("erro ao conectar: %s" % e, file=sys.stderr)
            return 1
        try:
            resumo = roda_uma_vez(conn, args)
            _imprime(resumo, args, backend)
        except Exception as e:
            print("erro durante a rodada: %s" % e, file=sys.stderr)
            return 1
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not args.watch:
            return 0
        try:
            time.sleep(max(args.interval, 1.0))
        except KeyboardInterrupt:
            print("", flush=True)
            return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
