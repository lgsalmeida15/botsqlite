# -*- coding: utf-8 -*-
"""Shim SQLite -> MySQL 8.0.19+ (Caminho A da migracao).

O bot continua escrito em dialeto SQLite (`?` como marcador, `ON CONFLICT ...
DO UPDATE`, `datetime('now')`, `INSERT OR IGNORE`, `PRAGMA`, `sqlite_master`).
Com `DB_BACKEND=mysql`, este modulo traduz o SQL no choke point
(`db.connect()`) e devolve um objeto com a MESMA cara de um
`sqlite3.Connection` (execute/executemany/executescript, `row_factory`,
`lastrowid`, `rowcount`, commit/rollback/close). Com `DB_BACKEND=sqlite`
(padrao) NADA disto e usado: o `connect()` segue com `sqlite3` puro e o
comportamento atual nao muda.

Duas invariantes valem para todo o SQL do projeto:

1. **UTC calculado no app, nunca no banco.** `datetime('now')` vira o instante
   calculado em Python e passado como parametro — nenhum `NOW()`/
   `UTC_TIMESTAMP()` entra em consulta. `UTC_TIMESTAMP()` so aparece em DDL
   (`DEFAULT (...)`), que tambem e UTC por causa do `SET time_zone='+00:00'`.
2. **Marcador posicional.** O SQLite usa `?`, o MySQL (PyMySQL) usa `%s`; todo
   `%` literal da consulta tem de virar `%%`, que e a exigencia do
   `%`-formatting do driver. Isso so acontece quando a execucao leva
   parametros — inclusive os que a propria traducao injeta (o "agora").

A ordem das regras e a de `sqliteformysql.md` §5.1: vocabulario antes da
pontuacao, e `datetime('now')` antes de qualquer regra que mexa em `||`/`?`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from . import config

SQLITE = "sqlite"
MYSQL = "mysql"

# API publica do shim (o bot pluga pelo `connect()`; o resto e para os testes).
__all__ = [
    "MYSQL",
    "SQLITE",
    "SqlCompatError",
    "TS_FMT",
    "connect",
    "connect_mysql",
    "convert_params",
    "is_disconnect",
    "modifier_seconds",
    "sem_pragma_fk",
    "shift_stamp",
    "split_statements",
    "table_ddl",
    "translate",
    "utc_now",
    "utc_stamp",
]


# Formato de carimbo do projeto: UTC, ordenavel como texto e igual ao que o
# SQLite produzia em `datetime('now')`.
TS_FMT = "%Y-%m-%d %H:%M:%S"

_SECONDS_PER_UNIT = {
    "second": 1,
    "seconds": 1,
    "minute": 60,
    "minutes": 60,
    "hour": 3600,
    "hours": 3600,
    "day": 86400,
    "days": 86400,
    "week": 604800,
    "weeks": 604800,
}


class SqlCompatError(Exception):
    """SQL que o tradutor nao sabe converter: falha alto, nunca em silencio."""


# ----------------------------------------------------------------------- tempo


def utc_now() -> datetime:
    """Agora em UTC (o banco guarda UTC: sessao com `time_zone='+00:00'`)."""
    return datetime.now(timezone.utc)


def utc_stamp(moment: datetime | None = None) -> str:
    """Carimbo UTC no formato que o projeto grava e le ('YYYY-MM-DD HH:MM:SS')."""
    return (moment or utc_now()).strftime(TS_FMT)


def shift_stamp(seconds: float) -> str:
    """Agora + N segundos (aceita negativo), em UTC."""
    return utc_stamp(utc_now() + timedelta(seconds=float(seconds)))


_MODIFIER_RE = re.compile(r"^(?P<sign>[+-])?\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[a-z]+)$")


def modifier_seconds(modifier: str) -> float:
    """'-300 seconds' / '+1 day' -> segundos com sinal (modificador do SQLite).

    O projeto usa dois jeitos de passar o prazo: literal no SQL e por parametro
    (`datetime('now', ?)` em `attempts_summary(since='-1 day')`), entao o
    tradutor precisa entender os dois.
    """
    # `None` (parametro ausente) e o unico valor que nao e modificador: usar
    # `modifier or ""` engoliria tambem o inteiro `0`, fazendo `0` e `"0"`
    # seguirem caminhos diferentes.
    texto = "" if modifier is None else str(modifier).strip().lower()
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", texto):
        # Sem unidade: so faz sentido dentro de `datetime('now', '+' || ? || ' seconds')`,
        # que o tradutor monta antes de chegar aqui.
        return float(texto)
    casado = _MODIFIER_RE.match(texto)
    if not casado or casado.group("unit") not in _SECONDS_PER_UNIT:
        raise SqlCompatError("modificador de tempo nao suportado: %r" % (modifier,))
    total = float(casado.group("value")) * _SECONDS_PER_UNIT[casado.group("unit")]
    return -total if casado.group("sign") == "-" else total


# ---------------------------------------------------------------- leitura do SQL


def _mask(sql: str) -> str:
    """Copia do SQL com literais e comentarios virados espaco (mesmo tamanho).

    Manter o comprimento e o que permite achar `datetime(`, `?` e `||` por
    posicao e so depois ler o texto ORIGINAL daquele trecho. Sem isso, um
    `'ON CONFLICT'` dentro de um texto viraria upsert e um `?` dentro de um
    comentario consumiria parametro.
    """
    saida = list(sql)
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == "\\" and c in "'\"":
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:   # '' / "" escapado
                        j += 2
                        continue
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                if saida[k] != "\n":
                    saida[k] = " "
            i = j + 1
        elif c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                saida[k] = " "
            i = j
        elif c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if saida[k] != "\n":
                    saida[k] = " "
            i = j
        else:
            i += 1
    return "".join(saida)


_ID_RESERVADO = re.compile(r"(?<![`\w])key(?![`\w])", re.I)
_ID_RESERVADO_ANTES = re.compile(
    r"(?:\b(?:PRIMARY|FOREIGN|UNIQUE|DUPLICATE|INDEX|PARTITION)\s*)$", re.I
)


def _escapa_identificadores(sql: str) -> str:
    """`key` (coluna do `bot_settings`) -> `` `key` ``: e reservada no MySQL.

    O SQLite aceita `key` como nome de coluna; o MySQL nao (erro de sintaxe em
    `bot_settings (key, value)`). Ficam de fora o `KEY` de `PRIMARY KEY`,
    `FOREIGN KEY` e `ON DUPLICATE KEY UPDATE` — que ja sao palavra-chave — e o
    texto dentro de literais, que a mascara esconde.
    """
    mask = _mask(sql)
    partes: list[str] = []
    anterior = 0
    for casado in _ID_RESERVADO.finditer(mask):
        if _ID_RESERVADO_ANTES.search(mask[: casado.start()]):
            continue
        partes.append(sql[anterior : casado.start()])
        partes.append("`%s`" % sql[casado.start() : casado.end()])
        anterior = casado.end()
    if not partes:
        return sql
    partes.append(sql[anterior:])
    return "".join(partes)


def _match_paren(mask: str, open_idx: int) -> int:
    """Indice do `)` que fecha o `(` de `open_idx` (a mascara esconde literais)."""
    profundidade = 0
    for i in range(open_idx, len(mask)):
        c = mask[i]
        if c == "(":
            profundidade += 1
        elif c == ")":
            profundidade -= 1
            if profundidade == 0:
                return i
    raise SqlCompatError("parentese nao fechado: %r" % mask[:80])


def _split_args(texto: str, mask: str) -> list[str]:
    """Separa os argumentos de uma chamada nas virgulas de nivel 0."""
    partes: list[str] = []
    inicio = 0
    profundidade = 0
    for i, c in enumerate(mask):
        if c == "(":
            profundidade += 1
        elif c == ")":
            profundidade -= 1
        elif c == "," and profundidade == 0:
            partes.append(texto[inicio:i])
            inicio = i + 1
    partes.append(texto[inicio:])
    return partes


class _ParamsState:
    """Consome parametros originais e monta a lista nova, na ordem da saida."""

    def __init__(self, params: Sequence[Any] | None):
        self.orig = list(params) if params is not None else []
        self.posto = 0
        self.saida: list[Any] = []

    def _take(self, quantos: int) -> list[Any]:
        if quantos <= 0:
            return []
        if self.posto + quantos > len(self.orig):
            raise SqlCompatError(
                "SQL pede %d parametro(s) e chegaram %d"
                % (self.posto + quantos, len(self.orig))
            )
        vals = self.orig[self.posto:self.posto + quantos]
        self.posto += quantos
        return vals

    def emite_comuns(self, texto: str) -> None:
        """Copia para a saida, na ordem, os parametros dos `?` deste trecho."""
        self.saida.extend(self._take(_mask(texto).count("?")))

    def consome_um(self) -> Any:
        """Retira o proximo parametro original sem devolve-lo para a saida."""
        return self._take(1)[0]

    def injeta(self, valor: Any) -> None:
        """Acrescenta um valor calculado neste processo (o \"agora\" UTC)."""
        self.saida.append(valor)


# --------------------------------------------------------------- familia tempo

_TIME_CALL = re.compile(r"\b(datetime|julianday)\s*\(", re.I)
_JULIANDAY_SEQ = re.compile(r"\s*-\s*julianday\s*\(", re.I)
_JULIANDAY_TAIL = re.compile(r"\s*\)\s*\*\s*86400", re.I)
_MOD_POR_PARAM = re.compile(r"'([+-])'\s*\|\|\s*\?\s*\|\|\s*'\s*seconds\s*'", re.I)
_OFFSET_LITERAL = re.compile(r"^'[+-]\d{1,2}:\d{2}'$")


def _rewrite_time(sql: str, params: Sequence[Any] | None) -> tuple[str, list[Any]]:
    """Resolve `datetime()`/`julianday()`: o instante UTC vem da aplicacao (5.4)."""
    estado = _ParamsState(params)
    texto = _segmento_tempo(sql, estado)
    if estado.posto < len(estado.orig):
        raise SqlCompatError(
            "sobraram %d parametro(s) sem lugar no SQL traduzido"
            % (len(estado.orig) - estado.posto)
        )
    return texto, estado.saida


def _segmento_tempo(seg: str, estado: _ParamsState) -> str:
    """Percorre `seg` da esquerda para a direita resolvendo a familia de tempo.

    A leitura e a ordem em que os parametros sao consumidos/inseridos; por isso
    este passo e um unico passe, e nao uma sequencia de substituicoes globais:
    um mesmo comando pode ter mais de uma data.
    """
    mask = _mask(seg)
    partes: list[str] = []
    i = 0
    while True:
        casado = _TIME_CALL.search(mask, i)
        if not casado:
            break
        abre = mask.index("(", casado.end() - 1)
        fecha = _match_paren(mask, abre)
        if casado.group(1).lower() == "julianday":
            ini, fim, lado_a, lado_b = _julianday_span(seg, mask, casado.start(), abre, fecha)
            antes = seg[i:ini]
            estado.emite_comuns(antes)
            partes.append(antes)
            partes.append(_julianday_texto(lado_a, lado_b, estado))
        else:
            antes = seg[i:casado.start()]
            estado.emite_comuns(antes)
            partes.append(antes)
            partes.append(_chamada_datetime(seg[abre + 1:fecha], estado))
            fim = fecha + 1
        i = fim
    resto = seg[i:]
    estado.emite_comuns(resto)
    partes.append(resto)
    return "".join(partes)


def _chamada_datetime(args: str, estado: _ParamsState) -> str:
    """Traduz o conteudo de um `datetime(...)` (o `(`/`)` ja ficaram fora)."""
    texto = args.strip()
    if texto[:5].lower() == "'now'":          # no texto CRU: a mascara apaga o literal
        resto = texto[5:].strip()
        if not resto:
            estado.injeta(utc_stamp())
            return "%s"
        if not resto.startswith(","):
            raise SqlCompatError("datetime() mal formado: %r" % args)
        return _datetime_modificador(resto[1:].strip(), estado)
    if "," in _mask(args):
        return _datetime_com_offset(args, estado)
    # `datetime(<coluna>)`: identidade — a coluna ja esta no formato canonico.
    return "(%s)" % _segmento_tempo(args, estado).strip()


def _datetime_modificador(mod: str, estado: _ParamsState) -> str:
    """`datetime('now', <modificador>)` -> `%s` com o instante calculado aqui."""
    por_param = _MOD_POR_PARAM.fullmatch(mod)
    if por_param:
        numero = estado.consome_um()
        sinal = -1 if por_param.group(1) == "-" else 1
        estado.injeta(shift_stamp(sinal * float(numero)))
        return "%s"
    if _mask(mod).strip() == "?":
        estado.injeta(shift_stamp(modifier_seconds(estado.consome_um())))
        return "%s"
    literal = re.fullmatch(r"'(.*)'", mod.strip(), re.S)
    if literal:
        estado.injeta(shift_stamp(modifier_seconds(literal.group(1))))
        return "%s"
    raise SqlCompatError("datetime('now', ...) nao suportado: %r" % mod)


def _datetime_com_offset(args: str, estado: _ParamsState) -> str:
    """`datetime(<coluna>, ?)` -> `CONVERT_TZ(<coluna>, '+00:00', ?)` (regra 8)."""
    partes = _split_args(args, _mask(args))
    if len(partes) != 2:
        raise SqlCompatError("datetime() com %d argumentos: %r" % (len(partes), args))
    col, off = partes[0].strip(), partes[1].strip()
    if _mask(off).strip() != "?" and not _OFFSET_LITERAL.match(off):
        raise SqlCompatError(
            "`datetime(<coluna>, ...)` so aceita deslocamento numerico ('-03:00'): %r" % args
        )
    coluna = _segmento_tempo(col, estado).strip()
    estado.emite_comuns(off)
    return "CONVERT_TZ(%s, '+00:00', %s)" % (coluna, off)


def _julianday_span(
    seg: str, mask: str, ini: int, abre: int, fecha: int
) -> tuple[int, int, str, str]:
    """Reconhece `(julianday(a) - julianday(b)) * 86400` a partir do 1o `julianday`.

    Devolve `(inicio, fim, a, b)`: o intervalo a substituir (incluindo o `(` de
    fora, quando existe) e as duas expressoes, lidas do SQL ORIGINAL.
    """
    seq = _JULIANDAY_SEQ.match(mask, fecha + 1)
    if not seq:
        raise SqlCompatError("julianday() solto, sem `- julianday(...)`: %r" % seg[:80])
    abre2 = mask.index("(", seq.end() - 1)
    fecha2 = _match_paren(mask, abre2)
    cauda = _JULIANDAY_TAIL.match(mask, fecha2 + 1)
    if not cauda:
        raise SqlCompatError("julianday(): faltou `) * 86400` na diferenca: %r" % seg[:80])
    inicio = ini
    j = ini - 1
    while j >= 0 and mask[j].isspace():
        j -= 1
    if j >= 0 and mask[j] == "(":
        inicio = j
    return inicio, cauda.end(), seg[abre + 1:fecha].strip(), seg[abre2 + 1:fecha2].strip()


def _julianday_texto(a: str, b: str, estado: _ParamsState) -> str:
    """`(julianday(a) - julianday(b)) * 86400` -> `TIMESTAMPDIFF(SECOND, b, a)`.

    Os argumentos invertem; o lado `'now'` vira `%s` com o instante calculado
    aqui, exatamente como na regra 3.
    """
    def lado(expr: str) -> str:
        if expr.strip().lower() == "'now'":   # CRU: a mascara apaga o literal
            estado.injeta(utc_stamp())
            return "%s"
        return expr

    return "TIMESTAMPDIFF(SECOND, %s, %s)" % (lado(b), lado(a))


# ---------------------------------------------------------------- regras de DDL

_DDL = re.compile(r"^\s*(?:CREATE|ALTER|DROP|RENAME)\b", re.I)
_AUTOINC_PK = re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.I)
_AUTOINC_SOZINHO = re.compile(r"\bAUTOINCREMENT\b", re.I)
_RENAME_TO = re.compile(
    r"\bALTER\s+TABLE\s+(?P<tabela>[A-Za-z_][\w$]*)\s+RENAME\s+TO\s+(?P<novo>[A-Za-z_][\w$]*)",
    re.I,
)
_DEFAULT_DATETIME = re.compile(r"DEFAULT\s*\(\s*datetime\s*\(\s*'now'\s*\)\s*\)", re.I)
_DEFAULT_AGORA = re.compile(r"DEFAULT\s*\(\s*CURRENT_TIMESTAMP\s*\)", re.I)
_DEFAULT_TEXTO = re.compile(r"DEFAULT\s+'((?:[^']|'')*)'")
_TEXTO_CHAVE = re.compile(
    r"\bTEXT\s+(?P<meio>(?:NOT\s+NULL\s+)?)(?P<chave>PRIMARY\s+KEY|UNIQUE)\b", re.I
)
_ALTER_TABELA = re.compile(r"ALTER\s+TABLE\s+(?P<tabela>[A-Za-z_][\w$]*)", re.I)
_ADD_FK = re.compile(
    r"ADD\s+COLUMN\s+(?P<col>[A-Za-z_][\w$]*)\s+(?:INTEGER|BIGINT)\s+"
    r"REFERENCES\s+(?P<tabela>[A-Za-z_][\w$]*)\s*\(\s*(?P<ref>[A-Za-z_][\w$]*)\s*\)"
    r"(?P<acao>\s+ON\s+DELETE\s+(?:SET\s+NULL|CASCADE|RESTRICT|NO\s+ACTION))?",
    re.I,
)


def _texto_para_varchar(casado: re.Match) -> str:
    """`TEXT ... PRIMARY KEY/UNIQUE` -> `VARCHAR(191)` (5.5.b: TEXT nao pode ser chave)."""
    meio = casado.group("meio") or ""
    chave = re.sub(r"\s+", " ", casado.group("chave").upper())
    return "VARCHAR(191) %s%s" % (meio, chave)


def _adiciona_fk(sql: str) -> str:
    """`ADD COLUMN x INTEGER REFERENCES t(c) ON DELETE ...` -> coluna + CONSTRAINT.

    O MySQL nao aceita `REFERENCES` inline em `ALTER TABLE ... ADD COLUMN`; a FK
    vira um `ADD CONSTRAINT` separado. O nome sai de `fk_<tabela>_<coluna>`, que
    e unico no schema (o MySQL exige unicidade por base, nao por tabela).
    """
    if not _ADD_FK.search(sql):
        return sql
    alterada = _ALTER_TABELA.search(sql)
    tabela = alterada.group("tabela") if alterada else "t"

    def troca(casado: re.Match) -> str:
        col = casado.group("col")
        acao = (casado.group("acao") or "").strip()
        return (
            "ADD COLUMN %s BIGINT NULL, ADD CONSTRAINT fk_%s_%s FOREIGN KEY (%s) "
            "REFERENCES %s(%s)%s"
            % (
                col,
                tabela,
                col,
                col,
                casado.group("tabela"),
                casado.group("ref"),
                (" " + acao) if acao else "",
            )
        )

    return _ADD_FK.sub(troca, sql)


# ---------------------------------------------------------- FK inline (secao 5.1)
#
# O MySQL **ignora** em silencio o `REFERENCES t(c)` escrito na coluna: a tabela
# nasce sem FK nenhuma e o `ON DELETE` nunca dispara (regra 4). Toda referencia
# inline do `CREATE TABLE` vira, entao, uma restricao de tabela explicita
# (`CONSTRAINT ... FOREIGN KEY ...`), que e a forma que o InnoDB aplica de fato.

_CREATE_TABELA = re.compile(
    r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(?P<tabela>[A-Za-z_][\w$]*)\s*(?P<abre>\()",
    re.I,
)
_ITEM_DE_TABELA = re.compile(
    r"^\s*(?:CONSTRAINT|PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|KEY|INDEX)\b", re.I
)
_COLUNA_FK = re.compile(r"^\s*(?P<col>[A-Za-z_][\w$]*)\b")
_REFERENCES_INLINE = re.compile(
    r"\bREFERENCES\s+(?P<tabela>[A-Za-z_][\w$]*)\s*\(\s*(?P<ref>[A-Za-z_][\w$]*)\s*\)"
    r"(?P<acoes>(?:\s+ON\s+(?:DELETE|UPDATE)\s+"
    r"(?:SET\s+NULL|SET\s+DEFAULT|CASCADE|RESTRICT|NO\s+ACTION)){0,2})",
    re.I,
)
# A ponta filha tem de ter o tipo da ponta referenciada: todo
# `INTEGER PRIMARY KEY AUTOINCREMENT` ja virou `BIGINT`, entao `INT`/`INTEGER`
# na coluna com FK vira `BIGINT` (mesma decisao do `_adiciona_fk`).
_FK_INT_PARA_BIGINT = re.compile(r"^(?P<col>[A-Za-z_][\w$]*)\s+(?:INT|INTEGER)\b", re.I)


def _fk_de_coluna(sql: str) -> str:
    """`REFERENCES` inline de `CREATE TABLE` -> `CONSTRAINT ... FOREIGN KEY` (5.1, regra 4).

    So mexe no `CREATE TABLE` (o `ALTER TABLE ... ADD COLUMN` tem o
    `_adiciona_fk`). A FK sai do item da coluna e vai para o fim do corpo, com o
    nome `fk_<tabela>_<coluna>` - unico por base, que e o que o MySQL exige.
    """
    cabeca = _CREATE_TABELA.search(sql)
    if not cabeca:
        return sql
    mask = _mask(sql)
    abre = cabeca.start("abre")
    fecha = _match_paren(mask, abre)
    corpo = sql[abre + 1 : fecha]
    itens: list[str] = []
    fks: list[str] = []
    for item in _split_args(corpo, mask[abre + 1 : fecha]):
        limpo = item.strip()
        if not limpo:
            continue
        achado = _REFERENCES_INLINE.search(_mask(limpo))
        coluna = _COLUNA_FK.match(limpo)
        if not achado or not coluna or _ITEM_DE_TABELA.match(limpo):
            itens.append(limpo)
            continue
        definicao = _FK_INT_PARA_BIGINT.sub(
            lambda m: "%s BIGINT" % m.group("col"),
            limpo[: achado.start()] + limpo[achado.end() :],
        )
        definicao = re.sub(r"\s+", " ", definicao).strip()
        nome = "fk_%s_%s" % (cabeca.group("tabela"), coluna.group("col"))
        fks.append(
            "CONSTRAINT %s FOREIGN KEY (%s) REFERENCES %s(%s)%s"
            % (
                nome,
                coluna.group("col"),
                achado.group("tabela"),
                achado.group("ref"),
                achado.group("acoes"),
            )
        )
        if definicao:
            itens.append(definicao)
    if not fks:
        return sql
    return "%s\n  %s\n%s" % (sql[: abre + 1], ",\n  ".join(itens + fks), sql[fecha:])


# ------------------------------------------------------------ indices (secao 5.3)
#
# O MySQL nao aceita `CREATE INDEX IF NOT EXISTS` (nao existe na gramatica) nem
# indice parcial. A clausula `IF NOT EXISTS` e resolvida na fachada (que confere
# `information_schema.statistics` antes de criar) e o parcial vira **indice
# funcional** (8.0.13+): a expressao devolve NULL quando a linha nao participa e
# o `UNIQUE` do MySQL ignora linhas com NULL em qualquer parte da chave — o
# mesmo "fora do indice" do SQLite. A coluna virtual criada e oculta, entao
# `SELECT *` continua igual (importante: `db.py` usa `SELECT *` em ~25 sites).

_CRIA_INDICE = re.compile(
    r"^\s*CREATE\s+(?P<unico>UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<nome>[A-Za-z_][\w$]*)\s+ON\s+(?P<tabela>[A-Za-z_][\w$]*)\s*"
    r"\((?P<colunas>.*?)\)\s*(?:WHERE\s+(?P<condicao>.+?))?\s*;?\s*$",
    re.I | re.S,
)
_DROPA_INDICE = re.compile(
    r"^\s*DROP\s+INDEX\s+(?:IF\s+EXISTS\s+)?(?P<nome>[A-Za-z_][\w$]*)\s*;?\s*$", re.I
)
_IS_NOT_NULL = re.compile(r"^\s*(?P<col>[A-Za-z_][\w$.]*)\s+IS\s+NOT\s+NULL\s*$", re.I)
_INDICE_NO_MYSQL = re.compile(r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\b", re.I)


def _colunas_indice(texto: str) -> list[str]:
    return [coluna.strip() for coluna in texto.split(",") if coluna.strip()]


def _so_coluna_nula(condicao: str, colunas: Sequence[str]) -> bool:
    """`WHERE col IS NOT NULL` (e `col` esta na chave) nao precisa de funcao."""
    casado = _IS_NOT_NULL.match(condicao)
    if not casado:
        return False
    alvo = casado.group("col").lower()
    return any(coluna.split()[0].lower() == alvo for coluna in colunas)


def _rewrite_indices(sql: str) -> str:
    """`CREATE [UNIQUE] INDEX ... [WHERE ...]` do SQLite para o MySQL (5.3)."""
    casado = _CRIA_INDICE.match(sql)
    if not casado:
        return sql
    unico = "UNIQUE " if casado.group("unico") else ""
    nome = casado.group("nome")
    tabela = casado.group("tabela")
    colunas = _colunas_indice(casado.group("colunas"))
    condicao = (casado.group("condicao") or "").strip()
    if not condicao or _so_coluna_nula(condicao, colunas):
        # `UNIQUE` ja ignora NULL no MySQL: identico ao parcial sem funcao.
        return "CREATE %sINDEX %s ON %s (%s)" % (unico, nome, tabela, ", ".join(colunas))
    if not casado.group("unico") and len(colunas) == 1:
        # Indice comum: so interessam as linhas que participam (5.3, item 4).
        return "CREATE INDEX %s ON %s ((CASE WHEN %s THEN %s END))" % (
            nome,
            tabela,
            condicao,
            colunas[0],
        )
    return "CREATE %sINDEX %s ON %s (%s, ((CASE WHEN %s THEN 1 END)))" % (
        unico,
        nome,
        tabela,
        ", ".join(colunas),
        condicao,
    )


def _rewrite_ddl(sql: str) -> str:
    """Ajustes de DDL (secao 5.1, regras 19-21 + indices da 5.3).

    Roda ANTES do passe de tempo para que o `DEFAULT (datetime('now'))` da DDL
    vire `DEFAULT (UTC_TIMESTAMP())` em vez de um parametro (o default so vale
    como fallback: a aplicacao sempre manda o valor).
    """
    sql = _DEFAULT_DATETIME.sub("DEFAULT (UTC_TIMESTAMP())", sql)
    sql = _DEFAULT_AGORA.sub("DEFAULT (UTC_TIMESTAMP())", sql)
    sql = _AUTOINC_PK.sub("BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY", sql)
    sql = _AUTOINC_SOZINHO.sub("AUTO_INCREMENT", sql)
    sql = _RENAME_TO.sub(
        lambda m: "RENAME TABLE %s TO %s" % (m.group("tabela"), m.group("novo")), sql
    )
    sql = _TEXTO_CHAVE.sub(_texto_para_varchar, sql)
    sql = _DEFAULT_TEXTO.sub(r"DEFAULT ('\1')", sql)
    return _adiciona_fk(_rewrite_indices(_fk_de_coluna(sql)))


# -------------------------------------------------------------- regras da DML

_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.I)
_INSERT_OR_REPLACE = re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.I)
_UPSERT = re.compile(
    r"ON\s+CONFLICT\s*\((?P<cols>[^)]*)\)\s*DO\s+UPDATE(?:\s+SET\b)?", re.I
)
_DO_NOTHING = re.compile(r"ON\s+CONFLICT\s*\((?P<cols>[^)]*)\)\s*DO\s+NOTHING", re.I)
_ON_CONFLICT = re.compile(r"\bON\s+CONFLICT\b", re.I)
_EXCLUDED = re.compile(r"\bexcluded\.", re.I)
_BEGIN_IMEDIATO = re.compile(r"^\s*BEGIN\s+(?:IMMEDIATE|EXCLUSIVE|DEFERRED)\s*;?\s*$", re.I)
_SQLITE_MASTER_LISTA = re.compile(
    r"SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'", re.I
)
_SQLITE_MASTER = re.compile(r"\bsqlite_master\b", re.I)
_TABELAS_MYSQL = (
    "SELECT table_name AS name FROM information_schema.tables "
    "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
)
# Restos de `name =` / `type =` depois da troca: a consulta pedia mais do que a
# contagem de tabelas e o MySQL nao tem essas colunas em `information_schema`.
_SQLITE_MASTER_RESTOS = re.compile(r"\bname\s*=|\btype\s*=", re.I)
_PRAGMA = re.compile(r"\bPRAGMA\b", re.I)
_PRAGMA_FK = re.compile(r"^\s*PRAGMA\s+foreign_keys\s*=\s*(ON|OFF|1|0)\s*;?\s*$", re.I)
_PRAGMA_INFO = re.compile(r"^\s*PRAGMA\s+table_info\s*\(\s*([^)]*?)\s*\)\s*;?\s*$", re.I)
_PRAGMA_ALGUM = re.compile(r"^\s*PRAGMA\s+[A-Za-z_]\w*\s*(?:=\s*[^;]*?)?\s*;?\s*$", re.I)


def _do_nothing(sql: str) -> str:
    """`ON CONFLICT (…) DO NOTHING` -> `ON DUPLICATE KEY UPDATE c = c` (regra 13).

    O MySQL nao tem `DO NOTHING`; atribuir a primeira coluna do alvo a ela mesma
    e o no-op explicito (a linha casa e nada muda).
    """
    def troca(casado: re.Match) -> str:
        primeira = casado.group("cols").split(",")[0].strip()
        return "ON DUPLICATE KEY UPDATE %s = %s" % (primeira, primeira)

    return _DO_NOTHING.sub(troca, sql)


def _lista_tabelas(sql: str) -> str:
    """`sqlite_master` -> `information_schema.tables` (regra 14).

    So a contagem de tabelas (`SELECT name ... WHERE type = 'table'`, que e o
    unico uso do projeto) tem traducao honesta. Qualquer outra leitura de DDL
    (o `SELECT sql FROM sqlite_master` de `db.py:116`, por exemplo) precisa de
    helper por backend, entao falha alto em vez de gerar consulta quebrada.
    """
    if _SQLITE_MASTER_LISTA.search(sql):
        trocado = _SQLITE_MASTER_LISTA.sub(_TABELAS_MYSQL, sql)
        if not _SQLITE_MASTER.search(trocado) and not _SQLITE_MASTER_RESTOS.search(trocado):
            return trocado
    raise SqlCompatError(
        "sqlite_master so e traduzido para listar tabelas; "
        "outras leituras de DDL precisam de helper por backend: %r" % sql[:120]
    )


def _info_colunas(bruto: str) -> str:
    """`PRAGMA table_info(x)` -> `information_schema.columns` (regra 15)."""
    nome = bruto.strip().strip("'\"`").replace("'", "''")
    return (
        "SELECT c.COLUMN_NAME AS name, c.COLUMN_TYPE AS type, "
        "IF(c.IS_NULLABLE = 'NO', 1, 0) AS `notnull`, "
        "c.COLUMN_DEFAULT AS dflt_value, "
        "IF(c.COLUMN_KEY = 'PRI', 1, 0) AS pk "
        "FROM information_schema.columns c "
        "WHERE c.TABLE_SCHEMA = DATABASE() AND c.TABLE_NAME = '%s' "
        "ORDER BY c.ORDINAL_POSITION" % nome
    )


def _sem_comentario_inicial(sql: str) -> str:
    """Tira espaco e comentario do comeco do comando.

    O `executescript` do `sqlite3` corta o script pelo `;`, entao o cabecalho do
    `schema.sql` (`-- HELPTOA ...`) chega colado no primeiro comando - e um
    `PRAGMA` com comentario na frente nao casaria com nenhuma regra.
    """
    while True:
        texto = sql.lstrip()
        if texto.startswith("--"):
            quebra = texto.find("\n")
            if quebra < 0:
                return ""
            sql = texto[quebra + 1:]
            continue
        if texto.startswith("/*"):
            fim = texto.find("*/")
            if fim < 0:
                return ""
            sql = texto[fim + 2:]
            continue
        return texto


def _rewrite_pragma(sql: str) -> str:
    """PRAGMA -> equivalente (ou nada): `journal_mode`/`busy_timeout` nao existem."""
    corpo = _sem_comentario_inicial(sql)
    fk = _PRAGMA_FK.match(corpo)
    if fk:
        return "SET FOREIGN_KEY_CHECKS = %s" % (
            "1" if fk.group(1).upper() in ("ON", "1") else "0"
        )
    info = _PRAGMA_INFO.match(corpo)
    if info:
        return _info_colunas(info.group(1))
    if _PRAGMA_ALGUM.match(corpo):
        return ""
    raise SqlCompatError("PRAGMA nao suportado: %r" % corpo)


def sem_pragma_fk(sql: str) -> str:
    """Tira do script os `PRAGMA foreign_keys` (nao valem no MySQL).

    O `schema.sql` liga a checagem de FK para o SQLite (`PRAGMA foreign_keys =
    ON`, linha 4), mas no MySQL quem manda nisso e o `db.ensure_schema`:
    `FOREIGN_KEY_CHECKS = 0` durante a criacao e `1` no fim. O PRAGMA do script
    religava a checagem no meio do `executescript` e derrubava o boot na
    primeira FK fora de ordem (erro 1824: `bot_users` e a tabela #1 e referencia
    `permission_groups`, que nasce so na #25). Medido em 8.0.44: com a checagem
    desligada o `CREATE TABLE` da filha passa e a FK fica gravada.
    """
    return "\n".join(
        linha for linha in sql.splitlines() if not _PRAGMA_FK.match(linha.strip())
    )


def _operando_esquerda(sql: str, mask: str, fim: int) -> tuple[int, str]:
    """Texto do operador a esquerda de `||` (literal, `?`, coluna ou `(…)`)."""
    i = fim - 1
    while i >= 0 and mask[i].isspace():
        i -= 1
    if i < 0:
        raise SqlCompatError("`||` sem operador a esquerda: %r" % sql[:80])
    if sql[i] == "'":
        j = i - 1
        while j >= 0:
            if sql[j] == "'":
                if j - 1 >= 0 and sql[j - 1] == "'":
                    j -= 2
                    continue
                break
            j -= 1
        if j < 0:
            raise SqlCompatError("literal nao fechado antes de `||`: %r" % sql[:80])
        return j, sql[j:i + 1]
    if mask[i] == ")":
        j = i
        profundidade = 0
        while j >= 0:
            if mask[j] == ")":
                profundidade += 1
            elif mask[j] == "(":
                profundidade -= 1
                if profundidade == 0:
                    break
            j -= 1
        if j < 0:
            raise SqlCompatError("parentese nao fechado antes de `||`: %r" % sql[:80])
        return j, sql[j:i + 1]
    if sql[i] == "?":
        return i, "?"
    j = i
    while j >= 0 and (sql[j].isalnum() or sql[j] in "_.$"):
        j -= 1
    if j == i:
        raise SqlCompatError("operador esquerdo de `||` nao reconhecido: %r" % sql[:80])
    return j + 1, sql[j + 1:i + 1]


def _operando_direita(sql: str, mask: str, ini: int) -> tuple[int, str]:
    """Texto do operador a direita de `||`."""
    i = ini
    while i < len(mask) and mask[i].isspace():
        i += 1
    if i >= len(mask):
        raise SqlCompatError("`||` sem operador a direita: %r" % sql[:80])
    if sql[i] == "'":
        j = i + 1
        while j < len(sql):
            if sql[j] == "'":
                if j + 1 < len(sql) and sql[j + 1] == "'":
                    j += 2
                    continue
                break
            j += 1
        return j + 1, sql[i:j + 1]
    if mask[i] == "(":
        j = _match_paren(mask, i)
        return j + 1, sql[i:j + 1]
    if sql[i] == "?":
        return i + 1, "?"
    j = i
    while j < len(sql) and (sql[j].isalnum() or sql[j] in "_.$"):
        j += 1
    if j == i:
        raise SqlCompatError("operador direito de `||` nao reconhecido: %r" % sql[:80])
    return j, sql[i:j]


def _rewrite_concat(sql: str) -> str:
    """`a || b` -> `CONCAT(a, b)` (regra 10: no MySQL `||` e OR logico)."""
    while "||" in _mask(sql):
        mask = _mask(sql)
        pos = mask.find("||")
        ini, esq = _operando_esquerda(sql, mask, pos)
        fim, dir_ = _operando_direita(sql, mask, pos + 2)
        sql = sql[:ini] + "CONCAT(%s, %s)" % (esq, dir_) + sql[fim:]
    return sql


def _placeholders(sql: str) -> str:
    """`?` -> `%s`, so fora de literal/comentario (regra 1)."""
    mask = _mask(sql)
    if "?" not in mask:
        return sql
    partes: list[str] = []
    ultimo = 0
    for i, c in enumerate(mask):
        if c == "?":
            partes.append(sql[ultimo:i])
            partes.append("%s")
            ultimo = i + 1
    partes.append(sql[ultimo:])
    return "".join(partes)


# ------------------------------------------------------------------- tradutor

_DATETIME_NOW = re.compile(r"\bdatetime\s*\(\s*'now'", re.I)


def convert_params(params: Sequence[Any] | None) -> list[Any] | None:
    """Converte valores que o banco/driver nao aceitam do jeito que vem (secao 4.2).

    `datetime`/`date` viram texto no formato canonico (o mesmo que o SQLite ja
    gravava), `bool` vira `0/1` (o SQLite tambem nao tinha booleano) e `None`
    segue `None` (NULL). Tipo sem adaptador (`dict`, objeto) falha alto: o
    `sqlite3` tambem recusaria.
    """
    if params is None:
        return None
    if not isinstance(params, (list, tuple)):
        raise SqlCompatError(
            "parametros precisam ser lista/tupla (marcador posicional `?`); veio %s"
            % type(params).__name__
        )
    return [_um_param(valor) for valor in params]


def _um_param(valor: Any) -> Any:
    if isinstance(valor, datetime):
        if valor.tzinfo is not None:
            valor = valor.astimezone(timezone.utc)
        return valor.strftime(TS_FMT)
    if isinstance(valor, date):
        return valor.strftime("%Y-%m-%d")
    if isinstance(valor, bool):
        return int(valor)
    if valor is None or isinstance(valor, (str, int, float, bytes, bytearray)):
        return valor
    raise SqlCompatError("parametro de tipo sem adaptador: %s" % type(valor).__name__)


def _exige_codigo(mask: str, sql: str, regex: re.Pattern) -> None:
    """Garante que a regra nao pegou texto de dentro de literal/comentario."""
    for casado in regex.finditer(sql):
        ini, fim = casado.span()
        if mask[ini:fim] != sql[ini:fim]:
            raise SqlCompatError(
                "regra casada dentro de literal/comentario: %r" % sql[ini:fim]
            )


def _confere_params(sql: str, dados: list[Any] | None) -> None:
    """Cada `%s` final precisa de exatamente um valor (falha alto, nao no driver).

    A contagem e feita na mascara, entao `%s`/`?` dentro de literal ou comentario
    nao contam; o `datetime('now')` injetado ja entrou na lista de parametros.
    """
    total = _mask(sql).count("%s")
    recebidos = 0 if dados is None else len(dados)
    if total != recebidos:
        raise SqlCompatError(
            "%d marcador(es) `%%s` para %d parametro(s) - a contagem de `?` nao "
            "fecha: %r" % (total, recebidos, sql[:120])
        )


def translate(sql: str, params: Sequence[Any] | None = None) -> tuple[str, list[Any] | None]:
    """Traduz um comando do dialeto SQLite para MySQL 8.0.19+ (secoes 5.1 a 5.5).

    Devolve `(sql, params)`: a lista pode ficar **menor** (o modificador de data
    que veio por parametro foi consumido) ou **maior** (entrou o instante UTC
    calculado aqui). Quem executa usa sempre a lista devolvida — nunca a de
    entrada.
    """
    if not isinstance(sql, str):
        raise SqlCompatError("SQL precisa ser texto; veio %s" % type(sql).__name__)
    # O `executescript` corta o script pelo `;`, entao o cabecalho comentado do
    # `schema.sql` chega colado no primeiro comando e o `CREATE TABLE` nao seria
    # reconhecido como DDL. Comentario no comeco nao e SQL: sai fora.
    sql = _sem_comentario_inicial(sql)
    dados = convert_params(params)
    if dados or _DATETIME_NOW.search(sql):
        # Regra 2: o driver faz `sql % params`, entao `%` literal vira `%%`.
        sql = sql.replace("%", "%%")
    if _DDL.match(sql):
        sql = _rewrite_ddl(sql)
    if _TIME_CALL.search(sql):
        sql, dados = _rewrite_time(sql, dados)
    mask = _mask(sql)
    if _INSERT_OR_REPLACE.search(mask):
        raise SqlCompatError(
            "`INSERT OR REPLACE` nao tem equivalente honesto no MySQL "
            "(o REPLACE apaga e reinsere, disparando CASCADE): reescreva a consulta"
        )
    if _INSERT_OR_IGNORE.search(mask):
        _exige_codigo(mask, sql, _INSERT_OR_IGNORE)
        sql = _INSERT_OR_IGNORE.sub("INSERT IGNORE", sql)
    if _DO_NOTHING.search(mask):
        sql = _do_nothing(sql)
    if _UPSERT.search(mask):
        _exige_codigo(mask, sql, _UPSERT)
        _exige_codigo(mask, sql, _EXCLUDED)
        sql = _EXCLUDED.sub("new.", _UPSERT.sub("AS new ON DUPLICATE KEY UPDATE", sql))
    if _ON_CONFLICT.search(_mask(sql)):
        raise SqlCompatError("ON CONFLICT em formato nao suportado: %r" % sql[:120])
    if _SQLITE_MASTER.search(mask):
        sql = _lista_tabelas(sql)
    if _PRAGMA.search(_mask(sql)):
        sql = _rewrite_pragma(sql)
    if _BEGIN_IMEDIATO.match(sql):
        sql = "START TRANSACTION"
    if "||" in _mask(sql):
        sql = _rewrite_concat(sql)
    sql = _escapa_identificadores(sql)
    sql = _placeholders(sql)
    _confere_params(sql, dados)
    return sql, dados


def split_statements(script: str) -> list[str]:
    """Divide um script em comandos pelo `;` de nivel 0 (mascara respeita literais)."""
    mask = _mask(script)
    comandos: list[str] = []
    inicio = 0
    for i, c in enumerate(mask):
        if c == ";":
            pedaco = script[inicio:i].strip()
            if pedaco:
                comandos.append(pedaco)
            inicio = i + 1
    resto = script[inicio:].strip()
    if resto:
        comandos.append(resto)
    return comandos


# ----------------------------------------------------------- catalogo (DDL)


def table_ddl(conn, nome: str) -> str:
    """`CREATE` da tabela: o unico DDL que o `sqlite_master` nao traduz.

    O texto do `CREATE TABLE` nao tem dialeto comum, entao sai por aqui: no
    SQLite e o proprio `sql` do `sqlite_master`; no MySQL, o `SHOW CREATE TABLE`
    (que nao passa pelo tradutor - vai direto no driver). Quem precisa conferir
    o CHECK de uma tabela (`db.py:116`, o `run_script` de `menu_options`) usa
    esta funcao em vez de consultar `sqlite_master` na mao. Tabela ausente
    devolve `''` nos dois backends.
    """
    if isinstance(conn, _MySQLConnection):
        bruto = conn.bruto.cursor()
        try:
            bruto.execute("SHOW CREATE TABLE `%s`" % nome.replace("`", "``"))
            linha = bruto.fetchone()
        except ProgrammingError:      # 1146: tabela nao existe
            return ""
        finally:
            bruto.close()
        if not linha:
            return ""
        colunas = list(linha)
        return colunas[1] if len(colunas) > 1 else ""
    linha = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (nome,)
    ).fetchone()
    if linha is None:
        return ""
    if isinstance(linha, dict):
        return linha.get("sql") or ""
    return linha[0] or ""


# ------------------------------------------------------------------- conexao

_DESCONEXAO = (2006, 2013, 2014, 2015, 4031)
_MARCAS_DESCONEXAO = ("gone away", "lost connection", "server has gone", "broken pipe")

try:  # pragma: no cover - depende do ambiente
    import pymysql as _pymysql
except Exception:  # pragma: no cover - sqlite puro nao precisa do pacote
    _pymysql = None

if _pymysql is not None:
    IntegrityError = _pymysql.IntegrityError
    OperationalError = _pymysql.OperationalError
    ProgrammingError = _pymysql.ProgrammingError
else:
    class OperationalError(Exception):
        """Fallback quando o PyMySQL nao esta instalado (backend sqlite)."""

    class IntegrityError(Exception):
        """Fallback quando o PyMySQL nao esta instalado (backend sqlite)."""

    class ProgrammingError(Exception):
        """Fallback quando o PyMySQL nao esta instalado (backend sqlite)."""


def is_disconnect(exc: BaseException) -> bool:
    """True quando a conexao caiu e vale reconectar (2006/2013/2014/2015/4031)."""
    codigo = exc.args[0] if exc.args else None
    if isinstance(codigo, int) and codigo in _DESCONEXAO:
        return True
    texto = str(exc).lower()
    return any(marca in texto for marca in _MARCAS_DESCONEXAO)


_HANDSHAKE = (
    "SET time_zone = '+00:00'",
    "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
    "SET SESSION innodb_lock_wait_timeout = 5",
    "SET SESSION sql_mode = ''",
)


def _driver_obrigatorio():
    if _pymysql is None:
        raise RuntimeError(
            "PyMySQL ausente: rode `pip install -r requirements.txt` "
            "(ou use DB_BACKEND=sqlite para voltar ao SQLite)"
        )
    return _pymysql


class _MySQLCursor:
    """Cursor com a cara do `sqlite3.Cursor` (linha de `dict` por padrao)."""

    def __init__(self, conexao: "_MySQLConnection", bruto):
        self._conexao = conexao
        self._bruto = bruto
        self._ignorado = False
        self.arraysize = 1

    def execute(self, sql, params=None):
        texto, novos = translate(sql, params)
        if not texto.strip():
            self._ignorado = True      # PRAGMA sem equivalente: nada a executar
            return self
        texto = self._ajusta_indice(sql, texto)
        if texto is None:
            self._ignorado = True      # indice ja existe / ja nao existe: no-op
            return self
        self._ignorado = False
        self._bruto.execute(texto, tuple(novos) if novos else None)
        return self

    def executemany(self, sql, seq):
        linhas = [list(linha) for linha in seq]
        if not linhas:
            self._ignorado = True
            return self
        texto, _ = translate(sql, linhas[0])       # o texto nao depende do valor
        lotes = []
        for linha in linhas:
            _, novos = translate(sql, linha)       # o "agora" e por linha
            lotes.append(tuple(novos or ()))
        self._ignorado = False
        self._bruto.executemany(texto, lotes)
        return self

    def executescript(self, script):
        for comando in split_statements(script):
            self.execute(comando)
        return self

    def _consulta(self, sql, params=None):
        """Consulta crua na conexao do driver (usada pelas regras de catalogo)."""
        bruto = self._conexao.bruto.cursor()
        try:
            bruto.execute(sql, params)
            return bruto.fetchall()
        finally:
            bruto.close()

    def _ajusta_indice(self, original, texto):
        """`CREATE/DROP INDEX ... IF [NOT] EXISTS`, que o MySQL nao expressa (5.3).

        O SQLite da os dois de graca: `CREATE INDEX IF NOT EXISTS` nao refaz o
        indice e `DROP INDEX IF EXISTS <nome>` (sem tabela!) nunca falha. Como
        o MySQL nao tem nenhuma das duas clausulas, a fachada confere o
        catalogo e devolve `None` quando a instrucao virou no-op.
        """
        cria = _CRIA_INDICE.match(original)
        if cria and "IF NOT EXISTS" in original.upper():
            if self._indice_existe(cria.group("tabela"), cria.group("nome")):
                return None            # ja existe: nada a executar
            return texto
        dropa = _DROPA_INDICE.match(original.strip())
        if dropa and "IF EXISTS" in original.upper():
            tabela = self._tabela_do_indice(dropa.group("nome"))
            if tabela is None:
                return None            # nao existe: nada a fazer
            return "DROP INDEX %s ON %s" % (dropa.group("nome"), tabela)
        return texto

    def _indice_existe(self, tabela, indice):
        linhas = self._consulta(
            "SELECT index_name FROM information_schema.statistics"
            " WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
            (tabela, indice),
        )
        return bool(linhas)

    def _tabela_do_indice(self, indice):
        linhas = self._consulta(
            "SELECT table_name FROM information_schema.statistics"
            " WHERE table_schema = DATABASE() AND index_name = %s",
            (indice,),
        )
        for linha in linhas:
            valor = linha[0] if not isinstance(linha, dict) else list(linha.values())[0]
            if valor:
                return valor
        return None

    def _linha(self, linha):
        if linha is None:
            return None
        fabrica = getattr(self._conexao, "row_factory", None)
        return fabrica(self, linha) if fabrica else linha

    def fetchone(self):
        if self._ignorado:
            return None
        return self._linha(self._bruto.fetchone())

    def fetchall(self):
        if self._ignorado:
            return []
        return [self._linha(linha) for linha in self._bruto.fetchall()]

    def fetchmany(self, size=None):
        if self._ignorado:
            return []
        quantos = self.arraysize if size is None else size
        return [self._linha(linha) for linha in self._bruto.fetchmany(quantos)]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def description(self):
        return self._bruto.description

    @property
    def rowcount(self):
        return self._bruto.rowcount

    @property
    def lastrowid(self):
        return self._bruto.lastrowid

    def close(self):
        self._bruto.close()


class _MySQLConnection:
    """Fachada da conexao MySQL: mesma cara do `sqlite3.Connection`."""

    def __init__(self, bruto, row_factory=None):
        self._bruto = bruto
        self.row_factory = row_factory

    @property
    def bruto(self):
        """Conexao do driver (para quem precisa de algo so do PyMySQL)."""
        return self._bruto

    def cursor(self, *args, **kwargs):
        return _MySQLCursor(self, self._bruto.cursor(*args, **kwargs))

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def executemany(self, sql, seq):
        return self.cursor().executemany(sql, seq)

    def executescript(self, script):
        return self.cursor().executescript(script)

    def commit(self):
        self._bruto.commit()

    def rollback(self):
        self._bruto.rollback()

    def close(self):
        self._bruto.close()

    def ping(self, reconnect=True):
        return self._bruto.ping(reconnect=reconnect)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def _sinalizadores(pacote) -> int:
    """`CLIENT.FOUND_ROWS`: `rowcount` com semantica de linhas casadas (secao 4.2).

    Sem isso, um `UPDATE` que grava o mesmo valor devolveria `0` e o codigo
    trataria \"nada mudou\" como \"nao achou o registro\".
    """
    cliente = getattr(getattr(pacote, "constants", None), "CLIENT", None)
    if cliente is None or not hasattr(cliente, "FOUND_ROWS"):
        raise RuntimeError("driver MySQL sem `constants.CLIENT.FOUND_ROWS`")
    return int(cliente.FOUND_ROWS)


def connect_mysql(dsn=None, row_factory=None, driver=None) -> _MySQLConnection:
    """Abre a conexao MySQL com o handshake da secao 5.5.d.

    `driver` existe para teste (duplo): em producao e o PyMySQL.
    """
    pacote = driver or _driver_obrigatorio()
    info = dict(dsn or config.mysql_dsn())
    info.setdefault("charset", "utf8mb4")
    bruto = pacote.connect(
        **info, autocommit=False, client_flag=_sinalizadores(pacote)
    )
    cursor = bruto.cursor()
    try:
        for comando in _HANDSHAKE:
            cursor.execute(comando)
    finally:
        cursor.close()
    bruto.commit()
    return _MySQLConnection(bruto, row_factory=row_factory)


def _connect_sqlite(sqlite_path=None, row_factory=None):
    """Conexao SQLite com os PRAGMAs do projeto (setup identico ao de sempre)."""
    import sqlite3

    caminho = sqlite_path or config.sqlite_path()
    conn = sqlite3.connect(str(caminho), timeout=5.0, check_same_thread=False)
    if row_factory is not None:
        conn.row_factory = row_factory
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect(sqlite_path=None, backend=None, row_factory=None, dsn=None, driver=None):
    """Abre a conexao do backend configurado (`DB_BACKEND`; padrao `sqlite`).

    E o unico ponto de conexao do bot: com `sqlite` devolve o mesmo
    `sqlite3.Connection` de hoje (PRAGMAs inclusos, `row_factory` = o
    `_dict_factory` do `db.py`) e com `mysql` a fachada traduzida. Trocar o
    `.env` de volta para `DB_BACKEND=sqlite` desfaz tudo.
    """
    alvo = (backend or config.db_backend() or SQLITE).strip().lower()
    if alvo != MYSQL:
        return _connect_sqlite(sqlite_path, row_factory)
    return connect_mysql(dsn=dsn, row_factory=row_factory, driver=driver)
