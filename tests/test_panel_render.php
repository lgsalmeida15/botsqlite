<?php
declare(strict_types=1);

/**
 * Render das abas do painel: sem erro fatal, HTML balanceado e conteudo visivel.
 *
 * Cobre duas regressoes reais de 22/09:
 *   - `mb_substr` sem a extensao mbstring derrubava a aba Roteamento;
 *   - `<div class="d-none">` sem fechamento escondia Atividade/Chats/Dashboard
 *     inteiros (aparecia so o cabecalho).
 *
 * Roda contra um banco TEMPORARIO (SQLITE_PATH no ambiente, ver bot_sqlite_path),
 * nunca contra data/helptoa.sqlite.
 */
require __DIR__ . '/../php/bootstrap.php';
require __DIR__ . '/../php/ui.php';

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

// ui_clip: o corte de texto do painel nao pode depender de mbstring.
ok(ui_clip('abcdef', 3) === 'abc', 'ui_clip corta texto simples');
ok(ui_clip('acao', 99) === 'acao', 'ui_clip nao corta quando cabe');
ok(ui_clip('ação e coração', 4) === 'ação', 'ui_clip corta por caractere (acentos) sem mbstring');
ok(ui_clip('x', 0) === '', 'ui_clip com tamanho 0 devolve vazio');
ok(function_exists('mb_substr') || ui_clip('Ação', 3) === 'Açã', 'ui_clip funciona sem a extensao mbstring');

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_panel_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

// Super admin (acesso total) + dados minimos para as abas renderizarem conteudo.
$pdo->exec("INSERT INTO permission_groups (name, is_super) VALUES ('Super', 1)");
$gid = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO bot_users (login, password_hash, group_id, display_name) VALUES ('teste', 'x', {$gid}, 'Teste')");
$uid = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q1', 'Fila 1', 1)");
$pdo->exec("INSERT INTO menus (name, active) VALUES ('M1', 1)");
$pdo->exec("INSERT INTO menu_queues (menu_id, queue_address) VALUES (1, 'h:q1')");
$pdo->exec("INSERT INTO menu_options (menu_id, sort_order, label, classification, action, reply_text) VALUES (1, 1, 'Validação', 'TESTE', 'reply_and_close', 'ok')");
$pdo->exec("INSERT INTO teams (name, active) VALUES ('T1', 1)");
$pdo->exec("INSERT INTO team_queues (team_id, queue_address) VALUES (1, 'h:q1')");
$pdo->exec("INSERT INTO team_members (team_id, address, name, active) VALUES (1, 'u:ana@claro.com', 'Ana', 1)");
$pdo->exec("INSERT INTO chats (chat_id, queue_address, status, classification) VALUES (10, 'h:q1', 'awaiting_reply', 'TESTE')");
$pdo->exec("INSERT INTO chats (chat_id, queue_address, status, linked_form_chat_id) VALUES (11, 'h:q1', 'closed', 99)");
$pdo->exec("INSERT INTO bot_events (chat_id, queue_address, reason, detail) VALUES (10, 'h:q1', 'form_aware_skip', 'abertos=1; na_fila=0')");
$pdo->exec("INSERT INTO bot_events (chat_id, queue_address, reason, detail) VALUES (11, 'h:q1', 'transferido', 'detalhe bem longo para exercitar o corte de texto do painel sem mbstring')");
$pdo->exec("INSERT INTO pending_routes (kind, chat_id, queue_address, status, detail) VALUES ('chat', 10, 'h:q1', 'failed', 'tentativas esgotadas; fallback=close')");
$pdo->exec("INSERT INTO delivery_attempts (kind, chat_id, op, target, result) VALUES ('chat', 10, 'move_chat', 'u:ana@claro.com', 'ok')");
$pdo->exec("INSERT INTO route_assignments (kind, chat_id, queue_address, team_id, assignee, case_key) VALUES ('form', 99, 'h:q1', 1, 'u:ana@claro.com', 'a:u:tec@claro.com')");
$pdo->exec("INSERT INTO route_assignments (kind, chat_id, queue_address, team_id, assignee, case_key) VALUES ('chat', 10, 'h:q1', 1, 'u:ana@claro.com', 'a:u:tec@claro.com')");

// Marcador de conteudo por aba: se o d-none ficar aberto, o marcador some.
$marcadores = [
    'dashboard' => 'Fila de espera do chat',
    'queues' => 'Novas filas entram desabilitadas',
    'operators' => 'Cadastrar',
    'menus' => 'Novo menu',
    'teams' => 'Plantão: pessoas + filas',
    'routing' => 'Rotas sem entrega',
    'settings' => 'Timeout de resposta',
    'templates' => 'form_pending',
    'activity' => 'Todos os eventos',
    'chats' => 'Filtrar',
];

putenv('SQLITE_PATH=' . $path);
$php = PHP_BINARY;
$harness = __DIR__ . DIRECTORY_SEPARATOR . '_panel_render.php';

foreach ($marcadores as $pPage => $marca) {
    $cmd = escapeshellarg($php) . ' ' . escapeshellarg($harness) . ' ' . escapeshellarg($pPage) . ' ' . $uid . ' 2>&1';
    $out = [];
    $code = 0;
    exec($cmd, $out, $code);
    $html = implode("\n", $out);
    $erro = (stripos($html, 'Fatal error') !== false)
        || (stripos($html, 'Uncaught') !== false)
        || (stripos($html, 'Warning:') !== false)
        || (stripos($html, 'Parse error') !== false);
    ok($code === 0 && !$erro, "aba {$pPage}: renderizou sem erro fatal/warning");
    if ($code !== 0 || $erro) {
        $primeira = '';
        foreach ($out as $linha) {
            if (stripos($linha, 'error') !== false || stripos($linha, 'Uncaught') !== false) {
                $primeira = trim($linha);
                break;
            }
        }
        echo "      " . $primeira . "\n";
        continue;
    }
    $abre = substr_count($html, '<div');
    $fecha = substr_count($html, '</div>');
    ok($abre === $fecha, "aba {$pPage}: <div> balanceado (abre={$abre} fecha={$fecha})");
    $pos = strpos($html, '<div class="d-none">');
    if ($pos !== false) {
        $fimOculto = strpos($html, '</div>', $pos);
        $posMarca = strpos($html, $marca);
        ok(
            $fimOculto !== false && $posMarca !== false && $posMarca > $fimOculto,
            "aba {$pPage}: conteudo fora do bloco oculto (marcador depois do fechamento)"
        );
    } else {
        ok(strpos($html, $marca) !== false, "aba {$pPage}: conteudo esperado presente");
    }
}

@unlink($path);
exit($failed === 0 ? 0 : 1);
