<?php
declare(strict_types=1);

require dirname(__DIR__) . '/php/bootstrap.php';

$login = $argv[1] ?? '';
$pass = $argv[2] ?? '';
if ($login === '' || $pass === '') {
    fwrite(STDERR, "Uso: php bin/create-user.php <login> <senha>\n");
    exit(1);
}

$hash = password_hash($pass, PASSWORD_DEFAULT);
$pdo = bot_pdo();
$st = $pdo->prepare(
    'INSERT INTO bot_users (login, password_hash, active) VALUES (?, ?, 1)
     ON CONFLICT (login) DO UPDATE SET password_hash = excluded.password_hash, active = 1'
);
$st->execute([$login, $hash]);
echo "Usuário '{$login}' gravado.\n";
