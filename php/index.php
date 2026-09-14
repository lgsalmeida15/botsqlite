<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';
session_start();

$pdo = bot_pdo();
$page = preg_replace('/[^a-z_]/', '', (string) ($_GET['page'] ?? 'queues')) ?: 'queues';
$public = ['login'];

function require_user(): void
{
    if (empty($_SESSION['bot_user_id'])) {
        header('Location: ?page=login');
        exit;
    }
}

function csrf_token(): string
{
    if (empty($_SESSION['csrf'])) {
        $_SESSION['csrf'] = bin2hex(random_bytes(16));
    }
    return $_SESSION['csrf'];
}

function csrf_ok(): bool
{
    return isset($_POST['csrf'], $_SESSION['csrf']) && hash_equals($_SESSION['csrf'], (string) $_POST['csrf']);
}

function posted_queue_addresses(): array
{
    $wanted = [];
    foreach ((array) ($_POST['queues'] ?? []) as $qa) {
        $qa = trim((string) $qa);
        if (substr($qa, 0, 2) === 'h:') {
            $wanted[$qa] = true;
        }
    }
    return array_keys($wanted);
}

function option_leaf_actions(): array
{
    return ['reply_and_close', 'ask_and_transfer', 'transfer_only', 'run_script'];
}

function scripts_manifest(): array
{
    static $cached = null;
    if ($cached !== null) {
        return $cached;
    }
    $cached = [];
    $path = bot_root() . DIRECTORY_SEPARATOR . 'python' . DIRECTORY_SEPARATOR . 'scripts' . DIRECTORY_SEPARATOR . 'manifest.json';
    if (!is_readable($path)) {
        return $cached;
    }
    $raw = file_get_contents($path);
    if ($raw === false) {
        return $cached;
    }
    $data = json_decode($raw, true);
    if (!is_array($data) || !isset($data['scripts']) || !is_array($data['scripts'])) {
        return $cached;
    }
    foreach ($data['scripts'] as $s) {
        if (!is_array($s)) {
            continue;
        }
        $slug = trim((string) ($s['slug'] ?? ''));
        if ($slug === '') {
            continue;
        }
        $cached[] = [
            'slug' => $slug,
            'label' => (string) ($s['label'] ?? $slug),
        ];
    }
    return $cached;
}

function script_slug_known(string $slug): bool
{
    foreach (scripts_manifest() as $s) {
        if ($s['slug'] === $slug) {
            return true;
        }
    }
    return false;
}

/** @return array{0:?string,1:string} error, slug */
function posted_script_slug(string $act): array
{
    if ($act !== 'run_script') {
        return [null, ''];
    }
    $slug = trim((string) ($_POST['script_slug'] ?? ''));
    if ($slug === '' || !script_slug_known($slug)) {
        return ['Selecione um script válido.', $slug];
    }
    return [null, $slug];
}

function option_action_ok(string $a, bool $allowSubmenu = true): bool
{
    $ok = option_leaf_actions();
    if ($allowSubmenu) {
        $ok[] = 'submenu';
    }
    return in_array($a, $ok, true);
}

function option_parent_id(array $row): int
{
    if (!array_key_exists('parent_id', $row) || $row['parent_id'] === null || $row['parent_id'] === '') {
        return 0;
    }
    return (int) $row['parent_id'];
}

function posted_parent_id(): int
{
    return max(0, (int) ($_POST['parent_id'] ?? 0));
}

function option_number_taken(PDO $pdo, int $menuId, int $sort, ?int $exceptId = null, int $parentId = 0): bool
{
    if ($parentId > 0) {
        $sql = 'SELECT 1 FROM menu_options WHERE menu_id = ? AND parent_id = ? AND sort_order = ?';
        $params = [$menuId, $parentId, $sort];
    } else {
        $sql = 'SELECT 1 FROM menu_options WHERE menu_id = ? AND parent_id IS NULL AND sort_order = ?';
        $params = [$menuId, $sort];
    }
    if ($exceptId) {
        $sql .= ' AND id <> ?';
        $params[] = $exceptId;
    }
    $st = $pdo->prepare($sql);
    $st->execute($params);
    return (bool) $st->fetch();
}

function option_load_parent(PDO $pdo, int $menuId, int $parentId): ?array
{
    if ($parentId < 1) {
        return null;
    }
    $st = $pdo->prepare('SELECT id, action, parent_id FROM menu_options WHERE id = ? AND menu_id = ?');
    $st->execute([$parentId, $menuId]);
    $row = $st->fetch();
    return $row ?: null;
}

function option_has_children(PDO $pdo, int $oid): bool
{
    $st = $pdo->prepare('SELECT 1 FROM menu_options WHERE parent_id = ?');
    $st->execute([$oid]);
    return (bool) $st->fetch();
}

/** @return array{0:?string,1:string,2:string} error, classification, reply */
function option_prepare_fields(
    PDO $pdo,
    int $mid,
    int $sort,
    string $label,
    string $classif,
    string $act,
    string $reply,
    int $parentId,
    ?int $exceptId = null
): array {
    if ($sort < 1 || $label === '') {
        return ['Preencha número e rótulo da opção.', $classif, $reply];
    }
    if ($parentId > 0) {
        $p = option_load_parent($pdo, $mid, $parentId);
        if (!$p || $p['action'] !== 'submenu' || option_parent_id($p) !== 0) {
            return ['Submenu inválido.', $classif, $reply];
        }
        if (!option_action_ok($act, false)) {
            return ['Opção do submenu deve encerrar, transferir ou executar script.', $classif, $reply];
        }
        if ($classif === '') {
            return ['Preencha classificação da opção.', $classif, $reply];
        }
    } else {
        if (!option_action_ok($act, true)) {
            return ['Ação inválida.', $classif, $reply];
        }
        if ($act === 'submenu') {
            $classif = '';
            $reply = '';
        } elseif ($classif === '') {
            return ['Preencha número, rótulo, classificação e ação da opção.', $classif, $reply];
        }
    }
    if ($exceptId && $act !== 'submenu' && option_has_children($pdo, $exceptId)) {
        return ['Remova as opções do submenu antes de mudar a ação.', $classif, $reply];
    }
    if (option_number_taken($pdo, $mid, $sort, $exceptId, $parentId)) {
        return ['Já existe opção ' . $sort . ' neste nível.', $classif, $reply];
    }
    return [null, $classif, $reply];
}

/** @return array{0:?string,1:string,2:string} error, action_if_form, reply_text_if_form */
function posted_form_overrides(string $act): array
{
    if ($act === 'submenu' || $act === 'run_script') {
        return [null, '', ''];
    }
    $formAct = trim((string) ($_POST['action_if_form'] ?? ''));
    $formReply = (string) ($_POST['reply_text_if_form'] ?? '');
    if ($formAct !== '' && !option_action_ok($formAct, false)) {
        return ['Ação com formulário aberto inválida.', $formAct, $formReply];
    }
    return [null, $formAct, $formReply];
}

function pdo_friendly_error(PDOException $e): string
{
    $code = (string) $e->getCode();
    $msg = $e->getMessage();
    if ($code === '23000' || $code === '23505' || strpos($msg, 'UNIQUE') !== false || strpos($msg, '23505') !== false) {
        return 'Já existe um registro com esses dados (fila em outro menu/time ou número de opção repetido).';
    }
    if ($code === '23503' || strpos($msg, 'FOREIGN KEY') !== false || strpos($msg, '23503') !== false) {
        return 'Não foi possível excluir: ainda há registros ligados a este item.';
    }
    return 'Não foi possível salvar. Tente de novo.';
}

function normalize_time_post(string $raw): ?string
{
    $raw = trim($raw);
    if (!preg_match('/^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?$/', $raw, $m)) {
        return null;
    }
    $h = (int) $m[1];
    $i = (int) $m[2];
    $s = isset($m[3]) && $m[3] !== '' ? (int) $m[3] : 0;
    if ($h > 23 || $i > 59 || $s > 59) {
        return null;
    }
    return sprintf('%02d:%02d:%02d', $h, $i, $s);
}

/** @return array{0:?string,1:?string,2:?string} start, end, error */
function posted_route_times(): array
{
    $startRaw = trim((string) ($_POST['route_start'] ?? ''));
    $endRaw = trim((string) ($_POST['route_end'] ?? ''));
    if ($startRaw === '' && $endRaw === '') {
        return [null, null, null];
    }
    if ($startRaw === '' || $endRaw === '') {
        return [null, null, 'Informe horário início e fim, ou deixe os dois vazios (24h).'];
    }
    $start = normalize_time_post($startRaw);
    $end = normalize_time_post($endRaw);
    if ($start === null || $end === null) {
        return [null, null, 'Horário inválido. Use HH:MM.'];
    }
    return [$start, $end, null];
}

function time_to_hms($v): ?string
{
    if ($v === null || $v === '') {
        return null;
    }
    if ($v instanceof DateTimeInterface) {
        return $v->format('H:i:s');
    }
    $s = trim((string) $v);
    if (!preg_match('/(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?/', $s, $m)) {
        return null;
    }
    $h = (int) $m[1];
    $i = (int) $m[2];
    $sec = isset($m[3]) && $m[3] !== '' ? (int) $m[3] : 0;
    if ($h > 23 || $i > 59 || $sec > 59) {
        return null;
    }
    return sprintf('%02d:%02d:%02d', $h, $i, $sec);
}

function time_input_value($v): string
{
    $hms = time_to_hms($v);
    return $hms === null ? '' : substr($hms, 0, 5);
}

function route_hms_minutes(?string $hms): ?int
{
    if ($hms === null || $hms === '') {
        return null;
    }
    $parts = explode(':', $hms);
    if (count($parts) < 2) {
        return null;
    }
    return ((int) $parts[0]) * 60 + (int) $parts[1];
}

function route_crosses_midnight($start, $end): bool
{
    $startS = time_to_hms($start);
    $endS = time_to_hms($end);
    if ($startS === null || $endS === null) {
        return false;
    }
    $startM = route_hms_minutes($startS);
    $endM = route_hms_minutes($endS);
    return $startM !== null && $endM !== null && $startM > $endM;
}

function member_in_route_window($start, $end): bool
{
    $startS = time_to_hms($start);
    $endS = time_to_hms($end);
    if ($startS === null || $endS === null) {
        return true;
    }
    $startM = route_hms_minutes($startS);
    $endM = route_hms_minutes($endS);
    if ($startM === null || $endM === null || $startM === $endM) {
        return true;
    }
    $now = new DateTimeImmutable('now', new DateTimeZone('America/Sao_Paulo'));
    $nowM = ((int) $now->format('G')) * 60 + (int) $now->format('i');
    if ($startM < $endM) {
        return $startM <= $nowM && $nowM < $endM;
    }
    return $nowM >= $startM || $nowM < $endM;
}

function action_select(string $name, string $current = '', bool $allowSubmenu = true): void
{
    $opts = [
        'reply_and_close' => 'Responder e encerrar',
        'ask_and_transfer' => 'Pedir info e transferir',
        'transfer_only' => 'Só transferir',
        'run_script' => 'Executar script',
    ];
    if ($allowSubmenu) {
        $opts['submenu'] = 'Abrir submenu';
    }
    echo '<select class="form-select" name="' . h($name) . '">';
    foreach ($opts as $k => $lab) {
        echo '<option value="' . h($k) . '"' . ($current === $k ? ' selected' : '') . '>' . h($lab) . '</option>';
    }
    echo '</select>';
}

function script_slug_select(string $current = ''): void
{
    echo '<select class="form-select" name="script_slug">';
    echo '<option value="">—</option>';
    foreach (scripts_manifest() as $s) {
        $slug = $s['slug'];
        $lab = $s['label'] !== $slug ? $s['label'] . ' (' . $slug . ')' : $slug;
        echo '<option value="' . h($slug) . '"' . ($current === $slug ? ' selected' : '') . '>' . h($lab) . '</option>';
    }
    echo '</select>';
}

function action_if_form_select(string $current = ''): void
{
    $opts = [
        '' => 'Igual à ação acima',
        'reply_and_close' => 'Responder e encerrar',
        'ask_and_transfer' => 'Pedir info e transferir',
        'transfer_only' => 'Só transferir',
    ];
    echo '<select class="form-select" name="action_if_form">';
    foreach ($opts as $k => $lab) {
        echo '<option value="' . h($k) . '"' . ($current === $k ? ' selected' : '') . '>' . h($lab) . '</option>';
    }
    echo '</select>';
}

function echo_option_form(string $csrf, int $menuId, array $o, int $parentId, bool $allowSubmenu): void
{
    $isNew = empty($o['id']);
    $act = (string) ($o['action'] ?? '');
    $classifRequired = $parentId > 0;
    echo '<form method="post" class="border rounded p-3 mb-2 ' . ($parentId > 0 ? 'bg-white' : 'bg-light') . '">';
    echo '<input type="hidden" name="csrf" value="' . h($csrf) . '">';
    echo '<input type="hidden" name="action" value="' . ($isNew ? 'add_option' : 'save_option') . '">';
    echo '<input type="hidden" name="menu_id" value="' . $menuId . '">';
    echo '<input type="hidden" name="parent_id" value="' . $parentId . '">';
    if (!$isNew) {
        echo '<input type="hidden" name="id" value="' . (int) $o['id'] . '">';
    }
    if ($isNew) {
        echo '<h3 class="h6">' . ($parentId > 0 ? 'Nova opção do submenu' : 'Nova opção') . '</h3>';
    }
    echo '<div class="row g-3">';
    $sortVal = isset($o['sort_order']) ? (string) (int) $o['sort_order'] : '';
    echo '<div class="col-md-3"><label class="form-label">Número</label><input class="form-control" type="number" name="sort_order" min="1" value="' . h($sortVal) . '" required></div>';
    echo '<div class="col-md-9"><label class="form-label">Rótulo</label><input class="form-control" name="label" value="' . h((string) ($o['label'] ?? '')) . '" required></div>';
    echo '<div class="col-md-6"><label class="form-label">Classificação';
    if ($allowSubmenu) {
        echo ' <span class="text-muted fw-normal">(só na folha)</span>';
    }
    echo '</label><input class="form-control" name="classification" value="' . h((string) ($o['classification'] ?? '')) . '"' . ($classifRequired ? ' required' : '') . '></div>';
    echo '<div class="col-md-6"><label class="form-label">Ação</label>';
    action_select('action_type', $act, $allowSubmenu);
    echo '</div>';
    echo '<div class="col-md-6"><label class="form-label">Script <span class="text-muted fw-normal">(se a ação for Executar script)</span></label>';
    script_slug_select((string) ($o['script_slug'] ?? ''));
    echo '</div>';
    echo '<div class="col-12"><label class="form-label">Texto de resposta</label><textarea class="form-control" name="reply_text" rows="2">' . h((string) ($o['reply_text'] ?? '')) . '</textarea></div>';
    if ($act !== 'submenu' && $act !== 'run_script') {
        echo '<div class="col-12"><hr class="my-1"><h4 class="h6 mb-0">Se o técnico tiver formulário de validação aberto</h4></div>';
        echo '<div class="col-md-6"><label class="form-label">Ação</label>';
        action_if_form_select((string) ($o['action_if_form'] ?? ''));
        echo '</div>';
        echo '<div class="col-12"><label class="form-label">Texto de resposta</label><textarea class="form-control" name="reply_text_if_form" rows="2" placeholder="Vazio = mesmo texto acima">' . h((string) ($o['reply_text_if_form'] ?? '')) . '</textarea></div>';
    }
    echo '</div><button class="btn btn-primary mt-3">' . ($isNew ? 'Adicionar opção' : 'Salvar opção') . '</button></form>';
}

function option_action_label(string $act): string
{
    $map = [
        'reply_and_close' => 'Responder e encerrar',
        'ask_and_transfer' => 'Pedir info e transferir',
        'transfer_only' => 'Só transferir',
        'run_script' => 'Executar script',
        'submenu' => 'Abrir submenu',
    ];
    return $map[$act] ?? $act;
}

function option_action_cell(array $o): string
{
    $act = (string) ($o['action'] ?? '');
    $lab = option_action_label($act);
    if ($act === 'run_script') {
        $slug = (string) ($o['script_slug'] ?? '');
        if ($slug !== '') {
            $lab .= ' (' . $slug . ')';
        }
    }
    return $lab;
}

function option_sort_cmp(array $a, array $b): int
{
    $d = ((int) $a['sort_order']) <=> ((int) $b['sort_order']);
    return $d !== 0 ? $d : ((int) $a['id'] <=> (int) $b['id']);
}

function option_form_collapse_id(int $menuId, int $optionId, int $parentId = 0): string
{
    if ($optionId > 0) {
        return 'opt-form-' . $menuId . '-' . $optionId;
    }
    if ($parentId > 0) {
        return 'opt-form-' . $menuId . '-new-' . $parentId;
    }
    return 'opt-form-' . $menuId . '-new';
}

function echo_option_editor_row(string $csrf, int $menuId, array $o, int $parentId, bool $allowSubmenu, string $collapseId, bool $open): void
{
    echo '<tr class="opt-editor-row"><td colspan="6" class="p-0 border-0">';
    echo '<div class="collapse opt-editor' . ($open ? ' show' : '') . '" id="' . h($collapseId) . '">';
    echo '<div class="p-3 bg-light border-top">';
    echo_option_form($csrf, $menuId, $o, $parentId, $allowSubmenu);
    echo '</div></div></td></tr>';
}

function echo_option_delete_btn(string $csrf, int $menuId, int $oid, bool $hasKids): void
{
    $msg = $hasKids ? 'Excluir esta opção e o submenu, se houver?' : 'Excluir esta opção?';
    echo '<form class="d-inline" method="post">';
    echo '<input type="hidden" name="csrf" value="' . h($csrf) . '">';
    echo '<input type="hidden" name="menu_id" value="' . $menuId . '">';
    echo '<input type="hidden" name="id" value="' . $oid . '">';
    echo '<button class="btn btn-outline-danger btn-sm" name="action" value="delete_option" onclick="return confirm(\'' . h($msg) . '\')">Excluir</button></form>';
}

function echo_option_data_row(
    string $csrf,
    int $menuId,
    array $o,
    bool $child,
    string $collapseId,
    bool $isSub,
    int $kidCount
): void {
    echo '<tr' . ($child ? ' class="opt-child"' : '') . '>';
    echo '<td>' . (int) $o['sort_order'] . '</td>';
    echo '<td>' . h((string) $o['label']);
    if ($isSub && $kidCount === 0) {
        echo ' <span class="badge bg-warning text-dark">vazio</span>';
    }
    echo '</td>';
    echo '<td>' . ($isSub ? '<span class="badge bg-primary">Submenu</span>' : '<span class="badge bg-secondary">Folha</span>') . '</td>';
    echo '<td>' . ($isSub ? '—' : h(option_action_cell($o))) . '</td>';
    echo '<td>' . ($isSub ? '—' : h((string) ($o['classification'] ?? ''))) . '</td>';
    echo '<td class="text-nowrap">';
    echo '<button type="button" class="btn btn-outline-primary btn-sm me-1" data-bs-toggle="collapse" data-bs-target="#' . h($collapseId) . '">Editar</button>';
    if ($isSub) {
        $newId = option_form_collapse_id($menuId, 0, (int) $o['id']);
        echo '<button type="button" class="btn btn-outline-secondary btn-sm me-1" data-bs-toggle="collapse" data-bs-target="#' . h($newId) . '">+ Filho</button>';
    }
    echo_option_delete_btn($csrf, $menuId, (int) $o['id'], $isSub);
    echo '</td></tr>';
}

function echo_queues_card(string $csrf, string $hiddenName, int $id, string $title, array $queues, array $owners, string $ownerKind): void
{
    $n = 0;
    foreach ($queues as $q) {
        $owner = $owners[$q['address']] ?? null;
        if ($owner && (int) $owner['id'] === $id) {
            $n++;
        }
    }
    $cid = 'queues-box-' . $hiddenName . '-' . $id;
    echo '<div class="card mb-3"><div class="card-header py-2 d-flex justify-content-between align-items-center">';
    echo '<button class="btn btn-sm btn-outline-secondary" type="button" data-bs-toggle="collapse" data-bs-target="#' . h($cid) . '">';
    echo h($title) . ' <span class="badge text-bg-light">' . $n . '</span></button></div>';
    echo '<div class="collapse" id="' . h($cid) . '"><div class="card-body py-2">';
    echo '<form method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="save_queues">';
    echo '<input type="hidden" name="' . h($hiddenName) . '" value="' . $id . '">';
    queue_picker($queues, $owners, $id, $ownerKind);
    echo '<button class="btn btn-primary btn-sm mt-2">Salvar filas</button></form>';
    echo '</div></div></div>';
}

function queue_checkboxes(array $queues, array $owners, int $selfId, string $ownerKind): void
{
    foreach ($queues as $q) {
        $addr = $q['address'];
        $owner = $owners[$addr] ?? null;
        $mine = $owner && (int) $owner['id'] === $selfId;
        $other = $owner && (int) $owner['id'] !== $selfId;
        $checked = $mine ? ' checked' : '';
        $disabled = $other ? ' disabled' : '';
        $note = $other ? ' — já no ' . $ownerKind . ' ' . $owner['name'] : '';
        $label = ($q['name'] ?: $addr) . ' — ' . $addr . $note;
        echo '<label class="form-check' . ($other ? ' off-q text-muted' : '') . '" data-filter="' . h($label) . '">';
        echo '<input class="form-check-input" type="checkbox" name="queues[]" value="' . h($addr) . '"' . $checked . $disabled . '> ';
        echo '<span class="form-check-label">' . h($label) . '</span></label>';
    }
}

function bool_on($v): bool
{
    return $v === true || $v === 't' || $v === 1 || $v === '1';
}

function badge_state(bool $on, string $yes = 'ativo', string $no = 'inativo'): string
{
    return '<span class="badge ' . ($on ? 'bg-success' : 'bg-secondary') . '">' . h($on ? $yes : $no) . '</span>';
}

function event_badge(string $reason, string $label): string
{
    $map = [
        'transferred' => 'bg-success',
        'assumed' => 'bg-success',
        'menu_sent' => 'bg-success',
        'classified' => 'bg-success',
        'script_ok' => 'bg-success',
        'script_error' => 'bg-danger',
        'script_denied' => 'bg-warning text-dark',
        'catalog_synced' => 'bg-success',
        'sync_error' => 'bg-danger',
        'no_agent' => 'bg-warning text-dark',
        'ignored_reply' => 'bg-warning text-dark',
        'closed_invalid' => 'bg-danger',
        'closed_timeout' => 'bg-danger',
        'bot_off' => 'bg-danger',
        'queue_off' => 'bg-secondary',
    ];
    return '<span class="badge ' . ($map[$reason] ?? 'bg-secondary') . '">' . h($label) . '</span>';
}

function status_badge(string $status): string
{
    $map = [
        'classified' => 'bg-success',
        'monitoring' => 'bg-primary',
        'awaiting_reply' => 'bg-warning text-dark',
        'awaiting_script_answer' => 'bg-warning text-dark',
        'awaiting_script_confirm' => 'bg-warning text-dark',
        'awaiting_script_run' => 'bg-warning text-dark',
        'joined' => 'bg-info text-dark',
        'detected' => 'bg-info text-dark',
        'left' => 'bg-secondary',
        'closed_timeout' => 'bg-danger',
        'closed_invalid' => 'bg-danger',
    ];
    return '<span class="badge ' . ($map[$status] ?? 'bg-secondary') . '">' . h($status) . '</span>';
}

function fmt_dt($v): string
{
    if ($v === null || $v === '') {
        return '';
    }
    try {
        if ($v instanceof DateTimeInterface) {
            $d = new DateTimeImmutable($v->format('c'));
        } else {
            $d = new DateTimeImmutable((string) $v);
        }
        return $d->setTimezone(new DateTimeZone('America/Sao_Paulo'))->format('d/m/Y H:i:s');
    } catch (Throwable $e) {
        return (string) $v;
    }
}

function list_page(): int
{
    return max(1, (int) ($_GET['p'] ?? 1));
}

function page_href(string $pageKey, int $p, array $filters = []): string
{
    $q = array_merge(['page' => $pageKey], $filters, ['p' => $p]);
    foreach ($q as $k => $v) {
        if ($v === '' || $v === null) {
            unset($q[$k]);
        }
    }
    return '?' . http_build_query($q);
}

function echo_pagination(string $pageKey, int $pageNum, int $total, array $filters = [], int $per = 50): void
{
    $pages = max(1, (int) ceil($total / $per));
    $from = $total === 0 ? 0 : (($pageNum - 1) * $per) + 1;
    $to = min($total, $pageNum * $per);
    echo '<div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mt-3">';
    echo '<p class="text-muted small mb-0">Mostrando ' . $from . '–' . $to . ' de ' . $total . '</p>';
    if ($pages > 1) {
        echo '<nav><ul class="pagination pagination-sm mb-0">';
        $window = 7;
        $start = max(1, $pageNum - 3);
        $end = min($pages, $start + $window - 1);
        $start = max(1, $end - $window + 1);
        if ($pageNum > 1) {
            echo '<li class="page-item"><a class="page-link" href="' . h(page_href($pageKey, $pageNum - 1, $filters)) . '">Anterior</a></li>';
        }
        if ($start > 1) {
            echo '<li class="page-item"><a class="page-link" href="' . h(page_href($pageKey, 1, $filters)) . '">1</a></li>';
            if ($start > 2) {
                echo '<li class="page-item disabled"><span class="page-link">…</span></li>';
            }
        }
        for ($i = $start; $i <= $end; $i++) {
            $active = $i === $pageNum ? ' active' : '';
            echo '<li class="page-item' . $active . '"><a class="page-link" href="' . h(page_href($pageKey, $i, $filters)) . '">' . $i . '</a></li>';
        }
        if ($end < $pages) {
            if ($end < $pages - 1) {
                echo '<li class="page-item disabled"><span class="page-link">…</span></li>';
            }
            echo '<li class="page-item"><a class="page-link" href="' . h(page_href($pageKey, $pages, $filters)) . '">' . $pages . '</a></li>';
        }
        if ($pageNum < $pages) {
            echo '<li class="page-item"><a class="page-link" href="' . h(page_href($pageKey, $pageNum + 1, $filters)) . '">Próxima</a></li>';
        }
        echo '</ul></nav>';
    }
    echo '</div>';
}

function queue_picker(array $queues, array $owners, int $selfId, string $ownerKind): void
{
    echo '<div class="queue-picker">';
    echo '<input type="search" class="form-control form-control-sm mb-2 queue-filter" placeholder="Filtrar filas…" autocomplete="off">';
    echo '<div class="queue-list list-group border rounded p-2 bg-white">';
    queue_checkboxes($queues, $owners, $selfId, $ownerKind);
    if (!$queues) {
        echo '<p class="text-muted small mb-0">Nenhuma fila ainda.</p>';
    }
    echo '</div></div>';
}

if ($page === 'login' && ($_SERVER['REQUEST_METHOD'] ?? '') === 'POST' && csrf_ok()) {
    $st = $pdo->prepare('SELECT id, password_hash, active FROM bot_users WHERE login = ?');
    $st->execute([trim((string) ($_POST['login'] ?? ''))]);
    $u = $st->fetch();
    if ($u && $u['active'] && password_verify((string) ($_POST['password'] ?? ''), $u['password_hash'])) {
        $_SESSION['bot_user_id'] = (int) $u['id'];
        header('Location: ?page=queues');
        exit;
    }
    $loginError = 'Login ou senha inválidos.';
}

if ($page === 'logout') {
    session_destroy();
    header('Location: ?page=login');
    exit;
}

if (!in_array($page, $public, true)) {
    require_user();
}

$postSynced = false;
if (($_SERVER['REQUEST_METHOD'] ?? '') === 'POST' && $page !== 'login') {
    if (!csrf_ok()) {
        http_response_code(400);
        exit('CSRF');
    }
    $action = (string) ($_POST['action'] ?? '');
    try {
        if ($page === 'queues' && $action === 'toggle') {
            $st = $pdo->prepare('UPDATE queues SET enabled = NOT enabled WHERE address = ?');
            $st->execute([(string) $_POST['address']]);
        }
        if (($page === 'queues' || $page === 'operators') && $action === 'sync_catalog') {
            $pdo->exec("INSERT INTO bot_jobs (kind) VALUES ('catalog_sync')");
            $postSynced = true;
        }
        if ($page === 'operators' && $action === 'add_operator') {
            $opName = trim((string) ($_POST['operator_name'] ?? ''));
            $opAddr = trim((string) ($_POST['operator_address'] ?? ''));
            $opQueues = posted_queue_addresses();
            if ($opName === '' || $opAddr === '') {
                $formError = 'Informe nome e address (u:login@dominio) do operador.';
            } elseif (substr($opAddr, 0, 2) !== 'u:') {
                $formError = 'Address do operador deve ser u:login@dominio.';
            } elseif (!$opQueues) {
                $formError = 'Selecione ao menos uma fila.';
            } else {
                $st = $pdo->prepare(
                    "INSERT INTO queue_operators (queue_address, address, name, source) VALUES (?, ?, ?, 'manual')
                     ON CONFLICT (queue_address, address) DO UPDATE
                     SET name = excluded.name,
                         source = CASE WHEN queue_operators.source = 'manual' THEN 'manual' ELSE excluded.source END"
                );
                foreach ($opQueues as $qa) {
                    $st->execute([$qa, $opAddr, $opName]);
                }
            }
        }
        if ($page === 'operators' && $action === 'remove_operator') {
            $qDel = trim((string) ($_POST['queue_address'] ?? ''));
            $aDel = trim((string) ($_POST['address'] ?? ''));
            $st = $pdo->prepare("DELETE FROM queue_operators WHERE queue_address = ? AND address = ? AND source = 'manual'");
            $st->execute([$qDel, $aDel]);
            if ($st->rowCount() === 0) {
                $formError = 'Registro não é manual ou não existe; itens vindos do TOA voltam na próxima sincronização.';
            }
        }
        if ($page === 'templates' && $action === 'save') {
            $st = $pdo->prepare("UPDATE templates SET body = ?, updated_at = datetime('now') WHERE slug = ?");
            $st->execute([(string) $_POST['body'], (string) $_POST['slug']]);
        }
        if ($page === 'settings' && $action === 'save') {
            $st = $pdo->prepare('INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value');
            foreach (['reply_timeout_sec', 'max_invalid_retries', 'config_poll_sec', 'bot_enabled', 'max_open_chats_per_agent', 'ws_stale_sec', 'transfer_attempts', 'transfer_retry_interval_sec'] as $k) {
                if (isset($_POST[$k])) {
                    $st->execute([$k, (string) $_POST[$k]]);
                }
            }
        }
        if ($page === 'menus') {
            $mid = (int) ($_POST['menu_id'] ?? 0);
            if ($action === 'create') {
                $name = trim((string) ($_POST['name'] ?? ''));
                if ($name === '') {
                    $formError = 'Informe o nome do menu.';
                } else {
                    $pdo->prepare('INSERT INTO menus (name, active) VALUES (?, 1)')->execute([$name]);
                }
            } elseif ($action === 'rename' && $mid > 0) {
                $name = trim((string) ($_POST['name'] ?? ''));
                if ($name === '') {
                    $formError = 'Informe o nome do menu.';
                } else {
                    $pdo->prepare('UPDATE menus SET name = ? WHERE id = ?')->execute([$name, $mid]);
                }
            } elseif ($action === 'toggle' && $mid > 0) {
                $pdo->prepare('UPDATE menus SET active = NOT active WHERE id = ?')->execute([$mid]);
            } elseif ($action === 'delete' && $mid > 0) {
                $pdo->prepare('DELETE FROM menus WHERE id = ?')->execute([$mid]);
            } elseif ($action === 'save_queues' && $mid > 0) {
                $wanted = posted_queue_addresses();
                $st = $pdo->prepare('SELECT queue_address, menu_id FROM menu_queues WHERE queue_address = ?');
                foreach ($wanted as $qa) {
                    $st->execute([$qa]);
                    $row = $st->fetch();
                    if ($row && (int) $row['menu_id'] !== $mid) {
                        $formError = 'A fila ' . $qa . ' já está em outro menu.';
                        break;
                    }
                }
                if (empty($formError)) {
                    $pdo->beginTransaction();
                    try {
                        $pdo->prepare('DELETE FROM menu_queues WHERE menu_id = ?')->execute([$mid]);
                        $ins = $pdo->prepare('INSERT INTO menu_queues (menu_id, queue_address) VALUES (?, ?)');
                        foreach ($wanted as $qa) {
                            $ins->execute([$mid, $qa]);
                        }
                        $pdo->commit();
                    } catch (Throwable $e) {
                        if ($pdo->inTransaction()) {
                            $pdo->rollBack();
                        }
                        throw $e;
                    }
                }
            } elseif ($action === 'add_option' && $mid > 0) {
                $sort = (int) ($_POST['sort_order'] ?? 0);
                $label = trim((string) ($_POST['label'] ?? ''));
                $classif = trim((string) ($_POST['classification'] ?? ''));
                $act = (string) ($_POST['action_type'] ?? '');
                $reply = (string) ($_POST['reply_text'] ?? '');
                $parentId = posted_parent_id();
                [$err, $classif, $reply] = option_prepare_fields($pdo, $mid, $sort, $label, $classif, $act, $reply, $parentId, null);
                if ($err !== null) {
                    $formError = $err;
                } else {
                    [$slugErr, $scriptSlug] = posted_script_slug($act);
                    if ($slugErr !== null) {
                        $formError = $slugErr;
                    } else {
                    [$formErr, $formAct, $formReply] = posted_form_overrides($act);
                    if ($formErr !== null) {
                        $formError = $formErr;
                    } else {
                        $st = $pdo->prepare(
                            'INSERT INTO menu_options (menu_id, queue_address, sort_order, label, classification, action, reply_text, reply_text_if_form, action_if_form, transfer_target_id, parent_id, script_slug)
                             VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)'
                        );
                        $st->execute([$mid, $sort, $label, $classif, $act, $reply, $formReply, $formAct, $parentId > 0 ? $parentId : null, $scriptSlug]);
                    }
                    }
                }
            } elseif ($action === 'save_option' && $mid > 0) {
                $oid = (int) ($_POST['id'] ?? 0);
                $sort = (int) ($_POST['sort_order'] ?? 0);
                $label = trim((string) ($_POST['label'] ?? ''));
                $classif = trim((string) ($_POST['classification'] ?? ''));
                $act = (string) ($_POST['action_type'] ?? '');
                $reply = (string) ($_POST['reply_text'] ?? '');
                $parentId = posted_parent_id();
                $st = $pdo->prepare('SELECT parent_id FROM menu_options WHERE id = ? AND menu_id = ?');
                $st->execute([$oid, $mid]);
                $existing = $st->fetch();
                if ($oid < 1 || !$existing) {
                    $formError = 'Opção inválida.';
                } elseif (option_parent_id($existing) !== $parentId) {
                    $formError = 'Opção inválida.';
                } else {
                    [$err, $classif, $reply] = option_prepare_fields($pdo, $mid, $sort, $label, $classif, $act, $reply, $parentId, $oid);
                    if ($err !== null) {
                        $formError = $err;
                    } else {
                        [$slugErr, $scriptSlug] = posted_script_slug($act);
                        if ($slugErr !== null) {
                            $formError = $slugErr;
                        } else {
                        [$formErr, $formAct, $formReply] = posted_form_overrides($act);
                        if ($formErr !== null) {
                            $formError = $formErr;
                        } else {
                            $pdo->prepare(
                                'UPDATE menu_options SET sort_order = ?, label = ?, classification = ?, action = ?, reply_text = ?, reply_text_if_form = ?, action_if_form = ?, script_slug = ?
                                 WHERE id = ? AND menu_id = ?'
                            )->execute([$sort, $label, $classif, $act, $reply, $formReply, $formAct, $scriptSlug, $oid, $mid]);
                        }
                        }
                    }
                }
            } elseif ($action === 'delete_option') {
                $pdo->prepare('DELETE FROM menu_options WHERE id = ? AND menu_id = COALESCE(NULLIF(?, 0), menu_id)')
                    ->execute([(int) ($_POST['id'] ?? 0), $mid]);
            }
        }
        if ($page === 'teams') {
            $tid = (int) ($_POST['team_id'] ?? 0);
            if ($action === 'create') {
                $name = trim((string) ($_POST['name'] ?? ''));
                if ($name === '') {
                    $formError = 'Informe o nome do time.';
                } else {
                    $pdo->prepare('INSERT INTO teams (name, active) VALUES (?, 1)')->execute([$name]);
                }
            } elseif ($action === 'rename' && $tid > 0) {
                $name = trim((string) ($_POST['name'] ?? ''));
                if ($name === '') {
                    $formError = 'Informe o nome do time.';
                } else {
                    $pdo->prepare('UPDATE teams SET name = ? WHERE id = ?')->execute([$name, $tid]);
                }
            } elseif ($action === 'toggle' && $tid > 0) {
                $pdo->prepare('UPDATE teams SET active = NOT active WHERE id = ?')->execute([$tid]);
            } elseif ($action === 'delete' && $tid > 0) {
                $pdo->prepare('DELETE FROM teams WHERE id = ?')->execute([$tid]);
            } elseif ($action === 'save_queues' && $tid > 0) {
                $wanted = posted_queue_addresses();
                $st = $pdo->prepare('SELECT queue_address, team_id FROM team_queues WHERE queue_address = ?');
                foreach ($wanted as $qa) {
                    $st->execute([$qa]);
                    $row = $st->fetch();
                    if ($row && (int) $row['team_id'] !== $tid) {
                        $formError = 'A fila ' . $qa . ' já está em outro time.';
                        break;
                    }
                }
                if (empty($formError)) {
                    $pdo->beginTransaction();
                    try {
                        $pdo->prepare('DELETE FROM team_queues WHERE team_id = ?')->execute([$tid]);
                        $ins = $pdo->prepare('INSERT INTO team_queues (team_id, queue_address) VALUES (?, ?)');
                        foreach ($wanted as $qa) {
                            $ins->execute([$tid, $qa]);
                        }
                        $pdo->commit();
                    } catch (Throwable $e) {
                        if ($pdo->inTransaction()) {
                            $pdo->rollBack();
                        }
                        throw $e;
                    }
                }
            } elseif ($action === 'add_member' && $tid > 0) {
                $addr = trim((string) ($_POST['address'] ?? ''));
                if ($addr === '') {
                    $addr = trim((string) ($_POST['catalog_address'] ?? ''));
                }
                $name = trim((string) ($_POST['member_name'] ?? ''));
                if (substr($addr, 0, 2) !== 'u:') {
                    $formError = 'Address do membro deve ser u:login@dominio.';
                } else {
                    if ($name === '') {
                        $st = $pdo->prepare('SELECT name FROM queue_operators WHERE address = ? ORDER BY (name IS NULL), name DESC LIMIT 1');
                        $st->execute([$addr]);
                        $name = (string) ($st->fetchColumn() ?: $addr);
                    }
                    $ord = (int) ($_POST['sort_order'] ?? 0);
                    [$routeStart, $routeEnd, $timeErr] = posted_route_times();
                    if ($timeErr) {
                        $formError = $timeErr;
                    } else {
                        $pdo->prepare(
                            'INSERT INTO team_members (team_id, address, name, sort_order, active, route_start, route_end)
                             VALUES (?, ?, ?, ?, 1, ?, ?)
                             ON CONFLICT (team_id, address) DO UPDATE
                             SET name = COALESCE(NULLIF(excluded.name, \'\'), team_members.name),
                                 sort_order = excluded.sort_order,
                                 active = 1,
                                 route_start = excluded.route_start,
                                 route_end = excluded.route_end'
                        )->execute([$tid, $addr, $name, $ord, $routeStart, $routeEnd]);
                    }
                }
            } elseif ($action === 'save_member' && $tid > 0) {
                $addr = (string) ($_POST['address'] ?? '');
                $name = trim((string) ($_POST['member_name'] ?? ''));
                $ord = (int) ($_POST['sort_order'] ?? 0);
                [$routeStart, $routeEnd, $timeErr] = posted_route_times();
                if ($addr === '') {
                    $formError = 'Membro inválido.';
                } elseif ($timeErr) {
                    $formError = $timeErr;
                } else {
                    $pdo->prepare(
                        'UPDATE team_members SET name = ?, sort_order = ?, route_start = ?, route_end = ?
                         WHERE team_id = ? AND address = ?'
                    )->execute([$name, $ord, $routeStart, $routeEnd, $tid, $addr]);
                }
            } elseif ($action === 'toggle_member' && $tid > 0) {
                $pdo->prepare('UPDATE team_members SET active = NOT active WHERE team_id = ? AND address = ?')
                    ->execute([$tid, (string) ($_POST['address'] ?? '')]);
            } elseif ($action === 'delete_member' && $tid > 0) {
                $pdo->prepare('DELETE FROM team_members WHERE team_id = ? AND address = ?')
                    ->execute([$tid, (string) $_POST['address']]);
            }
        }
    } catch (PDOException $e) {
        if ($pdo->inTransaction()) {
            $pdo->rollBack();
        }
        $formError = pdo_friendly_error($e);
    }
    if (empty($formError) && $page !== 'login') {
        $hash = '';
        if ($page === 'menus') {
            $hid = (int) ($_POST['menu_id'] ?? 0);
            if ($hid > 0 && $action !== 'delete') {
                $hash = '#menu-' . $hid;
            }
        }
        if ($page === 'teams') {
            $hid = (int) ($_POST['team_id'] ?? 0);
            if ($hid > 0 && $action !== 'delete') {
                $hash = '#team-' . $hid;
            }
        }
        header('Location: ?page=' . $page . $hash . ($postSynced ? '&synced=1' : ''));
        exit;
    }
}

$nav = [
    'dashboard' => 'Dashboard',
    'queues' => 'Filas',
    'operators' => 'Operadores',
    'activity' => 'Atividade',
    'templates' => 'Templates',
    'menus' => 'Menus',
    'teams' => 'Times',
    'settings' => 'Ajustes',
    'chats' => 'Chats',
];

function layout_start(string $title, array $nav, string $page, bool $autoRefresh = false): void
{
    echo '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">';
    echo '<title>' . h($title) . '</title>';
    if ($autoRefresh) {
        echo '<meta http-equiv="refresh" content="5">';
    }
    echo '<link href="assets/bootstrap.min.css" rel="stylesheet">';
    echo '<style>
      body{background:#f4f6f8}
      .navbar-helptoa{background:#0b3d2e}
      .off-q{color:#6c757d}
      .queue-list{max-height:200px;overflow:auto}
      .login-card{width:100%;max-width:400px}
      .d-contents{display:contents}
      .opt-child td:first-child{padding-left:1.75rem;border-left:3px solid #c5d4cc}
      tr.opt-editor-row > td{padding:0;border:0}
    </style></head><body>';
    if ($page !== 'login') {
        echo '<nav class="navbar navbar-expand-lg navbar-dark navbar-helptoa">';
        echo '<div class="container-xl"><a class="navbar-brand" href="?page=queues">HELPTOA Bot</a>';
        echo '<button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#botNav" aria-controls="botNav" aria-expanded="false" aria-label="Menu">';
        echo '<span class="navbar-toggler-icon"></span></button>';
        echo '<div class="collapse navbar-collapse" id="botNav"><ul class="navbar-nav me-auto mb-2 mb-lg-0">';
        foreach ($nav as $k => $label) {
            echo '<li class="nav-item"><a class="nav-link' . ($page === $k ? ' active' : '') . '" href="?page=' . h($k) . '">' . h($label) . '</a></li>';
        }
        echo '</ul><ul class="navbar-nav"><li class="nav-item"><a class="nav-link" href="?page=logout">Sair</a></li></ul>';
        echo '</div></div></nav>';
        echo '<main class="container-xl py-4">';
    } else {
        echo '<main class="min-vh-100 d-flex align-items-center justify-content-center p-3">';
    }
}

function layout_end(): void
{
    echo '</main>';
    echo '<script src="assets/bootstrap.bundle.min.js"></script>';
    echo '<script>
(function () {
  function openAccordion(hash) {
    if (!hash || (hash.indexOf("#menu-") !== 0 && hash.indexOf("#team-") !== 0)) return;
    var el = document.querySelector(hash);
    if (!el) return;
    var collapse = el.querySelector(".accordion-collapse");
    if (!collapse || typeof bootstrap === "undefined") return;
    document.querySelectorAll(".accordion-collapse.show").forEach(function (c) {
      if (c !== collapse) bootstrap.Collapse.getOrCreateInstance(c, {toggle: false}).hide();
    });
    bootstrap.Collapse.getOrCreateInstance(collapse, {toggle: false}).show();
    el.scrollIntoView({block: "start"});
  }
  function openHash() {
    openAccordion(location.hash);
  }
  openHash();
  window.addEventListener("hashchange", openHash);
  var ui = document.getElementById("ui-open");
  if (ui) {
    var acc = ui.getAttribute("data-hash") || "";
    var col = ui.getAttribute("data-collapse") || "";
    if (acc) openAccordion(acc);
    if (col && typeof bootstrap !== "undefined") {
      var target = document.querySelector(col);
      if (target) bootstrap.Collapse.getOrCreateInstance(target, {toggle: false}).show();
    }
  }
  document.querySelectorAll(".opt-editor").forEach(function (el) {
    el.addEventListener("show.bs.collapse", function () {
      var table = el.closest("table");
      if (!table || typeof bootstrap === "undefined") return;
      table.querySelectorAll(".opt-editor").forEach(function (other) {
        if (other !== el) bootstrap.Collapse.getOrCreateInstance(other, {toggle: false}).hide();
      });
    });
  });
  document.querySelectorAll(".queue-filter").forEach(function (input) {
    input.addEventListener("input", function () {
      var q = input.value.toLowerCase();
      var box = input.closest(".queue-picker");
      if (!box) return;
      box.querySelectorAll("[data-filter]").forEach(function (row) {
        var t = (row.getAttribute("data-filter") || "").toLowerCase();
        row.style.display = t.indexOf(q) === -1 ? "none" : "";
      });
    });
  });
})();
</script></body></html>';
}

$csrf = csrf_token();

if ($page === 'login') {
    layout_start('Login — HELPTOA Bot', [], 'login');
    echo '<div class="card shadow-sm login-card"><div class="card-body p-4">';
    echo '<h1 class="h4 mb-1">HELPTOA Bot</h1>';
    echo '<p class="text-muted mb-3">Acesso ao painel</p>';
    if (!empty($loginError)) {
        echo '<div class="alert alert-danger">' . h($loginError) . '</div>';
    }
    echo '<form method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
    echo '<div class="mb-3"><label class="form-label" for="bot-login">Login</label>';
    echo '<input class="form-control" id="bot-login" name="login" required autocomplete="username"></div>';
    echo '<div class="mb-3"><label class="form-label" for="bot-password">Senha</label>';
    echo '<input class="form-control" type="password" id="bot-password" name="password" required autocomplete="current-password"></div>';
    echo '<button class="btn btn-primary w-100">Entrar</button></form></div></div>';
    layout_end();
    exit;
}

if ($page === 'options' || $page === 'targets') {
    header('Location: ?page=menus');
    exit;
}

$listPage = list_page();
$filterReason = preg_replace('/[^a-z_]/', '', (string) ($_GET['reason'] ?? '')) ?: '';
$filterStatus = preg_replace('/[^a-z_]/', '', (string) ($_GET['status'] ?? '')) ?: '';
$autoRefresh = $page === 'activity' && $listPage === 1 && $filterReason === '';
layout_start('HELPTOA Bot', $nav, $page, $autoRefresh);

if ($page === 'queues') {
    echo '<div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-1">';
    echo '<h1 class="h3 mb-0">Filas</h1>';
    echo '<form method="post" class="d-inline"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="sync_catalog">';
    echo '<button class="btn btn-primary">Sincronizar (recoletar do TOA)</button></form></div>';
    if (($_GET['synced'] ?? '') === '1') {
        echo '<div class="alert alert-success py-2 mt-2 mb-2">Sincronização solicitada — o bot processa em até ~5 s (novas filas e operadores entram no catálogo).</div>';
    }
    echo '<p class="text-muted">Novas filas entram desabilitadas. O bot só atende as habilitadas.</p>';
    $rows = $pdo->query(
        'SELECT q.*, m.name AS menu_name, t.name AS team_name
         FROM queues q
         LEFT JOIN menu_queues mq ON mq.queue_address = q.address
         LEFT JOIN menus m ON m.id = mq.menu_id
         LEFT JOIN team_queues tq ON tq.queue_address = q.address
         LEFT JOIN teams t ON t.id = tq.team_id
         ORDER BY q.name, q.address'
    )->fetchAll();
    echo '<div class="table-responsive"><table class="table table-sm table-striped table-hover align-middle bg-white">';
    echo '<thead><tr><th>Nome</th><th>Address</th><th>Menu</th><th>Time</th><th>Habilitada</th><th></th></tr></thead><tbody>';
    foreach ($rows as $r) {
        $on = bool_on($r['enabled']);
        echo '<tr><td>' . h($r['name']) . '</td><td><code>' . h($r['address']) . '</code></td>';
        echo '<td>' . ($r['menu_name'] ? '<span class="badge text-bg-light">' . h($r['menu_name']) . '</span>' : '<span class="text-muted">—</span>') . '</td>';
        echo '<td>' . ($r['team_name'] ? '<span class="badge text-bg-light">' . h($r['team_name']) . '</span>' : '<span class="text-muted">—</span>') . '</td>';
        echo '<td>' . badge_state($on, 'sim', 'não') . '</td>';
        echo '<td><form class="d-inline" method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
        echo '<input type="hidden" name="action" value="toggle"><input type="hidden" name="address" value="' . h($r['address']) . '">';
        echo '<button class="btn btn-sm ' . ($on ? 'btn-outline-secondary' : 'btn-success') . '">' . ($on ? 'Desabilitar' : 'Habilitar') . '</button></form></td></tr>';
    }
    echo '</tbody></table></div>';
    if (!$rows) {
        echo '<p class="text-muted">Nenhuma fila ainda. O Python preenche na primeira conexão.</p>';
    }
}

if ($page === 'operators') {
    echo '<div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-1">';
    echo '<h1 class="h3 mb-0">Operadores</h1>';
    echo '<form method="post" class="d-inline"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="sync_catalog">';
    echo '<button class="btn btn-primary">Recoletar do TOA</button></form></div>';
    if (($_GET['synced'] ?? '') === '1') {
        echo '<div class="alert alert-success py-2 mt-2 mb-2">Sincronização solicitada — o bot processa em até ~5 s.</div>';
    }
    if (!empty($formError)) {
        echo '<div class="alert alert-danger mt-2">' . h($formError) . '</div>';
    }
    echo '<p class="text-muted">Catálogo usado ao adicionar membros em Times. Itens com origem <span class="badge text-bg-light">TOA</span> vêm do TOA; <span class="badge bg-warning text-dark">manual</span> foram cadastrados aqui.</p>';
    $queues = $pdo->query('SELECT address, name FROM queues ORDER BY name, address')->fetchAll();
    echo '<div class="card mb-3"><div class="card-body">';
    echo '<h2 class="h5">Cadastrar operador manual</h2><form method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="add_operator">';
    echo '<div class="row g-3"><div class="col-md-4"><label class="form-label">Nome</label><input class="form-control" name="operator_name" required placeholder="Nome exibido"></div>';
    echo '<div class="col-md-5"><label class="form-label">Address (u:login@dominio)</label><input class="form-control" name="operator_address" required placeholder="u:nome@claro.com"></div>';
    echo '<div class="col-md-3 align-self-end"><button class="btn btn-primary">Cadastrar</button></div></div>';
    if ($queues) {
        echo '<div class="mt-3"><label class="form-label">Vincular às filas (ao menos uma)</label><div class="queue-list border rounded p-2">';
        foreach ($queues as $q) {
            $qid = 'oq-' . preg_replace('/[^a-z0-9_:-]/i', '', $q['address']);
            echo '<div class="form-check"><input class="form-check-input" type="checkbox" name="queues[]" value="' . h($q['address']) . '" id="' . h($qid) . '">';
            echo '<label class="form-check-label" for="' . h($qid) . '">' . h($q['name'] ? $q['name'] . ' — ' : '') . '<code>' . h($q['address']) . '</code></label></div>';
        }
        echo '</div></div>';
    } else {
        echo '<div class="alert alert-info mt-3 mb-0">Nenhuma fila no catálogo ainda. Clique em “Recoletar do TOA” ou aguarde o primeiro chat para cadastrar.</div>';
    }
    echo '</form></div></div>';
    $opRows = $pdo->query(
        "SELECT qo.queue_address, qo.address, qo.name, qo.source, q.name AS queue_name
         FROM queue_operators qo
         LEFT JOIN queues q ON q.address = qo.queue_address
         ORDER BY qo.name, qo.address, qo.queue_address"
    )->fetchAll();
    echo '<div class="table-responsive"><table class="table table-sm table-striped table-hover align-middle bg-white">';
    echo '<thead><tr><th>Nome</th><th>Address</th><th>Fila</th><th>Origem</th><th></th></tr></thead><tbody>';
    foreach ($opRows as $op) {
        $manual = $op['source'] === 'manual';
        echo '<tr><td>' . h($op['name']) . '</td><td><code>' . h($op['address']) . '</code></td><td><code>' . h($op['queue_address']) . '</code>' . ($op['queue_name'] !== null && $op['queue_name'] !== '' ? ' (' . h($op['queue_name']) . ')' : '') . '</td>';
        echo '<td><span class="badge ' . ($manual ? 'bg-warning text-dark' : 'text-bg-light') . '">' . ($manual ? 'manual' : 'TOA') . '</span></td><td>';
        if ($manual) {
            echo '<form class="d-inline" method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
            echo '<input type="hidden" name="action" value="remove_operator"><input type="hidden" name="queue_address" value="' . h($op['queue_address']) . '"><input type="hidden" name="address" value="' . h($op['address']) . '">';
            echo '<button class="btn btn-sm btn-outline-danger">Remover</button></form>';
        } else {
            echo '<span class="text-muted small">—</span>';
        }
        echo '</td></tr>';
    }
    echo '</tbody></table></div>';
    if (!$opRows) {
        echo '<p class="text-muted">Nenhum operador no catálogo. Recolete do TOA ou cadastre manualmente.</p>';
    }
}

if ($page === 'templates') {
    echo '<h1 class="h3 mb-3">Templates</h1>';
    $rows = $pdo->query('SELECT * FROM templates ORDER BY slug')->fetchAll();
    foreach ($rows as $r) {
        echo '<div class="card mb-3"><div class="card-header"><code>' . h($r['slug']) . '</code></div><div class="card-body">';
        echo '<form method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
        echo '<input type="hidden" name="action" value="save"><input type="hidden" name="slug" value="' . h($r['slug']) . '">';
        echo '<textarea class="form-control font-monospace mb-3" name="body" rows="5">' . h($r['body']) . '</textarea>';
        echo '<button class="btn btn-primary">Salvar</button></form></div></div>';
    }
}

if ($page === 'menus') {
    echo '<h1 class="h3 mb-1">Menus</h1>';
    echo '<p class="text-muted">Cada menu tem as opções (1, 2, 3…) e as filas em que vale. Uma opção pode abrir um submenu (um nível). Número 0 é reservado para Voltar no chat. Uma fila só pode estar em um menu. Transferência usa o <strong>time</strong> da fila, não uma lista de pessoas na opção.</p>';
    if (!empty($formError)) {
        echo '<div class="alert alert-danger">' . h($formError) . '</div>';
    }
    $queues = $pdo->query('SELECT address, name FROM queues ORDER BY name, address')->fetchAll();
    echo '<div class="card mb-3"><div class="card-body">';
    echo '<form method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="create">';
    echo '<h2 class="h5">Novo menu</h2><div class="row g-2 align-items-end">';
    echo '<div class="col-md-6"><label class="form-label">Nome</label><input class="form-control" name="name" required></div>';
    echo '<div class="col-auto"><button class="btn btn-primary">Criar</button></div></div></form></div></div>';
    $menus = $pdo->query('SELECT * FROM menus ORDER BY name')->fetchAll();
    $mqRows = $pdo->query(
        'SELECT mq.menu_id, mq.queue_address, m.name AS menu_name FROM menu_queues mq JOIN menus m ON m.id = mq.menu_id'
    )->fetchAll();
    $byMenuQ = [];
    $queueMenu = [];
    foreach ($mqRows as $r) {
        $byMenuQ[(int) $r['menu_id']][] = $r['queue_address'];
        $queueMenu[$r['queue_address']] = ['id' => (int) $r['menu_id'], 'name' => $r['menu_name']];
    }
    $optRows = $pdo->query('SELECT * FROM menu_options WHERE menu_id IS NOT NULL ORDER BY menu_id, sort_order')->fetchAll();
    $byMenuO = [];
    foreach ($optRows as $r) {
        $byMenuO[(int) $r['menu_id']][] = $r;
    }
    $queuesWithTeam = [];
    foreach ($pdo->query('SELECT queue_address FROM team_queues') as $r) {
        $queuesWithTeam[$r['queue_address']] = true;
    }
    $openMenuId = 0;
    $openCollapse = '';
    if (!empty($formError) && ($_SERVER['REQUEST_METHOD'] ?? '') === 'POST') {
        $openMenuId = (int) ($_POST['menu_id'] ?? 0);
        $postAct = (string) ($_POST['action'] ?? '');
        if ($openMenuId > 0 && ($postAct === 'add_option' || $postAct === 'save_option')) {
            $oidPost = (int) ($_POST['id'] ?? 0);
            $pidPost = posted_parent_id();
            $openCollapse = option_form_collapse_id($openMenuId, $oidPost, $oidPost > 0 ? 0 : $pidPost);
        }
        if ($openMenuId > 0) {
            echo '<div id="ui-open" hidden data-hash="#menu-' . $openMenuId . '" data-collapse="' . ($openCollapse !== '' ? '#' . h($openCollapse) : '') . '"></div>';
        }
    }
    if ($menus) {
        echo '<div class="d-flex flex-wrap gap-2 mb-3">';
        foreach ($menus as $m) {
            $mid = (int) $m['id'];
            $nRoots = 0;
            $nKids = 0;
            foreach ($byMenuO[$mid] ?? [] as $o) {
                if (option_parent_id($o) === 0) {
                    $nRoots++;
                } else {
                    $nKids++;
                }
            }
            echo '<a class="btn btn-sm btn-outline-secondary" href="#menu-' . $mid . '">' . h($m['name']);
            echo ' <span class="badge text-bg-light">' . count($byMenuQ[$mid] ?? []) . ' filas</span>';
            echo ' <span class="badge text-bg-light">' . $nRoots . ' opções</span>';
            if ($nKids > 0) {
                echo ' <span class="badge text-bg-light">' . $nKids . ' no submenu</span>';
            }
            echo '</a>';
        }
        echo '</div>';
    }
    echo '<div class="accordion" id="menus-acc">';
    $firstMenu = true;
    foreach ($menus as $m) {
        $id = (int) $m['id'];
        $on = bool_on($m['active']);
        $sel = $byMenuQ[$id] ?? [];
        $opts = $byMenuO[$id] ?? [];
        $roots = [];
        $childrenByParent = [];
        foreach ($opts as $o) {
            $pid = option_parent_id($o);
            if ($pid === 0) {
                $roots[] = $o;
            } else {
                $childrenByParent[$pid][] = $o;
            }
        }
        usort($roots, 'option_sort_cmp');
        foreach ($childrenByParent as $pid => $kidsList) {
            usort($kidsList, 'option_sort_cmp');
            $childrenByParent[$pid] = $kidsList;
        }
        $hasTransfer = false;
        foreach ($opts as $o) {
            if (in_array($o['action'], ['ask_and_transfer', 'transfer_only'], true)) {
                $hasTransfer = true;
                break;
            }
        }
        $hasTeam = false;
        foreach ($sel as $qa) {
            if (!empty($queuesWithTeam[$qa])) {
                $hasTeam = true;
                break;
            }
        }
        if ($openMenuId > 0) {
            $showThis = $id === $openMenuId;
        } else {
            $showThis = $firstMenu;
            $firstMenu = false;
        }
        $show = $showThis ? ' show' : '';
        $collapsed = $showThis ? '' : ' collapsed';
        echo '<div class="accordion-item" id="menu-' . $id . '">';
        echo '<h2 class="accordion-header">';
        echo '<button class="accordion-button' . $collapsed . '" type="button" data-bs-toggle="collapse" data-bs-target="#menu-body-' . $id . '" aria-expanded="' . ($show ? 'true' : 'false') . '">';
        echo h($m['name']) . ' ' . badge_state($on) . '</button></h2>';
        echo '<div id="menu-body-' . $id . '" class="accordion-collapse collapse' . $show . '" data-bs-parent="#menus-acc">';
        echo '<div class="accordion-body">';
        echo '<form method="post" class="row g-2 align-items-end mb-3"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
        echo '<input type="hidden" name="action" value="rename"><input type="hidden" name="menu_id" value="' . $id . '">';
        echo '<div class="col-md-6"><label class="form-label">Nome</label><input class="form-control" name="name" value="' . h($m['name']) . '" required></div>';
        echo '<div class="col-auto"><button class="btn btn-primary">Salvar nome</button></div></form>';
        echo '<form class="d-inline" method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
        echo '<input type="hidden" name="menu_id" value="' . $id . '">';
        echo '<button class="btn btn-outline-secondary btn-sm" name="action" value="toggle">Ativar/desativar</button> ';
        echo '<button class="btn btn-outline-danger btn-sm" name="action" value="delete" onclick="return confirm(\'Excluir menu, opções e ligações com filas?\')">Excluir</button></form>';
        if ($hasTransfer && !$hasTeam) {
            echo '<div class="alert alert-warning mt-3">Há opção de transferência, mas nenhuma fila deste menu tem time. O bot vai gerar “sem analista”.</div>';
        }
        echo_queues_card($csrf, 'menu_id', $id, 'Filas deste menu', $queues, $queueMenu, 'menu');
        echo '<h3 class="h6 mt-3">Opções</h3>';
        echo '<div class="table-responsive"><table class="table table-sm table-hover align-middle bg-white mb-2">';
        echo '<thead><tr><th>#</th><th>Rótulo</th><th>Tipo</th><th>Ação</th><th>Classificação</th><th></th></tr></thead><tbody>';
        foreach ($roots as $o) {
            $oid = (int) $o['id'];
            $isSub = $o['action'] === 'submenu';
            $kids = $childrenByParent[$oid] ?? [];
            $cid = option_form_collapse_id($id, $oid, 0);
            echo_option_data_row($csrf, $id, $o, false, $cid, $isSub, count($kids));
            echo_option_editor_row($csrf, $id, $o, 0, true, $cid, $openCollapse === $cid);
            if ($isSub) {
                if (!$kids) {
                    echo '<tr class="opt-child"><td colspan="6"><div class="alert alert-warning py-2 mb-0">Submenu sem opções. No chat, escolher este número conta como resposta inválida.</div></td></tr>';
                }
                foreach ($kids as $child) {
                    $chid = (int) $child['id'];
                    $ccid = option_form_collapse_id($id, $chid, $oid);
                    echo_option_data_row($csrf, $id, $child, true, $ccid, false, 0);
                    echo_option_editor_row($csrf, $id, $child, $oid, false, $ccid, $openCollapse === $ccid);
                }
                $newChildId = option_form_collapse_id($id, 0, $oid);
                echo_option_editor_row($csrf, $id, [], $oid, false, $newChildId, $openCollapse === $newChildId);
            }
        }
        $newRootId = option_form_collapse_id($id, 0, 0);
        echo_option_editor_row($csrf, $id, [], 0, true, $newRootId, $openCollapse === $newRootId);
        echo '</tbody></table></div>';
        if (!$roots) {
            echo '<p class="text-muted">Nenhuma opção ainda.</p>';
        }
        echo '<button type="button" class="btn btn-primary btn-sm" data-bs-toggle="collapse" data-bs-target="#' . h($newRootId) . '">Adicionar opção</button>';
        echo '</div></div></div>';
    }
    echo '</div>';
    if (!$menus) {
        echo '<p class="text-muted">Nenhum menu. Crie um acima (os antigos são migrados na primeira carga).</p>';
    }
}

if ($page === 'teams') {
    echo '<h1 class="h3 mb-1">Times</h1>';
    echo '<p class="text-muted">Plantão: pessoas + filas que o time atende. Na transferência o bot convida o próximo da rodada entre quem está <strong>ativo</strong>, dentro da janela de horário (Brasília), online no TOA e abaixo do cap (Ajustes). Horários vazios = 24h. <strong>22:00 a 06:00 atravessa a meia-noite</strong> (plantão da madrugada). Desative o membro em ausência. Uma fila só pode estar em um time.</p>';
    if (!empty($formError)) {
        echo '<div class="alert alert-danger">' . h($formError) . '</div>';
    }
    $queues = $pdo->query('SELECT address, name FROM queues ORDER BY name, address')->fetchAll();
    $operators = [];
    try {
        $operators = $pdo->query(
            'SELECT DISTINCT address, MAX(name) AS name FROM queue_operators GROUP BY address ORDER BY MAX(name), address'
        )->fetchAll();
    } catch (Throwable $e) {
        $operators = [];
    }
    echo '<div class="card mb-3"><div class="card-body">';
    echo '<form method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="create">';
    echo '<h2 class="h5">Novo time</h2><div class="row g-2 align-items-end">';
    echo '<div class="col-md-6"><label class="form-label">Nome</label><input class="form-control" name="name" required placeholder="Plantão Ago/26"></div>';
    echo '<div class="col-auto"><button class="btn btn-primary">Criar</button></div></div></form></div></div>';
    $teams = $pdo->query('SELECT * FROM teams ORDER BY name')->fetchAll();
    $tqRows = $pdo->query(
        'SELECT tq.team_id, tq.queue_address, t.name AS team_name FROM team_queues tq JOIN teams t ON t.id = tq.team_id'
    )->fetchAll();
    $byTeamQ = [];
    $queueTeam = [];
    foreach ($tqRows as $r) {
        $byTeamQ[(int) $r['team_id']][] = $r['queue_address'];
        $queueTeam[$r['queue_address']] = ['id' => (int) $r['team_id'], 'name' => $r['team_name']];
    }
    $memRows = $pdo->query('SELECT * FROM team_members ORDER BY team_id, sort_order, name')->fetchAll();
    $byTeamM = [];
    foreach ($memRows as $r) {
        $byTeamM[(int) $r['team_id']][] = $r;
    }
    $openTeamId = 0;
    if (!empty($formError) && ($_SERVER['REQUEST_METHOD'] ?? '') === 'POST') {
        $openTeamId = (int) ($_POST['team_id'] ?? 0);
        if ($openTeamId > 0) {
            echo '<div id="ui-open" hidden data-hash="#team-' . $openTeamId . '" data-collapse=""></div>';
        }
    }
    if ($teams) {
        echo '<div class="d-flex flex-wrap gap-2 mb-3">';
        foreach ($teams as $t) {
            $tid = (int) $t['id'];
            echo '<a class="btn btn-sm btn-outline-secondary" href="#team-' . $tid . '">' . h($t['name']);
            echo ' <span class="badge text-bg-light">' . count($byTeamQ[$tid] ?? []) . ' filas</span>';
            echo ' <span class="badge text-bg-light">' . count($byTeamM[$tid] ?? []) . ' membros</span></a>';
        }
        echo '</div>';
    }
    echo '<div class="accordion" id="teams-acc">';
    $firstTeam = true;
    foreach ($teams as $t) {
        $id = (int) $t['id'];
        $on = bool_on($t['active']);
        $members = $byTeamM[$id] ?? [];
        $hasActive = false;
        foreach ($members as $mem) {
            if (bool_on($mem['active'])) {
                $hasActive = true;
                break;
            }
        }
        if ($openTeamId > 0) {
            $showThis = $id === $openTeamId;
        } else {
            $showThis = $firstTeam;
            $firstTeam = false;
        }
        $show = $showThis ? ' show' : '';
        $collapsed = $showThis ? '' : ' collapsed';
        echo '<div class="accordion-item" id="team-' . $id . '">';
        echo '<h2 class="accordion-header">';
        echo '<button class="accordion-button' . $collapsed . '" type="button" data-bs-toggle="collapse" data-bs-target="#team-body-' . $id . '" aria-expanded="' . ($show ? 'true' : 'false') . '">';
        echo h($t['name']) . ' ' . badge_state($on) . '</button></h2>';
        echo '<div id="team-body-' . $id . '" class="accordion-collapse collapse' . $show . '" data-bs-parent="#teams-acc">';
        echo '<div class="accordion-body">';
        echo '<form method="post" class="row g-2 align-items-end mb-3"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
        echo '<input type="hidden" name="action" value="rename"><input type="hidden" name="team_id" value="' . $id . '">';
        echo '<div class="col-md-6"><label class="form-label">Nome</label><input class="form-control" name="name" value="' . h($t['name']) . '" required></div>';
        echo '<div class="col-auto"><button class="btn btn-primary">Salvar nome</button></div></form>';
        echo '<form class="d-inline" method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
        echo '<input type="hidden" name="team_id" value="' . $id . '">';
        echo '<button class="btn btn-outline-secondary btn-sm" name="action" value="toggle">Ativar/desativar</button> ';
        echo '<button class="btn btn-outline-danger btn-sm" name="action" value="delete" onclick="return confirm(\'Excluir time, membros e ligações com filas? As filas ficam sem plantão.\')">Excluir</button></form>';
        if (!$hasActive) {
            echo '<div class="alert alert-warning mt-3">Nenhum membro ativo. Filas deste time não terão quem receber na transferência.</div>';
        }
        echo_queues_card($csrf, 'team_id', $id, 'Filas deste time', $queues, $queueTeam, 'time');
        echo '<form method="post" class="row g-2 align-items-end mb-3 p-2 border rounded bg-white">';
        echo '<input type="hidden" name="csrf" value="' . h($csrf) . '">';
        echo '<input type="hidden" name="action" value="add_member"><input type="hidden" name="team_id" value="' . $id . '">';
        echo '<div class="col-12"><h3 class="h6 mb-0">Adicionar membro</h3></div>';
        echo '<div class="col-md-3"><label class="form-label">Operador (catálogo TOA)</label><select class="form-select form-select-sm" name="catalog_address">';
        echo '<option value="">—</option>';
        foreach ($operators as $op) {
            echo '<option value="' . h($op['address']) . '">' . h(($op['name'] ?: $op['address']) . ' — ' . $op['address']) . '</option>';
        }
        echo '</select></div>';
        echo '<div class="col-md-3"><label class="form-label">Ou address manual</label><input class="form-control form-control-sm" name="address" placeholder="u:F123@clarosa"></div>';
        echo '<div class="col-md-2"><label class="form-label">Nome</label><input class="form-control form-control-sm" name="member_name" placeholder="Opcional"></div>';
        echo '<div class="col-md-1"><label class="form-label">Ordem</label><input class="form-control form-control-sm" type="number" name="sort_order" value="0"></div>';
        echo '<div class="col-md-1"><label class="form-label">Início</label><input class="form-control form-control-sm" type="time" name="route_start"></div>';
        echo '<div class="col-md-1"><label class="form-label">Fim</label><input class="form-control form-control-sm" type="time" name="route_end"></div>';
        echo '<div class="col-auto"><button class="btn btn-primary btn-sm">Adicionar</button></div>';
        echo '<div class="col-12"><p class="text-muted small mb-0">Vazio nos dois horários = 24h. Preencha os dois para limitar o plantão. 22:00 a 06:00 atravessa a meia-noite (plantão da madrugada).</p></div>';
        echo '</form>';
        echo '<h3 class="h6">Membros</h3>';
        echo '<div class="table-responsive"><table class="table table-sm table-hover align-middle bg-white">';
        echo '<thead><tr><th>Ordem</th><th>Nome</th><th>Address</th><th>Janela</th><th>Status</th><th>Agora</th><th></th></tr></thead><tbody>';
        foreach ($members as $mem) {
            $memOn = bool_on($mem['active']);
            $inWin = member_in_route_window($mem['route_start'] ?? null, $mem['route_end'] ?? null);
            $startVal = time_input_value($mem['route_start'] ?? null);
            $endVal = time_input_value($mem['route_end'] ?? null);
            $is24h = $startVal === '' && $endVal === '';
            echo '<form method="post" class="d-contents">';
            echo '<tr>';
            echo '<td style="width:5rem"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
            echo '<input type="hidden" name="team_id" value="' . $id . '">';
            echo '<input type="hidden" name="address" value="' . h($mem['address']) . '">';
            echo '<input class="form-control form-control-sm" type="number" name="sort_order" value="' . (int) $mem['sort_order'] . '"></td>';
            echo '<td><input class="form-control form-control-sm" name="member_name" value="' . h($mem['name']) . '"></td>';
            echo '<td><code>' . h($mem['address']) . '</code></td>';
            echo '<td style="min-width:11rem"><div class="d-flex gap-1">';
            echo '<input class="form-control form-control-sm" type="time" name="route_start" value="' . h($startVal) . '">';
            echo '<input class="form-control form-control-sm" type="time" name="route_end" value="' . h($endVal) . '">';
            echo '</div>';
            if ($is24h) {
                echo '<div class="form-text mt-0">24h</div>';
            } elseif (route_crosses_midnight($mem['route_start'] ?? null, $mem['route_end'] ?? null)) {
                echo '<div class="form-text mt-0">atravessa meia-noite</div>';
            }
            echo '</td>';
            echo '<td>' . badge_state($memOn) . '</td>';
            echo '<td>';
            if ($memOn) {
                echo badge_state($inWin, 'na janela', 'fora do horário');
            } else {
                echo '—';
            }
            echo '</td>';
            echo '<td class="text-nowrap">';
            echo '<button class="btn btn-primary btn-sm me-1" name="action" value="save_member">Salvar</button>';
            echo '<button class="btn btn-outline-secondary btn-sm me-1" name="action" value="toggle_member">' . ($memOn ? 'Desativar' : 'Ativar') . '</button>';
            echo '<button class="btn btn-outline-danger btn-sm" name="action" value="delete_member" onclick="return confirm(\'Remover este membro?\')">Remover</button>';
            echo '</td></tr></form>';
        }
        echo '</tbody></table></div>';
        if (!$members) {
            echo '<p class="text-muted">Nenhum membro ainda.</p>';
        }
        echo '</div></div></div>';
    }
    echo '</div>';
    if (!$teams) {
        echo '<p class="text-muted">Nenhum time. Crie o plantão do mês e marque as filas.</p>';
    }
}

if ($page === 'settings') {
    echo '<h1 class="h3 mb-3">Ajustes</h1>';
    $map = [];
    foreach ($pdo->query('SELECT key, value FROM bot_settings') as $r) {
        $map[$r['key']] = $r['value'];
    }
    echo '<div class="card"><div class="card-body"><form method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="save">';
    echo '<div class="row g-3">';
    echo '<div class="col-md-6"><label class="form-label">Timeout de resposta (s)</label><input class="form-control" name="reply_timeout_sec" value="' . h($map['reply_timeout_sec'] ?? '300') . '"></div>';
    echo '<div class="col-md-6"><label class="form-label">Retries inválidos</label><input class="form-control" name="max_invalid_retries" value="' . h($map['max_invalid_retries'] ?? '3') . '"></div>';
    echo '<div class="col-md-6"><label class="form-label">Poll de config (s)</label><input class="form-control" name="config_poll_sec" value="' . h($map['config_poll_sec'] ?? '15') . '"></div>';
    echo '<div class="col-md-6"><label class="form-label">Máximo de chats por analista (roteamento)</label><input class="form-control" name="max_open_chats_per_agent" value="' . h($map['max_open_chats_per_agent'] ?? '8') . '"></div>';
    echo '<div class="col-md-6"><label class="form-label">WebSocket mudo para reciclar Chromium (s)</label><input class="form-control" name="ws_stale_sec" value="' . h($map['ws_stale_sec'] ?? '180') . '"></div>';
    echo '<div class="col-md-6"><label class="form-label">Tentativas de transferência (busca de online)</label><input class="form-control" name="transfer_attempts" value="' . h($map['transfer_attempts'] ?? '5') . '"></div>';
    echo '<div class="col-md-6"><label class="form-label">Intervalo entre tentativas de transferência (s)</label><input class="form-control" name="transfer_retry_interval_sec" value="' . h($map['transfer_retry_interval_sec'] ?? '60') . '"></div>';
    echo '<div class="col-md-6"><label class="form-label">Bot ligado</label><select class="form-select" name="bot_enabled">';
    echo '<option value="true"' . (($map['bot_enabled'] ?? '') === 'true' ? ' selected' : '') . '>Ligado</option>';
    echo '<option value="false"' . (($map['bot_enabled'] ?? '') === 'false' ? ' selected' : '') . '>Desligado</option></select></div>';
    echo '</div><button class="btn btn-primary mt-3">Salvar</button></form></div></div>';
}

if ($page === 'activity') {
    $labels = [
        'bot_off' => 'bot desligado',
        'queue_off' => 'fila desabilitada',
        'queue_unknown' => 'sem fila',
        'not_new' => 'já atendido',
        'closure' => 'encerramento (ignorado)',
        'assumed' => 'assumiu',
        'menu_sent' => 'menu enviado',
        'classified' => 'classificou',
        'script_ok' => 'script ok',
        'script_error' => 'script erro',
        'script_denied' => 'script recusado',
        'catalog_synced' => 'catálogo sincronizado',
        'sync_error' => 'erro na sincronização',
        'ignored_reply' => 'resposta inválida',
        'closed_invalid' => 'encerrou (inválido)',
        'closed_timeout' => 'encerrou (timeout)',
        'transferred' => 'transferiu',
        'no_agent' => 'sem analista',
        'left' => 'saiu',
        'resume_stale' => 'chat sumiu no resume',
    ];
    echo '<h1 class="h3 mb-1">Atividade</h1>';
    echo '<p class="text-muted">Log do bot' . ($autoRefresh ? ' (atualiza a cada 5s)' : ' (atualização automática pausada nesta página ou filtro)') . '. Sem texto de conversa.</p>';
    echo '<form method="get" class="row g-2 align-items-end mb-3">';
    echo '<input type="hidden" name="page" value="activity">';
    echo '<div class="col-md-4"><label class="form-label">Evento</label><select class="form-select" name="reason">';
    echo '<option value="">Todos</option>';
    foreach ($labels as $rk => $lab) {
        echo '<option value="' . h($rk) . '"' . ($filterReason === $rk ? ' selected' : '') . '>' . h($lab) . '</option>';
    }
    echo '</select></div><div class="col-auto"><button class="btn btn-outline-secondary">Filtrar</button></div></form>';
    $per = 50;
    $total = 0;
    $rows = [];
    try {
        $where = '';
        $params = [];
        if ($filterReason !== '') {
            $where = 'WHERE e.reason = ?';
            $params[] = $filterReason;
        }
        $st = $pdo->prepare("SELECT COUNT(*) FROM bot_events e $where");
        $st->execute($params);
        $total = (int) $st->fetchColumn();
        $pages = max(1, (int) ceil($total / $per));
        if ($listPage > $pages) {
            $listPage = $pages;
        }
        $offset = ($listPage - 1) * $per;
        $st = $pdo->prepare(
            "SELECT e.at, e.chat_id, e.queue_address, e.queue_enabled, e.reason, e.detail, q.name AS queue_name
             FROM bot_events e
             LEFT JOIN queues q ON q.address = e.queue_address
             $where
             ORDER BY e.at DESC, e.id DESC
             LIMIT $per OFFSET $offset"
        );
        $st->execute($params);
        $rows = $st->fetchAll();
    } catch (Throwable $e) {
        echo '<div class="alert alert-danger">Tabela bot_events ausente. Rode o CREATE TABLE no Postgres.</div>';
        $rows = [];
    }
    echo '<div class="table-responsive"><table class="table table-sm table-striped table-hover align-middle bg-white">';
    echo '<thead><tr><th>Hora</th><th>chat_id</th><th>Fila</th><th>Habilitada</th><th>Evento</th><th>Detalhe</th></tr></thead><tbody>';
    foreach ($rows as $r) {
        $on = $r['queue_enabled'];
        $onKnown = $on === true || $on === 't' || $on === 1 || $on === false || $on === 'f' || $on === 0;
        $fila = $r['queue_name'] ? $r['queue_name'] . ' — ' . $r['queue_address'] : ($r['queue_address'] ?: '—');
        echo '<tr><td>' . h(fmt_dt($r['at'])) . '</td><td>' . h((string) ($r['chat_id'] ?? '')) . '</td>';
        echo '<td><code>' . h($fila) . '</code></td>';
        echo '<td>' . ($onKnown ? badge_state(bool_on($on), 'sim', 'não') : '<span class="text-muted">—</span>') . '</td>';
        echo '<td>' . event_badge((string) $r['reason'], $labels[$r['reason']] ?? (string) $r['reason']) . '</td>';
        echo '<td>' . h($r['detail']) . '</td></tr>';
    }
    echo '</tbody></table></div>';
    echo_pagination('activity', $listPage, $total, ['reason' => $filterReason], $per);
    if (!$rows) {
        echo '<p class="text-muted">Nenhum evento ainda. Deixe o run.py ligado e abra um chat numa fila.</p>';
    }
}

if ($page === 'chats') {
    echo '<h1 class="h3 mb-1">Chats</h1>';
    echo '<p class="text-muted">Só id e estado — sem texto de conversa.</p>';
    $statuses = [];
    try {
        $statuses = $pdo->query('SELECT DISTINCT status FROM chats ORDER BY status')->fetchAll(PDO::FETCH_COLUMN);
    } catch (Throwable $e) {
        $statuses = [];
    }
    echo '<form method="get" class="row g-2 align-items-end mb-3">';
    echo '<input type="hidden" name="page" value="chats">';
    echo '<div class="col-md-4"><label class="form-label">Status</label><select class="form-select" name="status">';
    echo '<option value="">Todos</option>';
    foreach ($statuses as $st) {
        echo '<option value="' . h((string) $st) . '"' . ($filterStatus === (string) $st ? ' selected' : '') . '>' . h((string) $st) . '</option>';
    }
    echo '</select></div><div class="col-auto"><button class="btn btn-outline-secondary">Filtrar</button></div></form>';
    $per = 50;
    $total = 0;
    $rows = [];
    $where = '';
    $params = [];
    if ($filterStatus !== '') {
        $where = 'WHERE status = ?';
        $params[] = $filterStatus;
    }
    $st = $pdo->prepare("SELECT COUNT(*) FROM chats $where");
    $st->execute($params);
    $total = (int) $st->fetchColumn();
    $pages = max(1, (int) ceil($total / $per));
    if ($listPage > $pages) {
        $listPage = $pages;
    }
    $offset = ($listPage - 1) * $per;
    $st = $pdo->prepare(
        "SELECT chat_id, queue_address, status, classification, retries, auto_close, updated_at
         FROM chats $where
         ORDER BY updated_at DESC
         LIMIT $per OFFSET $offset"
    );
    $st->execute($params);
    $rows = $st->fetchAll();
    echo '<div class="table-responsive"><table class="table table-sm table-striped table-hover align-middle bg-white">';
    echo '<thead><tr><th>chat_id</th><th>Fila</th><th>Status</th><th>Classificação</th><th>Retries</th><th>Auto close</th><th>Atualizado</th></tr></thead><tbody>';
    foreach ($rows as $r) {
        echo '<tr><td>' . h((string) $r['chat_id']) . '</td><td>' . h($r['queue_address']) . '</td><td>' . status_badge((string) $r['status']) . '</td>';
        echo '<td>' . h($r['classification']) . '</td><td>' . (int) $r['retries'] . '</td><td>' . badge_state(bool_on($r['auto_close']), 'sim', 'não') . '</td>';
        echo '<td>' . h(fmt_dt($r['updated_at'])) . '</td></tr>';
    }
    echo '</tbody></table></div>';
    echo_pagination('chats', $listPage, $total, ['status' => $filterStatus], $per);
}

if ($page === 'dashboard') {
    $tzDash = new DateTimeZone('America/Sao_Paulo');
    $hojeDash = (new DateTimeImmutable('now', $tzDash))->format('Y-m-d');
    $deDefault = (new DateTimeImmutable('-29 days', $tzDash))->format('Y-m-d');
    $deRaw = trim((string) ($_GET['de'] ?? $deDefault));
    $ateRaw = trim((string) ($_GET['ate'] ?? $hojeDash));
    $selDash = [];
    foreach ((array) ($_GET['filas'] ?? []) as $qa) {
        $qa = trim((string) $qa);
        if (substr($qa, 0, 2) === 'h:') {
            $selDash[$qa] = true;
        }
    }
    $selList = array_keys($selDash);

    $dashErr = '';
    $parseDay = static function (string $raw): ?DateTimeImmutable {
        if ($raw === '') {
            return null;
        }
        $d = DateTimeImmutable::createFromFormat('!Y-m-d', $raw);
        return $d === false ? null : $d;
    };
    $de = $parseDay($deRaw);
    $ate = $parseDay($ateRaw);
    if ($de === null || $ate === null) {
        $dashErr = 'Datas inválidas. Use o formato AAAA-MM-DD (de/até).';
    } elseif ($de > $ate) {
        $dashErr = 'A data inicial deve ser menor ou igual à final.';
    } elseif ($de->diff($ate)->days > 730) {
        $dashErr = 'Período máximo de 2 anos.';
    }

    $candQueues = [];
    try {
        $candQueues = $pdo->query(
            "SELECT DISTINCT e.queue_address, q.name AS queue_name
             FROM bot_events e
             LEFT JOIN queues q ON q.address = e.queue_address
             WHERE e.reason = 'classified' AND e.queue_address IS NOT NULL AND e.queue_address <> ''
             ORDER BY COALESCE(q.name, e.queue_address), e.queue_address"
        )->fetchAll();
    } catch (Throwable $e) {
        $candQueues = [];
    }

    echo '<h1 class="h3 mb-1">Dashboard</h1>';
    echo '<p class="text-muted">Volume de classificações (evento “classificou”) por categoria no período e filas selecionadas. Gráfico gerado pelo próprio painel, sem bibliotecas externas.</p>';

    echo '<div class="card mb-3"><div class="card-body">';
    echo '<form method="get" class="row g-3 align-items-end">';
    echo '<input type="hidden" name="page" value="dashboard">';
    echo '<div class="col-md-7"><label class="form-label">Filas — nenhuma marcada = todas</label><div class="queue-list border rounded p-2">';
    if ($candQueues) {
        foreach ($candQueues as $cq) {
            $qaDash = (string) $cq['queue_address'];
            $nomeDash = (string) ($cq['queue_name'] ?? '');
            $qidDash = 'dq-' . preg_replace('/[^a-z0-9_:-]/i', '', $qaDash);
            $checkedDash = isset($selDash[$qaDash]) ? ' checked' : '';
            echo '<div class="form-check form-check-inline"><input class="form-check-input" type="checkbox" name="filas[]" value="' . h($qaDash) . '" id="' . h($qidDash) . '"' . $checkedDash . '>';
            echo '<label class="form-check-label" for="' . h($qidDash) . '">' . h($nomeDash !== '' ? $nomeDash . ' — ' : '') . '<code>' . h($qaDash) . '</code></label></div>';
        }
    } else {
        echo '<span class="text-muted">Nenhuma fila com eventos de classificação ainda.</span>';
    }
    echo '</div></div>';
    echo '<div class="col-md-2"><label class="form-label">De</label><input type="date" class="form-control" name="de" value="' . h($deRaw) . '"></div>';
    echo '<div class="col-md-2"><label class="form-label">Até</label><input type="date" class="form-control" name="ate" value="' . h($ateRaw) . '"></div>';
    echo '<div class="col-auto"><button class="btn btn-primary">Filtrar</button></div>';
    echo '</form>';
    if ($dashErr !== '') {
        echo '<div class="alert alert-danger mt-3 mb-0">' . h($dashErr) . '</div>';
    }
    echo '</div></div>';

    $grand = 0;
    $maxV = 0;
    $norm = [];
    if ($dashErr === '' && $de !== null && $ate !== null) {
        $whereDash = "e.reason = 'classified'";
        $paramsDash = [];
        if ($selList) {
            $whereDash .= ' AND e.queue_address IN (' . implode(',', array_fill(0, count($selList), '?')) . ')';
            foreach ($selList as $qaDash2) {
                $paramsDash[] = $qaDash2;
            }
        }
        $whereDash .= ' AND date(datetime(e.at, ?)) BETWEEN date(?) AND date(?)';
        $paramsDash[] = '-3 hours';
        $paramsDash[] = $de->format('Y-m-d');
        $paramsDash[] = $ate->format('Y-m-d');
        try {
            $cats = $pdo->prepare(
                "SELECT CASE WHEN instr(e.detail, ';') > 0
                        THEN trim(substr(e.detail, 1, instr(e.detail, ';') - 1))
                        ELSE trim(e.detail) END AS categoria,
                        COUNT(*) AS total
                 FROM bot_events e
                 WHERE $whereDash
                 GROUP BY categoria
                 ORDER BY total DESC, categoria"
            );
            $cats->execute($paramsDash);
            $cats = $cats->fetchAll();
        } catch (Throwable $e) {
            $cats = [];
        }
        foreach ($cats as $row) {
            $cat = trim((string) $row['categoria']);
            if ($cat === '') {
                $cat = '(sem categoria)';
            }
            $v = (int) $row['total'];
            $norm[] = [$cat, $v];
            $grand += $v;
            if ($v > $maxV) {
                $maxV = $v;
            }
        }
    }

    if ($dashErr === '' && $de !== null && $ate !== null) {
        $legFila = $selList ? count($selList) . ' fila(s)' : 'todas as filas';
        echo '<div class="card"><div class="card-body">';
        echo '<h2 class="h6 mb-0">Classificações por categoria — ' . h($de->format('d/m/Y')) . ' a ' . h($ate->format('d/m/Y')) . ' (' . h($legFila) . ')</h2>';
        echo '<p class="text-muted mb-2 small">Total no período: ' . $grand . '</p>';
        if (!$norm) {
            echo '<p class="text-muted mb-0">Nenhuma classificação encontrada para esses filtros.</p>';
        } else {
            echo '<style>
.dash-scroll{overflow-x:auto}
.dash-chart{display:flex;align-items:flex-end;gap:6px;height:260px;min-width:max-content;padding:0 4px 0 0;
  background-image:repeating-linear-gradient(to top, rgba(0,0,0,.07) 0 1px, transparent 1px 50px);}
.dash-col{position:relative;flex:0 0 72px;height:100%}
.dash-bar{position:absolute;left:50%;bottom:0;transform:translateX(-50%);width:56%;max-width:46px;min-width:6px;border-radius:4px 4px 0 0}
.dash-val{position:absolute;left:0;right:0;text-align:center;font-size:.72rem;font-weight:600;line-height:1}
.dash-labels{display:flex;gap:6px;padding:0 4px 0 0;min-width:max-content}
.dash-cat{flex:0 0 72px;font-size:.68rem;color:#6c757d;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:2px 2px 0;text-align:center}
</style>';
            echo '<div class="dash-scroll"><div class="dash-chart">';
            $palDash = ['#0d6efd', '#198754', '#dc3545', '#fd7e14', '#6f42c1', '#20c997', '#d63384', '#ffc107', '#0dcaf0', '#6610f2', '#e83e8c', '#adb5bd'];
            foreach ($norm as $i => $nv) {
                [$cat, $v] = $nv;
                $px = $maxV > 0 ? max(3, (int) round($v / $maxV * 238)) : 0;
                $corDash = $palDash[$i % count($palDash)];
                $ttDash = $cat . ': ' . $v;
                echo '<div class="dash-col">';
                echo '<div class="dash-bar" style="height:' . $px . 'px;background:' . $corDash . '" title="' . h($ttDash) . '"></div>';
                echo '<div class="dash-val" style="bottom:' . ($px + 3) . 'px;color:' . $corDash . '">' . $v . '</div>';
                echo '</div>';
            }
            echo '</div><div class="dash-labels">';
            foreach ($norm as $nv) {
                [$cat] = $nv;
                echo '<div class="dash-cat" title="' . h($cat) . '">' . h($cat) . '</div>';
            }
            echo '</div></div>';
        }
        echo '</div></div>';
    }
}

layout_end();
