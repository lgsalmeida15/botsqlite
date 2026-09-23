<?php
declare(strict_types=1);

/**
 * Contrato do fluxo "Pedir info e transferir" com perguntas:
 * menu_option_questions, chats.ask_option_id e os templates do resumo.
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

function throws(callable $fn): bool
{
    try {
        $fn();
        return false;
    } catch (Throwable $e) {
        return true;
    }
}

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_ask_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

ok(bot_table_exists($pdo, 'menu_option_questions'), 'tabela menu_option_questions criada');
$chatCols = bot_table_columns($pdo, 'chats');
ok(isset($chatCols['ask_option_id']), 'chats.ask_option_id criada');
ok(isset($chatCols['last_reply_at']), 'chats.last_reply_at criada (timeout por silencio)');

$tpl = [];
foreach ($pdo->query('SELECT slug FROM templates') as $r) {
    $tpl[$r['slug']] = true;
}
ok(isset($tpl['ask_received']) && isset($tpl['ask_transferring']), 'templates ask_received e ask_transferring')
;
$pdo->exec("INSERT INTO menus (name, active) VALUES ('M1', 1)");
$pdo->exec("INSERT INTO menu_options (menu_id, sort_order, label, classification, action) VALUES (1, 1, 'Apoio', 'apoio', 'ask_and_transfer')");
$opt = (int) $pdo->lastInsertId();
$ins = $pdo->prepare(
    'INSERT INTO menu_option_questions (option_id, sort_order, prompt, kind, choices) VALUES (?, ?, ?, ?, ?)'
);
$ins->execute([$opt, 1, 'Qual o ID da atividade?', 'int', '']);
ok(true, 'pergunta valida aceita');
ok(
    throws(fn() => $pdo->exec("INSERT INTO menu_option_questions (option_id, sort_order, prompt, kind) VALUES ({$opt}, 9, 'x', 'data')")),
    'CHECK recusa kind invalido'
);
ok(throws(fn() => $ins->execute([$opt, 1, 'repetida', 'text', ''])), 'UNIQUE recusa ordem repetida na mesma opcao');
ok(throws(fn() => $ins->execute([999, 1, 'x', 'text', ''])), 'FK recusa opcao inexistente');

$ins->execute([$opt, 3, 'Observacao', 'text', '']);
$ins->execute([$opt, 2, 'Qual a rede?', 'choice', 'HFC|GPON']);
$ordem = array_map('intval', $pdo->query(
    "SELECT sort_order FROM menu_option_questions WHERE option_id = {$opt} ORDER BY sort_order"
)->fetchAll(PDO::FETCH_COLUMN));
ok($ordem === [1, 2, 3], 'perguntas ordenadas por sort_order');
ok(
    (string) $pdo->query("SELECT choices FROM menu_option_questions WHERE option_id = {$opt} AND sort_order = 2")->fetchColumn() === 'HFC|GPON',
    'alternativas guardadas separadas por |'
);

// --- 50 perguntas na mesma opcao (teto do painel) -------------------------
$ins50 = $pdo->prepare(
    'INSERT INTO menu_option_questions (option_id, sort_order, prompt, kind, choices) VALUES (?, ?, ?, ?, ?)'
);
for ($i50 = 53; $i50 >= 4; $i50--) {
    $ins50->execute([$opt, $i50, 'Pergunta ' . $i50, 'text', '']);
}
$total50 = (int) $pdo->query("SELECT COUNT(*) FROM menu_option_questions WHERE option_id = {$opt}")->fetchColumn();
ok($total50 === 53, 'grava 50 perguntas novas (53 no total com as 3 anteriores)');
$lista50 = $pdo->query("SELECT sort_order FROM menu_option_questions WHERE option_id = {$opt} ORDER BY sort_order")->fetchAll(PDO::FETCH_COLUMN);
ok((int) $lista50[0] === 1 && (int) $lista50[52] === 53, 'ordem lida da 1a a 53a pergunta');

$pdo->exec("DELETE FROM menu_options WHERE id = {$opt}");
ok((int) $pdo->query('SELECT COUNT(*) FROM menu_option_questions')->fetchColumn() === 0, 'excluir a opcao limpa as perguntas (CASCADE)');

@unlink($path);
exit($failed === 0 ? 0 : 1);