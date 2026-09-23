<?php
declare(strict_types=1);

require dirname(__DIR__) . '/php/bootstrap.php';

$login = $argv[1] ?? '';
$pass = $argv[2] ?? '';
if ($login === '' || $pass === '') {
    fwrite(
        STDERR,
        "Uso: php bin/create-user.php <login> <senha> [grupo]\n"
        . "  grupo: id ou nome do grupo de permissao (ex.: 1 ou \"Super Admin\").\n"
        . "  Sem grupo: se ainda nao houver Super Admin ativo, o usuario entra como Super Admin;\n"
        . "  caso contrario o usuario fica sem grupo (sem acesso) ate ser associado no painel.\n"
    );
    exit(1);
}

$pdo = bot_pdo();
$groupArg = trim((string) ($argv[3] ?? ''));
$groupId = null;
$groupNote = '';

if ($groupArg !== '') {
    if (ctype_digit($groupArg)) {
        $st = $pdo->prepare('SELECT id, name FROM permission_groups WHERE id = ?');
        $st->execute([(int) $groupArg]);
    } else {
        $st = $pdo->prepare('SELECT id, name FROM permission_groups WHERE name = ?');
        $st->execute([$groupArg]);
    }
    $group = $st->fetch();
    if (!$group) {
        fwrite(STDERR, "Grupo '{$groupArg}' nao encontrado. Veja os grupos no painel (Fase 2) ou use o id.\n");
        exit(1);
    }
    $groupId = (int) $group['id'];
    $groupNote = $group['name'] . ' (#' . $groupId . ')';
} else {
    $superId = bot_super_group_id($pdo);
    if ($superId > 0 && !bot_has_active_super_admin($pdo)) {
        $groupId = $superId;
        $groupNote = 'Super Admin (#' . $superId . ') — primeiro administrador do painel';
    }
}

$hash = password_hash($pass, PASSWORD_DEFAULT);
$st = $pdo->prepare(
    'INSERT INTO bot_users (login, password_hash, active, group_id) VALUES (?, ?, 1, ?)
     ON CONFLICT (login) DO UPDATE SET
       password_hash = excluded.password_hash,
       active = 1,
       group_id = COALESCE(excluded.group_id, bot_users.group_id)'
);
$st->execute([$login, $hash, $groupId]);

$st = $pdo->prepare(
    'SELECT u.group_id, g.name AS group_name
     FROM bot_users u LEFT JOIN permission_groups g ON g.id = u.group_id
     WHERE u.login = ?'
);
$st->execute([$login]);
$row = $st->fetch() ?: [];
$groupName = (string) ($row['group_name'] ?? '');
$userGroupId = $row['group_id'] ?? null;

echo "Usuário '{$login}' gravado.\n";
if ($groupName !== '' && $userGroupId !== null) {
    echo 'Grupo: ' . $groupName . ' (#' . (int) $userGroupId . ")\n";
} else {
    echo "Grupo: sem grupo — o usuário não acessa nada até ser associado a um grupo.\n";
}
if ($groupNote !== '') {
    echo "Grupo atribuído agora: {$groupNote}\n";
}

