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

// Nunca rodar contra o banco real: so com SQLITE_PATH explicito (temporario).
if (getenv('SQLITE_PATH') === false || getenv('SQLITE_PATH') === '') {
    fwrite(STDERR, "erro: defina SQLITE_PATH (banco de teste) antes de rodar o harness\n");
    exit(2);
}
// O `.env` do projeto tambem define SQLITE_PATH e vencia o ambiente: o override
// abaixo e o que garante que este harness leia o banco temporario.
putenv('SQLITE_PATH_OVERRIDE=' . (string) getenv('SQLITE_PATH'));

// Sessao fixa e JA gravada: o session_start() do index.php reabre a mesma
// sessao (sem warning de "sessao ja ativa") e o usuario chega logado.
session_id('panelrender' . getmypid());
session_start();
$_SESSION['bot_user_id'] = $userId;
$_SESSION['csrf'] = 'teste-csrf';
session_write_close();

$_GET = ['page' => $page];
$_SERVER['REQUEST_METHOD'] = 'GET';

require __DIR__ . '/../php/index.php';
