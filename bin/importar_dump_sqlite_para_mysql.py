#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Ponte: dump textual do SQLite (`sqlite3 .dump`) -> MySQL 8.

O integrador `bin/migrar_sqlite_para_mysql.py` copia um SQLite BINARIO
(`.sqlite`) para o MySQL 8. O que chega da producao, porem, costuma ser um dump
de TEXTO no formato do `sqlite3 .dump`:

    PRAGMA foreign_keys=OFF;
    BEGIN TRANSACTION;
    CREATE TABLE ... ;
    INSERT INTO <tabela> VALUES(...);
    COMMIT;

...que o integrador nao le: ele abre a origem via
`sqlite3.connect("file:...?mode=ro", uri=True)` e exige o arquivo binario.

Este script faz apenas a PONTE, em duas etapas:

 1. reconstroi um `.sqlite` temporario a partir do dump com o modulo `sqlite3`
    da biblioteca padrao (o CLI `sqlite3` NAO e requisito);
 2. chama o integrador existente sobre esse `.sqlite`.

Toda a inteligencia segue no integrador: traducao de dialeto pelo `sqlcompat`,
DDL a partir do `schema.sql`, `INSERT IGNORE` sem conflito, ajuste de
`AUTO_INCREMENT`, conferencia de contagens e de orfaos. Aqui nao se reimplementa
nada disso.

Uso (na raiz do projeto):

    python bin/importar_dump_sqlite_para_mysql.py                    # dry-run
    python bin/importar_dump_sqlite_para_mysql.py --rebuild-only     # so reconstroi o .sqlite
    python bin/importar_dump_sqlite_para_mysql.py --apply            # reconstroi + DDL + carga
    python bin/importar_dump_sqlite_para_mysql.py --check            # so confere origem x destino
    python bin/importar_dump_sqlite_para_mysql.py --dump outro.sql --apply
    python bin/importar_dump_sqlite_para_mysql.py --apply --only chats,pending_routes
    python bin/importar_dump_sqlite_para_mysql.py --apply --mysql-database botsqlite_test
    python bin/importar_dump_sqlite_para_mysql.py --apply --keep-tmp --out tmp_bak/rel.txt

Sem `--dump`, usa o `helptoa_dump*.sql` mais recente na raiz (ou o `SQLITE_DUMP`
do `.env`). O `.sqlite` temporario nasce em `data/_dump_from_sql.sqlite` (coberto
pelo `.gitignore`) e e removido no fim, salvo com `--keep-tmp` (ou `--rebuild-only`).

Conflito: por padrao a carga e `INSERT IGNORE` (idempotente, nao aborta em linha
repetida). ATENCAO (armadilha 32 do `sqliteformysql.md`): `INSERT IGNORE` NAO
sobrescreve o que ja existe - nas tabelas semeadas pelo `schema.sql`
(`templates`, `bot_settings`, `permission_groups`) o seed roda antes da carga e
vence. Com `--truncate` a tabela e esvaziada antes e a insercao e estrita.

Codigos de saida: 0 = ok, 1 = divergencia/falha, 2 = uso ou ambiente.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "bin")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from python.config import load_env  # noqa: E402

load_env()

import migrar_sqlite_para_mysql as integrador  # noqa: E402

DUMP_GLOB = "helptoa_dump*.sql"
TMP_PADRAO = ROOT / "data" / "_dump_from_sql.sqlite"

# Statements do dump que nao se reexecutam: o controle de transacao e as PRAGMAs
# sao reaplicados por este script (PRAGMA de carga so vale FORA de transacao).
_PULAR_PREFIXOS = ("PRAGMA", "BEGIN", "COMMIT", "END TRANSACTION", "START TRANSACTION")


class Erro(Exception):
    """Falha que aborta a ponte com mensagem propria (sem traceback cru)."""


def _encontra_dump(explicito: str | None) -> Path:
    """Resolve o caminho do dump: --dump > SQLITE_DUMP do .env > mais recente na raiz."""
    candidato = (explicito or os.environ.get("SQLITE_DUMP") or "").strip()
    if candidato:
        caminho = Path(candidato)
        if not caminho.is_absolute():
            caminho = ROOT / caminho
        if not caminho.is_file():
            raise Erro("dump nao encontrado: %s" % caminho)
        return caminho
    achados = sorted(ROOT.glob(DUMP_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    if not achados:
        raise Erro("nenhum dump %s na raiz; use --dump <arquivo.sql>" % DUMP_GLOB)
    return achados[0]


def _statements(stream):
    """Statements completos de um dump `.dump` (respeita literais multi-linha).

    O `sqlite3 .dump` emite um statement por vez terminado em `;`, mas strings
    podem conter quebras de linha. A regra: fecha quando a linha termina em `;`
    E o numero de aspas simples do acumulado e par (a aspa dentro de string vem
    dobrada, `''`, entao um statement fechado tem sempre um total par).
    """
    buffer: list[str] = []
    for linha in stream:
        buffer.append(linha)
        if linha.rstrip().endswith(";") and "".join(buffer).count("'") % 2 == 0:
            yield "".join(buffer)
            buffer = []
    resto = "".join(buffer).strip()
    if resto:
        yield resto


def reconstroi(dump: Path, destino: Path, rel) -> None:
    """Recria o SQLite (binario) a partir do dump textual, numa transacao so."""
    import sqlite3

    if destino.exists():
        destino.unlink()
    destino.parent.mkdir(parents=True, exist_ok=True)
    rel("dump     : %s (%.1f MB)" % (dump, dump.stat().st_size / (1024 * 1024)))
    rel("recriando: %s" % destino)

    conn = sqlite3.connect(str(destino))
    conn.isolation_level = None          # o controle de transacao e explicito aqui
    executados = pulados = erros = 0
    try:
        # PRAGMAs de carga (fora de transacao: PRAGMA nao vale dentro de BEGIN).
        for pragma in (
            "PRAGMA journal_mode = MEMORY",
            "PRAGMA synchronous = OFF",
            "PRAGMA temp_store = MEMORY",
            "PRAGMA foreign_keys = OFF",
        ):
            conn.execute(pragma)
        conn.execute("BEGIN")
        with dump.open("r", encoding="utf-8-sig", newline="") as fh:
            for sql in _statements(fh):
                texto = sql.strip()
                if not texto or texto.startswith("--"):
                    continue
                up = texto.upper()
                if up.startswith(_PULAR_PREFIXOS):
                    pulados += 1
                    continue
                # sqlite_sequence e interna: o CREATE dela sempre falha ("reserved
                # for internal use") e e desnecessario (o SQLite a cria sozinho ao
                # ver uma tabela AUTOINCREMENT). O DELETE/INSERT seguem valendo.
                if up.startswith("CREATE TABLE") and "SQLITE_SEQUENCE" in up:
                    pulados += 1
                    continue
                try:
                    conn.execute(texto)
                    executados += 1
                except sqlite3.Error as exc:
                    erros += 1
                    rel("AVISO   statement falhou: %s | %s" % (exc, texto[:90].replace("\n", " ")))
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise Erro("falha ao reconstruir o SQLite: %s" % exc) from exc
    finally:
        conn.close()

    rel("SQLite   : %d statement(s) executado(s), %d pulado(s), %d erro(s)" % (executados, pulados, erros))
    if erros:
        raise Erro("%d statement(s) do dump falharam (veja os AVISOS acima)" % erros)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="importar_dump_sqlite_para_mysql.py",
        description="Reconstroi o .sqlite a partir de um dump textual e chama o integrador MySQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "sem flag de modo o integrador roda em dry-run (lista as tabelas/linhas).\n"
            "a ponte apenas reconstroi o .sqlite antes de chamar o integrador."
        ),
    )
    parser.add_argument("--dump", help="dump textual (.sql) de origem (padrao: helptoa_dump*.sql mais recente)")
    parser.add_argument("--tmp", help="caminho do .sqlite temporario (padrao: data/_dump_from_sql.sqlite)")
    parser.add_argument("--rebuild", action="store_true", help="recria o .sqlite mesmo se ele ja existir")
    parser.add_argument("--rebuild-only", action="store_true", help="so reconstroi o .sqlite e para")
    parser.add_argument("--keep-tmp", action="store_true", help="nao apaga o .sqlite temporario no fim")
    parser.add_argument("--schema", action="store_true", help="(integrador) aplica o DDL e para")
    parser.add_argument("--apply", action="store_true", help="(integrador) DDL + carga (INSERT IGNORE)")
    parser.add_argument("--check", action="store_true", help="(integrador) compara contagens origem x destino")
    parser.add_argument("--truncate", action="store_true", help="(integrador) esvazia cada tabela antes de copiar")
    parser.add_argument("--only", help="(integrador) tabelas separadas por virgula")
    parser.add_argument("--batch", type=int, default=None, help="(integrador) linhas por lote (padrao 500)")
    parser.add_argument("--mysql-database", help="(integrador) base destino (padrao: DATABASE_MYSQL do .env)")
    parser.add_argument("--out", help="(integrador) grava o relatorio tambem neste arquivo")
    return parser


def _argv_integrador(tmp: Path, args) -> list[str]:
    """Monta os argumentos repassados ao integrador (respeita as exclusoes dele)."""
    argv = ["--sqlite", str(tmp)]
    if args.schema:
        argv.append("--schema")
    if args.apply:
        argv.append("--apply")
    if args.truncate:
        argv.append("--truncate")
    # `--apply` ja dispara a conferencia; o integrador recusa --apply + --check juntos.
    if args.check and not args.apply:
        argv.append("--check")
    if args.only:
        argv += ["--only", args.only]
    if args.batch is not None:
        argv += ["--batch", str(args.batch)]
    if args.mysql_database:
        argv += ["--mysql-database", args.mysql_database]
    if args.out:
        argv += ["--out", args.out]
    return argv


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.rebuild_only and (args.apply or args.schema or args.check):
        print("ERRO: --rebuild-only nao combina com --apply/--schema/--check")
        return 2

    linhas: list[str] = []

    def rel(texto: str = "") -> None:
        linhas.append(texto)
        print(texto, flush=True)

    tmp: Path | None = None
    rodou = False
    codigo = 1
    try:
        dump = _encontra_dump(args.dump)
        tmp = Path(args.tmp) if args.tmp else TMP_PADRAO
        if not tmp.is_absolute():
            tmp = ROOT / tmp
        rel("importar_dump_sqlite_para_mysql | %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        if args.rebuild or not tmp.is_file():
            reconstroi(dump, tmp, rel)
        else:
            rel("reutilizando: %s (use --rebuild para recriar)" % tmp)

        if args.rebuild_only:
            rel("rebuild-only: .sqlite pronto em %s" % tmp)
            codigo = 0
        else:
            rel("")
            rodou = True
            codigo = integrador.main(_argv_integrador(tmp, args))
    except Erro as exc:
        rel("ERRO: %s" % exc)
        codigo = 2
    finally:
        if tmp is not None and not (args.keep_tmp or args.rebuild_only):
            try:
                Path(tmp).unlink()
            except OSError:
                pass
        if args.out:
            destino = Path(args.out)
            destino.parent.mkdir(parents=True, exist_ok=True)
            anterior = destino.read_text(encoding="utf-8") if (rodou and destino.is_file()) else ""
            destino.write_text("\n".join(linhas) + "\n" + anterior, encoding="utf-8")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
