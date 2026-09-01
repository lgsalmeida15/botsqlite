-- HELPTOA bot — SQLite
-- Do chat persiste só chat_id. Sem texto, nome ou anexo.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS bot_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  login TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS queues (
  address TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transfer_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('user', 'queue')),
  address TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT NOT NULL UNIQUE,
  body TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS menus (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS menu_queues (
  menu_id INTEGER NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
  queue_address TEXT NOT NULL REFERENCES queues(address) ON DELETE CASCADE,
  PRIMARY KEY (menu_id, queue_address),
  UNIQUE (queue_address)
);

CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  last_assigned TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS team_members (
  team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  address TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  route_start TEXT,
  route_end TEXT,
  PRIMARY KEY (team_id, address)
);

CREATE TABLE IF NOT EXISTS team_queues (
  team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  queue_address TEXT NOT NULL REFERENCES queues(address) ON DELETE CASCADE,
  PRIMARY KEY (team_id, queue_address),
  UNIQUE (queue_address)
);

CREATE TABLE IF NOT EXISTS menu_options (
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
);

CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_queue
  ON menu_options (queue_address, sort_order) WHERE queue_address IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_root
  ON menu_options (menu_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_child
  ON menu_options (menu_id, parent_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS queue_operators (
  queue_address TEXT NOT NULL REFERENCES queues(address) ON DELETE CASCADE,
  address TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (queue_address, address)
);

CREATE TABLE IF NOT EXISTS menu_option_targets (
  option_id INTEGER NOT NULL REFERENCES menu_options(id) ON DELETE CASCADE,
  address TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (option_id, address)
);

CREATE TABLE IF NOT EXISTS bot_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
  chat_id INTEGER PRIMARY KEY,
  queue_address TEXT REFERENCES queues(address),
  status TEXT NOT NULL,
  classification TEXT,
  option_id INTEGER REFERENCES menu_options(id) ON DELETE SET NULL,
  menu_node_id INTEGER REFERENCES menu_options(id) ON DELETE SET NULL,
  retries INTEGER NOT NULL DEFAULT 0,
  auto_close INTEGER NOT NULL DEFAULT 0,
  menu_sent_at TEXT,
  last_prompt_at TEXT,
  classified_at TEXT,
  left_at TEXT,
  script_slug TEXT NOT NULL DEFAULT '',
  script_field_index INTEGER NOT NULL DEFAULT 0,
  script_answers TEXT NOT NULL DEFAULT '{}',
  script_uuid TEXT NOT NULL DEFAULT '',
  script_started_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS chats_status_idx ON chats (status);
CREATE INDEX IF NOT EXISTS chats_classified_idx ON chats (updated_at) WHERE classification IS NOT NULL;

CREATE TABLE IF NOT EXISTS bot_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  chat_id INTEGER,
  queue_address TEXT,
  queue_enabled INTEGER,
  reason TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS bot_events_at_idx ON bot_events (at DESC);

INSERT OR IGNORE INTO templates (slug, body) VALUES
  ('greeting', 'Olá. Sou o assistente de triagem.'),
  ('menu', 'Digite o número da opção:'),
  ('retry', 'Por gentileza, digite o numero que melhor corresponde a solicitação'),
  ('closing', 'Chat está sendo encerrado');

INSERT OR IGNORE INTO bot_settings (key, value) VALUES
  ('reply_timeout_sec', '300'),
  ('max_invalid_retries', '3'),
  ('config_poll_sec', '15'),
  ('bot_enabled', 'true'),
  ('max_open_chats_per_agent', '8'),
  ('ws_stale_sec', '180');
