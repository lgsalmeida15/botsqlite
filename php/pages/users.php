<?php
declare(strict_types=1);

/** Usuarios do painel (super-only): CRUD, grupo de permissao, times e filas avulsas. */

function bot_group_exists(PDO $pdo, int $id): bool
{
    $st = $pdo->prepare("SELECT 1 FROM permission_groups WHERE id = ?");
    $st->execute([$id]);
    return $st->fetchColumn() !== false;
}

function bot_groups_list(PDO $pdo): array
{
    return $pdo->query("SELECT id, name, active, is_super FROM permission_groups ORDER BY is_super DESC, name")->fetchAll();
}

function bot_user_row(PDO $pdo, int $id): ?array
{
    $st = $pdo->prepare(
        "SELECT u.id, u.login, u.display_name, u.operator_address, u.active, u.group_id, u.last_login_at,
                g.name AS group_name, g.is_super AS group_is_super
           FROM bot_users u LEFT JOIN permission_groups g ON g.id = u.group_id
          WHERE u.id = ?"
    );
    $st->execute([$id]);
    $row = $st->fetch();
    return $row ?: null;
}

/** Ids dos Super Admin ativos (guard anti-lockout). */
function bot_active_super_ids(PDO $pdo): array
{
    $superId = bot_super_group_id($pdo);
    if ($superId < 1) {
        return [];
    }
    $st = $pdo->prepare("SELECT id FROM bot_users WHERE group_id = ? AND active = 1");
    $st->execute([$superId]);
    return array_map("intval", $st->fetchAll(PDO::FETCH_COLUMN));
}

function bot_user_is_last_super(PDO $pdo, int $id): bool
{
    $ids = bot_active_super_ids($pdo);
    return count($ids) === 1 && (int) $ids[0] === $id;
}

function bot_users_handle_post(PDO $pdo, string $action): ?string
{
    $me = bot_current_user($pdo);
    $myId = $me ? (int) $me["id"] : 0;
    $id = (int) ($_POST["user_id"] ?? 0);

    if ($action === "create") {
        $login = trim((string) ($_POST["login"] ?? ""));
        $display = trim((string) ($_POST["display_name"] ?? ""));
        $oper = trim((string) ($_POST["operator_address"] ?? ""));
        $pass = (string) ($_POST["password"] ?? "");
        $groupRaw = trim((string) ($_POST["group_id"] ?? ""));
        if (!preg_match("/^[A-Za-z0-9._-]{3,40}$/", $login)) {
            return "Login invalido: 3 a 40 caracteres (letras, numeros, ponto, hifen ou underline).";
        }
        if (strlen($pass) < 6) {
            return "A senha precisa ter pelo menos 6 caracteres.";
        }
        $groupId = $groupRaw === "" ? null : (int) $groupRaw;
        if ($groupId !== null && !bot_group_exists($pdo, $groupId)) {
            return "Grupo invalido.";
        }
        $st = $pdo->prepare("SELECT 1 FROM bot_users WHERE login = ?");
        $st->execute([$login]);
        if ($st->fetchColumn() !== false) {
            return "Ja existe usuario com esse login.";
        }
        $pdo->prepare(
            "INSERT INTO bot_users (login, password_hash, display_name, operator_address, active, group_id)
             VALUES (?, ?, ?, ?, 1, ?)"
        )->execute([$login, password_hash($pass, PASSWORD_DEFAULT), $display, $oper, $groupId]);
        return null;
    }

    if ($id < 1) {
        return "Usuario invalido.";
    }
    $target = bot_user_row($pdo, $id);
    if (!$target) {
        return "Usuario nao encontrado.";
    }

    if ($action === "update") {
        $groupRaw = trim((string) ($_POST["group_id"] ?? ""));
        $groupId = $groupRaw === "" ? null : (int) $groupRaw;
        if ($groupId !== null && !bot_group_exists($pdo, $groupId)) {
            return "Grupo invalido.";
        }
        $superId = bot_super_group_id($pdo);
        $wasSuper = $superId > 0 && (int) $target["group_id"] === $superId;
        $willBeSuper = $superId > 0 && $groupId === $superId;
        if ($wasSuper && !$willBeSuper && bot_user_is_last_super($pdo, $id)) {
            return "Este e o unico Super Admin ativo: nao da para tirar o grupo dele.";
        }
        $pdo->prepare("UPDATE bot_users SET display_name = ?, operator_address = ?, group_id = ? WHERE id = ?")
            ->execute([
                trim((string) ($_POST["display_name"] ?? "")),
                trim((string) ($_POST["operator_address"] ?? "")),
                $groupId,
                $id,
            ]);
        return null;
    }

    if ($action === "toggle") {
        if ($id === $myId) {
            return "Voce nao pode ativar/desativar o proprio usuario.";
        }
        if (bot_user_is_last_super($pdo, $id)) {
            return "Este e o unico Super Admin ativo.";
        }
        $pdo->prepare("UPDATE bot_users SET active = NOT active WHERE id = ?")->execute([$id]);
        return null;
    }

    if ($action === "reset_password") {
        $pass = (string) ($_POST["password"] ?? "");
        if (strlen($pass) < 6) {
            return "A senha precisa ter pelo menos 6 caracteres.";
        }
        $pdo->prepare("UPDATE bot_users SET password_hash = ? WHERE id = ?")
            ->execute([password_hash($pass, PASSWORD_DEFAULT), $id]);
        return null;
    }

    if ($action === "delete") {
        if ($id === $myId) {
            return "Voce nao pode excluir o proprio usuario.";
        }
        if (bot_user_is_last_super($pdo, $id)) {
            return "Este e o unico Super Admin ativo.";
        }
        $pdo->prepare("DELETE FROM bot_users WHERE id = ?")->execute([$id]);
        return null;
    }

    return null;
}
/** Render da tela Usuarios (super-only). */
function bot_users_render(PDO $pdo, string $csrf, string $formError = ''): void
{
    $users = $pdo->query(
        'SELECT u.id, u.login, u.display_name, u.operator_address, u.active, u.group_id, u.last_login_at,
                g.name AS group_name, g.is_super AS group_is_super
           FROM bot_users u LEFT JOIN permission_groups g ON g.id = u.group_id
          ORDER BY u.login'
    )->fetchAll();
    $groups = bot_groups_list($pdo);
    $teams = $pdo->query('SELECT id, name, active FROM teams ORDER BY name')->fetchAll();
    $queues = $pdo->query('SELECT address, name FROM queues ORDER BY name, address')->fetchAll();
    $scopeSummary = [];
    $groupIds = [];
    foreach ($users as $uRow) {
        $groupIds[(int) ($uRow['group_id'] ?? 0)] = true;
    }
    foreach (array_keys($groupIds) as $gidOne) {
        $scopeSummary[$gidOne] = bot_group_scope_summary($pdo, $gidOne > 0 ? $gidOne : null);
    }
    $ctx = [
        'groups' => $groups,
        'scopeSummary' => $scopeSummary,
        'meId' => (int) (bot_current_user($pdo)['id'] ?? 0),
    ];

    page_header(
        'Usuarios',
        'Super Admin cria e mantem contas e escolhe o grupo. O escopo de dados (filas, menus e times) e definido no grupo, na aba Grupos.',
        '<button class="btn btn-primary btn-sm" type="button" data-bs-toggle="collapse" data-bs-target="#novo-usuario">Novo usuario</button>'
    );
    if ($formError !== '') {
        echo '<div class="alert alert-danger">' . h($formError) . '</div>';
    }
    echo '<div class="collapse mb-3" id="novo-usuario"><div class="card card-slim"><div class="card-body">';
    echo '<form method="post" class="row g-2"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="create">';
    echo '<div class="col-12 col-md-3"><label class="form-label">Login</label><input class="form-control form-control-sm" name="login" required placeholder="joao.silva"></div>';
    echo '<div class="col-12 col-md-3"><label class="form-label">Nome exibido</label><input class="form-control form-control-sm" name="display_name"></div>';
    echo '<div class="col-12 col-md-2"><label class="form-label">Senha</label><input class="form-control form-control-sm" type="password" name="password" required></div>';
    echo '<div class="col-12 col-md-2"><label class="form-label">Grupo</label><select class="form-select form-select-sm" name="group_id">';
    echo '<option value="">sem grupo (sem acesso)</option>';
    foreach ($groups as $g) {
        echo '<option value="' . (int) $g['id'] . '">' . h($g['name']) . ((int) $g['active'] === 1 ? '' : ' (inativo)') . '</option>';
    }
    echo '</select></div>';
    echo '<div class="col-12 col-md-2 align-self-end"><button class="btn btn-primary btn-sm w-100">Criar</button></div>';
    echo '<div class="col-12"><p class="text-muted small mb-0">O <strong>Address do TOA</strong> fica em <strong>Editar</strong>. O que o usuario enxerga vem do <strong>grupo</strong> (aba Grupos).</p></div>';
    echo '</form></div></div></div>';

    echo '<div class="card card-slim"><div class="card-body pb-2">';
    echo '<input type="search" class="form-control form-control-sm" placeholder="Filtrar usuarios..." data-filter-table="#tabela-usuarios" autocomplete="off">';
    echo '</div><div class="table-responsive sticky-head"><table class="table table-slim table-hover align-middle mb-0" id="tabela-usuarios">';
    echo '<thead><tr><th style="width:17rem">Usuario</th><th>Grupo</th><th>Escopo de dados (do grupo)</th><th>Ativo</th><th>Ultimo acesso</th><th></th></tr></thead><tbody>';
    foreach ($users as $u) {
        bot_users_render_row($u, $csrf, $ctx);
    }
    echo '</tbody></table></div></div>';
    if (!$users) {
        echo '<p class="text-muted">Nenhum usuario cadastrado.</p>';
    }
    if (!$teams) {
        echo '<div class="alert alert-warning small mt-3">Nenhum time cadastrado: usuarios sem a permissao de escopo total nao terao nenhuma fila visivel (aba Times).</div>';
    }
}
/** Linha da tabela de usuarios + editor (dados, senha e escopo). */
function bot_users_render_row(array $u, string $csrf, array $ctx): void
{
    $uid = (int) $u['id'];
    $on = bot_perm_truthy($u['active']);
    $isSuper = (int) ($u['group_is_super'] ?? 0) === 1;
    $gname = (string) ($u['group_name'] ?? '');
    $scopeHtml = (string) ($ctx['scopeSummary'][(int) ($u['group_id'] ?? 0)] ?? '<span class="text-muted small">-</span>');
    echo '<tr>';
    echo '<td><strong>' . h((string) $u['login']) . '</strong>';
    if ((string) $u['display_name'] !== '') {
        echo '<br><span class="text-muted small">' . h((string) $u['display_name']) . '</span>';
    }
    if ((string) $u['operator_address'] !== '') {
        echo '<br><code class="small">' . h((string) $u['operator_address']) . '</code>';
    }
    if ($uid === (int) $ctx['meId']) {
        echo ' <span class="badge badge-soft">voce</span>';
    }
    echo '</td>';
    if ($isSuper) {
        echo '<td><span class="badge bg-danger">Super Admin</span></td>';
    } elseif ($gname !== '') {
        echo '<td><span class="badge badge-soft">' . h($gname) . '</span></td>';
    } else {
        echo '<td><span class="text-muted small">sem grupo</span></td>';
    }
    echo '<td>' . $scopeHtml . '</td>';
    echo '<td>' . badge_state($on) . '</td>';
    echo '<td class="text-nowrap small text-muted">' . h($u['last_login_at'] !== null ? fmt_dt((string) $u['last_login_at']) : '') . '</td>';
    echo '<td class="text-nowrap">';
    echo '<button type="button" class="btn btn-outline-primary btn-sm me-1" data-bs-toggle="collapse" data-bs-target="#user-' . $uid . '">Editar</button>';
    echo '<form class="d-inline" method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="user_id" value="' . $uid . '">';
    echo '<button class="btn btn-outline-secondary btn-sm me-1" name="action" value="toggle">' . ($on ? 'Desativar' : 'Ativar') . '</button>';
    echo '<button class="btn btn-outline-danger btn-sm" name="action" value="delete" onclick="return confirm(\'Excluir este usuario?\')">Excluir</button></form>';
    echo '</td></tr>';
    bot_users_render_editor($u, $uid, $csrf, $ctx);
}

/** Editor do usuario (collapse): dados/grupo, senha e escopo de times/filas. */
function bot_users_render_editor(array $u, int $uid, string $csrf, array $ctx): void
{
    echo '<tr class="opt-editor-row"><td colspan="6" class="p-0 border-0">';
    echo '<div class="collapse" id="user-' . $uid . '"><div class="p-3 bg-light border-top">';
    echo '<form method="post" class="row g-2 mb-3"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="user_id" value="' . $uid . '"><input type="hidden" name="action" value="update">';
    echo '<div class="col-12 col-md-3"><label class="form-label">Nome exibido</label><input class="form-control form-control-sm" name="display_name" value="' . h((string) $u['display_name']) . '"></div>';
    echo '<div class="col-12 col-md-3"><label class="form-label">Address no TOA</label><input class="form-control form-control-sm" name="operator_address" value="' . h((string) $u['operator_address']) . '" placeholder="u:login@clarosa"></div>';
    echo '<div class="col-12 col-md-3"><label class="form-label">Grupo de permissao</label><select class="form-select form-select-sm" name="group_id">';
    echo '<option value="">sem grupo (sem acesso)</option>';
    foreach ($ctx['groups'] as $g) {
        echo '<option value="' . (int) $g['id'] . '"' . ((int) $u['group_id'] === (int) $g['id'] ? ' selected' : '') . '>' . h($g['name']) . ((int) $g['active'] === 1 ? '' : ' (inativo)') . '</option>';
    }
    echo '</select></div>';
    echo '<div class="col-12 col-md-3 align-self-end"><button class="btn btn-primary btn-sm">Salvar dados</button></div>';
    echo '</form>';
    echo '<form method="post" class="row g-2 mb-3"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="user_id" value="' . $uid . '"><input type="hidden" name="action" value="reset_password">';
    echo '<div class="col-12 col-md-3"><label class="form-label">Nova senha</label><input class="form-control form-control-sm" type="password" name="password" placeholder="minimo 6 caracteres"></div>';
    echo '<div class="col-12 col-md-3 align-self-end"><button class="btn btn-outline-secondary btn-sm">Redefinir senha</button></div>';
    echo '</form>';
    echo '<div class="row g-2"><div class="col-12"><h4 class="h6 mb-0">Escopo de dados (herdado do grupo)</h4></div>';
    echo '<div class="col-12">' . (string) ($ctx['scopeSummary'][(int) ($u['group_id'] ?? 0)] ?? '<span class="text-muted small">sem grupo</span>') . '</div>';
    echo '<div class="col-12"><p class="text-muted small mb-0">Para mudar filas, menus e times deste usuario, edite o grupo na aba <a href="?page=groups">Grupos</a>.</p></div></div>';
    echo '</div></div></td></tr>';
}