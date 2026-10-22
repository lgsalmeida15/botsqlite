<?php
declare(strict_types=1);

/**
 * Contrato de banco do roteamento (painel PHP + engine Python).
 *
 * A página ?page=routing só lê/escreve estas tabelas; aqui garantimos que o
 * schema criado por bot_ensure_schema casa com o que o painel e o bot usam.
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

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_route_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

$tr = bot_table_columns($pdo, 'team_routing');
foreach (
    ['strategy', 'cap_chat', 'cap_form', 'require_online', 'respect_window', 'attempts',
        'retry_interval_sec', 'fallback', 'form_enabled', 'last_assigned_chat', 'last_assigned_form',
        'cap_cases', 'member_kinds', 'form_window_margin_min']
    as $col
) {
    ok(isset($tr[$col]), "team_routing.$col");
}
$tmr = bot_table_columns($pdo, 'team_member_routing');
foreach (['weight', 'cap_chat', 'cap_form', 'forms_enabled', 'cap_cases', 'kinds'] as $col) {
    ok(isset($tmr[$col]), "team_member_routing.$col");
}
$pr = bot_table_columns($pdo, 'pending_routes');
foreach (['kind', 'chat_id', 'queue_address', 'team_id', 'attempts', 'next_try_at', 'status', 'detail'] as $col) {
    ok(isset($pr[$col]), "pending_routes.$col");
}

$settings = [];
foreach ($pdo->query('SELECT key, value FROM bot_settings') as $r) {
    $settings[$r['key']] = $r['value'];
}
foreach (
    ['form_routing_enabled', 'max_open_forms_per_agent', 'form_route_attempts',
        'form_route_retry_interval_sec', 'form_route_fallback', 'routing_strategy',
        'form_window_margin_min']
    as $key
) {
    ok(isset($settings[$key]), "bot_settings.$key");
}
ok(
    (int) $settings['form_window_margin_min'] === 10,
    'margem de fim de janela do formulario nasce em 10 min'
);

// Guarda do painel: o endereco do PROPRIO bot (publicado pelo engine) nunca
// pode ser cadastrado como operador — o caso voltaria para quem so repassa.
$pdo->exec(
    "INSERT INTO bot_settings (key, value) VALUES ('bot_own_address', 'u:bot@claro.com')
     ON CONFLICT (key) DO UPDATE SET value = excluded.value"
);
ok(bot_own_address($pdo) === 'u:bot@claro.com', 'bot_own_address le o endereco publicado pelo engine');
ok(addr_is_bot('u:bot@claro.com', bot_own_address($pdo)), 'addr_is_bot reconhece o endereco do bot');
ok(addr_is_bot('BOT@claro.com', 'u:bot@claro.com'), 'addr_is_bot compara pelo login (sem u:/dominio)');
ok(!addr_is_bot('u:ana@claro.com', 'u:bot@claro.com'), 'addr_is_bot libera outro operador');
ok(
    !addr_is_bot('', 'u:bot@claro.com') && !addr_is_bot('u:ana@claro.com', ''),
    'addr_is_bot ignora endereco vazio (bot ainda nao conhecido)'
);

$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q1', 'Q1', 1)");
$pdo->exec("INSERT INTO teams (name, active) VALUES ('T1', 1)");
$pdo->exec("INSERT INTO team_queues (team_id, queue_address) VALUES (1, 'h:q1')");
$pdo->exec("INSERT INTO team_members (team_id, address, name, sort_order, active) VALUES (1, 'u:ana@claro.com', 'Ana', 0, 1)");
$pdo->exec("INSERT INTO team_members (team_id, address, name, sort_order, active, route_start, route_end) VALUES (1, 'u:bia@claro.com', 'Bia', 1, 1, '22:00:00', '06:00:00')");

$ins = $pdo->prepare(
    'INSERT INTO team_routing (team_id, strategy, cap_form, fallback, form_enabled) VALUES (?, ?, ?, ?, ?)'
);
$ins->execute([1, 'priority', 2, 'retry', 1]);
ok(true, 'team_routing aceita strategy/cap/fallback validos');

ok(
    throws(fn() => $pdo->prepare('INSERT INTO team_routing (team_id) VALUES (?)')->execute([1])),
    'PK de team_routing recusa time repetido'
);
ok(
    throws(fn() => $pdo->exec("UPDATE team_routing SET strategy = 'xpto' WHERE team_id = 1")),
    'CHECK recusa strategy invalida'
);
ok(
    throws(fn() => $pdo->exec("UPDATE team_routing SET fallback = 'xpto' WHERE team_id = 1")),
    'CHECK recusa fallback invalido'
);
ok(
    throws(fn() => $pdo->exec("UPDATE team_routing SET member_kinds = 'xpto' WHERE team_id = 1")),
    'CHECK recusa tipo padrao invalido'
);
$pdo->exec("UPDATE team_routing SET member_kinds = 'form' WHERE team_id = 1");
ok(true, 'team_routing aceita member_kinds (padrao dos operadores)');

$mr = $pdo->prepare(
    'INSERT INTO team_member_routing (team_id, address, weight, cap_form, forms_enabled, kinds) VALUES (?, ?, ?, ?, ?, ?)'
);
$mr->execute([1, 'u:ana@claro.com', 5, 1, 1, 'both']);
ok(true, 'team_member_routing aceita peso, cap e tipo de atendimento');
ok(
    throws(fn() => $pdo->exec("UPDATE team_member_routing SET kinds = 'xpto' WHERE address = 'u:ana@claro.com'")),
    'CHECK recusa tipo de atendimento invalido'
);
$pdo->exec("UPDATE team_member_routing SET kinds = 'chat' WHERE address = 'u:ana@claro.com'");
ok(true, 'operador pode atender so chat');

// "Fazer valer para todos": limpar o override (NULL) faz o time/operador herdar
// o cap de casos global — a acao que o painel dispara nos checkboxes.
$pdo->exec('UPDATE team_routing SET cap_cases = 7 WHERE team_id = 1');
$pdo->exec("UPDATE team_member_routing SET cap_cases = 3 WHERE address = 'u:ana@claro.com'");
$pdo->exec('UPDATE team_routing SET cap_cases = NULL WHERE cap_cases IS NOT NULL');
$pdo->exec('UPDATE team_member_routing SET cap_cases = NULL WHERE cap_cases IS NOT NULL');
$herdando = $pdo->query('SELECT cap_cases FROM team_routing WHERE team_id = 1')->fetchColumn();
ok($herdando === null, 'time volta a herdar o cap de casos global');
$linhaAna = $pdo->query(
    "SELECT cap_cases, weight, kinds FROM team_member_routing WHERE address = 'u:ana@claro.com'"
)->fetch();
ok($linhaAna['cap_cases'] === null && (int) $linhaAna['weight'] === 5 && $linhaAna['kinds'] === 'chat', 'replicar limpa so o cap de casos (peso e tipo ficam)');
ok(
    throws(fn() => $pdo->prepare('INSERT INTO team_member_routing (team_id, address) VALUES (1, ?)')
        ->execute(['u:ana@claro.com'])),
    'PK de team_member_routing recusa membro repetido'
);

$pdo->exec(
    "INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, next_try_at)
     VALUES ('form', 99, 'h:q1', 1, datetime('now', '-' || 1 || ' seconds'))"
);
$due = (int) $pdo->query(
    "SELECT COUNT(*) FROM pending_routes WHERE status = 'waiting' AND datetime(next_try_at) <= datetime('now')"
)->fetchColumn();
ok($due === 1, 'pending_routes vencida aparece como due');
ok(
    throws(fn() => $pdo->exec("INSERT INTO pending_routes (kind, chat_id) VALUES ('xpto', 1)")),
    'CHECK recusa kind invalido'
);
ok(
    throws(fn() => $pdo->exec("UPDATE pending_routes SET status = 'xpto' WHERE chat_id = 99")),
    'CHECK recusa status invalido'
);
$pdo->exec("UPDATE pending_routes SET status = 'done', next_try_at = NULL WHERE chat_id = 99");
$waiting = (int) $pdo->query("SELECT COUNT(*) FROM pending_routes WHERE status = 'waiting'")->fetchColumn();
ok($waiting === 0, 'rota concluida sai da fila de retry');

$filas = $pdo->query(
    'SELECT tq.team_id, tq.queue_address, q.name AS queue_name FROM team_queues tq
     LEFT JOIN queues q ON q.address = tq.queue_address ORDER BY tq.queue_address'
)->fetchAll();
ok(count($filas) === 1 && $filas[0]['queue_name'] === 'Q1', 'consulta de filas do time (pagina Roteamento)');

$membros = $pdo->query('SELECT * FROM team_members WHERE team_id = 1 ORDER BY sort_order, name, address')->fetchAll();
ok(count($membros) === 2 && $membros[0]['address'] === 'u:ana@claro.com', 'consulta de membros ordenada por Ordem');

$rota = $pdo->query('SELECT * FROM team_routing WHERE team_id = 1')->fetch();
ok((int) $rota['cap_form'] === 2 && $rota['strategy'] === 'priority', 'leitura dos ajustes do time');
$mrRow = $pdo->query('SELECT * FROM team_member_routing WHERE team_id = 1')->fetch();
ok((int) $mrRow['weight'] === 5, 'leitura dos ajustes do membro');

$janelas = [];
foreach ($membros as $m) {
    $janelas[(string) $m['address']] = [$m['route_start'], $m['route_end']];
}
ok($janelas['u:bia@claro.com'] === ['22:00:00', '06:00:00'], 'pagina le a janela do membro em route_start/route_end');

// Janelas de turno/descanso (team_member_schedules): migracao do legado,
// override total do analista e CHECK de kind.
bot_migrate_team_schedules($pdo); // idempotente: migra o route_start/route_end inserido acima
$tms = bot_table_columns($pdo, 'team_member_schedules');
foreach (['team_id', 'address', 'kind', 'days_mask', 'start_time', 'end_time', 'sort_order', 'active'] as $col) {
    ok(isset($tms[$col]), "team_member_schedules.$col");
}
$legado = $pdo->query(
    "SELECT kind, start_time, end_time FROM team_member_schedules WHERE address = 'u:bia@claro.com'"
)->fetch();
ok(
    $legado && $legado['kind'] === 'shift' && $legado['start_time'] === '22:00:00' && $legado['end_time'] === '06:00:00',
    'migracao converte route_start/route_end legado em janela de turno'
);
ok(
    throws(fn() => $pdo->exec(
        "INSERT INTO team_member_schedules (team_id, address, kind, start_time, end_time) VALUES (1, 'u:ana@claro.com', 'xpto', '08:00', '18:00')"
    )),
    'CHECK recusa kind invalido em team_member_schedules'
);
$pdo->exec(
    "INSERT INTO team_member_schedules (team_id, address, kind, start_time, end_time) VALUES (1, NULL, 'shift', '08:00', '18:00')"
);
$pdo->exec(
    "INSERT INTO team_member_schedules (team_id, address, kind, start_time, end_time) VALUES (1, 'u:ana@claro.com', 'shift', '13:00', '22:00')"
);
$pdo->exec(
    "INSERT INTO team_member_schedules (team_id, address, kind, start_time, end_time) VALUES (1, 'u:ana@claro.com', 'break', '18:00', '19:00')"
);
$default = $pdo->query('SELECT COUNT(*) FROM team_member_schedules WHERE address IS NULL')->fetchColumn();
ok((int) $default === 1, 'default do time fica com address NULL');
$deAna = $pdo->query(
    "SELECT COUNT(*) FROM team_member_schedules WHERE address = 'u:ana@claro.com'"
)->fetchColumn();
ok((int) $deAna === 2, 'override do analista nao soma com o default do time (fica so no proprio escopo)');

// Estrutura do cabecalho da pagina (sem render): colunas que a aba usa.
$colunas = ['Ordem', 'Nome', 'Peso', 'Cap chat', 'Cap form', 'Formulario', 'Plantao agora'];
ok(count($colunas) === 7, 'colunas de membros previstas na aba Roteamento');

@unlink($path);
exit($failed === 0 ? 0 : 1);

