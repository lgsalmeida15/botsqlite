<?php
declare(strict_types=1);

/**
 * Permissoes do painel: catalogo de chaves, usuario logado, checks de pagina e
 * de acao, e escopo de dados (times/filas).
 *
 * Regras:
 *  - Super Admin (permission_groups.is_super = 1) pode tudo.
 *  - Usuarios e Grupos sao super-only: nao existe chave concedivel para eles.
 *  - Escopo de dados vem do GRUPO: group_teams (time + as filas dele), group_queues
 *    (filas soltas, inclusive sem time) e group_menus (quais menus o grupo administra).
 *    Sem marcacao e sem as chaves scope.all_* o grupo nao ve dado nenhum.
 */

function bot_perm_truthy($v): bool
{
    return $v === true || $v === 1 || $v === "1" || $v === "t";
}

/** Catalogo unico das permissoes concediveis (chave => grupo/label). */
function bot_permission_catalog(): array
{
    return [
        "page.dashboard" => ["group" => "Paginas", "label" => "Dashboard"],
        "page.queues" => ["group" => "Paginas", "label" => "Filas"],
        "page.operators" => ["group" => "Paginas", "label" => "Operadores"],
        "page.activity" => ["group" => "Paginas", "label" => "Atividade"],
        "page.load" => ["group" => "Paginas", "label" => "Carga (analistas)"],
        "page.templates" => ["group" => "Paginas", "label" => "Templates"],
        "page.menus" => ["group" => "Paginas", "label" => "Menus"],
        "page.teams" => ["group" => "Paginas", "label" => "Times"],
        "page.routing" => ["group" => "Paginas", "label" => "Roteamento"],
        "page.settings" => ["group" => "Paginas", "label" => "Ajustes"],
        "page.chats" => ["group" => "Paginas", "label" => "Chats"],
        "queues.manage" => ["group" => "Acoes", "label" => "Filas: ligar/desligar e sincronizar catalogo"],
        "operators.manage" => ["group" => "Acoes", "label" => "Operadores: cadastrar, remover e sincronizar"],
        "templates.manage" => ["group" => "Acoes", "label" => "Templates: editar textos"],
        "menus.manage" => ["group" => "Acoes", "label" => "Menus: criar, editar e excluir menus/opcoes"],
        "teams.manage" => ["group" => "Acoes", "label" => "Times: criar, editar, membros e filas"],
        "routing.manage" => ["group" => "Acoes", "label" => "Roteamento: ajustes globais, do time e do membro"],
        "settings.manage" => ["group" => "Acoes", "label" => "Ajustes: salvar parametros do bot"],
        "load.manage" => ["group" => "Acoes", "label" => "Carga: forcar a leitura das listas do TOA"],
        "scope.all_queues" => ["group" => "Escopo", "label" => "Ver dados de TODAS as filas (ignora filas/times marcados)"],
        "scope.all_teams" => ["group" => "Escopo", "label" => "Ver TODOS os times (ignora os times marcados)"],
        "scope.all_menus" => ["group" => "Escopo", "label" => "Ver TODOS os menus (ignora os menus marcados)"],
    ];
}

function bot_super_only_perms(): array
{
    return ["users.manage", "groups.manage"];
}

/** Paginas do painel (fora login/logout/redirects). */
function bot_panel_pages(): array
{
    return ["dashboard", "queues", "operators", "activity", "load", "templates", "menus", "teams", "routing", "settings", "chats", "users", "groups"];
}

function bot_super_only_pages(): array
{
    return ["users", "groups"];
}

function bot_page_permission(string $page): string
{
    return "page." . $page;
}
/** Permissao exigida por acao de escrita. null = acao desconhecida (handler ignora). */
function bot_action_permission(string $page, string $action): ?string
{
    static $map = [
        "queues" => ["toggle" => "queues.manage", "sync_catalog" => "queues.manage"],
        "operators" => [
            "sync_catalog" => "operators.manage",
            "add_operator" => "operators.manage",
            "remove_operator" => "operators.manage",
        ],
        "templates" => ["save" => "templates.manage"],
        "settings" => ["save" => "settings.manage"],
        "load" => ["load_now" => "load.manage", "requeue_offline" => "load.manage"],
        "menus" => [
            "create" => "menus.manage",
            "rename" => "menus.manage",
            "toggle" => "menus.manage",
            "delete" => "menus.manage",
            "save_queues" => "menus.manage",
            "add_option" => "menus.manage",
            "save_option" => "menus.manage",
            "delete_option" => "menus.manage",
        ],
        "teams" => [
            "create" => "teams.manage",
            "rename" => "teams.manage",
            "toggle" => "teams.manage",
            "delete" => "teams.manage",
            "save_queues" => "teams.manage",
            "add_member" => "teams.manage",
            "save_member" => "teams.manage",
            "toggle_member" => "teams.manage",
            "delete_member" => "teams.manage",
        ],
        "routing" => [
            "save_globals" => "routing.manage",
            "save_team_routing" => "routing.manage",
            "reset_team_routing" => "routing.manage",
            "save_member_routing" => "routing.manage",
            "reset_member_routing" => "routing.manage",
        ],
        "users" => ["*" => "users.manage"],
        "groups" => ["*" => "groups.manage"],
    ];
    if (!isset($map[$page])) {
        return null;
    }
    if (isset($map[$page]["*"])) {
        return $map[$page]["*"];
    }
    return $map[$page][$action] ?? null;
}

function bot_reset_panel_cache(): void
{
    unset(
        $GLOBALS["panel_user"],
        $GLOBALS["panel_user_perms"],
        $GLOBALS["panel_scope_queues"],
        $GLOBALS["panel_scope_teams"],
        $GLOBALS["panel_scope_menus"]
    );
}

function bot_load_current_user(PDO $pdo): ?array
{
    $uid = (int) ($_SESSION["bot_user_id"] ?? 0);
    if ($uid < 1) {
        return null;
    }
    $st = $pdo->prepare(
        "SELECT u.id, u.login, u.display_name, u.operator_address, u.active, u.group_id,
                g.name AS group_name, g.active AS group_active, g.is_super AS group_is_super
           FROM bot_users u
           LEFT JOIN permission_groups g ON g.id = u.group_id
          WHERE u.id = ?"
    );
    $st->execute([$uid]);
    $row = $st->fetch();
    if (!$row || !bot_perm_truthy($row["active"])) {
        return null;
    }
    if ($row["group_id"] === null || !bot_perm_truthy($row["group_active"])) {
        $row["group_is_super"] = 0;
        $row["group_name"] = $row["group_id"] === null ? "sem grupo" : (string) $row["group_name"] . " (inativo)";
    }
    return $row;
}

function bot_current_user(PDO $pdo): ?array
{
    if (!array_key_exists("panel_user", $GLOBALS)) {
        $GLOBALS["panel_user"] = bot_load_current_user($pdo);
    }
    return $GLOBALS["panel_user"];
}

function bot_user_label(?array $user): string
{
    if ($user === null) {
        return "";
    }
    $n = trim((string) ($user["display_name"] ?? ""));
    return $n !== "" ? $n : (string) $user["login"];
}

function bot_is_super(?array $user): bool
{
    return $user !== null && (int) ($user["group_is_super"] ?? 0) === 1;
}
/** Permissoes efetivas do usuario (super = catalogo inteiro). */
function bot_user_permissions(PDO $pdo, ?array $user): array
{
    if ($user === null) {
        return [];
    }
    if (!isset($GLOBALS["panel_user_perms"])) {
        $GLOBALS["panel_user_perms"] = [];
    }
    $uid = (int) $user["id"];
    if (array_key_exists($uid, $GLOBALS["panel_user_perms"])) {
        return $GLOBALS["panel_user_perms"][$uid];
    }
    $perms = [];
    if (bot_is_super($user)) {
        foreach (array_keys(bot_permission_catalog()) as $k) {
            $perms[$k] = true;
        }
        $perms["*"] = true;
    } elseif ($user["group_id"] !== null && bot_perm_truthy($user["group_active"])) {
        $st = $pdo->prepare("SELECT perm_key FROM group_permissions WHERE group_id = ?");
        $st->execute([(int) $user["group_id"]]);
        foreach ($st->fetchAll(PDO::FETCH_COLUMN) as $k) {
            $perms[(string) $k] = true;
        }
    }
    $GLOBALS["panel_user_perms"][$uid] = $perms;
    return $perms;
}

function bot_can(PDO $pdo, string $perm): bool
{
    $user = bot_current_user($pdo);
    if ($user === null) {
        return false;
    }
    if (bot_is_super($user)) {
        return true;
    }
    if (in_array($perm, bot_super_only_perms(), true)) {
        return false;
    }
    $perms = bot_user_permissions($pdo, $user);
    return isset($perms[$perm]);
}

/** Filas visiveis: null = todas (super/scope.all_queues); [] = nenhuma.
 *  Base do grupo: filas dos times marcados (group_teams) + filas marcadas (group_queues). */
function bot_visible_queue_addresses(PDO $pdo): ?array
{
    $user = bot_current_user($pdo);
    if ($user === null) {
        return [];
    }
    if (bot_is_super($user) || bot_can($pdo, "scope.all_queues")) {
        return null;
    }
    $gid = (int) ($user["group_id"] ?? 0);
    if ($gid < 1) {
        return [];
    }
    if (!isset($GLOBALS["panel_scope_queues"])) {
        $GLOBALS["panel_scope_queues"] = [];
    }
    if (array_key_exists($gid, $GLOBALS["panel_scope_queues"])) {
        return $GLOBALS["panel_scope_queues"][$gid];
    }
    $st = $pdo->prepare(
        "SELECT a FROM (
             SELECT tq.queue_address AS a FROM group_teams gt
               JOIN team_queues tq ON tq.team_id = gt.team_id
              WHERE gt.group_id = ?
             UNION
             SELECT queue_address AS a FROM group_queues WHERE group_id = ?
         ) AS x ORDER BY a"
    );
    $st->execute([$gid, $gid]);
    $out = [];
    foreach ($st->fetchAll(PDO::FETCH_COLUMN) as $a) {
        $a = trim((string) $a);
        if ($a !== "") {
            $out[] = $a;
        }
    }
    $GLOBALS["panel_scope_queues"][$gid] = $out;
    return $out;
}

/** Times visiveis: null = todos (super/scope.all_teams); [] = nenhum. */
function bot_visible_team_ids(PDO $pdo): ?array
{
    $user = bot_current_user($pdo);
    if ($user === null) {
        return [];
    }
    if (bot_is_super($user) || bot_can($pdo, "scope.all_teams")) {
        return null;
    }
    $gid = (int) ($user["group_id"] ?? 0);
    if ($gid < 1) {
        return [];
    }
    if (!isset($GLOBALS["panel_scope_teams"])) {
        $GLOBALS["panel_scope_teams"] = [];
    }
    if (array_key_exists($gid, $GLOBALS["panel_scope_teams"])) {
        return $GLOBALS["panel_scope_teams"][$gid];
    }
    $st = $pdo->prepare("SELECT team_id FROM group_teams WHERE group_id = ? ORDER BY team_id");
    $st->execute([$gid]);
    $ids = [];
    foreach ($st->fetchAll(PDO::FETCH_COLUMN) as $v) {
        $ids[] = (int) $v;
    }
    $GLOBALS["panel_scope_teams"][$gid] = $ids;
    return $ids;
}

/** Menus visiveis: null = todos (super/scope.all_menus); [] = nenhum.
 *  Menu e configuracao: nao concede dado de fila (isso vem de group_teams/group_queues). */
function bot_visible_menu_ids(PDO $pdo): ?array
{
    $user = bot_current_user($pdo);
    if ($user === null) {
        return [];
    }
    if (bot_is_super($user) || bot_can($pdo, "scope.all_menus")) {
        return null;
    }
    $gid = (int) ($user["group_id"] ?? 0);
    if ($gid < 1) {
        return [];
    }
    if (!isset($GLOBALS["panel_scope_menus"])) {
        $GLOBALS["panel_scope_menus"] = [];
    }
    if (array_key_exists($gid, $GLOBALS["panel_scope_menus"])) {
        return $GLOBALS["panel_scope_menus"][$gid];
    }
    $st = $pdo->prepare("SELECT menu_id FROM group_menus WHERE group_id = ? ORDER BY menu_id");
    $st->execute([$gid]);
    $ids = [];
    foreach ($st->fetchAll(PDO::FETCH_COLUMN) as $v) {
        $ids[] = (int) $v;
    }
    $GLOBALS["panel_scope_menus"][$gid] = $ids;
    return $ids;
}
/** Fragmento SQL de escopo por fila. "" = sem restricao; " AND 0 = 1" = sem dados. */
function bot_scope_queue_where(PDO $pdo, string $column, array &$params): string
{
    $addr = bot_visible_queue_addresses($pdo);
    if ($addr === null) {
        return "";
    }
    if (!$addr) {
        return " AND 0 = 1";
    }
    foreach ($addr as $a) {
        $params[] = $a;
    }
    return " AND " . $column . " IN (" . implode(",", array_fill(0, count($addr), "?")) . ")";
}

/** Fragmento SQL de escopo por time (mesma semantica). */
function bot_scope_team_where(PDO $pdo, string $column, array &$params): string
{
    $ids = bot_visible_team_ids($pdo);
    if ($ids === null) {
        return "";
    }
    if (!$ids) {
        return " AND 0 = 1";
    }
    foreach ($ids as $i) {
        $params[] = $i;
    }
    return " AND " . $column . " IN (" . implode(",", array_fill(0, count($ids), "?")) . ")";
}

/** Fragmento SQL de escopo por menu (mesma semantica). */
/**
 * Quem cria um menu/time no painel passa a enxergar o que criou: sem isso o item
 * novo ficaria invisivel, porque o escopo e do grupo (negar por padrao).
 */
function bot_grant_created_to_group(PDO $pdo, string $table, string $column, int $newId): void
{
    if ($newId < 1 || !in_array($table, ["group_menus", "group_teams"], true) || !in_array($column, ["menu_id", "team_id"], true)) {
        return;
    }
    $me = bot_current_user($pdo);
    if ($me === null || bot_is_super($me)) {
        return;
    }
    $gid = (int) ($me["group_id"] ?? 0);
    if ($gid < 1) {
        return;
    }
    if ($table === "group_menus" && bot_can($pdo, "scope.all_menus")) {
        return;
    }
    if ($table === "group_teams" && bot_can($pdo, "scope.all_teams")) {
        return;
    }
    $pdo->prepare('INSERT OR IGNORE INTO ' . $table . ' (group_id, ' . $column . ') VALUES (?, ?)')->execute([$gid, $newId]);
    bot_reset_panel_cache();
}

function bot_scope_menu_where(PDO $pdo, string $column, array &$params): string
{
    $ids = bot_visible_menu_ids($pdo);
    if ($ids === null) {
        return "";
    }
    if (!$ids) {
        return " AND 0 = 1";
    }
    foreach ($ids as $i) {
        $params[] = $i;
    }
    return " AND " . $column . " IN (" . implode(",", array_fill(0, count($ids), "?")) . ")";
}

function bot_menu_in_scope(PDO $pdo, int $menuId): bool
{
    $ids = bot_visible_menu_ids($pdo);
    return $ids === null || in_array($menuId, $ids, true);
}

function bot_queue_in_scope(PDO $pdo, string $address): bool
{
    $addr = bot_visible_queue_addresses($pdo);
    return $addr === null || in_array($address, $addr, true);
}

function bot_team_in_scope(PDO $pdo, int $teamId): bool
{
    $ids = bot_visible_team_ids($pdo);
    return $ids === null || in_array($teamId, $ids, true);
}

function bot_forbidden(string $msg): void
{
    $pdo = bot_pdo();
    $back = "dashboard";
    foreach (bot_panel_pages() as $pgBack) {
        if (in_array($pgBack, bot_super_only_pages(), true)) {
            if (bot_is_super(bot_current_user($pdo))) {
                $back = $pgBack;
                break;
            }
            continue;
        }
        if (bot_can($pdo, bot_page_permission($pgBack))) {
            $back = $pgBack;
            break;
        }
    }
    http_response_code(403);
    echo "<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><title>403 - HELPTOA Bot</title>"
        . "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        . "<link href=\"assets/bootstrap.min.css\" rel=\"stylesheet\">"
        . "<link href=\"assets/panel.css\" rel=\"stylesheet\"></head>"
        . "<body class=\"bg-light\"><main class=\"container-xl py-5\"><div class=\"alert alert-danger shadow-sm\">"
        . "<h1 class=\"h5\">Acesso negado</h1><p class=\"mb-3\">" . h($msg) . "</p>"
        . "<a class=\"btn btn-primary btn-sm\" href=\"?page=" . h($back) . "\">Voltar ao painel</a>"
        . "</div></main></body></html>";
    exit;
}

function bot_require_page(PDO $pdo, string $page): void
{
    if (!in_array($page, bot_panel_pages(), true)) {
        return;
    }
    if (in_array($page, bot_super_only_pages(), true)) {
        if (!bot_is_super(bot_current_user($pdo))) {
            bot_forbidden("Esta area e restrita ao Super Admin.");
        }
        return;
    }
    if (!bot_can($pdo, bot_page_permission($page))) {
        bot_forbidden("Seu grupo de permissao nao da acesso a esta pagina.");
    }
}

function bot_require_action(PDO $pdo, string $page, string $action): void
{
    $perm = bot_action_permission($page, $action);
    if ($perm === null) {
        return;
    }
    if (!bot_can($pdo, $perm)) {
        bot_forbidden("Seu grupo de permissao nao permite esta acao.");
    }
}