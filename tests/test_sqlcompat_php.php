<?php
declare(strict_types=1);

/**
 * Tradutor SQLite -> MySQL do painel (`php/db.php`): 100% offline, sem servidor.
 *
 * Trava as regras da secao 5.1 que o painel usa de verdade. O `datetime(<coluna>, ?)`
 * aqui e o caso que mais dói: o painel passa o MODIFICADOR (`'-3 hours'`) e o valor
 * so existe no `execute` — o SQL sai `DATE_ADD(<coluna>, INTERVAL ? SECOND)` e o
 * plano converte o parametro na hora de executar.
 */
require __DIR__ . '/../php/db.php';

$failed = 0;
function ok(bool $cond, string $msg): void
{
    global $failed;
    if ($cond) {
        echo "ok  $msg\n";
        return;
    }
    $failed++;
    echo "FAIL  $msg\n";
}

/** `[sql]` traduzido para MySQL. */
function tz(string $sql): string
{
    return bot_sql_traduz($sql, true)[0];
}

/** `[sql, plano]` completo. */
function tp(string $sql): array
{
    return bot_sql_traduz($sql, true);
}

// ------------------------------------------------------------------- sqlite
ok(bot_sql_traduz('SELECT 1 FROM queues WHERE address = ?', false)[0] === 'SELECT 1 FROM queues WHERE address = ?',
    'backend sqlite: comando intacto');
ok(bot_sql_traduz("PRAGMA journal_mode = WAL", false)[0] === 'PRAGMA journal_mode = WAL',
    'backend sqlite: PRAGMA intacto');

// -------------------------------------------------------------------- tempo
ok(preg_match("/'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'/", tz("SELECT datetime('now') AS a")) === 1
    && strpos(tz("SELECT datetime('now') AS a"), "'" . gmdate('Y-m-d H:i') . ':') !== false,
    "datetime('now') -> carimbo UTC literal");
$ontem = tz("SELECT datetime('now', '-1 day') AS a");
$aUmDia = gmdate('Y-m-d H:i:s', time() - 86400);
ok(strpos($ontem, "'" . $aUmDia . "'") !== false, "datetime('now','-1 day') -> um dia atras");
$umSeg = tz("SELECT datetime('now', '-1 seconds') AS a");
ok(strpos($umSeg, "'" . gmdate('Y-m-d H:i:s', time() - 1) . "'") !== false,
    "datetime('now','-1 seconds') -> um segundo atras (literal)");
ok(tz('SELECT datetime(e.at) AS a FROM bot_events e') === 'SELECT (e.at) AS a FROM bot_events e',
    'datetime(<coluna>) -> identidade');

[$sqlFiltro, $planoFiltro] = tp('SELECT COUNT(*) FROM bot_events e WHERE date(datetime(e.at, ?)) >= date(?)');
ok($sqlFiltro === 'SELECT COUNT(*) FROM bot_events e WHERE date(DATE_ADD(e.at, INTERVAL ? SECOND)) >= date(?)',
    'filtro de data -> DATE_ADD(col, INTERVAL ? SECOND)');
ok($planoFiltro === [['segundos', 0], ['comum', 1]], 'filtro de data -> plano [segundos, comum]');
$params = bot_sql_params_do_plano($planoFiltro, ['-3 hours', '2026-10-06']);
ok($params === [-10800, '2026-10-06'], "'-3 hours' vira -10800 segundos no execute");

ok(tz("SELECT datetime(e.at, '-03:00') FROM bot_events e") === "SELECT CONVERT_TZ(e.at, '+00:00', '-03:00') FROM bot_events e",
    "datetime(<coluna>, '<offset>') -> CONVERT_TZ");
ok(tz('SELECT (julianday(s.at) - julianday(MAX(e.at))) * 86400 AS seg FROM route_assignments s') ===
    'SELECT TIMESTAMPDIFF(SECOND, MAX(e.at), s.at) AS seg FROM route_assignments s',
    'julianday -> TIMESTAMPDIFF (argumentos invertem)');
ok(tz("SELECT (julianday('now') - julianday(at)) * 86400 AS idade FROM chats") !== '' &&
    stripos(tz("SELECT (julianday('now') - julianday(at)) * 86400 AS idade FROM chats"), 'TIMESTAMPDIFF') !== false,
    "julianday('now') tambem traduz");

$erro = false;
try {
    bot_sql_traduz('SELECT datetime(e.at, ?) FROM bot_events e', true);
    bot_sql_params_do_plano([['segundos', 0]], ['x']);
} catch (RuntimeException $e) {
    $erro = true;
}
ok($erro, 'modificador invalido falha alto (nao vira SQL silencioso)');

// --------------------------------------------------------------------- DML
ok(tz('INSERT OR IGNORE INTO group_teams (group_id, team_id) VALUES (?, ?)') ===
    'INSERT IGNORE INTO group_teams (group_id, team_id) VALUES (?, ?)',
    'INSERT OR IGNORE -> INSERT IGNORE');
ok(tz('INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value') ===
    'INSERT INTO bot_settings (`key`, value) VALUES (?, ?) AS new ON DUPLICATE KEY UPDATE value = new.value',
    'upsert -> AS new + ON DUPLICATE KEY UPDATE (+ crase no `key`)');
ok(tz('INSERT INTO t (a) VALUES (?) ON CONFLICT (a) DO NOTHING') ===
    'INSERT INTO t (a) VALUES (?) ON DUPLICATE KEY UPDATE a = a',
    'DO NOTHING -> no-op (a = a)');
ok(tz("UPDATE route_assignments SET case_key = 'a:' || case_key WHERE closed_at IS NULL") ===
    "UPDATE route_assignments SET case_key = CONCAT('a:', case_key) WHERE closed_at IS NULL",
    "`||` -> CONCAT");
ok(tz('BEGIN') === 'START TRANSACTION', 'BEGIN -> START TRANSACTION');

// ------------------------------------------------------------------ catalogo
$tabela = tz("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?");
ok(strpos($tabela, 'information_schema.tables') !== false && substr($tabela, -14) === 'table_name = ?',
    'sqlite_master (existe tabela) -> information_schema.tables');
$info = tz('PRAGMA table_info(queues)');
ok(strpos($info, 'information_schema.columns') !== false && strpos($info, 'column_name AS name') !== false,
    'PRAGMA table_info -> information_schema.columns');
ok(tz('PRAGMA foreign_keys = ON') === 'SET FOREIGN_KEY_CHECKS = 1', 'PRAGMA foreign_keys=ON -> SET 1');
ok(tz('PRAGMA foreign_keys = OFF') === 'SET FOREIGN_KEY_CHECKS = 0', 'PRAGMA foreign_keys=OFF -> SET 0');
ok(tz('PRAGMA journal_mode = WAL') === '', 'PRAGMA journal_mode -> nao existe (no-op)');
ok(tz('PRAGMA busy_timeout = 5000') === 'SET SESSION innodb_lock_wait_timeout = 5',
    'PRAGMA busy_timeout -> lock wait em segundos');

// ---------------------------------------------------------------------- DDL
$ddl = tz(
    "CREATE TABLE IF NOT EXISTS menu_options (\n"
    . "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    . "  queue_address VARCHAR(191) REFERENCES queues(address) ON DELETE CASCADE,\n"
    . "  label TEXT NOT NULL DEFAULT '',\n"
    . "  created_at TEXT NOT NULL DEFAULT (datetime('now'))\n"
    . ")"
);
ok(strpos($ddl, 'BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY') !== false, 'DDL: AUTOINCREMENT -> AUTO_INCREMENT (BIGINT)');
ok(strpos($ddl, 'queue_address VARCHAR(191) COLLATE utf8mb4_bin') !== false,
    'DDL: coluna de FK ganha COLLATE utf8mb4_bin');
ok(substr_count($ddl, 'REFERENCES') === 1
    && strpos(
        $ddl,
        'CONSTRAINT fk_menu_options_queue_address FOREIGN KEY (queue_address)'
        . ' REFERENCES queues(address) ON DELETE CASCADE'
    ) !== false,
    'DDL: REFERENCES inline -> CONSTRAINT ... FOREIGN KEY');
ok(strpos($ddl, "DEFAULT (UTC_TIMESTAMP())") !== false, "DDL: DEFAULT (datetime('now')) -> UTC_TIMESTAMP()");
ok(strpos($ddl, "DEFAULT ('')") !== false, "DDL: DEFAULT '' -> DEFAULT ('') em coluna TEXT");
ok(stripos($ddl, 'AUTOINCREMENT') === false, 'DDL: nenhum AUTOINCREMENT sobrou');

// Chave de TEXTO inline: vira VARCHAR(191) **e** ganha a colacao (regra 23 + 5.5c).
$chaveTexto = tz('CREATE TABLE IF NOT EXISTS analyst_load (address TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT \'\')');
ok(stripos($chaveTexto, 'address VARCHAR(191) COLLATE utf8mb4_bin PRIMARY KEY') !== false,
    'DDL: TEXT PRIMARY KEY -> VARCHAR(191) + COLLATE _bin');

$idxUnico = tz("CREATE UNIQUE INDEX IF NOT EXISTS pending_routes_active ON pending_routes (kind, chat_id)"
    . " WHERE status NOT IN ('done', 'failed', 'cancelled')");
ok(strpos($idxUnico, '((CASE WHEN status NOT IN') !== false && strpos($idxUnico, 'IF NOT EXISTS') === false,
    'indice unico parcial -> funcional (CASE WHEN) sem IF NOT EXISTS');
// `IS NOT NULL` na coluna que **e** a chave: nao precisa de funcao.
$idxComum = tz('CREATE UNIQUE INDEX IF NOT EXISTS menu_options_per_queue ON menu_options (queue_address, sort_order)'
    . ' WHERE queue_address IS NOT NULL');
ok($idxComum === 'CREATE UNIQUE INDEX menu_options_per_queue ON menu_options (queue_address, sort_order)',
    'indice parcial de `coluna IS NOT NULL` na chave -> indice comum');
// `IS NOT NULL` em coluna FORA da chave: o parcial vira funcional (5.3, item 4).
$idxFunc = tz('CREATE INDEX IF NOT EXISTS chats_classified_idx ON chats (updated_at) WHERE classification IS NOT NULL');
ok($idxFunc === 'CREATE INDEX chats_classified_idx ON chats ((CASE WHEN classification IS NOT NULL THEN updated_at END))',
    'indice parcial de coluna fora da chave -> funcional');

$alter = tz('ALTER TABLE chats ADD COLUMN ask_option_id INTEGER REFERENCES menu_options(id) ON DELETE SET NULL');
ok($alter === 'ALTER TABLE chats ADD COLUMN ask_option_id BIGINT NULL,'
    . ' ADD CONSTRAINT fk_chats_ask_option_id FOREIGN KEY (ask_option_id)'
    . ' REFERENCES menu_options(id) ON DELETE SET NULL',
    'ALTER ADD COLUMN com REFERENCES -> coluna + ADD CONSTRAINT');

$colacao = tz('ALTER TABLE analyst_load ADD COLUMN casos_toa_keys TEXT NOT NULL DEFAULT \'\'');
ok($colacao === "ALTER TABLE analyst_load ADD COLUMN casos_toa_keys TEXT NOT NULL DEFAULT ('')",
    'ALTER de coluna comum nao ganha COLLATE (so a lista 5.5c)');

ok(tz('SELECT value FROM bot_settings WHERE key = ?') === 'SELECT value FROM bot_settings WHERE `key` = ?',
    '`key` reservado -> crase');
ok(tz('CREATE TABLE t (a INTEGER, PRIMARY KEY (a))') !== '' &&
    stripos(tz('CREATE TABLE t (a INTEGER, PRIMARY KEY (a))'), 'PRIMARY KEY') !== false,
    'PRIMARY KEY nao vira crase');

$semEquivalente = false;
try {
    bot_sql_traduz('INSERT OR REPLACE INTO t (a) VALUES (?)', true);
} catch (RuntimeException $e) {
    $semEquivalente = true;
}
ok($semEquivalente, 'INSERT OR REPLACE falha alto (nao tem equivalente honesto)');

// ----------------------------------------------------------- busca por nome
putenv('MYSQL_TEST');
ok(bot_like_ci('a.assignee') === 'a.assignee', 'bot_like_ci: sqlite nao mexe na expressao');
putenv('MYSQL_TEST=1');
ok(bot_like_ci('a.assignee') === 'a.assignee COLLATE utf8mb4_0900_ai_ci',
    'bot_like_ci: mysql aplica a colacao case-insensitive');
putenv('MYSQL_TEST');

echo "\n" . ($failed === 0 ? 'SQLCOMPAT PHP OK' : ('FALHAS: ' . $failed)) . "\n";
exit($failed === 0 ? 0 : 1);
