-- HELPTOA bot — SQLite
-- Do chat persiste só chat_id. Sem texto, nome ou anexo.

PRAGMA foreign_keys = ON;

-- Colunas de RBAC (php/bootstrap.php -> bot_migrate_rbac adiciona em bases antigas).
-- group_id NULL = usuario sem grupo (sem acesso ao painel).
CREATE TABLE IF NOT EXISTS bot_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  login TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  display_name TEXT NOT NULL DEFAULT '',
  operator_address TEXT NOT NULL DEFAULT '',
  group_id INTEGER REFERENCES permission_groups(id) ON DELETE SET NULL,
  last_login_at TEXT,
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
  queue_address VARCHAR(191) NOT NULL REFERENCES queues(address) ON DELETE CASCADE,
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
  address VARCHAR(191) NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  route_start TEXT,
  route_end TEXT,
  PRIMARY KEY (team_id, address)
);

CREATE TABLE IF NOT EXISTS team_queues (
  team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  queue_address VARCHAR(191) NOT NULL REFERENCES queues(address) ON DELETE CASCADE,
  PRIMARY KEY (team_id, queue_address),
  UNIQUE (queue_address)
);

CREATE TABLE IF NOT EXISTS menu_options (
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
);

CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_queue
  ON menu_options (queue_address, sort_order) WHERE queue_address IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_root
  ON menu_options (menu_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_child
  ON menu_options (menu_id, parent_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NOT NULL;

-- Perguntas da opcao "Pedir info e transferir": o bot coleta no chat, o tecnico
-- confirma e so entao a transferencia acontece (mesmo wizard dos scripts).
CREATE TABLE IF NOT EXISTS menu_option_questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  option_id INTEGER NOT NULL REFERENCES menu_options(id) ON DELETE CASCADE,
  sort_order INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'text' CHECK (kind IN ('text', 'int', 'choice')),
  choices TEXT NOT NULL DEFAULT '',
  UNIQUE (option_id, sort_order)
);

CREATE TABLE IF NOT EXISTS queue_operators (
  queue_address VARCHAR(191) NOT NULL REFERENCES queues(address) ON DELETE CASCADE,
  address VARCHAR(191) NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'toa',
  PRIMARY KEY (queue_address, address)
);

CREATE TABLE IF NOT EXISTS menu_option_targets (
  option_id INTEGER NOT NULL REFERENCES menu_options(id) ON DELETE CASCADE,
  address VARCHAR(191) NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (option_id, address)
);

CREATE TABLE IF NOT EXISTS bot_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
  chat_id INTEGER PRIMARY KEY,
  queue_address VARCHAR(191) REFERENCES queues(address),
  status VARCHAR(191) NOT NULL,
  classification TEXT,
  option_id INTEGER REFERENCES menu_options(id) ON DELETE SET NULL,
  menu_node_id INTEGER REFERENCES menu_options(id) ON DELETE SET NULL,
  ask_option_id INTEGER REFERENCES menu_options(id) ON DELETE SET NULL,
  retries INTEGER NOT NULL DEFAULT 0,
  auto_close INTEGER NOT NULL DEFAULT 0,
  menu_sent_at TEXT,
  last_prompt_at TEXT,
  last_reply_at TEXT,
  classified_at TEXT,
  left_at TEXT,
  moved_at TEXT,
  -- Abertura form-aware: chat do tecnico vinculado ao formulario de validacao
  -- aberto dele (os dois itens passam a ser UM caso para o roteamento).
  linked_form_chat_id INTEGER,
  script_slug TEXT NOT NULL DEFAULT '',
  script_field_index INTEGER NOT NULL DEFAULT 0,
  script_answers TEXT NOT NULL DEFAULT '{}',
  script_uuid TEXT NOT NULL DEFAULT '',
  script_started_at TEXT,
  updated_at VARCHAR(191) NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS chats_status_idx ON chats (status);
CREATE INDEX IF NOT EXISTS chats_classified_idx ON chats (updated_at) WHERE classification IS NOT NULL;

CREATE TABLE IF NOT EXISTS bot_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at VARCHAR(191) NOT NULL DEFAULT (datetime('now')),
  chat_id INTEGER,
  queue_address VARCHAR(191),
  queue_enabled INTEGER,
  reason VARCHAR(191) NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  -- Autor do chat/formulario no momento do evento (nome + endereco). Fica
  -- vazio quando o payload nao traz identidade confiavel; o painel cai no
  -- endereco ou mostra "-". Colunas preenchidas pelo bot no `_emit`.
  author_addr TEXT NOT NULL DEFAULT '',
  author_name TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS bot_events_at_idx ON bot_events (at DESC);
CREATE INDEX IF NOT EXISTS bot_events_metrics_idx ON bot_events (reason, queue_address, at);
CREATE INDEX IF NOT EXISTS bot_events_chat_idx ON bot_events (chat_id, id);

CREATE TABLE IF NOT EXISTS bot_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'done', 'error')),
  detail TEXT NOT NULL DEFAULT '',
  requested_at TEXT NOT NULL DEFAULT (datetime('now')),
  started_at TEXT,
  finished_at TEXT
);

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
  ('ws_stale_sec', '180'),
  ('transfer_attempts', '5'),
  ('transfer_retry_interval_sec', '60'),
  -- Resgate por ausencia do operador: caso preso com analista offline volta para
  -- a fila depois de `offline_requeue_sec` (0 desliga; por time em team_routing).
  ('offline_requeue_enabled', 'true'),
  ('offline_requeue_sec', '120'),
  -- Formulario: para de ser OFERECIDO N minutos antes do fim da janela do
  -- operador (a escala dele), para o caso nao nascer com quem ja vai sair.
  ('form_window_margin_min', '10');

-- Roteamento por time (1:1 com teams). NULL = herda o global (bot_settings).
-- Só o formulário de validação tem retry assíncrono; o chat segue o fluxo atual.
CREATE TABLE IF NOT EXISTS team_routing (
  team_id INTEGER PRIMARY KEY REFERENCES teams(id) ON DELETE CASCADE,
  strategy TEXT CHECK (strategy IN ('round_robin', 'priority', 'least_load')),
  cap_chat INTEGER,
  cap_form INTEGER,
  cap_cases INTEGER,
  require_online INTEGER,
  respect_window INTEGER,
  attempts INTEGER,
  retry_interval_sec INTEGER,
  fallback TEXT CHECK (fallback IN ('close', 'retry', 'monitor')),
  form_enabled INTEGER NOT NULL DEFAULT 1,
  -- Resgate por ausencia do operador: NULL = herda o global; 0 = este time nao
  -- devolve para a fila o caso de analista offline.
  offline_requeue_sec INTEGER,
  -- Formulario: para de ser OFERECIDO N minutos antes do fim da janela do
  -- operador (NULL = herda o global `form_window_margin_min`).
  form_window_margin_min INTEGER,
  -- Tipo de atendimento padrao dos operadores do time (membro sem linha herda
  -- isto): 'both' (padrao), 'chat' ou 'form'.
  member_kinds TEXT CHECK (member_kinds IN ('both', 'chat', 'form')),
  last_assigned_chat TEXT,
  last_assigned_form TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Roteamento por membro do time (NULL = herda o cap do time/global).
CREATE TABLE IF NOT EXISTS team_member_routing (
  team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  address VARCHAR(191) NOT NULL,
  weight INTEGER NOT NULL DEFAULT 0,
  cap_chat INTEGER,
  cap_form INTEGER,
  cap_cases INTEGER,
  forms_enabled INTEGER NOT NULL DEFAULT 1,
  -- Tipo de atendimento do operador: 'both' (padrao), 'chat' (so chat) ou
  -- 'form' (so formulario). `forms_enabled` fica como espelho de leitura.
  kinds TEXT NOT NULL DEFAULT 'both' CHECK (kinds IN ('both', 'chat', 'form')),
  PRIMARY KEY (team_id, address)
);

-- Retry assíncrono do roteamento (hoje só formulário), drenado pelo tick.
CREATE TABLE IF NOT EXISTS pending_routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind VARCHAR(191) NOT NULL CHECK (kind IN ('chat', 'form')),
  chat_id INTEGER NOT NULL,
  queue_address TEXT,
  team_id INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_try_at VARCHAR(191),
  status VARCHAR(191) NOT NULL DEFAULT 'waiting'
    CHECK (status IN ('waiting', 'done', 'failed', 'cancelled')),
  detail TEXT NOT NULL DEFAULT '',
  expires_at TEXT,
  last_position INTEGER NOT NULL DEFAULT 0,
  -- Identidade do caso na entrega (sobrevive a restart; ver engine._deliver_*).
  case_key TEXT NOT NULL DEFAULT '',
  requester_addr TEXT NOT NULL DEFAULT '',
  -- Entrega: alvo escolhido (imutavel nas tentativas) e marca do move ja feito.
  invite_target TEXT NOT NULL DEFAULT '',
  move_sent_at TEXT,
  -- `move_chat` recusado pelo TOA (e=10 = formulario ja com alguem): a rota para
  -- depois de N falhas em vez de girar para sempre.
  move_fails INTEGER NOT NULL DEFAULT 0,
  -- Alvo que NAO deve receber de novo (resgate por analista offline): a rota
  -- socorrida exclui quem a detinha, para nao devolver o caso ao mesmo operador.
  exclude_assignee TEXT NOT NULL DEFAULT '',
  -- Posse da entrega (claim atomico em db.claim_delivery): evita dupla entrega.
  claimed_by TEXT NOT NULL DEFAULT '',
  claimed_at TEXT,
  claim_expires_at TEXT,
  -- Abertura form-aware: rota do chat que so pode ser entregue ao DONO do caso
  -- (analista que atende o formulario); sem dono ela espera, sem gastar tentativa.
  expects_owner INTEGER NOT NULL DEFAULT 0,
  -- Priorizacao manual (painel): fura a fila da propria linha (chat/formulario)
  -- e time. Ordem real e `priority DESC, id ASC` (ver db.due_routes).
  priority INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS pending_routes_due ON pending_routes (status, next_try_at);

-- Uma tentativa por chamada externa de entrega (move/invite/leave): base da
-- instrumentacao e da medicao de timeout/sem eco (toa.command devolve o `r`).
CREATE TABLE IF NOT EXISTS delivery_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at VARCHAR(191) NOT NULL DEFAULT (datetime('now')),
  kind TEXT NOT NULL DEFAULT '',
  chat_id INTEGER,
  route_id INTEGER,
  op TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  request_id TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS delivery_attempts_at_idx ON delivery_attempts (at DESC);
CREATE INDEX IF NOT EXISTS delivery_attempts_chat_idx ON delivery_attempts (chat_id, id);

INSERT OR IGNORE INTO bot_settings (key, value) VALUES
  ('form_routing_enabled', 'true'),
  ('max_open_forms_per_agent', '8'),
  ('form_route_attempts', '5'),
  ('form_route_retry_interval_sec', '60'),
  ('form_route_fallback', 'retry'),
  ('queue_wait_enabled', 'true'),
  -- A espera da fila NAO tem prazo: a rota so sai por entrega, saida do autor,
  -- remocao do chat, ausencia nas listas do TOA ou "Encerrar espera" no painel.
  -- (A antiga `queue_max_wait_sec` e removida na migracao.)
  -- Posicao falada ao tecnico so a partir daqui (fila interna conta chat+form,
  -- mas o numero anunciado conta so chats).
  ('queue_top_pos', '5'),
  -- Fala a posicao ao tecnico? `false` (padrao) = so "avancou na fila", nunca o
  -- numero. `true` volta ao comportamento por `queue_top_pos`.
  ('queue_show_pos', 'false'),
  -- Medicao de carga por analista (aba Carga): leitura das listas do TOA.
  ('load_poll_sec', '30'),
  ('analyst_load_keep', '200'),
  -- Leitura de formulario: ausente nas listas do TOA por N leituras com leitura
  -- bem sucedida = resolvido no TOA (fecha o caso e libera a vaga do analista).
  ('form_absent_readings', '2'),
  ('routing_strategy', 'round_robin');

-- Carga por analista: ultima leitura das listas do TOA + o que o ledger conta.
-- `analyst_load_history` guarda o historico curto (tendencia na aba Carga).
CREATE TABLE IF NOT EXISTS analyst_load (
  address TEXT PRIMARY KEY,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  name TEXT NOT NULL DEFAULT '',
  team_id INTEGER,
  janela TEXT NOT NULL DEFAULT '',
  online INTEGER NOT NULL DEFAULT 0,
  -- Presenca: `online_ok` diz se a leitura do `get_online_users` respondeu nesta
  -- volta (sem isso um timeout do TOA marcaria todo mundo como offline) e
  -- `offline_since` e o carimbo da PRIMEIRA leitura que o viu offline — e o que
  -- permite devolver o caso para a fila depois de `offline_requeue_sec`.
  online_ok INTEGER NOT NULL DEFAULT 0,
  offline_since TEXT,
  chats_toa INTEGER NOT NULL DEFAULT 0,
  forms_toa INTEGER NOT NULL DEFAULT 0,
  chats_ledger INTEGER NOT NULL DEFAULT 0,
  forms_ledger INTEGER NOT NULL DEFAULT 0,
  reservas INTEGER NOT NULL DEFAULT 0,
  casos_total INTEGER NOT NULL DEFAULT 0,
  form_ocioso_min REAL NOT NULL DEFAULT 0,
  form_presente_toa INTEGER NOT NULL DEFAULT 0,
  toa_ok INTEGER NOT NULL DEFAULT 0,
  -- Chaves de caso lidas do TOA nesta volta (servem a afinidade por analista):
  -- a coluna nasce aqui e nao so nas migracoes, senao a carga de origem para o
  -- MySQL morreria em 1054 (drift de coluna legada).
  casos_toa_keys TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS analyst_load_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at VARCHAR(191) NOT NULL DEFAULT (datetime('now')),
  address TEXT NOT NULL,
  chats_toa INTEGER NOT NULL DEFAULT 0,
  forms_toa INTEGER NOT NULL DEFAULT 0,
  chats_ledger INTEGER NOT NULL DEFAULT 0,
  forms_ledger INTEGER NOT NULL DEFAULT 0,
  casos_total INTEGER NOT NULL DEFAULT 0,
  toa_ok INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS analyst_load_history_at ON analyst_load_history (at DESC);

-- Fila do TOA por endereco (coleta do snapshot): o que a lista `pending` do
-- helpdesk mostrou para CADA FILA na ultima volta — uma linha por fila. E a
-- fonte do card FILA FORMS do dashboard: forms presentes na fila do TOA, sem
-- depender de `pending_routes` (rotas orfas de form inflavam o numero).
CREATE TABLE IF NOT EXISTS queue_load (
  queue_address TEXT PRIMARY KEY,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  pending_forms INTEGER NOT NULL DEFAULT 0,
  pending_chats INTEGER NOT NULL DEFAULT 0,
  toa_ok INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT ''
);

-- Ledger de atribuicoes: a carga real por CASO (solicitante distinto).
-- Formulario + chat do MESMO solicitante contam como um unico caso.
CREATE TABLE IF NOT EXISTS route_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('chat', 'form')),
  chat_id INTEGER NOT NULL UNIQUE,
  queue_address TEXT,
  team_id INTEGER,
  assignee VARCHAR(191) NOT NULL,
  case_key VARCHAR(191) NOT NULL DEFAULT '',
  requester_addr TEXT NOT NULL DEFAULT '',
  requester_name TEXT NOT NULL DEFAULT '',
  identity_source TEXT NOT NULL DEFAULT 'address',
  assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
  closed_at VARCHAR(191),
  -- Motivo do fechamento (chat_removed, stale_boot, form_done, ttl, manual):
  -- sem isso o case podia "sumir" do ledger sem explicacao.
  close_reason TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS route_assignments_open
  ON route_assignments (assignee, closed_at, case_key);

CREATE INDEX IF NOT EXISTS route_assignments_case
  ON route_assignments (case_key, closed_at);

INSERT OR IGNORE INTO bot_settings (key, value) VALUES
  ('max_cases_per_agent', '4'),
  ('route_capacity_source', 'both'),
  ('route_assign_ttl_sec', '43200'),
  ('route_reserve_ttl_sec', '300'),
  ('affinity_enabled', 'true'),
  -- A afinidade NAO e excecao ao online: `require_online` vale para todos (a
  -- chave antiga `affinity_require_online` foi removida).
  ('affinity_match_name', 'false'),
  ('delivery_claim_ttl_sec', '300'),
  ('case_stale_confirm_sec', '300'),
  -- Janela de vida do formulario (o TOA nao avisa a saida do analista):
  -- acima de `offer` ele deixa de ser oferecido na abertura; acima de `close`
  -- ele sai do indice e o caso fecha (`form_closed motivo=form_ocioso`).
  -- Com analista do time dentro vale a regua maior (`form_ocioso_analista`).
  ('form_idle_offer_min', '30'),
  ('form_idle_close_min', '120'),
  ('form_idle_owner_close_min', '240');

-- Textos do fluxo "Pedir info e transferir" com perguntas (aba Templates).
INSERT OR IGNORE INTO templates (slug, body) VALUES
  ('ask_received', 'Registrei as informações:'),
  ('ask_transferring', 'Já estou transferindo para o time responsável.'),
  ('queue_position', 'Você é o Nº {pos} da fila. Assim que um analista liberar, eu te chamo.'),
  -- Fila acima do topo (`queue_top_pos`, padrão 5): o técnico não ouve número —
  -- a posição inclui formulários que ele não vê e um número alto soa como fila
  -- que não anda. Aqui ele recebe só o progresso.
  ('queue_progress', 'Você avançou na fila. Assim que um analista liberar, eu te chamo.'),
  -- Espera vencida (`queue_max_wait_sec`) com fallback `monitor`: único aviso ao
  -- técnico de que ele saiu da fila (com `close` o texto `closing` já informa).
  ('queue_expired', 'Sua espera na fila foi encerrada. Assim que um analista ficar disponível eu volto a falar com você.');

-- Operador que estava atendendo ficou offline: o chat volta para a fila e o
-- tecnico e avisado (o bot ainda pode demorar a achar o proximo elegivel).
INSERT OR IGNORE INTO templates (slug, body) VALUES
  ('requeue_notice', 'Operador ficou offline, transferindo para outro operador');

-- Abertura form-aware: REMOVIDA em 28/09 (associava errado o formulario ao chat).
-- Os textos `form_pending`, `form_pending_wait` e `form_waiting` saem do banco na
-- migracao (`db._migrate_form_aware` / `bot_migrate_form_aware`); o chat de fila
-- sempre recebe o menu da fila.

-- ---------------------------------------------------------------------------
-- Painel: usuarios, grupos de permissao e escopo de dados (RBAC).
-- Aplicado pelo painel (php/bootstrap.php -> bot_migrate_rbac) e tolerado pelo
-- bot Python (python/db.py -> _migrate_rbac). O motor de atendimento NAO le
-- nenhuma destas tabelas.
--   permission_groups  grupo de permissao (is_super = Super Admin)
--   group_permissions  chaves de permissao do grupo (catalogo fica no PHP)
--   user_teams         escopo de dados: times que o usuario do painel enxerga
--   user_queues        excecao de escopo: filas concedidas fora de qualquer time
-- Escopo dos dados: user_teams -> team_queues -> queues.address.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS permission_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1,
  is_super INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS group_permissions (
  group_id INTEGER NOT NULL REFERENCES permission_groups(id) ON DELETE CASCADE,
  perm_key VARCHAR(191) NOT NULL,
  PRIMARY KEY (group_id, perm_key)
);

CREATE TABLE IF NOT EXISTS user_teams (
  user_id INTEGER NOT NULL REFERENCES bot_users(id) ON DELETE CASCADE,
  team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, team_id)
);

CREATE TABLE IF NOT EXISTS user_queues (
  user_id INTEGER NOT NULL REFERENCES bot_users(id) ON DELETE CASCADE,
  queue_address VARCHAR(191) NOT NULL REFERENCES queues(address) ON DELETE CASCADE,
  PRIMARY KEY (user_id, queue_address)
);

-- Escopo de dados POR GRUPO: filas, menus e times que o grupo enxerga.
-- Filas visiveis = filas dos times marcados (group_teams -> team_queues) + group_queues.
-- Sem marcacao e sem as chaves scope.all_* o grupo nao ve dado nenhum (negar por padrao).
-- group_menus da acesso a configuracao daqueles menus (nao concede dados de fila).
CREATE TABLE IF NOT EXISTS group_teams (
  group_id INTEGER NOT NULL REFERENCES permission_groups(id) ON DELETE CASCADE,
  team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  PRIMARY KEY (group_id, team_id)
);

CREATE TABLE IF NOT EXISTS group_queues (
  group_id INTEGER NOT NULL REFERENCES permission_groups(id) ON DELETE CASCADE,
  queue_address VARCHAR(191) NOT NULL REFERENCES queues(address) ON DELETE CASCADE,
  PRIMARY KEY (group_id, queue_address)
);

CREATE TABLE IF NOT EXISTS group_menus (
  group_id INTEGER NOT NULL REFERENCES permission_groups(id) ON DELETE CASCADE,
  menu_id INTEGER NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
  PRIMARY KEY (group_id, menu_id)
);

INSERT OR IGNORE INTO permission_groups (id, name, active, is_super) VALUES (1, 'Super Admin', 1, 1);

-- Flags da migracao: '0' = passo ainda nao executado.
INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('rbac_seeded', '0');
INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('rbac_scope_groups', '0');
