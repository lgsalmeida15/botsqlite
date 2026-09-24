<?php
declare(strict_types=1);

/**
 * Contrato da medicao de carga por analista (aba Carga).
 *
 * Espelho de python/db.py::_migrate_load: as duas migracoes precisam criar as
 * MESMAS colunas, porque o bot grava (python) e o painel le (php). Aqui tambem
 * entram os ajustes, as consultas que a aba usa e o RBAC da pagina.
 */
require __DIR__ . '/../php/bootstrap.php';
require __DIR__ . '/../php/auth.php';

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

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_load_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

$colunas = [
    'address', 'at', 'name', 'team_id', 'janela', 'online', 'chats_toa', 'forms_toa',
    'chats_ledger', 'forms_ledger', 'reservas', 'casos_total', 'form_ocioso_min',
    'form_presente_toa', 'toa_ok', 'detail',
];
$cols = bot_table_columns($pdo, 'analyst_load');
$faltando = array_values(array_diff($colunas, array_keys($cols)));
ok($faltando === [], 'analyst_load criada com as colunas da leitura' . ($faltando ? ': falta ' . implode(', ', $faltando) : ''));
$hist = bot_table_columns($pdo, 'analyst_load_history');
ok(
    array_diff(['id', 'at', 'address', 'chats_toa', 'forms_toa', 'chats_ledger', 'forms_ledger', 'casos_total', 'toa_ok'], array_keys($hist)) === [],
    'analyst_load_history criada com o historico curto'
);

// Idempotencia: garantir o schema de novo nao pode quebrar quem ja tem as tabelas.
bot_migrate_load($pdo);
ok(bot_table_exists($pdo, 'analyst_load') && bot_table_exists($pdo, 'analyst_load_history'), 'bot_migrate_load e idempotente');

$cfg = [];
foreach ($pdo->query('SELECT key, value FROM bot_settings') as $r) {
    $cfg[$r['key']] = (string) $r['value'];
}
ok(($cfg['load_poll_sec'] ?? '') === '30', 'bot_settings.load_poll_sec = 30 s');
ok(($cfg['form_absent_readings'] ?? '') === '2', 'bot_settings.form_absent_readings = 2 leituras');
ok(($cfg['analyst_load_keep'] ?? '') === '200', 'bot_settings.analyst_load_keep = 200 linhas');

// RBAC: pagina + acao no catalogo e no mapa de acoes.
$catalogo = bot_permission_catalog();
ok(isset($catalogo['page.load']), 'catalogo traz page.load');
ok(isset($catalogo['load.manage']), 'catalogo traz load.manage');
ok(in_array('load', bot_panel_pages(), true), 'load e pagina do painel');
ok(bot_action_permission('load', 'load_now') === 'load.manage', 'acao load_now exige load.manage');
ok(bot_action_permission('load', 'desconhecida') === null, 'acao desconhecida nao tem permissao mapeada');

// Dados: a leitura do bot + a consulta da aba (mesmo SQL de php/index.php).
$pdo->exec("INSERT INTO permission_groups (name, is_super) VALUES ('Super', 1)");
$gid = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO bot_users (login, password_hash, group_id, display_name) VALUES ('teste', 'x', {$gid}, 'Teste')");
$_SESSION['bot_user_id'] = (int) $pdo->lastInsertId();
bot_reset_panel_cache();
$pdo->exec("INSERT INTO teams (name, active) VALUES ('T1', 1)");
$pdo->exec("INSERT INTO teams (name, active) VALUES ('T2', 1)");
$pdo->exec(
    "INSERT INTO analyst_load (address, name, team_id, janela, online, chats_toa, forms_toa, chats_ledger, forms_ledger,"
    . " reservas, casos_total, form_ocioso_min, form_presente_toa, toa_ok, detail) VALUES"
    . " ('u:ana@claro.com', 'Ana', 1, '09:00-18:00', 1, 2, 1, 2, 1, 0, 2, 25.5, 1, 1, 'motivo=timer'),"
    . " ('u:bia@claro.com', 'Bia', 2, '', 0, 0, 0, 0, 1, 1, 1, 240.0, 0, 1, 'motivo=timer')"
);
$params = [];
$where = bot_scope_team_where($pdo, 'l.team_id', $params);
ok($where === '', 'super admin le todos os times (sem filtro de escopo)');
ok(count($params) === 0, 'escopo do super nao gera parametros');
$st = $pdo->prepare(
    'SELECT l.*, t.name AS team_name FROM analyst_load l'
    . ' LEFT JOIN teams t ON t.id = l.team_id WHERE 1 = 1' . $where
    . ' ORDER BY l.casos_total DESC, l.form_ocioso_min DESC, l.name'
);
$st->execute($params);
$linhas = $st->fetchAll();
ok(count($linhas) === 2, 'aba lista os dois analistas com o nome do time');
ok((string) $linhas[0]['address'] === 'u:ana@claro.com', 'ordenacao por casos desc (2 casos antes de 1)');
ok((string) $linhas[0]['team_name'] === 'T1', 'LEFT JOIN traz o nome do time');

// Card de divergencia: formulario no bot sem aparecer nas listas do TOA.
$divergentes = 0;
foreach ($linhas as $l) {
    if ((int) $l['forms_ledger'] > 0 && (int) $l['form_presente_toa'] === 0) {
        $divergentes++;
    }
}
ok($divergentes === 1, 'card de divergencia conta so o formulario ausente do TOA');

// Resumo dos formularios (cards da aba).
$pdo->exec("INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status, detail) VALUES ('form', 99, 'h:q1', 1, 'waiting', 'formulario')");
$pdo->exec("INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status, detail) VALUES ('chat', 10, 'h:q1', 1, 'waiting', 'chat')");
$pdo->exec("INSERT INTO route_assignments (kind, chat_id, queue_address, team_id, assignee, case_key) VALUES ('form', 99, 'h:q1', 1, 'u:ana@claro.com', 'a:u:tec@claro.com')");
$pdo->exec("INSERT INTO route_assignments (kind, chat_id, queue_address, team_id, assignee, case_key) VALUES ('chat', 10, 'h:q1', 1, 'u:ana@claro.com', 'a:u:tec@claro.com')");
$pdo->exec("INSERT INTO bot_events (reason, detail) VALUES ('form_closed', 'motivo=ocioso')");
$pdo->exec("INSERT INTO bot_events (reason, detail) VALUES ('form_resolvido_toa', 'ausente=2')");
ok(
    (int) $pdo->query("SELECT COUNT(*) FROM pending_routes WHERE kind = 'form' AND status = 'waiting'")->fetchColumn() === 1,
    'card: formularios na fila'
);
$cap = ['form' => 0, 'chat' => 0];
foreach ($pdo->query("SELECT kind, COUNT(*) AS n FROM route_assignments WHERE closed_at IS NULL GROUP BY kind") as $r) {
    $cap[(string) $r['kind']] = (int) $r['n'];
}
ok($cap['form'] === 1 && $cap['chat'] === 1, 'card: formularios e chats em atendimento (cap)');
$fechados = ['form_closed' => 0, 'form_resolvido_toa' => 0];
foreach ($pdo->query(
    "SELECT reason, COUNT(*) AS n FROM bot_events WHERE at >= datetime('now', '-1 hour')"
    . " AND reason IN ('form_closed', 'form_resolvido_toa') GROUP BY reason"
) as $r) {
    $fechados[(string) $r['reason']] = (int) $r['n'];
}
ok($fechados['form_closed'] === 1 && $fechados['form_resolvido_toa'] === 1, 'card: desfechos da ultima hora');

// Botao "Atualizar agora": o painel grava o job e o bot o consome (kind do motor).
$pdo->exec("INSERT INTO bot_jobs (kind) VALUES ('load_snapshot')");
$stJob = $pdo->query("SELECT kind, status FROM bot_jobs ORDER BY id DESC LIMIT 1")->fetch();
ok((string) $stJob['kind'] === 'load_snapshot' && (string) $stJob['status'] === 'pending', 'botao da aba cria job load_snapshot');

// Poda do historico (mesmo SQL de python/db.py::load_trim_history).
for ($i = 1; $i <= 5; $i++) {
    $pdo->prepare("INSERT INTO analyst_load_history (address, chats_toa, forms_toa, toa_ok) VALUES (?, ?, 0, 1)")
        ->execute(['u:ana@claro.com', $i]);
}
$pdo->exec("DELETE FROM analyst_load_history WHERE id NOT IN (SELECT id FROM analyst_load_history ORDER BY id DESC LIMIT 3)");
ok((int) $pdo->query('SELECT COUNT(*) FROM analyst_load_history')->fetchColumn() === 3, 'historico guarda so as leituras mais recentes');

@unlink($path);
exit($failed === 0 ? 0 : 1);
