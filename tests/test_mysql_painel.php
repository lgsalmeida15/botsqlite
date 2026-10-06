<?php
declare(strict_types=1);

/**
 * Gate da Fase 3: o painel rodando em MySQL 8.
 *
 * Gated por `MYSQL_TEST=1` + `MYSQL_TEST_DATABASE` (nome com "test"): sem isso o
 * arquivo sai com 0 e nao toca servidor nenhum. Com o gate ligado, roda contra a
 * base de teste (o mesmo `botsqlite_test` do migrador) e verifica:
 *
 *  1. boot real (`bot_pdo()` + `ensure_schema`) e idempotencia do script;
 *  2. render de CADA aba pelo harness, sem fatal/warning/SQLSTATE;
 *  3. busca por nome com MAIUSCULA/minuscula (a colacao das colunas `_bin`);
 *  4. filtro de data `-3 hours` (o `datetime(<coluna>, ?)` -> `DATE_ADD`);
 *  5. gravacao (membro de time) chegando no banco;
 *  6. `rowCount()` com semantica de linhas casadas (`FOUND_ROWS`);
 *  7. coluna criada por migracao (`analyst_load.casos_toa_keys`).
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

if (!bot_db_test_mode() || bot_db_backend() !== 'mysql') {
    echo "pulado: defina MYSQL_TEST=1 e MYSQL_TEST_DATABASE (base de teste) para rodar.\n";
    exit(0);
}
$cfg = bot_mysql_config();
if (stripos((string) $cfg['db'], 'test') === false) {
    echo "pulado: MYSQL_TEST_DATABASE precisa ter \"test\" no nome.\n";
    exit(0);
}
echo 'base de teste: ' . $cfg['db'] . ' em ' . $cfg['host'] . ':' . $cfg['port'] . "\n";

$pdo = bot_pdo();
ok(bot_pdo_is_mysql($pdo), 'bot_pdo() abriu o MySQL (backend=mysql)');

// Idempotencia: o mesmo script de novo nao pode criar/reclamar nada (o DDL do
// migrador ja criou; `IF NOT EXISTS` e o catalogo dos indices cuidam do resto).
$schema = (string) file_get_contents(bot_root() . DIRECTORY_SEPARATOR . 'schema.sql');
$comandos = bot_executa_script($pdo, $schema);
ok($comandos > 40, 'ensure_schema reaplicado sem erro (' . $comandos . ' comandos)');

// ---------------------------------------------------------------- semeadura
$fila = 'h:smoke_painel';
$addrA = 'u:Smoke.Painel@claro.com';
$addrB = 'u:Beta.Outro@claro.com';
$addrNovo = 'u:Smoke.Novo@claro.com';
$timeNome = 'Time smoke painel';
$dataLocal = gmdate('Y-m-d', time() - 3 * 3600);
$ontemLocal = gmdate('Y-m-d', time() - 3 * 3600 - 86400);

$pdo->exec("DELETE FROM bot_events WHERE detail LIKE 'smoke-painel-%'");
$pdo->exec("DELETE FROM team_members WHERE address IN ('$addrA', '$addrNovo')");
$pdo->exec("DELETE FROM analyst_load WHERE address IN ('$addrA', '$addrB')");
$pdo->exec("DELETE FROM team_queues WHERE queue_address = '$fila'");
$pdo->exec("DELETE FROM teams WHERE name = '$timeNome'");
$pdo->exec("DELETE FROM queues WHERE address = '$fila'");
$pdo->exec("DELETE FROM bot_users WHERE login = 'painel_smoke'");

$gid = bot_super_group_id($pdo);
if ($gid < 1) {
    $pdo->exec("INSERT INTO permission_groups (name, is_super) VALUES ('Super smoke', 1)");
    $gid = (int) $pdo->lastInsertId();
}
$pdo->prepare(
    'INSERT INTO bot_users (login, password_hash, group_id, display_name) VALUES (?, ?, ?, ?)'
)->execute(['painel_smoke', password_hash('x', PASSWORD_DEFAULT), $gid, 'Smoke Painel']);
$uid = (int) $pdo->lastInsertId();

$pdo->prepare('INSERT INTO queues (address, name, enabled) VALUES (?, ?, 0)')->execute([$fila, 'Fila smoke']);
$pdo->prepare('INSERT INTO teams (name, active) VALUES (?, 1)')->execute([$timeNome]);
$tid = (int) $pdo->lastInsertId();
$pdo->prepare('INSERT INTO team_members (team_id, address, name, active) VALUES (?, ?, ?, 1)')
    ->execute([$tid, $addrA, 'Smoke Painel']);
$pdo->prepare('INSERT INTO analyst_load (address, name, team_id, online) VALUES (?, ?, ?, 1)')
    ->execute([$addrA, 'Smoke Painel', $tid]);
$pdo->prepare('INSERT INTO analyst_load (address, name, team_id, online) VALUES (?, ?, ?, 1)')
    ->execute([$addrB, 'Beta Outro', $tid]);
// Dois eventos com hora conhecida: 12:00 UTC cai no dia local, 01:00 UTC cai no
// dia local ANTERIOR (o filtro do painel desloca 3 h para tras).
$pdo->prepare('INSERT INTO bot_events (chat_id, queue_address, reason, at, detail) VALUES (?, ?, ?, ?, ?)')
    ->execute([900001, $fila, 'classified', $dataLocal . ' 12:00:00', 'smoke-painel-meio']);
$pdo->prepare('INSERT INTO bot_events (chat_id, queue_address, reason, at, detail) VALUES (?, ?, ?, ?, ?)')
    ->execute([900002, $fila, 'classified', $dataLocal . ' 01:00:00', 'smoke-painel-virada']);
ok($uid > 0 && $tid > 0, 'semeadura ok (usuario #' . $uid . ', time #' . $tid . ')');
ok(
    isset(bot_table_columns($pdo, 'analyst_load')['casos_toa_keys']),
    'analyst_load.casos_toa_keys existe (coluna das migracoes no MySQL)'
);

/** Roda o harness (uma aba) contra a base de teste e devolve `[html, codigo]`. */
function render(string $pagina, int $uid, string $extra = '', string $post = ''): array
{
    // O POST vai por ambiente: argumento vazio no meio da linha nao sobrevive ao
    // shell do Windows (o harness recebia GET em vez de POST).
    putenv('PANEL_RENDER_POST=' . $post);
    $cmd = escapeshellarg(PHP_BINARY) . ' ' . escapeshellarg(__DIR__ . '/_panel_render.php') . ' '
        . escapeshellarg($pagina) . ' ' . $uid
        . ($extra !== '' ? ' ' . escapeshellarg($extra) : '') . ' 2>&1';
    $out = [];
    $code = 0;
    exec($cmd, $out, $code);
    putenv('PANEL_RENDER_POST');
    return [implode("\n", $out), $code];
}

/** O HTML do painel saiu limpo (sem fatal, warning ou erro de SQL)? */
function limpo(array $res): bool
{
    [$html, $code] = $res;
    if ($code !== 0) {
        return false;
    }
    foreach (['Fatal error', 'Uncaught', 'Parse error', 'Warning:', 'SQLSTATE', 'deprecated'] as $marca) {
        if (stripos($html, $marca) !== false) {
            return false;
        }
    }
    return true;
}

// ------------------------------------------------------------------- abas
$marcadores = [
    'dashboard' => 'Fila de espera do chat',
    'queues' => 'Novas filas entram desabilitadas',
    'operators' => 'Cadastrar',
    'menus' => 'Novo menu',
    'teams' => 'Plantão: pessoas + filas',
    'routing' => 'Rotas sem entrega',
    'settings' => 'Timeout de resposta',
    'templates' => 'greeting',
    'activity' => 'Todos os eventos',
    'load' => 'Leitura automática a cada',
    'chats' => 'Filtrar',
    'users' => 'Novo usuario',
    'groups' => 'Novo grupo',
];
foreach ($marcadores as $pagina => $marca) {
    $res = render($pagina, $uid);
    $html = $res[0];
    ok(limpo($res), "aba $pagina: renderizou sem fatal/warning/SQLSTATE");
    ok(stripos($html, $marca) !== false, "aba $pagina: conteudo presente ('" . $marca . "')");
}

// --------------------------------------------- busca por nome (a colacao)
$semFiltro = render('dashboard', $uid, 'de=' . $ontemLocal . '&ate=' . $dataLocal);
ok(strpos($semFiltro[0], $addrA) !== false, 'dashboard sem filtro: mostra o analista Smoke');
$comFiltro = render('dashboard', $uid, 'nome=smoke&de=' . $ontemLocal . '&ate=' . $dataLocal);
ok(strpos($comFiltro[0], $addrA) !== false, 'busca por nome MINUSCULO acha o address com MAIUSCULA');
ok(strpos($comFiltro[0], $addrB) === false, 'busca por nome nao traz o outro analista');
ok(strpos($comFiltro[0], 'Beta Outro') === false, 'busca por nome nao traz o nome do outro analista');

// ----------------------------------------------- filtro de data (`-3 hours`)
$hoje = render('activity', $uid, 'de=' . $dataLocal . '&ate=' . $dataLocal);
ok(limpo($hoje), 'aba activity com de/ate: renderizou sem erro');
ok(
    strpos($hoje[0], 'smoke-painel-meio') !== false,
    'filtro de data: evento das 12:00 UTC aparece no dia local'
);
ok(
    strpos($hoje[0], 'smoke-painel-virada') === false,
    'filtro de data: evento das 01:00 UTC (22:00 local do dia anterior) fica FORA'
);
$ontem = render('activity', $uid, 'de=' . $ontemLocal . '&ate=' . $ontemLocal);
ok(
    strpos($ontem[0], 'smoke-painel-virada') !== false,
    'filtro de data: o evento da virada aparece no dia local anterior'
);

// ---------------------------------------------------- gravacao (POST) real
$post = 'action=add_member&team_id=' . $tid . '&address=' . rawurlencode($addrNovo)
    . '&member_name=' . rawurlencode('Smoke Novo');
$gravou = render('teams', $uid, '', $post);
ok(limpo($gravou), 'POST add_member: renderizou sem erro');
$conferiu = $pdo->prepare('SELECT COUNT(*) AS n FROM team_members WHERE address = ?');
$conferiu->execute([$addrNovo]);
ok((int) $conferiu->fetch()['n'] === 1, 'POST add_member: linha gravada no MySQL');
$depois = render('teams', $uid);
ok(strpos($depois[0], $addrNovo) !== false, 'membro novo aparece na aba Times');

// ------------------------------------------- rowCount e linhas casadas
$up = $pdo->prepare('UPDATE queues SET name = ? WHERE address = ?');
$up->execute(['Fila smoke', $fila]);
ok($up->rowCount() >= 1, 'UPDATE que grava o MESMO valor conta linha casada (FOUND_ROWS)');
$nelas = $pdo->prepare('SELECT COUNT(*) AS n FROM information_schema.key_column_usage'
    . ' WHERE constraint_schema = DATABASE() AND referenced_table_name IS NOT NULL');
$nelas->execute();
ok((int) $nelas->fetch()['n'] >= 30, 'FKs gravadas no destino (>= 30 colunas com referencia)');

echo "\n" . ($failed === 0 ? 'MYSQL PAINEL OK' : ('FALHAS: ' . $failed)) . "\n";
exit($failed === 0 ? 0 : 1);
