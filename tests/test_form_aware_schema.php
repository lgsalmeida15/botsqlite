<?php
declare(strict_types=1);

/**
 * Contrato da abertura form-aware: vinculo chat<->formulario, rota presa ao dono
 * do caso e os textos novos da aba Templates (espelho de python/db.py).
 */
require __DIR__ . '/../php/bootstrap.php';

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

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_form_aware_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

$chatCols = bot_table_columns($pdo, 'chats');
ok(isset($chatCols['linked_form_chat_id']), 'chats.linked_form_chat_id criada');
$routeCols = bot_table_columns($pdo, 'pending_routes');
ok(isset($routeCols['expects_owner']), 'pending_routes.expects_owner criada');

$tpl = [];
foreach ($pdo->query('SELECT slug, body FROM templates') as $r) {
    $tpl[$r['slug']] = $r['body'];
}
// A abertura form-aware foi REMOVIDA: as colunas legadas ficam, os textos saem.
ok(!isset($tpl['form_pending']), 'template form_pending removido do banco');
ok(!isset($tpl['form_pending_wait']), 'template form_pending_wait removido do banco');
ok(!isset($tpl['form_waiting']), 'template form_waiting removido do banco');
ok(isset($tpl['greeting']) && isset($tpl['menu']), 'templates do fluxo normal seguem no banco');

// O vinculo do chat aceita NULL (chat sem formulario) e o valor do formulario.
$pdo->exec("INSERT INTO chats (chat_id, status) VALUES (10, 'awaiting_form_choice')");
$pdo->exec('UPDATE chats SET linked_form_chat_id = 99 WHERE chat_id = 10');
ok(
    (int) $pdo->query('SELECT linked_form_chat_id FROM chats WHERE chat_id = 10')->fetchColumn() === 99,
    'vinculo gravado no chat'
);
$pdo->exec('UPDATE chats SET linked_form_chat_id = NULL WHERE chat_id = 10');
ok(
    $pdo->query('SELECT linked_form_chat_id FROM chats WHERE chat_id = 10')->fetchColumn() === null,
    'vinculo aceita NULL'
);

// A rota presa ao dono do caso nasce com 0 (comportamento antigo preservado).
$pdo->exec(
    "INSERT INTO pending_routes (kind, chat_id, queue_address, detail) VALUES ('chat', 10, 'h:q1', 'teste')"
);
ok(
    (int) $pdo->query('SELECT expects_owner FROM pending_routes WHERE chat_id = 10')->fetchColumn() === 0,
    'expects_owner nasce 0'
);

// Idempotencia: a migracao roda de novo sem recriar texto nenhum da abertura.
bot_migrate_form_aware($pdo);
ok(
    (int) $pdo->query("SELECT COUNT(*) FROM templates WHERE slug = 'form_pending'")->fetchColumn() === 0,
    'migracao idempotente (texto da abertura nao volta)'
);

// Janela de vida do formulario (o TOA nao avisa a saida do analista): as duas
// chaves nascem no schema e o painel/bot leem daqui. Espelho de schema.sql.
$cfgFa = [];
foreach ($pdo->query('SELECT key, value FROM bot_settings') as $r) {
    $cfgFa[$r['key']] = $r['value'];
}
ok(($cfgFa['form_idle_offer_min'] ?? null) === '30', 'form_idle_offer_min nasce 30');
ok(($cfgFa['form_idle_close_min'] ?? null) === '120', 'form_idle_close_min nasce 120');
ok(
    ($cfgFa['form_idle_owner_close_min'] ?? null) === '240',
    'form_idle_owner_close_min nasce 240 (formulario parado COM analista dentro)'
);

// Banco antigo: o texto da abertura volta a ser gravado e a migracao o remove.
$pdo->prepare("INSERT OR REPLACE INTO templates (slug, body) VALUES ('form_pending', ?)")
    ->execute(['linha1' . "\n" . 'linha2']);
bot_migrate_form_aware($pdo);
$sobraram = (int) $pdo->query("SELECT COUNT(*) FROM templates WHERE slug = 'form_pending'")->fetchColumn();
ok($sobraram === 0, 'migracao remove o texto da abertura de banco antigo');

// Reparo do dialeto legado da chave de caso: `u:...` (address sem prefixo `a:`)
// passa a casar na afinidade — antes o caso aberto no dialeto antigo ficava
// invisivel e o formulario do mesmo tecnico virava caso duplicado.
$pdo->exec(
    "INSERT INTO route_assignments (kind, chat_id, queue_address, assignee, case_key)"
    . " VALUES ('form', 992, 'h:q1', 'u:ana@claro.com', 'u:tec@claro.com')"
);
$pdo->exec(
    "INSERT INTO pending_routes (kind, chat_id, status, case_key)"
    . " VALUES ('form', 993, 'waiting', 'u:outro@claro.com')"
);
bot_migrate_form_aware($pdo);
ok(
    (string) $pdo->query('SELECT case_key FROM route_assignments WHERE chat_id = 992')->fetchColumn()
        === 'a:u:tec@claro.com',
    'case_key legado do ledger normalizado para a:u:...'
);
ok(
    (string) $pdo->query('SELECT case_key FROM pending_routes WHERE chat_id = 993')->fetchColumn()
        === 'a:u:outro@claro.com',
    'case_key legado da rota normalizado para a:u:...'
);

@unlink($path);
exit($failed === 0 ? 0 : 1);
