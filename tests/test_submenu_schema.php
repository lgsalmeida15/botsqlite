<?php
declare(strict_types=1);

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

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_subtest_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

$cols = bot_table_columns($pdo, 'menu_options');
ok(isset($cols['parent_id']), 'menu_options.parent_id');
ok(isset($cols['reply_text_if_form']), 'menu_options.reply_text_if_form');
ok(isset($cols['action_if_form']), 'menu_options.action_if_form');
$chatCols = bot_table_columns($pdo, 'chats');
ok(isset($chatCols['menu_node_id']), 'chats.menu_node_id');
ok(isset($cols['script_slug']), 'menu_options.script_slug');
ok(isset($chatCols['script_slug']), 'chats.script_slug');
ok(isset($chatCols['script_field_index']), 'chats.script_field_index');
ok(isset($chatCols['script_answers']), 'chats.script_answers');
ok(isset($chatCols['script_uuid']), 'chats.script_uuid');
ok(isset($chatCols['script_started_at']), 'chats.script_started_at');

$sql = (string) $pdo->query("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'menu_options'")->fetchColumn();
ok(strpos($sql, 'submenu') !== false, 'CHECK action inclui submenu');
ok(strpos($sql, 'run_script') !== false, 'CHECK action inclui run_script');

$pdo->exec("INSERT INTO menus (name, active) VALUES ('M1', 1)");
$pdo->prepare(
    'INSERT INTO menu_options (menu_id, sort_order, label, classification, action, reply_text)
     VALUES (1, 1, \'Grupo\', \'\', \'submenu\', \'\')'
)->execute();
$parentId = (int) $pdo->lastInsertId();
$pdo->prepare(
    'INSERT INTO menu_options (menu_id, parent_id, sort_order, label, classification, action, reply_text)
     VALUES (1, ?, 1, \'Filho\', \'f\', \'reply_and_close\', \'\')'
)->execute([$parentId]);

$dup = false;
try {
    $pdo->prepare(
        'INSERT INTO menu_options (menu_id, parent_id, sort_order, label, classification, action, reply_text)
         VALUES (1, ?, 1, \'Dup\', \'d\', \'reply_and_close\', \'\')'
    )->execute([$parentId]);
} catch (PDOException $e) {
    $dup = true;
}
ok($dup, 'unicidade do filho no mesmo parent');

$n = (int) $pdo->query('SELECT COUNT(*) FROM menu_options WHERE sort_order = 1')->fetchColumn();
ok($n === 2, 'mesmo número na raiz e no filho é permitido');

$okScript = true;
try {
    $pdo->prepare(
        'INSERT INTO menu_options (menu_id, parent_id, sort_order, label, classification, action, reply_text, script_slug)
         VALUES (1, ?, 2, \'Apoio\', \'apoio\', \'run_script\', \'\', \'criar_apoio\')'
    )->execute([$parentId]);
} catch (PDOException $e) {
    $okScript = false;
}
ok($okScript, 'CHECK aceita action run_script');

@unlink($path);
exit($failed === 0 ? 0 : 1);
