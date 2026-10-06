# botsqlite → MySQL 8 — plano de mudança completo (fase a fase)

**Status:** EM EXECUCAO — Fases 1, 2 e 3 concluidas e medidas: suite Python **501 casos / 0 falhas**,
DDL + carga de **31/31 tabelas** sem 1054 (`--check` = 0 diferenca) e o painel PHP operando em MySQL
8.0.44 (**MYSQL PAINEL OK**: 13 abas, gravacao, colacao e filtros de data). A **Fase 4 esta em ensaio**
(bot real em `DB_BACKEND=mysql`): o boot E2E expos dois erros de runtime que o SQLite escondia —
armadilhas 28 e 29 — ja corrigidos no tradutor (regra 26 + caminho linha-a-linha do `executemany`,
4.2), com o boot subindo limpo e a suite reexecutada em 501/0. Falta: ensaio com dados
reais (Fase 5) e cutover (Fase 6). Nada foi aplicado na base corporativa `chat_bot_toa`.
**Alvo:** bot Python + painel PHP operando 100% em **MySQL 8.0.44** (servidor corporativo
`10.70.107.52`, base `chat_bot_toa`), **mantendo** a suíte de testes rodando **offline** (SQLite
em memória / arquivo temporário) na máquina de desenvolvimento.

---

## 0. Como usar este documento

1. Leia as seções 1–6 (o **porquê** e o **o que** muda).
2. A execução real está na **seção 7 (Fases 0–6)**. Cada fase tem:
   - **Objetivo**, **Arquivos**, **Passos**;
   - **GATE** = comando(s) de validação + **critério de aceite** + "**não avance se…**";
   - **Rollback** (como voltar atrás sem perder dados).
3. As fases são **cumulativas e bloqueantes**: só se avança para a fase N+1 com o GATE da fase N
   verde. Se um GATE falhar, para, corrige e reexecuta o GATE (não se avança "no escuro").
4. Nada de `DB_BACKEND=mysql` por padrão em **nenhum** arquivo versionado: o default é
   sempre `sqlite`. MySQL só liga por `.env`/ambiente explícito do servidor.
5. Fecham o documento: §8 (dados do ambiente que faltam), §9 (riscos aceitos e fora de escopo) e
   §10 (checklist final de aceite).

---

## 1. Objetivo, escopo e não-objetivos

**Objetivo**

- Bot Python gravando/lendo **apenas MySQL** em produção.
- Painel PHP acessando o **mesmo** MySQL (base `chat_bot_toa`).
- Migrar os dados existentes do SQLite (`data/helptoa.sqlite`) para o MySQL sem perda.
- **Todos** os testes Python (**24 arquivos / 501 casos** — medidos agora, todos verdes; 23 deles
  são os que já existiam) e as
  suítes PHP (12 arquivos + `tests/_panel_render.php`) continuam
  rodando **localmente e offline** contra SQLite, sem tocar no banco corporativo.

**Não-objetivos (agora)**

- Trocar SQLite por outro backend no painel remoto para desenvolvimento local (o painel local
  segue SQLite por padrão).
- Reescrever o motor de atendimento, o painel ou o esquema lógico (nenhuma tabela muda de
  significado; só de dialeto/tipos).
- Multi-tenant/SaaS, réplica, sharding, pooler externo (ProxySQL) — fora de escopo.

**Restrição dura de segurança**

> Nenhum teste (Python ou PHP) pode abrir conexão com `10.70.107.52` por acidente. O acesso
> a MySQL em teste é **opt-in**: `MYSQL_TEST=1` + `MYSQL_TEST_DATABASE` (banco de teste
> dedicado). Sem isso, tudo roda em SQLite.

---

## 2. Decisão de arquitetura — Caminho A (adotado)

**Ideia:** o código continua escrito em **dialeto SQLite** (`?`, `ON CONFLICT`,
`datetime('now')`, `INSERT OR IGNORE`, `sqlite3`-like cursor). Uma **camada de tradução**
(`python/sqlcompat.py` no Python e `BotPDO`/`BotStatement` no PHP) converte na hora para
MySQL quando `DB_BACKEND=mysql`, e vira *pass-through* quando `DB_BACKEND=sqlite`.

**Por quê este caminho**

| Critério | Caminho A (shim) | Caminho B (reescrever 200+ sites SQL) |
|---|---|---|
| Risco de regressão no bot | baixo (SQL lógico intacto) | alto (200 sites manuais + PHP) |
| Testes offline | **mantidos tal e qual** | precisam de banco real ou doubles |
| Diff/PR revisável | 1 arquivo novo por linguagem + pontos pontuais | dezenas de arquivos tocados |
| Dívida futura | 1 dialeto canônico + 1 tradutor | 2 dialetos misturados no código |

**Consequência assumida:** o tradutor é um componente com contrato próprio e **precisa de
teste** (`tests/test_sqlcompat.py`), porque ele é o único lugar onde o SQL "mente" sobre o
dialeto real.

**Choke points (poucos lugares onde a decisão de dialeto vive):**

- Python: `python/db.py::connect()` (única fábrica de conexão) + `DB_BACKEND` em `config.py`.
- PHP: `php/bootstrap.php::bot_pdo()` (única fábrica de PDO) + `bot_ensure_schema()`.

**Compatibilidade do shim (contrato mínimo exigido pelo código atual):**

- `conn.execute(sql, params)` → cursor; `cursor.fetchone()/fetchall()/fetchmany()/__iter__`.
- `cursor.rowcount`, `cursor.lastrowid`, `cursor.description`.
- `conn.commit()`, `conn.rollback()`, `conn.close()`, `conn.executescript(sql)`.
- `conn.row_factory = db._dict_factory` (atributo aceito; no MySQL o retorno já é `dict`).
- Exceções: `db.IntegrityError` / `db.OperationalError` / `db.ProgrammingError` apontando
  para as classes do PyMySQL quando backend=mysql (e para `sqlite3` quando backend=sqlite).

---

## 3. Ambiente alvo (fatos medidos, não supostos)

Descoberta **read-only** via ODBC (sem cliente MySQL instalado localmente):

| Item | Valor observado | Impacto na migração |
|---|---|---|
| Versão | MySQL **8.0.44** | suporta `CHECK`, CTE, `INSERT … AS new ON DUPLICATE KEY`, colunas geradas |
| Engine | **InnoDB** | transações/FK reais; `wait_timeout` importa |
| Charset/collation | **utf8mb4** (+ ci por default) | resolver com `utf8mb4_bin` nas colunas de igualdade e `ai_ci` nos 4 `LIKE` (5.5c); sem isso `=`/`UNIQUE` de `login`, `address`, `slug`, `case_key` ficariam *case-insensitive* (≠ SQLite) |
| `sql_mode` | **vazio** | sem `STRICT_TRANS_TABLES`: truncamento silencioso pode reaparecer (validar entrada no app) |
| `time_zone` | **SYSTEM = -03** | `NOW()` ≠ `datetime('now')` (UTC). Por isso o tradutor **não** usa funções de data do servidor para "agora" (5.4) |
| `wait_timeout` | **300 s** | o bot é de longa duração → precisa de `ping()`/reconexão na camada de conexão |
| `lower_case_table_names` | **0** | nomes de tabela/coluna são *case-sensitive* → os nomes atuais (minúsculos) devem ser preservados |
| Base `chat_bot_toa` | existe e está **vazia** | não é preciso dropar nada; migração pode semear do zero |
| Permissões do usuário | `ALL PRIVILEGES` **só** em `chat\_bot\_toa` (`SHOW GRANTS`: `USAGE ON *.*` + `ALL ON chat\_bot\_toa.*`); a base `botsqlite_test` **não** aparece no `SHOW DATABASES` dele | conectar com essa credencial em base de teste dá **1044** (`Access denied ... to database 'botsqlite_test'`) — não é limite de conexões. Por isso o gate da Fase 2/3 roda em um **MySQL 8 local de teste** (`127.0.0.1:3307`, datadir `C:\tools\mysql-data`) |
| Driver MySQL local (PHP) | `pdo_mysql` **habilitado** em `C:\tools\php83\php.ini` (linha 954, 06/10/2026) | era o unico bloqueio do painel em MySQL; o DLL ja vinha na pasta `ext` |
| Driver MySQL local (Python) | **nenhum** instalado | Fase 0 instala `PyMySQL` (PyPI:443 alcançável) |
| Driver ODBC | presente (usado para inspecionar) | permite auditorias sem instalar cliente MySQL |

**Ponto crítico de data/hora:** hoje o SQLite grava UTC em todas as colunas `*_at`/`at` (por
`datetime('now')`). O MySQL com `time_zone=-03` gravaria **o horário errado em 3 h**. Por isso:
(a) o valor de "agora" é **calculado em Python/PHP** (UTC) e entra como parâmetro (5.4);
(b) `UTC_TIMESTAMP()` só é usado na **DDL** (`DEFAULT (UTC_TIMESTAMP())`, 4.6.6). **Nunca**
`NOW()`, nem dentro de `DATE(...)`/comparações.


---

## 4. Inventário completo — **tudo** que precisa mudar

Nada abaixo é opcional: é a lista fechada do que está acoplado a SQLite hoje.

### 4.1 `python/config.py` (aditivo, ~25 linhas)

Hoje expõe `load_env()`, `env(key, default)`, `sqlite_path()`, helpers de proxy. Adicionar:

```python
def db_backend() -> str        # "sqlite" (default) | "mysql"; valida e erra cedo
def mysql_params() -> dict     # host/port/user/password/database/charset/autocommit
def mysql_configured() -> bool # todas as chaves presentes? (usado no log de boot)
```

- Lê `DB_BACKEND`, `IP_MYSQL`, `PORTA_MYSQL` (default `3306`), `USUARIO_MYSQL`, `SENHA_MYSQL`,
  `DATABASE_MYSQL` — nomes **já existentes** no `.env` (conferido: `SQLITE_PATH`,
  `USUARIO_MYSQL`, `SENHA_MYSQL`, `DATABASE_MYSQL`, `IP_MYSQL`, `PORTA_MYSQL`).
- `db_backend()` **default `sqlite`**: sem `DB_BACKEND` no ambiente, nenhum teste/run local
  encosta no servidor corporativo. Se `DB_BACKEND=mysql` e faltar chave → `RuntimeError` com a
  lista das chaves ausentes (nunca falha silenciosa).
- `sqlite_path()` continua existindo (usado por testes e pelo modo SQLite).

### 4.2 `python/sqlcompat.py` (NOVO — o coração da mudança)

Fachada que entrega "um `sqlite3` de mentira" quando o backend é MySQL.

```
sqlcompat.connect(sqlite_path, backend) -> Connection
  - backend="sqlite": delega ao sqlite3 (PRAGMAs + _dict_factory como hoje, pass-through)
  - backend="mysql" : PyMySQL + CLIENT.FOUND_ROWS (LIGADO) + utf8mb4 + autocommit=False
    + `SET time_zone='+00:00'`/`SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED`
    + tradução de SQL
sqlcompat.translate(sql, params=None) -> (sql, params)   # reescrita de dialeto (secao 5)
                                                          # devolve params porque a familia
                                                          # de datas consome/insere parametros
sqlcompat.convert_params(params)       # datetime/date/bool/None -> tipos aceitos pelo driver
sqlcompat.is_disconnect(exc) -> bool   # 2006, 2013, 2014, 2015, 4031 -> reconectar/ping
sqlcompat.IntegrityError / OperationalError / ProgrammingError  # aliases do backend
```

Classes internas:

- `_MySQLConnection`: `cursor()`, `execute()`, `executescript()`, `commit()`, `rollback()`,
  `close()`, `ping()`; aceita `.row_factory` (ignorado, o retorno já é `dict`).
- `_MySQLCursor`: `execute()`, `executemany()`, `fetchone/fetchall/fetchmany`, `__iter__`,
  `rowcount`, `lastrowid`, `description`, `close()`, `arraysize`.
  O `executemany` tem **dois caminhos**: usa o lote do driver (que cola `(…),(…),(…)` via
  `RE_INSERT_VALUES`) só quando **todo** `%s` está dentro do grupo do `VALUES`; se o tradutor
  injetou parâmetro **fora** dele (o `datetime('now')` do `ON DUPLICATE KEY`), o lote do driver
  não existe — o `postfix` é concatenado sem formatação — e a fachada executa **linha a linha**
  (armadilha 29). O `INSERT` do migrador (`INSERT IGNORE … VALUES (…)`) continua no lote.

**Contrato de compatibilidade com o código atual (não negociável):**

1. `rowcount` com **semântica de linhas casadas** → `CLIENT.FOUND_ROWS` **LIGADO** (equivale a
   `MYSQL_ATTR_FOUND_ROWS=true` no PDO). Motivo: o `changes()` do SQLite conta a linha mesmo
   quando o `UPDATE` grava o **mesmo valor**; sem a flag, o MySQL devolveria `0` nesse caso e o
   `if rowcount == 0` do `claim_delivery` (db.py:1379), o `bool(rowcount)` (db.py:1497) e as
   contagens de `db.py:1124/1239` tratariam "nada mudou" como "não achou o registro".
   **Exceção a auditar:** em `INSERT … ON DUPLICATE KEY UPDATE` o MySQL devolve `1` (inseriu),
   `2` (atualizou de fato) ou `1` (não mudou, com a flag) — diferente do `1` único do SQLite.
   Nenhum dos 6 sites do `db.py` é upsert (são `DELETE`/`UPDATE`), mas o gate da Fase 2 procura
   comparações `rowcount == 1`/`rowCount() === 1` em ambos os projetos.
2. Datas: `datetime('now'…)` (e a familia `julianday`) resolvidas pela regra 5.4 - o valor UTC e
   **calculado na aplicacao** e entra como `%s` (quando o modificador vem por parametro, ele sai
   da lista); o tradutor devolve `(sql, params)` justamente por isso. Parametros `datetime`/`date`
   viram string `'YYYY-MM-DD HH:MM:SS'` (mesmo formato textual que o SQLite ja grava nas colunas).
   `UTC_TIMESTAMP()` so aparece na **DDL** (`DEFAULT (UTC_TIMESTAMP())`).
3. `?` → `%s` e, **quando a query tem parâmetros**, `%` literal → `%%` (senão o driver
   interpreta como placeholder; hoje existe `LIKE 'u:%'` no código de migração).
4. Nenhum `NOW()`/`CURDATE()` gerado pelo tradutor, nunca.

### 4.3 `python/db.py` (1615 linhas, 94 funções, ~200 sites SQL)

**Regra:** só tocar onde há dialeto. Nomes/assinaturas/retornos das funções **não mudam** —
é isso que preserva os **487 casos** de teste (24 arquivos).

| Linha(s) | Hoje (SQLite) | Depois | Observação |
|---|---|---|---|
| 16 | `_dict_factory` | mantém | só o backend sqlite usa |
| 20–27 | `connect()` + `PRAGMA foreign_keys/WAL/busy_timeout` | bifurca em `sqlcompat.connect()`; PRAGMAs só no SQLite | MySQL: `FOUND_ROWS`, utf8mb4, sem `journal_mode` |
| 43 | `SELECT name FROM sqlite_master WHERE type='table'` | `information_schema.tables` | usado por `ensure_schema` |
| 64 | `PRAGMA table_info(t)` | `information_schema.columns` | devolve os mesmos campos consumidos hoje |
| 106–121 | `DROP INDEX IF EXISTS menu_options_per_menu` + `SELECT sql FROM sqlite_master …menu_options` | `DROP INDEX`: resolvido pela fachada (regra 5.3); DDL da tabela: `sqlcompat.table_ddl(conn, "menu_options")` | SQLite: `sqlite_master`; MySQL: `SHOW CREATE TABLE` (direto no driver). No MySQL o DDL já nasce com `script_slug` → **sem rebuild** |
| 123–164 | `_rebuild_menu_options_check` (recria tabela + `PRAGMA foreign_keys=OFF/ON`) | `SET FOREIGN_KEY_CHECKS=0/1` via tradutor, com `conn.commit()` antes | MySQL ignora `FOREIGN_KEY_CHECKS` dentro de transação aberta (e o driver não é autocommit) → o `commit()` precede o toggle. Só dispara com o `CHECK` antigo |
| 214, 221 | `INSERT OR IGNORE INTO group_teams/group_queues` | `INSERT IGNORE` | via tradutor |
| 287 | `expires_at > datetime('now')` | valor UTC calculado na aplicacao (5.4) | comparação textual preservada |
| 330–332 | `CREATE UNIQUE INDEX … pending_routes_active … WHERE status NOT IN (...)` (**parcial**) | MySQL: índice funcional (seção 5.3) | 5º índice parcial (contagem final: **5**) |
| 390 | `SET case_key = 'a:' \|\| case_key` | `CONCAT('a:', case_key)` | única concatenação "de verdade" |
| 448/472/473 | DDL `AUTOINCREMENT` + `DEFAULT (datetime('now'))` | DDL MySQL (4.6) | usado por `_migrate_load` |
| 508 | `(julianday('now') - julianday(at)) * 86400 AS idade` | `TIMESTAMPDIFF(SECOND, at, %s)` (valor UTC calculado na aplicacao) | não existe `julianday` no MySQL |
| 546/682/703/837/848/881/889/952/1529 | `ON CONFLICT (…) DO UPDATE … excluded.x` | `ON DUPLICATE KEY UPDATE … = new.x` (seção 5.2) | **9 sites no `db.py` (16 no projeto)** |
| 670 | `BEGIN IMMEDIATE` | `START TRANSACTION` | |
| 1023/1099/1390 | `cur.lastrowid` | idem (PyMySQL fornece) | sem mudança de código |
| 608/1124/1239/1379/1497/1566 | `cur.rowcount` | idem, garantido por `FOUND_ROWS` **ligado** | coberto por teste dedicado |
| 34 sites `datetime('now' …)` | ver lista abaixo | valor UTC calculado na aplicacao (5.4) | **nenhum** `NOW()` |

`datetime('now')` — **34** ocorrências: linhas **287, 448, 473, 545, 547, 575, 595, 684, 838,
883, 939, 951, 952, 964, 981, 997, 1034, 1044, 1060, 1091, 1254, 1265, 1335, 1371, 1372, 1376,
1386, 1387, 1432, 1493, 1538, 1552, 1561, 1563**.

Concatenação `||` — **9** ocorrências: **390** (`'a:' || case_key`) e 595, 981, 997, 1091, 1265,
1372, 1387, 1563 (as últimas são `datetime('now', '-' || ? || ' seconds')` → valor calculado na
aplicação, 5.4).

### 4.4 `python/main.py`, `python/engine.py`, `run.py`

- `db.connect()` é o **único** ponto de conexão do bot (Fase 2 confirma: nenhum
  `sqlite3.connect` fora de `db.py`/shim) → nenhum SQL a mudar nesses arquivos.
- **`engine.py:549`**: `asyncio.to_thread(db.ingest_search_contact, …)` abre **conexão
  própria**. No MySQL isso é 1 sessão por thread: precisa seguir pelo shim e ser fechada
  (`finally: conn.close()`), senão o servidor acumula sessões até o `max_connections`.
- Boot/loop: log de backend (`db backend=mysql host=… base=…`, **sem senha**) e `ping()`/
  reconexão — o bot fica ocioso além de `wait_timeout=300`.
- `run.py` / `bin/deploy.sh`: sem dialeto; só refletem o `.env` do servidor.

### 4.5 `requirements.txt`

- Adicionar `PyMySQL==1.1.1` (puro Python, sem compilar, sem client MySQL no servidor).
- Alternativa considerada: `mysqlclient` (C ext, precisa de build tools) — **rejeitada**.
- Se `DB_BACKEND=mysql` e o pacote não estiver instalado → erro claro no boot
  ("PyMySQL ausente: pip install -r requirements.txt"), nunca `ImportError` cru no meio.

### 4.6 `schema.sql` → `schema.mysql.sql` (NOVO, espelho do atual)

`php bootstrap` e `python ensure_schema` passam a escolher o arquivo pelo backend. Não é
tradução automática: é DDL MySQL escrita à mão, tabela por tabela, preservando **os mesmos
nomes** (colunas/tabelas minúsculas, `lower_case_table_names=0`).

**31 tabelas a reproduzir** (ordem do `schema.sql`):

| # | Tabela | Notas MySQL obrigatórias |
|---|---|---|
| 1 | `bot_users` | `id BIGINT AUTO_INCREMENT`; `login VARCHAR(60)` **UNIQUE** (utf8mb4_ci já é case-insensitive) |
| 2 | `queues` | `address` PK/UNIQUE; `name`, `active TINYINT(1)` |
| 3 | `transfer_targets` | `id AUTO_INCREMENT`; `address` UNIQUE |
| 4 | `templates` | `slug VARCHAR(60) UNIQUE`; `body` TEXT; `updated_at` default UTC |
| 5 | `menus` | `id AUTO_INCREMENT` |
| 6 | `menu_queues` | PK/UNIQUE composta (`menu_id`,`queue_address`) + 2 FKs |
| 7 | `teams` | `id AUTO_INCREMENT` |
| 8 | `team_members` | `address` UNIQUE em (team_id, address) |
| 9 | `team_queues` | idem para filas |
| 10 | `menu_options` | maior risco: `CHECK` de `script_slug`/`kind`, `parent_id` self-FK, e os 3 **índices únicos parciais** (5.3) |
| 11 | `menu_option_questions` | `UNIQUE (option_id, sort_order)` nomeada; FK cascade |
| 12 | `queue_operators` | `address` UNIQUE; FKs |
| 13 | `menu_option_targets` | FKs |
| 14 | `bot_settings` | `key VARCHAR(64) PRIMARY KEY` (note: `key` é **palavra reservada** → usar crase/backtick no DDL) |
| 15 | `chats` | `chat_id BIGINT PRIMARY KEY`; `updated_at` default UTC; índice parcial `chats_classified_idx` |
| 16 | `bot_events` | `id BIGINT AUTO_INCREMENT`; `at` default UTC; índices `(at DESC)` e `(reason, queue_address, at)` |
| 17 | `bot_jobs` | `CHECK (status IN (...))` |
| 18 | `team_routing` | `cap_cases` NULL-able; `ON CONFLICT (team_id)` |
| 19 | `team_member_routing` | `(team_id, address)` único |
| 20 | `pending_routes` | `case_key`, claim (`claimed_by/claimed_at/claim_expires_at`), índice parcial `pending_routes_active` |
| 21 | `delivery_attempts` | `id` + índices `(at DESC)` e `(chat_id, id)` |
| 22 | `analyst_load` | `id`/PK por analista |
| 23 | `analyst_load_history` | índice `(at DESC)` |
| 24 | `route_assignments` | `assigned_at` default UTC; `closed_at`, `close_reason`; 2 índices |
| 25 | `permission_groups` | seed fixo `id=1` (Super Admin) — ver 5.5 (contador AUTO_INCREMENT) |
| 26 | `group_permissions` | PK composta |
| 27 | `user_teams` | PK composta |
| 28 | `user_queues` | PK composta |
| 29 | `group_teams` | PK composta |
| 30 | `group_queues` | PK composta |
| 31 | `group_menus` | PK composta |

**Índices:** 9 não-parciais (`chats_status_idx`, `bot_events_at_idx`, `bot_events_metrics_idx`,
`pending_routes_due`, `delivery_attempts_at_idx`, `delivery_attempts_chat_idx`,
`analyst_load_history_at`, `route_assignments_open`, `route_assignments_case`) + **5 parciais**
(5.3) + os `UNIQUE` inline.

**Seeds:** 9 blocos `INSERT OR IGNORE` (schema.sql linhas 194, 200, 317, 408, 428, 442, 510,
513, 514) → no MySQL, `INSERT IGNORE` (a única diferença é a sintaxe).

**Decisão de tipos (fundamental para não quebrar nada):**

1. **Datas continuam `VARCHAR(19)`** no formato `'YYYY-MM-DD HH:MM:SS'` (UTC). Motivo: o código
   compara timestamp como **texto** (`datetime(at) >= datetime('now', ?)`, `ORDER BY at DESC`) e
   o painel faz `date(datetime(e.at, '-03:00'))` (offset como parâmetro!). Com `DATETIME` nativo,
   todas essas comparações/expressões mudariam de semântica e a migração de dados exigiria
   conversão de tipos. Ordem lexicográfica = ordem cronológica no formato fixo, e `DATE(x)`
   do MySQL aceita a string. Alternativa (DATETIME nativo) **rejeitada** por risco/ganho.
2. Chaves de texto indexadas → `VARCHAR(n)` com `n` folgado (`address VARCHAR(80)`,
   `login VARCHAR(60)`, `slug VARCHAR(60)`, `status/kind/op/result VARCHAR(20)`, `case_key
   VARCHAR(120)`, `key VARCHAR(64)`); textos livres (`body`, `detail`, `value`, `answers`,
   `script_answers`, `error`) → `TEXT`/`MEDIUMTEXT`.
3. Booleanos `INTEGER`/`TINYINT` → `TINYINT(1) NOT NULL DEFAULT 0`.
4. **FK:** MySQL **ignora** `REFERENCES x(id)` inline em `CREATE TABLE` (silenciosamente!) →
   toda FK vira `CONSTRAINT fk_<tabela>_<col> FOREIGN KEY (col) REFERENCES t(id) ON DELETE …`
   + índice correspondente (o InnoDB cria sozinho). Tipos das duas pontas **idênticos**: a coluna
   `INTEGER` da ponta filha vira `BIGINT` (o `INTEGER PRIMARY KEY AUTOINCREMENT` já virou
   `BIGINT`), e a de texto fica com o `VARCHAR(191)` de sempre. **Medido (8.0.44):** `schema.sql`
   → 54 comandos, **30 FKs** gravadas em `information_schema.table_constraints` (antes desta
   regra: 31 tabelas, **0 FKs**). A ordem não importa desde que `FOREIGN_KEY_CHECKS = 0` valha
   durante o script inteiro — e é aí que o `PRAGMA foreign_keys = ON` da linha 4 do `schema.sql`
   morde (armadilha 26).
5. `UNIQUE (a, b)` inline → `UNIQUE KEY uq_<tabela>_<a>_<b> (a, b)`, explicitamente nomeado.
6. `DEFAULT (datetime('now'))` → `DEFAULT (UTC_TIMESTAMP())` (MySQL 8.0.13+ aceita default com
   expressão). Validar na Fase 2 com INSERT que **omite** a coluna.
7. `CHECK` mantidos (MySQL 8.0 valida), com os **mesmos nomes lógicos** para facilitar diff.
8. **Colação:** `COLLATE utf8mb4_bin` nas colunas de igualdade/chave e `utf8mb4_0900_ai_ci`
   (default) nas demais; os `LIKE '%termo%'` de busca por nome/endereço levam
   `COLLATE utf8mb4_0900_ai_ci` explícito. Motivo em 5.5(c): fidelidade ao `=` e aos `UNIQUE`
   *case-sensitive* do SQLite, sem perder a busca que ignora maiúsculas.

**Tamanho de índice:** `utf8mb4` = 4 bytes/char; com `InnoDB` (limite 3072 bytes) as chaves
acima ficam muito abaixo do limite (nenhum prefixo de índice é necessário).

### 4.7 `php/bootstrap.php` (choke point do painel)

Hoje: `bot_sqlite_path()` (l.32–55), `bot_pdo()` (l.57–77, cria `PDO('sqlite:…')` + 3 PRAGMAs
l.72–74) e `bot_ensure_schema()` (l.79, lê `schema.sql`, remove comentários, `preg_split('/;\s*/')`).

Depois:

- `bot_db_backend()` (novo): lê `DB_BACKEND` (default `sqlite`) do `.env`/ambiente.
- `bot_pdo()`: se `mysql` → `new BotPDO(...)` com `PDO::ATTR_ERRMODE => ERRMODE_EXCEPTION`,
  `ATTR_DEFAULT_FETCH_MODE => FETCH_ASSOC`, `ATTR_EMULATE_PREPARES => false`,
  `MYSQL_ATTR_FOUND_ROWS => true` (mantém `rowCount()` com a semântica de linhas casadas do
  SQLite),
  `charset=utf8mb4`. Se `sqlite` → comportamento atual, com as PRAGMAs.
- **`class BotPDO extends PDO`** com `PDO::ATTR_STATEMENT_CLASS = [BotStatement::class, []]` e
  `prepare()/query()/exec()` traduzindo o SQL antes de chamar o `parent::`. Vantagem: as
  assinaturas `function … (PDO $pdo)` espalhadas no painel **continuam válidas** (é subclasse),
  sem tocar em nenhuma página.
- **`class BotStatement extends PDOStatement`** com `execute()` traduzindo SQL + parâmetros.
  Alternativa (não escolhida): reescrever os ~117 sites SQL do `index.php` à mão.
- `bot_ensure_schema()`: escolhe `schema.mysql.sql` vs `schema.sql`; o splitter por `;` continua,
  mas precisa **respeitar aspas** (um `;` dentro de literal `''` quebraria o statement) e
  **ignorar os `PRAGMA`** quando backend=mysql.
- `bot_table_exists()` (l.411, `sqlite_master`) e `bot_table_columns()` (l.419,
  `PRAGMA table_info`) → `information_schema` quando mysql.
- Migrações do painel a ajustar: `bot_migrate_ask` (l.119, ADD COLUMN com `REFERENCES` inline),
  `bot_migrate_load` (l.217, DDL `AUTOINCREMENT` + `DEFAULT (datetime('now'))` l.261–262),
  `bot_migrate_form_aware` (l.279), `bot_migrate_queue` (l.324, `datetime('now')` l.340),
  `bot_migrate_delivery` (l.347, DDL l.381–382 e índice parcial `pending_routes_active` l.404),
  `bot_migrate_submenu` (l.461, `sqlite_master` l.527, `PRAGMA foreign_keys=OFF/ON` l.535/575,
  DDL l.539), `bot_migrate_rbac` (l.578, `INSERT OR IGNORE` l.641/649) e as concatenações
  `||` das linhas 304/309/312/313.
- **`ALTER TABLE … ADD COLUMN … REFERENCES`**: MySQL **ignora** a cláusula inline → separar em
  `ADD COLUMN` + `ADD CONSTRAINT … FOREIGN KEY … ON DELETE SET NULL` (só se ainda não existir,
  checando `information_schema`).
- MySQL **não tem** `ADD COLUMN IF NOT EXISTS` → a checagem existente por coluna
  (`bot_table_columns`) é o guarda; nunca depender de `IF NOT EXISTS`.

### 4.8 Demais arquivos PHP

| Arquivo / linha | Hoje (SQLite) | Depois |
|---|---|---|
| `index.php:866` | `UPDATE bot_users SET last_login_at = datetime('now')` | traduzido (valor UTC calculado na aplicacao) |
| `index.php:975, 998, 1262, 1308, 1437, 1499` | `ON CONFLICT … DO UPDATE … EXCLUDED.value` | `ON DUPLICATE KEY UPDATE … = new.x` (alias `AS new`, 8.0.19+; `VALUES(x)` é deprecado) |
| `index.php:989` | `if ($st->rowCount() === 0)` (upsert de template) | igual, garantido por `MYSQL_ATTR_FOUND_ROWS=true` (nunca devolve 0 num upsert casado) |
| `index.php:994, 1348, 1374, 1391, 1408, 1446, 2426` | `datetime('now'…)` (inclusive `'-1 day'`) | valor UTC calculado na aplicacao (regra 5.4) |
| `index.php:1013, 1165` | `lastInsertId()` | idem (PDO MySQL suporta) |
| `index.php:1471, 1478` | `rowCount()` da replicação de escopo | idem (`FOUND_ROWS=true` = linhas casadas, que é o que a mensagem "X linha(s) atualizada(s)" já conta hoje) |
| `index.php:2785, 2790, 2925, 2930, 3399, 3476, 3802, 3841` | `date(datetime(col, ?))` (offset `-03:00` vindo por parâmetro) | `DATE(CONVERT_TZ(col, '+00:00', ?))` |
| `index.php:292` | trata erro `23000`/`23505` / texto `UNIQUE` | manter `23000` + acrescentar `1062` e texto `Duplicate entry` |
| `auth.php:384` | `INSERT OR IGNORE` | `INSERT IGNORE` |
| `pages/groups.php:87, 135, 144, 153` | `INSERT OR IGNORE` | `INSERT IGNORE` |
| `bin/create-user.php` | `ON CONFLICT (login) DO UPDATE … excluded.…` e `bot_users.group_id` | `ON DUPLICATE KEY UPDATE … = new.x` (alias `AS new`); manter a forma qualificada |
| `api.php` | só `SELECT` simples | **nada a mudar** |
| `pages/*.php` (demais) | SQL comum + `?` | nada (o tradutor cuida) |
| `php/.htaccess`, `bin/deploy.sh` | — | nada |

### 4.9 `bin/migrar_sqlite_para_mysql.py` (NOVO)

Copia `data/helptoa.sqlite` → MySQL, sem `INSERT` artesanal:

```
python bin/migrar_sqlite_para_mysql.py --sqlite data/helptoa.sqlite            # dry-run
python bin/migrar_sqlite_para_mysql.py --schema                               # so aplica o DDL
python bin/migrar_sqlite_para_mysql.py --sqlite data/helptoa.sqlite --apply
python bin/migrar_sqlite_para_mysql.py --check      # compara contagens origem x destino
python bin/migrar_sqlite_para_mysql.py --only chats,pending_routes,route_assignments
```

- Escreve em **lotes** (`executemany`, 500 linhas) com `commit` por tabela.
- `SET FOREIGN_KEY_CHECKS=0` durante a carga (a ordem das FKs deixa de importar) e `=1` no fim,
  seguido de checagem de órfãos.
- **Não** converte datas: as colunas são texto no mesmo formato (decisão 4.6.1) → cópia literal
  (`str()`), garantindo **zero** deslocamento de fuso.
- `--check` compara `COUNT(*)` tabela a tabela **e** o `MAX(id)` das tabelas com `id`.
- Idempotente: `--apply` faz `TRUNCATE` antes **só** com `--truncate`; senão usa
  `INSERT IGNORE`/`REPLACE` nas tabelas de chave natural e reporta conflitos.
- Ao final, reajusta `AUTO_INCREMENT` de cada tabela para `MAX(id)+1` (necessário porque a seed
  de `permission_groups` grava `id=1` explícito).
- **Drift de coluna legada (corrigido em 06/10/2026).** A lista de colunas da carga sai do
  **destino** (`information_schema.columns`), não da origem: coluna que só existe na origem entra
  como `AVISO` no relatório e fica fora do `INSERT`, em vez de derrubar a tabela inteira com
  **1054**. Eram duas: `bot_jobs.payload` (código morto — segue fora, com aviso) e
  `analyst_load.casos_toa_keys` (coluna **viva**, usada por `db.py::_migrate_load`): essa entrou no
  `schema.sql`, então agora é criada junto com a tabela e a cópia acontece normal.

### 4.10 Testes locais (Python + PHP) — o que muda e o que **não** muda

**Não muda:** nenhum dos 24 arquivos Python (501 casos) / 12 suites PHP passa a exigir MySQL. O default
`DB_BACKEND=sqlite` é o que garante isso.

**Muda (obrigatório):**

1. `tests/test_catalog_sync.py:124-140` chama o **`db.connect()` real** com `SQLITE_PATH`
   patchado → é o único teste que "decidiria" pelo backend real. **Fix:** no `patch.dict`, fixar
   `{"SQLITE_PATH": dbp, "DB_BACKEND": "sqlite"}` (nos dois `connect()`), tornando explícito que
   é teste de arquivo SQLite. Sem isso, um `.env` com MySQL faria o teste escrever no servidor
   corporativo.
2. `tests/test_submenu.py` (l.48–49, 103), `test_form_open.py` (l.83) e `test_form_aware.py`
   (l.157) criam `sqlite3.connect(":memory:")` + `db._dict_factory` + `db.ensure_schema` →
   passam a usar um helper comum `tests/support.py::mem_conn()`, reduzindo duplicação e
   permitindo, opcionalmente, rodar o mesmo teste contra MySQL com `MYSQL_TEST=1`.
3. Suítes PHP (`tests/test_panel_render.php:139-140`, `tests/_panel_render.php:19-25`): hoje
   exigem `SQLITE_PATH`; passam a **também** fixar `DB_BACKEND=sqlite` via `putenv`, e o harness
   `_panel_render.php` aborta se o backend não for sqlite (mesma proteção já documentada na
   l.18 contra o banco real).
4. **Novos testes:**
   - `tests/test_sqlcompat.py` — 100% offline, sem MySQL: tradução de cada construção
     (`datetime('now')`, `datetime(col)`, `datetime(col, ?)`, `date(...)`, `||`→`CONCAT`,
     `INSERT OR IGNORE`, `ON CONFLICT`, `PRAGMA`→`information_schema`, `?`→`%s`, `%`→`%%`,
     `BEGIN IMMEDIATE`, `excluded.x`→`new.x`), `convert_params`, `is_disconnect` e o
     contrato do cursor/`rowcount` (com duplo/fake, sem servidor).
     Inclui o **lint cruzado** da 5.7 (`TextEmChaveTest`): lê `schema.sql` **e** `db.py` para um
     mapa de tipos só e acusa `TEXT`/`BLOB` em chave composta, `CREATE INDEX` ou `REFERENCES`
     inline — o caso que o tradutor não cobre e que só apareceria no boot.
     Inclui ainda o **lint do 1093** (`SubqueryNaPropriaTabelaTest`): varre o DML do `db.py` e
     acusa `UPDATE`/`DELETE` com subquery na própria tabela alvo — armadilha que **nem o SQLite nem
     o teste com driver falso** denunciam (5.6).
   - `tests/test_mysql_schema.py` — gated por `MYSQL_TEST=1` + `MYSQL_TEST_DATABASE`: aplica
     `schema.mysql.sql` do zero, roda `ensure_schema` 2× (idempotência), confere
     colunas/FK/índices via `information_schema` e testa `DEFAULT (UTC_TIMESTAMP())`.
   - `tests/test_mysql_upserts.py` — gated: cada um dos 16 upserts faz INSERT e depois UPDATE e
     verifica o contrato de `rowcount`.
   - `tests/test_mysql_painel.php` — gated: aplica o DDL MySQL e faz smoke de cada aba.
5. **Documentação de testes** (`README.md`): a seção de testes ganha as duas variáveis novas e a
   regra "sem `MYSQL_TEST` nada toca o servidor".
6. **Testes que não rodavam (defeito achado e corrigido):** `tests/test_delivery_dupla.py`,
   `tests/test_delivery_guard.py` e `tests/test_delivery_recovery.py` não têm o bloco
   `if __name__ == "__main__": unittest.main()` que os outros 20 arquivos têm — o runner
   documentado (`Get-ChildItem tests\test_*.py | % { python $_.FullName }`) os executava **sem
   rodar caso nenhum** (saída vazia, `exit 0`), escondendo **17 casos**. Um deles é
   `test_delivery_guard.py::test_migracao_saneia_duplicatas_antigas`, que é exatamente o teste do
   saneamento 1093 do `_migrate_delivery` (5.6) — ou seja, o GATE "tudo verde" não cobria o
   código que a mudança de DML tocou. Com o bloco, a suíte vai de **463** para **487 casos** nos 24
   arquivos (0 falhas, medido agora: 487 = 398 dos 23 arquivos que já existiam + 89 do novo
   `test_sqlcompat.py`); é esse número que os GATEs das fases usam.

### 4.11 `.env`, `.env.example`, `.gitignore`, docs, deploy

- `.env.example`: bloco SQLite (default, comentado como "teste/local") + bloco MySQL completo
  (`DB_BACKEND=mysql`, `IP_MYSQL`, `PORTA_MYSQL`, `USUARIO_MYSQL`, `SENHA_MYSQL`,
  `DATABASE_MYSQL`) com o aviso de que `DB_BACKEND` é o interruptor.
- `.gitignore`: conferir que `.env` e `data/*.sqlite*` seguem ignorados (o dump de origem da
  migração tem dados reais) e que o banco de teste local nunca é versionado.
- `docs/desenho-bot.md` e `README.md`: seção "Banco de dados" passa a descrever os dois backends,
  o arquivo de schema por backend e o `--check` do migrador.
- `bin/deploy.sh`: nenhuma mudança de SQL; só garantir que o `.env` do servidor tem
  `DB_BACKEND=mysql` e que o log de boot mostra o backend correto.

---

## 5. Mapa de tradução SQLite → MySQL (contrato do tradutor)

### 5.1 Regras e ordem de aplicação

O tradutor roda **na hora do `execute`/`prepare`**, na ordem abaixo (algumas regras dependem das
outras). Ele recebe o SQL e os parâmetros (opcional) e devolve **`(sql, params)`** — porque as
regras 3–6 podem *consumir* um parâmetro (o modificador de data) e inserir outro (o valor UTC).

| # | Detecta | Reescreve para | Cuidado |
|---|---|---|---|
| 1 | `?` (placeholder) | `%s` | nunca tocar `?` dentro de literal `'…'` |
| 2 | `%` literal (quando há params) | `%%` | senão o driver lê como placeholder |
| 3 | `datetime('now')` | `%s` + valor UTC calculado na aplicação | **UTC** e **nunca** `NOW()`; mesmo formato textual do SQLite (5.4) |
| 4 | `datetime('now', '<modificador literal>')` | `%s` + valor calculado | cobre `'-300 seconds'`, `'+60 seconds'`, `'-1 day'` |
| 5 | `datetime('now', '-' \|\| ? \|\| ' seconds')` | `%s` + valor calculado, com o `?` **consumido** | o parâmetro sai da lista devolvida (5.4) |
| 6 | `datetime('now', ?)` (modificador vindo por parâmetro) | idem | `db.py:1335` e `test_form_aware.py:39` |
| 7 | `datetime(<col>)` (1 arg, coluna) | `<col>` (identidade) | a coluna já está no formato canônico `YYYY-MM-DD HH:MM:SS` (decisão 4.6.1) |
| 8 | `datetime(<col>, ?)` | `CONVERT_TZ(<col>, '+00:00', ?)` | offset **numérico** (`-03:00`) → não exige `mysql.time_zone*`. **Painel (Fase 3):** o `?` do painel é o **modificador** `'-3 hours'`, não um offset → `DATE_ADD(<col>, INTERVAL ? SECOND)`, com o parâmetro virando segundos (-10800) no `execute` |
| 9 | `date(<expr>)` | `DATE(<expr>)` | idempotente no formato texto |
| 10 | `\| \|` entre literais/colunas | `CONCAT(...)` | só o caso `'a:' \|\| case_key` precisa; os demais são o item 3–5 |
| 11 | `INSERT OR IGNORE INTO t` | `INSERT IGNORE INTO t` | |
| 12 | `ON CONFLICT (<cols>) DO UPDATE SET` | `AS new ON DUPLICATE KEY UPDATE` | e `excluded.x`/`EXCLUDED.x` → `new.x` (5.2) |
| 13 | `ON CONFLICT (<cols>) DO NOTHING` | `ON DUPLICATE KEY UPDATE <pk> = <pk>` (no-op) | não existe no MySQL; alternativa: `INSERT IGNORE` |
| 14 | `sqlite_master` (listar tabelas) | `information_schema.tables` | |
| 15 | `PRAGMA table_info(x)` | `information_schema.columns` (mesmas colunas de saída) | |
| 16 | `PRAGMA foreign_keys = OFF/ON` | `SET FOREIGN_KEY_CHECKS=0/1` | |
| 17 | `PRAGMA journal_mode/busy_timeout` | *(remove)* | não existe no MySQL |
| 18 | `BEGIN IMMEDIATE` | `START TRANSACTION` | |
| 19 | `AUTOINCREMENT` (DDL) | `AUTO_INCREMENT` | + `INTEGER PRIMARY KEY` → `BIGINT ... AUTO_INCREMENT` |
| 20 | `DEFAULT (datetime('now'))` (DDL) | `DEFAULT (UTC_TIMESTAMP())` | MySQL 8.0.13+ |
| 21 | `(julianday(a) - julianday(b)) * 86400` | `TIMESTAMPDIFF(SECOND, b, a)` | atenção: a ordem dos argumentos **inverte**; onde um dos lados é `'now'` ele vira `%s` (5.4). Casos: `db.py:508`, `index.php:3863` |
| 22 | `` `key` `` (identificador) | mantém | **crases funcionam em SQLite e MySQL** → usar crase onde o nome é palavra reservada (`key`) e o mesmo SQL vale para os dois. O shim já aplica isso sozinho (regra 25) |
| 23 | `TEXT`/`BLOB` **inline** seguido de `PRIMARY KEY`/`UNIQUE` | `VARCHAR(191) …` | 1170. Só o caso *inline* da `CREATE TABLE`: chave **composta** (`PRIMARY KEY (a, b)`), `CREATE INDEX` e `REFERENCES` inline **não** são convertidos — a coluna tem de nascer `VARCHAR` (trava o lint da 5.7) |
| 24 | `DEFAULT '<literal>'` em coluna `TEXT` | `DEFAULT ('<literal>')` | 1101: em `TEXT` só é aceito default **de expressão**; a regra cobre `''`, `'{}'`, `'toa'`… (5.5.b) |
| 25 | identificador `key` (coluna, tabela, alias) | `` `key` `` | 1064; **não** toca `PRIMARY KEY`, `FOREIGN KEY`, `UNIQUE KEY`, `ON DUPLICATE KEY UPDATE`, `PARTITION` nem literais/`'… key'` dentro de comentário |
| 26 | `x IS ?` (valor comparado vindo por parâmetro) | `x <=> %s` | 1064. O `IS` do SQLite é igualdade *null-safe*; o do MySQL só aceita `NULL`/`TRUE`/`FALSE`, e como o PyMySQL inlineia o parâmetro a query vira `IS 1`. `<=>` é a igualdade null-safe do MySQL (`IS NOT ?` → `NOT (x <=> %s)`), então a semântica é preservada; `IS NULL`/`IS NOT NULL` seguem intactos e operando à esquerda composto falha alto. Site: `db.py:1263` (`queue_position`) |

**Ordem importa:** fazer 1/2 por último, depois que 3–22 já geraram SQL novo (senão `%` e `?`
novos seriam reprocessados indevidamente). Regra prática: **primeiro as regras de "vocabulário"
(3–22), depois as de "pontuação" (1–2)**. Exceção: a **26** roda **antes** da 1 — ela *consome* o
`?` do usuário e devolve `%s` pronto; se rodasse depois, a 1 já teria convertido o `?` e o padrão
não casaria mais.

### 5.2 Upserts (`ON CONFLICT` → `ON DUPLICATE KEY UPDATE`) — 16 sites

| Origem | Tabela | Alvo do conflito | UNIQUE que cobre |
|---|---|---|---|
| `db.py:546` | `analyst_load` | `(address)` | PK `address` ✔ |
| `db.py:682` | `queues` | `(address)` | PK `address` ✔ |
| `db.py:703` | `bot_settings` | `(key)` | PK `key` ✔ |
| `db.py:837` | `team_routing` | `(team_id)` | PK `team_id` ✔ |
| `db.py:848` | `queue_operators` | `(queue_address, address)` | PK composta ✔ |
| `db.py:881` | `queues` (via `executemany`) | `(address)` | PK `address` ✔ |
| `db.py:889` | `queue_operators` (via `executemany`) | `(queue_address, address)` | PK composta ✔ |
| `db.py:952` | `chats` | `(chat_id)` | PK `chat_id` ✔ |
| `db.py:1529` | `route_assignments` | `(chat_id)` | UNIQUE `chat_id` ✔ (schema.sql:387) |
| `index.php:975` | `queue_operators` | `(queue_address, address)` | PK composta ✔ |
| `index.php:998` | `bot_settings` | `(key)` | PK `key` ✔ |
| `index.php:1262` | `team_members` | `(team_id, address)` | PK composta ✔ |
| `index.php:1308` | `bot_settings` | `(key)` | PK `key` ✔ |
| `index.php:1437` | `team_routing` | `(team_id)` | PK `team_id` ✔ |
| `index.php:1499` | `team_member_routing` | `(team_id, address)` | PK composta ✔ |
| `create-user.php` | `bot_users` | `(login)` | UNIQUE `login` ✔ |

**Diferença semântica que precisa de auditoria (Fase 2/3):** o `ON CONFLICT (col…)` do SQLite
dispara **somente** naquele alvo; o `ON DUPLICATE KEY UPDATE` do MySQL dispara em **qualquer**
chave única violada. Tabelas com **mais de um** UNIQUE (`menu_queues`, `team_queues` têm
PK + `UNIQUE (queue_address)`) exigem conferência: se um dia virarem alvo de upsert, o
comportamento muda. O gate da Fase 2 roda um script que lista, por tabela, todas as chaves
únicas (`information_schema.statistics`) e cruza com a lista acima.

**Sintaxe escolhida:** `INSERT … VALUES (…) AS new ON DUPLICATE KEY UPDATE col = new.col`
(8.0.19+). `VALUES(col)` também funciona, mas está **deprecado** em 8.0 → usar `new.`.

### 5.3 Índices parciais (5) → índice funcional com `CASE`

MySQL não tem índice parcial. O equivalente exato é um **índice funcional** (8.0.13+) cuja
expressão devolve `NULL` quando a linha **não** participa — e `UNIQUE` no MySQL **permite
vários `NULL`**, reproduzindo o "fora do índice" do SQLite. Índice funcional cria uma coluna
virtual **oculta**, o que preserva `SELECT *` (importante: `db.py` usa `SELECT *` em ~25 sites).

```sql
-- 1) menu_options_per_queue: WHERE queue_address IS NOT NULL
UNIQUE KEY menu_options_per_queue (queue_address, sort_order)
--   (UNIQUE já ignora NULL → idêntico ao parcial, sem função)

-- 2) menu_options_per_menu_root: WHERE menu_id IS NOT NULL AND parent_id IS NULL
UNIQUE KEY menu_options_per_menu_root
  (menu_id, sort_order,
   ((CASE WHEN menu_id IS NOT NULL AND parent_id IS NULL THEN 1 END)))

-- 3) menu_options_per_menu_child: WHERE menu_id IS NOT NULL AND parent_id IS NOT NULL
UNIQUE KEY menu_options_per_menu_child
  (menu_id, parent_id, sort_order,
   ((CASE WHEN menu_id IS NOT NULL AND parent_id IS NOT NULL THEN 1 END)))

-- 4) chats_classified_idx (NÃO único): WHERE classification IS NOT NULL
KEY chats_classified_idx ((CASE WHEN classification IS NOT NULL THEN updated_at END))

-- 5) pending_routes_active: UNIQUE (kind, chat_id)
--    WHERE status NOT IN ('done', 'failed', 'cancelled')
UNIQUE KEY pending_routes_active
  (kind, chat_id, ((CASE WHEN status IN ('done','failed','cancelled') THEN NULL ELSE 1 END)))
```

Regras/limites que a Fase 2 precisa validar:

- Observar as **parênteses duplas** `((expr))` — é o que distingue "expressão" de "coluna".
- A expressão tem de ser **determinística** (nada de `NOW()`/`CURRENT_TIMESTAMP` dentro dela);
  as cinco acima só referenciam colunas ✔.
- Plano B, se algum índice funcional for recusado: coluna gerada explícita
  (`x_scope TINYINT GENERATED ALWAYS AS (…) VIRTUAL`) + índice — **porém** coluna gerada
  aparece em `SELECT *` e pode quebrar um `INSERT` montado a partir de um `fetch` (auditar
  antes). Por isso ficamos com o índice funcional.
- `information_schema.statistics` mostra a parte de expressão com `COLUMN_NAME = NULL` e a
  expressão em `EXPRESSION` → o script de auditoria da Fase 2 tem de ler as duas colunas.

Como o shim implementa (e o que ele **não** faz):

- A expressão é gerada a partir da `WHERE` **original**, textualmente: `CASE WHEN <condição>
  THEN 1 END`. Então a condição precisa ser determinística e só citar colunas da tabela —
  `NOW()` passaria direto e o MySQL recusaria.
- `WHERE col IS NOT NULL` com a coluna na **própria chave** perde a `WHERE` (vira índice comum):
  `UNIQUE` já ignora `NULL` no MySQL. É o caso do `menu_options_per_queue`.
- 1170 também vale para as colunas do índice funcional: `TEXT` **não** pode aparecer na chave.
  Por isso `pending_routes.kind` é `VARCHAR(191)` **no `schema.sql`** (não só no espelho MySQL) —
  o índice parcial `(kind, chat_id, …)` precisa de chave de tamanho fixo. É exatamente o que o
  lint da 5.7 trava (foi um `TEXT` aí que escapou na primeira tentativa).
- `CREATE INDEX IF NOT EXISTS` / `DROP INDEX IF EXISTS` não existem no MySQL (1061): o shim
  consulta `information_schema.statistics`, só cria o que falta e ignora o `DROP` do que não
  existe — é o que mantém `ensure_schema` idempotente (porta `sqlcompat`, `_ajusta_indice`).

### 5.4 Datas, horas e a regra "calcular do lado da aplicação"

**Nunca** gerar `NOW()`, `CURRENT_TIMESTAMP`, `CURDATE()` ou `LOCALTIME()`: com
`time_zone=-03` eles mentem em 3 h. E o formato das colunas é o canônico UTC (4.6.1).

| Construção SQLite | Tradução MySQL | Comentário |
|---|---|---|
| `datetime('now')` | `%s` + valor UTC calculado na aplicação | `datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')` no Python; `gmdate('Y-m-d H:i:s')` no PHP |
| `datetime('now', '-N seconds')` / `'+N seconds'` (literal) | `%s` + valor calculado | idem; o modificador é interpretado na aplicação |
| `datetime('now', '-' \|\| ? \|\| ' seconds')` | `%s` + valor calculado **com o parâmetro** consumido | o `?` sai da lista de parâmetros |
| `datetime('now', ?)` (modificador por parâmetro) | idem | é o caso do `db.py:1335` (`attempts_summary(since="-1 day")`) e do `test_form_aware.py:39` |
| `datetime(<coluna>)` | `<coluna>` | identidade: a coluna já está no formato canônico |
| `datetime(<coluna>, ?)` | `CONVERT_TZ(<coluna>, '+00:00', ?)` | offset numérico (`-03:00`) → **não** exige as tabelas `mysql.time_zone*` |
| `date(<expr>)` | `DATE(<expr>)` | aceita a string canônica |
| `(julianday(a) - julianday(b)) * 86400` | `TIMESTAMPDIFF(SECOND, b, a)` | `db.py:508` (idade) e `index.php:3863` / `test_queue_schema.php:128` (segundos entre eventos); o lado `'now'` → `%s` (regra 3) |
| `DEFAULT (datetime('now'))` (DDL) | `DEFAULT (UTC_TIMESTAMP())` | 8.0.13+; o default só vale como *fallback* (a aplicação manda o valor) |

**Por que "calcular na aplicação" em vez de `TIMESTAMPADD`/`INTERVAL` no SQL:** o parâmetro do
modificador no código atual é uma **string de modificador do SQLite** (`'-1 day'`), não um
número — traduzir para `INTERVAL ? DAY` exigiria *parsear* a string no tradutor. Calcular na
aplicação deixa o resultado textual **byte a byte igual** ao que o SQLite gravaria (mesma
função, mesmo formato), mantém tudo testável offline e cobre os 34 sites + o `julianday` com
uma única regra. `UTC_TIMESTAMP()` continua sendo usado **na DDL** (default) e onde o valor é
puramente SQL (visto em 5.2/5.3).

**Fuso do servidor:** além de nunca emitir `NOW()`, a conexão faz `SET time_zone = '+00:00'`
no handshake. Custo zero e blinda qualquer `CURRENT_TIMESTAMP` implícito de ferramenta
externa (ex.: alguém rodando SQL manual no MySQL Workbench).

**Retrocompatibilidade dos dados:** como as colunas guardam texto UTC no formato canônico, a
migração **não** converte nada (4.9) e nenhuma data "envelhece" ou desloca.

### 5.5 Diversos (palavras reservadas, tipos, colacao, sessao)

**a) `key` e palavra reservada no MySQL** (nao e no SQLite). A coluna `bot_settings.key` aparece
em `SELECT`/`INSERT`/`UPDATE` nos dois projetos (ocorrencias de `key`: `db.py` 17, `index.php` 15,
`bootstrap.php` 12, `groups.php` 5 - nem todas sao SQL). Solucao: **crase** em toda ocorrencia
dentro de SQL (`` `key` ``). Como o **SQLite tambem aceita crase**, a mesma query vale para os
dois dialetos - nada de condicional.

**b) `TEXT`/`BLOB` nao podem ser PK/UNIQUE sem prefixo no MySQL.** Hoje temos
`queues.address TEXT PRIMARY KEY`, `analyst_load.address TEXT PRIMARY KEY`,
`bot_settings.key TEXT PRIMARY KEY`, `templates.slug TEXT UNIQUE`, `bot_users.login TEXT UNIQUE`
etc. -> todas viram `VARCHAR(n)` (4.6.2). Uma traducao "1:1 de tipos" falharia aqui; e um dos
erros que a Fase 2 pega de imediato.

**c) Colacao (mudanca sutil de comportamento).** Decisao: **`COLLATE utf8mb4_bin`** nas colunas
de igualdade/chave (`login`, `slug`, `address`, `queue_address`, `case_key`, `perm_key`, `status`,
`kind`, `source`, `result`, `op`) e **`utf8mb4_0900_ai_ci` (default)** nos demais. Motivo: o `=`
do SQLite e *case-sensitive* e os `UNIQUE` tambem; `ai_ci` nos campos-chave mudaria isso em
silencio (ex.: `Ana` e `ana` virariam o mesmo `login`). Consequencia a tratar: com `_bin`, o
`LIKE` fica *case-sensitive*, e o painel usa `LIKE '%termo%'` em busca por nome/endereco - os
sites `index.php:3445, 3450, 3463, 3574` (e qualquer outro com `LIKE` sobre coluna `_bin`)
recebem `COLLATE utf8mb4_0900_ai_ci` explicito (ou `LOWER()` nos dois lados) para manter a
busca que nao liga para maiusculas. `case_key LIKE 'u:%'` (`db.py:391`,
`bootstrap.php:309/313`) fica `_bin` de proposito: literal e valores sao minusculos.

Implementado em `sqlcompat._colacao_de_igualdade`, por dois caminhos: `_colacao_no_create` (ve a
chave da tabela - `PRIMARY KEY`/`UNIQUE`, inclusive composta -, a ponta **filha** de FK e a lista
acima) e `_colacao_no_alter` (o `ALTER TABLE ... ADD COLUMN` das migracoes de banco antigo do
`db.py`, onde so o nome da coluna decide). Medido na base real (`botsqlite_test`): **82** colunas
na consulta 2.4 (chave ou `_bin`), **0** par de FK com colacao divergente (3780) e os **101**
pares de linhas de `queue_operators` que so diferem por caixa convivendo sob `_bin` - sob `ai_ci`
cada par colapsa em um so e a PK `(queue_address, address)` recusa a segunda linha com **1062
Duplicate entry**, que era o erro que matava a carga.

**d) Sessao da conexao (o handshake completo):**

```sql
SET time_zone = '+00:00';                                  -- NOW() nunca mente
SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;    -- evita snapshot/gap lock do RR
SET SESSION innodb_lock_wait_timeout = 5;                  -- espelha o busy_timeout=5000 do SQLite
SET SESSION sql_mode = '';                                 -- igual ao servidor (nao ligar strict)
```

- **autocommit no Python = `False`** (paridade com o `sqlite3` atual, que abre transacao implicita
  em DML e depende dos `commit()` explicitos do codigo).
- `innodb_lock_wait_timeout=5` importa porque agora **dois processos** escrevem (bot + painel):
  sem isso o bot ficaria ate 50 s travado num lock em vez dos ~5 s de hoje.
- DDL no MySQL faz **commit implicito** (o SQLite transaciona DDL). Como o unico ponto que
  dependia disso (`_rebuild_menu_options_check`) vira no-op no MySQL, nao ha impacto - mas fica
  registrado para nao descobrir depois.

**e) Tipos devolvidos pelo driver.** No PHP, `PDO::ATTR_EMULATE_PREPARES => false` e
**obrigatorio**: com emulacao (default do PDO MySQL) **todo** valor volta como *string* e
comparacoes `=== 1`/`!== null` do painel mudariam de resultado. No Python, o PyMySQL substitui os
`%s` **no lado do cliente** (sem *prepared statement* nativo), entao `LIMIT %s` com inteiro
funciona igual ao SQLite - mas isso **obriga** a regra de escapar `%` literal (5.1, regra 2).

**f) `AUTO_INCREMENT` com `id` explicito.** A seed `INSERT IGNORE INTO permission_groups (id, ...)
VALUES (1, ...)` faz o contador ir para 2 (automatico no InnoDB). Depois da migracao em lote, o
`bin/migrar_sqlite_para_mysql.py` normaliza cada contador para `MAX(id)+1` (4.9).

**g) `lower_case_table_names=0`.** Os nomes ja sao minusculos (`chats`, `bot_settings`...):
manter exatamente assim e nunca citar como `Chats`/`Bot_Settings`.

**h) `preg_match` com `$offset` (so no port do painel).** Diferente de `substr`, o `^` continua
ancorado no **inicio do sujeito** quando se passa um offset: o passo que procura o `)` da diferenca de
`julianday` precisou de **`\G`** (`/\G\s*\)\s*\*\s*86400/i`). Sem isso o SQL valido do painel
(`(julianday(s.at) - julianday(MAX(e.at))) * 86400`) estourava como "julianday solto".

### 5.6 DML: erro 1093 (subquery que lê a própria tabela alvo)

O MySQL **não materializa** a subquery de `UPDATE`/`DELETE`: `UPDATE t … WHERE x IN (SELECT …
FROM t)` falha com *1093 — You can't specify target table 't' for update in FROM clause*. O
SQLite aceita — é a única armadilha desta lista que o SQLite **não** denuncia de forma nenhuma.
Padrão portável (a mesma query roda nos dois motores): embrulhar a subquery numa **derivada
com alias**, que força a materialização.

```sql
-- NÃO portável (1093 no MySQL):
DELETE FROM analyst_load_history
 WHERE id NOT IN (SELECT id FROM analyst_load_history
                   ORDER BY id DESC LIMIT ?);

-- Portável: a derivada `recentes` é materializada antes do DELETE
DELETE FROM analyst_load_history
 WHERE id NOT IN (SELECT id FROM (
         SELECT id FROM analyst_load_history
          ORDER BY id DESC LIMIT ?) AS recentes);
```

Os dois sites reais já estão reescritos assim, e a varredura (`'IN (SELECT'`, `'= (SELECT'` e
`EXISTS (SELECT` sobre a própria tabela) encontrou **exatamente esses dois**:

| Site | Tabela | Forma aplicada |
|---|---|---|
| `db.py::load_trim_history` | `analyst_load_history` | derivada `AS recentes` (acima) |
| `db.py::_migrate_delivery` | `pending_routes` | `UPDATE … SET status='cancelled'` + derivada `AS rotas_ativas` (`GROUP BY kind, chat_id` + `MIN(id)`) |

O `_migrate_delivery` é o caso completo: o `EXISTS (SELECT … FROM pending_routes)` original virou
uma derivada que escolhe, por `(kind, chat_id)`, a rota **mais antiga** (`MIN(id)`) — o mesmo
critério que o índice único parcial `pending_routes_active` passou a impor no MySQL. O
saneamento roda **antes** da criação do índice (senão o próprio `CREATE` falharia com a duplicata
que veio do SQLite, onde o índice nunca existiu).

**Regra para código novo:** nunca `UPDATE`/`DELETE` com subquery na própria tabela alvo. Se a
subquery precisa agregar a mesma tabela, envolva-a numa derivada com alias — o SQLite executa
igual e a intenção fica explícita.

**Guarda permanente** (a varredura da investigação, `tmp_bak/scan_1093.py`, virou teste):
`tests/test_sqlcompat.py::SubqueryNaPropriaTabelaTest` extrai os comandos `UPDATE`/`DELETE` do
`db.py` (strings literais do módulo, via `ast`) e falha se algum ler a própria tabela alvo fora de
uma derivada:

- `test_projeto_sem_subquery_na_propria_tabela` — o check do projeto; tem antídoto contra vacuidade
  (`assertGreater(len(_dml_do_py()), 20)` — hoje são 50 comandos), senão o teste passaria só por não
  estar olhando nada;
- `test_lint_acusa_as_tres_formas` — `IN (SELECT …)`, `EXISTS (SELECT …)` e escalar
  `= (SELECT …)` na mesma tabela **têm** de ser acusados;
- `test_lint_pega_a_forma_real_do_migrate_delivery` — exercita a **cadeia inteira** (texto do módulo
  → `ast` → tradução com os `?` inventados → lint) sobre a forma real do `_migrate_delivery`, que
  junta subquery na própria tabela **e** prazo por parâmetro; é o caminho que já quebrou uma vez;
- `test_lint_aceita_a_derivada` — as duas reescritas reais passam, e ler tabela **alheia** é livre.

O lint reusa o extrator `_sentencas` (5.7) e, para traduzir cada comando, precisa **inventar valor**
para os `?`. Esse valor sintético tem de servir às **três** formas de `datetime(…)` que o projeto
usa: um **número sem unidade** é o único aceito pelas duas que **interpretam** o parâmetro — a
concatenada faz `float(param)` e a direta passa por `modifier_seconds`, que trata número puro como
segundos; a terceira (`datetime(<coluna>, ?)`) só **repassa** o valor para o `CONVERT_TZ`, então
qualquer um serve. Daí `_params_do_lint` devolver `_VALOR_DE_MODIFICADOR = "60"`, e não `None` nem
`'-60 seconds'`. A guarda disso é `TempoTest.test_valor_do_lint_atravessa_as_tres_formas`.

**Escopo:** só o `db.py`. A varredura ampla — todos os módulos de `python/`, em
`tmp_bak/lint_1093_projeto.py` — dá **0 achados** e mostra que o DML vive só ali (50 comandos em
`db.py`, 0 nos outros 10 módulos); o `sqlcompat.py` fica fora porque carrega SQL **já traduzido**
(`%s`) nos próprios templates, e isso é ruído para o lint. Limitação conhecida, igual à do lint de
TEXT: SQL montado por f-string/campo não é string literal e escapa da varredura.

### 5.7 Lint cruzado (guarda permanente contra 1170 e as regras 23–25)

`tests/test_sqlcompat.py::TextEmChaveTest` varre `schema.sql` **e** `db.py` num **único mapa de
tipos** (coluna → tipo, por tabela, lendo as duas fontes) e falha se alguma coluna declarada
`TEXT` aparecer em: chave composta (`PRIMARY KEY (…)` / `UNIQUE (…)`), `CREATE INDEX`, ou
`REFERENCES` inline. É o lint que o 1170 do `pending_routes.kind` exige: a coluna nasce num
arquivo (`schema.sql`) e o índice que a usa está em **outro** (`db.py`) — um lint por arquivo
nunca veria os dois juntos.

O lint cobre **exatamente três padrões** — e `test_lint_acusa_os_tres_padroes` garante que ele
não passa por vacuidade (a amostra ruim tem de ser acusada): chave composta, `CREATE INDEX` e
`REFERENCES` inline. As regras 24/25 seguem guardadas pelos testes unitários do shim
(`DdlTest.test_default_de_texto_ganha_parenteses`,
`DdlTest.test_default_literal_nao_vazio_ganha_parenteses`,
`DdlTest.test_identificador_key_vira_coluna_escapada`,
`DmlTest.test_upsert_do_bot_settings_escapa_a_coluna_key`), não pelo lint de projeto.

---

## 6. Armadilhas confirmadas (o que daria errado sem tratamento)

| # | Armadilha | Onde | Tratamento | Gate que pega |
|---|---|---|---|---|
| 1 | `NOW()` com `time_zone=-03` gravaria 3 h adiantado | 34 no `db.py` (+29 nos testes), 12 no painel (`index.php` 8 + `bootstrap.php` 4), 9 nos testes PHP | valor UTC calculado na aplicacao + `UTC_TIMESTAMP()` na DDL + `SET time_zone='+00:00'` | Fase 1 (unitario) e Fase 6 (integracao) |
| 2 | `rowcount` de "nada mudou" volta `0` | `db.py:1124/1239/1379/1497`, `index.php:989` | `CLIENT.FOUND_ROWS` ligado / `MYSQL_ATTR_FOUND_ROWS => true` | Fase 2 (auditoria de `rowcount`) |
| 3 | Indices parciais nao existem no MySQL | 5 indices (4.6, 5.3) | indice funcional com `CASE` devolvendo `NULL` fora do indice | Fase 2 (aplicar DDL + tentar duplicar) |
| 4 | `ON DUPLICATE KEY UPDATE` dispara em **qualquer** chave unica | 16 upserts (5.2) | auditoria tabela a tabela; `menu_queues`/`team_queues` tem 2 UNIQUEs | Fase 2 (script de chaves unicas) |
| 5 | `key` e reservado | `bot_settings` (Python + PHP) | crase (funciona nos dois dialetos) | Fase 2/3 (smoke de settings) |
| 6 | `TEXT PRIMARY KEY`/`TEXT UNIQUE` | `queues`, `analyst_load`, `bot_settings`, `templates`, `bot_users` | `VARCHAR(n)` | Fase 2 (aplicar DDL a seco) |
| 7 | `REFERENCES` inline em `CREATE TABLE`/`ALTER` e **ignorada** | `schema.sql`, `db.py:_migrate_rbac`, `php:bootstrap` | `CONSTRAINT ... FOREIGN KEY ...` explicita | Fase 2 (contar FKs em `information_schema`) |
| 8 | `wait_timeout=300` derruba sessao ociosa | bot (processo de longa duracao) | `ping()`/reconexao no shim + log | Fase 5 (bot ocioso > 5 min) |
| 9 | `sql_mode` vazio -> truncamento silencioso | insercoes longas | `VARCHAR` folgado + validacao que ja existe no painel | Fase 3/5 (smoke) |
| 10 | Colacao `ci` mudaria `=`/UNIQUE e `LIKE` | chaves e buscas do painel | `utf8mb4_bin` nas chaves + `ai_ci` explicito nos 4 `LIKE` | Fase 3 (busca do painel) |
| 11 | `SELECT *` + coluna gerada quebraria `INSERT` montado do `fetch` | `db.py` (~25 sites com `SELECT *`) | indice **funcional** (coluna oculta), nao coluna gerada visivel | Fase 2 (grep `SELECT *`) |
| 12 | `autocommit=True` mascararia falta de `commit()` | `db.py` (usa `commit()` explicito) | `autocommit=False` no Python | Fase 2 (teste de upsert/rollback) |
| 13 | `innodb_lock_wait_timeout=50` travaria o tick do bot | bot + painel escrevendo juntos | `SET SESSION innodb_lock_wait_timeout=5` | Fase 5 (concorrencia painel+bot) |
| 14 | PDO com emulacao devolve tudo string | painel inteiro | `ATTR_EMULATE_PREPARES => false` | Fase 3 (abas comparando `===`) |
| 15 | `%` literal viraria placeholder | `LIKE 'u:%'` (3 sites) | `%` -> `%%` quando ha parametros | Fase 1 (unitario) |
| 16 | `julianday()` nao existe no MySQL | `db.py:508`, `index.php:3863`, `test_queue_schema.php:128` | `TIMESTAMPDIFF(SECOND, b, a)` | Fase 1/2 (metricas de load/fila) |
| 17 | `datetime(col, ?)` (offset do painel) | 8 sites no `index.php` | `CONVERT_TZ(col, '+00:00', ?)` | Fase 3 (filtros de data) |
| 18 | Sessao por thread (`to_thread`) acumulando conexoes | `engine.py:549` | `finally: close()` + reuso do shim | Fase 5 (`SHOW PROCESSLIST`) |
| 19 | `time_zone=-03` faria `CONVERT_TZ` errar se o offset vier nomeado | filtros de data do painel | offset sempre numerico (`-03:00`); nome exigiria tabelas `mysql.time_zone*` | Fase 3 (grep dos offsets) |
| 20 | `TEXT` em chave **composta**, `CREATE INDEX` ou `REFERENCES` inline -> 1170 (nao tratado pela regra 23, que so ve o caso inline da `CREATE TABLE`) | `schema.sql` (`pending_routes.kind`) x `db.py` (`pending_routes_active`) | coluna nasce `VARCHAR(191)` nos dois arquivos | Fase 1/2 (lint cruzado, 5.7) |
| 21 | `DEFAULT 'literal'` em coluna `TEXT` -> 1101 | `schema.sql` (`menu_option_questions.kind` = `'text'`, `queue_operators.source` = `'toa'`) | `DEFAULT ('literal')` (regra 24) | Fase 2 (aplicar DDL a seco) |
| 22 | subquery que le a **propria tabela** alvo do `UPDATE`/`DELETE` -> 1093 (e a unica que o SQLite tambem aceita silenciosamente) | `db.py::load_trim_history`, `db.py::_migrate_delivery` | derivada com alias (`AS recentes` / `AS rotas_ativas`), 5.6 | Fase 2 (boot real, nao unitario: driver falso nao tem 1093) |
| 23 | `CREATE INDEX IF NOT EXISTS` / `DROP INDEX IF EXISTS` nao existem no MySQL -> 1061/1064 | `db.py` (migracoes de indice) | consulta `information_schema.statistics` antes de criar (5.3) | Fase 2 (`ensure_schema` 2a passada em base ja migrada) |
| 24 | Paridade do indice parcial **unico** (a duvida "NULL colide?") | `menu_options_per_queue`, `pending_routes_active` | `(NULL, sort_order)` repetido em menus diferentes: **aceito** nos dois; `(menu_id, sort_order)` repetido no mesmo menu sem `parent_id`: **recusado** nos dois | Fase 2 (script de paridade SQLite <-> MySQL) |
| 25 | Valor sintetico do lint 1093 que nao serve as formas de `datetime(...)` do projeto -> o lint estoura (`float('-60 seconds')`) em vez de acusar SQL ruim | `tests/test_sqlcompat.py::_params_do_lint` | valor numerico sem unidade (`"60"`): `float()` na concatenada, `modifier_seconds()` na direta; a forma com offset so repassa | Fase 1 (`TempoTest.test_valor_do_lint_atravessa_as_tres_formas`) |
| 26 | `PRAGMA foreign_keys = ON` **dentro** do `schema.sql` religa a checagem no meio do `executescript` e desfaz o `OFF` do boot -> **1824** (`Failed to open the referenced table 'permission_groups'`) na primeira FK fora de ordem (`bot_users` e a tabela #1 e referencia `permission_groups`, que nasce so na #25) | `schema.sql:4` + `db.ensure_schema` | `sqlcompat.sem_pragma_fk()` tira o PRAGMA do script **só** no MySQL (o SQLite continua usando); a sessao fica OFF durante a criacao e ON no `finally` | Fase 2 (boot real: 31 tabelas / 30 FKs; `tmp_bak/fk_check.py` e `tmp_bak/boot_mysql.py`) |
| 27 | Backend decidido no **import** do `db` antes do `load_env()`: com `DB_BACKEND=mysql` no `.env` o `_BACKEND_MYSQL` fica `False` (sem `PRAGMA foreign_keys = OFF`, excecoes do `sqlite3` no `except` do bot) | `python/db.py` (import) x ordem em `python/main.py:161` e `run.py` | `load_env()` antes de importar o `db` (`main.py`/`run.py`) + `db._resolve_backend()` reavaliado no import **e** em `connect()` | Fase 2 (`test_backend_do_db_se_realinha_depois_do_import`) |
| 28 | `x IS ?` (valor por parâmetro) → **1064** (`IS 1`): o `IS` do MySQL só compara com `NULL`/`TRUE`/`FALSE` | `db.py:1263` (`queue_position`, chamada pelo tick de rotas a cada 5 s) | `x <=> %s` (regra 26); operando à esquerda composto falha alto em vez de gerar SQL inválido | Fase 4/5 (boot real: `tmp_bak/bot-20261006-161042.err` registrou o 1064 a cada 5 s) |
| 29 | Parâmetro **fora do grupo do `VALUES`** + `executemany` → `TypeError: not all arguments converted during string formatting`: o atalho `RE_INSERT_VALUES` do PyMySQL entrega a linha inteira só para o grupo do `VALUES` (o `postfix` nem é formatado) | `db.py:937`/`945` (`ingest_search_contact`: `last_seen = datetime('now')` no `ON DUPLICATE KEY UPDATE`) | a fachada só usa o lote do driver quando todo `%s` está no `VALUES`; fora disso executa linha a linha (4.2) | Fase 4/5 (boot real; o driver falso dos testes **não** emula o atalho) |

---

## 7. Fases de execucao — passo a passo com GATE

Cada fase tem **Objetivo**, **Arquivos**, **Passos**, **GATE** (comandos + criterio de aceite) e
**Nao avance se...**. As fases sao **cumulativas**: o GATE da fase N tem de estar verde antes de
comecar a N+1. Comandos de teste usados em todas as fases (PowerShell, na raiz do repo):

```powershell
# Python: a suite e executada ARQUIVO POR ARQUIVO (nao existe tests/__init__.py:
# `python -m unittest discover` falha com "Start directory is not importable")
Get-ChildItem tests\test_*.py | ForEach-Object { python $_.FullName }
# PHP
Get-ChildItem tests\test_*.php | ForEach-Object { php $_.FullName }
```

### Fase 0 - baseline e preparacao (nao muda codigo)

**Objetivo:** provar que o repositorio esta verde **antes** de mexer em qualquer coisa e guardar
uma copia consistente do SQLite.
**Arquivos:** nenhum de codigo (so `data\backup_pre-mysql.sqlite`).
**Passos:**
1. Parar o bot e o painel (o arquivo SQLite tem de ficar quieto durante a copia).
2. `Copy-Item data\helptoa.sqlite data\backup_pre-mysql.sqlite` + `Get-FileHash` (guardar o hash).
3. Rodar os dois blocos de teste acima e anotar os numeros.
4. Medir o servidor:
   `SELECT VERSION(), @@sql_mode, @@time_zone, @@lower_case_table_names, @@wait_timeout, @@innodb_lock_wait_timeout;`
5. Criar a base vazia e o usuario (ALL PRIVILEGES no schema).
6. Preparar os drivers (§3/§8): `pip install -r requirements.txt` (traz o `PyMySQL`) e habilitar
   `extension=pdo_mysql` no `C:\tools\php83\php.ini`; conferir com
   `python -c "import pymysql; print(pymysql.__version__)"` e `php -m | Select-String pdo_mysql`.

**GATE:** os 23 arquivos Python **já existentes** -> **398 casos / 0 falhas** (`tests/test_sqlcompat.py`
só nasce na Fase 1); 10 arquivos PHP -> `exit 0`; hash do
backup anotado; base vazia criada; `PyMySQL` importavel e `pdo_mysql` carregado.
**Nao avance se:** qualquer teste falhar (o baseline tem de estar verde) ou se o servidor nao
responder ao `SELECT VERSION()` (§3).

### Fase 1 - `sqlcompat.py` + `tests/test_sqlcompat.py` (nada plugado)

**Objetivo:** ter o tradutor pronto e testado **sem** conectar em ninguem.
**Arquivos:** `python/sqlcompat.py` (novo), `python/config.py` (chaves novas), `tests/test_sqlcompat.py` (novo).
**Passos:**
1. Implementar as 22 regras de 5.1 + `convert_params`, `is_disconnect` e o cursor/`rowcount` (4.2).
2. `python tests/test_sqlcompat.py`.
3. `python -c "import sys; sys.path.insert(0,'python'); import sqlcompat; print(sqlcompat.__all__)"`.
4. Rodar a suite completa de novo.

**GATE:** `python tests/test_sqlcompat.py` verde (**89 casos**) **e** a suite inteira verde - **487
casos / 0 falhas** nos 24 arquivos (prova de que nada foi plugado ainda).
**Nao avance se:** faltar caso de teste para alguma construcao real do inventario (as 34
ocorrencias de `datetime('now')`, os 5 indices parciais, os 16 upserts, os 3 `LIKE` com `%`).

### Fase 2 - `schema.mysql.sql` + migrador + auditoria (base de TESTE)

**Objetivo:** ter o DDL MySQL espelhando o SQLite atual (tabelas, indices, FKs, defaults) e o
migrador copiando dados sem conversao de data.
**Arquivos:** `schema.mysql.sql` (novo, 4.6), `bin/migrar_sqlite_para_mysql.py` (novo, 4.9),
`tests/test_mysql_schema.py` + `tests/test_mysql_upserts.py` (novos, gated).
**Passos:**
1. `python bin/migrar_sqlite_para_mysql.py --schema` -> aplica o DDL do zero. Rodar **duas vezes**
   (tem de ser idempotente).
2. Rodar as consultas de auditoria 2.1-2.5 (abaixo) e conferir item a item.
3. Provar os indices parciais: inserir um 2o `pending_routes` **encerrado** para o mesmo
   `(kind, chat_id)` (deve passar) e um 2o **ativo** (deve dar erro `1062`).
4. `MYSQL_TEST=1 MYSQL_TEST_DATABASE=botsqlite_test python tests/test_mysql_schema.py`
5. `MYSQL_TEST=1 MYSQL_TEST_DATABASE=botsqlite_test python tests/test_mysql_upserts.py`
6. Rodar a suite antiga completa.

**GATE:** DDL aplicado 2x sem erro; consultas 2.1-2.5 sem divergencia (nenhuma coluna de chave
`TEXT`/`BLOB`, 5 indices funcionais presentes, FKs existentes, defaults `UTC_TIMESTAMP`); duplicata
ativa recusada; testes gated verdes; **487 casos / 0 falhas**.
**Nao avance se:** aparecer `BLOB/TEXT column used in key specification`, FK faltando em
`key_column_usage`, `DEFAULT NOW()`/`CURRENT_TIMESTAMP` em alguma coluna, ou contagem de indices
parciais diferente de 5.
**Rollback:** `DROP DATABASE botsqlite_test` (ou dropar tabela a tabela). Producao intocada.

```sql
-- 2.1 tabelas
SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() ORDER BY table_name;
-- 2.2 chaves unicas (as que os 16 upserts usam): cada linha aqui e um alvo de ON DUPLICATE KEY
SELECT table_name, index_name, GROUP_CONCAT(column_name ORDER BY seq_in_index), non_unique
  FROM information_schema.statistics
 WHERE table_schema = DATABASE() GROUP BY table_name, index_name, non_unique ORDER BY table_name, index_name;
-- 2.3 FKs explicitas (o REFERENCES inline seria ignorado em silencio)
SELECT table_name, constraint_name, column_name, referenced_table_name
  FROM information_schema.key_column_usage
 WHERE table_schema = DATABASE() AND referenced_table_name IS NOT NULL;
-- 2.4 colunas-chave: tipo (nada de TEXT/BLOB) e colacao (as chaves em utf8mb4_bin)
--      (`collation` nao existe em `information_schema.columns`: o nome e `COLLATION_NAME`;
--       com `collation` a consulta morre em **1054 Unknown column 'collation'**)
SELECT table_name, column_name, column_type, COLLATION_NAME, is_nullable, column_default
  FROM information_schema.columns
 WHERE table_schema = DATABASE() AND (column_key <> '' OR COLLATION_NAME LIKE '%\_bin')
 ORDER BY table_name, ordinal_position;
-- 2.5 defaults de data (tem de aparecer UTC_TIMESTAMP, nunca NOW/CURRENT_TIMESTAMP)
SELECT table_name, column_name, column_default FROM information_schema.columns
 WHERE table_schema = DATABASE() AND column_default LIKE '%timestamp%';
```

### Fase 3 - painel PHP — **CONCLUIDA (06/10/2026)**

**Objetivo:** o painel funcionar nos dois backends **sem reescrever as ~117 queries** do `index.php`
(o `BotPDO`/`BotStatement` traduz no choke point). **Feito.**
**Arquivos:** `php/db.php` (NOVO, 1400 linhas: `bot_db_backend`, `bot_mysql_config`, `BotPDO`,
`BotStatement`, o tradutor `bot_sql_traduz` e `bot_like_ci`), `php/bootstrap.php` (`require` da
fachada, `bot_pdo()` -> `bot_db_abre()`, `ensure_schema` via `bot_executa_script`, 1093 do
`bot_migrate_delivery`), `php/index.php` (4 `LIKE` com `COLLATE` + erro de constraint por codigo do
driver), `tests/_panel_render.php` (modo MySQL gated), `tests/test_mysql_painel.php` (NOVO, gated),
`tests/test_sqlcompat_php.php` (NOVO, offline, 42 checagens).

**Como ficou (o que difere do plano):**

1. **`prepare()`/`query()`/`exec()` do `BotPDO` traduzem e a `BotStatement` resolve o plano de
   parametros no `execute`.** O `datetime(<coluna>, ?)` do painel recebe o **modificador**
   (`'-3 hours'`), nao um offset `'-03:00'`: o SQL sai `DATE_ADD(<coluna>, INTERVAL ? SECOND)` e o
   parametro e convertido para segundos (-10800) na hora de executar. O `CONVERT_TZ` da regra 8 fica
   para offset **literal** no SQL (que o painel nao usa).
2. **`php -l` + 3 suites de painel:** o `_panel_render.php` decide o modo por `SQLITE_PATH` (suites
   SQLite, backend fixado em `sqlite`) e, sem ele, por `MYSQL_TEST=1` + `MYSQL_TEST_DATABASE` (nome
   com "test" obrigatorio). Um `MYSQL_TEST=1` esquecido no terminal nao contamina as suites de SQLite.
3. **Paridade de DDL comprovada:** o `schema.sql` inteiro (54 comandos) sai **byte a byte igual** ao
   do `sqlcompat.translate` (comparacao direta dos dois tradutores) — mesma colacao `utf8mb4_bin`,
   mesmas FKs explicitas, mesmos indices funcionais.
4. **Bug achado no port:** no PHP, `preg_match($p, $sujeito, $m, 0, $offset)` mantem o `^` ancorado no
   inicio do SUJEITO (nao no offset): o passo do `julianday` precisou de `\G`. Sem isso o
   `julianday(s.at) - julianday(MAX(e.at))` da aba Dashboard estourava.

**Medicoes (MySQL 8.0.44 local, base `botsqlite_test`, 31 tabelas / 209 876 `bot_events`):**
`php tests/test_mysql_painel.php` -> **MYSQL PAINEL OK** (46 checagens): 13 abas rendem sem
`SQLSTATE`/warning, busca por nome **minusculo achando address com MAIUSCULA** (a colacao das colunas
`_bin`), filtro de data com o deslocamento de 3 h conferido na virada do dia local, `POST add_member`
gravando, `rowCount()` com semantica de linhas casadas e 30 FKs no destino. Suites: **12 arquivos PHP
com `exit 0`** (SQLite, com e sem `MYSQL_TEST` no ambiente) e a suite Python inteira (24 arquivos,
491 casos).

**GATE:** atendido — 12 arquivos PHP com `exit 0` no SQLite; smoke MySQL com **todas** as abas
renderizando e salvando; nenhuma excecao `SQLSTATE`.
**Rollback:** `DB_BACKEND=sqlite` no `.env` (o caminho MySQL nao e tocado quando o backend e sqlite).

**Ordem dos gates:** o smoke **grava** na base de teste (usuario, time, membro, fila e dois
`bot_events`), entao o `--check` do migrador tem de rodar **antes** dele — ou a carga e refeita depois.
Com a base recem-carregada, `--check` = 0 diferenca; depois do smoke, as 4 tabelas semeadas divergem
por desenho (o teste precisaria de linhas proprias para exercitar busca, data e gravacao).

**Dependencia de ambiente (nao esta no repo):** o painel em MySQL exige `extension=pdo_mysql` no
`php.ini` (no Windows o DLL ja vem na pasta `ext`, so descomentar) e um MySQL 8 alcancavel com a base
de teste e **privilegio** nela. Foi exatamente ali que apareceu o **1044** das pendencias: a credencial
de `.env` (`F104751`) tem `ALL PRIVILEGES` so em `chat\_bot\_toa`; a base de teste do gate roda em um
servidor local separado (`127.0.0.1:3307`), com `root` de teste.

### Fase 4 - bot Python

**Objetivo:** o bot rodar nos dois backends, com o SQLite intacto para os testes.
**Arquivos:** `python/db.py` (troca do `connect()` + as 3 colunas que geram data + o `julianday` da
linha 508 + `%` nos `LIKE`), `python/engine.py` (fechamento da sessao por thread), `python/main.py`,
`requirements.txt` (PyMySQL).
**Passos:**
1. `Get-ChildItem tests\test_*.py | ForEach-Object { python $_.FullName }` (default `sqlite`).
2. Boot com `DB_BACKEND=sqlite` e conferir no log qual backend foi escolhido.
3. `MYSQL_TEST=1 MYSQL_TEST_DATABASE=botsqlite_test python tests/test_mysql_upserts.py`.

**GATE:** **501 casos / 0 falhas**; boot logando backend `sqlite`; os 16 upserts validados no
MySQL de teste.
**Nao avance se:** qualquer caso falhar, o log mostrar backend errado, ou sobrar
`sqlite3`/`PRAGMA`/`julianday` em caminho MySQL (`grep` de conferencia).

**Achados do ensaio (06/10/2026, boot real com `DB_BACKEND=mysql`, base `botsqlite_test`):** o log
(`tmp_bak/bot-20261006-161042.err`) mostrou dois erros de **runtime** que a suite nao pegava — o
`1064` de `x IS ?` (armadilha 28, agora regra 26) e o `TypeError` do `executemany` com parametro
fora do grupo do `VALUES` (armadilha 29, caminho linha-a-linha em 4.2). Os dois nasceram do driver
**falso** dos testes, que nao emula o inlineamento do PyMySQL nem o atalho `RE_INSERT_VALUES`; os
testes novos fecham esse ponto cego (`IgualdadeNulaTest` + os de `executemany` em
`tests/test_sqlcompat.py`). Com as correcoes: boot E2E limpo (`search_contact: 278 operadores em
82 filas`, zero `ERROR`/`Traceback`), `db.queue_position` respondendo contra o MySQL real (fila
`811/812/815` -> posicao `1/2/3`) e a suite em **501 casos / 0 falhas** (eram 491: +10 dos testes
novos). Probes em `tmp_bak/` (`probe_queue_position.py`, `probe_mysql_real.py`).

**Rollback:** `DB_BACKEND=sqlite`.

### Fase 5 - ensaio de integracao com dados reais (copia)

**Objetivo:** rodar bot + painel **juntos** contra um MySQL com os dados reais copiados, antes de
tocar na producao.
**Arquivos:** nenhum de codigo (so configuracao apontando para `botsqlite_test`).
**Passos:**
1. `python bin/migrar_sqlite_para_mysql.py --sqlite data\backup_pre-mysql.sqlite --schema --apply`
   (base de teste) e depois `--check` -> 0 diferenca.
2. Subir o painel (backend mysql) e o bot (backend mysql) apontando para a base de teste.
3. Observar >= 30 min / varios ciclos reais: transferencia, formulario, fila de espera, entrega,
   leitura de load.
4. `SHOW PROCESSLIST` durante o periodo.

**GATE:**
- `python bin/migrar_sqlite_para_mysql.py --check` -> 0 diferenca de contagem/`MAX(id)`.
- Log do bot **sem** `OperationalError 2006`/`2013` e sem `Lost connection`.
- Bot ocioso > 5 min (nenhuma transferencia) e depois uma acao real -> sessao volta sozinha (ping).
- `SHOW PROCESSLIST` sem sessao orfa acumulando.
- Painel salvando durante uma transferencia -> sem `Lock wait timeout` (5 s) e sem sequencia de
  `id` quebrada.
**Nao avance se:** qualquer erro de sessao/lock acima aparecer, ou se alguma contagem de
`--check` divergir.
**Rollback:** desligar os dois processos e voltar para `DB_BACKEND=sqlite` (o SQLite de origem
nunca foi tocado).

### Fase 6 - cutover (producao)

**Objetivo:** virar a chave para MySQL com o menor intervalo possivel de indisponibilidade.
**Arquivos:** `.env` do servidor (so `DB_BACKEND` + credenciais).
**Passos:**
1. Parar o bot; parar o painel; confirmar que nao ha `helptoa.sqlite` aberto.
2. Backup final: `data\backup_cutover.sqlite` + hash.
3. `python bin/migrar_sqlite_para_mysql.py --sqlite data\helptoa.sqlite --schema --apply`
   e, na sequencia, `--check`.
4. Trocar o `.env` para `DB_BACKEND=mysql` (+ host/porta/usuario/senha/base).
5. Subir o painel (conferir login e as abas) e depois o bot.
6. Monitorar 30-60 min: log do bot, `SHOW PROCESSLIST`, contagens.

**GATE:** `--check` = 0 diferenca logo apos a carga; painel e bot operando; log limpo; **501/0** + 12
suites verdes no repositorio.
**Nao avance se:** o `--check` divergir, o login falhar ou aparecer excecao de SQL no log do bot.
**Rollback (1 linha):** parar os dois, `DB_BACKEND=sqlite`, subir de novo. O arquivo SQLite
continua com os dados de antes do cutover; o que foi escrito no MySQL depois do cutover (poucos
minutos) se perde - por isso a janela de observacao do passo 6 e curta e vigiada.

---

## 8. Dependencias externas (o que preciso do ambiente)

1. **Credenciais do MySQL** - as chaves ja existem no `.env` (`IP_MYSQL`, `PORTA_MYSQL`,
   `USUARIO_MYSQL`, `SENHA_MYSQL`, `DATABASE_MYSQL`); falta apenas o **valor** de cada uma e,
   depois, `DB_BACKEND=mysql`. Sem isso, o default `sqlite` continua valendo (nada quebra).
2. **Base de teste separada** (recomendada, nao obrigatoria): um schema `botsqlite_test` no mesmo
   servidor permite rodar as fases 2, 3 e 5 sem tocar na producao. Se nao houver, as fases ficam
   restritas ao `--schema` + tabelas temporarias e o cutover fica mais arriscado.
3. **Versao minima real:** `SELECT VERSION();` precisa dar **8.0.19+** (por causa do alias
   `INSERT … AS new ON DUPLICATE KEY UPDATE`). Entre 8.0.13 e 8.0.18: usar `VALUES(col)`
   (deprecado, mas funciona) - uma troca mecanica no `sqlcompat.py`. Abaixo de 8.0.13 nao ha
   indice funcional nem `DEFAULT (UTC_TIMESTAMP())` -> cairia em coluna gerada explicita.
4. **Privilegios:** `ALL PRIVILEGES` no schema basta (inclui `CREATE`/`REFERENCES`, necessarios
   para criar as FKs).
5. **Onde roda o bot e o painel:** mesma maquina (hoje) ou hosts diferentes? No caso de hosts
   diferentes, `IP_MYSQL` deixa de ser `localhost` e o fuso do *servidor* MySQL continua
   irrelevante (usamos `UTC_TIMESTAMP()` + `SET time_zone='+00:00'`).
6. **Janela de manutencao:** o cutover (Fase 6) exige ~10 min com bot e painel parados.

## 9. Riscos residuais (aceitos, com mitigacao) e fora de escopo

| Risco | Por que aceito | Mitigacao |
|---|---|---|
| `sql_mode=''` sem strict -> truncamento silencioso em `VARCHAR` | o SQLite tambem nao reclamava de tamanho | `VARCHAR` folgado (4.6.2) + validacao que ja existe no painel; se aparecer dado cortado, ligar `STRICT_TRANS_TABLES` **so** em uma sessao de auditoria |
| Dois processos escrevendo (bot + painel) | e o desenho atual, so que com dois *writers* de verdade | `innodb_lock_wait_timeout=5`, transacoes curtas (o padrao do codigo), retry no proximo tick em caso de lock |
| Upsert devolvendo `2` (linha atualizada) em vez de `1` | nenhum site do `db.py` compara com `1`; o painel so compara com `0` | auditoria da Fase 2 (`rowcount == 1`) |
| Colacao `_bin` deixar uma busca do painel "sensivel a maiuscula" | fidelidade ao `=` do SQLite e aos `UNIQUE` | `COLLATE utf8mb4_0900_ai_ci` nos 4 `LIKE` de busca por nome/endereco |
| Sem retry automatico de reconexao no meio de um tick | o tick seguinte refaz o trabalho (o codigo ja tolera) | `ping()` no inicio do tick + log de `is_disconnect` |
| Dump com dados reais versionado por engano | `.gitignore` ja cobre `.env`/`data/*.sqlite*` | conferir o `.gitignore` antes do primeiro `git add` (4.11) |
| Credencial de producao **sem privilegio na base de teste** -> `1044` (medido) | o usuario do `.env` tem `ALL PRIVILEGES` so em `chat\_bot\_toa`; nao e limite de conexoes | base de teste em servidor separado (`127.0.0.1:3307`) + `MYSQL_TEST=1`, que **forca** o MySQL e recusa base sem "test" no nome (`php/db.php::bot_mysql_config`) |
| `MYSQL_TEST=1` esquecido no terminal | poderia apontar o painel para outra base | a trava do nome ("test") no `bot_mysql_config` + o harness fixando `DB_BACKEND=sqlite` quando recebe `SQLITE_PATH` |

**Fora de escopo (nao faz parte desta mudanca):** replicacao/HA, troca de driver por
SQLAlchemy/ORM, cache Redis, migracao dos eventos historicos de `analytics_events` para
*timestamp* nativo, unificacao das migracoes PHP/Python num so framework, e reescrita das ~200
queries para "SQL 100% ANSI".

---

## 10. Checklist final (o que tem de ser verdade no fim)

| # | Afirmacao | Como verificar |
|---|---|---|
| 1 | O default e SQLite e nada mudou para quem nao tem MySQL | `DB_BACKEND` ausente no `.env` -> `python run.py` loga backend `sqlite` |
| 2 | Os 501 casos Python (24 arquivos) e as 12 suites PHP seguem verdes | `Get-ChildItem tests\test_*.py \| % { python $_.FullName }`; idem `.php` |
| 3 | O tradutor cobre as 26 regras de 5.1 + upserts (5.2) + indices funcionais (5.3) | `python tests/test_sqlcompat.py` |
| 4 | O DDL MySQL cria as 31 tabelas com indices, FKs e defaults UTC | consultas 2.1-2.5 do §7 na base real |
| 5 | Os 5 indices parciais viraram funcionais e continuam barrando duplicata ativa | consulta 2.2 (`EXPRESSION`) + teste de duplicata |
| 6 | Nenhuma coluna de chave e `TEXT`/`BLOB` | consulta 2.4 -> tudo `varchar(...)` |
| 7 | As chaves de igualdade estao em `utf8mb4_bin` e os 4 `LIKE` em `ai_ci` | consulta 2.4 + busca do painel com maiuscula |
| 8 | Nenhum default/valor gravado usa `NOW()` | consulta 2.5 + `grep -n "NOW()"` no codigo |
| 9 | `rowcount`/`rowCount()` com semantica de linhas casadas | flags nas duas conexoes (4.7/4.2) + auditoria de `== 1` |
| 10 | Bot e painel rodam juntos sem lock/`Lost connection` | Fase 5 (portoes) |
| 11 | As contagens origem x destino batem | `bin/migrar_sqlite_para_mysql.py --check` -> 0 diferenca |
| 12 | O rollback e uma linha | `DB_BACKEND=sqlite` + reiniciar os dois processos |
| 13 | A documentacao do repo descreve os dois backends | `README.md` + `docs/desenho-bot.md` (4.11) |
| 14 | Nada de dado real foi commitado | `git status` sem `.env`/`data/*.sqlite*` |

### Ordem de leitura recomendada

1. §2 (decisao), §3 (fatos do ambiente) e §4 (inventario) para entender **o que** muda e **onde**.
2. §5 (mapa de traducao) e §6 (armadilhas) sao o contrato tecnico do `sqlcompat.py`.
3. §7 (fases) e a ordem de execucao; cada fase tem entregaveis, portao e rollback.
4. §8 (o que falta do ambiente) e §9 (riscos aceitos) fecham o que depende de fora do codigo.


