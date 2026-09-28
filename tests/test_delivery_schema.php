<?php
declare(strict_types=1);

/**
 * Contrato da FASE A no painel: colunas da entrega, motivo de fechamento do
 * ledger, tabela de tentativas e o indice unico que impede duas entregas ativas
 * para o mesmo chat/formulario (a mesma trava que o motor Python usa).
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

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_delivery_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

$cols = bot_table_columns($pdo, 'pending_routes');
foreach (['case_key', 'requester_addr', 'invite_target', 'move_sent_at', 'move_fails', 'claimed_by', 'claim_expires_at', 'exclude_assignee'] as $c) {
    ok(isset($cols[$c]), "pending_routes.$c criada");
}
ok((int) $pdo->query('SELECT move_fails FROM pending_routes LIMIT 1')->fetchColumn() === 0, 'move_fails nasce 0');
$pdo->exec("INSERT INTO pending_routes (kind, chat_id) VALUES ('form', 991)");
ok(
    (int) $pdo->query('SELECT move_fails FROM pending_routes WHERE chat_id = 991')->fetchColumn() === 0,
    'move_fails nasce 0 na rota nova'
);
ok(
    (string) $pdo->query('SELECT exclude_assignee FROM pending_routes WHERE chat_id = 991')->fetchColumn() === '',
    'exclude_assignee nasce vazio (rota normal entrega para qualquer elegivel)'
);
ok(isset(bot_table_columns($pdo, 'route_assignments')['close_reason']), 'route_assignments.close_reason criada');
ok(bot_table_exists($pdo, 'delivery_attempts'), 'delivery_attempts criada');

$cfg = [];
foreach ($pdo->query('SELECT key, value FROM bot_settings') as $r) {
    $cfg[$r['key']] = $r['value'];
}
ok(($cfg['delivery_claim_ttl_sec'] ?? '') === '300', 'bot_settings.delivery_claim_ttl_sec (5 min)');
ok(($cfg['case_stale_confirm_sec'] ?? '') === '300', 'bot_settings.case_stale_confirm_sec (5 min)');

$ins = $pdo->prepare(
    "INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status) VALUES (?, ?, 'h:q1', 1, ?)"
);
$ins->execute(['chat', 1, 'waiting']);
$duplicou = false;
try {
    $ins->execute(['chat', 1, 'waiting']);
} catch (PDOException $e) {
    $duplicou = true;
}
ok($duplicou, 'indice unico ativo bloqueia a segunda rota do mesmo chat');

$ins->execute(['chat', 1, 'done']);
$ins->execute(['chat', 1, 'failed']);
ok(true, 'status terminal (done/failed) permite historico do mesmo chat');

$pdo->exec('DROP INDEX IF EXISTS pending_routes_active');
$pdo->prepare('DELETE FROM pending_routes')->execute();
$ins->execute(['form', 9, 'waiting']);
$ins->execute(['form', 9, 'waiting']);
bot_migrate_delivery($pdo);
$ativas = (int) $pdo->query(
    "SELECT COUNT(*) FROM pending_routes WHERE status NOT IN ('done','failed','cancelled')"
)->fetchColumn();
ok($ativas === 1, 'migracao saneia duplicatas antigas antes de criar o indice');
$idx = [];
foreach ($pdo->query("SELECT name FROM sqlite_master WHERE type = 'index'") as $r) {
    $idx[] = (string) $r['name'];
}
ok(in_array('pending_routes_active', $idx, true), 'indice unico recriado pela migracao');
bot_migrate_delivery($pdo);
ok((int) $pdo->query('SELECT COUNT(*) FROM pending_routes')->fetchColumn() === 2, 'migracao e idempotente');

$pdo->prepare(
    "INSERT INTO delivery_attempts (kind, chat_id, op, target, request_id, result, latency_ms)"
    . " VALUES ('chat', 1, 'move_chat', 'u:a@x', '123', 'timeout', 8000)"
)->execute();
$linha = $pdo->query('SELECT * FROM delivery_attempts ORDER BY id DESC LIMIT 1')->fetch();
ok($linha['result'] === 'timeout' && $linha['target'] === 'u:a@x', 'tentativa grava resultado e alvo');

@unlink($path);
exit($failed === 0 ? 0 : 1);
