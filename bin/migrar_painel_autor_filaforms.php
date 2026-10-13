<?php
declare(strict_types=1);

// ============================================================================
//  Migração do PAINEL (aplicar no banco de PRODUÇÃO sem atualizar o bot).
//
//  Mudanças de schema desta leva (todas idempotentes — rodar N vezes é seguro):
//    1. bot_events + author_addr/author_name  -> coluna AUTOR da aba Atividade;
//    2. tabela queue_load                     -> fonte do card FILA FORMS
//       (forms presentes na lista `pending` do TOA por fila, lidos no snapshot).
//
//  O bot de produção continua com o código antigo: ele não grava as colunas
//  novas (DEFAULT '' segura o INSERT antigo) nem escreve em queue_load — o
//  painel local, plugado no mesmo banco, enxerga as tabelas e mostra "-" no
//  card enquanto a coleta nova não rodar.
//
//  Uso:
//     php bin/migrar_painel_autor_filaforms.php             -> usa o .env da raiz
//     php bin/migrar_painel_autor_filaforms.php .env.prod-x -> outro ambiente
//
//  Conexão: mesmo backend do painel (DB_BACKEND / .env). SQLite ou MySQL 8.
// ============================================================================

// Aceita um arquivo .env alternativo como argv[1] (mesmo costume dos scripts
// python de watchdog): exporta as variáveis ANTES do bootstrap ler o .env.
$envArg = trim((string) ($argv[1] ?? ''));
if ($envArg !== '') {
    $envPath = dirname(__DIR__) . DIRECTORY_SEPARATOR . ltrim(str_replace(['/', '\\'], DIRECTORY_SEPARATOR, $envArg), DIRECTORY_SEPARATOR);
    if (!is_readable($envPath)) {
        fwrite(STDERR, "Arquivo .env não encontrado: {$envPath}\n");
        exit(1);
    }
    foreach (file($envPath, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $line) {
        $line = trim($line);
        if ($line === '' || $line[0] === '#') {
            continue;
        }
        [$k, $v] = array_pad(explode('=', $line, 2), 2, '');
        $k = trim($k);
        $v = trim($v, " \t\"'");
        if ($k !== '' && getenv($k) === false) {
            putenv("{$k}={$v}");
            $_ENV[$k] = $v;
        }
    }
}

require dirname(__DIR__) . '/php/bootstrap.php';

// Conecta SEM passar pelo bot_pdo() (que aplica o schema.sql inteiro): este
// script é cirúrgico — só as mudanças desta leva, com relatório do que fez.
$pdo = bot_db_abre(bot_sqlite_path());
$pdo->exec('PRAGMA foreign_keys = ON');
$pdo->exec('PRAGMA busy_timeout = 5000');

echo 'Backend: ' . bot_db_backend() . "\n";
echo 'Banco:   ' . (bot_db_backend() === 'mysql' ? bot_mysql_config()['host'] . '/' . bot_mysql_config()['db'] : bot_sqlite_path()) . "\n\n";

$antes = static function (PDO $pdo, string $tabela, string $coluna): bool {
    return isset(bot_table_columns($pdo, $tabela)[$coluna]);
};

// 1) bot_events: autor do chat/form no evento (coluna AUTOR da aba Atividade).
$tinhaAuthorAddr = $antes($pdo, 'bot_events', 'author_addr');
$tinhaAuthorName = $antes($pdo, 'bot_events', 'author_name');
bot_migrate_events_author($pdo);
echo $tinhaAuthorAddr && $tinhaAuthorName
    ? "ok  bot_events.author_addr/author_name já existiam\n"
    : "ok  bot_events + author_addr/author_name ("
        . (!$tinhaAuthorAddr ? 'author_addr ' : '')
        . (!$tinhaAuthorName ? 'author_name' : '') . ")\n";

// 2) queue_load: leitura da fila do TOA por endereco (card FILA FORMS).
$tinhaQueueLoad = bot_table_exists($pdo, 'queue_load');
bot_migrate_queue_load($pdo);
echo ($tinhaQueueLoad ? 'ok' : 'ok ') . " queue_load " . ($tinhaQueueLoad ? 'já existia' : 'criada') . "\n";

// Prova de leitura: o SELECT do dashboard tem que funcionar.
$pdo->query('SELECT queue_address, pending_forms, toa_ok, at FROM queue_load LIMIT 1');
echo "\nMigração concluída. Reinicie o painel (ou apenas recarregue a aba) para ver as mudanças.\n";
exit(0);
