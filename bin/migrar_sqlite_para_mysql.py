#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Copia `data/helptoa.sqlite` -> MySQL 8 (secao 4.9 do `sqliteformysql.md`).

Uso (na raiz do projeto):

    python bin/migrar_sqlite_para_mysql.py                     # dry-run: nao escreve nada
    python bin/migrar_sqlite_para_mysql.py --schema            # so o DDL (2x = idempotente)
    python bin/migrar_sqlite_para_mysql.py --apply             # DDL + dados (`INSERT IGNORE`)
    python bin/migrar_sqlite_para_mysql.py --apply --truncate  # esvazia antes e insere estrito
    python bin/migrar_sqlite_para_mysql.py --check             # contagens origem x destino
    python bin/migrar_sqlite_para_mysql.py --only chats,pending_routes
    python bin/migrar_sqlite_para_mysql.py --apply --mysql-database botsqlite_test

A origem abre em modo somente leitura (`mode=ro`) e o destino pode ser trocado
por `--mysql-database` sem mexer no `.env` - e assim que as fases 2 e 5 rodam
contra a base de TESTE, sem tocar na producao. As datas nao sao convertidas: as
colunas sao texto `'YYYY-MM-DD HH:MM:SS'` nos dois lados (mesmo formato, mesmo
fuso UTC), entao a copia e literal e nao ha deslocamento de hora.

Codigos de saida: 0 = ok, 1 = divergencia/falha, 2 = erro de uso ou de ambiente.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from python import sqlcompat  # noqa: E402
from python.config import load_env, mysql_dsn, mysql_missing_keys, sqlite_path  # noqa: E402

SCHEMA_PATH = ROOT / "schema.sql"
BATCH = 500


class Erro(Exception):
    """Falha que aborta a migracao com mensagem propria (sem traceback cru)."""


class Relatorio:
    """Linhas do relatorio: saem no stdout e (com `--out`) tambem no arquivo."""

    def __init__(self, destino: Path | None = None) -> None:
        self.linhas: list[str] = []
        self.destino = destino

    def __call__(self, texto: str = "") -> None:
        self.linhas.append(texto)
        print(texto, flush=True)

    def fecha(self) -> None:
        if self.destino:
            self.destino.write_text("\n".join(self.linhas) + "\n", encoding="utf-8")


def _dict_factory(cursor, row) -> dict:
    """Mesma forma de linha do bot (`db._dict_factory`), sem depender dele."""
    return {coluna[0]: row[i] for i, coluna in enumerate(cursor.description)}


# --------------------------------------------------------------------- origem


def abre_sqlite(caminho: Path):
    """Abre o SQLite de origem em modo somente leitura (nunca escreve de volta)."""
    import sqlite3

    if not caminho.is_file():
        raise Erro("origem SQLite nao encontrada: %s" % caminho)
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % caminho.as_posix(), uri=True)
    except sqlite3.OperationalError:        # caminho que o URI nao aceita
        conn = sqlite3.connect(str(caminho))
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def tabelas_sqlite(conn, only: Sequence[str] | None = None) -> list[str]:
    """Tabelas da origem na ordem de criacao (a ordem do `schema.sql`)."""
    linhas = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
    ).fetchall()
    nomes = [linha["name"] for linha in linhas]
    if not only:
        return nomes
    fora = [nome for nome in only if nome not in nomes]
    if fora:
        raise Erro("tabela(s) inexistente(s) na origem: %s" % ", ".join(fora))
    return [nome for nome in nomes if nome in set(only)]


def colunas_sqlite(conn, tabela: str) -> list[str]:
    return [coluna["name"] for coluna in conn.execute("PRAGMA table_info(%s)" % tabela)]


def colunas_mysql(conn, tabela: str) -> list[str]:
    linhas = conn.execute(
        "SELECT column_name AS nome FROM information_schema.columns"
        " WHERE table_schema = DATABASE() AND table_name = ?"
        " ORDER BY ordinal_position",
        (tabela,),
    ).fetchall()
    return [linha["nome"] for linha in linhas]


def colunas_da_carga(origem, destino, tabela: str, rel: Relatorio) -> list[str]:
    """Colunas que valem a copia: a origem manda, o destino filtra.

    Coluna que so existe na **origem** (legado morto, como `bot_jobs.payload`)
    nao tem para onde ir: entra como aviso no relatorio em vez de derrubar a
    carga com 1054. O contrario (coluna so no destino, criada por migracao nova)
    nao atrapalha: ela nasce com o default.
    """
    origem_cols = colunas_sqlite(origem, tabela)
    destino_cols = colunas_mysql(destino, tabela)
    if not destino_cols:
        raise Erro("tabela %s nao existe no destino" % tabela)
    conhecidas = {nome.lower() for nome in destino_cols}
    sobrando = [nome for nome in origem_cols if nome.lower() not in conhecidas]
    if sobrando:
        rel(
            "AVISO   %-26s coluna(s) so na origem, fora da carga: %s"
            % (tabela, ", ".join(sobrando))
        )
    return [nome for nome in origem_cols if nome.lower() in conhecidas]


def linhas_origem(conn, tabela: str) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM %s" % tabela).fetchone()["n"])


def valor_migravel(valor: Any, tabela: str, coluna: str) -> Any:
    """Copia literal; `bytes` nao viaja (o schema so tem coluna de texto)."""
    if isinstance(valor, bytes):
        try:
            return valor.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Erro("%s.%s tem BLOB que nao e UTF-8: %s" % (tabela, coluna, exc)) from exc
    return valor


# ------------------------------------------------------------------------ DDL


def sql_schema() -> str:
    if not SCHEMA_PATH.is_file():
        raise Erro("schema.sql nao encontrado em %s" % SCHEMA_PATH)
    return SCHEMA_PATH.read_text(encoding="utf-8")


def traduz_schema() -> tuple[list[str], int]:
    """Traduz o `schema.sql` inteiro sem banco (`SqlCompatError` = schema fora do tradutor)."""
    comandos = sqlcompat.split_statements(sql_schema())
    traduzidos = [sqlcompat.translate(comando)[0] for comando in comandos]
    return traduzidos, sum(texto.count("FOREIGN KEY") for texto in traduzidos)


def aplica_schema(conn, rel: Relatorio) -> int:
    """Cria/atualiza as tabelas com a mesma regra do `db.ensure_schema`.

    A checagem de FK fica desligada durante o script (o `schema.sql` cria as
    tabelas na ordem em que foi escrito, nao na ordem das FKs) e o
    `PRAGMA foreign_keys = ON` da linha 4 sai fora (`sem_pragma_fk`), senao ele
    religaria a checagem no meio do `executescript` (erro 1824). O `commit`
    antes do `SET` existe porque o MySQL ignora `FOREIGN_KEY_CHECKS` com
    transacao aberta.
    """
    sql = sql_schema()
    comandos = sqlcompat.split_statements(sql)
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(sqlcompat.sem_pragma_fk(sql))
    finally:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
    rel("DDL: %d comando(s) do schema.sql aplicados" % len(comandos))
    return len(comandos)


def conta_fks(conn) -> int:
    linha = conn.execute(
        "SELECT COUNT(*) AS n FROM information_schema.table_constraints"
        " WHERE constraint_schema = DATABASE() AND constraint_type = 'FOREIGN KEY'"
    ).fetchone()
    return int(linha["n"])


def tabelas_mysql(conn) -> list[str]:
    linhas = conn.execute(
        "SELECT table_name AS nome FROM information_schema.tables"
        " WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
        " ORDER BY table_name"
    ).fetchall()
    return [linha["nome"] for linha in linhas]


# ---------------------------------------------------------------------- dados


def _insert(tabela: str, colunas: Sequence[str], estrito: bool) -> str:
    verbo = "INSERT" if estrito else "INSERT IGNORE"
    return "%s INTO %s (%s) VALUES (%s)" % (
        verbo,
        tabela,
        ", ".join(colunas),
        ", ".join(["?"] * len(colunas)),
    )


def copia_tabela(
    origem,
    destino,
    tabela: str,
    rel: Relatorio,
    lote: int = BATCH,
    truncate: bool = False,
) -> dict:
    """Copia uma tabela em lotes de `lote` linhas, com `commit` no fim.

    Sem `--truncate` a carga e idempotente (`INSERT IGNORE`) e as linhas que ja
    existiam aparecem como `conflitos` (`lidas - gravadas`); com `--truncate` a
    tabela e esvaziada antes e a insercao e estrita (qualquer erro aborta).
    """
    colunas = colunas_da_carga(origem, destino, tabela, rel)
    total = linhas_origem(origem, tabela)
    if truncate:
        destino.execute("TRUNCATE TABLE %s" % tabela)
        destino.commit()
    if not total:
        return {"tabela": tabela, "lidas": 0, "gravadas": 0, "conflitos": 0}
    sql = _insert(tabela, colunas, estrito=truncate)
    lidas = gravadas = 0
    cursor = origem.execute("SELECT %s FROM %s" % (", ".join(colunas), tabela))
    while True:
        linhas = cursor.fetchmany(lote)
        if not linhas:
            break
        dados = [
            tuple(valor_migravel(linha[coluna], tabela, coluna) for coluna in colunas)
            for linha in linhas
        ]
        gravadas += int(destino.executemany(sql, dados).rowcount or 0)
        lidas += len(dados)
    destino.commit()
    return {
        "tabela": tabela,
        "lidas": lidas,
        "gravadas": gravadas,
        "conflitos": lidas - gravadas,
    }


def ajusta_autoincremento(conn, tabelas: Sequence[str], rel: Relatorio) -> int:
    """Contador de cada `id` em `MAX(id)+1` (a seed grava `id=1` explicito)."""
    linhas = conn.execute(
        "SELECT table_name AS tabela FROM information_schema.columns"
        " WHERE table_schema = DATABASE() AND extra LIKE '%%auto_increment%%'"
    ).fetchall()
    com_auto = {linha["tabela"] for linha in linhas}
    ajustadas = 0
    for tabela in tabelas:
        if tabela not in com_auto:
            continue
        maximo = conn.execute("SELECT MAX(id) AS m FROM %s" % tabela).fetchone()["m"]
        if maximo is None:
            continue
        conn.execute("ALTER TABLE %s AUTO_INCREMENT = %d" % (tabela, int(maximo) + 1))
        ajustadas += 1
    conn.commit()
    rel("AUTO_INCREMENT ajustado em %d tabela(s)" % ajustadas)
    return ajustadas


def checa_orfaos(conn, rel: Relatorio) -> int:
    """Confere cada FK gravada: filha apontando para pai inexistente e orfao."""
    linhas = conn.execute(
        "SELECT table_name AS filha, column_name AS coluna,"
        " referenced_table_name AS pai, referenced_column_name AS pai_col"
        " FROM information_schema.key_column_usage"
        " WHERE constraint_schema = DATABASE() AND referenced_table_name IS NOT NULL"
        " ORDER BY table_name, column_name"
    ).fetchall()
    orfaos = 0
    for fk in linhas:
        consulta = (
            "SELECT COUNT(*) AS n FROM `%(filha)s` c LEFT JOIN `%(pai)s` p"
            " ON c.`%(coluna)s` = p.`%(pai_col)s`"
            " WHERE c.`%(coluna)s` IS NOT NULL AND p.`%(pai_col)s` IS NULL"
            % fk
        )
        quantos = int(conn.execute(consulta).fetchone()["n"])
        if quantos:
            orfaos += 1
            rel(
                "ORFAO: %s.%s -> %s (%d linha(s))"
                % (fk["filha"], fk["coluna"], fk["pai"], quantos)
            )
    rel("FKs conferidas: %d | com orfao: %d" % (len(linhas), orfaos))
    return orfaos


# ---------------------------------------------------------------- conferencia


def compara(origem, destino, tabelas: Sequence[str], rel: Relatorio) -> int:
    """Contagens (e `MAX(id)`) tabela a tabela: origem x destino."""
    no_mysql = set(tabelas_mysql(destino))
    divergencias = 0
    for tabela in tabelas:
        na_origem = linhas_origem(origem, tabela)
        if tabela not in no_mysql:
            rel("FALTA   %-26s origem=%-6d (tabela ausente no MySQL)" % (tabela, na_origem))
            divergencias += 1
            continue
        consulta = destino.execute("SELECT COUNT(*) AS n FROM %s" % tabela).fetchone()
        no_destino = int(consulta["n"])
        if na_origem == no_destino:
            rel("ok      %-26s %d linha(s)" % (tabela, na_origem))
        else:
            rel("DIVERGE %-26s origem=%-6d destino=%-6d" % (tabela, na_origem, no_destino))
            divergencias += 1
        if "id" not in colunas_sqlite(origem, tabela):
            continue
        max_origem = origem.execute("SELECT MAX(id) AS m FROM %s" % tabela).fetchone()["m"]
        max_destino = destino.execute("SELECT MAX(id) AS m FROM %s" % tabela).fetchone()["m"]
        if max_origem != max_destino:
            rel("        MAX(id) diverge: origem=%s destino=%s" % (max_origem, max_destino))
            divergencias += 1
    return divergencias


def planeja(origem, caminho: Path, tabelas: Sequence[str], rel: Relatorio) -> int:
    """Dry-run: mostra o plano e valida a traducao do schema sem tocar em banco."""
    traduzidos, fks = traduz_schema()
    dsn = mysql_dsn()
    rel("origem : %s" % caminho)
    rel(
        "destino: mysql://%s@%s:%s/%s"
        % (dsn.get("user"), dsn.get("host"), dsn.get("port"), dsn.get("database"))
    )
    rel("schema : %d comando(s), %d FOREIGN KEY (traducao validada offline)" % (len(traduzidos), fks))
    rel("")
    rel("%-26s %8s" % ("tabela", "linhas"))
    rel("%-26s %8s" % ("-" * 26, "-" * 8))
    total = 0
    for tabela in tabelas:
        quantas = linhas_origem(origem, tabela)
        total += quantas
        rel("%-26s %8d" % (tabela, quantas))
    rel("%-26s %8s" % ("-" * 26, "-" * 8))
    rel("%-26s %8d" % ("TOTAL", total))
    rel("")
    rel("dry-run: nada foi escrito. Use --schema (so o DDL) ou --apply (DDL + dados).")
    return 0


# ----------------------------------------------------------------------- main


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrar_sqlite_para_mysql.py",
        description="Copia o SQLite do bot para o MySQL 8 (secao 4.9 do sqliteformysql.md).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "sem flag alguma o script e dry-run: lista as tabelas/linhas da origem e\n"
            "valida a traducao do schema.sql sem abrir conexao MySQL."
        ),
    )
    parser.add_argument("--sqlite", help="origem (padrao: SQLITE_PATH do .env)")
    parser.add_argument("--schema", action="store_true", help="aplica o DDL e para")
    parser.add_argument("--apply", action="store_true", help="aplica o DDL e copia os dados")
    parser.add_argument("--check", action="store_true", help="compara contagens origem x destino")
    parser.add_argument(
        "--truncate", action="store_true", help="esvazia cada tabela antes de copiar (com --apply)"
    )
    parser.add_argument("--only", help="tabelas separadas por virgula (padrao: todas)")
    parser.add_argument("--batch", type=int, default=BATCH, help="linhas por lote (padrao 500)")
    parser.add_argument(
        "--mysql-database",
        help="base destino (padrao: DATABASE_MYSQL do .env) - use a base de teste",
    )
    parser.add_argument("--out", help="grava o relatorio tambem neste arquivo")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    modos = [args.schema, args.apply, args.check]
    if sum(1 for modo in modos if modo) > 1:
        parser.error("--schema, --apply e --check sao exclusivos entre si")
    if args.truncate and not args.apply:
        parser.error("--truncate so vale com --apply")
    if args.batch < 1:
        parser.error("--batch precisa ser >= 1")
    apenas = [nome.strip() for nome in (args.only or "").split(",") if nome.strip()]

    load_env()
    if args.mysql_database:
        os.environ["DATABASE_MYSQL"] = args.mysql_database

    rel = Relatorio(Path(args.out).resolve() if args.out else None)
    origem = conn = None
    try:
        caminho = Path(args.sqlite).resolve() if args.sqlite else sqlite_path()
        origem = abre_sqlite(caminho)
        tabelas = tabelas_sqlite(origem, apenas)
        rel("migrar_sqlite_para_mysql | %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        if not any(modos):
            return planeja(origem, caminho, tabelas, rel)

        sem_valor = mysql_missing_keys()
        if sem_valor:
            raise Erro("credenciais do MySQL sem valor no .env: %s" % ", ".join(sem_valor))
        conn = sqlcompat.connect_mysql(row_factory=_dict_factory)
        cabecalho = conn.execute("SELECT VERSION() AS v, DATABASE() AS base").fetchone()
        rel("origem : %s (%d tabela(s))" % (caminho, len(tabelas)))
        rel("destino: MySQL %s / base %s" % (cabecalho["v"], cabecalho["base"]))
        rel("")

        problemas = 0
        orfaos = 0
        if args.schema or args.apply:
            aplica_schema(conn, rel)
            rel("FKs gravadas no destino: %d" % conta_fks(conn))
            ajusta_autoincremento(conn, tabelas, rel)
        if args.apply:
            problemas += _carga(origem, conn, tabelas, rel, args.batch, args.truncate)
            ajusta_autoincremento(conn, tabelas, rel)
            rel("")
            orfaos = checa_orfaos(conn, rel)
            problemas += orfaos
        if args.check or args.apply:
            rel("")
            rel("conferencia origem x destino:")
            problemas += compara(origem, conn, tabelas, rel)
        rel("")
        rel("FALHAS: %d" % problemas)
        return 1 if problemas else 0
    except (Erro, sqlcompat.SqlCompatError) as exc:
        rel("ERRO: %s" % exc)
        return 1
    except Exception as exc:                    # conexao, permissao, driver...
        rel("ERRO (%s.%s): %s" % (type(exc).__module__, type(exc).__name__, exc))
        return 1
    finally:
        if conn is not None:
            conn.close()
        if origem is not None:
            origem.close()
        rel.fecha()


def _carga(origem, conn, tabelas: Sequence[str], rel: Relatorio, lote: int, truncate: bool) -> int:
    """Copia todas as tabelas com a checagem de FK desligada (a ordem nao importa)."""
    rel(
        "carga: %d tabela(s), lote=%d, %s"
        % (len(tabelas), lote, "TRUNCATE + INSERT estrito" if truncate else "INSERT IGNORE")
    )
    rel("%-26s %8s %8s %9s" % ("tabela", "lidas", "gravadas", "conflitos"))
    rel("%-26s %8s %8s %9s" % ("-" * 26, "-" * 8, "-" * 8, "-" * 9))
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for tabela in tabelas:
            resultado = copia_tabela(origem, conn, tabela, rel, lote=lote, truncate=truncate)
            rel(
                "%-26s %8d %8d %9d"
                % (
                    resultado["tabela"],
                    resultado["lidas"],
                    resultado["gravadas"],
                    resultado["conflitos"],
                )
            )
    finally:
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
    # Conflito nao e falha: sem `--truncate` a carga e idempotente de proposito.
    # O gate de verdade e a conferencia (`--check`) e a checagem de orfaos.
    return 0


if __name__ == "__main__":
    sys.exit(main())
