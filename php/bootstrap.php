<?php
declare(strict_types=1);

// Fachada de banco: `sqlite` (padrao) ou `mysql` (secao 5 do sqliteformysql.md).
// O `BotPDO` traduz no choke point — o painel segue escrito no dialeto SQLite.
require_once __DIR__ . '/db.php';

function bot_env(string $key, ?string $default = null): ?string
{
    static $env = null;
    if ($env === null) {
        $env = [];
        $path = dirname(__DIR__) . DIRECTORY_SEPARATOR . '.env';
        if (!is_readable($path)) {
            $path = __DIR__ . DIRECTORY_SEPARATOR . '.env';
        }
        if (is_readable($path)) {
            foreach (file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
                $line = trim($line);
                if ($line === '' || $line[0] === '#') {
                    continue;
                }
                [$k, $v] = array_pad(explode('=', $line, 2), 2, '');
                $env[trim($k)] = trim($v, " \t\"'");
            }
        }
    }
    return $env[$key] ?? $default;
}

function bot_root(): string
{
    return dirname(__DIR__);
}

function bot_sqlite_path(): string
{
    // Override explicito (so as suites de painel usam): garante banco temporario
    // mesmo com `.env` definindo SQLITE_PATH — sem isso o harness renderizava o
    // banco real e o teste "passava" sem exercitar os dados semeados.
    $override = getenv('SQLITE_PATH_OVERRIDE');
    if ($override !== false && $override !== '') {
        return $override;
    }
    // `.env` manda (é a fonte documentada); sem a chave lá, aceita o ambiente —
    // é o que permite rodar as suítes de painel em banco temporário sem tocar
    // em data/helptoa.sqlite.
    $rel = bot_env('SQLITE_PATH');
    if ($rel === null || $rel === '') {
        $rel = getenv('SQLITE_PATH') ?: 'data/helptoa.sqlite';
    }
    if ($rel === null || $rel === '') {
        $rel = 'data/helptoa.sqlite';
    }
    if (preg_match('#^[a-zA-Z]:[\\\\/]#', $rel) || (isset($rel[0]) && $rel[0] === '/')) {
        return $rel;
    }
    return bot_root() . DIRECTORY_SEPARATOR . str_replace(['/', '\\'], DIRECTORY_SEPARATOR, $rel);
}

function bot_pdo(): PDO
{
    static $pdo = null;
    if ($pdo instanceof PDO) {
        return $pdo;
    }
    // Abre pelo backend configurado (`DB_BACKEND`); o caminho do arquivo SQLite
    // so e usado quando o backend e `sqlite`.
    $pdo = bot_db_abre(bot_sqlite_path());
    // Os tres PRAGMAs valem no SQLite e o tradutor os converte no MySQL:
    // `foreign_keys` -> `SET FOREIGN_KEY_CHECKS`, `busy_timeout` -> tempo de
    // espera por lock da sessao e `journal_mode` (que nao existe) sai fora.
    $pdo->exec('PRAGMA foreign_keys = ON');
    $pdo->exec('PRAGMA journal_mode = WAL');
    $pdo->exec('PRAGMA busy_timeout = 5000');
    bot_ensure_schema($pdo);
    return $pdo;
}

function bot_ensure_schema(PDO $pdo): void
{
    static $done = false;
    if ($done) {
        return;
    }
    $done = true;
    $file = bot_root() . DIRECTORY_SEPARATOR . 'schema.sql';
    if (!is_readable($file)) {
        throw new RuntimeException('schema.sql ausente em ' . $file);
    }
    $sql = file_get_contents($file);
    if ($sql === false) {
        throw new RuntimeException('Não foi possível ler schema.sql');
    }
    if (bot_table_exists($pdo, 'menu_options')) {
        bot_migrate_submenu($pdo);
    }
    // O script sai inteiro pelo `bot_executa_script`: no MySQL ele desliga a
    // checagem de FK durante o DDL (as tabelas nascem fora da ordem das FKs) e
    // tira o `PRAGMA foreign_keys = ON` do schema (seria o erro 1824 no meio).
    bot_executa_script($pdo, $sql);
    bot_migrate_submenu($pdo);
    bot_migrate_rbac($pdo);
    bot_migrate_ask($pdo);
    bot_migrate_queue($pdo);
    bot_migrate_delivery($pdo);
    bot_migrate_load($pdo);
    bot_migrate_form_aware($pdo);
    bot_migrate_events_author($pdo);
    bot_migrate_queue_load($pdo);
}

/**
 * Fila do TOA por endereco (card FILA FORMS do dashboard): tabela queue_load.
 *
 * O bot grava a cada snapshot o que a lista `pending` do TOA mostrou para cada
 * fila; o painel soma para o card. Sem a tabela o SELECT do dashboard cai no
 * try/catch e o card vira "-".
 */
function bot_migrate_queue_load(PDO $pdo): void
{
    if (bot_table_exists($pdo, 'queue_load')) {
        return;
    }
    $pdo->exec(
        'CREATE TABLE queue_load ('
        . ' queue_address TEXT PRIMARY KEY,'
        . " at TEXT NOT NULL DEFAULT (datetime('now')),"
        . ' pending_forms INTEGER NOT NULL DEFAULT 0,'
        . ' pending_chats INTEGER NOT NULL DEFAULT 0,'
        . ' toa_ok INTEGER NOT NULL DEFAULT 0,'
        . " detail TEXT NOT NULL DEFAULT '')"
    );
}

/**
 * Autor do chat/formulario em bot_events (coluna AUTOR da aba Atividade).
 *
 * Base criada antes destas colunas nao ganha nada com o CREATE IF NOT EXISTS
 * do schema.sql: o ALTER e o que vale — inclusive no painel local plugado no
 * banco de producao, roda uma unica vez e e idempotente.
 */
function bot_migrate_events_author(PDO $pdo): void
{
    if (!bot_table_exists($pdo, 'bot_events')) {
        return;
    }
    $cols = bot_table_columns($pdo, 'bot_events');
    if (!isset($cols['author_addr'])) {
        $pdo->exec("ALTER TABLE bot_events ADD COLUMN author_addr TEXT NOT NULL DEFAULT ''");
    }
    if (!isset($cols['author_name'])) {
        $pdo->exec("ALTER TABLE bot_events ADD COLUMN author_name TEXT NOT NULL DEFAULT ''");
    }
}

/**
 * Fluxo "Pedir info e transferir" com perguntas: a tabela menu_option_questions
 * vem do schema.sql; aqui garantimos a coluna de estado em chats (bases antigas)
 * e a marca de atividade do tecnico (timeout por silencio).
 */
function bot_migrate_ask(PDO $pdo): void
{
    if (!bot_table_exists($pdo, 'chats')) {
        return;
    }
    $chatCols = bot_table_columns($pdo, 'chats');
    if (!isset($chatCols['ask_option_id'])) {
        $pdo->exec('ALTER TABLE chats ADD COLUMN ask_option_id INTEGER REFERENCES menu_options(id) ON DELETE SET NULL');
    }
    if (!isset($chatCols['last_reply_at'])) {
        $pdo->exec('ALTER TABLE chats ADD COLUMN last_reply_at TEXT');
    }
    if (!isset($chatCols['moved_at'])) {
        // Transferencia parcial: `move_chat` ja feito e `invite_to_chat` pendente.
        $pdo->exec('ALTER TABLE chats ADD COLUMN moved_at TEXT');
    }
}

/**
 * Carga por analista (medicao): ultima leitura das listas do TOA + historico.
 * Espelho de python/db.py::_migrate_load — o bot e quem grava; o painel le.
 */
/**
 * Status de chat em que o BOT esta atuando (antes de passar para um analista).
 * Espelho de python/db.py::resume_chats — use sempre por aqui, para nao divergir.
 */
function bot_open_chat_statuses(): array
{
    return [
        'joined', 'menu_sent', 'awaiting_reply', 'classified', 'monitoring',
        'awaiting_script_answer', 'awaiting_script_confirm', 'awaiting_script_run',
        'awaiting_form_choice',
    ];
}

/**
 * Uma fila so pode ser HABILITADA no painel com Menu ATIVO e Time ATIVO vinculados.
 *
 * Sao as mesmas premissas que o bot usa ao atender: sem menu o chat morre em
 * "menu vazio" (join_failed) e sem time ativo o caso acaba encerrado
 * (closed_no_agent). Habilitar a fila sem os dois deixava o bot assumir o chat,
 * mandar o menu e fechar depois, sem transferir ninguem.
 * Devolve '' quando pode habilitar, ou o motivo do bloqueio (texto para a tela).
 */
function queue_enable_block(PDO $pdo, string $address): string
{
    $st = $pdo->prepare(
        'SELECT'
        . ' (SELECT COUNT(*) FROM menu_queues mq JOIN menus m ON m.id = mq.menu_id'
        . '   WHERE mq.queue_address = q.address AND m.active = 1) AS menus,'
        . ' (SELECT COUNT(*) FROM menu_options o JOIN menus m ON m.id = o.menu_id'
        . '    JOIN menu_queues mq ON mq.menu_id = m.id'
        . '   WHERE mq.queue_address = q.address AND m.active = 1 AND o.parent_id IS NULL) AS opcoes,'
        . ' (SELECT COUNT(*) FROM team_queues tq JOIN teams t ON t.id = tq.team_id'
        . '   WHERE tq.queue_address = q.address AND t.active = 1) AS times'
        . ' FROM queues q WHERE q.address = ?'
    );
    $st->execute([$address]);
    $row = $st->fetch();
    if (!$row) {
        return 'fila não encontrada';
    }
    $faltas = [];
    if ((int) ($row['menus'] ?? 0) < 1) {
        $faltas[] = 'nenhum Menu ativo vinculado (aba Menus)';
    } elseif ((int) ($row['opcoes'] ?? 0) < 1) {
        $faltas[] = 'menu ativo sem nenhuma opção cadastrada (aba Menus)';
    }
    if ((int) ($row['times'] ?? 0) < 1) {
        $faltas[] = 'nenhum Time ativo vinculado (aba Times)';
    }
    return $faltas ? implode(' e ', $faltas) : '';
}

/**
 * Filas HABILITADAS vinculadas a um menu ou time.
 *
 * `$ownerTable` e `menu_queues` (coluna `menu_id`) ou `team_queues` (`team_id`).
 * A trava de habilitacao da fila so vale se o vinculo nao puder sumir depois:
 * desativar/excluir o menu ou o time — ou desvincular a fila — deixaria a fila
 * ligada sem dono e o chat cairia em "menu vazio"/closed_no_agent.
 * Devolve a lista de addresses (ordenada).
 */
function bot_enabled_queues_linked(PDO $pdo, string $ownerTable, int $ownerId): array
{
    $coluna = ['menu_queues' => 'menu_id', 'team_queues' => 'team_id'][$ownerTable] ?? null;
    if ($coluna === null) {
        return [];
    }
    $st = $pdo->prepare(
        'SELECT l.queue_address FROM ' . $ownerTable . ' l'
        . ' JOIN queues q ON q.address = l.queue_address'
        . ' WHERE l.' . $coluna . ' = ? AND q.enabled = 1 ORDER BY l.queue_address'
    );
    $st->execute([$ownerId]);
    return array_map(static fn($r) => (string) $r['queue_address'], $st->fetchAll());
}

function bot_migrate_load(PDO $pdo): void
{
    if (!bot_table_exists($pdo, 'analyst_load')) {
        $pdo->exec(
            'CREATE TABLE analyst_load ('
            . ' address TEXT PRIMARY KEY,'
            . " at TEXT NOT NULL DEFAULT (datetime('now')),"
            . " name TEXT NOT NULL DEFAULT '',"
            . ' team_id INTEGER,'
            . " janela TEXT NOT NULL DEFAULT '',"
            . ' online INTEGER NOT NULL DEFAULT 0,'
            . ' chats_toa INTEGER NOT NULL DEFAULT 0,'
            . ' forms_toa INTEGER NOT NULL DEFAULT 0,'
            . ' chats_ledger INTEGER NOT NULL DEFAULT 0,'
            . ' forms_ledger INTEGER NOT NULL DEFAULT 0,'
            . ' reservas INTEGER NOT NULL DEFAULT 0,'
            . ' casos_total INTEGER NOT NULL DEFAULT 0,'
            . ' form_ocioso_min REAL NOT NULL DEFAULT 0,'
            . ' form_presente_toa INTEGER NOT NULL DEFAULT 0,'
            . ' toa_ok INTEGER NOT NULL DEFAULT 0,'
            . ' casos_toa_keys TEXT NOT NULL DEFAULT \'\','
            . " detail TEXT NOT NULL DEFAULT '')"
        );
    }
    if (bot_table_exists($pdo, 'analyst_load') && !isset(bot_table_columns($pdo, 'analyst_load')['casos_toa_keys'])) {
        $pdo->exec("ALTER TABLE analyst_load ADD COLUMN casos_toa_keys TEXT NOT NULL DEFAULT ''");
    }
    // Presenca persistida (resgate por analista offline): `online_ok` separa
    // "leitura falhou" de "esta offline"; `offline_since` carimba o comeco.
    if (bot_table_exists($pdo, 'analyst_load')) {
        $loadCols = bot_table_columns($pdo, 'analyst_load');
        if (!isset($loadCols['online_ok'])) {
            $pdo->exec('ALTER TABLE analyst_load ADD COLUMN online_ok INTEGER NOT NULL DEFAULT 0');
        }
        if (!isset($loadCols['offline_since'])) {
            $pdo->exec('ALTER TABLE analyst_load ADD COLUMN offline_since TEXT');
        }
    }
    if (bot_table_exists($pdo, 'team_routing') && !isset(bot_table_columns($pdo, 'team_routing')['offline_requeue_sec'])) {
        $pdo->exec('ALTER TABLE team_routing ADD COLUMN offline_requeue_sec INTEGER');
    }
    if (!bot_table_exists($pdo, 'analyst_load_history')) {
        $pdo->exec(
            'CREATE TABLE analyst_load_history ('
            . ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
            . " at TEXT NOT NULL DEFAULT (datetime('now')),"
            . ' address TEXT NOT NULL,'
            . ' chats_toa INTEGER NOT NULL DEFAULT 0,'
            . ' forms_toa INTEGER NOT NULL DEFAULT 0,'
            . ' chats_ledger INTEGER NOT NULL DEFAULT 0,'
            . ' forms_ledger INTEGER NOT NULL DEFAULT 0,'
            . ' casos_total INTEGER NOT NULL DEFAULT 0,'
            . ' toa_ok INTEGER NOT NULL DEFAULT 0)'
        );
        $pdo->exec('CREATE INDEX IF NOT EXISTS analyst_load_history_at ON analyst_load_history (at DESC)');
    }
}

/**
 * Abertura form-aware: vinculo chat<->formulario, rota presa ao dono do caso e
 * os textos novos da aba Templates. Espelho de python/db.py::_migrate_form_aware.
 */
function bot_migrate_form_aware(PDO $pdo): void
{
    if (!bot_table_exists($pdo, 'templates')) {
        return;
    }
    if (bot_table_exists($pdo, 'chats') && !isset(bot_table_columns($pdo, 'chats')['linked_form_chat_id'])) {
        $pdo->exec('ALTER TABLE chats ADD COLUMN linked_form_chat_id INTEGER');
    }
    if (bot_table_exists($pdo, 'pending_routes') && !isset(bot_table_columns($pdo, 'pending_routes')['expects_owner'])) {
        $pdo->exec('ALTER TABLE pending_routes ADD COLUMN expects_owner INTEGER NOT NULL DEFAULT 0');
    }
    // A abertura form-aware foi REMOVIDA (28/09): a oferta "1) falar sobre o
    // formulario / 2) abrir o menu" nao existe mais, entao os textos dela saem
    // do banco (o chat de fila sempre recebe o menu da fila).
    $delTpl = $pdo->prepare('DELETE FROM templates WHERE slug = ?');
    foreach (['form_pending', 'form_pending_wait', 'form_waiting'] as $slugTpl) {
        $delTpl->execute([$slugTpl]);
    }

    // Reparo do "balde" de identidade: chave generica do agente (ou vazia) vira
    // caso POR FORMULARIO (`chat:<id>`) — assim a afinidade nao junta tecnicos
    // diferentes no mesmo analista. Espelho de python/db.py::_migrate_case_keys.
    $generica = "(case_key = '' OR lower(case_key) IN"
        . " ('n:message scenario agent', 'n:mensagem de cenario', 'n:solicitacao de encerramento'))";
    if (bot_table_exists($pdo, 'route_assignments') && isset(bot_table_columns($pdo, 'route_assignments')['case_key'])) {
        $pdo->exec("UPDATE route_assignments SET case_key = 'chat:' || chat_id WHERE closed_at IS NULL AND {$generica}");
        // Dialeto legado: chave de address SEM o prefixo `a:` (`u:...`). A
        // afinidade compara string exata, entao essa chave nao casava com a do
        // formulario/chat novo do mesmo tecnico. Idempotente. Espelho de
        // python/db.py::_migrate_case_keys.
        $pdo->exec("UPDATE route_assignments SET case_key = 'a:' || case_key WHERE closed_at IS NULL AND case_key LIKE 'u:%'");
    }
    if (bot_table_exists($pdo, 'pending_routes') && isset(bot_table_columns($pdo, 'pending_routes')['case_key'])) {
        $pdo->exec("UPDATE pending_routes SET case_key = 'chat:' || chat_id WHERE status = 'waiting' AND {$generica}");
        $pdo->exec("UPDATE pending_routes SET case_key = 'a:' || case_key WHERE status = 'waiting' AND case_key LIKE 'u:%'");
    }
}

/**
 * Fila de espera (Fase 2): ultima posicao avisada e o encerramento manual.
 *
 * A espera **nao tem prazo**: `expires_at` fica so como marcador do botao
 * "Encerrar espera"; a chave antiga `queue_max_wait_sec` e removida do banco.
 * Espelho de python/db.py::_migrate_queue.
 */
function bot_migrate_queue(PDO $pdo): void
{
    if (!bot_table_exists($pdo, 'pending_routes')) {
        return;
    }
    $cols = bot_table_columns($pdo, 'pending_routes');
    if (!isset($cols['expires_at'])) {
        $pdo->exec('ALTER TABLE pending_routes ADD COLUMN expires_at TEXT');
    }
    if (bot_table_exists($pdo, 'bot_settings')) {
        $pdo->exec("DELETE FROM bot_settings WHERE key = 'queue_max_wait_sec'");
    }
    // Rotas que ja esperavam com prazo automatico perdem o vencimento (o pedido
    // manual do painel grava `expires_at` no passado e e preservado).
    $pdo->exec(
        "UPDATE pending_routes SET expires_at = NULL"
        . " WHERE status = 'waiting' AND expires_at IS NOT NULL AND expires_at > datetime('now')"
    );
    if (!isset($cols['last_position'])) {
        $pdo->exec('ALTER TABLE pending_routes ADD COLUMN last_position INTEGER NOT NULL DEFAULT 0');
    }
}

function bot_migrate_delivery(PDO $pdo): void
{
    // Espelho de python/db.py::_migrate_delivery: identidade do caso na entrega,
    // marcas (alvo imutavel/move ja enviado), posse (claim) e o indice unico que
    // impede duas entregas ativas para o mesmo chat/formulario.
    if (!bot_table_exists($pdo, 'pending_routes')) {
        return;
    }
    $cols = bot_table_columns($pdo, 'pending_routes');
    $novas = [
        'case_key' => "case_key TEXT NOT NULL DEFAULT ''",
        'requester_addr' => "requester_addr TEXT NOT NULL DEFAULT ''",
        'invite_target' => "invite_target TEXT NOT NULL DEFAULT ''",
        'move_sent_at' => 'move_sent_at TEXT',
        // `move_chat` recusado pelo TOA (e=10 = formulario ja com alguem):
        // a rota para depois de N falhas em vez de girar para sempre.
        'move_fails' => 'move_fails INTEGER NOT NULL DEFAULT 0',
        'claimed_by' => "claimed_by TEXT NOT NULL DEFAULT ''",
        'claimed_at' => 'claimed_at TEXT',
        'claim_expires_at' => 'claim_expires_at TEXT',
        // Resgate por analista offline: alvo que nao pode receber de volta.
        'exclude_assignee' => "exclude_assignee TEXT NOT NULL DEFAULT ''",
    ];
    foreach ($novas as $nome => $ddl) {
        if (!isset($cols[$nome])) {
            $pdo->exec('ALTER TABLE pending_routes ADD COLUMN ' . $ddl);
        }
    }
    if (bot_table_exists($pdo, 'route_assignments') && !isset(bot_table_columns($pdo, 'route_assignments')['close_reason'])) {
        $pdo->exec("ALTER TABLE route_assignments ADD COLUMN close_reason TEXT NOT NULL DEFAULT ''");
    }
    if (!bot_table_exists($pdo, 'delivery_attempts')) {
        $pdo->exec(
            'CREATE TABLE delivery_attempts ('
            . ' id INTEGER PRIMARY KEY AUTOINCREMENT,'
            . " at TEXT NOT NULL DEFAULT (datetime('now')),"
            . " kind TEXT NOT NULL DEFAULT '',"
            . ' chat_id INTEGER, route_id INTEGER,'
            . ' op TEXT NOT NULL,'
            . " target TEXT NOT NULL DEFAULT '',"
            . " request_id TEXT NOT NULL DEFAULT '',"
            . " result TEXT NOT NULL DEFAULT '',"
            . ' latency_ms INTEGER NOT NULL DEFAULT 0,'
            . " error TEXT NOT NULL DEFAULT '')"
        );
        $pdo->exec('CREATE INDEX IF NOT EXISTS delivery_attempts_at_idx ON delivery_attempts (at DESC)');
        $pdo->exec('CREATE INDEX IF NOT EXISTS delivery_attempts_chat_idx ON delivery_attempts (chat_id, id)');
    }
    // Saneamento: mantem a rota ativa mais antiga por (kind, chat_id).
    // O `EXISTS` antigo lia a propria `pending_routes` de dentro do UPDATE, o que
    // o SQLite aceita e o MySQL recusa (erro 1093, "target table ... in FROM
    // clause"): a tabela derivada `rotas_ativas` e materializada antes do UPDATE
    // nos dois motores, e o `MIN(id)` por grupo deixa uma so rota ativa.
    $pdo->exec(
        "UPDATE pending_routes SET status = 'cancelled',"
        . " detail = 'duplicata saneada na migracao'"
        . " WHERE status NOT IN ('done', 'failed', 'cancelled')"
        . ' AND id NOT IN (SELECT id FROM ('
        . '   SELECT MIN(id) AS id FROM pending_routes'
        . "    WHERE status NOT IN ('done', 'failed', 'cancelled')"
        . '    GROUP BY kind, chat_id) AS rotas_ativas)'
    );
    $pdo->exec(
        "CREATE UNIQUE INDEX IF NOT EXISTS pending_routes_active ON pending_routes (kind, chat_id)"
        . " WHERE status NOT IN ('done', 'failed', 'cancelled')"
    );
}

function bot_table_exists(PDO $pdo, string $table): bool
{
    $st = $pdo->prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?");
    $st->execute([$table]);
    return (bool) $st->fetch();
}

function bot_table_columns(PDO $pdo, string $table): array
{
    $cols = [];
    foreach ($pdo->query('PRAGMA table_info(' . $table . ')') as $r) {
        $cols[$r['name']] = true;
    }
    return $cols;
}

/** Endereço do PRÓPRIO bot (gravado pelo engine em `bot_settings`). */
function bot_own_address(PDO $pdo): string
{
    try {
        $raw = $pdo->query("SELECT value FROM bot_settings WHERE key = 'bot_own_address'")->fetchColumn();
    } catch (Throwable $e) {
        return '';
    }
    return trim((string) ($raw ?: ''));
}

/** O endereço informado é o do próprio bot? (ele nunca pode ser operador) */
function addr_is_bot(string $addr, string $own): bool
{
    $a = strtolower(trim($addr));
    $b = strtolower(trim($own));
    if ($a === '' || $b === '') {
        return false;
    }
    if ($a === $b) {
        return true;
    }
    // Compara também pelo login: o TOA às vezes devolve o endereço sem o prefixo
    // `u:` e o bot não pode entrar como operador de jeito nenhum (o caso voltaria
    // para quem só repassa o atendimento).
    $login = static function (string $v): string {
        if (strpos($v, 'u:') === 0) {
            $v = substr($v, 2);
        }
        $at = strpos($v, '@');
        return $at === false ? $v : substr($v, 0, $at);
    };
    $la = $login($a);
    return $la !== '' && $la === $login($b);
}

function bot_migrate_submenu(PDO $pdo): void
{
    $optCols = bot_table_columns($pdo, 'menu_options');
    if (!isset($optCols['parent_id'])) {
        $pdo->exec('ALTER TABLE menu_options ADD COLUMN parent_id INTEGER REFERENCES menu_options(id) ON DELETE CASCADE');
    }
    $chatCols = bot_table_columns($pdo, 'chats');
    if (!isset($chatCols['menu_node_id'])) {
        $pdo->exec('ALTER TABLE chats ADD COLUMN menu_node_id INTEGER REFERENCES menu_options(id) ON DELETE SET NULL');
    }
    if (!isset($optCols['reply_text_if_form'])) {
        $pdo->exec("ALTER TABLE menu_options ADD COLUMN reply_text_if_form TEXT NOT NULL DEFAULT ''");
    }
    if (!isset($optCols['action_if_form'])) {
        $pdo->exec("ALTER TABLE menu_options ADD COLUMN action_if_form TEXT NOT NULL DEFAULT ''");
    }
    $optCols = bot_table_columns($pdo, 'menu_options');
    if (!isset($optCols['script_slug'])) {
        $pdo->exec("ALTER TABLE menu_options ADD COLUMN script_slug TEXT NOT NULL DEFAULT ''");
    }
    $chatCols = bot_table_columns($pdo, 'chats');
    if (!isset($chatCols['script_slug'])) {
        $pdo->exec("ALTER TABLE chats ADD COLUMN script_slug TEXT NOT NULL DEFAULT ''");
    }
    if (!isset($chatCols['script_field_index'])) {
        $pdo->exec('ALTER TABLE chats ADD COLUMN script_field_index INTEGER NOT NULL DEFAULT 0');
    }
    if (!isset($chatCols['script_answers'])) {
        $pdo->exec("ALTER TABLE chats ADD COLUMN script_answers TEXT NOT NULL DEFAULT '{}'");
    }
    if (!isset($chatCols['script_uuid'])) {
        $pdo->exec("ALTER TABLE chats ADD COLUMN script_uuid TEXT NOT NULL DEFAULT ''");
    }
    if (!isset($chatCols['script_started_at'])) {
        $pdo->exec('ALTER TABLE chats ADD COLUMN script_started_at TEXT');
    }
    if (bot_table_exists($pdo, 'queue_operators')) {
        $qoCols = bot_table_columns($pdo, 'queue_operators');
        if (!isset($qoCols['source'])) {
            $pdo->exec("ALTER TABLE queue_operators ADD COLUMN source TEXT NOT NULL DEFAULT 'toa'");
        }
    }
    if (bot_table_exists($pdo, 'team_routing')) {
        $trCols = bot_table_columns($pdo, 'team_routing');
        if (!isset($trCols['cap_cases'])) {
            $pdo->exec('ALTER TABLE team_routing ADD COLUMN cap_cases INTEGER');
        }
        if (!isset($trCols['form_window_margin_min'])) {
            $pdo->exec('ALTER TABLE team_routing ADD COLUMN form_window_margin_min INTEGER');
        }
    }
    if (bot_table_exists($pdo, 'team_member_routing')) {
        $tmrCols = bot_table_columns($pdo, 'team_member_routing');
        if (!isset($tmrCols['cap_cases'])) {
            $pdo->exec('ALTER TABLE team_member_routing ADD COLUMN cap_cases INTEGER');
        }
    }
    $pdo->exec('DROP INDEX IF EXISTS menu_options_per_menu');
    $pdo->exec(
        'CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_root
         ON menu_options (menu_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NULL'
    );
    $pdo->exec(
        'CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_child
         ON menu_options (menu_id, parent_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NOT NULL'
    );
    $sql = (string) $pdo->query("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'menu_options'")->fetchColumn();
    if ($sql !== '' && strpos($sql, 'run_script') === false) {
        bot_rebuild_menu_options_check($pdo);
    }
}

function bot_rebuild_menu_options_check(PDO $pdo): void
{
    $pdo->exec('PRAGMA foreign_keys = OFF');
    $pdo->exec('BEGIN');
    $pdo->exec(
        "CREATE TABLE menu_options_new (
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
        )"
    );
    $pdo->exec(
        'INSERT INTO menu_options_new (id, menu_id, queue_address, sort_order, label, classification, action, reply_text, reply_text_if_form, action_if_form, transfer_target_id, parent_id, script_slug)
         SELECT id, menu_id, queue_address, sort_order, label, classification, action, reply_text,
                COALESCE(reply_text_if_form, \'\'), COALESCE(action_if_form, \'\'), transfer_target_id, parent_id,
                COALESCE(script_slug, \'\') FROM menu_options'
    );
    $pdo->exec('DROP TABLE menu_options');
    $pdo->exec('ALTER TABLE menu_options_new RENAME TO menu_options');
    $pdo->exec(
        'CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_queue
         ON menu_options (queue_address, sort_order) WHERE queue_address IS NOT NULL'
    );
    $pdo->exec(
        'CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_root
         ON menu_options (menu_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NULL'
    );
    $pdo->exec(
        'CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_menu_child
         ON menu_options (menu_id, parent_id, sort_order) WHERE menu_id IS NOT NULL AND parent_id IS NOT NULL'
    );
    $pdo->exec('COMMIT');
    $pdo->exec('PRAGMA foreign_keys = ON');
}

function bot_migrate_rbac(PDO $pdo): void
{
    if (!bot_table_exists($pdo, 'bot_users')) {
        return;
    }
    $userCols = bot_table_columns($pdo, 'bot_users');
    if (!isset($userCols['group_id'])) {
        $pdo->exec('ALTER TABLE bot_users ADD COLUMN group_id INTEGER REFERENCES permission_groups(id) ON DELETE SET NULL');
    }
    if (!isset($userCols['display_name'])) {
        $pdo->exec("ALTER TABLE bot_users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''");
    }
    if (!isset($userCols['operator_address'])) {
        $pdo->exec("ALTER TABLE bot_users ADD COLUMN operator_address TEXT NOT NULL DEFAULT ''");
    }
    if (!isset($userCols['last_login_at'])) {
        $pdo->exec('ALTER TABLE bot_users ADD COLUMN last_login_at TEXT');
    }
    bot_rbac_promote_legacy_users($pdo);
    bot_rbac_copy_user_scope_to_groups($pdo);
}

/**
 * Promove para Super Admin os usuarios que existiam antes do RBAC, uma unica
 * vez (flag rbac_seeded em bot_settings), para o painel nunca ficar sem
 * administrador. Usuarios criados depois ficam sem grupo ate serem associados.
 */
function bot_rbac_promote_legacy_users(PDO $pdo): void
{
    if (!bot_table_exists($pdo, 'permission_groups') || !bot_table_exists($pdo, 'bot_settings')) {
        return;
    }
    $st = $pdo->prepare('SELECT value FROM bot_settings WHERE key = ?');
    $st->execute(['rbac_seeded']);
    $flag = $st->fetchColumn();
    if ($flag === false || (string) $flag === '1') {
        return;
    }
    $superId = bot_super_group_id($pdo);
    if ($superId > 0) {
        $pdo->prepare('UPDATE bot_users SET group_id = ? WHERE group_id IS NULL')->execute([$superId]);
    }
    $pdo->prepare('UPDATE bot_settings SET value = ? WHERE key = ?')->execute(['1', 'rbac_seeded']);
}

/**
 * O escopo de dados passou a ser do GRUPO. Copia uma unica vez o que estava no
 * nivel do usuario (user_teams/user_queues) para group_teams/group_queues.
 * As tabelas de usuario ficam como legado, sem uso pelo painel.
 */
function bot_rbac_copy_user_scope_to_groups(PDO $pdo): void
{
    if (!bot_table_exists($pdo, 'group_teams') || !bot_table_exists($pdo, 'group_queues') || !bot_table_exists($pdo, 'bot_settings')) {
        return;
    }
    $st = $pdo->prepare('SELECT value FROM bot_settings WHERE key = ?');
    $st->execute(['rbac_scope_groups']);
    $flag = $st->fetchColumn();
    if ($flag === false || (string) $flag === '1') {
        return;
    }
    if (bot_table_exists($pdo, 'user_teams')) {
        $pdo->exec(
            'INSERT OR IGNORE INTO group_teams (group_id, team_id)
             SELECT u.group_id, ut.team_id FROM user_teams ut
               JOIN bot_users u ON u.id = ut.user_id
              WHERE u.group_id IS NOT NULL'
        );
    }
    if (bot_table_exists($pdo, 'user_queues')) {
        $pdo->exec(
            'INSERT OR IGNORE INTO group_queues (group_id, queue_address)
             SELECT u.group_id, uq.queue_address FROM user_queues uq
               JOIN bot_users u ON u.id = uq.user_id
              WHERE u.group_id IS NOT NULL'
        );
    }
    $pdo->prepare('UPDATE bot_settings SET value = ? WHERE key = ?')->execute(['1', 'rbac_scope_groups']);
}

function bot_super_group_id(PDO $pdo): int
{
    if (!bot_table_exists($pdo, 'permission_groups')) {
        return 0;
    }
    $st = $pdo->query('SELECT id FROM permission_groups WHERE is_super = 1 ORDER BY id LIMIT 1');
    $id = $st === false ? false : $st->fetchColumn();
    return ($id === false || $id === null) ? 0 : (int) $id;
}

function bot_has_active_super_admin(PDO $pdo): bool
{
    $superId = bot_super_group_id($pdo);
    if ($superId < 1) {
        return false;
    }
    $st = $pdo->prepare('SELECT 1 FROM bot_users WHERE group_id = ? AND active = 1 LIMIT 1');
    $st->execute([$superId]);
    return $st->fetchColumn() !== false;
}

function h(?string $v): string
{
    return htmlspecialchars((string) $v, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}
