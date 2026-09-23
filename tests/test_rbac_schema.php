<?php
declare(strict_types=1);

/**
 * Contrato de banco do RBAC do painel.
 *
 * Fase 1 da evolucao do painel: schema.sql + bot_migrate_rbac (php/bootstrap.php)
 * + _migrate_rbac (python/db.py) precisam criar as tabelas de grupos/escopo,
 * adicionar as colunas em bot_users e promover os usuarios legados para Super
 * Admin exatamente uma vez.
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

function throws(callable $fn): bool
{
    try {
        $fn();
        return false;
    } catch (Throwable $e) {
        return true;
    }
}

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_rbac_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

// --- Tabelas novas ---------------------------------------------------------
foreach (
    ['permission_groups', 'group_permissions', 'user_teams', 'user_queues', 'group_teams', 'group_queues', 'group_menus'] as $table
) {
    ok(bot_table_exists($pdo, $table), "tabela {$table} criada");
}

// --- Colunas novas em bot_users --------------------------------------------
$cols = bot_table_columns($pdo, 'bot_users');
foreach (['group_id', 'display_name', 'operator_address', 'last_login_at'] as $col) {
    ok(isset($cols[$col]), "bot_users.{$col}");
}

// --- Grupo Super Admin (seed) ---------------------------------------------
$superId = bot_super_group_id($pdo);
ok($superId > 0, 'grupo Super Admin existe (is_super = 1)');
$super = $pdo->query("SELECT name, active, is_super FROM permission_groups WHERE id = {$superId}")->fetch();
ok(
    $super && $super['name'] === 'Super Admin' && (int) $super['is_super'] === 1 && (int) $super['active'] === 1,
    'Super Admin ativo com is_super = 1'
);

// --- Flag da migracao ------------------------------------------------------
$flagColumn = $pdo->query("SELECT value FROM bot_settings WHERE key = 'rbac_seeded'")->fetchColumn();
ok($flagColumn !== false, 'bot_settings.rbac_seeded existe');
ok((string) $flagColumn === '1', 'base nova (sem usuarios legados) fecha a flag sem promover ninguem');

// --- Idempotencia ----------------------------------------------------------
$idem = true;
try {
    bot_migrate_rbac($pdo);
    bot_migrate_rbac($pdo);
} catch (Throwable $e) {
    $idem = false;
}
ok($idem, 'bot_migrate_rbac roda duas vezes sem erro (ALTER idempotente)');

$raw = (string) file_get_contents(bot_root() . DIRECTORY_SEPARATOR . 'schema.sql');
$sqlOk = true;
try {
    foreach (preg_split('/;\s*/', (string) preg_replace('/^\s*--.*$/m', '', $raw)) as $stmt) {
        $stmt = trim($stmt);
        if ($stmt !== '') {
            $pdo->exec($stmt);
        }
    }
} catch (Throwable $e) {
    $sqlOk = false;
}
ok($sqlOk, 'schema.sql reaplicado sem erro (IF NOT EXISTS / INSERT OR IGNORE)');

// --- Promocao dos usuarios legados (anti-lockout) --------------------------
$pdo->exec("INSERT INTO bot_users (login, password_hash, active) VALUES ('legado', 'x', 1)");
$pdo->exec("INSERT INTO bot_users (login, password_hash, active, group_id) VALUES ('ja_tinha', 'x', 1, NULL)");
$pdo->prepare("UPDATE bot_settings SET value = '0' WHERE key = 'rbac_seeded'")->execute();
bot_migrate_rbac($pdo);
$legadoGroup = $pdo->query("SELECT group_id FROM bot_users WHERE login = 'legado'")->fetchColumn();
ok((int) $legadoGroup === $superId, 'usuario legado promovido para Super Admin');
ok(bot_has_active_super_admin($pdo), 'bot_has_active_super_admin = true depois da promocao');
$flagAfter = (string) $pdo->query("SELECT value FROM bot_settings WHERE key = 'rbac_seeded'")->fetchColumn();
ok($flagAfter === '1', 'flag fechada depois da promocao');

$pdo->exec("INSERT INTO bot_users (login, password_hash, active) VALUES ('novo', 'x', 1)");
bot_migrate_rbac($pdo);
$novoGroup = $pdo->query("SELECT group_id FROM bot_users WHERE login = 'novo'")->fetchColumn();
ok($novoGroup === null, 'usuario criado depois do RBAC continua sem grupo (sem acesso)');

// --- Vinculos de escopo ----------------------------------------------------
$pdo->exec("INSERT INTO teams (name, active) VALUES ('T1', 1)");
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q1', 'Q1', 1)");
$uid = (int) $pdo->query("SELECT id FROM bot_users WHERE login = 'legado'")->fetchColumn();
$pdo->exec("INSERT INTO user_teams (user_id, team_id) VALUES ({$uid}, 1)");
$pdo->exec("INSERT INTO user_queues (user_id, queue_address) VALUES ({$uid}, 'h:q1')");
ok(true, 'user_teams e user_queues aceitam vinculos validos');
ok(
    throws(fn() => $pdo->prepare('INSERT INTO user_teams (user_id, team_id) VALUES (?, ?)')->execute([$uid, 1])),
    'PK de user_teams recusa time repetido'
);
ok(
    throws(fn() => $pdo->exec("INSERT INTO user_teams (user_id, team_id) VALUES ({$uid}, 999)")),
    'FK de user_teams recusa time inexistente'
);
ok(
    throws(fn() => $pdo->exec("INSERT INTO user_queues (user_id, queue_address) VALUES ({$uid}, 'h:nada')")),
    'FK de user_queues recusa fila inexistente'
);
$escopo = $pdo->query('SELECT team_id FROM user_teams WHERE user_id = ' . $uid)->fetchAll(PDO::FETCH_COLUMN);
ok(count($escopo) === 1 && (int) $escopo[0] === 1, 'escopo do usuario lido de user_teams');

// --- Chaves de permissao do grupo -----------------------------------------
$pdo->exec("INSERT INTO group_permissions (group_id, perm_key) VALUES ({$superId}, 'page.dashboard')");
ok(
    throws(fn() => $pdo->exec("INSERT INTO group_permissions (group_id, perm_key) VALUES ({$superId}, 'page.dashboard')")),
    'PK de group_permissions recusa chave repetida'
);
ok(
    throws(fn() => $pdo->exec("INSERT INTO group_permissions (group_id, perm_key) VALUES (999, 'page.dashboard')")),
    'FK de group_permissions recusa grupo inexistente'
);

// --- Cascatas --------------------------------------------------------------
$pdo->exec("DELETE FROM permission_groups WHERE id = {$superId}");
$left = (int) $pdo->query("SELECT COUNT(*) FROM group_permissions WHERE group_id = {$superId}")->fetchColumn();
ok($left === 0, 'excluir o grupo limpa as permissoes (ON DELETE CASCADE)');
$orphan = $pdo->query("SELECT group_id FROM bot_users WHERE login = 'legado'")->fetchColumn();
ok($orphan === null, 'excluir o grupo solta os usuarios (ON DELETE SET NULL)');
ok(!bot_has_active_super_admin($pdo), 'sem grupo Super Admin, bot_has_active_super_admin = false');

$pdo->exec("DELETE FROM bot_users WHERE login = 'legado'");
$links = (int) $pdo->query("SELECT COUNT(*) FROM user_teams WHERE user_id = {$uid}")->fetchColumn();
ok($links === 0, 'excluir o usuario limpa user_teams (ON DELETE CASCADE)');

// --- Escopo por grupo (group_teams/group_queues/group_menus) --------------
$pdo->prepare("INSERT INTO permission_groups (name, active, is_super) VALUES ('G escopo', 1, 0)")->execute();
$gEsc = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO menus (name, active) VALUES ('M escopo', 1)");
$mEsc = (int) $pdo->lastInsertId();
$pdo->prepare('INSERT INTO group_teams (group_id, team_id) VALUES (?, 1)')->execute([$gEsc]);
$pdo->prepare("INSERT INTO group_queues (group_id, queue_address) VALUES (?, 'h:q1')")->execute([$gEsc]);
$pdo->prepare('INSERT INTO group_menus (group_id, menu_id) VALUES (?, ?)')->execute([$gEsc, $mEsc]);
ok(true, 'vinculos de escopo do grupo aceitos');
ok(
    throws(fn() => $pdo->prepare('INSERT INTO group_teams (group_id, team_id) VALUES (?, ?)')->execute([$gEsc, 1])),
    'PK de group_teams recusa time repetido'
);
ok(
    throws(fn() => $pdo->exec("INSERT INTO group_teams (group_id, team_id) VALUES ({$gEsc}, 999)")),
    'FK de group_teams recusa time inexistente'
);
ok(
    throws(fn() => $pdo->exec("INSERT INTO group_menus (group_id, menu_id) VALUES ({$gEsc}, 999)")),
    'FK de group_menus recusa menu inexistente'
);
$pdo->exec('DELETE FROM teams WHERE id = 1');
ok((int) $pdo->query('SELECT COUNT(*) FROM group_teams')->fetchColumn() === 0, 'excluir o time limpa group_teams (CASCADE)');
$pdo->exec("DELETE FROM menus WHERE id = {$mEsc}");
ok((int) $pdo->query('SELECT COUNT(*) FROM group_menus')->fetchColumn() === 0, 'excluir o menu limpa group_menus (CASCADE)');
$pdo->exec("DELETE FROM permission_groups WHERE id = {$gEsc}");
ok((int) $pdo->query('SELECT COUNT(*) FROM group_queues')->fetchColumn() === 0, 'excluir o grupo limpa group_queues (CASCADE)');

// --- Migracao: escopo legado do usuario -> grupo --------------------------
$pdo->prepare("INSERT INTO permission_groups (name, active, is_super) VALUES ('G migracao', 1, 0)")->execute();
$gMig = (int) $pdo->lastInsertId();
$pdo->prepare("INSERT INTO bot_users (login, password_hash, active, group_id) VALUES ('com_escopo', 'x', 1, ?)")->execute([$gMig]);
$uMig = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO teams (name, active) VALUES ('T migracao', 1)");
$tMig = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:qm', 'Q migracao', 1)");
$pdo->prepare('INSERT INTO user_teams (user_id, team_id) VALUES (?, ?)')->execute([$uMig, $tMig]);
$pdo->prepare("INSERT INTO user_queues (user_id, queue_address) VALUES (?, 'h:qm')")->execute([$uMig]);
$pdo->prepare("UPDATE bot_settings SET value = '0' WHERE key = 'rbac_scope_groups'")->execute();
bot_rbac_copy_user_scope_to_groups($pdo);
$gTeams = array_map('intval', $pdo->query("SELECT team_id FROM group_teams WHERE group_id = {$gMig}")->fetchAll(PDO::FETCH_COLUMN));
$gQueues = $pdo->query("SELECT queue_address FROM group_queues WHERE group_id = {$gMig}")->fetchAll(PDO::FETCH_COLUMN);
ok(in_array($tMig, $gTeams, true), 'escopo de time do usuario copiado para o grupo');
ok(in_array('h:qm', array_map('strval', $gQueues), true), 'escopo de fila do usuario copiado para o grupo');
$flagScope = (string) $pdo->query("SELECT value FROM bot_settings WHERE key = 'rbac_scope_groups'")->fetchColumn();
ok($flagScope === '1', 'flag rbac_scope_groups fechada apos a copia');
$pdo->prepare("UPDATE bot_settings SET value = '0' WHERE key = 'rbac_scope_groups'")->execute();
bot_rbac_copy_user_scope_to_groups($pdo);
ok((int) $pdo->query("SELECT COUNT(*) FROM group_teams WHERE group_id = {$gMig}")->fetchColumn() === 1, 'copia idempotente (nao duplica)');

@unlink($path);
exit($failed === 0 ? 0 : 1);
