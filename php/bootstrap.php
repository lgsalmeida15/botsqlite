<?php
declare(strict_types=1);

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
    $rel = bot_env('SQLITE_PATH', 'data/helptoa.sqlite');
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
    $path = bot_sqlite_path();
    $dir = dirname($path);
    if (!is_dir($dir) && !mkdir($dir, 0775, true) && !is_dir($dir)) {
        throw new RuntimeException('Não foi possível criar a pasta do SQLite: ' . $dir);
    }
    $pdo = new PDO('sqlite:' . $path, null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
    ]);
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
    $sql = preg_replace('/^\s*--.*$/m', '', $sql);
    foreach (preg_split('/;\s*/', $sql) as $stmt) {
        $stmt = trim($stmt);
        if ($stmt === '') {
            continue;
        }
        $pdo->exec($stmt);
    }
    bot_migrate_submenu($pdo);
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

function h(?string $v): string
{
    return htmlspecialchars((string) $v, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}
