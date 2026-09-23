<?php
declare(strict_types=1);

/**
 * Contrato das permissoes e do escopo de dados (php/auth.php + php/pages/users.php).
 *
 * Cobre: catalogo de chaves, mapa acao->permissao, bot_can (concedido/negado),
 * super-only (Usuarios/Grupos), escopo por time e fila avulsa, e as guardas de
 * dono do painel (ultimo Super Admin / proprio usuario).
 */
require __DIR__ . '/../php/bootstrap.php';
require __DIR__ . '/../php/auth.php';
require __DIR__ . '/../php/pages/users.php';
require __DIR__ . '/../php/pages/groups.php';

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

function login_as(int $userId): void
{
    $_SESSION["bot_user_id"] = $userId;
    bot_reset_panel_cache();
}

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_perm_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

// --- Catalogo cobre todas as paginas --------------------------------------
$cat = bot_permission_catalog();
$faltando = [];
foreach (bot_panel_pages() as $pg) {
    if (in_array($pg, bot_super_only_pages(), true)) {
        continue;
    }
    if (!isset($cat[bot_page_permission($pg)])) {
        $faltando[] = $pg;
    }
}
ok(count($faltando) === 0, 'catalogo tem chave de pagina para todas as paginas' . ($faltando ? ': falta ' . implode(', ', $faltando) : ''));
ok(!isset($cat[bot_page_permission('users')]) && !isset($cat[bot_page_permission('groups')]), 'Usuarios/Grupos nao tem chave concedivel');

// --- Toda acao de escrita tem permissao no catalogo -----------------------
$acoes = [
    'queues' => ['toggle', 'sync_catalog'],
    'operators' => ['sync_catalog', 'add_operator', 'remove_operator'],
    'templates' => ['save'],
    'settings' => ['save'],
    'menus' => ['create', 'rename', 'toggle', 'delete', 'save_queues', 'add_option', 'save_option', 'delete_option'],
    'teams' => ['create', 'rename', 'toggle', 'delete', 'save_queues', 'add_member', 'save_member', 'toggle_member', 'delete_member'],
    'routing' => ['save_globals', 'save_team_routing', 'reset_team_routing', 'save_member_routing', 'reset_member_routing'],
    'users' => ['create', 'update', 'toggle', 'reset_password', 'delete', 'save_scope'],
    'groups' => ['create', 'rename', 'toggle', 'delete', 'save_permissions', 'save_scope'],
];
$semMapa = [];
foreach ($acoes as $pg => $lista) {
    foreach ($lista as $act) {
        $perm = bot_action_permission($pg, $act);
        if ($perm === null || (!isset($cat[$perm]) && !in_array($perm, bot_super_only_perms(), true))) {
            $semMapa[] = $pg . '.' . $act;
        }
    }
}
ok(count($semMapa) === 0, 'toda acao de escrita tem permissao no catalogo' . ($semMapa ? ': falta ' . implode(', ', $semMapa) : ''));
ok(bot_action_permission('queues', 'toggle') === 'queues.manage', 'mapa queues.toggle -> queues.manage');
ok(bot_action_permission('users', 'qualquer_coisa') === 'users.manage', 'pagina super-only exige users.manage');
ok(bot_action_permission('dashboard', 'x') === null, 'pagina sem escrita nao exige permissao de acao');
// --- Fixture: Super Admin, grupo Triagem e usuarios -----------------------
$superGroup = bot_super_group_id($pdo);
ok($superGroup > 0, 'grupo Super Admin existe');
$pdo->prepare("INSERT INTO bot_users (login, password_hash, active, display_name) VALUES ('super1', 'x', 1, 'Super Um')")->execute();
$super1 = (int) $pdo->lastInsertId();
$pdo->prepare('UPDATE bot_users SET group_id = ? WHERE id = ?')->execute([$superGroup, $super1]);
$pdo->prepare("INSERT INTO permission_groups (name, active, is_super) VALUES ('Triagem', 1, 0)")->execute();
$gid = (int) $pdo->lastInsertId();
$insPerm = $pdo->prepare('INSERT INTO group_permissions (group_id, perm_key) VALUES (?, ?)');
foreach (['page.queues', 'page.activity', 'queues.manage'] as $permKey) {
    $insPerm->execute([$gid, $permKey]);
}
$pdo->prepare("INSERT INTO bot_users (login, password_hash, active, group_id) VALUES ('triagem', 'x', 1, ?)")->execute([$gid]);
$triagem = (int) $pdo->lastInsertId();

// --- Super Admin pode tudo -------------------------------------------------
login_as($super1);
ok(bot_is_super(bot_current_user($pdo)), 'super reconhecido');
ok(bot_can($pdo, 'page.users') && bot_can($pdo, 'users.manage') && bot_can($pdo, 'groups.manage'), 'super acessa Usuarios e Grupos');
ok(bot_can($pdo, 'chave.inexistente'), 'super passa por qualquer chave');
ok(bot_visible_queue_addresses($pdo) === null, 'super ve todas as filas (null)');
ok(bot_visible_team_ids($pdo) === null, 'super ve todos os times (null)');

// --- Grupo concedido -------------------------------------------------------
login_as($triagem);
ok(!bot_is_super(bot_current_user($pdo)), 'usuario de grupo comum nao e super');
ok(bot_can($pdo, 'page.queues') && bot_can($pdo, 'queues.manage'), 'grupo concedido libera pagina e acao');
ok(!bot_can($pdo, 'page.menus'), 'pagina nao concedida e negada');
ok(!bot_can($pdo, 'page.users') && !bot_can($pdo, 'users.manage') && !bot_can($pdo, 'groups.manage'), 'Usuarios/Grupos sao super-only');
ok(!bot_can($pdo, 'scope.all_queues'), 'escopo total nao vem por padrao');
ok(bot_visible_queue_addresses($pdo) === [], 'sem time e sem fila avulsa: nenhuma fila visivel');
$paramsVazios = [];
ok(bot_scope_queue_where($pdo, 'e.queue_address', $paramsVazios) === ' AND 0 = 1' && $paramsVazios === [], 'escopo vazio gera AND 0 = 1 sem parametros');

// --- Escopo por GRUPO (times, filas e menus) ------------------------------
$pdo->exec("INSERT INTO teams (name, active) VALUES ('Time A', 1)");
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q1', 'Fila 1', 1)");
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q9', 'Fila 9', 1)");
$pdo->exec("INSERT INTO team_queues (team_id, queue_address) VALUES (1, 'h:q1')");
$pdo->exec("INSERT INTO menus (name, active) VALUES ('Menu A', 1)");
$menuA = (int) $pdo->lastInsertId();
$pdo->prepare('INSERT INTO group_teams (group_id, team_id) VALUES (?, 1)')->execute([$gid]);
$pdo->prepare("INSERT INTO group_queues (group_id, queue_address) VALUES (?, 'h:q9')")->execute([$gid]);
$pdo->prepare('INSERT INTO group_menus (group_id, menu_id) VALUES (?, ?)')->execute([$gid, $menuA]);
bot_reset_panel_cache();
ok(bot_visible_queue_addresses($pdo) === ['h:q1', 'h:q9'], 'filas visiveis = filas do time marcado + filas marcadas');
ok(bot_visible_team_ids($pdo) === [1], 'times visiveis = group_teams');
ok(bot_visible_menu_ids($pdo) === [$menuA], 'menus visiveis = group_menus');
ok(bot_menu_in_scope($pdo, $menuA) && !bot_menu_in_scope($pdo, $menuA + 999), 'menu_in_scope');
$paramsOk = [];
$whereScope = bot_scope_queue_where($pdo, 'x.address', $paramsOk);
ok($whereScope === ' AND x.address IN (?,?)' && $paramsOk === ['h:q1', 'h:q9'], 'fragmento SQL de escopo por fila');
$paramsMenu = [];
$whereMenu = bot_scope_menu_where($pdo, 'm.id', $paramsMenu);
ok($whereMenu === ' AND m.id IN (?)' && $paramsMenu === [$menuA], 'fragmento SQL de escopo por menu');
ok(bot_queue_in_scope($pdo, 'h:q1') && !bot_queue_in_scope($pdo, 'h:zz'), 'queue_in_scope');
ok(bot_team_in_scope($pdo, 1) && !bot_team_in_scope($pdo, 99), 'team_in_scope');

// --- Chaves "ver tudo" ignoram as marcacoes -------------------------------
$pdo->prepare("INSERT INTO group_permissions (group_id, perm_key) VALUES (?, 'scope.all_menus')")->execute([$gid]);
bot_reset_panel_cache();
ok(bot_visible_menu_ids($pdo) === null, 'scope.all_menus libera todos os menus');
$pdo->prepare("DELETE FROM group_permissions WHERE group_id = ? AND perm_key = 'scope.all_menus'")->execute([$gid]);
$pdo->prepare("INSERT INTO group_permissions (group_id, perm_key) VALUES (?, 'scope.all_teams')")->execute([$gid]);
bot_reset_panel_cache();
ok(bot_visible_team_ids($pdo) === null, 'scope.all_teams libera todos os times');
$pdo->prepare("DELETE FROM group_permissions WHERE group_id = ? AND perm_key = 'scope.all_teams'")->execute([$gid]);
bot_reset_panel_cache();
ok(bot_visible_team_ids($pdo) === [1], 'sem scope.all_teams volta ao escopo marcado');

// --- Grupo desativado perde tudo ------------------------------------------
$pdo->prepare('UPDATE permission_groups SET active = 0 WHERE id = ?')->execute([$gid]);
bot_reset_panel_cache();
ok(!bot_can($pdo, 'page.queues'), 'grupo desativado nao concede permissao');
$pdo->prepare('UPDATE permission_groups SET active = 1 WHERE id = ?')->execute([$gid]);
bot_reset_panel_cache();
ok(bot_can($pdo, 'page.queues'), 'grupo reativado volta a conceder');

// --- Guardas do dono do painel --------------------------------------------
ok(bot_user_is_last_super($pdo, $super1), 'super1 e o unico Super Admin ativo');
login_as($super1);
$_POST = ['user_id' => $super1];
$errSelf = bot_users_handle_post($pdo, 'delete');
ok($errSelf !== null && stripos($errSelf, 'proprio') !== false, 'nao deixa excluir o proprio usuario');
login_as($triagem);
$errLast = bot_users_handle_post($pdo, 'toggle');
ok($errLast !== null && stripos($errLast, 'unico') !== false, 'nao deixa desativar o ultimo Super Admin');

// --- Criacao de usuario ---------------------------------------------------
login_as($super1);
$_POST = ['login' => 'triagem', 'password' => 'senha123'];
ok(bot_users_handle_post($pdo, 'create') !== null, 'create recusa login repetido');
$_POST = ['login' => 'novo.usuario', 'password' => '123'];
ok(bot_users_handle_post($pdo, 'create') !== null, 'create recusa senha curta');
$_POST = ['login' => 'novo.usuario', 'password' => 'senha123', 'display_name' => 'Novo', 'group_id' => (string) $gid];
ok(bot_users_handle_post($pdo, 'create') === null, 'create aceita dados validos');
ok((int) $pdo->query("SELECT COUNT(*) AS n FROM bot_users WHERE login = 'novo.usuario'")->fetch()['n'] === 1, 'usuario gravado no banco');

// --- save_scope do GRUPO ignora lixo --------------------------------------
$_POST = ['scope_teams' => [1, 1, 999], 'scope_menus' => [$menuA, 999], 'scope_queues' => ['h:q9', 'h:nada', 'lixo']];
ok(bot_groups_save_scope($pdo, $gid) === null, 'save_scope do grupo grava sem erro');
$nTeams = (int) $pdo->query('SELECT COUNT(*) AS n FROM group_teams WHERE group_id = ' . $gid)->fetch()['n'];
$nMenus = (int) $pdo->query('SELECT COUNT(*) AS n FROM group_menus WHERE group_id = ' . $gid)->fetch()['n'];
$nQueues = (int) $pdo->query('SELECT COUNT(*) AS n FROM group_queues WHERE group_id = ' . $gid)->fetch()['n'];
ok($nTeams === 1, 'save_scope do grupo ignora time repetido e inexistente');
ok($nMenus === 1, 'save_scope do grupo ignora menu inexistente');
ok($nQueues === 1, 'save_scope do grupo ignora fila inexistente e valor invalido');

@unlink($path);
exit($failed === 0 ? 0 : 1);