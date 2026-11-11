from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import sqlcompat
from .config import sqlite_path, ROOT, db_backend

log = logging.getLogger("helptoa")

SCHEMA_PATH = ROOT / "schema.sql"

# Excecoes na forma do backend ativo (secao 2.2): o codigo do bot captura
# `db.IntegrityError`, nunca a classe do driver.
_BACKEND_MYSQL = False


def _resolve_backend() -> None:
    """Realinha o backend ativo com o `DB_BACKEND` do ambiente (secao 2.2).

    O import do modulo nao pode ser o juiz: o `.env` entra DEPOIS (o
    `python/main.py` chama `load_env()`, mas ele proprio ja importou este
    modulo, e o `run.py` importa o `main` antes de tudo). Se o backend ficasse
    decidido no import, com `DB_BACKEND=mysql` o `ensure_schema` perderia o
    `PRAGMA foreign_keys = OFF` (erro 1824 nas FKs fora de ordem do
    `schema.sql`) e o bot capturaria a excecao do driver errado. Por isso quem
    abre a conexao (`connect()`) reavalia; o import so deixa o padrao.
    """
    global _BACKEND_MYSQL, IntegrityError, OperationalError, ProgrammingError
    _BACKEND_MYSQL = db_backend() == sqlcompat.MYSQL
    IntegrityError = sqlcompat.IntegrityError if _BACKEND_MYSQL else sqlite3.IntegrityError
    OperationalError = sqlcompat.OperationalError if _BACKEND_MYSQL else sqlite3.OperationalError
    ProgrammingError = sqlcompat.ProgrammingError if _BACKEND_MYSQL else sqlite3.ProgrammingError


_resolve_backend()


def _dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def connect():
    """Abre a conexao do backend configurado (`DB_BACKEND`; padrao `sqlite`).

    Este e o unico ponto de conexao do bot: com `sqlite` devolve o mesmo
    `sqlite3.Connection` com os PRAGMAs de sempre; com `mysql` a fachada do
    `sqlcompat` traduz o dialeto aqui, sem o resto do codigo saber a diferenca.
    O backend e reavaliado aqui (`_resolve_backend`): o `.env` pode ter chegado
    depois do import deste modulo.
    """
    _resolve_backend()
    conn = sqlcompat.connect(sqlite_path=sqlite_path(), row_factory=_dict_factory)
    ensure_schema(conn)
    return conn


def conn_closed(conn) -> bool:
    try:
        if conn is None:
            return True
        conn.execute("SELECT 1")
        return False
    except Exception:
        return True


def _table_names(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def ensure_schema(conn) -> None:
    if "menu_options" in _table_names(conn):
        _migrate_submenu(conn)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    # O `schema.sql` cria as tabelas na ordem em que foram escritas, que nao e a
    # ordem das FKs (`menu_options` referencia `transfer_targets`, que nasce
    # depois). O MySQL exige a tabela referenciada no instante do `CREATE`, mas
    # com `foreign_key_checks=0` deixa passar; no SQLite o PRAGMA equivalente
    # nao muda nada de util aqui, entao ele fica so no MySQL.
    if _BACKEND_MYSQL:
        conn.commit()                       # o SET nao vale com transacao aberta
        conn.execute("PRAGMA foreign_keys = OFF")
        # O `PRAGMA foreign_keys = ON` do proprio script (linha 4, para o
        # SQLite) religaria a checagem no meio do `executescript` e desfaria o
        # OFF acima: no MySQL quem manda e o `finally` la embaixo.
        sql = sqlcompat.sem_pragma_fk(sql)
    try:
        conn.executescript(sql)
    finally:
        if _BACKEND_MYSQL:
            conn.execute("PRAGMA foreign_keys = ON")
    _migrate_submenu(conn)
    _migrate_rbac(conn)
    _migrate_ask(conn)
    _migrate_queue(conn)
    _migrate_queue_priority(conn)
    _migrate_bot_events_chat_idx(conn)
    _migrate_delivery(conn)
    _migrate_form_aware(conn)
    _migrate_case_keys(conn)
    _migrate_load(conn)
    _migrate_kinds(conn)
    _migrate_team_schedules(conn)
    _migrate_form_window(conn)
    _migrate_events_author(conn)
    _migrate_queue_load(conn)


def _migrate_queue_load(conn) -> None:
    """Fila do TOA por endereco (card FILA FORMS do dashboard).

    Base antiga nao ganha a tabela com o CREATE IF NOT EXISTS do schema.sql
    rodar de novo? Ganha — CREATE IF NOT EXISTS cobre tabela nova. A funcao
    existe para o mesmo padrao das demais migracoes (checagem explicita) e para
    o painel PHP espelhar a chamada.
    """
    if "queue_load" not in _table_names(conn):
        conn.execute(
            """CREATE TABLE queue_load (
                 queue_address TEXT PRIMARY KEY,
                 at TEXT NOT NULL DEFAULT (datetime('now')),
                 pending_forms INTEGER NOT NULL DEFAULT 0,
                 pending_chats INTEGER NOT NULL DEFAULT 0,
                 toa_ok INTEGER NOT NULL DEFAULT 0,
                 detail TEXT NOT NULL DEFAULT ''
               )"""
        )


def _migrate_events_author(conn) -> None:
    """Autor do chat/formulario em bot_events: colunas novas em base antiga.

    Sem o ALTER, o INSERT do `_emit` (que ja manda author_addr/author_name)
    quebraria em banco criado antes destas colunas — e o painel local plugado
    no banco de producao depende delas para a coluna AUTOR da aba Atividade.
    """
    if "bot_events" not in _table_names(conn):
        return
    cols = _table_columns(conn, "bot_events")
    if "author_addr" not in cols:
        conn.execute("ALTER TABLE bot_events ADD COLUMN author_addr TEXT NOT NULL DEFAULT ''")
    if "author_name" not in cols:
        conn.execute("ALTER TABLE bot_events ADD COLUMN author_name TEXT NOT NULL DEFAULT ''")


def _table_columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_submenu(conn) -> None:
    cols = _table_columns(conn, "menu_options")
    if "parent_id" not in cols:
        conn.execute(
            "ALTER TABLE menu_options ADD COLUMN parent_id INTEGER REFERENCES menu_options(id) ON DELETE CASCADE"
        )
    chat_cols = _table_columns(conn, "chats")
    if "menu_node_id" not in chat_cols:
        conn.execute(
            "ALTER TABLE chats ADD COLUMN menu_node_id INTEGER REFERENCES menu_options(id) ON DELETE SET NULL"
        )
    if "reply_text_if_form" not in cols:
        conn.execute("ALTER TABLE menu_options ADD COLUMN reply_text_if_form TEXT NOT NULL DEFAULT ''")
    if "action_if_form" not in cols:
        conn.execute("ALTER TABLE menu_options ADD COLUMN action_if_form TEXT NOT NULL DEFAULT ''")
    if "script_slug" not in cols:
        conn.execute("ALTER TABLE menu_options ADD COLUMN script_slug TEXT NOT NULL DEFAULT ''")
    chat_cols = _table_columns(conn, "chats")
    if "script_slug" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN script_slug TEXT NOT NULL DEFAULT ''")
    if "script_field_index" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN script_field_index INTEGER NOT NULL DEFAULT 0")
    if "script_answers" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN script_answers TEXT NOT NULL DEFAULT '{}'")
    if "script_uuid" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN script_uuid TEXT NOT NULL DEFAULT ''")
    if "script_started_at" not in chat_cols:
        conn.execute("ALTER TABLE chats ADD COLUMN script_started_at TEXT")
    qo_tables = _table_names(conn)
    if "queue_operators" in qo_tables:
        qo_cols = _table_columns(conn, "queue_operators")
        if "source" not in qo_cols:
            conn.execute(
                "ALTER TABLE queue_operators ADD COLUMN source TEXT NOT NULL DEFAULT 'toa'"
            )
    if "team_routing" in qo_tables and "cap_cases" not in _table_columns(conn, "team_routing"):
        conn.execute("ALTER TABLE team_routing ADD COLUMN cap_cases INTEGER")
    if "team_member_routing" in qo_tables and "cap_cases" not in _table_columns(conn, "team_member_routing"):
        conn.execute("ALTER TABLE team_member_routing ADD COLUMN cap_cases INTEGER")
    conn.execute("DROP INDEX IF EXISTS menu_options_per_menu")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_root
           ON menu_options (menu_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_child
           ON menu_options (menu_id, parent_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NOT NULL"""
    )
    # O texto do `CREATE TABLE` nao tem dialeto comum: `sqlite_master` no
    # SQLite, `SHOW CREATE TABLE` no MySQL. O shim resolve por backend.
    sql = sqlcompat.table_ddl(conn, "menu_options")
    if sql and "run_script" not in sql:
        _rebuild_menu_options_check(conn)


def _rebuild_menu_options_check(conn) -> None:
    # Fecha o que estava pendente antes de mexer em FK: o MySQL ignora
    # `FOREIGN_KEY_CHECKS` dentro de transacao aberta (e o driver nao e
    # autocommit), entao sem isto o `DROP TABLE` do rebuild falharia com as
    # tabelas que referenciam `menu_options`. No SQLite (autocommit) e no-op.
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    conn.execute(
        """CREATE TABLE menu_options_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id INTEGER REFERENCES menus(id) ON DELETE CASCADE,
            queue_address VARCHAR(191) REFERENCES queues(address) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL,
            label TEXT NOT NULL,
            classification TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL CHECK (action IN ('reply_and_close', 'ask_and_transfer', 'transfer_only', 'submenu', 'run_script')),
            reply_text TEXT NOT NULL DEFAULT '',
            reply_text_if_form TEXT NOT NULL DEFAULT '',
            action_if_form TEXT NOT NULL DEFAULT '',
            transfer_target_id INTEGER REFERENCES transfer_targets(id),
            parent_id INTEGER REFERENCES menu_options(id) ON DELETE CASCADE,
            script_slug TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        """INSERT INTO menu_options_new (id, menu_id, queue_address, sort_order, label, classification, action, reply_text, reply_text_if_form, action_if_form, transfer_target_id, parent_id, script_slug)
           SELECT id, menu_id, queue_address, sort_order, label, classification, action, reply_text,
                  COALESCE(reply_text_if_form, ''), COALESCE(action_if_form, ''), transfer_target_id, parent_id,
                  COALESCE(script_slug, '') FROM menu_options"""
    )
    conn.execute("DROP TABLE menu_options")
    conn.execute("ALTER TABLE menu_options_new RENAME TO menu_options")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_queue
           ON menu_options (queue_address, sort_order) WHERE queue_address IS NOT NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_root
           ON menu_options (menu_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_child
           ON menu_options (menu_id, parent_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NOT NULL"""
    )
    conn.execute("COMMIT")
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_rbac(conn) -> None:
    """RBAC do painel: grupo de permissao e escopo. Espelho de bot_migrate_rbac (PHP)."""
    if "bot_users" not in _table_names(conn):
        return
    cols = _table_columns(conn, "bot_users")
    if "group_id" not in cols:
        conn.execute(
            "ALTER TABLE bot_users ADD COLUMN group_id INTEGER REFERENCES permission_groups(id) ON DELETE SET NULL"
        )
    if "display_name" not in cols:
        conn.execute("ALTER TABLE bot_users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
    if "operator_address" not in cols:
        conn.execute("ALTER TABLE bot_users ADD COLUMN operator_address TEXT NOT NULL DEFAULT ''")
    if "last_login_at" not in cols:
        conn.execute("ALTER TABLE bot_users ADD COLUMN last_login_at TEXT")
    _promote_legacy_panel_users(conn)
    _copy_user_scope_to_groups(conn)


def _promote_legacy_panel_users(conn) -> None:
    """Promove os usuarios anteriores ao RBAC para Super Admin, uma unica vez."""
    tables = _table_names(conn)
    if "permission_groups" not in tables or "bot_settings" not in tables:
        return
    row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", ("rbac_seeded",)).fetchone()
    if row is None or str(row["value"]) == "1":
        return
    super_row = conn.execute(
        "SELECT id FROM permission_groups WHERE is_super = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if super_row is not None:
        conn.execute(
            "UPDATE bot_users SET group_id = ? WHERE group_id IS NULL", (int(super_row["id"]),)
        )
    conn.execute("UPDATE bot_settings SET value = ? WHERE key = ?", ("1", "rbac_seeded"))


def _copy_user_scope_to_groups(conn) -> None:
    """Escopo passou a ser do grupo: copia uma vez user_teams/user_queues para group_*."""
    tables = _table_names(conn)
    if "group_teams" not in tables or "group_queues" not in tables or "bot_settings" not in tables:
        return
    row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", ("rbac_scope_groups",)).fetchone()
    if row is None or str(row["value"]) == "1":
        return
    if "user_teams" in tables:
        conn.execute(
            """INSERT OR IGNORE INTO group_teams (group_id, team_id)
               SELECT u.group_id, ut.team_id FROM user_teams ut
                 JOIN bot_users u ON u.id = ut.user_id
                WHERE u.group_id IS NOT NULL"""
        )
    if "user_queues" in tables:
        conn.execute(
            """INSERT OR IGNORE INTO group_queues (group_id, queue_address)
               SELECT u.group_id, uq.queue_address FROM user_queues uq
                 JOIN bot_users u ON u.id = uq.user_id
                WHERE u.group_id IS NOT NULL"""
        )
    conn.execute("UPDATE bot_settings SET value = ? WHERE key = ?", ("1", "rbac_scope_groups"))


def _migrate_ask(conn) -> None:
    """Perguntas antes de transferir + atividade do tecnico (timeout por silencio)."""
    if "chats" not in _table_names(conn):
        return
    cols = _table_columns(conn, "chats")
    if "ask_option_id" not in cols:
        conn.execute(
            "ALTER TABLE chats ADD COLUMN ask_option_id INTEGER REFERENCES menu_options(id) ON DELETE SET NULL"
        )
    if "last_reply_at" not in cols:
        conn.execute("ALTER TABLE chats ADD COLUMN last_reply_at TEXT")
    if "moved_at" not in cols:
        # Transferencia parcial: o `move_chat` ja foi feito e o `invite_to_chat` ainda
        # nao. Sem a marca o tick repetiria o move a cada retentativa (chat "pulando").
        conn.execute("ALTER TABLE chats ADD COLUMN moved_at TEXT")


def option_questions(conn, option_id: int | None) -> list[dict]:
    """Perguntas de uma opcao ask_and_transfer, no formato do wizard (key/type/prompt/choices)."""
    if not option_id:
        return []
    rows = conn.execute(
        """SELECT sort_order, prompt, kind, choices FROM menu_option_questions
           WHERE option_id = ? ORDER BY sort_order, id""",
        (int(option_id),),
    ).fetchall()
    fields: list[dict] = []
    for i, r in enumerate(rows, start=1):
        kind = str(r.get("kind") or "text")
        item = {"key": "q%d" % i, "type": kind, "prompt": str(r.get("prompt") or "")}
        if kind == "choice":
            raw = str(r.get("choices") or "")
            item["choices"] = [c.strip() for c in raw.split("|") if c.strip()]
        fields.append(item)
    return fields


def _migrate_queue(conn) -> None:
    """Fila de espera (Fase 2): ultima posicao avisada e o encerramento manual.

    A espera **nao tem prazo** (nao existe mais `queue_max_wait_sec`): o
    `expires_at` continua na tabela apenas como marcador do botao "Encerrar
    espera" do painel. A chave antiga e removida aqui para nao voltar a aparecer
    como ajuste na aba Roteamento.
    """
    if "pending_routes" not in _table_names(conn):
        return
    cols = _table_columns(conn, "pending_routes")
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE pending_routes ADD COLUMN expires_at TEXT")
    if "last_position" not in cols:
        conn.execute("ALTER TABLE pending_routes ADD COLUMN last_position INTEGER NOT NULL DEFAULT 0")
    if "bot_settings" in _table_names(conn):
        conn.execute("DELETE FROM bot_settings WHERE key = 'queue_max_wait_sec'")
    # Rotas que ja estavam esperando com prazo automatico perdem o vencimento (o
    # pedido MANUAL do painel grava `expires_at` no passado e e preservado).
    conn.execute(
        "UPDATE pending_routes SET expires_at = NULL"
        " WHERE status = 'waiting' AND expires_at IS NOT NULL AND expires_at > datetime('now')"
    )


def _migrate_queue_priority(conn) -> None:
    """Priorizacao manual da fila (painel): fura a fila da propria linha/time.

    Sem isso a fila e estritamente FIFO por `id` — o atendente so pode encerrar
    a espera (aplica o fallback), nunca adiantar um item especifico.
    Espelho de php/bootstrap.php::bot_migrate_queue_priority.
    """
    if "pending_routes" not in _table_names(conn):
        return
    if "priority" not in _table_columns(conn, "pending_routes"):
        conn.execute("ALTER TABLE pending_routes ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")


def _migrate_bot_events_chat_idx(conn) -> None:
    """Indice p/ resolver o ultimo evento de um chat (autor/nome) sem table scan.

    Usado pela tabela "Fila de chat" do painel Dashboard (PHP), que roda a cada
    auto-refresh. Espelho de php/bootstrap.php::bot_migrate_bot_events_chat_idx.
    """
    if "bot_events" not in _table_names(conn):
        return
    conn.execute("CREATE INDEX IF NOT EXISTS bot_events_chat_idx ON bot_events (chat_id, id)")


def _migrate_delivery(conn) -> None:
    """Entrega robusta: identidade do caso, marcas da entrega e claim atomico.

    O indice unico parcial e criado aqui (e nao no schema.sql) porque bancos
    antigos podem ter duas rotas ativas para o mesmo chat: saneia antes de criar.
    """
    if "pending_routes" not in _table_names(conn):
        return
    cols = _table_columns(conn, "pending_routes")
    for name, ddl in (
        ("case_key", "case_key TEXT NOT NULL DEFAULT ''"),
        ("requester_addr", "requester_addr TEXT NOT NULL DEFAULT ''"),
        ("invite_target", "invite_target TEXT NOT NULL DEFAULT ''"),
        ("move_sent_at", "move_sent_at TEXT"),
        # `move_chat` recusado pelo TOA (e=10 quando o formulario ja esta com
        # alguem): conta as falhas para a rota parar em vez de girar para sempre.
        ("move_fails", "move_fails INTEGER NOT NULL DEFAULT 0"),
        ("claimed_by", "claimed_by TEXT NOT NULL DEFAULT ''"),
        ("claimed_at", "claimed_at TEXT"),
        ("claim_expires_at", "claim_expires_at TEXT"),
        # Resgate por analista offline: alvo que nao pode receber o caso de volta.
        ("exclude_assignee", "exclude_assignee TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE pending_routes ADD COLUMN {ddl}")
    if "route_assignments" in _table_names(conn) and "close_reason" not in _table_columns(conn, "route_assignments"):
        conn.execute("ALTER TABLE route_assignments ADD COLUMN close_reason TEXT NOT NULL DEFAULT ''")
    # Saneamento: mantem a rota ativa mais antiga por (kind, chat_id).
    # O `EXISTS` antigo lia a propria `pending_routes` de dentro do UPDATE, o que
    # o SQLite aceita e o MySQL recusa (erro 1093, "target table ... in FROM
    # clause"): a tabela derivada `rotas_ativas` e materializada antes do UPDATE
    # nos dois motores, e o `MIN(id)` por grupo deixa uma so rota ativa.
    conn.execute(
        """UPDATE pending_routes SET status = 'cancelled',
                  detail = 'duplicata saneada na migracao'
            WHERE status NOT IN ('done', 'failed', 'cancelled')
              AND id NOT IN (SELECT id FROM (
                    SELECT MIN(id) AS id FROM pending_routes
                     WHERE status NOT IN ('done', 'failed', 'cancelled')
                     GROUP BY kind, chat_id) AS rotas_ativas)"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS pending_routes_active
             ON pending_routes (kind, chat_id)
           WHERE status NOT IN ('done', 'failed', 'cancelled')"""
    )


# Textos da abertura form-aware REMOVIDA (28/09): a migracao apaga estes slugs
# porque a oferta "1) falar sobre o formulario / 2) abrir o menu" nao existe mais.
# Sao as mesmas chaves usadas por `db._migrate_form_aware` e `bot_migrate_form_aware`.
_FORM_AWARE_TEMPLATE_SLUGS = (
    "form_pending",
    "form_pending_wait",
    "form_waiting",
)


def _migrate_case_keys(conn) -> None:
    """Repara chaves de caso genericas do formulario (o "balde" de identidade).

    Formulario que chegou sem o address do tecnico virava o MESMO caso de todo
    mundo (`n:message scenario agent`), e a afinidade entregava tudo ao mesmo
    analista. Aqui a chave desses casos passa a ser por formulario
    (`chat:<id>`): o caso continua aberto, so deixa de ser compartilhado.
    Idempotente: so mexe em chave generica/vazia.
    """
    from . import form_open

    if "route_assignments" not in _table_names(conn):
        return

    def _generica(chave) -> bool:
        k = str(chave or "").strip().lower()
        if not k:
            return True
        return bool(k.startswith("n:")) and form_open.nome_generico(k[2:])

    alvos = (
        ("route_assignments", " AND closed_at IS NULL", "id"),
        ("pending_routes", " AND status = 'waiting'", "id"),
    )
    for tabela, filtro, pk in alvos:
        if tabela not in _table_names(conn):
            continue
        if "case_key" not in _table_columns(conn, tabela):
            continue
        rows = conn.execute(
            f"SELECT {pk} AS pk, chat_id, case_key FROM {tabela} WHERE 1 = 1{filtro}"
        ).fetchall()
        for r in rows:
            if not _generica(r.get("case_key")):
                continue
            conn.execute(
                f"UPDATE {tabela} SET case_key = ? WHERE {pk} = ?",
                ("chat:%d" % int(r["chat_id"]), int(r["pk"])),
            )
        # Dialeto legado: chave de address SEM o prefixo `a:` (`u:...`). A
        # afinidade compara string exata, entao essa chave nao casava com a do
        # formulario/chat novo do mesmo tecnico (virava caso duplicado, entregue
        # a outro analista). Idempotente.
        conn.execute(
            f"UPDATE {tabela} SET case_key = 'a:' || case_key"
            f" WHERE case_key LIKE 'u:%'{filtro}"
        )


def _migrate_kinds(conn) -> None:
    """Tipo de atendimento por operador (chat / form / ambos) e padrao do time.

    `team_member_routing.forms_enabled` continua existindo como espelho: a
    migracao converte o `0` (so chat) para `kinds='chat'` e deixa o resto em
    `both` (padrao = atende os dois, como sempre foi).
    """
    tabelas = _table_names(conn)
    if "team_routing" in tabelas and "member_kinds" not in _table_columns(conn, "team_routing"):
        conn.execute("ALTER TABLE team_routing ADD COLUMN member_kinds TEXT")
    if "team_routing" in tabelas and "offline_requeue_sec" not in _table_columns(conn, "team_routing"):
        conn.execute("ALTER TABLE team_routing ADD COLUMN offline_requeue_sec INTEGER")
    if "team_member_routing" in tabelas and "kinds" not in _table_columns(conn, "team_member_routing"):
        conn.execute(
            "ALTER TABLE team_member_routing ADD COLUMN kinds TEXT NOT NULL DEFAULT 'both'"
        )
    if "team_member_routing" in tabelas:
        conn.execute(
            "UPDATE team_member_routing SET kinds = 'chat'"
            " WHERE kinds = 'both' AND forms_enabled = 0"
        )


def _migrate_team_schedules(conn) -> None:
    """Janelas de atuacao (turno) e descanso por analista/time.

    Base antiga nao ganha a tabela com o CREATE IF NOT EXISTS do schema.sql
    rodar de novo? Ganha — a funcao existe para o mesmo padrao das demais
    migracoes (checagem explicita) e para o painel PHP espelhar a chamada, alem
    de fazer a migracao de dados abaixo.

    Dados: quem ja tinha `team_members.route_start/route_end` preenchidos e
    ainda nao tem nenhuma linha em `team_member_schedules` ganha uma linha
    `shift` (todos os dias) equivalente, para nao perder o plantao configurado
    na troca de release. Dali em diante a tabela nova e a fonte unica lida pelo
    motor; as colunas antigas ficam so como historico.
    """
    if "team_member_schedules" not in _table_names(conn):
        conn.execute(
            """CREATE TABLE team_member_schedules (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                 address VARCHAR(191),
                 kind TEXT NOT NULL DEFAULT 'shift' CHECK (kind IN ('shift', 'break')),
                 days_mask INTEGER NOT NULL DEFAULT 127,
                 start_time TEXT NOT NULL,
                 end_time TEXT NOT NULL,
                 sort_order INTEGER NOT NULL DEFAULT 0,
                 active INTEGER NOT NULL DEFAULT 1,
                 created_at TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
        )
        conn.execute(
            """CREATE INDEX team_member_schedules_lookup
               ON team_member_schedules (team_id, address, active)"""
        )
    if "team_members" not in _table_names(conn):
        return
    existentes = {
        (r["team_id"], str(r["address"] or ""))
        for r in conn.execute(
            "SELECT DISTINCT team_id, address FROM team_member_schedules WHERE address IS NOT NULL"
        ).fetchall()
    }
    legados = conn.execute(
        "SELECT team_id, address, route_start, route_end FROM team_members"
        " WHERE route_start IS NOT NULL AND route_start != ''"
        " AND route_end IS NOT NULL AND route_end != ''"
    ).fetchall()
    for row in legados:
        key = (row["team_id"], str(row["address"] or ""))
        if key in existentes:
            continue
        conn.execute(
            """INSERT INTO team_member_schedules
                 (team_id, address, kind, days_mask, start_time, end_time)
               VALUES (?, ?, 'shift', 127, ?, ?)""",
            (row["team_id"], row["address"], row["route_start"], row["route_end"]),
        )


def _migrate_form_window(conn) -> None:
    """Margem de fim de janela do formulario (por time; NULL = herda o global).

    O formulario deixa de ser OFERECIDO N minutos antes do fim da escala do
    operador (`team_members.route_end`): entregar em cima da saida prendia o caso
    com quem ja tinha ido embora. O valor global fica em `bot_settings`
    (`form_window_margin_min`, padrao 10) e o time sobrescreve se quiser.

    Aqui tambem sai a chave MORTA `affinity_require_online`: a afinidade deixou de
    ser excecao ao online, entao o ajuste antigo e removido para nao parecer que
    ainda vale (mesma convencao do `queue_max_wait_sec`).
    """
    if "team_routing" in _table_names(conn) and "form_window_margin_min" not in _table_columns(conn, "team_routing"):
        conn.execute("ALTER TABLE team_routing ADD COLUMN form_window_margin_min INTEGER")
    if "bot_settings" in _table_names(conn):
        conn.execute("DELETE FROM bot_settings WHERE key = 'affinity_require_online'")


def _migrate_load(conn) -> None:
    """Carga por analista (medicao): estado atual + historico curto.

    O bot le `get_helpdesk_chats(in_process)` por fila (mesma fonte da capacidade)
    e guarda por analista: chats/formularios vistos no TOA, o que o ledger conta,
    reservas e a ociosidade do formulario mais antigo — e e essa leitura que
    permite fechar por AUSENCIA o formulario ja resolvido no TOA.
    """
    if "analyst_load" not in _table_names(conn):
        conn.execute(
            """CREATE TABLE analyst_load (
                 address TEXT PRIMARY KEY,
                 at TEXT NOT NULL DEFAULT (datetime('now')),
                 name TEXT NOT NULL DEFAULT '',
                 team_id INTEGER,
                 janela TEXT NOT NULL DEFAULT '',
                 online INTEGER NOT NULL DEFAULT 0,
                 chats_toa INTEGER NOT NULL DEFAULT 0,
                 forms_toa INTEGER NOT NULL DEFAULT 0,
                 chats_ledger INTEGER NOT NULL DEFAULT 0,
                 forms_ledger INTEGER NOT NULL DEFAULT 0,
                 reservas INTEGER NOT NULL DEFAULT 0,
                 casos_total INTEGER NOT NULL DEFAULT 0,
                 form_ocioso_min REAL NOT NULL DEFAULT 0,
                 form_presente_toa INTEGER NOT NULL DEFAULT 0,
                 toa_ok INTEGER NOT NULL DEFAULT 0,
                 -- Chaves de caso (autor) vistas no TOA nessa leitura: a decisao
                 -- usa isso como a CARGA FISICA do analista (todas as filas do
                 -- time), em vez de depender so da fila do item.
                 casos_toa_keys TEXT NOT NULL DEFAULT '',
                 detail TEXT NOT NULL DEFAULT ''
               )"""
        )
    if "analyst_load_history" not in _table_names(conn):
        conn.execute(
            """CREATE TABLE analyst_load_history (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 at VARCHAR(191) NOT NULL DEFAULT (datetime('now')),
                 address TEXT NOT NULL,
                 chats_toa INTEGER NOT NULL DEFAULT 0,
                 forms_toa INTEGER NOT NULL DEFAULT 0,
                 chats_ledger INTEGER NOT NULL DEFAULT 0,
                 forms_ledger INTEGER NOT NULL DEFAULT 0,
                 casos_total INTEGER NOT NULL DEFAULT 0,
                 toa_ok INTEGER NOT NULL DEFAULT 0
               )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS analyst_load_history_at ON analyst_load_history (at DESC)"
        )
    if "analyst_load" in _table_names(conn) and "casos_toa_keys" not in _table_columns(conn, "analyst_load"):
        conn.execute(
            "ALTER TABLE analyst_load ADD COLUMN casos_toa_keys TEXT NOT NULL DEFAULT ''"
        )
    # Presenca persistida (resgate por analista offline): `online_ok` separa
    # "leitura falhou" de "esta offline" e `offline_since` carimba o comeco.
    if "analyst_load" in _table_names(conn):
        cols = _table_columns(conn, "analyst_load")
        if "online_ok" not in cols:
            conn.execute("ALTER TABLE analyst_load ADD COLUMN online_ok INTEGER NOT NULL DEFAULT 0")
        if "offline_since" not in cols:
            conn.execute("ALTER TABLE analyst_load ADD COLUMN offline_since TEXT")


def load_reading(conn, address: str, fresh_sec: float = 90.0) -> dict | None:
    """Ultima leitura de carga do analista — so se recente E bem sucedida.

    Serve para a decisao usar a carga FISICA (o que o analista tem aberto no TOA,
    somado a todas as filas do time). Leitura velha ou com `toa_ok=0` (uma das
    listas falhou) nao decide nada: o chamador cai no calculo por fila.
    """
    row = conn.execute(
        "SELECT *, (julianday('now') - julianday(at)) * 86400 AS idade"
        " FROM analyst_load WHERE address = ?",
        (str(address),),
    ).fetchone()
    if not row or not int(row.get("toa_ok") or 0):
        return None
    if float(row.get("idade") or 0) > float(fresh_sec):
        return None
    return row


def online_reading(conn, address: str, fresh_sec: float = 90.0) -> dict | None:
    """Ultima leitura de PRESENCA do analista — so se recente E bem sucedida.

    Mesmo padrao do `load_reading` (carga), mas o gate aqui e `online_ok`: diz
    se a leitura de `get_online_users` respondeu nesta volta — nao `toa_ok`,
    que e sobre as listas de chat (outra chamada ao TOA). Confundir os dois
    fazia a elegibilidade "online" (routing) nunca reaproveitar o cache da
    aba Carga e depender de uma chamada ao vivo em toda retentativa.
    """
    row = conn.execute(
        "SELECT *, (julianday('now') - julianday(at)) * 86400 AS idade"
        " FROM analyst_load WHERE address = ?",
        (str(address),),
    ).fetchone()
    if not row or not int(row.get("online_ok") or 0):
        return None
    if float(row.get("idade") or 0) > float(fresh_sec):
        return None
    return row


def load_case_keys(row: dict | None) -> set[str]:
    """Chaves de caso (autor) que a ultima leitura viu no TOA para o analista."""
    if not row:
        return set()
    bruto = str(row.get("casos_toa_keys") or "")
    if not bruto:
        return set()
    try:
        dados = json.loads(bruto)
    except ValueError:
        return set()
    return {str(x) for x in dados if x} if isinstance(dados, list) else set()


def load_upsert(conn, address: str, **campos) -> None:
    """Grava a leitura atual do analista (uma linha por endereco)."""
    base = {
        "name": "", "team_id": None, "janela": "", "online": 0, "online_ok": 0, "chats_toa": 0, "forms_toa": 0,
        "chats_ledger": 0, "forms_ledger": 0, "reservas": 0, "casos_total": 0,
        "form_ocioso_min": 0.0, "form_presente_toa": 0, "toa_ok": 0, "casos_toa_keys": "",
        "detail": "",
    }
    base.update({k: v for k, v in campos.items() if k in base})
    conn.execute(
        f"""INSERT INTO analyst_load
              (address, at, {', '.join(base)})
            VALUES (?, datetime('now'), {', '.join('?' * len(base))})
            ON CONFLICT (address) DO UPDATE SET
              at = datetime('now'),
              {', '.join(f'{k} = excluded.{k}' for k in base)}""",
        [str(address)] + list(base.values()),
    )


def set_presence(conn, address: str, *, online: bool, online_ok: bool) -> None:
    """Carimba a presenca do analista (base do resgate por ausencia).

    `online_ok=False` = a leitura do `get_online_users` falhou: nao da para
    afirmar que o analista esta offline, entao o carimbo nao e criado nem
    apagado (o resgate exige `online_ok=1`). Com leitura boa, o carimbo nasce na
    PRIMEIRA vez que ele aparece offline e e limpo quando ele volta.
    """
    if not online_ok:
        conn.execute(
            "UPDATE analyst_load SET online = 0, online_ok = 0 WHERE address = ?",
            (str(address),),
        )
        return
    if online:
        conn.execute(
            "UPDATE analyst_load SET online = 1, online_ok = 1, offline_since = NULL WHERE address = ?",
            (str(address),),
        )
        return
    conn.execute(
        "UPDATE analyst_load SET online = 0, online_ok = 1,"
        " offline_since = COALESCE(offline_since, datetime('now')) WHERE address = ?",
        (str(address),),
    )


def offline_open_assignments(conn, min_offline_sec: float) -> list[dict]:
    """Casos ABERTOS no ledger cujo analista esta offline ha `min_offline_sec`.

    Exige leitura de presenca BOA (`online_ok=1`): queda do TOA nao devolve caso
    de ninguem. Ordem do ledger (FIFO) para o resgate respeitar a vez.
    """
    return conn.execute(
        """SELECT a.*, l.offline_since, l.at AS load_at
             FROM route_assignments a
             JOIN analyst_load l ON l.address = a.assignee
            WHERE a.closed_at IS NULL
              AND a.assignee <> ''
              AND l.online_ok = 1
              AND l.online = 0
              AND l.offline_since IS NOT NULL
              AND datetime(l.offline_since) <= datetime('now', '-' || ? || ' seconds')
            ORDER BY a.id""",
        (str(int(max(min_offline_sec, 0))),),
    ).fetchall()


def load_trim_history(conn, keep: int = 200) -> int:
    """Mantem so as `keep` leituras mais recentes do historico.

    A subconsulta virou tabela derivada: ler a propria tabela do `DELETE` e
    valido no SQLite e erro 1093 no MySQL.
    """
    cur = conn.execute(
        """DELETE FROM analyst_load_history
            WHERE id NOT IN (SELECT id FROM (
                  SELECT id FROM analyst_load_history
                   ORDER BY id DESC LIMIT ?) AS recentes)""",
        (int(max(keep, 1)),),
    )
    return int(cur.rowcount or 0)


def queue_load_upsert(conn, queue_address: str, **campos) -> None:
    """Grava a leitura da fila no TOA (uma linha por endereco de fila)."""
    base = {"pending_forms": 0, "pending_chats": 0, "toa_ok": 0, "detail": ""}
    base.update({k: v for k, v in campos.items() if k in base})
    conn.execute(
        f"""INSERT INTO queue_load
              (queue_address, at, {', '.join(base)})
            VALUES (?, datetime('now'), {', '.join('?' * len(base))})
            ON CONFLICT (queue_address) DO UPDATE SET
              at = datetime('now'),
              {', '.join(f'{k} = excluded.{k}' for k in base)}""",
        [str(queue_address)] + list(base.values()),
    )


def queue_load_frescas(conn, fresh_sec: float = 90.0) -> list[dict]:
    """Leituras de fila ainda frescas (2 ciclos do snapshot, minimo 60 s).

    Leitura velha ou com `toa_ok=0` (a lista `pending` nao respondeu) nao decide
    nada: o chamador mostra "sem coleta recente" em vez de um numero mentiroso.
    """
    return conn.execute(
        "SELECT *, (julianday('now') - julianday(at)) * 86400 AS idade"
        " FROM queue_load"
        " WHERE toa_ok = 1 AND (julianday('now') - julianday(at)) * 86400 <= ?",
        (float(fresh_sec),),
    ).fetchall()


def load_add_history(conn, address: str, **campos) -> None:
    """Uma linha por analista por rodada de leitura."""
    conn.execute(
        """INSERT INTO analyst_load_history
             (address, chats_toa, forms_toa, chats_ledger, forms_ledger, casos_total, toa_ok)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            str(address),
            int(campos.get("chats_toa") or 0),
            int(campos.get("forms_toa") or 0),
            int(campos.get("chats_ledger") or 0),
            int(campos.get("forms_ledger") or 0),
            int(campos.get("casos_total") or 0),
            1 if campos.get("toa_ok") else 0,
        ),
    )


def load_rows(conn) -> list[dict]:
    """Leitura atual de todos os analistas (a aba Carga le daqui)."""
    return conn.execute("SELECT * FROM analyst_load ORDER BY casos_total DESC, name").fetchall()


def load_history(conn, address: str | None = None, limit: int = 60) -> list[dict]:
    """Historico recente (por analista ou geral)."""
    if address:
        return conn.execute(
            """SELECT * FROM analyst_load_history WHERE address = ?
                ORDER BY id DESC LIMIT ?""",
            (str(address), int(limit)),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM analyst_load_history ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()


def _migrate_form_aware(conn) -> None:
    """Colunas legadas do vinculo chat<->formulario + limpeza dos textos.

    A abertura form-aware foi **removida** (28/09): o chat de fila sempre recebe o
    menu. As colunas ficam (bancos antigos ja as tem e o painel le
    `linked_form_chat_id`) e os textos da oferta saem do banco, porque nao tem
    mais uso.
    """
    if "chats" in _table_names(conn):
        cols = _table_columns(conn, "chats")
        if "linked_form_chat_id" not in cols:
            conn.execute("ALTER TABLE chats ADD COLUMN linked_form_chat_id INTEGER")
    if "pending_routes" in _table_names(conn):
        cols = _table_columns(conn, "pending_routes")
        if "expects_owner" not in cols:
            conn.execute("ALTER TABLE pending_routes ADD COLUMN expects_owner INTEGER NOT NULL DEFAULT 0")
    if "templates" in _table_names(conn):
        for slug in _FORM_AWARE_TEMPLATE_SLUGS:
            conn.execute("DELETE FROM templates WHERE slug = ?", (slug,))


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def upsert_queue(conn, address: str, name: str) -> None:
    conn.execute(
        """INSERT INTO queues (address, name) VALUES (?, ?)
           ON CONFLICT (address) DO UPDATE
           SET name = COALESCE(NULLIF(excluded.name, ''), queues.name),
               last_seen = datetime('now')""",
        (address, name or ""),
    )


def enabled_queues(conn) -> set[str]:
    rows = conn.execute("SELECT address FROM queues WHERE enabled = 1").fetchall()
    return {r["address"] for r in rows}


def settings(conn) -> dict:
    rows = conn.execute("SELECT key, value FROM bot_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_setting(conn, key: str, value) -> None:
    """Grava um ajuste do bot (mesma tabela/forma que o painel usa)."""
    conn.execute(
        """INSERT INTO bot_settings (key, value) VALUES (?, ?)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (str(key), "" if value is None else str(value)),
    )


def templates(conn) -> dict:
    rows = conn.execute("SELECT slug, body FROM templates").fetchall()
    return {r["slug"]: r["body"] for r in rows}


def menu_option(conn, option_id: int | None) -> dict | None:
    if not option_id:
        return None
    return conn.execute("SELECT * FROM menu_options WHERE id = ?", (int(option_id),)).fetchone()


def queue_menu_options(conn, queue_address: str | None) -> list[dict]:
    """Opções-RAIZ do menu ATIVO da fila — sem os fallbacks globais.

    `menu_options` cai num menu global (opções sem `menu_id`/`queue_address`)
    quando a fila não tem menu: bom para não deixar o técnico sem opção, mas não
    serve para responder "esta fila TEM menu ativo?". Este é o teste usado no
    portão de entrada do bot (fila habilitada sem menu ativo não é assumida) e na
    trava da aba Filas.
    """
    if not queue_address:
        return []
    rows = conn.execute(
        """SELECT o.*
           FROM menu_options o
           JOIN menus m ON m.id = o.menu_id
           JOIN menu_queues mq ON mq.menu_id = m.id
           WHERE mq.queue_address = ? AND m.active = 1 AND o.parent_id IS NULL
           ORDER BY o.sort_order""",
        (queue_address,),
    ).fetchall()
    if rows:
        return rows
    return conn.execute(
        """SELECT * FROM menu_options
           WHERE queue_address = ? AND parent_id IS NULL
           ORDER BY sort_order""",
        (queue_address,),
    ).fetchall()


def menu_options(conn, queue_address: str | None, parent_id: int | None = None) -> list[dict]:
    if parent_id is not None:
        return conn.execute(
            "SELECT * FROM menu_options WHERE parent_id = ? ORDER BY sort_order",
            (int(parent_id),),
        ).fetchall()
    if queue_address:
        rows = conn.execute(
            """SELECT o.*
               FROM menu_options o
               JOIN menus m ON m.id = o.menu_id
               JOIN menu_queues mq ON mq.menu_id = m.id
               WHERE mq.queue_address = ? AND m.active = 1 AND o.parent_id IS NULL
               ORDER BY o.sort_order""",
            (queue_address,),
        ).fetchall()
        if rows:
            return rows
        own = conn.execute(
            """SELECT * FROM menu_options
               WHERE queue_address = ? AND parent_id IS NULL
               ORDER BY sort_order""",
            (queue_address,),
        ).fetchall()
        if own:
            return own
    return conn.execute(
        """SELECT o.*
           FROM menu_options o
           JOIN menus m ON m.id = o.menu_id
           WHERE m.active = 1 AND o.menu_id IS NOT NULL
             AND o.parent_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM menu_queues mq WHERE mq.menu_id = m.id)
             AND o.queue_address IS NULL
           ORDER BY o.sort_order"""
    ).fetchall() or conn.execute(
        """SELECT * FROM menu_options
           WHERE queue_address IS NULL AND parent_id IS NULL
           ORDER BY sort_order"""
    ).fetchall()


def team_for_queue(conn, queue_address: str | None) -> dict | None:
    if not queue_address:
        return None
    return conn.execute(
        """SELECT t.*
           FROM teams t
           JOIN team_queues tq ON tq.team_id = t.id
           WHERE tq.queue_address = ? AND t.active = 1""",
        (queue_address,),
    ).fetchone()


def team_members(conn, team_id: int) -> list[dict]:
    return conn.execute(
        """SELECT * FROM team_members
           WHERE team_id = ? AND active = 1
           ORDER BY sort_order, name, address""",
        (team_id,),
    ).fetchall()


def set_team_last_assigned(conn, team_id: int, address: str) -> None:
    conn.execute(
        "UPDATE teams SET last_assigned = ? WHERE id = ?",
        (address, team_id),
    )


def team_routing(conn, team_id: int) -> dict | None:
    return conn.execute(
        "SELECT * FROM team_routing WHERE team_id = ?", (int(team_id),)
    ).fetchone()


def team_member_routing(conn, team_id: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT * FROM team_member_routing WHERE team_id = ?", (int(team_id),)
    ).fetchall()
    return {str(r["address"]): r for r in rows}


def team_schedules(conn, team_id: int) -> dict[str, list[dict]]:
    """Janelas (turno/descanso) do time, agrupadas por analista.

    Chave `""` = default do time (`address IS NULL`); demais chaves = override
    total do analista. Veja `routing.resolve_member_schedule` para a herança.
    """
    rows = conn.execute(
        """SELECT * FROM team_member_schedules
           WHERE team_id = ? AND active = 1
           ORDER BY sort_order, id""",
        (int(team_id),),
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r["address"] or ""), []).append(r)
    return out


def replace_schedules(conn, team_id: int, address: str, rows: list[dict]) -> None:
    """Substitui por completo as janelas de um escopo (time ou analista).

    `address` vazio/None grava o default do time (`address IS NULL`).
    """
    addr = str(address or "").strip()
    if addr:
        conn.execute(
            "DELETE FROM team_member_schedules WHERE team_id = ? AND address = ?",
            (int(team_id), addr),
        )
    else:
        conn.execute(
            "DELETE FROM team_member_schedules WHERE team_id = ? AND address IS NULL",
            (int(team_id),),
        )
    for i, row in enumerate(rows or []):
        kind = str(row.get("kind") or "shift").strip().lower()
        if kind not in ("shift", "break"):
            kind = "shift"
        start = str(row.get("start_time") or "").strip()
        end = str(row.get("end_time") or "").strip()
        if not start or not end:
            continue
        conn.execute(
            """INSERT INTO team_member_schedules
                 (team_id, address, kind, days_mask, start_time, end_time, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                int(team_id),
                addr or None,
                kind,
                int(row.get("days_mask") or 127),
                start,
                end,
                i,
            ),
        )


def clear_member_schedules(conn, team_id: int, address: str) -> None:
    """Remove as janelas proprias do analista: ele volta a herdar o default do time."""
    conn.execute(
        "DELETE FROM team_member_schedules WHERE team_id = ? AND address = ?",
        (int(team_id), str(address or "").strip()),
    )


def set_last_assigned(conn, team_id: int, kind: str, address: str) -> None:
    """Rodízio separado por tipo de tráfego, sem perder o legado teams.last_assigned."""
    col = "last_assigned_form" if str(kind) == "form" else "last_assigned_chat"
    conn.execute(
        f"""INSERT INTO team_routing (team_id, {col}) VALUES (?, ?)
            ON CONFLICT (team_id) DO UPDATE
            SET {col} = excluded.{col}, updated_at = datetime('now')""",
        (int(team_id), address or ""),
    )
    if str(kind) != "form":
        set_team_last_assigned(conn, int(team_id), address)


def upsert_queue_operator(conn, queue_address: str, address: str, name: str, source: str = "toa") -> None:
    conn.execute(
        """INSERT INTO queue_operators (queue_address, address, name, source) VALUES (?, ?, ?, ?)
           ON CONFLICT (queue_address, address) DO UPDATE
           SET name = COALESCE(NULLIF(excluded.name, ''), queue_operators.name),
               source = CASE WHEN queue_operators.source = 'manual' THEN 'manual' ELSE excluded.source END""",
        (queue_address, address, name or "", source or "toa"),
    )


def ingest_search_contact(raw: dict) -> tuple[int, int]:
    """Grava filas/operadores em lote numa conexão própria (thread-safe)."""
    queues: list[tuple[str, str]] = []
    operators: list[tuple[str, str, str]] = []
    seen_ops: set[str] = set()
    seen_q: set[str] = set()
    for qa, qd in raw.items():
        qa = str(qa or "")
        if not qa.startswith("h:") or not isinstance(qd, dict):
            continue
        if qa not in seen_q:
            queues.append((qa, qd.get("name") or ""))
            seen_q.add(qa)
        for op in qd.get("operators") or []:
            if not isinstance(op, dict):
                continue
            addr = str(op.get("address") or "")
            if not addr.startswith("u:"):
                continue
            operators.append((qa, addr, op.get("name") or "", "toa"))
            seen_ops.add(addr)
    conn = connect()
    try:
        if queues:
            conn.executemany(
                """INSERT INTO queues (address, name) VALUES (?, ?)
                   ON CONFLICT (address) DO UPDATE
                   SET name = COALESCE(NULLIF(excluded.name, ''), queues.name),
                       last_seen = datetime('now')""",
                queues,
            )
        if operators:
            conn.executemany(
                """INSERT INTO queue_operators (queue_address, address, name, source) VALUES (?, ?, ?, ?)
                   ON CONFLICT (queue_address, address) DO UPDATE
                   SET name = COALESCE(NULLIF(excluded.name, ''), queue_operators.name),
                       source = CASE WHEN queue_operators.source = 'manual' THEN 'manual' ELSE excluded.source END""",
                operators,
            )
    finally:
        conn.close()
    return len(seen_ops), len(seen_q)


def transfer_target(conn, tid: int | None) -> dict | None:
    if not tid:
        return None
    return conn.execute(
        "SELECT * FROM transfer_targets WHERE id = ? AND active = 1", (tid,)
    ).fetchone()


def option_targets(conn, option_id: int | None) -> list[dict]:
    if not option_id:
        return []
    rows = conn.execute(
        "SELECT address, name FROM menu_option_targets WHERE option_id = ? ORDER BY name, address",
        (option_id,),
    ).fetchall()
    if rows:
        return rows
    opt = conn.execute(
        "SELECT transfer_target_id FROM menu_options WHERE id = ?",
        (option_id,),
    ).fetchone()
    if not opt or not opt.get("transfer_target_id"):
        return []
    tgt = transfer_target(conn, opt["transfer_target_id"])
    if not tgt:
        return []
    addr = str(tgt.get("address") or "")
    if not addr.startswith("u:"):
        return []
    return [{"address": addr, "name": tgt.get("name") or ""}]


def lock_chat(conn, chat_id: int) -> dict | None:
    return conn.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()


def set_linked_form(conn, chat_id: int, form_chat_id: int | None) -> None:
    """UPDATE puro (como `mark_chat_moved`): `upsert_chat` monta INSERT e a tabela
    `chats` exige `status`, entao nao serve para gravar so o vinculo do formulario."""
    conn.execute(
        "UPDATE chats SET linked_form_chat_id = ?, updated_at = datetime('now') WHERE chat_id = ?",
        (None if form_chat_id is None else int(form_chat_id), int(chat_id)),
    )


def upsert_chat(conn, chat_id: int, **fields) -> None:
    cols = ["chat_id"] + list(fields)
    vals = [chat_id] + list(fields.values())
    placeholders = ", ".join(["?"] * len(vals))
    assignments = ", ".join(f"{k} = excluded.{k}" for k in fields)
    conn.execute(
        f"""INSERT INTO chats ({", ".join(cols)}, updated_at)
            VALUES ({placeholders}, datetime('now'))
            ON CONFLICT (chat_id) DO UPDATE SET {assignments}, updated_at = datetime('now')""",
        vals,
    )


def mark_reply(conn, chat_id: int, when=None) -> None:
    """Marca a atividade do solicitante (usado pelo timeout por silencio).

    UPDATE puro de proposito: `upsert_chat` monta INSERT e a tabela `chats` exige
    `status`, entao a linha tem de existir (existe: o timeout le a linha antes).
    """
    conn.execute(
        "UPDATE chats SET last_reply_at = ?, updated_at = datetime('now') WHERE chat_id = ?",
        ((when or datetime.now(timezone.utc)), int(chat_id)),
    )


def awaiting_timeouts(conn, seconds: int | None = None) -> list[dict]:
    """Chats esperando resposta do tecnico.

    Sem `seconds` devolve so os candidatos: o filtro fino do silencio (que
    considera `last_reply_at`) fica no engine, onde a data e interpretada.
    """
    sql = """SELECT * FROM chats
             WHERE status IN ('awaiting_reply', 'awaiting_script_answer',
                              'awaiting_script_confirm')
               AND last_prompt_at IS NOT NULL"""
    args: list = []
    if seconds is not None:
        sql += " AND datetime(last_prompt_at) < datetime('now', '-' || ? || ' seconds')"
        args.append(str(int(seconds)))
    return conn.execute(sql, args).fetchall()


def resume_chats(conn) -> list[dict]:
    return conn.execute(
        "SELECT * FROM chats WHERE status IN ('joined', 'menu_sent', 'awaiting_reply', 'classified', 'monitoring', 'awaiting_script_answer', 'awaiting_script_confirm', 'awaiting_script_run')"
    ).fetchall()


def script_run_timeouts(conn, seconds: int = 60) -> list[dict]:
    return conn.execute(
        """SELECT * FROM chats
           WHERE status = 'awaiting_script_run'
             AND script_started_at IS NOT NULL
             AND datetime(script_started_at) < datetime('now', '-' || ? || ' seconds')""",
        (str(seconds),),
    ).fetchall()


def record_message(
    conn,
    chat_id: int,
    kind: str,
    direction: str,
    dedupe_key: str,
    *,
    queue_address: str = "",
    message_id: str = "",
    author_addr: str = "",
    author_name: str = "",
    is_bot: bool = False,
    system_message: str = "",
    text: str = "",
    has_attachment: bool = False,
    attachment_name: str = "",
    attachment_mime: str = "",
    attachment_size: int = 0,
    attachment_data: bytes | None = None,
    attachment_fetch_status: str = "",
    msg_time: str | None = None,
) -> None:
    """Grava uma linha de `chat_messages` (ver schema.sql) -- reproducao do chat no painel.

    Idempotente: `dedupe_key` e UNIQUE por `chat_id` e o insert usa
    `INSERT OR IGNORE`, entao o mesmo poll/replay nao duplica a linha. Nunca
    lanca para o chamador -- gravar a conversa e efeito colateral de
    `on_message`/`send`/`_refresh_form_state`, nao motivo deles existirem
    (mesmo espirito de `log_event`).
    """
    try:
        conn.execute(
            """INSERT OR IGNORE INTO chat_messages
                 (chat_id, kind, direction, queue_address, message_id, dedupe_key,
                  author_addr, author_name, is_bot, system_message, text,
                  has_attachment, attachment_name, attachment_mime, attachment_size,
                  attachment_data, attachment_fetch_status, msg_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(chat_id), kind, direction, str(queue_address or ""),
                str(message_id or ""), dedupe_key,
                str(author_addr or ""), str(author_name or ""), 1 if is_bot else 0,
                str(system_message or ""), str(text or ""),
                1 if has_attachment else 0, str(attachment_name or ""),
                str(attachment_mime or ""), int(attachment_size or 0),
                attachment_data, str(attachment_fetch_status or ""), msg_time,
            ),
        )
    except Exception:
        log.warning("chat_messages: insert falhou chat=%s", chat_id, exc_info=True)


# ---------------------------------------------------------------------- #
# Formulario: utilitarios PERMANENTES (dedup por activityId do TOA e leitura
# do texto gravado). Nao removiveis -- ao contrario da checagem do robo logo
# abaixo, isto nao depende da automacao de realocacao.
# ---------------------------------------------------------------------- #

_ACTIVITY_ID_RE = re.compile(r"activityId=(\d+)")


def form_messages_text(conn, chat_id: int) -> list[str]:
    """Textos ja gravados do formulario (ver `chat_messages`), para classificar.

    Nao filtra por `kind = 'form'`: uma corrida de gravacao (mesma mensagem
    processada por `_record_inbound_chat` antes de `_refresh_form_state`, ver
    `_message_dedupe_key`) pode deixar o conteudo do formulario gravado como
    `kind='chat'`. Como esta consulta ja e restrita ao `chat_id` dedicado da
    conversa do formulario, ler os dois `kind` aqui e seguro e evita cegar a
    classificacao por causa dessa corrida.
    """
    rows = conn.execute(
        "SELECT text FROM chat_messages WHERE chat_id = ?",
        (int(chat_id),),
    ).fetchall()
    return [str(r.get("text") or "") for r in rows]


def form_duplicate_of(conn, chat_id: int, desde_utc) -> tuple[int, int] | None:
    """Este formulario repete o activityId de um chat_id MAIS ANTIGO, dentro da
    janela (`desde_utc`)? Devolve `(chat_id_original, activity_id)` quando sim,
    senao `None` (sem activityId neste formulario, ou nenhum mais antigo com o
    mesmo id dentro da janela).

    O TOA as vezes reenvia o mesmo formulario de validacao mais de uma vez
    (mesmo activityId, conteudo identico) -- so o PRIMEIRO (menor chat_id)
    deve chegar a disputar vaga de analista. `desde_utc` existe porque o MESMO
    activityId pode ser reaproveitado pelo TOA dias depois para uma
    reatribuicao nova de verdade: sem o limite de tempo, um formulario
    legitimo seria descartado por engano so por reciclar o mesmo id.
    """
    textos = form_messages_text(conn, chat_id)
    activity_id = None
    for t in textos:
        m = _ACTIVITY_ID_RE.search(t or "")
        if m:
            activity_id = int(m.group(1))
            break
    if activity_id is None:
        return None
    # Mesmo motivo de `form_messages_text`: nao filtra por `kind = 'form'`
    # aqui (a corrida de gravacao pode ter deixado o formulario original como
    # `kind='chat'`), senao o candidato mais antigo nunca e encontrado.
    candidatos = conn.execute(
        """SELECT chat_id, text FROM chat_messages
            WHERE chat_id < ? AND at >= ?
            ORDER BY chat_id ASC""",
        (int(chat_id), desde_utc),
    ).fetchall()
    for row in candidatos:
        m = _ACTIVITY_ID_RE.search(str(row.get("text") or ""))
        if m and int(m.group(1)) == activity_id:
            return int(row["chat_id"]), activity_id
    return None


# ---------------------------------------------------------------------- #
# Checagem TEMPORARIA do formulario "TAREFA REALOCADA" (ver python/realocado.py
# e Engine._realocado_segurar). Remover junto com o resto da feature quando a
# ponte com o robo de realocacao nao for mais necessaria.
# ---------------------------------------------------------------------- #

def realocado_resolvido(conn, activity_id: int, desde_brt) -> bool:
    """O robo (outro schema, mesmo servidor) ja marcou sucesso para esta atividade?

    `desde_brt` e o inicio do formulario (convertido para horario de Brasilia,
    o relogio que a tabela `robo.toa__validacao_realocado` usa -- sem isso o
    filtro de data compara fusos diferentes e erra) MENOS a margem de
    tolerancia (ver `Engine._realocado_tolerancia_sec`): o robo as vezes
    resolve poucos segundos ANTES de um formulario duplicado chegar para o
    mesmo `activityId`, e sem essa margem esse sucesso real seria descartado.
    """
    row = conn.execute(
        """SELECT 1 FROM robo.toa__validacao_realocado
            WHERE activityId = ? AND sucesso = 1 AND criado_em > ?
            LIMIT 1""",
        (int(activity_id), desde_brt),
    ).fetchone()
    return row is not None


def log_event(
    conn,
    reason: str,
    chat_id: int | None = None,
    queue_address: str | None = None,
    queue_enabled: bool | None = None,
    detail: str = "",
    author_addr: str = "",
    author_name: str = "",
) -> None:
    enabled = None if queue_enabled is None else (1 if queue_enabled else 0)
    try:
        conn.execute(
            """INSERT INTO bot_events
                 (chat_id, queue_address, queue_enabled, reason, detail, author_addr, author_name)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, queue_address, enabled, reason, detail or "",
             str(author_addr or ""), str(author_name or "")),
        )
    except Exception:
        log.warning("bot_events: insert falhou", exc_info=True)


def request_catalog_sync(conn) -> int:
    cur = conn.execute("INSERT INTO bot_jobs (kind) VALUES ('catalog_sync')")
    return int(cur.lastrowid)


def claim_pending_job(conn) -> dict | None:
    with tx(conn):
        row = conn.execute(
            "SELECT * FROM bot_jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE bot_jobs SET status = 'running', started_at = datetime('now') WHERE id = ?",
            (int(row["id"]),),
        )
        row = dict(row)
        row["status"] = "running"
        return row


def finish_job(conn, job_id: int, status: str, detail: str = "") -> None:
    conn.execute(
        "UPDATE bot_jobs SET status = ?, detail = ?, finished_at = datetime('now') WHERE id = ?",
        (status, detail or "", job_id),
    )


def chat_moved(conn, chat_id: int) -> bool:
    """O `move_chat` desse chat ja foi feito (transferencia em andamento/parcial)?"""
    row = conn.execute(
        "SELECT moved_at FROM chats WHERE chat_id = ?", (int(chat_id),)
    ).fetchone()
    return bool(row and row.get("moved_at"))


def mark_chat_moved(conn, chat_id: int) -> None:
    """Grava a marca do move: a proxima tentativa convida sem arrastar o chat de novo."""
    conn.execute(
        "UPDATE chats SET moved_at = datetime('now'), updated_at = datetime('now')"
        " WHERE chat_id = ? AND (moved_at IS NULL OR moved_at = '')",
        (int(chat_id),),
    )


def route_seen(conn, kind: str, chat_id: int) -> bool:
    """Evita rotear/transferir o mesmo chat duas vezes (inclusive após restart)."""
    row = conn.execute(
        "SELECT 1 FROM pending_routes WHERE kind = ? AND chat_id = ? LIMIT 1",
        (str(kind), int(chat_id)),
    ).fetchone()
    return bool(row)


def enqueue_route(
    conn,
    kind: str,
    chat_id: int,
    queue_address: str | None,
    team_id: int | None,
    detail: str = "",
    delay_sec: float = 60.0,
    expires_at: str | None = None,
    expects_owner: bool = False,
    exclude_assignee: str = "",
) -> int:
    cur = conn.execute(
        """INSERT INTO pending_routes
             (kind, chat_id, queue_address, team_id, next_try_at, detail, expires_at, expects_owner,
              exclude_assignee)
           VALUES (?, ?, ?, ?, datetime('now', '+' || ? || ' seconds'), ?, ?, ?, ?)""",
        (
            str(kind), int(chat_id), queue_address, team_id,
            str(max(int(delay_sec), 0)), detail or "", (expires_at or None),
            1 if expects_owner else 0,
            str(exclude_assignee or ""),
        ),
    )
    return int(cur.lastrowid)


def relink_case(
    conn,
    chat_id: int,
    case_key: str,
    requester_addr: str = "",
    requester_name: str = "",
    identity_source: str = "address",
) -> int:
    """Reaponta o caso aberto do chat (chat + formulario do mesmo tecnico = 1 caso).

    Diferente de `assign_open`, nao mexe em `assigned_at`: a entrega do formulario
    continua valendo para o TTL de caso orfao (`case_stale_confirm_sec`).
    """
    cur = conn.execute(
        """UPDATE route_assignments
              SET case_key = ?, requester_addr = ?, requester_name = ?, identity_source = ?
            WHERE chat_id = ? AND closed_at IS NULL""",
        (
            str(case_key or ""), str(requester_addr or ""), str(requester_name or ""),
            str(identity_source or "address"), int(chat_id),
        ),
    )
    return int(cur.rowcount or 0)


def form_assignments_open(conn, queue_address: str | None = None) -> list[dict]:
    """Formularios abertos com dono (ledger): base da afinidade e do vinculo."""
    sql = """SELECT * FROM route_assignments
              WHERE kind = 'form' AND closed_at IS NULL"""
    args: list = []
    if queue_address:
        sql += " AND queue_address = ?"
        args.append(str(queue_address))
    return conn.execute(sql + " ORDER BY id", args).fetchall()


def form_case_state(conn, chat_id: int) -> str:
    """Estado do caso do formulario no ledger: 'open', 'done' ou 'unknown'.

    A abertura form-aware usa isso para nao oferecer um formulario que ja foi
    encerrado (`form_done`/`chat_removed`) sem descartar um que apenas expirou
    por TTL ou pela varredura de boot (`stale_boot`/`ttl`) — nesses dois o
    formulario pode muito bem estar aberto no TOA.
    """
    row = conn.execute(
        """SELECT closed_at, close_reason FROM route_assignments
            WHERE chat_id = ? AND kind = 'form' ORDER BY id DESC LIMIT 1""",
        (int(chat_id),),
    ).fetchone()
    if not row:
        return "unknown"
    if not row.get("closed_at"):
        return "open"
    motivo = str(row.get("close_reason") or "").strip().lower()
    if motivo in ("form_done", "chat_removed"):
        return "done"
    return "unknown"


def open_linked_forms(conn) -> set[int]:
    """Formularios JA oferecidos a algum chat (vínculo definitivo).

    Inclui chats encerrados de propósito: depois que o tecnico foi atendido, o
    formulario dele nao pode voltar a ser oferecido num chat posterior (era o
    falso positivo do teste de 22/09: o chat fechava e o formulario voltava a
    ficar "livre"). Situacao do formulario (incluindo "passado") e outro assunto.
    """
    rows = conn.execute(
        "SELECT linked_form_chat_id AS fid FROM chats WHERE linked_form_chat_id IS NOT NULL"
    ).fetchall()
    return {int(r["fid"]) for r in rows if r.get("fid") is not None}


def pending_route_for_case(conn, case_key: str, kind: str) -> dict | None:
    """Rota ativa (waiting) do caso naquele tipo: a peca irma a entregar junto."""
    if not case_key:
        return None
    return conn.execute(
        """SELECT * FROM pending_routes
            WHERE kind = ? AND case_key = ? AND status = 'waiting'
            ORDER BY id LIMIT 1""",
        (str(kind), str(case_key)),
    ).fetchone()


def queue_position(conn, route_id: int, so_chat: bool = False) -> int:
    """Posicao FIFO da rota na fila do time (1 = primeiro).

    Chat e formulario esperam na MESMA linha (a "fila normal") — e isso que define
    a ordem de entrega. Para o **numero falado ao tecnico** use `so_chat=True`:
    ele nao sabe que existem formularios na frente dele, entao contar formulario
    inflava o aviso (com 18 formularios o chat com 2 chats na frente ouvia "Nº 20").
    """
    if not route_id:
        return 0
    row = conn.execute(
        "SELECT team_id, id, priority FROM pending_routes WHERE id = ?", (int(route_id),)
    ).fetchone()
    if not row:
        return 0
    filtro = " AND kind = 'chat'" if so_chat else ""
    prioridade = int(row["priority"] or 0)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM pending_routes"
        " WHERE status = 'waiting' AND (team_id IS ?)"
        " AND (priority > ? OR (priority = ? AND id < ?))" + filtro,
        (row["team_id"], prioridade, prioridade, int(row["id"])),
    ).fetchone()
    return int(total["n"]) + 1


def set_route_position(conn, route_id: int, position: int) -> None:
    conn.execute(
        "UPDATE pending_routes SET last_position = ? WHERE id = ?",
        (int(position), int(route_id)),
    )


def waiting_route(conn, kind: str, chat_id: int) -> dict | None:
    """Rota aberta (status waiting) do chat, se houver."""
    return conn.execute(
        """SELECT * FROM pending_routes
           WHERE kind = ? AND chat_id = ? AND status = 'waiting'
           ORDER BY id LIMIT 1""",
        (str(kind), int(chat_id)),
    ).fetchone()


def finish_routes_for_chat(conn, chat_id: int, status: str, detail: str = "") -> int:
    """Encerra as rotas ainda abertas do chat (chat removido, timeout, tecnico saiu).

    Sem isso o tick continua tentando `move_chat` num chat que nao existe mais.
    """
    cur = conn.execute(
        """UPDATE pending_routes
           SET status = ?, detail = ?, next_try_at = NULL
           WHERE chat_id = ? AND status = 'waiting'""",
        (str(status), detail or "", int(chat_id)),
    )
    return int(cur.rowcount or 0)


def finish_orphan_routes(conn, status: str = "failed",
                         detail: str = "rota orfa: chat fora do TOA") -> int:
    """Encerra rotas de chat cujo chat saiu do conjunto aberto (rota orfa).

    Cinto de seguranca do `resume_stale`: se qualquer caminho deixar o chat fora do
    estado aberto sem fechar a rota, ela para de girar `move_chat` para sempre.
    Nao toca em formulario nem em rota com posse (lease) de entrega viva.
    """
    cur = conn.execute(
        """UPDATE pending_routes
              SET status = ?, detail = ?, next_try_at = NULL,
                  claimed_by = '', claim_expires_at = NULL
            WHERE status = 'waiting' AND kind = 'chat'
              AND chat_id IN (
                    SELECT chat_id FROM chats
                     WHERE status NOT IN
                           ('joined', 'menu_sent', 'awaiting_reply', 'classified',
                            'monitoring', 'awaiting_script_answer',
                            'awaiting_script_confirm', 'awaiting_script_run'))
              AND (COALESCE(claimed_by, '') = ''
                   OR claim_expires_at IS NULL
                   OR datetime(claim_expires_at) <= datetime('now'))""",
        (str(status), detail or ""),
    )
    return int(cur.rowcount or 0)


def _fair_round_robin(rows: list[dict], budget: int) -> list[dict]:
    """1 rota por TIME a cada rodada antes de repetir — sem isso o backlog de
    um time (sem analista elegivel ha horas) monopoliza os poucos slots do
    tick para sempre, porque suas rotas sao sempre as mais antigas (menor
    `id`) e `due_routes` so olhava `id`, nunca `team_id`. A ordem relativa
    dentro de cada time (priority DESC, id) e preservada.
    """
    por_time: dict = {}
    ordem: list = []
    for r in rows:
        tid = r.get("team_id")
        fila_time = por_time.setdefault(tid, [])
        if not fila_time:
            ordem.append(tid)
        fila_time.append(r)
    escolhidos: list[dict] = []
    rodada = 0
    while len(escolhidos) < budget:
        avancou = False
        for tid in ordem:
            fila_time = por_time[tid]
            if rodada < len(fila_time):
                escolhidos.append(fila_time[rodada])
                avancou = True
                if len(escolhidos) >= budget:
                    break
        if not avancou:
            break
        rodada += 1
    return escolhidos


def due_routes(conn, per_line: int = 5, scan_limit: int = 2000) -> dict[str, list[dict]]:
    """Rotas vencidas por LINHA: chat e formulario esperam em filas apartadas.

    Devolve as duas listas, uma rodada de round-robin POR TIME (ver
    `_fair_round_robin`) dentro de cada linha, para o tick tentar a cabeca de
    cada time na mesma passada — um formulario enviado depois de 10 chats e o
    1o da linha dele, nao o 11, e um time com backlog velho sem elegivel nao
    engole os slots de todo mundo. `priority` (painel, acao manual) fura a
    fila do proprio time sem quebrar o FIFO entre iguais. `scan_limit` e so
    uma rede de seguranca de performance — nao e o orcamento real por tick.
    """
    linhas: dict[str, list[dict]] = {}
    for kind in ("chat", "form"):
        vencidas = conn.execute(
            """SELECT * FROM pending_routes
               WHERE status = 'waiting' AND kind = ?
                 AND (next_try_at IS NULL OR datetime(next_try_at) <= datetime('now'))
               ORDER BY priority DESC, id
               LIMIT ?""",
            (kind, max(1, int(scan_limit))),
        ).fetchall()
        linhas[kind] = _fair_round_robin(vencidas, max(1, int(per_line)))
    return linhas


def retry_route(conn, route_id: int, attempts: int, delay_sec: float, detail: str = "") -> None:
    conn.execute(
        """UPDATE pending_routes
           SET attempts = ?, detail = ?, next_try_at = datetime('now', '+' || ? || ' seconds'),
               claimed_by = '', claim_expires_at = NULL
           WHERE id = ?""",
        (int(attempts), detail or "", str(max(int(delay_sec), 0)), int(route_id)),
    )


def retry_route_now(conn, route_id: int) -> bool:
    """Forca a proxima tentativa AGORA, ignorando o backoff do retry.

    Acao manual do painel: usada quando o atendente quer que um item especifico
    seja tentado no proximo tick (5s) em vez de esperar o intervalo cheio
    (`transfer_retry_interval_sec`/`form_route_retry_interval_sec`, 60s por
    padrao).
    """
    cur = conn.execute(
        "UPDATE pending_routes SET next_try_at = datetime('now')"
        " WHERE id = ? AND status = 'waiting'",
        (int(route_id),),
    )
    return bool(cur.rowcount)


def set_route_priority(conn, route_id: int, priority: int) -> bool:
    """Prioriza (ou remove a prioridade de) um item da fila: acao manual do painel.

    `priority=1` fura a fila da propria linha (chat/formulario) e time; entre
    varios priorizados a ordem relativa continua FIFO por `id` (ver due_routes).
    """
    cur = conn.execute(
        "UPDATE pending_routes SET priority = ? WHERE id = ? AND status = 'waiting'",
        (int(priority), int(route_id)),
    )
    return bool(cur.rowcount)


def wake_team_routes(conn, team_id) -> int:
    """Acorda a fila do time ao liberar vaga: zera o backoff de quem ainda espera.

    Sem isso um item podia ficar ate `transfer_retry_interval_sec` (padrao 60s)
    parado mesmo que um analista tivesse liberado vaga um segundo depois da
    ultima tentativa falha — o tick so olha rotas com `next_try_at` vencido.
    Chamada em `Engine._close_case` (qualquer caso que fecha libera a vaga).
    """
    cur = conn.execute(
        "UPDATE pending_routes SET next_try_at = datetime('now')"
        " WHERE status = 'waiting' AND team_id IS ?"
        " AND next_try_at IS NOT NULL AND datetime(next_try_at) > datetime('now')",
        (team_id,),
    )
    return int(cur.rowcount or 0)


def waiting_routes(conn, limite: int = 5) -> list[dict]:
    """Rotas ainda em espera, de QUALQUER linha (chat e formulario).

    Usada quando o bot nao pode atuar de jeito nenhum (desligado no painel): o
    tick encerra o que esta esperando em vez de deixar pendurado.
    """
    return conn.execute(
        """SELECT * FROM pending_routes
            WHERE status = 'waiting'
            ORDER BY id
            LIMIT ?""",
        (max(int(limite), 1),),
    ).fetchall()


def finish_route(conn, route_id: int, status: str, detail: str = "") -> None:
    conn.execute(
        "UPDATE pending_routes SET status = ?, detail = ?, next_try_at = NULL,"
        " claimed_by = '', claim_expires_at = NULL WHERE id = ?",
        (str(status), detail or "", int(route_id)),
    )


def log_attempt(
    conn,
    op: str,
    *,
    kind: str = "",
    chat_id: int | None = None,
    route_id: int | None = None,
    target: str = "",
    request_id: str = "",
    result: str = "",
    latency_ms: int = 0,
    error: str = "",
) -> None:
    """Uma linha por chamada externa de entrega (base da medicao de timeout/sem eco)."""
    try:
        conn.execute(
            """INSERT INTO delivery_attempts
                 (kind, chat_id, route_id, op, target, request_id, result, latency_ms, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(kind or ""),
                None if chat_id is None else int(chat_id),
                None if route_id is None else int(route_id),
                str(op),
                str(target or ""),
                str(request_id or ""),
                str(result or ""),
                int(latency_ms or 0),
                str(error or "")[:300],
            ),
        )
    except Exception:
        log.warning("delivery_attempts: insert falhou", exc_info=True)


def attempts_summary(conn, since: str = "-1 day") -> list[dict]:
    """Contagem de tentativas por resultado (painel: mede o timeout de verdade)."""
    return conn.execute(
        """SELECT op, result, count(*) AS n
             FROM delivery_attempts
            WHERE datetime(at) >= datetime('now', ?)
            GROUP BY op, result ORDER BY n DESC""",
        (str(since),),
    ).fetchall()



def claim_delivery(
    conn,
    kind: str,
    chat_id: int,
    queue_address: str | None,
    team_id: int | None,
    token: str,
    detail: str = "",
    lease_sec: int = 300,
) -> dict | None:
    """Claim atomico da entrega: uma operacao ativa por (kind, chat_id).

    Devolve a rota quando a posse e deste `token`; None quando outra execucao
    esta entregando. A criacao da linha e a trava, garantida pelo indice unico
    parcial `pending_routes_active`; a posse (lease) cobre reinicio no meio.
    """
    lease = str(max(int(lease_sec), 1))
    with tx(conn):
        row = conn.execute(
            """SELECT * FROM pending_routes
                WHERE kind = ? AND chat_id = ?
                  AND status NOT IN ('done', 'failed', 'cancelled')
                ORDER BY id LIMIT 1""",
            (str(kind), int(chat_id)),
        ).fetchone()
        if row:
            rid = int(row["id"])
            cur = conn.execute(
                """UPDATE pending_routes
                      SET claimed_by = ?, claimed_at = datetime('now'),
                          claim_expires_at = datetime('now', '+' || ? || ' seconds')
                    WHERE id = ?
                      AND (COALESCE(claimed_by, '') IN ('', ?)
                           OR claim_expires_at IS NULL
                           OR datetime(claim_expires_at) <= datetime('now'))""",
                (str(token), lease, rid, str(token)),
            )
            if int(cur.rowcount or 0) == 0:
                return None
        else:
            cur = conn.execute(
                """INSERT INTO pending_routes
                     (kind, chat_id, queue_address, team_id, attempts, next_try_at, detail,
                      claimed_by, claimed_at, claim_expires_at)
                   VALUES (?, ?, ?, ?, 0, datetime('now'), ?, ?, datetime('now'),
                           datetime('now', '+' || ? || ' seconds'))""",
                (str(kind), int(chat_id), queue_address, team_id, detail or "", str(token), lease),
            )
            rid = int(cur.lastrowid)
        return conn.execute("SELECT * FROM pending_routes WHERE id = ?", (rid,)).fetchone()


def release_delivery(conn, route_id: int | None, token: str) -> None:
    """Libera a posse sem encerrar a rota (a linha segue ativa para o tick)."""
    if not route_id:
        return
    conn.execute(
        "UPDATE pending_routes SET claimed_by = '', claim_expires_at = NULL"
        " WHERE id = ? AND claimed_by = ?",
        (int(route_id), str(token)),
    )


def delivery_marks(conn, route_id: int | None) -> dict:
    """Marcas da entrega: alvo imutavel, move ja enviado e identidade do caso."""
    if not route_id:
        return {}
    return conn.execute("SELECT * FROM pending_routes WHERE id = ?", (int(route_id),)).fetchone() or {}


def set_delivery_marks(
    conn,
    route_id: int | None,
    *,
    invite_target: str | None = None,
    move_sent_at: bool = False,
    case_key: str | None = None,
    requester_addr: str | None = None,
    team_id: int | None = None,
    exclude_assignee: str | None = None,
) -> None:
    """Grava a intencao da entrega (alvo/caso) e a marca do move ja enviado."""
    if not route_id:
        return
    sets: list[str] = []
    args: list = []
    if invite_target is not None:
        sets.append("invite_target = ?")
        args.append(str(invite_target or ""))
    if move_sent_at:
        sets.append("move_sent_at = datetime('now')")
    if case_key is not None:
        sets.append("case_key = ?")
        args.append(str(case_key or ""))
    if requester_addr is not None:
        sets.append("requester_addr = ?")
        args.append(str(requester_addr or ""))
    if team_id is not None:
        sets.append("team_id = ?")
        args.append(int(team_id))
    if exclude_assignee is not None:
        sets.append("exclude_assignee = ?")
        args.append(str(exclude_assignee or ""))
    if not sets:
        return
    args.append(int(route_id))
    conn.execute(f"UPDATE pending_routes SET {', '.join(sets)} WHERE id = ?", args)


def bump_move_fails(conn, route_id: int | None) -> int:
    """Conta mais uma recusa de `move_chat` na rota e devolve o total.

    O TOA recusa com `e=10` mover um formulario que ja esta com alguem; sem esse
    contador a rota retentava para sempre (e o mesmo analista era alvo de
    `move_chat` a cada minuto).
    """
    if not route_id:
        return 0
    conn.execute(
        "UPDATE pending_routes SET move_fails = move_fails + 1 WHERE id = ?",
        (int(route_id),),
    )
    row = conn.execute(
        "SELECT move_fails FROM pending_routes WHERE id = ?", (int(route_id),)
    ).fetchone()
    return int((row or {}).get("move_fails") or 0)


def open_assignment(conn, chat_id: int) -> dict | None:
    """Caso ja atribuido e aberto para este chat (guarda anti-dupla entrega)."""
    return conn.execute(
        "SELECT * FROM route_assignments WHERE chat_id = ? AND closed_at IS NULL ORDER BY id LIMIT 1",
        (int(chat_id),),
    ).fetchone()


def stuck_routes(conn, limit: int = 50) -> list[dict]:
    """Rotas que precisam de olho humano: falhas/paradas recentes (painel)."""
    return conn.execute(
        """SELECT * FROM pending_routes
            WHERE status IN ('failed', 'cancelled')
            ORDER BY id DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()


def reopen_route(conn, route_id: int) -> bool:
    """Reprocessa uma rota falha/parada (acao explicita do painel)."""
    cur = conn.execute(
        """UPDATE pending_routes
              SET status = 'waiting', attempts = 0, claimed_by = '', claim_expires_at = NULL,
                  move_fails = 0, next_try_at = datetime('now'), detail = 'reprocessada pelo painel'
            WHERE id = ? AND status IN ('failed', 'cancelled')""",
        (int(route_id),),
    )
    return bool(cur.rowcount)


def classified_without_route(conn) -> list[dict]:
    """Chats classificados sem nenhuma rota: crash entre classificar e entregar."""
    return conn.execute(
        """SELECT c.* FROM chats c
            WHERE c.status = 'classified'
              AND c.queue_address IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM pending_routes r WHERE r.chat_id = c.chat_id)
            ORDER BY c.chat_id"""
    ).fetchall()


def assign_open(
    conn,
    kind: str,
    chat_id: int,
    queue_address: str | None,
    team_id: int | None,
    assignee: str,
    case_key: str = '',
    requester_addr: str = '',
    requester_name: str = '',
    identity_source: str = 'address',
) -> None:
    """Abre (ou reabre) o caso no ledger: a carga real passa a contar aqui."""
    conn.execute(
        """INSERT INTO route_assignments
             (kind, chat_id, queue_address, team_id, assignee, case_key,
              requester_addr, requester_name, identity_source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (chat_id) DO UPDATE SET
             kind = excluded.kind,
             queue_address = excluded.queue_address,
             team_id = excluded.team_id,
             assignee = excluded.assignee,
             case_key = excluded.case_key,
             requester_addr = excluded.requester_addr,
             requester_name = excluded.requester_name,
             identity_source = excluded.identity_source,
             assigned_at = datetime('now'),
             closed_at = NULL""",
        (kind, int(chat_id), queue_address, team_id, assignee, case_key or '',
         requester_addr or '', requester_name or '', identity_source or 'address'),
    )


def assign_close(conn, chat_id: int, reason: str = "") -> None:
    """Encerra o caso (chat removido, formulario encerrado, TOA sem o item...).

    O motivo fica gravado: fechar sem explicacao foi o que deixou os casos
    "sumirem" do ledger sem que ninguem soubesse por que (A1).
    """
    conn.execute(
        "UPDATE route_assignments SET closed_at = datetime('now'), close_reason = ?"
        " WHERE chat_id = ? AND closed_at IS NULL",
        (str(reason or ""), int(chat_id)),
    )


def assign_close_stale(conn, ttl_sec: int) -> int:
    """Rede de seguranca: caso orfao (sem evento de saida) deixa de contar."""
    cur = conn.execute(
        "UPDATE route_assignments SET closed_at = datetime('now'), close_reason = 'ttl'"
        " WHERE closed_at IS NULL"
        " AND datetime(assigned_at) < datetime('now', '-' || ? || ' seconds')",
        (str(int(max(ttl_sec, 0))),),
    )
    return int(cur.rowcount or 0)


def close_stale_pending_routes(conn, ttl_sec: int) -> int:
    """Rede de seguranca: rota 'waiting' parada ha tempo demais para de girar.

    Cobre o que o teto de falhas (`move_max_fail`) nao cobre: `dono_offline`
    nao consome falha de proposito (espera o dono sair do TOA), e "time sem
    elegivel" nunca chega a tentar move_chat/invite pra incrementar falha
    nenhuma — os dois casos giram `retry_route` para sempre, sem isso. Visto
    em producao uma rota de 29/09 ainda 'waiting' em 21/10, com 28 mil
    tentativas, disputando toda vez os poucos slots que `due_routes` libera
    por tick com formulario/chat genuinamente novo.

    Vale pra chat e formulario (o chat ja tem `finish_orphan_routes` pra uma
    condicao especifica — chat saiu do bot; isso aqui e so o cinto de
    seguranca generico por tempo, nao substitui aquele).
    """
    cur = conn.execute(
        """UPDATE pending_routes
              SET status = 'failed', detail = 'ttl: rota parada ha tempo demais (rede de seguranca)',
                  next_try_at = NULL, claimed_by = '', claim_expires_at = NULL
            WHERE status = 'waiting'
              AND datetime(created_at) < datetime('now', '-' || ? || ' seconds')
              AND (COALESCE(claimed_by, '') = ''
                   OR claim_expires_at IS NULL
                   OR datetime(claim_expires_at) <= datetime('now'))""",
        (str(int(max(ttl_sec, 0))),),
    )
    return int(cur.rowcount or 0)


def open_chat_assignments(conn) -> list[dict]:
    """Casos de CHAT ainda abertos no ledger (reconciliacao com o chat real).

    O caso de um chat tem que morrer junto com o chat: sem isso ele segue
    contando na carga do analista ate o TTL (`route_assign_ttl_sec`, 12 h) e a
    distribuicao trava com o time "cheio" sem ninguem atendendo.
    """
    return conn.execute(
        "SELECT * FROM route_assignments WHERE kind = 'chat' AND closed_at IS NULL ORDER BY id"
    ).fetchall()


def open_assignments(conn) -> list[dict]:
    return conn.execute(
        "SELECT * FROM route_assignments WHERE closed_at IS NULL ORDER BY id"
    ).fetchall()


def _case_key_variantes(case_key: str) -> list[str]:
    """O mesmo caso no dialeto atual (`a:u:...`) e no legado (`u:...`).

    A afinidade e comparacao exata de string: sem isso um caso aberto antes da
    normalizacao das chaves ficava invisivel para o match — e o formulario do
    mesmo tecnico virava um caso NOVO, entregue a outro analista.
    """
    k = str(case_key or "").strip()
    if not k:
        return []
    if k.startswith("a:"):
        return [k, k[2:]]
    if k.startswith("u:"):
        return ["a:" + k, k]
    return [k]


def case_owner(conn, case_key: str) -> str:
    """Quem ja atende esse caso (afinidade formulario <-> chat)."""
    variantes = _case_key_variantes(case_key)
    if not variantes:
        return ''
    marcadores = ", ".join("?" for _ in variantes)
    row = conn.execute(
        "SELECT assignee FROM route_assignments WHERE closed_at IS NULL"
        f" AND case_key IN ({marcadores}) ORDER BY id DESC",
        variantes,
    ).fetchone()
    return str((row or {}).get('assignee') or '')
