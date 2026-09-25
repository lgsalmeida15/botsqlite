<?php
declare(strict_types=1);

/**
 * Contrato da fila de espera (Fase 2): colunas, ajustes globais, texto de posicao
 * e a regra FIFO por time (a mesma que o motor usa).
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

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_queue_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

$cols = bot_table_columns($pdo, 'pending_routes');
ok(isset($cols['expires_at']), 'pending_routes.expires_at criada');
ok(isset($cols['last_position']), 'pending_routes.last_position criada');
$chatCols = bot_table_columns($pdo, 'chats');
ok(isset($chatCols['moved_at']), 'chats.moved_at criada (transferencia parcial)');

$cfg = [];
foreach ($pdo->query('SELECT key, value FROM bot_settings') as $r) {
    $cfg[$r['key']] = $r['value'];
}
ok(($cfg['queue_wait_enabled'] ?? '') === 'true', 'bot_settings.queue_wait_enabled (ligado por padrao)');
ok(!array_key_exists('queue_max_wait_sec', $cfg), 'a espera nao tem prazo (queue_max_wait_sec removida)');
ok(($cfg['queue_show_pos'] ?? '') === 'false', 'bot_settings.queue_show_pos desligado por padrao (nao fala numero)');

$tpl = [];
foreach ($pdo->query('SELECT slug, body FROM templates') as $r) {
    $tpl[$r['slug']] = $r['body'];
}
ok(strpos((string) ($tpl['queue_position'] ?? ''), '{pos}') !== false, 'template queue_position traz {pos}');

$ins = $pdo->prepare(
    "INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, next_try_at, detail) VALUES (?, ?, ?, ?, datetime('now'), '')"
);
$ins->execute(['chat', 1, 'h:q1', 1]);
$a = (int) $pdo->lastInsertId();
$ins->execute(['chat', 2, 'h:q1', 1]);
$b = (int) $pdo->lastInsertId();
$ins->execute(['chat', 3, 'h:q2', 2]);
$c = (int) $pdo->lastInsertId();
$pos = static function (PDO $pdo, int $id): int {
    $st = $pdo->prepare(
        "SELECT COUNT(*) FROM pending_routes WHERE status = 'waiting' AND kind = 'chat' AND id < ?
           AND team_id IS (SELECT team_id FROM pending_routes WHERE id = ?)"
    );
    $st->execute([$id, $id]);
    return (int) $st->fetchColumn() + 1;
};
ok($pos($pdo, $a) === 1 && $pos($pdo, $b) === 2, 'posicao FIFO na mesma fila/time');
ok($pos($pdo, $c) === 1, 'fila de outro time tem posicao propria');
$pdo->prepare("UPDATE pending_routes SET status = 'done', next_try_at = NULL WHERE id = ?")->execute([$a]);
ok($pos($pdo, $b) === 1, 'atendido sai da fila e a posicao do proximo melhora');
ok(
    (int) $pdo->query("SELECT COUNT(*) FROM pending_routes WHERE status = 'waiting'")->fetchColumn() === 2,
    'duas rotas seguem esperando'
);

// SQL do card "Fila de espera (agora)" da aba Roteamento.
$pdo->exec("INSERT INTO teams (name, active) VALUES ('Plantao', 1)");
$tid = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q1', 'Fila 1', 1)");
$pdo->exec("INSERT INTO team_queues (team_id, queue_address) VALUES ($tid, 'h:q1')");
$pdo->exec("UPDATE pending_routes SET team_id = $tid WHERE team_id = 1");
$listaLimpa = [];
$st = $pdo->prepare(
    "SELECT r.*, c.status AS chat_status FROM pending_routes r
      LEFT JOIN chats c ON c.chat_id = r.chat_id
     WHERE r.status = ? ORDER BY r.id LIMIT 50"
);
$st->execute(['waiting']);
$listaCard = $st->fetchAll();
$cont = [];
$posCard = [];
foreach ($listaCard as $rq) {
    $chave = (string) $rq['kind'] . '|' . (string) $rq['team_id'];
    $cont[$chave] = ($cont[$chave] ?? 0) + 1;
    $posCard[(int) $rq['id']] = $cont[$chave];
}
ok(count($listaCard) === 2, 'card lista as rotas waiting (LEFT JOIN em chats funciona sem chat)');
ok(($posCard[$b] ?? 0) === 1 && ($posCard[$c] ?? 0) === 1, 'posicao do card e por tipo+time, na ordem de chegada');
ok(($posCard[$a] ?? null) === null, 'rota ja atendida sai do card');
ok((int) $pdo->query("SELECT COUNT(*) FROM pending_routes WHERE expires_at IS NULL")->fetchColumn() === 3, 'formulario/retry antigo segue sem expiracao');

$pdo->prepare("UPDATE pending_routes SET expires_at = datetime('now', '-1 seconds') WHERE id = ?")->execute([$b]);
$stVenc = $pdo->prepare("SELECT COUNT(*) FROM pending_routes WHERE id = ? AND status = 'waiting' AND expires_at <= datetime('now')");
$stVenc->execute([$b]);
ok((int) $stVenc->fetchColumn() === 1, 'Encerrar espera deixa a rota vencida para o motor expirar');
$stEsc = $pdo->prepare('SELECT COUNT(*) FROM pending_routes WHERE id = ? AND status = ? AND team_id IN (?)');
$stEsc->execute([$b, 'waiting', 999]);
ok((int) $stEsc->fetchColumn() === 0, 'Encerrar espera respeita o escopo de times do grupo');

// SQL das metricas de espera do Dashboard (queued -> queued_assigned).
$ev = $pdo->prepare(
    "INSERT INTO bot_events (at, chat_id, queue_address, reason, detail) VALUES (datetime('now', ?), ?, ?, ?, '')"
);
$ev->execute(['-600 seconds', 501, 'h:q1', 'queued']);
$ev->execute(['-300 seconds', 501, 'h:q1', 'queued_assigned']);
$ev->execute(['-120 seconds', 502, 'h:q1', 'queued']);
$ev->execute(['-60 seconds', 502, 'h:q1', 'queued_assigned']);
$tzBRT = new DateTimeZone('America/Sao_Paulo');
$hojeBRT = new DateTimeImmutable('now', $tzBRT);
$filtroEspera = ' AND date(datetime(s.at, ?)) BETWEEN date(?) AND date(?)';
$paramsEspera = [
    '-3 hours',
    $hojeBRT->modify('-1 day')->format('Y-m-d'),
    $hojeBRT->modify('+1 day')->format('Y-m-d'),
];
$stEspera = $pdo->prepare(
    "SELECT AVG(seg) AS media, MAX(seg) AS maxima, COUNT(*) AS casos FROM (
        SELECT (julianday(s.at) - julianday(MAX(e.at))) * 86400 AS seg
          FROM bot_events s
          JOIN bot_events e ON e.chat_id = s.chat_id AND e.reason = 'queued' AND e.at <= s.at
         WHERE s.reason = 'queued_assigned'" . $filtroEspera . "
         GROUP BY s.id
     )"
);
$stEspera->execute($paramsEspera);
$linhaEspera = $stEspera->fetch();
ok((int) ($linhaEspera['casos'] ?? 0) === 2, 'metricas: duas entregas na amostra');
ok(abs((float) $linhaEspera['maxima'] - 300) <= 3, 'metricas: maior espera ~300s');
ok(abs((float) $linhaEspera['media'] - 180) <= 3, 'metricas: espera media ~180s');
$stEntradas = $pdo->prepare("SELECT COUNT(*) FROM bot_events s WHERE s.reason = 'queued'" . $filtroEspera);
$stEntradas->execute($paramsEspera);
ok((int) $stEntradas->fetchColumn() === 2, 'metricas: duas entradas na fila no periodo');
$filtroFora = ' AND date(datetime(s.at, ?)) BETWEEN date(?) AND date(?)';
$stFora = $pdo->prepare("SELECT COUNT(*) FROM bot_events s WHERE s.reason = 'queued'" . $filtroFora);
$stFora->execute(['-3 hours', '2000-01-01', '2000-01-02']);
ok((int) $stFora->fetchColumn() === 0, 'metricas: periodo sem amostra fica zerado (card mostra "-")');

// Linhas apartadas: o formulario abre a PROPRIA linha (nao e o "N+1" dos chats).
$insLin = $pdo->prepare(
    "INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, next_try_at, detail) VALUES (?, ?, ?, ?, datetime('now'), '')"
);
$insLin->execute(['form', 90, 'h:q1', $tid]);
$f1 = (int) $pdo->lastInsertId();
$posForm = static function (PDO $pdo, int $id): int {
    $st = $pdo->prepare(
        "SELECT COUNT(*) FROM pending_routes WHERE status = 'waiting' AND kind = 'form' AND id < ?
           AND team_id IS (SELECT team_id FROM pending_routes WHERE id = ?)"
    );
    $st->execute([$id, $id]);
    return (int) $st->fetchColumn() + 1;
};
$linhaUnica = (int) $pdo->query(
    "SELECT COUNT(*) FROM pending_routes WHERE status = 'waiting' AND id < $f1"
)->fetchColumn() + 1;
ok($posForm($pdo, $f1) === 1, 'formulario e o 1o da linha de formularios');
ok($linhaUnica > 1, 'na linha unica ele seria o ultimo (o N+1 que motivou a separacao)');

@unlink($path);
exit($failed === 0 ? 0 : 1);