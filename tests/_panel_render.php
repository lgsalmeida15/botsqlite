<?php
declare(strict_types=1);

/**
 * Harness interno: renderiza UMA pagina do painel em CLI, no banco indicado.
 *
 * Uso (chamado por tests/test_panel_render.php):
 *   SQLITE_PATH=<arquivo> php tests/_panel_render.php <pagina> <id_usuario>
 *
 * Sai com o HTML da pagina em stdout. Erro fatal do painel derruba o processo
 * (exit != 0), que e exatamente o que a suite quer detectar.
 */
$page = preg_replace('/[^a-z_]/', '', (string) ($argv[1] ?? 'dashboard')) ?: 'dashboard';
$userId = (int) ($argv[2] ?? 1);
$extraQuery = (string) ($argv[3] ?? '');   // ex.: 'teams[]=1' (filtros da pagina)
$dumpKey = (string) ($argv[4] ?? '');      // chave de $_SESSION para ecoar no fim

// Nunca rodar contra o banco real: so com SQLITE_PATH explicito (temporario).
if (getenv('SQLITE_PATH') === false || getenv('SQLITE_PATH') === '') {
    fwrite(STDERR, "erro: defina SQLITE_PATH (banco de teste) antes de rodar o harness\n");
    exit(2);
}
// O `.env` do projeto tambem define SQLITE_PATH e vencia o ambiente: o override
// abaixo e o que garante que este harness leia o banco temporario.
putenv('SQLITE_PATH_OVERRIDE=' . (string) getenv('SQLITE_PATH'));

// Sessao fixa e JA gravada: o session_start() do index.php reabre a mesma
// sessao (sem warning de "sessao ja ativa") e o usuario chega logado. O id pode
// vir do ambiente para o teste simular DUAS requisicoes na mesma sessao.
$sid = (string) getenv('PANEL_RENDER_SESSION');
session_id($sid !== '' ? $sid : ('panelrender' . getmypid()));
session_start();
$_SESSION['bot_user_id'] = $userId;
$_SESSION['csrf'] = 'teste-csrf';
session_write_close();

$_GET = ['page' => $page];
if ($extraQuery !== '') {
    $extras = [];
    parse_str($extraQuery, $extras);
    $_GET = array_merge($_GET, $extras);
}
$_SERVER['REQUEST_METHOD'] = 'GET';

// POST simulado (opcional, argv[5]): passa pelo mesmo CSRF da sessao gravada
// acima. Ex.: 'action=add_member&team_id=1&address=u:bot@claro.com'.
$postQuery = (string) ($argv[5] ?? '');
if ($postQuery !== '') {
    $posted = [];
    parse_str($postQuery, $posted);
    $_POST = array_merge(['csrf' => 'teste-csrf'], $posted);
    $_SERVER['REQUEST_METHOD'] = 'POST';
}

require __DIR__ . '/../php/index.php';

// Eco da sessao (quando pedido): prova que o filtro ficou gravado no servidor.
if ($dumpKey !== '' && array_key_exists($dumpKey, $_SESSION)) {
    echo "\n<!-- session:" . $dumpKey . '=' . json_encode($_SESSION[$dumpKey]) . " -->\n";
}
