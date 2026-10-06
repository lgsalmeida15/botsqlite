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

/** Modo MySQL de teste (`MYSQL_TEST=1`), lido antes de o painel carregar. */
function bot_render_modo_mysql(): bool
{
    $valor = getenv('MYSQL_TEST');
    if ($valor === false) {
        return false;
    }
    return !in_array(strtolower(trim($valor)), ['', '0', 'false', 'no', 'off'], true);
}

// Nunca rodar contra o banco real. `SQLITE_PATH` explicito manda: e assim que as
// suites de painel rodam (banco temporario) — e o backend fica fixo em `sqlite`,
// mesmo com um `MYSQL_TEST=1` esquecido na sessao do terminal. Sem `SQLITE_PATH`,
// o caminho e o MySQL de teste (`MYSQL_TEST` + `MYSQL_TEST_DATABASE` com "test").
if (getenv('SQLITE_PATH') !== false && getenv('SQLITE_PATH') !== '') {
    putenv('MYSQL_TEST');
    putenv('DB_BACKEND=sqlite');
    // O `.env` do projeto tambem define SQLITE_PATH e vencia o ambiente: o
    // override abaixo e o que garante que este harness leia o banco temporario.
    putenv('SQLITE_PATH_OVERRIDE=' . (string) getenv('SQLITE_PATH'));
} else {
    if (!bot_render_modo_mysql()) {
        fwrite(STDERR, "erro: defina SQLITE_PATH (banco de teste) ou MYSQL_TEST=1\n");
        exit(2);
    }
    $base = (string) getenv('MYSQL_TEST_DATABASE');
    if ($base === '' || stripos($base, 'test') === false) {
        fwrite(STDERR, "erro: MYSQL_TEST exige MYSQL_TEST_DATABASE com \"test\" no nome\n");
        exit(2);
    }
}

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

// POST simulado (opcional, argv[5] ou `PANEL_RENDER_POST`): passa pelo mesmo CSRF
// da sessao gravada acima. Ex.: 'action=add_member&team_id=1&address=u:bot@claro.com'.
// A variavel de ambiente existe porque argumento VAZIO no meio da linha de comando
// nao sobrevive ao shell do Windows (o POST chegava como GET).
$postQuery = (string) ($argv[5] ?? '');
if ($postQuery === '') {
    $postQuery = (string) getenv('PANEL_RENDER_POST');
}
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
