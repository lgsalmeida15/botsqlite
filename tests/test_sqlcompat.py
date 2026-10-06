# -*- coding: utf-8 -*-
"""Shim SQLite -> MySQL (Fase 1): traducao do SQL e fachada da conexao.

Roda sem servidor MySQL: os testes de traducao sao puros e a fachada usa um
duplo do PyMySQL. O backend SQLite continua intocado (`BackendSqliteTest`).
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db, sqlcompat  # noqa: E402

CARIMBO = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def epoch(carimbo: str) -> float:
    """Segundos desde 1970 de um carimbo UTC do projeto."""
    return datetime.strptime(carimbo, sqlcompat.TS_FMT).replace(tzinfo=timezone.utc).timestamp()


def perto(carimbo: str, deslocamento: float = 0.0, tolerancia: float = 5.0) -> bool:
    """True quando o carimbo esta a `deslocamento` segundos do agora (UTC)."""
    return abs(epoch(carimbo) - (time.time() + deslocamento)) < tolerancia


# --- lint de TEXT/BLOB em chave, indice ou FK (o MySQL recusa: erro 1170) ----

_TIPO = re.compile(r"^\s*(?P<col>[A-Za-z_]\w*)\s+(?P<tipo>TEXT|BLOB)\b", re.I)
_TABELA = re.compile(r"\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)\s*\(", re.I)
_INDICE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+\w+\s+ON\s+(\w+)\s*\(([^)]*)\)",
    re.I,
)
_CHAVE = re.compile(r"\b(?:PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|KEY|INDEX)\s*\(([^)]*)\)", re.I)
_CASO = re.compile(r"\bTHEN\s+(\w+)\s+END", re.I)
_SO_COLUNA = re.compile(r"^\w+(?:\s+(?:ASC|DESC))?$", re.I)
_DDL_NO_PY = re.compile(r"\bCREATE\s+(?:TABLE|(?:UNIQUE\s+)?INDEX)\b", re.I)
_FK = re.compile(r"FOREIGN\s+KEY\s*\((\w+)\)\s*REFERENCES\s+(\w+)\s*\((\w+)\)", re.I)
_COLACAO_NA_COLUNA = re.compile(
    r"(?m)^\s*`?(?P<col>\w+)`?\s+\w+(?:\(\d+\))?\s+COLLATE\s+utf8mb4_bin\b", re.I
)


def _corpo(sql: str) -> str:
    """Texto entre os parenteses externos do `CREATE TABLE`/`INDEX`."""
    abre = sql.find("(")
    if abre < 0:
        return ""
    return sql[abre + 1 : sqlcompat._match_paren(sql, abre)]


def _colunas_da_chave(texto: str) -> list[str]:
    """Colunas citadas numa especificacao de chave.

    Cobre o `((CASE WHEN ... THEN col END))` do indice parcial: quando o `CASE`
    devolve a propria coluna, ela faz parte da chave.
    """
    fora: list[str] = []
    for pedaco in texto.split(","):
        fora.extend(_CASO.findall(pedaco))
        limpo = pedaco.strip().strip("()").strip()
        if _SO_COLUNA.match(limpo):
            fora.append(limpo.split()[0])
    return fora


def _colunas_bin(sql: str) -> set[str]:
    """Colunas que sairam com `COLLATE utf8mb4_bin` no `CREATE TABLE` (4.6.8)."""
    return {casado.group("col") for casado in _COLACAO_NA_COLUNA.finditer(sql)}


def _criados_por_tabela(comandos: list[str]) -> dict[str, str]:
    """`CREATE TABLE` **traduzido** por nome de tabela (o resto do script fica fora)."""
    tabelas: dict[str, str] = {}
    for comando in comandos:
        achado = _TABELA.search(comando)
        if achado:
            tabelas[achado.group(1)] = sqlcompat.translate(comando)[0]
    return tabelas


def _tipos_das_tabelas(comandos: list[str]) -> dict[str, dict[str, str]]:
    tipos: dict[str, dict[str, str]] = {}
    for sql in comandos:
        achado = _TABELA.search(sql)
        if not achado:
            continue
        colunas: dict[str, str] = {}
        for linha in _corpo(sql).split("\n"):
            casado = _TIPO.match(linha.strip().rstrip(","))
            if casado:
                colunas[casado.group("col")] = casado.group("tipo").upper()
        tipos[achado.group(1)] = colunas
    return tipos


def _lint(comandos: list[str], tipos: dict[str, dict[str, str]]) -> list[tuple[str, str, str]]:
    """`(tabela, coluna, motivo)` de cada TEXT/BLOB dentro de chave/indice/FK."""
    achados: list[tuple[str, str, str]] = []
    for sql in comandos:
        tabela = _TABELA.search(sql)
        if tabela:
            nome = tabela.group(1)
            colunas = tipos.get(nome, {})
            for chave in _CHAVE.finditer(_corpo(sql)):
                for coluna in _colunas_da_chave(chave.group(1)):
                    if colunas.get(coluna) in ("TEXT", "BLOB"):
                        achados.append((nome, coluna, "chave: %s" % chave.group(0)[:32]))
            continue
        indice = _INDICE.search(sql)
        if indice:
            colunas = tipos.get(indice.group(1), {})
            for coluna in _colunas_da_chave(indice.group(2)):
                if colunas.get(coluna) in ("TEXT", "BLOB"):
                    achados.append((indice.group(1), coluna, "CREATE INDEX"))
    return sorted(set(achados))


# O unico valor que o shim aceita como modificador de tempo nas DUAS formas do
# projeto que **interpretam** o parametro: `datetime('now', '-' || ? || ' seconds')`
# (o shim faz `float(param)`, entao quer um numero) e `datetime('now', ?)`
# (`modifier_seconds(param)`, que aceita numero sem unidade como segundos). Um
# modificador com unidade (`'-60 seconds'`) morre no `float`; um nao-numerico
# (`None`) morre no `modifier_seconds` do caminho direto. A terceira forma,
# `datetime(<coluna>, ?)`, so repassa o valor para o `CONVERT_TZ`: qualquer um serve.
_VALOR_DE_MODIFICADOR = "60"


def _params_do_lint(sql: str) -> tuple:
    """Um valor por marcador `?`: o lint so traduz, nunca executa.

    O valor nao importa — **exceto** onde o shim interpreta o parametro (o
    modificador de `datetime('now', ...)`): ai o lint injeta
    `_VALOR_DE_MODIFICADOR` em vez de `None`, senao o lint estoura por ruido e
    nao por SQL ruim. A cobertura disso e
    `TempoTest.test_valor_do_lint_atravessa_as_tres_formas`.
    """
    return (_VALOR_DE_MODIFICADOR,) * sqlcompat._mask(sql).count("?")


def _sentencas(texto: str, gatilho: re.Pattern) -> list[str]:
    """Comandos soltos no modulo (constantes de texto) que casam o `gatilho`.

    Le as strings por `ast` (sem importar o modulo), separa em comandos e traduz
    cada um: e o que permite lintar o SQL que so existe dentro do `db.py`.
    """
    fora: list[str] = []
    nos = sorted(ast.walk(ast.parse(texto)), key=lambda n: getattr(n, "lineno", 0))
    for no in nos:
        if not (isinstance(no, ast.Constant) and isinstance(no.value, str)):
            continue
        if not gatilho.search(no.value):
            continue
        for pedaco in sqlcompat.split_statements(no.value):
            if gatilho.search(pedaco):
                fora.append(sqlcompat.translate(pedaco, _params_do_lint(pedaco))[0])
    return fora


def _ddl_do_py(texto: str) -> list[str]:
    """Comandos de DDL soltos no modulo (constantes de texto), em ordem."""
    return _sentencas(texto, _DDL_NO_PY)


def _lint_texto(texto: str) -> list[tuple[str, str, str]]:
    """Lint de um texto SQL solto (o `schema.sql` inteiro, no teste)."""
    comandos = [sqlcompat.translate(c)[0] for c in sqlcompat.split_statements(texto)]
    return _lint(comandos, _tipos_das_tabelas(comandos))


def _comandos_do_projeto() -> list[str]:
    """DDL do `schema.sql` + do `db.py`, em ordem (schema primeiro, como o boot)."""
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    modulo = (ROOT / "python" / "db.py").read_text(encoding="utf-8")
    return [sqlcompat.translate(c)[0] for c in sqlcompat.split_statements(schema)] + _ddl_do_py(
        modulo
    )


def _lint_projeto() -> list[tuple[str, str, str]]:
    """Lint do projeto inteiro, com um unico mapa de tipos.

    Por arquivo o lint nao ve o `CREATE INDEX` do `db.py` sobre uma tabela que
    nasce no `schema.sql` — foi assim que `pending_routes.kind` (TEXT, no indice
    parcial `pending_routes_active`) escapou ate o boot.
    """
    comandos = _comandos_do_projeto()
    return _lint(comandos, _tipos_das_tabelas(comandos))


# --- lint de subquery sobre a propria tabela alvo (o MySQL recusa: erro 1093)


_SUBQUERY = re.compile(r"\(\s*SELECT\b", re.I)
_ALVO = re.compile(r"^\s*(?:UPDATE|DELETE\s+FROM)\s+(?P<tabela>[A-Za-z_]\w*)", re.I)
_DML_NO_PY = re.compile(r"\b(?:UPDATE|DELETE)\b", re.I)
_PARENTESE_OU_FROM = re.compile(r"[()]|\bFROM\s+([A-Za-z_]\w*|\()", re.I)


def _fonte_do_topo(sql: str, inicio: int, fim: int) -> str:
    """Tabela do primeiro `FROM` no nivel de topo do trecho `sql[inicio:fim]`.

    `FROM (` devolve `(`, que e a marca de derivada: quem le a propria tabela
    dentro de uma derivada nao dispara o 1093.
    """
    profundidade = 0
    for casado in _PARENTESE_OU_FROM.finditer(sql, inicio, fim):
        texto = casado.group(0)
        if texto == "(":
            profundidade += 1
        elif texto == ")":
            profundidade -= 1
        elif profundidade == 0:
            return (casado.group(1) or "").lower()
    return ""


def _lint_1093(comandos: list[str]) -> list[tuple[str, str]]:
    """`(tabela, subquery)` de cada leitura da propria tabela alvo do DML.

    O MySQL **nao materializa** a subquery: `UPDATE/DELETE t ... WHERE x IN
    (SELECT ... FROM t)` morre com 1093 (`You can't specify target table ... for
    update in FROM clause`). Embrulhar numa derivada com alias
    (`FROM (SELECT ... FROM t) AS d`) forca a materializacao — e o mesmo SQL
    roda no SQLite.
    """
    achados: list[tuple[str, str]] = []
    for sql in comandos:
        alvo = _ALVO.match(sql)
        if not alvo:
            continue
        tabela = alvo.group("tabela").lower()
        for sub in _SUBQUERY.finditer(sql):
            fecha = sqlcompat._match_paren(sql, sub.start())
            if fecha < 0:
                continue
            if sql[: sub.start()].rstrip().upper().endswith("FROM"):
                continue  # corpo de derivada: materializada antes do DML
            if _fonte_do_topo(sql, sub.end(), fecha) == tabela:
                achados.append((tabela, " ".join(sub.group(0).split())))
    return achados


def _dml_do_py() -> list[str]:
    """Comandos de DML (`UPDATE`/`DELETE`) soltos no `db.py`, ja traduzidos.

    So as strings literais do modulo entram: SQL montado por f-string/campo nao e
    `ast.Constant` e escapa da varredura (mesma limitacao do lint de TEXT). A
    varredura ampla de `tmp_bak/lint_1093_projeto.py` mostrou que nenhum outro
    modulo de `python/` carrega DML — por isso o escopo e o `db.py`.
    """
    modulo = (ROOT / "python" / "db.py").read_text(encoding="utf-8")
    return _sentencas(modulo, _DML_NO_PY)


def _lint_1093_do_projeto() -> list[tuple[str, str]]:
    """Lint do `db.py` (mesmo escopo do lint de TEXT: e onde vive o DML)."""
    return _lint_1093(_dml_do_py())


class TempoTest(unittest.TestCase):
    """`datetime()`/`julianday()`: o instante UTC nasce na aplicacao (5.4)."""

    def test_now_vira_parametro_utc(self):
        sql, params = sqlcompat.translate(
            "UPDATE chats SET updated_at = datetime('now') WHERE chat_id = ?", (7,)
        )
        self.assertEqual(sql, "UPDATE chats SET updated_at = %s WHERE chat_id = %s")
        self.assertTrue(CARIMBO.match(params[0]), params)
        self.assertTrue(perto(params[0]), params)
        self.assertEqual(params[1], 7)

    def test_now_com_modificador_literal(self):
        sql, params = sqlcompat.translate(
            "SELECT * FROM route_attempts WHERE at >= datetime('now', '-1 day')"
        )
        self.assertEqual(sql, "SELECT * FROM route_attempts WHERE at >= %s")
        self.assertEqual(len(params), 1)
        self.assertTrue(perto(params[0], -86400), params)

    def test_now_com_modificador_por_parametro(self):
        sql, params = sqlcompat.translate(
            "SELECT * FROM route_attempts WHERE at >= datetime('now', ?)", ("-7 days",)
        )
        self.assertEqual(sql, "SELECT * FROM route_attempts WHERE at >= %s")
        self.assertEqual(len(params), 1, "o modificador tem de ser consumido")
        self.assertTrue(perto(params[0], -7 * 86400), params)

    def test_now_mais_segundos_por_parametro(self):
        sql, params = sqlcompat.translate(
            "INSERT INTO pending_routes (next_try_at)"
            " VALUES (datetime('now', '+' || ? || ' seconds'))",
            (90,),
        )
        self.assertEqual(sql, "INSERT INTO pending_routes (next_try_at) VALUES (%s)")
        self.assertEqual(len(params), 1)
        self.assertTrue(perto(params[0], 90), params)

    def test_now_menos_segundos_por_parametro(self):
        sql, params = sqlcompat.translate(
            "SELECT * FROM chats"
            " WHERE datetime(script_started_at) < datetime('now', '-' || ? || ' seconds')",
            (600,),
        )
        self.assertEqual(sql, "SELECT * FROM chats WHERE (script_started_at) < %s")
        self.assertEqual(len(params), 1)
        self.assertTrue(perto(params[0], -600), params)

    def test_valor_do_lint_atravessa_as_tres_formas(self):
        """O valor sintetico do lint serve as TRES formas de `datetime(...)` do projeto.

        `_params_do_lint` injeta valor onde o `db.py` passa o prazo: **numero** na forma
        concatenada (`'-' || ? || ' seconds'`, o shim faz `float`), **modificador** na
        direta (`datetime('now', ?)`, `modifier_seconds`) e **offset de fuso** em
        `datetime(<coluna>, ?)` — nesta terceira o shim so repassa o valor para o
        `CONVERT_TZ`, entao qualquer um serve. Um valor que sirva so a uma delas faz o
        lint estourar por ruido, e nao por SQL ruim.
        """
        formas = (
            ("SELECT 1 FROM chats WHERE datetime(at) >= datetime('now', ?)", 60),
            (
                "SELECT 1 FROM chats"
                " WHERE datetime(offline_since) <= datetime('now', '-' || ? || ' seconds')",
                -60,
            ),
        )
        for sql, deslocamento in formas:
            with self.subTest(sql=sql):
                traduzido, params = sqlcompat.translate(sql, _params_do_lint(sql))
                self.assertNotIn("?", traduzido)
                self.assertEqual(len(params), 1, params)
                self.assertTrue(CARIMBO.match(params[0]), params)
                self.assertTrue(perto(params[0], deslocamento), params)

        sql = "SELECT 1 FROM chats WHERE datetime(updated_at, ?) <= datetime('now')"
        with self.subTest(sql=sql):
            traduzido, params = sqlcompat.translate(sql, _params_do_lint(sql))
            self.assertIn("CONVERT_TZ(updated_at, '+00:00', %s)", traduzido)
            self.assertEqual(params[0], _VALOR_DE_MODIFICADOR)
            self.assertTrue(CARIMBO.match(params[1]), params)

    def test_datetime_de_coluna_so_perde_a_envoltoria(self):
        sql, _ = sqlcompat.translate(
            "SELECT * FROM chats"
            " WHERE datetime(script_started_at) < datetime('now')"
        )
        self.assertIn("(script_started_at) < %s", sql)

    def test_offset_de_fuso_vira_convert_tz(self):
        sql, params = sqlcompat.translate(
            "SELECT datetime(at, ?) FROM analyst_load", ("-03:00",)
        )
        self.assertEqual(sql, "SELECT CONVERT_TZ(at, '+00:00', %s) FROM analyst_load")
        self.assertEqual(params, ["-03:00"])

    def test_julianday_vira_timestampdiff(self):
        sql, params = sqlcompat.translate(
            "SELECT *, (julianday('now') - julianday(at)) * 86400 AS idade"
            " FROM analyst_load WHERE address = ?",
            ("u:ana@claro.com",),
        )
        self.assertEqual(
            sql,
            "SELECT *, TIMESTAMPDIFF(SECOND, at, %s) AS idade"
            " FROM analyst_load WHERE address = %s",
        )
        self.assertTrue(perto(params[0]))
        self.assertEqual(params[1], "u:ana@claro.com")

    def test_julianday_com_max(self):
        sql, _ = sqlcompat.translate(
            "SELECT s.queue_address, (julianday('now') - julianday(MAX(e.at))) * 86400 AS seg"
            " FROM sessions s GROUP BY s.queue_address"
        )
        self.assertIn("TIMESTAMPDIFF(SECOND, MAX(e.at), %s) AS seg", sql)

    def test_modificador_sem_unidade_falha_alto(self):
        with self.assertRaises(sqlcompat.SqlCompatError):
            sqlcompat.translate("SELECT * FROM t WHERE at > datetime('now', '-1 year')")

    def test_modificador_numerico_aceita_zero(self):
        """`datetime('now', ?)` com `0` (int) segue o mesmo caminho de `"0"`."""
        self.assertEqual(sqlcompat.modifier_seconds(0), 0.0)
        self.assertEqual(sqlcompat.modifier_seconds("0"), 0.0)
        self.assertEqual(sqlcompat.modifier_seconds(-90), -90.0)
        sql, params = sqlcompat.translate("SELECT datetime('now', ?)", (0,))
        self.assertEqual(sql, "SELECT %s")
        self.assertTrue(perto(params[0]))

    def test_modificador_ausente_falha_alto(self):
        for valor in (None, "", "  ", "ontem"):
            with self.assertRaises(sqlcompat.SqlCompatError):
                sqlcompat.modifier_seconds(valor)

    def test_shift_stamp_respeita_o_sinal(self):
        self.assertTrue(perto(sqlcompat.shift_stamp(-30), -30))
        self.assertTrue(perto(sqlcompat.shift_stamp(30), 30))


class MarcadorTest(unittest.TestCase):
    """`?` -> `%s` e `%` literal -> `%%` (5.5a, armadilha 15)."""

    def test_marcadores_viram_porcento_s(self):
        sql, params = sqlcompat.translate("SELECT * FROM t WHERE a = ? AND b = ?", (1, "x"))
        self.assertEqual(sql, "SELECT * FROM t WHERE a = %s AND b = %s")
        self.assertEqual(params, [1, "x"])

    def test_porcento_escapa_so_com_parametro(self):
        sql, params = sqlcompat.translate(
            "SELECT * FROM chats WHERE case_key LIKE 'u:%' AND chat_id = ?", (10,)
        )
        self.assertIn("case_key LIKE 'u:%%'", sql)
        self.assertEqual(params, [10])
        sem_param = sqlcompat.translate("SELECT * FROM chats WHERE case_key LIKE 'u:%'")[0]
        self.assertIn("case_key LIKE 'u:%'", sem_param)

    def test_interrogacao_dentro_de_literal_nao_e_marcador(self):
        sql, params = sqlcompat.translate("SELECT * FROM t WHERE nome = 'a?b' AND id = ?", (5,))
        self.assertIn("nome = 'a?b'", sql)
        self.assertEqual(params, [5])

    def test_contagem_de_parametro_errada_falha_alto(self):
        with self.assertRaises(sqlcompat.SqlCompatError):
            sqlcompat.translate("SELECT * FROM t WHERE a = ? AND b = ?", (1,))

    def test_parametros_de_data_hora_viram_carimbo(self):
        quando = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(sqlcompat.convert_params((quando,)), ["2026-10-05 12:00:00"])
        self.assertEqual(sqlcompat.convert_params((True,)), [1])


class IgualdadeNulaTest(unittest.TestCase):
    """`x IS ?` -> `x <=> %s`: a igualdade que nao perde NULL (regra 26).

    O SQLite aceita `IS` com valor (nao so `IS NULL`) e o MySQL nao: sem a regra
    a consulta de `db.queue_position` (`db.py:1263`) morre com 1064 no boot.
    """

    def test_is_com_valor_vira_null_safe(self):
        sql, params = sqlcompat.translate(
            "SELECT COUNT(*) AS n FROM pending_routes"
            " WHERE status = 'waiting' AND id < ? AND (team_id IS ?) AND kind = 'chat'",
            (828, 1),
        )
        self.assertIn("(team_id <=> %s)", sql)
        self.assertEqual(params, [828, 1])

    def test_is_com_null_tambem_vira_null_safe(self):
        sql, params = sqlcompat.translate("SELECT 1 FROM t WHERE team_id IS ?", (None,))
        self.assertEqual(sql, "SELECT 1 FROM t WHERE team_id <=> %s")
        self.assertEqual(params, [None])

    def test_is_not_com_parametro_nega_o_null_safe(self):
        sql, params = sqlcompat.translate("SELECT 1 FROM t WHERE team_id IS NOT ?", (3,))
        self.assertEqual(sql, "SELECT 1 FROM t WHERE NOT (team_id <=> %s)")
        self.assertEqual(params, [3])

    def test_is_null_e_is_not_null_seguem_como_estao(self):
        sql, params = sqlcompat.translate(
            "SELECT 1 FROM t WHERE a IS NULL AND b IS NOT NULL", None
        )
        self.assertEqual(sql, "SELECT 1 FROM t WHERE a IS NULL AND b IS NOT NULL")
        self.assertIsNone(params)

    def test_is_em_literal_ou_comentario_nao_e_tocado(self):
        sql, _ = sqlcompat.translate(
            "SELECT 1 FROM t WHERE nome = 'x IS ?' AND nome2 = ? -- y IS ?", ("a",)
        )
        self.assertIn("nome = 'x IS ?'", sql)
        self.assertIn("-- y IS ?", sql)

    def test_is_com_coluna_entre_crases(self):
        """A mascara apaga o conteudo entre crases: a regra nao pode pular a coluna."""
        sql, _ = sqlcompat.translate("SELECT 1 FROM t WHERE `team_id` IS ?", (1,))
        self.assertEqual(sql, "SELECT 1 FROM t WHERE `team_id` <=> %s")

    def test_is_com_operador_composto_falha_alto(self):
        with self.assertRaises(sqlcompat.SqlCompatError):
            sqlcompat.translate("SELECT 1 FROM t WHERE `t`.`c` IS ?", (1,))


class DmlTest(unittest.TestCase):
    """Upsert e concatenacao do SQLite para o do MySQL (5.2)."""

    def test_insert_or_ignore(self):
        sql, _ = sqlcompat.translate(
            "INSERT OR IGNORE INTO group_teams (group_id, team_id) VALUES (?, ?)", (1, 2)
        )
        self.assertEqual(
            sql, "INSERT IGNORE INTO group_teams (group_id, team_id) VALUES (%s, %s)"
        )

    def test_insert_or_replace_falha_alto(self):
        with self.assertRaises(sqlcompat.SqlCompatError):
            sqlcompat.translate("INSERT OR REPLACE INTO queues (address) VALUES (?)", ("h:q1",))

    def test_upsert_vira_on_duplicate_key(self):
        sql, params = sqlcompat.translate(
            "INSERT INTO queues (address, name) VALUES (?, ?)\n"
            "   ON CONFLICT (address) DO UPDATE\n"
            "   SET name = COALESCE(NULLIF(excluded.name, ''), queues.name),\n"
            "       last_seen = datetime('now')",
            ("h:q1", "Q1"),
        )
        self.assertIn("AS new ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("COALESCE(NULLIF(new.name, ''), queues.name)", sql)
        self.assertNotIn("excluded.", sql)
        self.assertNotIn("ON CONFLICT", sql)
        self.assertEqual(params[:2], ["h:q1", "Q1"])
        self.assertTrue(perto(params[2]), params)

    def test_do_nothing_vira_no_op(self):
        sql, _ = sqlcompat.translate(
            "INSERT INTO t (a, b) VALUES (?, ?) ON CONFLICT (a) DO NOTHING", (1, 2)
        )
        self.assertEqual(
            sql, "INSERT INTO t (a, b) VALUES (%s, %s) ON DUPLICATE KEY UPDATE a = a"
        )

    def test_upsert_do_bot_settings_escapa_a_coluna_key(self):
        sql, _ = sqlcompat.translate(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?)\n"
            "   ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            ("rbac_seeded", "0"),
        )
        self.assertIn("(`key`, value)", sql)
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertNotIn("`KEY`", sql)
        self.assertNotIn("ON CONFLICT", sql)

    def test_key_dentro_de_literal_fica_intacto(self):
        sql, _ = sqlcompat.translate(
            "UPDATE bot_settings SET value = ? WHERE key = 'chave_key_velha'", ("1",)
        )
        self.assertIn("'chave_key_velha'", sql)
        self.assertIn("`key` =", sql)

    def test_concatenacao_vira_concat(self):
        sql, _ = sqlcompat.translate(
            "UPDATE chats SET case_key = 'a:' || case_key WHERE case_key LIKE 'u:%'"
        )
        self.assertEqual(
            sql, "UPDATE chats SET case_key = CONCAT('a:', case_key) WHERE case_key LIKE 'u:%'"
        )

    def test_begin_immediate_vira_start_transaction(self):
        self.assertEqual(sqlcompat.translate("BEGIN IMMEDIATE")[0], "START TRANSACTION")


class DdlTest(unittest.TestCase):
    """DDL do `schema.sql` e das migracoes do `db.py` (5.1/5.3)."""

    def test_autoincrement_e_default_de_agora(self):
        sql, _ = sqlcompat.translate(
            "CREATE TABLE t (\n"
            "   id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "   at TEXT NOT NULL DEFAULT (datetime('now'))\n"
            ")"
        )
        self.assertIn("id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY", sql)
        self.assertIn("DEFAULT (UTC_TIMESTAMP())", sql)
        self.assertNotIn("datetime('now')", sql)

    def test_chave_texto_vira_varchar(self):
        """`TEXT` em chave vira `VARCHAR(191)` e a chave leva a colacao `_bin`.

        O `=`/`UNIQUE` do SQLite e *case-sensitive*: sem `COLLATE utf8mb4_bin` o
        destino (`ai_ci`) trataria `Ana` e `ana` como a mesma chave (4.6.8/5.5c).
        """
        sql, _ = sqlcompat.translate(
            "CREATE TABLE queues (address TEXT PRIMARY KEY, name TEXT NOT NULL)"
        )
        self.assertIn("address VARCHAR(191) COLLATE utf8mb4_bin PRIMARY KEY", sql)
        self.assertIn("name TEXT NOT NULL", sql)
        self.assertNotIn("name TEXT COLLATE", sql)

    def test_colacao_bin_nas_tres_origens_de_igualdade(self):
        """Chave, filha de FK e a lista do 5.5c levam `COLLATE utf8mb4_bin`.

        `detail` fica no default (`ai_ci`): o 4.6.8 so marca igualdade/chave, e a
        busca `LIKE '%termo%'` do painel conta com o `ai_ci`.
        """
        sql, _ = sqlcompat.translate(
            "CREATE TABLE t (\n"
            "   id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "   address TEXT NOT NULL,\n"
            "   status TEXT NOT NULL DEFAULT 'waiting',\n"
            "   queue_address TEXT NOT NULL REFERENCES queues(address),\n"
            "   detail TEXT NOT NULL DEFAULT '',\n"
            "   UNIQUE (address)\n"
            ")"
        )
        self.assertIn("address TEXT COLLATE utf8mb4_bin NOT NULL", sql)
        self.assertIn("status TEXT COLLATE utf8mb4_bin NOT NULL DEFAULT ('waiting')", sql)
        self.assertIn("queue_address TEXT COLLATE utf8mb4_bin NOT NULL", sql)
        self.assertIn("detail TEXT NOT NULL DEFAULT ('')", sql)
        self.assertNotIn("detail TEXT COLLATE", sql)

    def test_colacao_do_alter_so_na_coluna_de_igualdade(self):
        """`ALTER ... ADD COLUMN` da migracao do `db.py` tambem recebe a colacao.

        `queue_operators.source` (lista do 5.5c) nasce por `ALTER` em banco antigo;
        sem a regra a coluna do banco migrado ficaria `ai_ci` e divergiria do
        schema novo. A coluna fora da lista fica intocada.
        """
        sql, _ = sqlcompat.translate(
            "ALTER TABLE queue_operators ADD COLUMN source TEXT NOT NULL DEFAULT 'toa'"
        )
        self.assertIn("ADD COLUMN source TEXT COLLATE utf8mb4_bin NOT NULL DEFAULT ('toa')", sql)
        sql, _ = sqlcompat.translate(
            "ALTER TABLE analyst_load ADD COLUMN casos_toa_keys TEXT NOT NULL DEFAULT ''"
        )
        self.assertIn("casos_toa_keys TEXT NOT NULL DEFAULT ('')", sql)
        self.assertNotIn("casos_toa_keys TEXT COLLATE", sql)

    def test_default_de_texto_ganha_parenteses(self):
        sql, _ = sqlcompat.translate(
            "ALTER TABLE analyst_load ADD COLUMN casos_toa_keys TEXT NOT NULL DEFAULT ''"
        )
        self.assertIn("DEFAULT ('')", sql)

    def test_default_literal_nao_vazio_ganha_parenteses(self):
        """TEXT nao aceita default literal (erro 1101): so como expressao.

        O caso vazio ja era tratado; `DEFAULT 'toa'`/`'{}'`/`'both'` do
        `schema.sql` e das migracoes paravam o boot no MySQL.
        """
        sql, _ = sqlcompat.translate(
            "ALTER TABLE queue_operators ADD COLUMN source TEXT NOT NULL DEFAULT 'toa'"
        )
        self.assertIn("DEFAULT ('toa')", sql)
        self.assertNotIn("DEFAULT 'toa'", sql)

    def test_identificador_key_vira_coluna_escapada(self):
        """`key` e reservada no MySQL: `bot_settings (key, value)` nao compila.

        O `KEY` de `PRIMARY KEY`/`FOREIGN KEY`/`ON DUPLICATE KEY UPDATE` tem de
        continuar cru.
        """
        sql, _ = sqlcompat.translate(
            "CREATE TABLE bot_settings (\n"
            "   key TEXT PRIMARY KEY,\n"
            "   value TEXT NOT NULL\n"
            ")"
        )
        self.assertIn("`key` VARCHAR(191) COLLATE utf8mb4_bin PRIMARY KEY", sql)
        self.assertIn("PRIMARY KEY", sql)
        self.assertNotIn("PRIMARY `KEY`", sql)

    def test_rename_de_tabela(self):
        self.assertEqual(
            sqlcompat.translate("ALTER TABLE menu_options_new RENAME TO menu_options")[0],
            "RENAME TABLE menu_options_new TO menu_options",
        )

    def test_alter_add_column_com_references_ganha_fk_explicita(self):
        sql, _ = sqlcompat.translate(
            "ALTER TABLE chats ADD COLUMN menu_node_id INTEGER"
            " REFERENCES menu_options(id) ON DELETE SET NULL"
        )
        self.assertIn("ADD COLUMN menu_node_id BIGINT NULL", sql)
        self.assertIn(
            "ADD CONSTRAINT fk_chats_menu_node_id FOREIGN KEY (menu_node_id)"
            " REFERENCES menu_options(id) ON DELETE SET NULL",
            sql,
        )

    def test_pragma_table_info(self):
        sql, _ = sqlcompat.translate("PRAGMA table_info(menu_options)")
        self.assertIn("information_schema.columns", sql)
        self.assertIn("c.TABLE_NAME = 'menu_options'", sql)
        self.assertIn("AS `notnull`", sql)

    def test_pragma_sem_equivalente_nao_executa_nada(self):
        self.assertEqual(sqlcompat.translate("PRAGMA journal_mode = WAL")[0], "")
        self.assertEqual(sqlcompat.translate("PRAGMA busy_timeout = 5000")[0], "")

    def test_pragma_foreign_keys(self):
        self.assertEqual(
            sqlcompat.translate("PRAGMA foreign_keys = OFF")[0], "SET FOREIGN_KEY_CHECKS = 0"
        )
        self.assertEqual(
            sqlcompat.translate("PRAGMA foreign_keys = ON")[0], "SET FOREIGN_KEY_CHECKS = 1"
        )

    def test_pragma_de_fk_do_script_sai_para_o_mysql(self):
        """O `PRAGMA foreign_keys` do `schema.sql` nao pode valer no MySQL.

        `db.ensure_schema` desliga a checagem antes do `executescript` (as FKs do
        `schema.sql` nascem fora de ordem: `bot_users` -> `permission_groups`,
        que nasce depois). O PRAGMA do proprio script religava a checagem no meio
        da execucao e o boot morria em 1824 (`Failed to open the referenced
        table`) - medido no 8.0.44 local. No SQLite o PRAGMA continua valendo.
        """
        self.assertEqual(
            sqlcompat.sem_pragma_fk(
                "-- cabecalho\nPRAGMA foreign_keys = ON;\nPRAGMA foreign_keys = OFF;\n"
                "CREATE TABLE t (id INTEGER);"
            ),
            "-- cabecalho\nCREATE TABLE t (id INTEGER);",
        )
        self.assertIn("PRAGMA", sqlcompat.sem_pragma_fk("PRAGMA journal_mode = WAL;"))

    def test_sqlite_master_lista_tabelas(self):
        sql, _ = sqlcompat.translate("SELECT name FROM sqlite_master WHERE type = 'table'")
        self.assertIn("information_schema.tables", sql)
        self.assertIn("table_type = 'BASE TABLE'", sql)

    def test_sqlite_master_alem_de_listar_falha_alto(self):
        with self.assertRaises(sqlcompat.SqlCompatError):
            sqlcompat.translate(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'menu_options'"
            )
        with self.assertRaises(sqlcompat.SqlCompatError):
            sqlcompat.translate(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'x'"
            )

    def test_references_inline_vira_constraint_de_tabela(self):
        """`REFERENCES` na coluna e ignorado pelo MySQL (5.1, regra 4).

        Sem esta regra o `CREATE TABLE` do `schema.sql` nascia no MySQL **sem FK
        nenhuma**: a referencia sumia em silencio (medido: 31 tabelas, 0 FKs).
        Agora cada uma vira `CONSTRAINT fk_<tabela>_<coluna> FOREIGN KEY` no fim
        do corpo, e a coluna `INTEGER` da ponta filha vira `BIGINT` (o id do
        SQLite nasce `BIGINT NOT NULL AUTO_INCREMENT`). A ponta filha de **texto**
        tambem leva `COLLATE utf8mb4_bin`: o InnoDB exige a mesma colacao das duas
        pontas (erro 3780) e o pai de uma FK de texto e sempre chave.
        """
        sql, _ = sqlcompat.translate(
            "CREATE TABLE menu_queues (\n"
            "   menu_id INTEGER NOT NULL REFERENCES menus(id) ON DELETE CASCADE,\n"
            "   queue_address VARCHAR(191) NOT NULL REFERENCES queues(address)"
            " ON DELETE CASCADE,\n"
            "   PRIMARY KEY (menu_id, queue_address),\n"
            "   UNIQUE (queue_address)\n"
            ")"
        )
        self.assertIn("menu_id BIGINT NOT NULL", sql)
        self.assertNotIn("INTEGER", sql)
        self.assertIn("queue_address VARCHAR(191) COLLATE utf8mb4_bin NOT NULL", sql)
        self.assertIn(
            "CONSTRAINT fk_menu_queues_menu_id FOREIGN KEY (menu_id)"
            " REFERENCES menus(id) ON DELETE CASCADE",
            sql,
        )
        self.assertIn(
            "CONSTRAINT fk_menu_queues_queue_address FOREIGN KEY (queue_address)"
            " REFERENCES queues(address) ON DELETE CASCADE",
            sql,
        )
        self.assertIn("PRIMARY KEY (menu_id, queue_address)", sql)
        self.assertIn("UNIQUE (queue_address)", sql)
        self.assertEqual(sql.upper().count("REFERENCES"), 2)

    def test_fk_inline_sem_acao_nao_inventa_on_delete(self):
        """`REFERENCES t(c)` sem acao vira FK sem `ON DELETE` (o InnoDB usa RESTRICT)."""
        sql, _ = sqlcompat.translate(
            "CREATE TABLE menu_options (\n"
            "   id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "   transfer_target_id INTEGER REFERENCES transfer_targets(id),\n"
            "   parent_id INTEGER REFERENCES menu_options(id) ON DELETE CASCADE\n"
            ")"
        )
        self.assertIn(
            "CONSTRAINT fk_menu_options_transfer_target_id FOREIGN KEY"
            " (transfer_target_id) REFERENCES transfer_targets(id)",
            sql,
        )
        self.assertNotIn("transfer_targets(id) ON DELETE", sql)
        self.assertIn("fk_menu_options_parent_id", sql)


    def test_table_ddl_sai_do_shim_por_backend(self):
        """`table_ddl` e o unico jeito de ler o `CREATE`: `sqlite_master`/`SHOW`."""
        with tempfile.TemporaryDirectory() as pasta:
            conn = sqlcompat.connect(
                sqlite_path=Path(pasta) / "t.sqlite",
                backend="sqlite",
                row_factory=db._dict_factory,
            )
            try:
                conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT)")
                ddl = sqlcompat.table_ddl(conn, "t")
                self.assertIn("CREATE TABLE t", ddl)
                self.assertIn("AUTOINCREMENT", ddl)
                self.assertEqual(sqlcompat.table_ddl(conn, "nao_existe"), "")
            finally:
                conn.close()

        bruta = _ConexaoBruta()
        bruta.linhas = [("t", "CREATE TABLE `t` (\n  `action` varchar(20)\n)")]
        mysql = sqlcompat._MySQLConnection(bruta, row_factory=db._dict_factory)
        self.assertEqual(sqlcompat.table_ddl(mysql, "t"), bruta.linhas[0][1])
        self.assertIn(("SHOW CREATE TABLE `t`", None), bruta.recebidos)

        class _SemTabela:
            def cursor(self):
                return self

            def execute(self, sql, params=None):
                raise sqlcompat.ProgrammingError(1146, "Table 'd.t' doesn't exist")

            def fetchone(self):
                return None

            def close(self):
                pass

        ausente = sqlcompat._MySQLConnection(_SemTabela())
        self.assertEqual(sqlcompat.table_ddl(ausente, "t"), "")


class SchemaCompletoTest(unittest.TestCase):
    """O `schema.sql` inteiro passa pelo tradutor, comando por comando."""

    def setUp(self):
        self.comandos = sqlcompat.split_statements(
            (ROOT / "schema.sql").read_text(encoding="utf-8")
        )

    def test_divide_em_comandos(self):
        self.assertGreater(len(self.comandos), 10)
        self.assertTrue(all(c.strip() for c in self.comandos))

    def test_todo_comando_traduz_sem_sobra(self):
        proibidos = ("AUTOINCREMENT", "datetime('now')", "sqlite_", "ON CONFLICT", "PRAGMA")
        for comando in self.comandos:
            sql, _ = sqlcompat.translate(comando)
            self.assertTrue(sql, comando[:80])
            for proibido in proibidos:
                self.assertNotIn(proibido, sql, comando[:80])

    def test_chaves_de_texto_viraram_varchar(self):
        criados = [c for c in self.comandos if "PRIMARY KEY" in c.upper()]
        traduzidos = [sqlcompat.translate(c)[0] for c in criados]
        self.assertTrue(
            any("VARCHAR(191) COLLATE utf8mb4_bin PRIMARY KEY" in sql for sql in traduzidos)
        )

    def test_pragma_de_fk_do_schema_sai_no_mysql(self):
        """O `schema.sql` mantem o PRAGMA (o SQLite precisa dele) e o MySQL nao.

        Quem desliga a checagem no MySQL e o `db.ensure_schema`; o PRAGMA do
        script (linha 4) religava a checagem no meio do `executescript`.
        """
        texto = (ROOT / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("PRAGMA foreign_keys = ON", texto)
        self.assertNotIn("PRAGMA", sqlcompat.sem_pragma_fk(texto))

    def test_nenhuma_referencia_inline_sobra(self):
        """Nenhuma coluna do `schema.sql` traduzido guarda `REFERENCES` (5.1, regra 4).

        `REFERENCES` so pode sobrar dentro de um `FOREIGN KEY (...)` de tabela -
        na coluna o MySQL descarta a clausula em silencio.
        """
        criados = [sqlcompat.translate(c)[0] for c in self.comandos]
        criados = [sql for sql in criados if "CREATE TABLE" in sql.upper()]
        self.assertGreater(len(criados), 20)
        for sql in criados:
            corpo = _corpo(sql)
            itens = sqlcompat._split_args(corpo, sqlcompat._mask(corpo))
            for item in itens:
                if sqlcompat._ITEM_DE_TABELA.match(item):
                    continue
                self.assertNotIn("REFERENCES", item.upper(), sql[:60])

    def test_colacao_bin_na_pk_do_queue_operators(self):
        """O `queue_operators` do gate da Fase 5 nasce com a PK `_bin` (era o 1062).

        A origem real guarda `u:N5932208@clarosa` e `u:n5932208@clarosa` na **mesma**
        fila: o `=` do SQLite e BINARY e aceita as duas, o `utf8mb4_0900_ai_ci` do
        MySQL colapsa as duas na chave primaria — `Duplicate entry
        'u:N5932208@clarosa' for key 'queue_operators.PRIMARY'` (erro 1062) no
        `LOAD DATA`/`INSERT` da migracao.
        """
        por_tabela = _criados_por_tabela(self.comandos)
        operadores = por_tabela["queue_operators"]
        self.assertIn("queue_address VARCHAR(191) COLLATE utf8mb4_bin NOT NULL", operadores)
        self.assertIn("address VARCHAR(191) COLLATE utf8mb4_bin NOT NULL", operadores)
        self.assertIn("PRIMARY KEY (queue_address, address)", operadores)
        self.assertIn(
            "address VARCHAR(191) COLLATE utf8mb4_bin PRIMARY KEY", por_tabela["queues"]
        )
        self.assertIn(
            "`key` VARCHAR(191) COLLATE utf8mb4_bin PRIMARY KEY", por_tabela["bot_settings"]
        )
        # Coluna comum segue no default: o `LIKE` do painel depende do `ai_ci`.
        self.assertNotIn("COLLATE", por_tabela["teams"])

    def test_colacao_das_pontas_de_fk_bate(self):
        """Toda FK entre colunas de texto tem a mesma colacao nas duas pontas.

        O InnoDB recusa o par com colacoes diferentes (erro 3780: filha `ai_ci`
        contra pai `_bin` falha). Pai de FK de texto e sempre chave, entao a filha
        tem de sair `_bin` junto — e e isso que a regra das tres origens garante.
        """
        por_tabela = _criados_por_tabela(self.comandos)
        bin_por_tabela = {t: _colunas_bin(sql) for t, sql in por_tabela.items()}
        total = 0
        for tabela, sql in por_tabela.items():
            for filha, pai, referenciada in _FK.findall(sql):
                total += 1
                self.assertEqual(
                    filha in bin_por_tabela[tabela],
                    referenciada in bin_por_tabela.get(pai, set()),
                    "%s.%s -> %s.%s" % (tabela, filha, pai, referenciada),
                )
        self.assertGreater(total, 20)

    def test_contagem_de_fk_bate_com_o_schema(self):
        """Cada `REFERENCES` inline do schema virou um `FOREIGN KEY` explicito.

        A contagem e o teste que fecha o furo medido (31 tabelas, 0 FKs): o
        `SHOW CREATE TABLE` do MySQL nao teria o que mostrar sem esta regra.
        """
        inline = sum(
            len(sqlcompat._REFERENCES_INLINE.findall(sqlcompat._mask(c))) for c in self.comandos
        )
        fks = sum(sqlcompat.translate(c)[0].count("FOREIGN KEY") for c in self.comandos)
        self.assertGreater(inline, 20)
        self.assertEqual(fks, inline)


class TextEmChaveTest(unittest.TestCase):
    """Lint do projeto: nenhuma coluna TEXT/BLOB em chave, indice ou FK.

    O MySQL recusa com "BLOB/TEXT column ... used in key specification without a
    key length" (1170), e recusa no boot — o `CREATE TABLE` inteiro nao entra.
    A regra do shim cobre a chave declarada **inline** na coluna
    (`TEXT PRIMARY KEY`/`TEXT ... UNIQUE` -> `VARCHAR(191)`) e o `REFERENCES`
    inline (que agora vira `FOREIGN KEY` de tabela, regra 4 da 5.1). O que sobra
    para o lint e o texto **ja traduzido**: chave composta, `CREATE INDEX` e a
    coluna TEXT/BLOB que entrou numa FK.
    """

    def test_projeto_sem_text_em_chave(self):
        """`schema.sql` + `db.py` num mapa de tipos so (cobre indice cruzado)."""
        achados = _lint_projeto()
        self.assertEqual(achados, [], "TEXT/BLOB em chave: %s" % (achados,))

    def test_lint_acusa_os_tres_padroes(self):
        """Nao pode passar por vacuidade: a amostra ruim tem de ser acusada.

        O `zz2` e a referencia inline: traduzida, a coluna `e` (TEXT) entra numa
        `FOREIGN KEY (e)` — o lint tem de acusar ai (o MySQL recusa com 1170).
        """
        amostra = (
            "CREATE TABLE zz (\n"
            "  a TEXT NOT NULL,\n"
            "  b TEXT NOT NULL,\n"
            "  c TEXT NOT NULL,\n"
            "  PRIMARY KEY (a, b));\n"
            "CREATE INDEX zz_ix ON zz (c);\n"
            "CREATE TABLE zz2 (\n"
            "  e TEXT NOT NULL REFERENCES zz(a));\n"
        )
        achados = _lint_texto(amostra)
        self.assertEqual(
            achados,
            [
                ("zz", "a", "chave: PRIMARY KEY (a, b)"),
                ("zz", "b", "chave: PRIMARY KEY (a, b)"),
                ("zz", "c", "CREATE INDEX"),
                ("zz2", "e", "chave: FOREIGN KEY (e)"),
            ],
        )

    def test_lint_ignora_coluna_varchar(self):
        amostra = (
            "CREATE TABLE zz (\n"
            "  a VARCHAR(191) NOT NULL,\n"
            "  PRIMARY KEY (a));\n"
            "CREATE INDEX zz_ix ON zz (a);\n"
        )
        self.assertEqual(_lint_texto(amostra), [])


class SubqueryNaPropriaTabelaTest(unittest.TestCase):
    """Lint do projeto: `UPDATE`/`DELETE` sem subquery na propria tabela alvo.

    O MySQL recusa com 1093 (`You can't specify target table ... for update in
    FROM clause`) porque nao materializa a subquery. O SQLite aceita a forma
    antiga — e nem o teste de unidade com driver falso nem o backend SQLite
    denunciam: so o driver real, no boot. A derivada com alias resolve nos dois.
    """

    def test_projeto_sem_subquery_na_propria_tabela(self):
        """`db.py`: `load_trim_history` e `_migrate_delivery` (5.6)."""
        self.assertGreater(len(_dml_do_py()), 20, "a varredura do `db.py` ficou vazia")
        achados = _lint_1093_do_projeto()
        self.assertEqual(achados, [], "subquery na propria tabela alvo: %s" % (achados,))

    def test_lint_acusa_as_tres_formas(self):
        """Nao pode passar por vacuidade: `IN`, `EXISTS` e escalar."""
        amostra = (
            "DELETE FROM zz WHERE id NOT IN (SELECT id FROM zz ORDER BY id DESC LIMIT ?)",
            "UPDATE zz SET x = 1 WHERE EXISTS (SELECT 1 FROM zz r2 WHERE r2.id < zz.id)",
            "UPDATE zz SET x = 1 WHERE id = (SELECT MIN(id) FROM zz)",
        )
        achados = _lint_1093(amostra)
        self.assertEqual([tabela for tabela, _ in achados], ["zz", "zz", "zz"])

    def test_lint_pega_a_forma_real_do_migrate_delivery(self):
        """A cadeia inteira (ast -> traducao com `?` -> lint) acusa a forma do `db.py`.

        E o caminho que ja quebrou uma vez: a traducao de cada comando depende dos valores
        que o lint inventa (`_params_do_lint`), e a subquery real usa a forma de prazo por
        parametro. Se o valor sintetico nao servir, o lint estoura no lugar de acusar.
        """
        fonte = (
            "def f(conn):\n"
            "    conn.execute(\n"
            "        \"UPDATE pending_routes SET status = 'cancelled'\"\n"
            "        \" WHERE kind = 'delivery' AND id NOT IN (\"\n"
            "        \"   SELECT MIN(id) FROM pending_routes\"\n"
            "        \"    WHERE next_try_at <= datetime('now', '+' || ? || ' seconds')\"\n"
            "        \"    GROUP BY kind, chat_id)\",\n"
            "        (90,),\n"
            "    )\n"
        )
        comandos = _sentencas(fonte, _DML_NO_PY)
        self.assertEqual(len(comandos), 1, comandos)
        achados = _lint_1093(comandos)
        self.assertEqual([tabela for tabela, _ in achados], ["pending_routes"], achados)

    def test_lint_aceita_a_derivada(self):
        """As duas reescritas reais (`db.py`) passam, e tabela alheia e livre."""
        boas = (
            "DELETE FROM zz WHERE id NOT IN (SELECT id FROM ("
            "SELECT id FROM zz ORDER BY id DESC LIMIT ?) AS recentes)",
            "UPDATE zz SET status = 'cancelled' WHERE id NOT IN (SELECT id FROM ("
            "SELECT MIN(id) AS id FROM zz GROUP BY kind, chat_id) AS rotas_ativas)",
            "DELETE FROM zz WHERE id IN (SELECT id FROM outra)",
            "UPDATE zz SET x = (SELECT COUNT(*) FROM outra)",
            "UPDATE zz SET x = 1 WHERE id IN (SELECT 1 WHERE 1 IN (SELECT id FROM outra))",
        )
        self.assertEqual(_lint_1093(boas), [])


class IndiceTest(unittest.TestCase):
    """`CREATE INDEX` e os 5 indices parciais do projeto (5.3)."""

    def test_indice_comum_perde_o_if_not_exists(self):
        self.assertEqual(
            sqlcompat.translate(
                "CREATE INDEX IF NOT EXISTS bot_events_at_idx ON bot_events (at DESC)"
            )[0],
            "CREATE INDEX bot_events_at_idx ON bot_events (at DESC)",
        )

    def test_parcial_so_de_nulo_dispensa_a_funcao(self):
        sql, _ = sqlcompat.translate(
            "CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_queue\n"
            "  ON menu_options (queue_address, sort_order) WHERE queue_address IS NOT NULL"
        )
        self.assertEqual(
            sql, "CREATE UNIQUE INDEX menu_options_per_queue ON menu_options (queue_address, sort_order)"
        )

    def test_parcial_unico_vira_indice_funcional(self):
        sql, _ = sqlcompat.translate(
            "CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_root\n"
            "  ON menu_options (menu_id, sort_order)"
            " WHERE menu_id IS NOT NULL AND parent_id IS NULL"
        )
        self.assertEqual(
            sql,
            "CREATE UNIQUE INDEX menu_options_per_menu_root ON menu_options"
            " (menu_id, sort_order,"
            " ((CASE WHEN menu_id IS NOT NULL AND parent_id IS NULL THEN 1 END)))",
        )

    def test_parcial_unico_de_dois_parentescos(self):
        sql, _ = sqlcompat.translate(
            "CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_child\n"
            "  ON menu_options (menu_id, parent_id, sort_order)"
            " WHERE menu_id IS NOT NULL AND parent_id IS NOT NULL"
        )
        self.assertIn("(menu_id, parent_id, sort_order, ((CASE WHEN", sql)
        self.assertIn("parent_id IS NOT NULL THEN 1 END)))", sql)

    def test_parcial_nao_unico_indexa_so_quem_participa(self):
        sql, _ = sqlcompat.translate(
            "CREATE INDEX IF NOT EXISTS chats_classified_idx ON chats (updated_at)"
            " WHERE classification IS NOT NULL"
        )
        self.assertEqual(
            sql,
            "CREATE INDEX chats_classified_idx ON chats"
            " ((CASE WHEN classification IS NOT NULL THEN updated_at END))",
        )

    def test_parcial_com_not_in(self):  # db.py:330
        sql, _ = sqlcompat.translate(
            "CREATE UNIQUE INDEX IF NOT EXISTS pending_routes_active\n"
            "     ON pending_routes (kind, chat_id)\n"
            "   WHERE status NOT IN ('done', 'failed', 'cancelled')"
        )
        self.assertEqual(
            sql,
            "CREATE UNIQUE INDEX pending_routes_active ON pending_routes"
            " (kind, chat_id,"
            " ((CASE WHEN status NOT IN ('done', 'failed', 'cancelled') THEN 1 END)))",
        )

    def test_drop_de_indice_sem_tabela_so_a_fachada_resolve(self):
        # O MySQL exige `DROP INDEX x ON tabela`: quem monta e a fachada.
        self.assertEqual(
            sqlcompat.translate("DROP INDEX IF EXISTS menu_options_per_menu")[0],
            "DROP INDEX IF EXISTS menu_options_per_menu",
        )


class SqlDoBotTest(unittest.TestCase):
    """Consultas reais e complicadas do bot (copiadas do `db.py`) traduzem."""

    def test_claim_de_pending_routes(self):  # db.py:1370
        sql, params = sqlcompat.translate(
            "UPDATE pending_routes\n"
            "      SET claimed_by = ?, claimed_at = datetime('now'),\n"
            "          claim_expires_at = datetime('now', '+' || ? || ' seconds')\n"
            "    WHERE id = ?\n"
            "      AND (COALESCE(claimed_by, '') IN ('', ?)\n"
            "           OR claim_expires_at IS NULL\n"
            "           OR datetime(claim_expires_at) <= datetime('now'))",
            ("u:ana@claro.com", 120, 33, "u:pepe@claro.com"),
        )
        self.assertNotIn("?", sql)
        self.assertNotIn("||", sql)
        self.assertIn("claim_expires_at = %s", sql)
        self.assertIn("(claim_expires_at) <= %s", sql)
        self.assertEqual(len(params), 6)
        self.assertEqual(params[0], "u:ana@claro.com")
        self.assertTrue(perto(params[1]))
        self.assertTrue(perto(params[2], 120))
        self.assertEqual(params[3], 33)
        self.assertEqual(params[4], "u:pepe@claro.com")
        self.assertTrue(perto(params[5]))

    def test_upsert_de_chat_montado_por_fstring(self):  # db.py:949
        campos = ["status", "retries"]
        sql, params = sqlcompat.translate(
            "INSERT INTO chats (chat_id, %s) VALUES (?, %s, datetime('now'))"
            " ON CONFLICT (chat_id) DO UPDATE SET %s, updated_at = datetime('now')"
            % (
                ", ".join(campos),
                ", ".join(["?"] * len(campos)),
                ", ".join("%s = excluded.%s" % (c, c) for c in campos),
            ),
            (10, "awaiting_reply", 2),
        )
        self.assertIn("AS new ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("status = new.status", sql)
        self.assertNotIn("excluded.", sql)
        self.assertEqual(len(params), 5)
        self.assertEqual(params[0], 10)
        self.assertEqual(params[2], 2)
        self.assertTrue(perto(params[3]))
        self.assertTrue(perto(params[4]))

    def test_marcar_analista_offline(self):  # db.py:574
        sql, params = sqlcompat.translate(
            "UPDATE analyst_load SET online = 0, online_ok = 1,"
            " offline_since = COALESCE(offline_since, datetime('now')) WHERE address = ?",
            ("u:ana@claro.com",),
        )
        self.assertIn("COALESCE(offline_since, %s)", sql)
        self.assertEqual(len(params), 2)
        self.assertTrue(perto(params[0]))
        self.assertEqual(params[1], "u:ana@claro.com")


class _CursorBruto:
    """Cursor do driver falso: guarda o SQL recebido e devolve linhas de mentira."""

    def __init__(self, dono: "_ConexaoBruta") -> None:
        self._dono = dono
        self.description = None
        self.rowcount = -1
        self.lastrowid = 0
        self._pendentes: list[tuple] = []

    def execute(self, sql, params=None):
        self._dono.recebidos.append((sql, params))
        self.lastrowid = self._dono.proximo_id
        self._dono.proximo_id += 1
        if " ".join(sql.split()).upper().startswith(("SELECT", "SHOW")):
            self._pendentes = self._dono.resposta(sql, params)
            self.description = tuple((nome,) for nome in self._dono.colunas)
            self.rowcount = len(self._pendentes)
        else:
            self._pendentes = []
            self.rowcount = 1
        return self

    def executemany(self, sql, lotes):
        self._dono.recebidos.append((sql, [tuple(lote) for lote in lotes]))
        self.rowcount = len(lotes)
        return self

    def fetchone(self):
        return self._pendentes.pop(0) if self._pendentes else None

    def fetchall(self):
        linhas, self._pendentes = self._pendentes, []
        return linhas

    def fetchmany(self, size=None):
        quantos = 1 if size is None else size
        linhas, self._pendentes = self._pendentes[:quantos], self._pendentes[quantos:]
        return linhas

    def close(self):
        self._dono.fechados += 1


class _ConexaoBruta:
    """Duplo da conexao do PyMySQL: nada de socket, tudo registrado."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.recebidos: list[tuple] = []
        self.linhas: list[tuple] = []
        self.colunas: tuple[str, ...] = ()
        self.indices: set[tuple[str, str]] = set()
        self.proximo_id = 1
        self.commits = 0
        self.rollbacks = 0
        self.fechados = 0
        self.fechada = False

    def resposta(self, sql, params=None):
        """Responde as consultas de catalogo que a propria fachada dispara."""
        corpo = " ".join(sql.split()).upper()
        if "INFORMATION_SCHEMA.STATISTICS" in corpo:
            dados = tuple(params or ())
            if "INDEX_NAME = %S" in corpo and "TABLE_NAME = %S" in corpo:
                alvo = (dados[0], dados[1])
                return [("index_name",)] if alvo in self.indices else []
            if "INDEX_NAME = %S" in corpo:
                return [(tab,) for tab, nome in sorted(self.indices) if nome == dados[0]]
        return list(self.linhas)

    def cursor(self):
        return _CursorBruto(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.fechada = True

    def ping(self, reconnect=True):
        return None


class _Cliente:
    FOUND_ROWS = 2


class _Constantes:
    CLIENT = _Cliente()


class DriverFalso:
    """Duplo do pacote PyMySQL: nenhuma conexao de verdade sai daqui."""

    constants = _Constantes()

    def __init__(self) -> None:
        self.abertas: list[_ConexaoBruta] = []

    def connect(self, **kwargs):
        bruta = _ConexaoBruta(**kwargs)
        self.abertas.append(bruta)
        return bruta


class FachadaTest(unittest.TestCase):
    """A fachada tem a cara do `sqlite3.Connection` (secao 4.2), com driver duplo."""

    def setUp(self):
        self.driver = DriverFalso()
        self.conn = sqlcompat.connect_mysql(
            dsn={
                "host": "10.0.0.1",
                "port": 3306,
                "user": "bot",
                "password": "segredo",
                "database": "helptoa",
            },
            row_factory=db._dict_factory,
            driver=self.driver,
        )
        self.bruta = self.driver.abertas[0]

    def enviados(self):
        return [sql for sql, _ in self.bruta.recebidos]

    def test_handshake_da_sessao(self):
        self.assertEqual(
            self.enviados(),
            [
                "SET time_zone = '+00:00'",
                "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
                "SET SESSION innodb_lock_wait_timeout = 5",
                "SET SESSION sql_mode = ''",
            ],
        )
        self.assertEqual(self.bruta.kwargs["charset"], "utf8mb4")
        self.assertFalse(self.bruta.kwargs["autocommit"])       # armadilha 12
        self.assertEqual(self.bruta.kwargs["client_flag"], 2)   # CLIENT.FOUND_ROWS
        self.assertEqual(self.bruta.kwargs["database"], "helptoa")
        self.assertEqual(self.bruta.commits, 1)

    def test_execute_traduz_e_entrega_dict(self):
        self.bruta.colunas = ("name", "total")
        self.bruta.linhas = [("q1", 3), ("q2", 1)]
        linhas = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        self.assertEqual(linhas, [{"name": "q1", "total": 3}, {"name": "q2", "total": 1}])
        sql, params = self.bruta.recebidos[-1]
        self.assertIn("information_schema.tables", sql)
        self.assertIsNone(params)           # sem parametro: nada de tupla vazia

    def test_execute_com_marcador_e_agora(self):
        self.conn.execute(
            "UPDATE chats SET updated_at = datetime('now') WHERE chat_id = ?", (7,)
        )
        sql, params = self.bruta.recebidos[-1]
        self.assertEqual(sql, "UPDATE chats SET updated_at = %s WHERE chat_id = %s")
        self.assertTrue(CARIMBO.match(params[0]), params)
        self.assertEqual(params[1], 7)

    def test_pragma_sem_equivalente_nao_vai_ao_driver(self):
        antes = len(self.bruta.recebidos)
        cur = self.conn.execute("PRAGMA journal_mode = WAL")
        self.assertEqual(cur.fetchall(), [])
        self.assertIsNone(cur.fetchone())
        self.assertEqual(len(self.bruta.recebidos), antes)

    def test_lastrowid_e_rowcount(self):
        self.bruta.proximo_id = 42
        cur = self.conn.execute("INSERT INTO queues (address) VALUES (?)", ("u:fila",))
        self.assertEqual(cur.lastrowid, 42)
        self.assertEqual(cur.rowcount, 1)

    def test_executemany_traduz_o_lote(self):
        self.conn.executemany(
            "INSERT INTO group_teams (group_id, team_id) VALUES (?, ?)", [(1, 2), (1, 3)]
        )
        sql, lotes = self.bruta.recebidos[-1]
        self.assertEqual(sql, "INSERT INTO group_teams (group_id, team_id) VALUES (%s, %s)")
        self.assertEqual(lotes, [(1, 2), (1, 3)])

    def test_executemany_com_agora_por_linha(self):
        self.conn.executemany(
            "INSERT INTO bot_jobs (id, started_at) VALUES (?, datetime('now'))",
            [(1,), (2,)],
        )
        sql, lotes = self.bruta.recebidos[-1]
        self.assertEqual(sql, "INSERT INTO bot_jobs (id, started_at) VALUES (%s, %s)")
        self.assertEqual([lote[0] for lote in lotes], [1, 2])
        for lote in lotes:
            self.assertTrue(perto(lote[1]), lote)

    def test_execute_com_is_parametro_manda_null_safe(self):   # db.py:1263
        self.bruta.colunas = ("n",)
        self.bruta.linhas = []
        self.conn.execute(
            "SELECT COUNT(*) AS n FROM pending_routes"
            " WHERE status = 'waiting' AND id < ? AND (team_id IS ?) AND kind = 'chat'",
            (828, 1),
        )
        sql, params = self.bruta.recebidos[-1]
        self.assertEqual(
            sql,
            "SELECT COUNT(*) AS n FROM pending_routes WHERE status = 'waiting'"
            " AND id < %s AND (team_id <=> %s) AND kind = 'chat'",
        )
        self.assertEqual(tuple(params), (828, 1))

    def test_executemany_simples_usa_o_lote_do_driver(self):
        antes = len(self.bruta.recebidos)
        self.conn.executemany(
            "INSERT INTO group_teams (group_id, team_id) VALUES (?, ?)", [(1, 2), (1, 3)]
        )
        novos = self.bruta.recebidos[antes:]
        self.assertEqual(len(novos), 1)                        # um lote, nao duas linhas
        sql, lotes = novos[0]
        self.assertEqual(sql, "INSERT INTO group_teams (group_id, team_id) VALUES (%s, %s)")
        self.assertEqual(lotes, [(1, 2), (1, 3)])

    def test_executemany_com_agora_fora_do_values_vai_linha_a_linha(self):  # db.py:937
        """O atalho `RE_INSERT_VALUES` do PyMySQL so aceita marcador no `VALUES`."""
        antes = len(self.bruta.recebidos)
        self.conn.executemany(
            "INSERT INTO queues (address, name) VALUES (?, ?)\n"
            "   ON CONFLICT (address) DO UPDATE\n"
            "   SET name = COALESCE(NULLIF(excluded.name, ''), queues.name),\n"
            "       last_seen = datetime('now')",
            [("h:a", "A"), ("h:b", "B")],
        )
        novos = self.bruta.recebidos[antes:]
        self.assertEqual(len(novos), 2)                        # linha a linha
        for sql, params in novos:
            self.assertEqual(
                " ".join(sql.split()),
                "INSERT INTO queues (address, name) VALUES (%s, %s) AS new"
                " ON DUPLICATE KEY UPDATE name = COALESCE(NULLIF(new.name, ''),"
                " queues.name), last_seen = %s",
            )
            self.assertEqual(len(params), 3)                   # 2 do VALUES + o agora
        self.assertEqual([params[0] for _, params in novos], ["h:a", "h:b"])

    def test_executescript_comando_a_comando(self):
        antes = len(self.bruta.recebidos)
        self.conn.executescript(
            "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT);\n"
            "PRAGMA busy_timeout = 5000;\n"
            "SELECT 1;"
        )
        novos = self.enviados()[antes:]
        self.assertEqual(len(novos), 2)     # o PRAGMA sem equivalente nao executa
        self.assertIn("AUTO_INCREMENT", novos[0])
        self.assertEqual(novos[1], "SELECT 1")

    def test_context_manager_commita_e_desfaz(self):
        self.bruta.commits = 0
        with self.conn:
            self.conn.execute("SELECT 1")
        self.assertEqual(self.bruta.commits, 1)
        with self.assertRaises(ZeroDivisionError):
            with self.conn:
                raise ZeroDivisionError("para o rollback")
        self.assertEqual(self.bruta.rollbacks, 1)

    def test_is_disconnect(self):
        self.assertTrue(sqlcompat.is_disconnect(sqlcompat.OperationalError(2006, "gone away")))
        self.assertTrue(
            sqlcompat.is_disconnect(RuntimeError("Lost connection to MySQL server"))
        )
        self.assertFalse(sqlcompat.is_disconnect(RuntimeError("Duplicate entry 'x'")))


class IndiceNaFachadaTest(unittest.TestCase):
    """`CREATE/DROP INDEX IF [NOT] EXISTS`: a fachada resolve no catalogo (5.3)."""

    def setUp(self):
        self.driver = DriverFalso()
        self.conn = sqlcompat.connect_mysql(
            dsn={"host": "h", "user": "u", "password": "p", "database": "d"},
            driver=self.driver,
        )
        self.bruta = self.driver.abertas[0]
        self.antes = len(self.bruta.recebidos)

    def novos(self):
        return [sql for sql, _ in self.bruta.recebidos[self.antes:]]

    def test_cria_quando_o_indice_nao_existe(self):
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_origem ON queue_operators (source)")
        novos = self.novos()
        self.assertEqual(len(novos), 2)                  # catalogo + CREATE
        self.assertIn("information_schema.statistics", novos[0])
        self.assertEqual(novos[1], "CREATE INDEX ix_origem ON queue_operators (source)")

    def test_nao_recria_quando_o_indice_existe(self):
        self.bruta.indices.add(("queue_operators", "ix_origem"))
        cur = self.conn.execute("CREATE INDEX IF NOT EXISTS ix_origem ON queue_operators (source)")
        self.assertEqual(len(self.novos()), 1)           # so a consulta de catalogo
        self.assertIsNone(cur.fetchone())
        self.assertEqual(cur.fetchall(), [])

    def test_drop_de_indice_existente_ganha_a_tabela(self):
        self.bruta.indices.add(("menu_options", "menu_options_per_menu"))
        self.conn.execute("DROP INDEX IF EXISTS menu_options_per_menu")
        novos = self.novos()
        self.assertEqual(len(novos), 2)
        self.assertIn("information_schema.statistics", novos[0])
        self.assertEqual(novos[1], "DROP INDEX menu_options_per_menu ON menu_options")

    def test_drop_de_indice_ausente_e_no_op(self):
        cur = self.conn.execute("DROP INDEX IF EXISTS menu_options_per_menu")
        self.assertEqual(len(self.novos()), 1)
        self.assertIsNone(cur.fetchone())

    def test_parcial_ja_existente_nao_e_recriado(self):
        self.bruta.indices.add(("pending_routes", "pending_routes_active"))
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS pending_routes_active\n"
            "     ON pending_routes (kind, chat_id)\n"
            "   WHERE status NOT IN ('done', 'failed', 'cancelled')"
        )
        self.assertEqual(len(self.novos()), 1)

    def test_parcial_ausente_vira_funcional_de_verdade(self):
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS pending_routes_active\n"
            "     ON pending_routes (kind, chat_id)\n"
            "   WHERE status NOT IN ('done', 'failed', 'cancelled')"
        )
        self.assertEqual(
            self.novos()[1],
            "CREATE UNIQUE INDEX pending_routes_active ON pending_routes"
            " (kind, chat_id,"
            " ((CASE WHEN status NOT IN ('done', 'failed', 'cancelled') THEN 1 END)))",
        )


class BackendTest(unittest.TestCase):
    """`sqlcompat.connect()`: `sqlite` segue identico e `mysql` usa a fachada."""

    def test_sqlite_igual_ao_de_hoje(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = Path(pasta) / "teste.sqlite"
            conn = sqlcompat.connect(
                sqlite_path=caminho, backend="sqlite", row_factory=db._dict_factory
            )
            try:
                self.assertIsInstance(conn, sqlite3.Connection)
                self.assertEqual(
                    conn.execute("PRAGMA foreign_keys").fetchone()["foreign_keys"], 1
                )
                conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT)")
                conn.execute("INSERT INTO t (nome) VALUES (?)", ("ana",))
                linha = conn.execute("SELECT * FROM t WHERE nome = ?", ("ana",)).fetchone()
                self.assertEqual(linha["id"], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM t").fetchone()["n"], 1)
            finally:
                conn.close()
            self.assertTrue(caminho.exists())

    def test_mysql_usa_a_fachada(self):
        driver = DriverFalso()
        conn = sqlcompat.connect(
            backend="mysql",
            dsn={"host": "h", "user": "u", "password": "p", "database": "d"},
            row_factory=db._dict_factory,
            driver=driver,
        )
        self.assertIsInstance(conn, sqlcompat._MySQLConnection)
        self.assertIs(conn.row_factory, db._dict_factory)
        self.assertEqual(len(driver.abertas), 1)
        conn.close()
        self.assertTrue(driver.abertas[0].fechada)

    def test_env_escolhe_o_backend(self):
        with patch.dict(os.environ, {"DB_BACKEND": "mysql"}):
            self.assertEqual(sqlcompat.config.db_backend(), "mysql")
        with patch.dict(os.environ, {"DB_BACKEND": "sqlite"}):
            self.assertEqual(sqlcompat.config.db_backend(), "sqlite")

    def test_backend_do_db_se_realinha_depois_do_import(self):
        """O `.env` pode chegar depois do import: `db` reavalia o backend (2.2).

        `db` decide as excecoes no import (`db.IntegrityError`), mas o `.env` so
        era lido dentro do `amain()` - e o `run.py` importa o `main` (com o `db`
        dentro) antes disso. Com `DB_BACKEND=mysql` o `ensure_schema` ficaria
        sem o `PRAGMA foreign_keys = OFF` (erro 1824 nas FKs fora de ordem) e o
        `except db.IntegrityError` do bot pegaria a classe errada. O backend e
        reavaliado no import e de novo em `connect()`.
        """
        self.assertFalse(db._BACKEND_MYSQL)
        self.assertIs(db.IntegrityError, sqlite3.IntegrityError)
        try:
            with patch.dict(os.environ, {"DB_BACKEND": "mysql"}):
                db._resolve_backend()
                self.assertTrue(db._BACKEND_MYSQL)
                self.assertIs(db.IntegrityError, sqlcompat.IntegrityError)
                self.assertIs(db.OperationalError, sqlcompat.OperationalError)
                self.assertIs(db.ProgrammingError, sqlcompat.ProgrammingError)
        finally:
            db._resolve_backend()
        self.assertFalse(db._BACKEND_MYSQL)
        self.assertIs(db.IntegrityError, sqlite3.IntegrityError)

    def test_db_connect_e_o_ponto_unico_de_conexao(self):
        """`db.connect()` passa pelo shim: com sqlite, schema inteiro e o de sempre."""
        with tempfile.TemporaryDirectory() as pasta:
            destino = Path(pasta) / "bot.sqlite"
            ambiente = {"SQLITE_PATH": str(destino), "DB_BACKEND": "sqlite"}
            with patch.dict(os.environ, ambiente):
                conn = db.connect()
                try:
                    self.assertIsInstance(conn, sqlite3.Connection)
                    self.assertIs(conn.row_factory, db._dict_factory)
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) AS n FROM menu_options").fetchone()["n"],
                        0,
                    )
                    self.assertTrue(conn.execute("PRAGMA foreign_keys").fetchone()["foreign_keys"])
                finally:
                    conn.close()
        self.assertEqual(db.IntegrityError, sqlite3.IntegrityError)

    def test_ensure_schema_inteiro_no_mysql(self):
        """`ensure_schema` fecha no MySQL: o DDL vem de `SHOW CREATE TABLE`.

        Antes do `sqlcompat.table_ddl` a leitura de DDL em `db.py:123` levantava
        `SqlCompatError` em **toda** conexao MySQL, derrubando o bot no boot.
        """
        driver = DriverFalso()
        conn = sqlcompat.connect_mysql(
            dsn={"host": "h", "user": "u", "password": "p", "database": "d"},
            row_factory=db._dict_factory,
            driver=driver,
        )
        db.ensure_schema(conn)
        comandos = [sql for bruta in driver.abertas for sql, _ in bruta.recebidos]
        self.assertIn("SHOW CREATE TABLE `menu_options`", comandos)
        self.assertEqual([c for c in comandos if "sqlite_master" in c.lower()], [])


    def test_rebuild_do_check_legado_dispara_com_o_ddl_antigo(self):
        """`table_ddl` decide o rebuild: sem `run_script` no `CREATE`, refaz a tabela."""
        with tempfile.TemporaryDirectory() as pasta:
            destino = Path(pasta) / "bot.sqlite"
            conn = sqlcompat.connect(
                sqlite_path=destino, backend="sqlite", row_factory=db._dict_factory
            )
            try:
                with patch.object(
                    sqlcompat, "table_ddl", return_value="CREATE TABLE menu_options (action TEXT)"
                ):
                    db.ensure_schema(conn)      # base "antiga": o CHECK nao tem run_script
                self.assertIn("run_script", sqlcompat.table_ddl(conn, "menu_options"))
                self.assertIn(
                    "script_slug", {r["name"] for r in conn.execute("PRAGMA table_info(menu_options)")}
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

