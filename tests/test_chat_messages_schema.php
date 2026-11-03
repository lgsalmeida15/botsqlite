<?php
declare(strict_types=1);

/**
 * Contrato de `chat_messages` (reproducao do chat no painel, em outro repositorio):
 * colunas, dedupe por `(chat_id, dedupe_key)` e round-trip de anexo binario.
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

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_chat_messages_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

$cols = bot_table_columns($pdo, 'chat_messages');
foreach (
    [
        'chat_id', 'kind', 'direction', 'queue_address', 'message_id', 'dedupe_key',
        'author_addr', 'author_name', 'is_bot', 'system_message', 'text',
        'has_attachment', 'attachment_name', 'attachment_mime', 'attachment_size',
        'attachment_data', 'attachment_fetch_status', 'msg_time', 'at',
    ] as $col
) {
    ok(isset($cols[$col]), "chat_messages.$col criada");
}

$ins = $pdo->prepare(
    'INSERT INTO chat_messages (chat_id, kind, direction, queue_address, author_addr, text, dedupe_key, msg_time)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)'
);
$ins->execute([10, 'chat', 'in', 'h:q1', 'u:tec@claro.com', 'bom dia', 'h:abc', '2026-01-01 00:00:00']);
$ins->execute([10, 'chat', 'in', 'h:q1', 'u:ana@claro.com', 'pode confirmar?', 'h:def', '2026-01-01 00:01:00']);
ok(
    (int) $pdo->query('SELECT COUNT(*) FROM chat_messages WHERE chat_id = 10')->fetchColumn() === 2,
    'duas mensagens distintas gravadas'
);

try {
    $ins->execute([10, 'chat', 'in', 'h:q1', 'u:tec@claro.com', 'bom dia (repetida)', 'h:abc', '2026-01-01 00:00:05']);
    ok(false, 'dedupe_key repetido no mesmo chat_id deveria falhar por UNIQUE');
} catch (PDOException $e) {
    ok(true, 'dedupe_key repetido no mesmo chat_id e rejeitado (UNIQUE chat_id+dedupe_key)');
}

$ins->execute([11, 'chat', 'in', 'h:q1', 'u:tec@claro.com', 'outro chat', 'h:abc', '2026-01-01 00:00:00']);
ok(
    (int) $pdo->query('SELECT COUNT(*) FROM chat_messages WHERE dedupe_key = \'h:abc\'')->fetchColumn() === 2,
    'mesmo dedupe_key em chat_id diferente nao colide (UNIQUE e por chat_id+dedupe_key)'
);

// Anexo binario (BLOB/MEDIUMBLOB) sobrevive ao round-trip via PDO::PARAM_LOB.
$bin = random_bytes(256);
$stAnexo = $pdo->prepare(
    "INSERT INTO chat_messages (chat_id, kind, direction, author_addr, dedupe_key, has_attachment,
                                 attachment_name, attachment_mime, attachment_size, attachment_data, attachment_fetch_status)
     VALUES (?, 'chat', 'in', ?, ?, 1, ?, ?, ?, ?, 'ok')"
);
$stAnexo->bindValue(1, 12);
$stAnexo->bindValue(2, 'u:tec@claro.com');
$stAnexo->bindValue(3, 'h:anexo');
$stAnexo->bindValue(4, 'print.png');
$stAnexo->bindValue(5, 'image/png');
$stAnexo->bindValue(6, strlen($bin));
$stAnexo->bindValue(7, $bin, PDO::PARAM_LOB);
$stAnexo->execute();
$lida = $pdo->query("SELECT attachment_data, attachment_size FROM chat_messages WHERE dedupe_key = 'h:anexo'")->fetch();
$dadoLido = is_resource($lida['attachment_data']) ? stream_get_contents($lida['attachment_data']) : $lida['attachment_data'];
ok($dadoLido === $bin, 'anexo binario (MEDIUMBLOB) volta identico ao gravado');
ok((int) $lida['attachment_size'] === strlen($bin), 'attachment_size gravado confere com o tamanho real');

// kind/direction fora do CHECK sao rejeitados (mesma regra do bot Python).
try {
    $pdo->prepare("INSERT INTO chat_messages (chat_id, kind, direction, dedupe_key) VALUES (99, 'invalido', 'in', 'h:xx')")->execute();
    ok(false, 'kind fora de (chat, form) deveria falhar pelo CHECK');
} catch (PDOException $e) {
    ok(true, 'kind fora de (chat, form) e rejeitado pelo CHECK');
}

@unlink($path);
exit($failed === 0 ? 0 : 1);
