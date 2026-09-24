from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import sqlite_path, ROOT

log = logging.getLogger("helptoa")

SCHEMA_PATH = ROOT / "schema.sql"


def _dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def connect():
    path = sqlite_path()
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    conn.row_factory = _dict_factory
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
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
    conn.executescript(sql)
    _migrate_submenu(conn)
    _migrate_rbac(conn)
    _migrate_ask(conn)
    _migrate_queue(conn)
    _migrate_delivery(conn)
    _migrate_form_aware(conn)
    _migrate_case_keys(conn)
    _migrate_load(conn)


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
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'menu_options'"
    ).fetchone()
    sql = (row or {}).get("sql") or ""
    if sql and "run_script" not in sql:
        _rebuild_menu_options_check(conn)


def _rebuild_menu_options_check(conn) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    conn.execute(
        """CREATE TABLE menu_options_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id INTEGER REFERENCES menus(id) ON DELETE CASCADE,
            queue_address TEXT REFERENCES queues(address) ON DELETE CASCADE,
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
    """Fila de espera (Fase 2): expiracao e ultima posicao avisada."""
    if "pending_routes" not in _table_names(conn):
        return
    cols = _table_columns(conn, "pending_routes")
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE pending_routes ADD COLUMN expires_at TEXT")
    if "last_position" not in cols:
        conn.execute("ALTER TABLE pending_routes ADD COLUMN last_position INTEGER NOT NULL DEFAULT 0")


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
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE pending_routes ADD COLUMN {ddl}")
    if "route_assignments" in _table_names(conn) and "close_reason" not in _table_columns(conn, "route_assignments"):
        conn.execute("ALTER TABLE route_assignments ADD COLUMN close_reason TEXT NOT NULL DEFAULT ''")
    # Saneamento: mantem a rota ativa mais antiga por (kind, chat_id).
    conn.execute(
        """UPDATE pending_routes SET status = 'cancelled',
                  detail = 'duplicata saneada na migracao'
            WHERE status NOT IN ('done', 'failed', 'cancelled')
              AND EXISTS (SELECT 1 FROM pending_routes r2
                           WHERE r2.kind = pending_routes.kind
                             AND r2.chat_id = pending_routes.chat_id
                             AND r2.status NOT IN ('done', 'failed', 'cancelled')
                             AND r2.id < pending_routes.id)"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS pending_routes_active
             ON pending_routes (kind, chat_id)
           WHERE status NOT IN ('done', 'failed', 'cancelled')"""
    )


# Textos da abertura form-aware. Ficam aqui (e no schema.sql) porque bancos
# antigos precisam dos slugs novos sem reexecutar o schema inteiro.
FORM_AWARE_TEMPLATES = (
    (
        "form_pending",
        "Você tem um formulário de validação em andamento com {analista}."
        + chr(10) + chr(10) + "Digite o número da opção:"
        + chr(10) + "1) Falar sobre o formulário"
        + chr(10) + "2) Abrir o menu",
    ),
    (
        "form_pending_wait",
        "Você tem um formulário de validação aguardando atendimento."
        + chr(10) + chr(10) + "Digite o número da opção:"
        + chr(10) + "1) Falar sobre o formulário"
        + chr(10) + "2) Abrir o menu",
    ),
    (
        "form_waiting",
        "Assim que um analista assumir a sua validação, eu encaminho este chat para ele."
        " Em breve você será atendido.",
    ),
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
                 detail TEXT NOT NULL DEFAULT ''
               )"""
        )
    if "analyst_load_history" not in _table_names(conn):
        conn.execute(
            """CREATE TABLE analyst_load_history (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 at TEXT NOT NULL DEFAULT (datetime('now')),
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


def load_upsert(conn, address: str, **campos) -> None:
    """Grava a leitura atual do analista (uma linha por endereco)."""
    base = {
        "name": "", "team_id": None, "janela": "", "online": 0, "chats_toa": 0, "forms_toa": 0,
        "chats_ledger": 0, "forms_ledger": 0, "reservas": 0, "casos_total": 0,
        "form_ocioso_min": 0.0, "form_presente_toa": 0, "toa_ok": 0, "detail": "",
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


def load_trim_history(conn, keep: int = 200) -> int:
    """Mantem so as `keep` leituras mais recentes do historico."""
    cur = conn.execute(
        """DELETE FROM analyst_load_history
            WHERE id NOT IN (SELECT id FROM analyst_load_history ORDER BY id DESC LIMIT ?)""",
        (int(max(keep, 1)),),
    )
    return int(cur.rowcount or 0)


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
    """Abertura form-aware: vinculo chat<->formulario e rota presa ao dono do caso.

    - `chats.linked_form_chat_id`: o chat do tecnico sabe qual formulario e o dele;
    - `pending_routes.expects_owner`: a rota do chat do ramo 1 so entrega ao dono
      do caso (analista que atende o formulario) e, sem dono, apenas espera;
    - templates novos da abertura (aba Templates).
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
        for slug, body in FORM_AWARE_TEMPLATES:
            conn.execute(
                "INSERT OR IGNORE INTO templates (slug, body) VALUES (?, ?)",
                (slug, body),
            )
        # Reparo: a PRIMEIRA versao da semente usava "\n" dentro da string SQL —
        # e SQLite nao interpreta escape, entao gravou barra+n em vez de quebra
        # de linha. Como o seed e INSERT OR IGNORE, a linha errada nunca era
        # corrigida. Aqui convertemos (idempotente: so onde existe o literal).
        conn.execute(
            "UPDATE templates SET body = replace(body, ?, char(10))"
            " WHERE slug IN ('form_pending', 'form_pending_wait', 'form_waiting')"
            " AND instr(body, ?) > 0",
            ("\\n", "\\n"),
        )


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


def templates(conn) -> dict:
    rows = conn.execute("SELECT slug, body FROM templates").fetchall()
    return {r["slug"]: r["body"] for r in rows}


def menu_option(conn, option_id: int | None) -> dict | None:
    if not option_id:
        return None
    return conn.execute("SELECT * FROM menu_options WHERE id = ?", (int(option_id),)).fetchone()


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
                              'awaiting_script_confirm', 'awaiting_form_choice')
               AND last_prompt_at IS NOT NULL"""
    args: list = []
    if seconds is not None:
        sql += " AND datetime(last_prompt_at) < datetime('now', '-' || ? || ' seconds')"
        args.append(str(int(seconds)))
    return conn.execute(sql, args).fetchall()


def resume_chats(conn) -> list[dict]:
    return conn.execute(
        "SELECT * FROM chats WHERE status IN ('joined', 'menu_sent', 'awaiting_reply', 'classified', 'monitoring', 'awaiting_script_answer', 'awaiting_script_confirm', 'awaiting_script_run', 'awaiting_form_choice')"
    ).fetchall()


def script_run_timeouts(conn, seconds: int = 60) -> list[dict]:
    return conn.execute(
        """SELECT * FROM chats
           WHERE status = 'awaiting_script_run'
             AND script_started_at IS NOT NULL
             AND datetime(script_started_at) < datetime('now', '-' || ? || ' seconds')""",
        (str(seconds),),
    ).fetchall()


def log_event(
    conn,
    reason: str,
    chat_id: int | None = None,
    queue_address: str | None = None,
    queue_enabled: bool | None = None,
    detail: str = "",
) -> None:
    enabled = None if queue_enabled is None else (1 if queue_enabled else 0)
    try:
        conn.execute(
            """INSERT INTO bot_events (chat_id, queue_address, queue_enabled, reason, detail)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, queue_address, enabled, reason, detail or ""),
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
) -> int:
    cur = conn.execute(
        """INSERT INTO pending_routes
             (kind, chat_id, queue_address, team_id, next_try_at, detail, expires_at, expects_owner)
           VALUES (?, ?, ?, ?, datetime('now', '+' || ? || ' seconds'), ?, ?, ?)""",
        (
            str(kind), int(chat_id), queue_address, team_id,
            str(max(int(delay_sec), 0)), detail or "", (expires_at or None),
            1 if expects_owner else 0,
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
        "SELECT team_id, id FROM pending_routes WHERE id = ?", (int(route_id),)
    ).fetchone()
    if not row:
        return 0
    filtro = " AND kind = 'chat'" if so_chat else ""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM pending_routes"
        " WHERE status = 'waiting' AND id < ? AND (team_id IS ?)" + filtro,
        (int(row["id"]), row["team_id"]),
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


def due_routes(conn, limit: int = 5) -> list[dict]:
    return conn.execute(
        """SELECT * FROM pending_routes
           WHERE status = 'waiting'
             AND (next_try_at IS NULL OR datetime(next_try_at) <= datetime('now'))
           ORDER BY id
           LIMIT ?""",
        (int(limit),),
    ).fetchall()


def retry_route(conn, route_id: int, attempts: int, delay_sec: float, detail: str = "") -> None:
    conn.execute(
        """UPDATE pending_routes
           SET attempts = ?, detail = ?, next_try_at = datetime('now', '+' || ? || ' seconds'),
               claimed_by = '', claim_expires_at = NULL
           WHERE id = ?""",
        (int(attempts), detail or "", str(max(int(delay_sec), 0)), int(route_id)),
    )


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
