<?php
declare(strict_types=1);

/** Grupos de permissao (super-only): CRUD + matriz de permissoes. */

function bot_groups_handle_post(PDO $pdo, string $action): ?string
{
    $id = (int) ($_POST['group_id'] ?? 0);

    if ($action === 'create') {
        $name = trim((string) ($_POST['name'] ?? ''));
        if ($name === '' || strlen($name) > 60) {
            return 'Informe um nome de grupo (ate 60 caracteres).';
        }
        $st = $pdo->prepare('SELECT 1 FROM permission_groups WHERE name = ?');
        $st->execute([$name]);
        if ($st->fetchColumn() !== false) {
            return 'Ja existe um grupo com esse nome.';
        }
        $pdo->prepare('INSERT INTO permission_groups (name, active, is_super) VALUES (?, 1, 0)')->execute([$name]);
        return null;
    }

    if ($id < 1) {
        return 'Grupo invalido.';
    }
    $st = $pdo->prepare('SELECT id, name, active, is_super FROM permission_groups WHERE id = ?');
    $st->execute([$id]);
    $group = $st->fetch();
    if (!$group) {
        return 'Grupo nao encontrado.';
    }
    $isSuperGroup = (int) $group['is_super'] === 1;

    if ($action === 'save_scope') {
        if ($isSuperGroup) {
            return 'O grupo Super Admin ja enxerga todos os dados.';
        }
        return bot_groups_save_scope($pdo, $id);
    }

    if ($action === 'rename') {
        $name = trim((string) ($_POST['name'] ?? ''));
        if ($name === '' || strlen($name) > 60) {
            return 'Informe um nome de grupo (ate 60 caracteres).';
        }
        $dup = $pdo->prepare('SELECT 1 FROM permission_groups WHERE name = ? AND id <> ?');
        $dup->execute([$name, $id]);
        if ($dup->fetchColumn() !== false) {
            return 'Ja existe um grupo com esse nome.';
        }
        $pdo->prepare('UPDATE permission_groups SET name = ? WHERE id = ?')->execute([$name, $id]);
        return null;
    }

    if ($action === 'toggle') {
        if ($isSuperGroup) {
            return 'O grupo Super Admin nao pode ser desativado.';
        }
        $pdo->prepare('UPDATE permission_groups SET active = NOT active WHERE id = ?')->execute([$id]);
        return null;
    }

    if ($action === 'delete') {
        if ($isSuperGroup) {
            return 'O grupo Super Admin nao pode ser excluido.';
        }
        $pdo->prepare('DELETE FROM permission_groups WHERE id = ?')->execute([$id]);
        return null;
    }

    if ($action === 'save_permissions') {
        if ($isSuperGroup) {
            return 'O grupo Super Admin ja tem todas as permissoes.';
        }
        $catalog = bot_permission_catalog();
        $wanted = [];
        foreach ((array) ($_POST['perms'] ?? []) as $k) {
            $k = (string) $k;
            if (isset($catalog[$k])) {
                $wanted[$k] = true;
            }
        }
        $pdo->beginTransaction();
        try {
            $pdo->prepare('DELETE FROM group_permissions WHERE group_id = ?')->execute([$id]);
            $ins = $pdo->prepare('INSERT OR IGNORE INTO group_permissions (group_id, perm_key) VALUES (?, ?)');
            foreach (array_keys($wanted) as $k) {
                $ins->execute([$id, $k]);
            }
            $pdo->commit();
        } catch (Throwable $e) {
            if ($pdo->inTransaction()) {
                $pdo->rollBack();
            }
            throw $e;
        }
        return null;
    }

    return null;
}
/** Grava o escopo de dados do grupo (times, filas e menus). Negar por padrao. */
function bot_groups_save_scope(PDO $pdo, int $groupId): ?string
{
    $st = $pdo->prepare('SELECT 1 FROM permission_groups WHERE id = ?');
    $st->execute([$groupId]);
    if ($st->fetchColumn() === false) {
        return 'Grupo nao encontrado.';
    }
    $teams = [];
    foreach ((array) ($_POST['scope_teams'] ?? []) as $t) {
        $t = (int) $t;
        if ($t > 0) {
            $teams[$t] = true;
        }
    }
    $menus = [];
    foreach ((array) ($_POST['scope_menus'] ?? []) as $m) {
        $m = (int) $m;
        if ($m > 0) {
            $menus[$m] = true;
        }
    }
    $queues = [];
    foreach ((array) ($_POST['scope_queues'] ?? []) as $qa) {
        $qa = trim((string) $qa);
        if (substr($qa, 0, 2) === 'h:') {
            $queues[$qa] = true;
        }
    }
    $pdo->beginTransaction();
    try {
        $pdo->prepare('DELETE FROM group_teams WHERE group_id = ?')->execute([$groupId]);
        $ins = $pdo->prepare('INSERT OR IGNORE INTO group_teams (group_id, team_id) VALUES (?, ?)');
        $has = $pdo->prepare('SELECT 1 FROM teams WHERE id = ?');
        foreach (array_keys($teams) as $t) {
            $has->execute([$t]);
            if ($has->fetchColumn() !== false) {
                $ins->execute([$groupId, $t]);
            }
        }
        $pdo->prepare('DELETE FROM group_menus WHERE group_id = ?')->execute([$groupId]);
        $ins = $pdo->prepare('INSERT OR IGNORE INTO group_menus (group_id, menu_id) VALUES (?, ?)');
        $has = $pdo->prepare('SELECT 1 FROM menus WHERE id = ?');
        foreach (array_keys($menus) as $m) {
            $has->execute([$m]);
            if ($has->fetchColumn() !== false) {
                $ins->execute([$groupId, $m]);
            }
        }
        $pdo->prepare('DELETE FROM group_queues WHERE group_id = ?')->execute([$groupId]);
        $ins = $pdo->prepare('INSERT OR IGNORE INTO group_queues (group_id, queue_address) VALUES (?, ?)');
        $has = $pdo->prepare('SELECT 1 FROM queues WHERE address = ?');
        foreach (array_keys($queues) as $qa) {
            $has->execute([$qa]);
            if ($has->fetchColumn() !== false) {
                $ins->execute([$groupId, $qa]);
            }
        }
        $pdo->commit();
    } catch (Throwable $e) {
        if ($pdo->inTransaction()) {
            $pdo->rollBack();
        }
        throw $e;
    }
    bot_reset_panel_cache();
    return null;
}

/** Resumo (HTML) do que o grupo enxerga. Usado na tela de Usuarios. */
function bot_group_scope_summary(PDO $pdo, ?int $groupId): string
{
    if ($groupId === null || $groupId < 1) {
        return '<span class="badge bg-warning text-dark">sem grupo - sem acesso</span>';
    }
    $perm = [];
    $st = $pdo->prepare('SELECT perm_key FROM group_permissions WHERE group_id = ?');
    $st->execute([$groupId]);
    foreach ($st->fetchAll(PDO::FETCH_COLUMN) as $k) {
        $perm[(string) $k] = true;
    }
    $parts = [];
    if (isset($perm['scope.all_queues'])) {
        $parts[] = '<span class="badge bg-danger">todas as filas</span>';
    }
    if (isset($perm['scope.all_teams'])) {
        $parts[] = '<span class="badge bg-danger">todos os times</span>';
    }
    if (isset($perm['scope.all_menus'])) {
        $parts[] = '<span class="badge bg-danger">todos os menus</span>';
    }
    foreach ([['group_teams', 'time(s)'], ['group_queues', 'fila(s)'], ['group_menus', 'menu(s)']] as $spec) {
        $st = $pdo->prepare('SELECT COUNT(*) AS n FROM ' . $spec[0] . ' WHERE group_id = ?');
        $st->execute([$groupId]);
        $n = (int) $st->fetchColumn();
        if ($n > 0) {
            $parts[] = '<span class="badge badge-soft">' . $n . ' ' . $spec[1] . '</span>';
        }
    }
    if (!$parts) {
        return '<span class="badge bg-warning text-dark">sem escopo - nao ve dados</span>';
    }
    return implode(' ', $parts);
}

/** Render da tela Grupos (super-only). */
function bot_groups_render(PDO $pdo, string $csrf, string $formError = ''): void
{
    $groups = $pdo->query('SELECT id, name, active, is_super FROM permission_groups ORDER BY is_super DESC, name')->fetchAll();
    $permsOf = [];
    foreach ($pdo->query('SELECT group_id, perm_key FROM group_permissions') as $r) {
        $permsOf[(int) $r['group_id']][(string) $r['perm_key']] = true;
    }
    $usersOf = [];
    foreach ($pdo->query('SELECT group_id, COUNT(*) AS n FROM bot_users WHERE group_id IS NOT NULL GROUP BY group_id') as $r) {
        $usersOf[(int) $r['group_id']] = (int) $r['n'];
    }
    $teamsOf = [];
    foreach ($pdo->query('SELECT gt.group_id AS gid, t.id AS tid, t.name AS tname FROM group_teams gt JOIN teams t ON t.id = gt.team_id') as $r) {
        $teamsOf[(int) $r['gid']][(int) $r['tid']] = (string) $r['tname'];
    }
    $menusOf = [];
    foreach ($pdo->query('SELECT gm.group_id AS gid, m.id AS mid, m.name AS mname FROM group_menus gm JOIN menus m ON m.id = gm.menu_id') as $r) {
        $menusOf[(int) $r['gid']][(int) $r['mid']] = (string) $r['mname'];
    }
    $queuesOf = [];
    foreach ($pdo->query('SELECT group_id AS gid, queue_address AS a FROM group_queues') as $r) {
        $queuesOf[(int) $r['gid']][] = (string) $r['a'];
    }
    $teamOpts = [];
    foreach ($pdo->query('SELECT id, name, active FROM teams ORDER BY name') as $t) {
        $teamOpts[(int) $t['id']] = (string) $t['name'] . ((int) $t['active'] === 1 ? '' : ' (inativo)');
    }
    $menuOpts = [];
    foreach ($pdo->query('SELECT id, name, active FROM menus ORDER BY name') as $m) {
        $menuOpts[(int) $m['id']] = (string) $m['name'] . ((int) $m['active'] === 1 ? '' : ' (inativo)');
    }
    $queueOpts = [];
    foreach ($pdo->query('SELECT address, name FROM queues ORDER BY name, address') as $q) {
        $queueOpts[(string) $q['address']] = ($q['name'] !== '' ? $q['name'] . ' - ' : '') . (string) $q['address'];
    }
    $byGroup = [];
    foreach (bot_permission_catalog() as $key => $info) {
        $byGroup[(string) $info['group']][(string) $key] = (string) $info['label'];
    }

    page_header(
        'Grupos de permissao',
        'O grupo define quais paginas e acoes o usuario pode usar. O escopo de dados (quais filas ele ve) vem dos times do usuario, na aba Usuarios.',
        '<button class="btn btn-primary btn-sm" type="button" data-bs-toggle="collapse" data-bs-target="#novo-grupo">Novo grupo</button>'
    );
    if ($formError !== '') {
        echo '<div class="alert alert-danger">' . h($formError) . '</div>';
    }
    echo '<div class="collapse mb-3" id="novo-grupo"><div class="card card-slim"><div class="card-body">';
    echo '<form method="post" class="row g-2 align-items-end"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="create">';
    echo '<div class="col-12 col-md-4"><label class="form-label">Nome do grupo</label><input class="form-control form-control-sm" name="name" required placeholder="Triagem / Supervisao"></div>';
    echo '<div class="col-12 col-md-3"><button class="btn btn-primary btn-sm">Criar grupo</button></div>';
    echo '<div class="col-12"><p class="text-muted small mb-0">O grupo nasce sem nenhuma permissao: marque as permissoes depois de criar.</p></div>';
    echo '</form></div></div></div>';

    echo '<p class="text-muted small">Escopo: sem a permissao <code>scope.all_queues</code>, o usuario ve apenas dados das filas dos times dele (e das filas avulsas marcadas em Usuarios). Sem time nenhum, ele nao ve dado nenhum.</p>';
    echo '<div class="accordion" id="groups-acc">';
    $first = true;
    foreach ($groups as $g) {
        $gid = (int) $g['id'];
        $isSuper = (int) $g['is_super'] === 1;
        $on = bot_perm_truthy($g['active']);
        $show = $first ? ' show' : '';
        $collapsed = $first ? '' : ' collapsed';
        $first = false;
        $nPerms = $isSuper ? count(bot_permission_catalog()) : count($permsOf[$gid] ?? []);
        echo '<div class="accordion-item" id="group-' . $gid . '">';
        echo '<h2 class="accordion-header"><button class="accordion-button' . $collapsed . '" type="button" data-bs-toggle="collapse" data-bs-target="#group-body-' . $gid . '">';
        echo h((string) $g['name']) . ' ' . badge_state($on);
        echo ' <span class="badge badge-soft">' . (int) ($usersOf[$gid] ?? 0) . ' usuario(s)</span>';
        echo ' <span class="badge badge-soft">escopo: ' . count($teamsOf[$gid] ?? []) . 'T / ' . count($queuesOf[$gid] ?? []) . 'F / ' . count($menusOf[$gid] ?? []) . 'M</span>';
        echo ' <span class="badge ' . ($isSuper ? 'bg-danger' : 'badge-soft') . '">' . ($isSuper ? 'acesso total' : $nPerms . ' permissao(oes)') . '</span>';
        echo '</button></h2>';
        echo '<div id="group-body-' . $gid . '" class="accordion-collapse collapse' . $show . '" data-bs-parent="#groups-acc"><div class="accordion-body">';
        if ($isSuper) {
            echo '<p class="text-muted small mb-0">Grupo do dono do painel: tem todas as permissoes, nao pode ser desativado nem excluido, e e o unico que cria usuarios e grupos.</p>';
        } else {
            echo '<form method="post" class="row g-2 align-items-end mb-3"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="group_id" value="' . $gid . '"><input type="hidden" name="action" value="rename">';
            echo '<div class="col-12 col-md-4"><label class="form-label">Nome</label><input class="form-control form-control-sm" name="name" value="' . h((string) $g['name']) . '" required></div>';
            echo '<div class="col-auto"><button class="btn btn-primary btn-sm">Salvar nome</button></div></form>';
            echo '<form class="d-inline" method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="group_id" value="' . $gid . '">';
            echo '<button class="btn btn-outline-secondary btn-sm me-1" name="action" value="toggle">Ativar/desativar</button>';
            echo '<button class="btn btn-outline-danger btn-sm" name="action" value="delete" onclick="return confirm(\'Excluir o grupo? Os usuarios ficam sem grupo.\')">Excluir</button></form>';
            echo '<hr>';
            echo '<form method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="group_id" value="' . $gid . '"><input type="hidden" name="action" value="save_permissions">';
            foreach ($byGroup as $grpName => $items) {
                echo '<div class="mb-3"><h3 class="h6 text-uppercase text-muted" style="font-size:.75rem;letter-spacing:.04em">' . h($grpName) . '</h3><div class="row g-1">';
                foreach ($items as $key => $label) {
                    echo '<div class="col-12 col-md-6 col-xl-4"><label class="form-check"><input class="form-check-input" type="checkbox" name="perms[]" value="' . h($key) . '"' . (isset($permsOf[$gid][$key]) ? ' checked' : '') . '> <span class="form-check-label small">' . h($label) . '</span></label></div>';
                }
                echo '</div></div>';
            }
            echo '<button class="btn btn-primary btn-sm">Salvar permissoes</button></form>';
            echo '<hr><h3 class="h6">Escopo de dados</h3>';
            echo '<p class="text-muted small mb-2">Sem marcacao o grupo <strong>nao ve dado nenhum</strong> (negar por padrao). As chaves <em>Ver TODOS...</em> da matriz acima ignoram as marcacoes. Menu marcado libera a configuracao daquele menu e <strong>nao</strong> concede as filas dele.</p>';
            if (!isset($permsOf[$gid]['scope.all_queues']) && !isset($permsOf[$gid]['scope.all_teams']) && !isset($permsOf[$gid]['scope.all_menus'])
                && !($teamsOf[$gid] ?? []) && !($queuesOf[$gid] ?? []) && !($menusOf[$gid] ?? [])) {
                echo '<div class="alert alert-warning py-2 small">Este grupo esta sem escopo: os usuarios dele nao veem filas, times nem menus.</div>';
            }
            echo '<form method="post" class="row g-2"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="group_id" value="' . $gid . '"><input type="hidden" name="action" value="save_scope">';
            echo '<div class="col-12 col-xl-4"><label class="form-label">Times (libera o time e as filas dele)</label><div class="border rounded bg-white p-2 list-scroll">';
            if ($teamOpts) {
                foreach ($teamOpts as $tid => $tlab) {
                    echo '<label class="form-check"><input class="form-check-input" type="checkbox" name="scope_teams[]" value="' . (int) $tid . '"' . (isset($teamsOf[$gid][$tid]) ? ' checked' : '') . '> <span class="form-check-label small">' . h($tlab) . '</span></label>';
                }
            } else {
                echo '<span class="text-muted small">Nenhum time cadastrado.</span>';
            }
            echo '</div></div>';
            echo '<div class="col-12 col-xl-4"><label class="form-label">Filas (inclusive as que nao tem time)</label>';
            ui_multiselect('scope_queues', $queueOpts, $queuesOf[$gid] ?? [], 'Nenhuma fila');
            echo '<div class="form-text">As filas dos times marcados entram automaticamente.</div></div>';
            echo '<div class="col-12 col-xl-4"><label class="form-label">Menus (configuracao)</label><div class="border rounded bg-white p-2 list-scroll">';
            if ($menuOpts) {
                foreach ($menuOpts as $mid => $mlab) {
                    echo '<label class="form-check"><input class="form-check-input" type="checkbox" name="scope_menus[]" value="' . (int) $mid . '"' . (isset($menusOf[$gid][$mid]) ? ' checked' : '') . '> <span class="form-check-label small">' . h($mlab) . '</span></label>';
                }
            } else {
                echo '<span class="text-muted small">Nenhum menu cadastrado.</span>';
            }
            echo '</div></div>';
            echo '<div class="col-12"><button class="btn btn-primary btn-sm">Salvar escopo</button></div>';
            echo '</form>';
        }
        echo '</div></div></div>';
    }
    echo '</div>';
    if (!$groups) {
        echo '<p class="text-muted">Nenhum grupo.</p>';
    }
}