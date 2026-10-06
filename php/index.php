<?php
declare(strict_types=1);

require __DIR__ . '/bootstrap.php';
require __DIR__ . '/auth.php';
require __DIR__ . '/ui.php';
require __DIR__ . '/pages/users.php';
require __DIR__ . '/pages/groups.php';
session_start();

$pdo = bot_pdo();
// Pagina inicial do painel. login/logout ficam fora do RBAC.
$page = preg_replace('/[^a-z_]/', '', (string) ($_GET['page'] ?? 'dashboard')) ?: 'dashboard';
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

/**
 * Perguntas da opcao "Pedir info e transferir", validadas a partir do POST do editor.
 * @return array{0:?string,1:array}
 */
function posted_option_questions(): array
{
    $prompts = (array) ($_POST['q_prompt'] ?? []);
    $kinds = (array) ($_POST['q_kind'] ?? []);
    $choices = (array) ($_POST['q_choices'] ?? []);
    $out = [];
    foreach ($prompts as $i => $rawPrompt) {
        $prompt = trim((string) $rawPrompt);
        if ($prompt === '') {
            continue;
        }
        $kind = (string) ($kinds[$i] ?? 'text');
        if (!in_array($kind, ['text', 'int', 'choice'], true)) {
            $kind = 'text';
        }
        $lista = '';
        if ($kind === 'choice') {
            $parts = [];
            foreach (explode('|', (string) ($choices[$i] ?? '')) as $c) {
                $c = trim($c);
                if ($c !== '') {
                    $parts[] = $c;
                }
            }
            if (count($parts) < 2) {
                return ['Pergunta "' . $prompt . '": informe ao menos 2 alternativas separadas por |.', []];
            }
            $lista = implode('|', $parts);
        }
        $out[] = ['prompt' => $prompt, 'kind' => $kind, 'choices' => $lista];
        if (count($out) > 50) {
            return ['Maximo de 50 perguntas por opcao.', []];
        }
    }
    return [null, $out];
}

/** Substitui as perguntas da opcao (salvar idempotente). */
function save_option_questions(PDO $pdo, int $optionId, array $perguntas): void
{
    $pdo->prepare('DELETE FROM menu_option_questions WHERE option_id = ?')->execute([$optionId]);
    if (!$perguntas) {
        return;
    }
    $ins = $pdo->prepare(
        'INSERT INTO menu_option_questions (option_id, sort_order, prompt, kind, choices) VALUES (?, ?, ?, ?, ?)'
    );
    foreach ($perguntas as $i => $q) {
        $ins->execute([$optionId, $i + 1, $q['prompt'], $q['kind'], $q['choices']]);
    }
}

/** Perguntas ja gravadas (cache por request). */
function option_questions_list(PDO $pdo, int $optionId): array
{
    static $cache = [];
    if (array_key_exists($optionId, $cache)) {
        return $cache[$optionId];
    }
    $st = $pdo->prepare(
        'SELECT prompt, kind, choices FROM menu_option_questions WHERE option_id = ? ORDER BY sort_order, id'
    );
    $st->execute([$optionId]);
    $cache[$optionId] = $st->fetchAll();
    return $cache[$optionId];
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


function pdo_friendly_error(PDOException $e): string
{
    $code = (string) $e->getCode();
    $msg = strtoupper($e->getMessage());
    // Codigo do DRIVER (`errorInfo[1]`): no MySQL o duplicado e 1062 e a FK e
    // 1451/1452 — os dois chegam com SQLSTATE 23000, entao o codigo do driver e
    // o unico jeito de nao chamar violacao de FK de "registro duplicado".
    $driver = 0;
    if (isset($e->errorInfo[1])) {
        $driver = (int) $e->errorInfo[1];
    }
    $fk = in_array($driver, [1451, 1452], true)
        || $code === '23503'
        || strpos($msg, 'FOREIGN KEY') !== false
        || strpos($msg, '23503') !== false;
    $duplicado = $driver === 1062
        || $code === '23000' || $code === '23505'
        || strpos($msg, 'DUPLICATE ENTRY') !== false
        || strpos($msg, 'UNIQUE') !== false
        || strpos($msg, '23505') !== false;
    // A FK vem primeiro: e o caso em que o 23000 dos dois motores engana.
    if ($fk) {
        return 'Não foi possível excluir: ainda há registros ligados a este item.';
    }
    if ($duplicado) {
        return 'Já existe um registro com esses dados (fila em outro menu/time ou número de opção repetido).';
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
    echo '</div><button class="btn btn-primary mt-3">' . ($isNew ? 'Adicionar opção' : 'Salvar opção') . '</button></form>';
    if ($act === 'ask_and_transfer') {
        if ($isNew) {
            echo '<div class="alert alert-info py-2 small mt-3 mb-0">Salve a opção para cadastrar as perguntas.</div>';
        } else {
            echo_option_questions_form($csrf, $menuId, (int) $o['id']);
        }
    }
}

/** Editor das perguntas da opcao "Pedir info e transferir" (form proprio, sem aninhar). */
function echo_option_questions_form(string $csrf, int $menuId, int $optionId): void
{
    $linhas = option_questions_list(bot_pdo(), $optionId);
    $extras = $linhas ? 1 : 3;
    for ($extra = 0; $extra < $extras; $extra++) {
        $linhas[] = ['prompt' => '', 'kind' => 'text', 'choices' => ''];
    }
    $rowsId = 'ask-rows-' . $optionId;
    echo '<div class="border rounded p-3 mt-3 bg-white">';
    echo '<h4 class="h6 mb-1">Perguntas antes de transferir</h4>';
    echo '<p class="text-muted small mb-2">O bot pergunta uma a uma, o tecnico confirma e so entao transfere. Use <strong>+ Adicionar pergunta</strong> para incluir mais (ate 50). Linha sem texto e ignorada; alternativas separadas por | (ex.: HFC|GPON).</p>';
    echo '<form method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="save_questions">';
    echo '<input type="hidden" name="menu_id" value="' . (int) $menuId . '"><input type="hidden" name="id" value="' . (int) $optionId . '">';
    echo '<div class="table-responsive"><table class="table table-sm align-middle mb-2">';
    echo '<thead><tr><th style="width:2.5rem">#</th><th>Pergunta</th><th style="width:8rem">Tipo</th><th style="width:14rem">Alternativas (|)</th><th style="width:3rem"></th></tr></thead>';
    echo '<tbody id="' . h($rowsId) . '">';
    foreach ($linhas as $i => $q) {
        $kind = (string) ($q['kind'] ?? 'text');
        echo '<tr data-ask-row><td class="text-muted" data-ask-num>' . ($i + 1) . '</td>';
        echo '<td><input class="form-control form-control-sm" name="q_prompt[]" value="' . h((string) ($q['prompt'] ?? '')) . '" placeholder="Texto da pergunta"></td>';
        echo '<td><select class="form-select form-select-sm" name="q_kind[]">';
        foreach (['text' => 'Texto', 'int' => 'Numero', 'choice' => 'Escolha'] as $k => $lab) {
            echo '<option value="' . h($k) . '"' . ($kind === $k ? ' selected' : '') . '>' . h($lab) . '</option>';
        }
        echo '</select></td>';
        echo '<td><input class="form-control form-control-sm" name="q_choices[]" value="' . h((string) ($q['choices'] ?? '')) . '" placeholder="HFC|GPON"></td>';
        echo '<td><button type="button" class="btn btn-outline-danger btn-sm" data-ask-del title="Remover pergunta">&times;</button></td></tr>';
    }
    echo '</tbody></table></div>';
    echo '<div class="d-flex flex-wrap gap-2 align-items-center">';
    echo '<button class="btn btn-outline-primary btn-sm">Salvar perguntas</button>';
    echo '<button type="button" class="btn btn-outline-secondary btn-sm" data-ask-add="' . h($rowsId) . '">+ Adicionar pergunta</button>';
    echo '</div>';
    echo '</form></div>';
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
        'transferred_partial' => 'bg-warning text-dark',
        'queued' => 'bg-info text-dark',
        'queue_position' => 'bg-info text-dark',
        'queued_assigned' => 'bg-success',
        'queue_expired' => 'bg-danger',
        'form_aware_greeting' => 'bg-secondary',
        'form_aware_choice' => 'bg-secondary',
        'form_aware_wait' => 'bg-secondary',
        'form_aware_skip' => 'bg-secondary',
        'case_linked' => 'bg-secondary',
        'case_sibling_delivered' => 'bg-success',
        'form_indexed' => 'bg-info text-dark',
        'form_not_open' => 'bg-warning text-dark',
        'form_dropped' => 'bg-secondary',
        'form_ignored_payload' => 'bg-secondary',
        'form_queue_left' => 'bg-secondary',
        'form_forget' => 'bg-secondary',
        'case_key_divergente' => 'bg-warning text-dark',
        'reply_replayed' => 'bg-info text-dark',
        'send_failed' => 'bg-danger',
        'ignored_reply' => 'bg-warning text-dark',
        'closed_invalid' => 'bg-danger',
        'closed_timeout' => 'bg-danger',
        'bot_off' => 'bg-danger',
        'queue_off' => 'bg-secondary',
        'queue_no_menu' => 'bg-danger',
        'queue_no_team' => 'bg-danger',
        'form_queue_off' => 'bg-secondary',
        'self_target_blocked' => 'bg-danger',
        'case_reclaimed' => 'bg-warning text-dark',
        'chat_adopted' => 'bg-success',
        'reclaim_invite_only' => 'bg-warning text-dark',
        'reclaim_no_queue' => 'bg-danger',
        'reclaim_empty' => 'bg-secondary',
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
        'awaiting_form_choice' => 'bg-warning text-dark',
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

/**
 * Donut em SVG puro (sem CDN): uma fatia por item de $slices.
 *
 * @param array<int,array{0:string,1:int,2:string}> $slices [rótulo, valor, cor]
 * @param string $centro  Número grande no meio do donut
 * @param string $legenda Rótulo pequeno abaixo do número
 */
function dash_donut(array $slices, string $centro = '', string $legenda = ''): void
{
    $total = 0;
    foreach ($slices as $fatia) {
        $total += max(0, (int) $fatia[1]);
    }
    $cx = 70;
    $cy = 70;
    $raio = 54;
    $volta = 2 * M_PI * $raio;
    $andado = 0.0;
    echo '<div class="dash-donut">';
    echo '<svg viewBox="0 0 140 140" role="img" aria-label="' . h($legenda !== '' ? $legenda : 'gráfico') . '">';
    echo '<circle cx="' . $cx . '" cy="' . $cy . '" r="' . $raio . '" fill="none" stroke="#e9ecef" stroke-width="20"></circle>';
    if ($total > 0) {
        foreach ($slices as $fatia) {
            $valor = max(0, (int) $fatia[1]);
            if ($valor === 0) {
                continue;
            }
            // Cada fatia é um círculo com dasharray do tamanho do arco; o
            // dashoffset negativo encosta a fatia onde a anterior terminou.
            $arco = $volta * ($valor / $total);
            echo '<circle cx="' . $cx . '" cy="' . $cy . '" r="' . $raio . '" fill="none" stroke="' . h((string) $fatia[2]) . '"'
                . ' stroke-width="20" stroke-dasharray="' . round($arco, 2) . ' ' . round($volta - $arco, 2) . '"'
                . ' stroke-dashoffset="' . round(-$andado, 2) . '" transform="rotate(-90 ' . $cx . ' ' . $cy . ')">'
                . '<title>' . h((string) $fatia[0] . ': ' . $valor) . '</title></circle>';
            $andado += $arco;
        }
    }
    echo '<text class="dash-donut-num" x="' . $cx . '" y="68" text-anchor="middle">' . h($centro) . '</text>';
    echo '<text class="dash-donut-lbl" x="' . $cx . '" y="86" text-anchor="middle">' . h($legenda) . '</text>';
    echo '</svg>';
    echo '<ul class="dash-legend">';
    foreach ($slices as $fatia) {
        $valor = max(0, (int) $fatia[1]);
        $pct = $total > 0 ? (int) round($valor / $total * 100) : 0;
        echo '<li><span class="dot" style="background:' . h((string) $fatia[2]) . '"></span>'
            . h((string) $fatia[0]) . ' <strong>' . $valor . '</strong>'
            . ' <span class="text-muted">(' . $pct . '%)</span></li>';
    }
    echo '</ul></div>';
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
    if ($u && bot_perm_truthy($u['active']) && password_verify((string) ($_POST['password'] ?? ''), $u['password_hash'])) {
        session_regenerate_id(true);
        $_SESSION['bot_user_id'] = (int) $u['id'];
        $pdo->prepare("UPDATE bot_users SET last_login_at = datetime('now') WHERE id = ?")->execute([(int) $u['id']]);
        header('Location: ?page=dashboard');
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
    if (!in_array($page, bot_panel_pages(), true)) {
        if ($page === 'options' || $page === 'targets') {
            header('Location: ?page=menus');
            exit;
        }
        header('Location: ?page=dashboard');
        exit;
    }
    bot_require_page($pdo, $page);
}

$postSynced = false;
if (($_SERVER['REQUEST_METHOD'] ?? '') === 'POST' && $page !== 'login') {
    if (!csrf_ok()) {
        http_response_code(400);
        exit('CSRF');
    }
    $action = (string) ($_POST['action'] ?? '');
    bot_require_action($pdo, $page, $action);
    try {
        if ($page === 'users') {
            $errUsers = bot_users_handle_post($pdo, $action);
            if ($errUsers !== null) {
                $formError = $errUsers;
            }
        }
        if ($page === 'groups') {
            $errGroups = bot_groups_handle_post($pdo, $action);
            if ($errGroups !== null) {
                $formError = $errGroups;
            }
        }
        if (in_array($page, ['teams', 'routing'], true)) {
            $postTeamId = (int) ($_POST['team_id'] ?? 0);
            if ($postTeamId > 0 && !bot_team_in_scope($pdo, $postTeamId)) {
                bot_forbidden('Este time esta fora do seu escopo de acesso.');
            }
        }
        if ($page === 'menus') {
            $postMenuId = (int) ($_POST['menu_id'] ?? 0);
            if ($postMenuId > 0 && !bot_menu_in_scope($pdo, $postMenuId)) {
                bot_forbidden('Este menu esta fora do seu escopo de acesso.');
            }
        }
        if ($action === 'save_queues' || ($page === 'operators' && $action === 'add_operator')) {
            foreach (posted_queue_addresses() as $qaScope) {
                if (!bot_queue_in_scope($pdo, $qaScope)) {
                    bot_forbidden('Fila fora do seu escopo de acesso.');
                }
            }
        }
        if ($page === 'queues' && $action === 'toggle') {
            // Habilitar exige Menu ativo + Time ativo vinculados (as mesmas
            // regras do bot); DESABILITAR e sempre permitido.
            $qaToggle = (string) ($_POST['address'] ?? '');
            $st = $pdo->prepare('SELECT enabled FROM queues WHERE address = ?');
            $st->execute([$qaToggle]);
            $atual = $st->fetchColumn();
            if ($atual === false) {
                $formError = 'Fila não encontrada.';
            } elseif (bool_on($atual)) {
                $pdo->prepare('UPDATE queues SET enabled = 0 WHERE address = ?')->execute([$qaToggle]);
            } else {
                $bloqueio = queue_enable_block($pdo, $qaToggle);
                if ($bloqueio !== '') {
                    $formError = 'Não é possível habilitar a fila ' . $qaToggle . ': ' . $bloqueio
                        . '. Vincule na aba Menus/Times e tente de novo.';
                } else {
                    $pdo->prepare('UPDATE queues SET enabled = 1 WHERE address = ?')->execute([$qaToggle]);
                }
            }
        }
        if (($page === 'queues' || $page === 'operators') && $action === 'sync_catalog') {
            $pdo->exec("INSERT INTO bot_jobs (kind) VALUES ('catalog_sync')");
            $postSynced = true;
        }
        if ($page === 'load' && $action === 'load_now') {
            // Botao "Atualizar agora": o bot le as listas do TOA na hora e grava.
            $pdo->exec("INSERT INTO bot_jobs (kind) VALUES ('load_snapshot')");
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
                    bot_grant_created_to_group($pdo, 'group_menus', 'menu_id', (int) $pdo->lastInsertId());
                }
            } elseif ($action === 'rename' && $mid > 0) {
                $name = trim((string) ($_POST['name'] ?? ''));
                if ($name === '') {
                    $formError = 'Informe o nome do menu.';
                } else {
                    $pdo->prepare('UPDATE menus SET name = ? WHERE id = ?')->execute([$name, $mid]);
                }
            } elseif ($action === 'toggle' && $mid > 0) {
                // Desativar menu com fila HABILITADA vinculada deixaria a fila
                // atendendo sem opcoes (chat cairia em "menu vazio").
                $stMenu = $pdo->prepare('SELECT active FROM menus WHERE id = ?');
                $stMenu->execute([$mid]);
                $menuAtivo = $stMenu->fetchColumn();
                if ($menuAtivo !== false && bool_on($menuAtivo)) {
                    $ligadas = bot_enabled_queues_linked($pdo, 'menu_queues', $mid);
                    if ($ligadas) {
                        $formError = 'Não é possível desativar o menu: a(s) fila(s) ' . implode(', ', $ligadas)
                            . ' está(ão) habilitada(s) com ele. Desabilite a(s) fila(s) primeiro.';
                    } else {
                        $pdo->prepare('UPDATE menus SET active = 0 WHERE id = ?')->execute([$mid]);
                    }
                } else {
                    $pdo->prepare('UPDATE menus SET active = 1 WHERE id = ?')->execute([$mid]);
                }
            } elseif ($action === 'delete' && $mid > 0) {
                $ligadas = bot_enabled_queues_linked($pdo, 'menu_queues', $mid);
                if ($ligadas) {
                    $formError = 'Não é possível excluir o menu: a(s) fila(s) ' . implode(', ', $ligadas)
                        . ' está(ão) habilitada(s) com ele. Desabilite a(s) fila(s) primeiro.';
                } else {
                    $pdo->prepare('DELETE FROM menus WHERE id = ?')->execute([$mid]);
                }
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
                    $saemMenu = array_values(array_diff(
                        bot_enabled_queues_linked($pdo, 'menu_queues', $mid),
                        array_map('strval', $wanted)
                    ));
                    if ($saemMenu) {
                        $formError = 'Não é possível desvincular do menu a(s) fila(s) habilitada(s): '
                            . implode(', ', $saemMenu) . '. Desabilite a(s) fila(s) antes de tirar o menu.';
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
                        $st = $pdo->prepare(
                            'INSERT INTO menu_options (menu_id, queue_address, sort_order, label, classification, action, reply_text, transfer_target_id, parent_id, script_slug)
                             VALUES (?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?)'
                        );
                        $st->execute([$mid, $sort, $label, $classif, $act, $reply, $parentId > 0 ? $parentId : null, $scriptSlug]);
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
                            $pdo->prepare(
                                'UPDATE menu_options SET sort_order = ?, label = ?, classification = ?, action = ?, reply_text = ?, script_slug = ?, reply_text_if_form = ?, action_if_form = ?
                                 WHERE id = ? AND menu_id = ?'
                            )->execute([$sort, $label, $classif, $act, $reply, $scriptSlug, '', '', $oid, $mid]);
                        }
                    }
                }
            } elseif ($action === 'save_questions' && $mid > 0) {
                $oidQ = (int) ($_POST['id'] ?? 0);
                $stQ = $pdo->prepare('SELECT action FROM menu_options WHERE id = ? AND menu_id = ?');
                $stQ->execute([$oidQ, $mid]);
                $rowQ = $stQ->fetch();
                if (!$rowQ || (string) $rowQ['action'] !== 'ask_and_transfer') {
                    $formError = 'As perguntas valem apenas para a acao Pedir info e transferir.';
                } else {
                    [$qErr, $perguntas] = posted_option_questions();
                    if ($qErr !== null) {
                        $formError = $qErr;
                    } else {
                        save_option_questions($pdo, $oidQ, $perguntas);
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
                    bot_grant_created_to_group($pdo, 'group_teams', 'team_id', (int) $pdo->lastInsertId());
                }
            } elseif ($action === 'rename' && $tid > 0) {
                $name = trim((string) ($_POST['name'] ?? ''));
                if ($name === '') {
                    $formError = 'Informe o nome do time.';
                } else {
                    $pdo->prepare('UPDATE teams SET name = ? WHERE id = ?')->execute([$name, $tid]);
                }
            } elseif ($action === 'toggle' && $tid > 0) {
                // Desativar time com fila HABILITADA vinculada deixaria o chat
                // sem ninguem elegivel: o caso acabaria em closed_no_agent.
                $stTime = $pdo->prepare('SELECT active FROM teams WHERE id = ?');
                $stTime->execute([$tid]);
                $timeAtivo = $stTime->fetchColumn();
                if ($timeAtivo !== false && bool_on($timeAtivo)) {
                    $ligadas = bot_enabled_queues_linked($pdo, 'team_queues', $tid);
                    if ($ligadas) {
                        $formError = 'Não é possível desativar o time: a(s) fila(s) ' . implode(', ', $ligadas)
                            . ' está(ão) habilitada(s) com ele. Desabilite a(s) fila(s) primeiro.';
                    } else {
                        $pdo->prepare('UPDATE teams SET active = 0 WHERE id = ?')->execute([$tid]);
                    }
                } else {
                    $pdo->prepare('UPDATE teams SET active = 1 WHERE id = ?')->execute([$tid]);
                }
            } elseif ($action === 'delete' && $tid > 0) {
                $ligadas = bot_enabled_queues_linked($pdo, 'team_queues', $tid);
                if ($ligadas) {
                    $formError = 'Não é possível excluir o time: a(s) fila(s) ' . implode(', ', $ligadas)
                        . ' está(ão) habilitada(s) com ele. Desabilite a(s) fila(s) primeiro.';
                } else {
                    $pdo->prepare('DELETE FROM teams WHERE id = ?')->execute([$tid]);
                }
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
                    $saemTime = array_values(array_diff(
                        bot_enabled_queues_linked($pdo, 'team_queues', $tid),
                        array_map('strval', $wanted)
                    ));
                    if ($saemTime) {
                        $formError = 'Não é possível desvincular do time a(s) fila(s) habilitada(s): '
                            . implode(', ', $saemTime) . '. Desabilite a(s) fila(s) antes de tirar o time.';
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
                if (addr_is_bot($addr, bot_own_address($pdo))) {
                    // O bot só repassa o atendimento: como operador ele receberia o
                    // proprio caso de volta (e a carga dele nao existe no TOA).
                    $formError = 'Este endereço é o PRÓPRIO bot — ele só repassa o atendimento e não pode ser operador.';
                } elseif (substr($addr, 0, 2) !== 'u:') {
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
                if ($addr !== '' && addr_is_bot($addr, bot_own_address($pdo))) {
                    $formError = 'Este endereço é o PRÓPRIO bot — ele só repassa o atendimento e não pode ser operador.';
                } elseif ($addr === '') {
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
        if ($page === 'routing') {
            $rid = (int) ($_POST['team_id'] ?? 0);
            $int_or_null = static function (string $key): ?int {
                $raw = trim((string) ($_POST[$key] ?? ''));
                return $raw === '' ? null : max((int) $raw, 0);
            };
            $flag_or_null = static function (string $key): ?int {
                $raw = trim((string) ($_POST[$key] ?? ''));
                return $raw === '' ? null : ($raw === '1' ? 1 : 0);
            };
            if ($action === 'save_globals') {
                $st = $pdo->prepare(
                    'INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value'
                );
                foreach (
                    [
                        'max_open_chats_per_agent', 'max_cases_per_agent', 'transfer_attempts',
                        'transfer_retry_interval_sec',
                        'max_open_forms_per_agent', 'form_route_attempts', 'form_route_retry_interval_sec',
                        'queue_top_pos',
                        // Resgate por analista offline (0 = desligado).
                        'offline_requeue_sec',
                        // Formulário: para de ser oferecido N min antes do fim da
                        // janela do operador (0 = desligado).
                        'form_window_margin_min',
                    ] as $k
                ) {
                    if (isset($_POST[$k])) {
                        $st->execute([$k, trim((string) $_POST[$k])]);
                    }
                }
                foreach (
                    ['bot_enabled', 'form_routing_enabled', 'queue_wait_enabled', 'queue_show_pos',
                     'offline_requeue_enabled'] as $k
                ) {
                    $st->execute([$k, ((string) ($_POST[$k] ?? 'false')) === 'true' ? 'true' : 'false']);
                }
                $strategy = (string) ($_POST['routing_strategy'] ?? 'round_robin');
                if (!in_array($strategy, ['round_robin', 'priority', 'least_load'], true)) {
                    $strategy = 'round_robin';
                }
                $st->execute(['routing_strategy', $strategy]);
                $fbForm = (string) ($_POST['form_route_fallback'] ?? 'retry');
                if (!in_array($fbForm, ['retry', 'monitor'], true)) {
                    $fbForm = 'retry';
                }
                $st->execute(['form_route_fallback', $fbForm]);
                // "Fazer valer para todos": limpa os overrides (NULL = herda o global).
                $nTimes = 0;
                $nOperadores = 0;
                if (((string) ($_POST['cases_all_teams'] ?? '')) === '1') {
                    $nTimes = (int) $pdo->exec(
                        "UPDATE team_routing SET cap_cases = NULL, updated_at = datetime('now')"
                        . ' WHERE cap_cases IS NOT NULL'
                    );
                }
                if (((string) ($_POST['cases_all_members'] ?? '')) === '1') {
                    $nOperadores = (int) $pdo->exec(
                        'UPDATE team_member_routing SET cap_cases = NULL WHERE cap_cases IS NOT NULL'
                    );
                }
                if ($nTimes > 0 || $nOperadores > 0) {
                    $queueMsg = $nTimes . ' time(s) e ' . $nOperadores
                        . ' operador(es) passaram a herdar o cap de casos global.';
                }
            } elseif ($action === 'queue_expire') {
                $ridQ = (int) ($_POST['route_id'] ?? 0);
                $qParamsA = [];
                $stQ = $pdo->prepare(
                    'SELECT id FROM pending_routes WHERE id = ? AND status = ?'
                    . bot_scope_team_where($pdo, 'team_id', $qParamsA)
                );
                $stQ->execute(array_merge([$ridQ, 'waiting'], $qParamsA));
                if (!$stQ->fetchColumn()) {
                    $formError = 'Item de fila inexistente ou fora do seu escopo.';
                } else {
                    // Encerra a espera agora: o motor aplica o fallback do time na
                    // proxima varredura (a fila nao tem prazo automatico).
                    $pdo->prepare("UPDATE pending_routes SET expires_at = datetime('now', '-1 seconds') WHERE id = ?")
                        ->execute([$ridQ]);
                    $queueMsg = 'Espera encerrada: o bot libera o tecnico na proxima varredura (ate 5 s).';
                }
            } elseif ($action === 'route_reopen') {
                $ridR = (int) ($_POST['route_id'] ?? 0);
                $rParams = [];
                $stR = $pdo->prepare(
                    "SELECT id FROM pending_routes WHERE id = ? AND status IN ('failed', 'cancelled')"
                    . bot_scope_team_where($pdo, 'team_id', $rParams)
                );
                $stR->execute(array_merge([$ridR], $rParams));
                if (!$stR->fetchColumn()) {
                    $formError = 'Rota inexistente, ja ativa ou fora do seu escopo.';
                } else {
                    $pdo->prepare(
                        "UPDATE pending_routes SET status = 'waiting', attempts = 0, move_fails = 0,"
                        . " claimed_by = '', claim_expires_at = NULL, next_try_at = datetime('now'),"
                        . " detail = 'reprocessada pelo painel' WHERE id = ?"
                    )->execute([$ridR]);
                    $queueMsg = 'Rota reprocessada: o bot tenta a entrega na proxima varredura (ate 5 s).';
                }
            } elseif ($action === 'case_release') {
                $cidR = (int) ($_POST['chat_id'] ?? 0);
                $cParams = [];
                $stC = $pdo->prepare(
                    'SELECT chat_id FROM route_assignments WHERE chat_id = ? AND closed_at IS NULL'
                    . bot_scope_team_where($pdo, 'team_id', $cParams)
                );
                $stC->execute(array_merge([$cidR], $cParams));
                if (!$stC->fetchColumn()) {
                    $formError = 'Caso inexistente, ja fechado ou fora do seu escopo.';
                } else {
                    $pdo->prepare(
                        "UPDATE route_assignments SET closed_at = datetime('now'), close_reason = 'manual'"
                        . ' WHERE chat_id = ? AND closed_at IS NULL'
                    )->execute([$cidR]);
                    $queueMsg = 'Caso liberado no ledger (motivo manual): a vaga volta a contar.';
                }
            } elseif ($action === 'save_team_routing') {
                $st = $pdo->prepare('SELECT 1 FROM teams WHERE id = ?');
                $st->execute([$rid]);
                if ($rid < 1 || !$st->fetchColumn()) {
                    $formError = 'Time inválido.';
                } else {
                    $strategy = trim((string) ($_POST['strategy'] ?? ''));
                    if (!in_array($strategy, ['', 'round_robin', 'priority', 'least_load'], true)) {
                        $strategy = '';
                    }
                    $fallback = trim((string) ($_POST['fallback'] ?? ''));
                    if (!in_array($fallback, ['', 'close', 'retry', 'monitor'], true)) {
                        $fallback = '';
                    }
                    $memberKinds = (string) ($_POST['member_kinds'] ?? 'both');
                    if (!in_array($memberKinds, ['both', 'chat', 'form'], true)) {
                        $memberKinds = 'both';
                    }
                    $pdo->prepare(
                        "INSERT INTO team_routing
                           (team_id, strategy, cap_chat, cap_form, cap_cases, require_online, respect_window,
                            attempts, retry_interval_sec, fallback, form_enabled, member_kinds, offline_requeue_sec,
                            form_window_margin_min)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                         ON CONFLICT (team_id) DO UPDATE SET
                           strategy = excluded.strategy, cap_chat = excluded.cap_chat,
                           cap_form = excluded.cap_form, cap_cases = excluded.cap_cases,
                           require_online = excluded.require_online,
                           respect_window = excluded.respect_window, attempts = excluded.attempts,
                           retry_interval_sec = excluded.retry_interval_sec, fallback = excluded.fallback,
                           form_enabled = excluded.form_enabled, member_kinds = excluded.member_kinds,
                           offline_requeue_sec = excluded.offline_requeue_sec,
                           form_window_margin_min = excluded.form_window_margin_min,
                           updated_at = datetime('now')"
                    )->execute([
                        $rid,
                        $strategy === '' ? null : $strategy,
                        $int_or_null('cap_chat'),
                        $int_or_null('cap_form'),
                        $int_or_null('cap_cases'),
                        $flag_or_null('require_online'),
                        $flag_or_null('respect_window'),
                        $int_or_null('attempts'),
                        $int_or_null('retry_interval_sec'),
                        $fallback === '' ? null : $fallback,
                        ((string) ($_POST['form_enabled'] ?? '1')) === '1' ? 1 : 0,
                        $memberKinds,
                        $int_or_null('offline_requeue_sec'),
                        $int_or_null('form_window_margin_min'),
                    ]);
                    // "Replicar para todos os operadores": limpa o ajuste individual
                    // (NULL = herda o time) e, se pedido, o tipo individual tambem.
                    $replicou = 0;
                    if (((string) ($_POST['cases_all_members'] ?? '')) === '1') {
                        $stRep = $pdo->prepare(
                            'UPDATE team_member_routing SET cap_cases = NULL WHERE team_id = ?'
                        );
                        $stRep->execute([$rid]);
                        $replicou += (int) $stRep->rowCount();
                    }
                    if (((string) ($_POST['kinds_all_members'] ?? '')) === '1') {
                        $stRep = $pdo->prepare(
                            'UPDATE team_member_routing SET kinds = ?, forms_enabled = ? WHERE team_id = ?'
                        );
                        $stRep->execute([$memberKinds, $memberKinds === 'chat' ? 0 : 1, $rid]);
                        $replicou += (int) $stRep->rowCount();
                    }
                    if ($replicou > 0) {
                        $queueMsg = 'Ajustes replicados para os operadores do time ('
                            . $replicou . ' linha(s) atualizada(s)).';
                    }
                }
            } elseif ($action === 'reset_team_routing' && $rid > 0) {
                $pdo->prepare('DELETE FROM team_routing WHERE team_id = ?')->execute([$rid]);
            } elseif ($action === 'save_member_routing') {
                $addr = trim((string) ($_POST['address'] ?? ''));
                if ($rid < 1 || $addr === '') {
                    $formError = 'Membro inválido.';
                } else {
                    $kindsMembro = (string) ($_POST['kinds'] ?? 'both');
                    if (!in_array($kindsMembro, ['both', 'chat', 'form'], true)) {
                        $kindsMembro = 'both';
                    }
                    $pdo->prepare(
                        'INSERT INTO team_member_routing (team_id, address, weight, cap_chat, cap_form, cap_cases, forms_enabled, kinds)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                         ON CONFLICT (team_id, address) DO UPDATE SET
                           weight = excluded.weight, cap_chat = excluded.cap_chat,
                           cap_form = excluded.cap_form, cap_cases = excluded.cap_cases,
                           forms_enabled = excluded.forms_enabled, kinds = excluded.kinds'
                    )->execute([
                        $rid,
                        $addr,
                        $int_or_null('weight') ?? 0,
                        $int_or_null('cap_chat'),
                        $int_or_null('cap_form'),
                        $int_or_null('cap_cases'),
                        $kindsMembro === 'chat' ? 0 : 1,
                        $kindsMembro,
                    ]);
                }
            } elseif ($action === 'reset_member_routing') {
                $pdo->prepare('DELETE FROM team_member_routing WHERE team_id = ? AND address = ?')
                    ->execute([$rid, (string) ($_POST['address'] ?? '')]);
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
        if ($page === 'routing') {
            $hid = (int) ($_POST['team_id'] ?? 0);
            if ($hid > 0) {
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
    'load' => 'Carga',
    'templates' => 'Templates',
    'menus' => 'Menus',
    'teams' => 'Times',
    'routing' => 'Roteamento',
    'settings' => 'Ajustes',
    'chats' => 'Chats',
    'groups' => 'Grupos',
    'users' => 'Usuarios',
];
// Menu filtrado por permissao: Usuarios/Grupos sao exclusivos do Super Admin.
foreach ($nav as $navKey => $navLabel) {
    if (in_array($navKey, bot_super_only_pages(), true)) {
        if (!bot_is_super(bot_current_user($pdo))) {
            unset($nav[$navKey]);
        }
        continue;
    }
    if (!bot_can($pdo, bot_page_permission($navKey))) {
        unset($nav[$navKey]);
    }
}

function layout_start(string $title, array $nav, string $page, bool|int $autoRefresh = false): void
{
    echo '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">';
    echo '<title>' . h($title) . '</title>';
    if ($autoRefresh) {
        echo '<meta http-equiv="refresh" content="' . (is_int($autoRefresh) ? (int) $autoRefresh : 5) . '">';
    }
    echo '<link href="assets/bootstrap.min.css" rel="stylesheet">';
    echo '<link href="assets/panel.css" rel="stylesheet">';
    echo '</head><body data-page="' . h($page) . '">';
    if ($page !== 'login') {
        echo '<nav class="navbar navbar-expand-lg navbar-dark navbar-helptoa">';
        $brandPage = (string) (array_key_first($nav) ?: 'dashboard');
        echo '<div class="container-xl"><a class="navbar-brand" href="?page=' . h($brandPage) . '">HELPTOA Bot</a>';
        echo '<button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#botNav" aria-controls="botNav" aria-expanded="false" aria-label="Menu">';
        echo '<span class="navbar-toggler-icon"></span></button>';
        echo '<div class="collapse navbar-collapse" id="botNav"><ul class="navbar-nav me-auto mb-2 mb-lg-0">';
        foreach ($nav as $k => $label) {
            echo '<li class="nav-item"><a class="nav-link' . ($page === $k ? ' active' : '') . '" href="?page=' . h($k) . '">' . h($label) . '</a></li>';
        }
        echo '</ul><ul class="navbar-nav align-items-lg-center">';
        $navUser = bot_current_user(bot_pdo());
        if ($navUser !== null) {
            $navGroup = bot_is_super($navUser) ? 'Super Admin' : (string) ($navUser['group_name'] ?? 'sem grupo');
            echo '<li class="nav-item me-lg-2"><div class="nav-user d-none d-lg-block"><strong>' . h(bot_user_label($navUser)) . '</strong>' . h((string) $navGroup) . '</div></li>';
        }
        echo '<li class="nav-item"><a class="btn btn-outline-light btn-sm" href="?page=logout">Sair</a></li></ul>';
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
    echo '<script src="assets/panel.js"></script>';
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
$autoRefresh = ($page === 'activity' && $listPage === 1 && $filterReason === '' && empty($_GET['filas']) && empty($_GET['de']) && empty($_GET['ate'])) || $page === 'load';
layout_start('HELPTOA Bot', $nav, $page, $page === 'load' ? 30 : $autoRefresh);

if ($page === 'queues') {
    $syncBtn = bot_can($pdo, 'queues.manage')
        ? '<form method="post" class="d-inline"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="sync_catalog"><button class="btn btn-primary btn-sm">Sincronizar do TOA</button></form>'
        : '';
    page_header('Filas', 'Fila habilitada e o que o bot atende; o catalogo vem do TOA.', $syncBtn);
    echo '<div class="d-none">';
    echo '<h1 class="h3 mb-0">Filas</h1>';
    echo '<form method="post" class="d-inline"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="sync_catalog">';
    echo '<button class="btn btn-primary">Sincronizar (recoletar do TOA)</button></form></div>';
    if (($_GET['synced'] ?? '') === '1') {
        echo '<div class="alert alert-success py-2 mt-2 mb-2">Sincronização solicitada — o bot processa em até ~5 s (novas filas e operadores entram no catálogo).</div>';
    }
    echo '<p class="text-muted">Novas filas entram desabilitadas. O bot só atende as habilitadas — e só é possível <strong>habilitar</strong> uma fila com <strong>Menu</strong> e <strong>Time</strong> ativos vinculados.</p>';
    if (!empty($formError)) {
        echo '<div class="alert alert-danger py-2 mt-2 mb-2">' . h($formError) . '</div>';
    }
    $listParams = [];
    $listWhere = bot_scope_queue_where($pdo, 'q.address', $listParams);
    // Menu e Time vem por SUBQUERY (uma linha por fila, sem risco de
    // duplicacao) e mostram apenas o vinculo ATIVO — o que o bot enxerga. Os
    // contadores `menus_ativos`/`times_ativos` alimentam a trava de habilitacao
    // sem consulta extra por linha.
    $stList = $pdo->prepare(
        'SELECT q.*,'
        . ' (SELECT m.name FROM menu_queues mq JOIN menus m ON m.id = mq.menu_id'
        . '   WHERE mq.queue_address = q.address AND m.active = 1'
        . '   ORDER BY m.name LIMIT 1) AS menu_name,'
        . ' (SELECT t.name FROM team_queues tq JOIN teams t ON t.id = tq.team_id'
        . '   WHERE tq.queue_address = q.address AND t.active = 1'
        . '   ORDER BY t.name LIMIT 1) AS team_name,'
        . ' (SELECT COUNT(*) FROM menu_queues mq2 JOIN menus m2 ON m2.id = mq2.menu_id'
        . '   WHERE mq2.queue_address = q.address AND m2.active = 1) AS menus_ativos,'
        . ' (SELECT COUNT(*) FROM team_queues tq2 JOIN teams t2 ON t2.id = tq2.team_id'
        . '   WHERE tq2.queue_address = q.address AND t2.active = 1) AS times_ativos'
        . ' FROM queues q WHERE 1 = 1' . $listWhere
        . ' ORDER BY q.name, q.address'
    );
    $stList->execute($listParams);
    $rows = $stList->fetchAll();
    echo '<div class="card card-slim"><div class="card-body pb-2"><input type="search" class="form-control form-control-sm" placeholder="Filtrar filas por nome, address, menu ou time..." data-filter-table="#tabela-filas" autocomplete="off"></div>';
    echo '<div class="table-responsive sticky-head"><table class="table table-slim table-hover align-middle mb-0" id="tabela-filas">';
    echo '<thead><tr><th>Nome</th><th>Address</th><th>Menu</th><th>Time</th><th>Habilitada</th><th></th></tr></thead><tbody>';
    foreach ($rows as $r) {
        $on = bool_on($r['enabled']);
        $semMenu = (int) ($r['menus_ativos'] ?? 0) < 1;
        $semTime = (int) ($r['times_ativos'] ?? 0) < 1;
        $pendencia = '';
        if ($semMenu && $semTime) {
            $pendencia = 'Vincule um Menu e um Time ativos antes de habilitar';
        } elseif ($semMenu) {
            $pendencia = 'Vincule um Menu ativo antes de habilitar';
        } elseif ($semTime) {
            $pendencia = 'Vincule um Time ativo antes de habilitar';
        }
        echo '<tr><td>' . h($r['name']) . '</td><td><code>' . h($r['address']) . '</code></td>';
        echo '<td>' . ($r['menu_name'] ? '<span class="badge text-bg-light">' . h($r['menu_name']) . '</span>' : '<span class="text-muted">—</span>')
            . ($semMenu ? ' <span class="badge bg-warning text-dark">sem menu ativo</span>' : '') . '</td>';
        echo '<td>' . ($r['team_name'] ? '<span class="badge text-bg-light">' . h($r['team_name']) . '</span>' : '<span class="text-muted">—</span>')
            . ($semTime ? ' <span class="badge bg-warning text-dark">sem time ativo</span>' : '') . '</td>';
        echo '<td>' . badge_state($on, 'sim', 'não') . '</td>';
        echo '<td><form class="d-inline" method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
        echo '<input type="hidden" name="action" value="toggle"><input type="hidden" name="address" value="' . h($r['address']) . '">';
        if ($on) {
            echo '<button class="btn btn-sm btn-outline-secondary">Desabilitar</button>';
        } elseif ($pendencia !== '') {
            echo '<button class="btn btn-sm btn-success" disabled title="' . h($pendencia) . '">Habilitar</button>'
                . '<div class="form-text">' . h($pendencia) . '</div>';
        } else {
            echo '<button class="btn btn-sm btn-success">Habilitar</button>';
        }
        echo '</form></td></tr>';
    }
    echo '</tbody></table></div></div>';
    if (!$rows) {
        echo '<p class="text-muted">Nenhuma fila ainda. O Python preenche na primeira conexão.</p>';
    }
}

if ($page === 'operators') {
    $opSyncBtn = bot_can($pdo, 'operators.manage')
        ? '<form method="post" class="d-inline"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="sync_catalog"><button class="btn btn-primary btn-sm">Recoletar do TOA</button></form>'
        : '';
    page_header('Operadores', 'Catalogo usado ao montar os times. Itens do TOA voltam na proxima sincronizacao.', $opSyncBtn);
    echo '<div class="d-none">';
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
    $qScopeParams = [];
    $qScopeWhere = bot_scope_queue_where($pdo, 'address', $qScopeParams);
    $stQscope = $pdo->prepare('SELECT address, name FROM queues WHERE 1 = 1' . $qScopeWhere . ' ORDER BY name, address');
    $stQscope->execute($qScopeParams);
    $queues = $stQscope->fetchAll();
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
    $opParams = [];
    $opWhere = bot_scope_queue_where($pdo, 'qo.queue_address', $opParams);
    $stOp = $pdo->prepare(
        "SELECT qo.queue_address, qo.address, qo.name, qo.source, q.name AS queue_name
         FROM queue_operators qo
         LEFT JOIN queues q ON q.address = qo.queue_address
         WHERE 1 = 1" . $opWhere . "
         ORDER BY qo.name, qo.address, qo.queue_address"
    );
    $stOp->execute($opParams);
    $opRows = $stOp->fetchAll();
    echo '<div class="table-responsive sticky-head"><table class="table table-slim table-hover align-middle bg-white mb-0">';
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
    $qScopeParams = [];
    $qScopeWhere = bot_scope_queue_where($pdo, 'address', $qScopeParams);
    $stQscope = $pdo->prepare('SELECT address, name FROM queues WHERE 1 = 1' . $qScopeWhere . ' ORDER BY name, address');
    $stQscope->execute($qScopeParams);
    $queues = $stQscope->fetchAll();
    echo '<div class="card mb-3"><div class="card-body">';
    echo '<form method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="create">';
    echo '<h2 class="h5">Novo menu</h2><div class="row g-2 align-items-end">';
    echo '<div class="col-md-6"><label class="form-label">Nome</label><input class="form-control" name="name" required></div>';
    echo '<div class="col-auto"><button class="btn btn-primary">Criar</button></div></div></form></div></div>';
    $menuParams = [];
    $menuWhere = bot_scope_menu_where($pdo, 'm.id', $menuParams);
    $stMenu = $pdo->prepare('SELECT m.* FROM menus m WHERE 1 = 1' . $menuWhere . ' ORDER BY m.name');
    $stMenu->execute($menuParams);
    $menus = $stMenu->fetchAll();
    $mqParams = [];
    $mqWhere = bot_scope_menu_where($pdo, 'm.id', $mqParams);
    $stMq = $pdo->prepare(
        'SELECT mq.menu_id, mq.queue_address, m.name AS menu_name FROM menu_queues mq JOIN menus m ON m.id = mq.menu_id WHERE 1 = 1' . $mqWhere
    );
    $stMq->execute($mqParams);
    $mqRows = $stMq->fetchAll();
    $byMenuQ = [];
    $queueMenu = [];
    foreach ($mqRows as $r) {
        $byMenuQ[(int) $r['menu_id']][] = $r['queue_address'];
        $queueMenu[$r['queue_address']] = ['id' => (int) $r['menu_id'], 'name' => $r['menu_name']];
    }
    $optParams = [];
    $optWhere = bot_scope_menu_where($pdo, 'menu_id', $optParams);
    $stOpts = $pdo->prepare('SELECT * FROM menu_options WHERE menu_id IS NOT NULL' . $optWhere . ' ORDER BY menu_id, sort_order');
    $stOpts->execute($optParams);
    $optRows = $stOpts->fetchAll();
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
    $qScopeParams2 = [];
    $qScopeWhere2 = bot_scope_queue_where($pdo, 'address', $qScopeParams2);
    $stQscope2 = $pdo->prepare('SELECT address, name FROM queues WHERE 1 = 1' . $qScopeWhere2 . ' ORDER BY name, address');
    $stQscope2->execute($qScopeParams2);
    $queues = $stQscope2->fetchAll();
    $operators = [];
    try {
        $opParams2 = [];
        $opWhere2 = bot_scope_queue_where($pdo, 'queue_address', $opParams2);
        $stOps = $pdo->prepare(
            'SELECT DISTINCT address, MAX(name) AS name FROM queue_operators WHERE 1 = 1' . $opWhere2
            . ' GROUP BY address ORDER BY MAX(name), address'
        );
        $stOps->execute($opParams2);
        $operators = $stOps->fetchAll();
    } catch (Throwable $e) {
        $operators = [];
    }
    echo '<div class="card mb-3"><div class="card-body">';
    echo '<form method="post"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="create">';
    echo '<h2 class="h5">Novo time</h2><div class="row g-2 align-items-end">';
    echo '<div class="col-md-6"><label class="form-label">Nome</label><input class="form-control" name="name" required placeholder="Plantão Ago/26"></div>';
    echo '<div class="col-auto"><button class="btn btn-primary">Criar</button></div></div></form></div></div>';
    $teamParams = [];
    $teamWhere = bot_scope_team_where($pdo, 'id', $teamParams);
    $stTeam = $pdo->prepare('SELECT * FROM teams WHERE 1 = 1' . $teamWhere . ' ORDER BY name');
    $stTeam->execute($teamParams);
    $teams = $stTeam->fetchAll();
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

if ($page === 'routing') {
    echo '<h1 class="h3 mb-1">Roteamento</h1>';
    echo '<p class="text-muted"><strong>Carga = casos</strong>: formulário e chat do <strong>mesmo solicitante</strong> contam como <strong>um caso</strong> — por isso o chat de quem já tem formulário vai <strong>direto para o mesmo analista</strong>, mesmo com o cap cheio. <strong>Cap de casos</strong> limita solicitantes distintos; <em>Cap chat</em> e <em>Cap form</em> são tetos extras por tipo. A carga vem do TOA (chats em atendimento na fila) somada ao que o bot já entregou. Quem recebe o <strong>chat</strong> e o <strong>formulário de validação</strong>. Herança: <strong>membro → time → global</strong> (vazio = herda). A ordem do rodízio é a <em>Ordem</em> do membro na aba Times; o <em>peso</em> só vale na estratégia <strong>Prioridade</strong>, onde o maior peso é o titular e, se ele estiver no cap, cai para o próximo. Online e carga de cada analista são lidos no TOA a cada transferência, não aqui.</p>';
    if (!empty($formError)) {
        echo '<div class="alert alert-danger">' . h($formError) . '</div>';
    }
    if (!empty($queueMsg)) {
        echo '<div class="alert alert-success py-2 mt-2 mb-2">' . h($queueMsg) . '</div>';
    }
    $map = [];
    foreach ($pdo->query('SELECT key, value FROM bot_settings') as $r) {
        $map[$r['key']] = $r['value'];
    }
    echo '<div class="card mb-3"><div class="card-body"><h2 class="h5">Globais</h2>';
    echo '<form method="post" class="row g-3"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="save_globals">';
    echo '<div class="col-md-3"><label class="form-label">Bot ligado</label><select class="form-select" name="bot_enabled">';
    echo '<option value="true"' . (($map['bot_enabled'] ?? 'true') === 'true' ? ' selected' : '') . '>Ligado</option>';
    echo '<option value="false"' . (($map['bot_enabled'] ?? '') === 'false' ? ' selected' : '') . '>Desligado</option></select></div>';
    echo '<div class="col-md-3"><label class="form-label">Formulário roteado</label><select class="form-select" name="form_routing_enabled">';
    echo '<option value="true"' . (($map['form_routing_enabled'] ?? 'true') === 'true' ? ' selected' : '') . '>Ligado</option>';
    echo '<option value="false"' . (($map['form_routing_enabled'] ?? '') === 'false' ? ' selected' : '') . '>Desligado (só registra o evento)</option></select></div>';
    echo '<div class="col-md-3"><label class="form-label">Devolver caso de analista offline</label><select class="form-select" name="offline_requeue_enabled">';
    echo '<option value="true"' . (($map['offline_requeue_enabled'] ?? 'true') === 'true' ? ' selected' : '') . '>Ligado</option>';
    echo '<option value="false"' . (($map['offline_requeue_enabled'] ?? '') === 'false' ? ' selected' : '') . '>Desligado</option></select></div>';
    echo '<div class="col-md-3"><label class="form-label">Offline para devolver (s)</label><input class="form-control" name="offline_requeue_sec" value="' . h((string) ($map['offline_requeue_sec'] ?? '120')) . '"></div>';
    echo '<div class="col-md-3"><label class="form-label">Estratégia</label><select class="form-select" name="routing_strategy">';
    foreach (['round_robin' => 'Rodízio (como hoje)', 'priority' => 'Prioridade (peso)', 'least_load' => 'Menor carga'] as $sk => $sl) {
        echo '<option value="' . h($sk) . '"' . (($map['routing_strategy'] ?? 'round_robin') === $sk ? ' selected' : '') . '>' . h($sl) . '</option>';
    }
    echo '</select></div>';
    echo '<div class="col-md-3"><label class="form-label">Fila de espera (chat)</label><select class="form-select" name="queue_wait_enabled">';
    echo '<option value="true"' . (($map['queue_wait_enabled'] ?? 'true') === 'true' ? ' selected' : '') . '>Ligada (avisa a posição)</option>';
    echo '<option value="false"' . (($map['queue_wait_enabled'] ?? '') === 'false' ? ' selected' : '') . '>Desligada (só tenta de novo)</option></select></div>';
    echo '<div class="col-md-3"><label class="form-label">Posição falada no chat</label><select class="form-select" name="queue_show_pos">';
    echo '<option value="false"' . (($map['queue_show_pos'] ?? 'false') === 'false' ? ' selected' : '') . '>Não falar o número (só "avançou na fila")</option>';
    echo '<option value="true"' . (($map['queue_show_pos'] ?? '') === 'true' ? ' selected' : '') . '>Falar o número da fila</option></select></div>';
    echo '<div class="col-md-3"><label class="form-label">Fallback do formulário</label><select class="form-select" name="form_route_fallback">';
    echo '<option value="retry"' . (($map['form_route_fallback'] ?? 'retry') === 'retry' ? ' selected' : '') . '>Repetir até achar analista</option>';
    echo '<option value="monitor"' . (($map['form_route_fallback'] ?? '') === 'monitor' ? ' selected' : '') . '>Não insistir</option></select></div>';
    $globFields = [
        'max_open_chats_per_agent' => ['Cap de chats por analista', '8'],
        'max_open_forms_per_agent' => ['Cap de formulários por analista', '8'],
        'max_cases_per_agent' => ['Casos por analista (form + chat do mesmo autor = 1 caso)', '4'],
        'transfer_attempts' => ['Tentativas de transferência (chat)', '5'],
        'transfer_retry_interval_sec' => ['Intervalo entre tentativas do chat (s)', '60'],
        'queue_top_pos' => ['Posição a partir da qual o chat ouve o número (0 = sempre)', '5'],
        'form_route_attempts' => ['Tentativas de rota do formulário', '5'],
        'form_route_retry_interval_sec' => ['Intervalo entre tentativas do formulário (s)', '60'],
        'form_window_margin_min' => ['Formulário: parar de oferecer (min antes do fim da janela)', '10'],
    ];
    foreach ($globFields as $gk => $gv) {
        echo '<div class="col-md-3"><label class="form-label">' . h($gv[0]) . '</label>';
        echo '<input class="form-control" name="' . h($gk) . '" value="' . h($map[$gk] ?? $gv[1]) . '"></div>';
    }
    echo '<div class="col-12 mt-3"><div class="form-check form-check-inline">';
    echo '<input class="form-check-input" type="checkbox" name="cases_all_teams" id="casesAllTeams" value="1">';
    echo '<label class="form-check-label" for="casesAllTeams">Fazer o <strong>Casos</strong> global valer para <strong>todos os times</strong></label></div>';
    echo '<div class="form-check form-check-inline">';
    echo '<input class="form-check-input" type="checkbox" name="cases_all_members" id="casesAllMembers" value="1">';
    echo '<label class="form-check-label" for="casesAllMembers">… e também para <strong>todos os operadores</strong></label></div>';
    echo '<div class="form-text">Marcar limpa o cap de casos configurado no time/operador: eles passam a herdar este valor (nenhum peso ou outra configuração é perdido).</div></div>';
    echo '<button class="btn btn-primary mt-3">Salvar globais</button></form>';
    echo '<p class="text-muted small mb-0 mt-3">O cap limita quantos chats/formulários simultâneos o analista recebe; o <strong>Casos</strong> limita solicitantes distintos (formulário e chat do <strong>mesmo autor</strong> contam como um caso) e é o número usado na escolha do analista. O intervalo do chat é a espera entre as consultas de online; o do formulário é o intervalo do retry, que roda em background e não trava o atendimento. Com a <strong>fila de espera</strong> ligada, o item <strong>aguarda sem prazo</strong>: ele só sai da fila por <strong>entrega</strong>, porque o <strong>técnico saiu do chat</strong> (ou o chat foi removido), porque o item <strong>sumiu das listas do TOA</strong> (resolvido) ou pelo botão <em>Encerrar espera</em>, que aplica o fallback do time. A <strong>posição falada</strong> é contada só entre chats (a linha do formulário é apartada) e, no padrão, o técnico ouve apenas "Você avançou na fila" — para falar o número, escolha <em>Falar o número da fila</em> e o número aparece a partir do campo <em>Posição a partir da qual o chat ouve o número</em>. Desligada, o bot apenas repete as tentativas e aplica o fallback do time.</p>';
    echo '</div></div>';
    $qParams = [];
    $qWhere = bot_scope_team_where($pdo, 'r.team_id', $qParams);
    $stQueue = $pdo->prepare(
        'SELECT r.*, c.status AS chat_status FROM pending_routes r
          LEFT JOIN chats c ON c.chat_id = r.chat_id
         WHERE r.status = ?' . $qWhere . ' ORDER BY r.id LIMIT 50'
    );
    $stQueue->execute(array_merge(['waiting'], $qParams));
    $queueRows = $stQueue->fetchAll();
    $qPos = [];
    $qCont = [];
    foreach ($queueRows as $rq) {
        $chave = (string) $rq['kind'] . '|' . (string) $rq['team_id'];
        $qCont[$chave] = ($qCont[$chave] ?? 0) + 1;
        $qPos[(int) $rq['id']] = $qCont[$chave];
    }
    $agoraTs = time();
    $qEspera = static function (?string $iso) use ($agoraTs): string {
        $ts = $iso ? strtotime($iso . ' UTC') : 0;
        if (!$ts) {
            return '-';
        }
        $seg = max(0, $agoraTs - $ts);
        if ($seg < 60) {
            return $seg . ' s';
        }
        if ($seg < 3600) {
            return floor($seg / 60) . ' min ' . ($seg % 60) . ' s';
        }
        return floor($seg / 3600) . ' h ' . floor(($seg % 3600) / 60) . ' min';
    };
    $qRestante = static function (?string $iso) use ($agoraTs): string {
        $ts = $iso ? strtotime($iso . ' UTC') : 0;
        if (!$ts) {
            return 'sem prazo';
        }
        $seg = $ts - $agoraTs;
        if ($seg <= 0) {
            return 'encerrando';
        }
        if ($seg < 3600) {
            return 'em ' . max(1, (int) floor($seg / 60)) . ' min';
        }
        return 'em ' . floor($seg / 3600) . ' h ' . floor(($seg % 3600) / 60) . ' min';
    };
    $qNomes = [];
    foreach ($pdo->query('SELECT address, name FROM queues') as $rqN) {
        $qNomes[(string) $rqN['address']] = (string) ($rqN['name'] ?: $rqN['address']);
    }
    $qTimes = [];
    foreach ($pdo->query('SELECT id, name FROM teams') as $rtN) {
        $qTimes[(int) $rtN['id']] = (string) $rtN['name'];
    }
    echo '<div class="card mb-3"><div class="card-body"><h2 class="h5">Fila de espera (agora)</h2>';
    if (!$queueRows) {
        echo '<p class="text-muted mb-0">Ninguém esperando.</p>';
    } else {
        echo '<div class="table-responsive"><table class="table table-sm table-hover align-middle mb-0">';
        echo '<thead><tr><th>#</th><th>Tipo</th><th>Fila</th><th>Time</th><th>Chat</th><th>Esperando</th><th>Prazo</th><th></th></tr></thead><tbody>';
        foreach ($queueRows as $rq) {
            $ridQ = (int) $rq['id'];
            $ehChat = (string) $rq['kind'] === 'chat';
            echo '<tr>';
            echo '<td><span class="badge text-bg-dark">' . (int) $qPos[$ridQ] . 'º</span></td>';
            echo '<td>' . ($ehChat ? 'chat' : 'formulário') . '</td>';
            echo '<td><code>' . h($qNomes[(string) $rq['queue_address']] ?? (string) $rq['queue_address']) . '</code></td>';
            echo '<td>' . h($qTimes[(int) $rq['team_id']] ?? '-') . '</td>';
            echo '<td>' . (int) $rq['chat_id'];
            if (!$ehChat) {
                echo ' <span class="badge text-bg-light">silencioso</span>';
            }
            echo '</td>';
            echo '<td>' . h($qEspera($rq['created_at'])) . ' <span class="text-muted small">(' . (int) $rq['attempts'] . ' tentativa(s))</span></td>';
            echo '<td>' . h($qRestante($rq['expires_at'])) . '</td>';
            echo '<td class="text-end">';
            if ($ehChat) {
                echo '<form method="post" class="d-inline"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
                echo '<input type="hidden" name="action" value="queue_expire"><input type="hidden" name="route_id" value="' . $ridQ . '">';
                echo '<button class="btn btn-sm btn-outline-secondary" title="Encerra a espera agora: o bot aplica o fallback do time">Encerrar espera</button></form>';
            }
            echo '</td></tr>';
        }
        echo '</tbody></table></div>';
        echo '<p class="small text-muted mb-0 mt-2">Chat e formulário esperam em <strong>linhas apartadas</strong>: a posição é contada por time e por tipo, em ordem de chegada — um formulário enviado depois de 10 chats é o <strong>1º da linha dele</strong>, não o 11º. A espera <strong>não vence sozinha</strong>: o item só sai da fila por <strong>entrega</strong>, porque o <strong>técnico saiu do chat</strong> (ou o chat foi removido), porque <strong>sumiu das listas do TOA</strong> (resolvido) ou pelo botão <em>Encerrar espera</em> (aí o bot aplica o fallback do time). O aviso de posição vai apenas para o chat; o formulário espera em silêncio.</p>';
    }
    echo '</div></div>';
    // Entregas paradas, casos sem entrega e ledger (FASE A: o que era invisivel).
    $oParams = [];
    $oWhere = bot_scope_team_where($pdo, 'r.team_id', $oParams);
    $stOrf = $pdo->prepare(
        'SELECT r.*, c.status AS chat_status FROM pending_routes r'
        . ' LEFT JOIN chats c ON c.chat_id = r.chat_id'
        . " WHERE r.status IN ('failed', 'cancelled')" . $oWhere
        . ' ORDER BY r.id DESC LIMIT 50'
    );
    $stOrf->execute($oParams);
    $orfRows = $stOrf->fetchAll();
    $stSemRota = $pdo->prepare(
        'SELECT c.chat_id, c.queue_address, c.classification, c.updated_at FROM chats c'
        . " WHERE c.status = 'classified' AND c.queue_address IS NOT NULL"
        . ' AND NOT EXISTS (SELECT 1 FROM pending_routes r WHERE r.chat_id = c.chat_id)'
        . ' ORDER BY c.chat_id LIMIT 50'
    );
    $stSemRota->execute();
    $semRota = $stSemRota->fetchAll();
    echo '<div class="card mb-3"><div class="card-body"><h2 class="h5">Entregas paradas e sem dono</h2>';
    if (!$orfRows && !$semRota) {
        echo '<p class="text-muted mb-0">Nenhuma rota parada e nenhum caso classificado sem entrega.</p>';
    } else {
        if ($semRota) {
            echo '<p class="mb-1"><strong>Classificados sem rota</strong> <span class="text-muted small">(o bot recria a entrega na proxima varredura)</span></p>';
            echo '<div class="table-responsive"><table class="table table-sm align-middle mb-3"><thead><tr><th>Chat</th><th>Fila</th><th>Classificacao</th><th>Desde</th></tr></thead><tbody>';
            foreach ($semRota as $r) {
                echo '<tr><td>' . (int) $r['chat_id'] . '</td><td><code>' . h((string) $r['queue_address']) . '</code></td><td>' . h((string) $r['classification']) . '</td><td>' . h($qEspera($r['updated_at'])) . '</td></tr>';
            }
            echo '</tbody></table></div>';
        }
        if ($orfRows) {
            echo '<p class="mb-1"><strong>Rotas sem entrega</strong> <span class="text-muted small">(falha ou cancelada: use Reprocessar para o bot tentar de novo)</span></p>';
            echo '<div class="table-responsive"><table class="table table-sm align-middle mb-0"><thead><tr><th>#</th><th>Tipo</th><th>Chat</th><th>Fila</th><th>Estado</th><th>Motivo</th><th></th></tr></thead><tbody>';
            foreach ($orfRows as $r) {
                $ridO = (int) $r['id'];
                echo '<tr><td>' . $ridO . '</td><td>' . h((string) $r['kind']) . '</td><td>' . (int) $r['chat_id'] . '</td><td><code>' . h((string) $r['queue_address']) . '</code></td><td><span class="badge text-bg-warning">' . h((string) $r['status']) . '</span></td><td class="small">' . h(ui_clip((string) $r['detail'], 90)) . '</td><td class="text-end">';
                echo '<form method="post" class="d-inline"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="route_reopen"><input type="hidden" name="route_id" value="' . $ridO . '">';
                echo '<button class="btn btn-sm btn-outline-secondary" title="Tenta a entrega de novo na proxima varredura">Reprocessar</button></form>';
                echo '</td></tr>';
            }
            echo '</tbody></table></div>';
        }
        echo '<p class="small text-muted mb-0 mt-2">Rota parada aqui nao e re-roteada automaticamente (a regra do formulario continua: o mesmo caso nao e roteado duas vezes).</p>';
    }
    echo '</div></div>';
    $aParams = [];
    $aWhere = bot_scope_team_where($pdo, 'team_id', $aParams);
    $stCasos = $pdo->prepare(
        'SELECT * FROM route_assignments WHERE closed_at IS NULL' . $aWhere . ' ORDER BY assigned_at LIMIT 50'
    );
    $stCasos->execute($aParams);
    $casosAbertos = $stCasos->fetchAll();
    $stTries = $pdo->query(
        "SELECT op, result, count(*) AS n FROM delivery_attempts"
        . " WHERE datetime(at) >= datetime('now', '-1 day') GROUP BY op, result ORDER BY n DESC"
    );
    $tries = $stTries ? $stTries->fetchAll() : [];
    echo '<div class="card mb-3"><div class="card-body"><h2 class="h5">Casos abertos (ledger) e tentativas de entrega (24 h)</h2>';
    if (!$casosAbertos) {
        echo '<p class="text-muted mb-2">Nenhum caso aberto.</p>';
    } else {
        echo '<div class="table-responsive"><table class="table table-sm align-middle mb-2"><thead><tr><th>Chat</th><th>Tipo</th><th>Analista</th><th>Caso</th><th>Aberto ha</th><th></th></tr></thead><tbody>';
        foreach ($casosAbertos as $rc) {
            echo '<tr><td>' . (int) $rc['chat_id'] . '</td><td>' . h((string) $rc['kind']) . '</td><td><code>' . h((string) $rc['assignee']) . '</code></td><td class="small">' . h((string) $rc['case_key']) . '</td><td>' . h($qEspera($rc['assigned_at'])) . '</td><td class="text-end">';
            echo '<form method="post" class="d-inline"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="case_release"><input type="hidden" name="chat_id" value="' . (int) $rc['chat_id'] . '">';
            echo '<button class="btn btn-sm btn-outline-secondary" title="Fecha o caso no ledger com motivo manual (libera a vaga)">Liberar</button></form>';
            echo '</td></tr>';
        }
        echo '</tbody></table></div>';
    }
    if ($tries) {
        echo '<p class="mb-1 small"><strong>Tentativas (ok / timeout / erro)</strong> ';
        foreach ($tries as $rt) {
            echo '<span class="badge ' . ($rt['result'] === 'timeout' ? 'text-bg-danger' : ($rt['result'] === 'error' ? 'text-bg-warning' : 'text-bg-secondary')) . ' me-1">' . h((string) $rt['op']) . '=' . h((string) $rt['result']) . ': ' . (int) $rt['n'] . '</span>';
        }
        echo '</p><p class="small text-muted mb-0">Timeout de ACK aparece aqui: e a medicao que autoriza a FASE B.</p>';
    }
    echo '</div></div>';
    $teamParams2 = [];
    $teamWhere2 = bot_scope_team_where($pdo, 'id', $teamParams2);
    $stTeam2 = $pdo->prepare('SELECT * FROM teams WHERE 1 = 1' . $teamWhere2 . ' ORDER BY name');
    $stTeam2->execute($teamParams2);
    $teams = $stTeam2->fetchAll();
    $tqRows = $pdo->query(
        'SELECT tq.team_id, tq.queue_address, q.name AS queue_name FROM team_queues tq
         LEFT JOIN queues q ON q.address = tq.queue_address ORDER BY tq.queue_address'
    )->fetchAll();
    $byTeamQ = [];
    foreach ($tqRows as $r) {
        $byTeamQ[(int) $r['team_id']][] = $r;
    }
    $trByTeam = [];
    foreach ($pdo->query('SELECT * FROM team_routing') as $r) {
        $trByTeam[(int) $r['team_id']] = $r;
    }
    $mrByTeam = [];
    foreach ($pdo->query('SELECT * FROM team_member_routing') as $r) {
        $mrByTeam[(int) $r['team_id']][(string) $r['address']] = $r;
    }
    $numOrNull = static function ($v): string {
        return $v === null || $v === '' ? '' : (string) (int) $v;
    };
    echo '<h2 class="h5 mt-4">Ajustes por time</h2>';
    if (!$teams) {
        echo '<p class="text-muted">Nenhum time ainda. Crie o plantão na aba <a href="?page=teams">Times</a>.</p>';
    }
    echo '<div class="accordion" id="routing-acc">';
    $firstTeam = true;
    foreach ($teams as $t) {
        $id = (int) $t['id'];
        $tr = $trByTeam[$id] ?? [];
        $show = $firstTeam ? ' show' : '';
        $collapsed = $firstTeam ? '' : ' collapsed';
        $firstTeam = false;
        $filas = $byTeamQ[$id] ?? [];
        echo '<div class="accordion-item" id="team-' . $id . '">';
        echo '<h3 class="accordion-header"><button class="accordion-button' . $collapsed . '" type="button" data-bs-toggle="collapse" data-bs-target="#rt-' . $id . '" aria-expanded="' . ($show ? 'true' : 'false') . '">';
        echo h($t['name']) . ' ' . badge_state(bool_on($t['active']));
        echo ' <span class="badge text-bg-light ms-2">' . count($filas) . ' filas</span>';
        if ((int) ($tr['form_enabled'] ?? 1) !== 1) {
            echo ' <span class="badge text-bg-warning ms-1">sem formulário</span>';
        }
        echo '</button></h3>';
        echo '<div id="rt-' . $id . '" class="accordion-collapse collapse' . $show . '" data-bs-parent="#routing-acc">';
        echo '<div class="accordion-body">';
        if ($filas) {
            $nomes = [];
            foreach ($filas as $f) {
                $nomes[] = '<code>' . h($f['queue_name'] ?: $f['queue_address']) . '</code>';
            }
            echo '<p class="small text-muted mb-2">Filas: ' . implode(', ', $nomes) . ' — <a href="?page=teams#team-' . $id . '">marcar/desmarcar na aba Times</a>.</p>';
        } else {
            echo '<p class="small text-muted mb-2">Sem filas: nenhum chat ou formulário vai para este time. <a href="?page=teams#team-' . $id . '">Marcar filas na aba Times</a>.</p>';
        }
        echo '<form method="post" class="row g-2 align-items-end"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
        echo '<input type="hidden" name="action" value="save_team_routing"><input type="hidden" name="team_id" value="' . $id . '">';
        echo '<div class="col-md-2"><label class="form-label">Estratégia</label><select class="form-select form-select-sm" name="strategy">';
        $stratOpts = ['' => 'herda global', 'round_robin' => 'rodízio', 'priority' => 'prioridade (peso)', 'least_load' => 'menor carga'];
        foreach ($stratOpts as $k => $l) {
            echo '<option value="' . h($k) . '"' . (($tr['strategy'] ?? '') === $k ? ' selected' : '') . '>' . h($l) . '</option>';
        }
        echo '</select></div>';
        echo '<div class="col-md-1"><label class="form-label">Cap chat</label><input class="form-control form-control-sm" type="number" min="0" name="cap_chat" value="' . h($numOrNull($tr['cap_chat'] ?? null)) . '" placeholder="' . h($map['max_open_chats_per_agent'] ?? '8') . '"></div>';
        echo '<div class="col-md-1"><label class="form-label">Cap form</label><input class="form-control form-control-sm" type="number" min="0" name="cap_form" value="' . h($numOrNull($tr['cap_form'] ?? null)) . '" placeholder="' . h($map['max_open_forms_per_agent'] ?? '8') . '"></div>';
        echo '<div class="col-md-1"><label class="form-label">Casos</label><input class="form-control form-control-sm" type="number" min="0" name="cap_cases" value="' . h($numOrNull($tr['cap_cases'] ?? null)) . '" placeholder="' . h($map['max_cases_per_agent'] ?? '4') . '" title="Casos simultaneos: formulario e chat do MESMO tecnico contam como um"></div>';
        echo '<div class="col-md-1"><label class="form-label">Tent.</label><input class="form-control form-control-sm" type="number" min="1" name="attempts" value="' . h($numOrNull($tr['attempts'] ?? null)) . '" placeholder="' . h($map['transfer_attempts'] ?? '5') . '"></div>';
        echo '<div class="col-md-1"><label class="form-label">Intervalo</label><input class="form-control form-control-sm" type="number" min="0" name="retry_interval_sec" value="' . h($numOrNull($tr['retry_interval_sec'] ?? null)) . '" placeholder="' . h($map['transfer_retry_interval_sec'] ?? '60') . '"></div>';
        echo '<div class="col-md-1"><label class="form-label">Online</label><select class="form-select form-select-sm" name="require_online">';
        foreach (['' => 'herda', '1' => 'exigir', '0' => 'ignorar'] as $k => $l) {
            echo '<option value="' . h((string) $k) . '"' . ((string) ($tr['require_online'] ?? '') === $k ? ' selected' : '') . '>' . h($l) . '</option>';
        }
        echo '</select></div>';
        echo '<div class="col-md-1"><label class="form-label">Plantão</label><select class="form-select form-select-sm" name="respect_window">';
        foreach (['' => 'herda', '1' => 'respeitar', '0' => 'ignorar'] as $k => $l) {
            echo '<option value="' . h((string) $k) . '"' . ((string) ($tr['respect_window'] ?? '') === $k ? ' selected' : '') . '>' . h($l) . '</option>';
        }
        echo '</select></div>';
        echo '<div class="col-md-1"><label class="form-label">Sem ninguém</label><select class="form-select form-select-sm" name="fallback">';
        $fbOpts = ['' => 'herda', 'retry' => 'repetir', 'monitor' => 'não insistir', 'close' => 'encerrar (chat)'];
        foreach ($fbOpts as $k => $l) {
            echo '<option value="' . h($k) . '"' . (($tr['fallback'] ?? '') === $k ? ' selected' : '') . '>' . h($l) . '</option>';
        }
        echo '</select></div>';
        echo '<div class="col-md-1"><label class="form-label">Formulário</label><select class="form-select form-select-sm" name="form_enabled">';
        foreach (['1' => 'recebe', '0' => 'não recebe'] as $k => $l) {
            echo '<option value="' . h((string) $k) . '"' . ((string) (int) ($tr['form_enabled'] ?? 1) === $k ? ' selected' : '') . '>' . h($l) . '</option>';
        }
        echo '</select></div>';
        echo '<div class="col-md-1"><label class="form-label">Offline</label><input class="form-control form-control-sm" type="number" min="0" name="offline_requeue_sec" value="' . h($numOrNull($tr['offline_requeue_sec'] ?? null)) . '" placeholder="' . h($map['offline_requeue_sec'] ?? '120') . '" title="Segundos offline antes de devolver o caso do operador para a fila (0 = este time nao devolve)"></div>';
        echo '<div class="col-md-1"><label class="form-label">Fim janela</label><input class="form-control form-control-sm" type="number" min="0" name="form_window_margin_min" value="' . h($numOrNull($tr['form_window_margin_min'] ?? null)) . '" placeholder="' . h($map['form_window_margin_min'] ?? '10') . '" title="Minutos antes do fim da janela do operador em que o formulário deixa de ser oferecido (vazio = herda o global; 0 = desligado)"></div>';
        echo '<div class="col-md-2"><label class="form-label">Padrão dos operadores</label><select class="form-select form-select-sm" name="member_kinds">';
        foreach (['both' => 'atende os dois', 'chat' => 'só chat', 'form' => 'só formulário'] as $k => $l) {
            $atualMK = (string) ($tr['member_kinds'] ?? 'both');
            if ($atualMK === '' || $atualMK === '0') {
                $atualMK = 'both';
            }
            echo '<option value="' . h($k) . '"' . ($atualMK === $k ? ' selected' : '') . '>' . h($l) . '</option>';
        }
        echo '</select></div>';
        echo '<div class="col-auto"><button class="btn btn-primary btn-sm">Salvar</button></div>';
        echo '<div class="col-12"><p class="text-muted small mb-0">Vazio = herda o global. <strong>Sem ninguém</strong>: no formulário, <em>repetir</em> tenta de novo a cada intervalo até o limite de tentativas e <em>não insistir</em> registra o evento e para; no chat, <em>encerrar</em> mantém o comportamento atual (bot sai) e <em>não insistir</em> deixa o chat em monitoramento. <strong>Casos</strong> conta solicitantes distintos: formulário e chat do <strong>mesmo autor</strong> são um caso só, e é esse número (não a soma de itens) que a escolha do analista usa.</p>';
        echo '<div class="form-check mt-2">';
        echo '<input class="form-check-input" type="checkbox" name="cases_all_members" id="casesAllMembers' . $id . '" value="1">';
        echo '<label class="form-check-label small" for="casesAllMembers' . $id . '">Replicar o <strong>Casos</strong> deste time para <strong>todos os operadores</strong> (limpa o cap de casos individual)</label></div>';
        echo '<div class="form-check">';
        echo '<input class="form-check-input" type="checkbox" name="kinds_all_members" id="kindsAllMembers' . $id . '" value="1">';
        echo '<label class="form-check-label small" for="kindsAllMembers' . $id . '">Replicar o <strong>Padrão dos operadores</strong> para todos (limpa o tipo individual)</label></div>';
        echo '</div>';
        echo '</form>';
        echo '<form method="post" class="mt-2"><input type="hidden" name="csrf" value="' . h($csrf) . '">';
        echo '<input type="hidden" name="action" value="reset_team_routing"><input type="hidden" name="team_id" value="' . $id . '">';
        echo '<button class="btn btn-outline-secondary btn-sm" onclick="return confirm(\'Limpar os ajustes deste time e voltar a herdar o global?\')">Limpar ajustes do time</button></form>';
        $stM = $pdo->prepare('SELECT * FROM team_members WHERE team_id = ? ORDER BY sort_order, name, address');
        $stM->execute([$id]);
        $mRows = $stM->fetchAll();
        $ultC = trim((string) ($tr['last_assigned_chat'] ?? ($t['last_assigned'] ?? '')));
        $ultF = trim((string) ($tr['last_assigned_form'] ?? ''));
        echo '<p class="small text-muted mb-2">Última atribuição — chat: <code>' . h($ultC !== '' ? $ultC : '—') . '</code>; formulário: <code>' . h($ultF !== '' ? $ultF : '—') . '</code>.</p>';
        echo '<h4 class="h6 mt-3">Membros e prioridade</h4>';
        if (!$mRows) {
            echo '<p class="text-muted small">Sem membros. Adicione na aba <a href="?page=teams#team-' . $id . '">Times</a>.</p>';
        } else {
            echo '<div class="table-responsive"><table class="table table-sm table-hover align-middle bg-white">';
            echo '<thead><tr><th>Ordem</th><th>Nome</th><th>Peso</th><th>Cap chat</th><th>Cap form</th><th>Casos</th><th>Atende</th><th>Plantão agora</th><th></th></tr></thead><tbody>';
            foreach ($mRows as $mem) {
                $addr = (string) $mem['address'];
                $mr = $mrByTeam[$id][$addr] ?? [];
                $naJanela = member_in_route_window($mem['route_start'] ?? null, $mem['route_end'] ?? null);
                echo '<form method="post" class="d-contents">';
                echo '<tr>';
                echo '<td>' . (int) $mem['sort_order'] . '<input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="save_member_routing"><input type="hidden" name="team_id" value="' . $id . '"><input type="hidden" name="address" value="' . h($addr) . '"></td>';
                echo '<td>' . h($mem['name'] ?: $addr) . ' ' . badge_state(bool_on($mem['active'])) . '<br><code class="small">' . h($addr) . '</code></td>';
                echo '<td style="width:6rem"><input class="form-control form-control-sm" type="number" name="weight" value="' . h((string) (int) ($mr['weight'] ?? 0)) . '" title="Maior peso é titular na estratégia Prioridade"></td>';
                echo '<td style="width:6rem"><input class="form-control form-control-sm" type="number" min="0" name="cap_chat" value="' . h($numOrNull($mr['cap_chat'] ?? null)) . '" placeholder="' . h((string) ($tr['cap_chat'] ?? ($map['max_open_chats_per_agent'] ?? '8'))) . '"></td>';
                echo '<td style="width:6rem"><input class="form-control form-control-sm" type="number" min="0" name="cap_form" value="' . h($numOrNull($mr['cap_form'] ?? null)) . '" placeholder="' . h((string) ($tr['cap_form'] ?? ($map['max_open_forms_per_agent'] ?? '8'))) . '"></td>';
                echo '<td style="width:6rem"><input class="form-control form-control-sm" type="number" min="0" name="cap_cases" value="' . h($numOrNull($mr['cap_cases'] ?? null)) . '" placeholder="' . h((string) ($tr['cap_cases'] ?? ($map['max_cases_per_agent'] ?? '4'))) . '"></td>';
                $kindsAtual = (string) ($mr['kinds'] ?? '');
                if (!in_array($kindsAtual, ['both', 'chat', 'form'], true)) {
                    $kindsAtual = ((string) (int) ($mr['forms_enabled'] ?? 1)) === '0' ? 'chat' : 'both';
                }
                echo '<td style="width:9rem"><select class="form-select form-select-sm" name="kinds">';
                foreach (['both' => 'ambos', 'form' => 'formulário', 'chat' => 'só chat'] as $k => $l) {
                    echo '<option value="' . h($k) . '"' . ($kindsAtual === $k ? ' selected' : '') . '>' . h($l) . '</option>';
                }
                echo '</select></td>';
                echo '<td>' . badge_state($naJanela, 'na janela', 'fora') . '</td>';
                echo '<td class="text-nowrap"><button class="btn btn-primary btn-sm me-1">Salvar</button>';
                echo '<button class="btn btn-outline-secondary btn-sm" name="action" value="reset_member_routing" onclick="return confirm(\'Limpar peso/cap deste membro e voltar a herdar?\')">Limpar</button></td>';
                echo '</tr></form>';
            }
            echo '</tbody></table></div>';
            $naJanelaN = 0;
            $ativosN = 0;
            foreach ($mRows as $mem) {
                if (!bool_on($mem['active'])) {
                    continue;
                }
                $ativosN++;
                if (member_in_route_window($mem['route_start'] ?? null, $mem['route_end'] ?? null)) {
                    $naJanelaN++;
                }
            }
            echo '<p class="small text-muted mb-0">Agora: <strong>' . $naJanelaN . '</strong> de ' . count($mRows) . ' membro(s) dentro da janela de plantão, ' . $ativosN . ' ativo(s). A escolha final ainda considera online no TOA, cap e carga do momento.</p>';
        }
        echo '</div></div></div>';
    }
    echo '</div>';
    echo '<div class="card mt-3"><div class="card-body">';
    echo '<h2 class="h6">Como a escolha é feita (ordem exata)</h2>';
    echo '<ol class="small mb-2">';
    echo '<li><strong>Time da fila</strong> — 1 fila pertence a 1 time (aba Times). O formulário de validação usa o mesmo time da fila dele e não é um chat de menu.</li>';
    echo '<li><strong>Membros ativos</strong>, na ordem definida pelo campo <em>Ordem</em> na aba Times.</li>';
    echo '<li><strong>Janela de plantão</strong> do membro (horário de Brasília). Vazio ou início = fim valem 24h; 22:00–06:00 atravessa a meia-noite.</li>';
    echo '<li><strong>Online no TOA</strong> quando <em>Online</em> = exigir. É consultado a cada transferência.</li>';
    echo '<li><strong>Cap</strong> (membro → time → global) contando chats/formulários em que o analista já está.</li>';
    echo '<li><strong>Estratégia</strong>: <em>rodízio</em> pega o próximo depois do último escolhido; <em>prioridade</em> pega o maior peso (no empate, o rodízio, e se o titular estiver no cap cai para o próximo); <em>menor carga</em> pega quem tem menos atendimentos, desempatando por peso e rodízio.</li>';
    echo '</ol>';
    echo '<p class="small text-muted mb-2">No chat, o bot espera o online até o limite de tentativas e então aplica o fallback. No formulário, cada tentativa é agendada em segundo plano (intervalo do formulário), então o atendimento não fica esperando; ao esgotar as tentativas, fica registrado no log. O bot nunca entra nem encerra o formulário.</p>';
    echo '<p class="small text-muted mb-0">Eventos gerados na aba <a href="?page=activity">Atividade</a>: <code>form_routed</code> (entregue), <code>form_no_agent</code> (sem analista), <code>form_route_retry</code> (nova tentativa), <code>form_no_team</code> (fila sem time ou time sem formulário), <code>form_no_queue</code> (formulário sem fila) e <code>closure</code> (formulário recebido).</p>';
    echo '</div></div>';
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
    echo '<div class="col-md-6"><label class="form-label">WebSocket mudo para reciclar Chromium (s)</label><input class="form-control" name="ws_stale_sec" value="' . h($map['ws_stale_sec'] ?? '180') . '"></div>';
    echo '<div class="col-md-6"><label class="form-label">Bot ligado</label><select class="form-select" name="bot_enabled">';
    echo '<option value="true"' . (($map['bot_enabled'] ?? '') === 'true' ? ' selected' : '') . '>Ligado</option>';
    echo '<option value="false"' . (($map['bot_enabled'] ?? '') === 'false' ? ' selected' : '') . '>Desligado</option></select></div>';
    echo '</div><button class="btn btn-primary mt-3">Salvar</button></form>';
    echo '<p class="text-muted small mb-0 mt-3">Cap por analista, tentativas de transferência, estratégia e roteamento de formulário ficam na aba <a href="?page=routing">Roteamento</a>.</p>';
    echo '</div></div>';
}

if ($page === 'activity') {
    $labels = [
        'bot_off' => 'bot desligado',
        'queue_off' => 'fila desabilitada no painel',
        'queue_unknown' => 'sem fila',
        'not_new' => 'já atendido',
        'closure' => 'formulário recebido',
        'form_routed' => 'formulário roteado',
        'form_no_agent' => 'formulário sem analista',
        'form_route_retry' => 'nova tentativa do formulário',
        'form_no_team' => 'formulário sem time',
        'form_no_queue' => 'formulário sem fila',
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
        'closed_no_agent' => 'encerrou (sem analista)',
        'transferred' => 'transferiu',
        'transferred_partial' => 'transferiu (convite pendente)',
        'ask_collected' => 'perguntas confirmadas',
        'route_retry' => 'nova tentativa de transferência',
        'no_agent' => 'sem analista',
        'queued' => 'entrou na fila',
        'queue_position' => 'mudou de posição',
        'queued_assigned' => 'saiu da fila (atribuído)',
        'queue_expired' => 'espera expirou',
        'queue_no_menu' => 'fila habilitada sem menu ativo',
        'queue_no_team' => 'fila habilitada sem time ativo',
        'form_queue_off' => 'formulário com a fila desabilitada (rota encerrada)',
        'self_target_blocked' => 'entrega cancelada: o alvo era o próprio bot',
        'case_reclaimed' => 'caso devolvido à fila (analista offline)',
        'chat_adopted' => 'analista entrou no chat da fila (caso adotado)',
        'reclaim_invite_only' => 'resgate: convite sem novo move',
        'reclaim_no_queue' => 'resgate: caso sem fila para devolver',
        'reclaim_empty' => '(removida) resgate: nenhum caso de chat para devolver',
        'form_aware_greeting' => '(removida) abertura: formulário em andamento',
        'form_aware_choice' => '(removida) abertura: escolha do técnico',
        'form_aware_wait' => '(removida) abertura: aguardava o analista do formulário',
        'form_aware_skip' => '(removida) abertura: sem formulário para vincular',
        'case_linked' => '(removida) chat e formulário no mesmo caso',
        'case_sibling_delivered' => 'caso entregue junto (chat + formulário)',
        'form_indexed' => 'formulário em acompanhamento',
        'form_not_open' => 'formulário com fim explícito (não indexado)',
        'form_dropped' => 'formulário saiu do acompanhamento',
        'form_ignored_payload' => 'payload sem sinal de formulário (índice mantido)',
        'form_queue_left' => 'formulário saiu da fila do helpdesk (segue aberto)',
        'form_forget' => 'formulário removido do acompanhamento',
        'case_key_divergente' => 'caso: chave da rota ignorada (identidade manda)',
        'reply_replayed' => 'resposta recuperada (restart)',
        'send_failed' => 'falha ao enviar mensagem',
        'left' => 'saiu',
        'resume_stale' => 'chat sumiu no resume',
    ];
    $rawReasons = $_GET['reason'] ?? [];
    if (!is_array($rawReasons)) {
        $rawReasons = [$rawReasons];
    }
    $filterReasons = [];
    foreach ($rawReasons as $rrOne) {
        $rrOne = preg_replace('/[^a-z_]/', '', (string) $rrOne);
        if ($rrOne !== '') {
            $filterReasons[$rrOne] = true;
        }
    }
    $filterQueues = [];
    foreach ((array) ($_GET['filas'] ?? []) as $fqOne) {
        $fqOne = trim((string) $fqOne);
        if (substr($fqOne, 0, 2) === 'h:' && bot_queue_in_scope($pdo, $fqOne)) {
            $filterQueues[$fqOne] = true;
        }
    }
    $deRaw = trim((string) ($_GET['de'] ?? ''));
    $ateRaw = trim((string) ($_GET['ate'] ?? ''));
    [$deDate, $ateDate, $periodErr] = ui_parse_period($deRaw, $ateRaw);
    $actQParams = [];
    $actQWhere = bot_scope_queue_where($pdo, 'address', $actQParams);
    $stActQ = $pdo->prepare('SELECT address, name FROM queues WHERE 1 = 1' . $actQWhere . ' ORDER BY name, address');
    $stActQ->execute($actQParams);
    $actQueues = $stActQ->fetchAll();
    $actQueueOpts = [];
    foreach ($actQueues as $actQ) {
        $actQueueOpts[(string) $actQ['address']] = ($actQ['name'] !== '' ? $actQ['name'] . ' - ' : '') . (string) $actQ['address'];
    }
    page_header('Atividade', 'Log do bot sem texto de conversa. Filtre por evento, fila e periodo.', '');
    echo '<div class="d-none">';
    echo '<h1 class="h3 mb-1">Atividade</h1>';
    echo '<p class="text-muted">Log do bot' . ($autoRefresh ? ' (atualiza a cada 5s)' : ' (atualização automática pausada nesta página ou filtro)') . '. Sem texto de conversa.</p></div>';
    if ($periodErr !== null) {
        echo '<div class="alert alert-danger py-2">' . h($periodErr) . '</div>';
    }
    echo '<form method="get" class="row g-2 align-items-end mb-3">';
    echo '<input type="hidden" name="page" value="activity">';
    echo '<div class="col-12 col-md-3"><label class="form-label">Evento</label>';
    ui_multiselect('reason', $labels, array_keys($filterReasons), 'Todos os eventos');
    echo '</div>';
    echo '<div class="col-12 col-md-3"><label class="form-label">Fila</label>';
    ui_multiselect('filas', $actQueueOpts, array_keys($filterQueues), 'Todas as filas');
    echo '</div>';
    ui_date_range($deRaw, $ateRaw);
    echo '<div class="col-12 col-md-auto align-self-end"><button class="btn btn-primary btn-sm">Filtrar</button> <a class="btn btn-outline-secondary btn-sm" href="?page=activity">Limpar</a></div>';
    echo '</form>';
    $per = 50;
    $total = 0;
    $rows = [];
    try {
        $conds = [];
        $params = [];
        if ($filterReasons) {
            $conds[] = 'e.reason IN (' . implode(',', array_fill(0, count($filterReasons), '?')) . ')';
            foreach (array_keys($filterReasons) as $rrKey) {
                $params[] = $rrKey;
            }
        }
        if ($filterQueues) {
            $conds[] = 'e.queue_address IN (' . implode(',', array_fill(0, count($filterQueues), '?')) . ')';
            foreach (array_keys($filterQueues) as $fqKey) {
                $params[] = $fqKey;
            }
        }
        $actScope = bot_scope_queue_where($pdo, 'e.queue_address', $params);
        if ($actScope !== '') {
            $conds[] = substr($actScope, 5);
        }
        if ($deDate !== null) {
            $conds[] = 'date(datetime(e.at, ?)) >= date(?)';
            $params[] = '-3 hours';
            $params[] = $deDate->format('Y-m-d');
        }
        if ($ateDate !== null) {
            $conds[] = 'date(datetime(e.at, ?)) <= date(?)';
            $params[] = '-3 hours';
            $params[] = $ateDate->format('Y-m-d');
        }
        $where = $conds ? 'WHERE ' . implode(' AND ', $conds) : '';
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
    echo '<div class="table-responsive sticky-head"><table class="table table-slim table-hover align-middle bg-white mb-0">';
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
    $actPg = [];
    if ($filterReasons) {
        $actPg['reason'] = array_keys($filterReasons);
    }
    if ($filterQueues) {
        $actPg['filas'] = array_keys($filterQueues);
    }
    if ($deRaw !== '') {
        $actPg['de'] = $deRaw;
    }
    if ($ateRaw !== '') {
        $actPg['ate'] = $ateRaw;
    }
    echo_pagination('activity', $listPage, $total, $actPg, $per);
    if (!$rows) {
        echo '<p class="text-muted">Nenhum evento ainda. Deixe o run.py ligado e abra um chat numa fila.</p>';
    }
}

if ($page === 'chats') {
    $filterStatuses = [];
    foreach ((array) ($_GET['status'] ?? []) as $stOne) {
        $stOne = preg_replace('/[^a-z_]/', '', (string) $stOne);
        if ($stOne !== '') {
            $filterStatuses[$stOne] = true;
        }
    }
    $chatQueues = [];
    foreach ((array) ($_GET['filas'] ?? []) as $cqOne) {
        $cqOne = trim((string) $cqOne);
        if (substr($cqOne, 0, 2) === 'h:' && bot_queue_in_scope($pdo, $cqOne)) {
            $chatQueues[$cqOne] = true;
        }
    }
    $chDeRaw = trim((string) ($_GET['de'] ?? ''));
    $chAteRaw = trim((string) ($_GET['ate'] ?? ''));
    [$chDe, $chAte, $chPeriodErr] = ui_parse_period($chDeRaw, $chAteRaw);
    $chatQParams = [];
    $chatQWhere = bot_scope_queue_where($pdo, 'address', $chatQParams);
    $stChatQ = $pdo->prepare('SELECT address, name FROM queues WHERE 1 = 1' . $chatQWhere . ' ORDER BY name, address');
    $stChatQ->execute($chatQParams);
    $chatQueueOpts = [];
    foreach ($stChatQ->fetchAll() as $chatQ) {
        $chatQueueOpts[(string) $chatQ['address']] = ($chatQ['name'] !== '' ? $chatQ['name'] . ' - ' : '') . (string) $chatQ['address'];
    }
    page_header('Chats', 'Apenas id e estado do chat; nenhum texto de conversa.', '');
    echo '<div class="d-none">';
    echo '<h1 class="h3 mb-1">Chats</h1>';
    echo '<p class="text-muted">Só id e estado — sem texto de conversa.</p></div>';
    $statuses = [];
    try {
        $statuses = $pdo->query('SELECT DISTINCT status FROM chats ORDER BY status')->fetchAll(PDO::FETCH_COLUMN);
    } catch (Throwable $e) {
        $statuses = [];
    }
    $statusOpts = [];
    foreach ($statuses as $st) {
        $statusOpts[(string) $st] = (string) $st;
    }
    if ($chPeriodErr !== null) {
        echo '<div class="alert alert-danger py-2">' . h($chPeriodErr) . '</div>';
    }
    echo '<form method="get" class="row g-2 align-items-end mb-3">';
    echo '<input type="hidden" name="page" value="chats">';
    echo '<div class="col-12 col-md-3"><label class="form-label">Status</label>';
    ui_multiselect('status', $statusOpts, array_keys($filterStatuses), 'Todos os status');
    echo '</div>';
    echo '<div class="col-12 col-md-3"><label class="form-label">Fila</label>';
    ui_multiselect('filas', $chatQueueOpts, array_keys($chatQueues), 'Todas as filas');
    echo '</div>';
    ui_date_range($chDeRaw, $chAteRaw);
    echo '<div class="col-12 col-md-auto align-self-end"><button class="btn btn-primary btn-sm">Filtrar</button> <a class="btn btn-outline-secondary btn-sm" href="?page=chats">Limpar</a></div>';
    echo '</form>';
    $per = 50;
    $total = 0;
    $rows = [];
    $conds = [];
    $params = [];
    if ($filterStatuses) {
        $conds[] = 'status IN (' . implode(',', array_fill(0, count($filterStatuses), '?')) . ')';
        foreach (array_keys($filterStatuses) as $stKey) {
            $params[] = $stKey;
        }
    }
    if ($chatQueues) {
        $conds[] = 'queue_address IN (' . implode(',', array_fill(0, count($chatQueues), '?')) . ')';
        foreach (array_keys($chatQueues) as $cqKey) {
            $params[] = $cqKey;
        }
    }
    $chatScope = bot_scope_queue_where($pdo, 'queue_address', $params);
    if ($chatScope !== '') {
        $conds[] = substr($chatScope, 5);
    }
    if ($chDe !== null) {
        $conds[] = 'date(datetime(updated_at, ?)) >= date(?)';
        $params[] = '-3 hours';
        $params[] = $chDe->format('Y-m-d');
    }
    if ($chAte !== null) {
        $conds[] = 'date(datetime(updated_at, ?)) <= date(?)';
        $params[] = '-3 hours';
        $params[] = $chAte->format('Y-m-d');
    }
    $where = $conds ? 'WHERE ' . implode(' AND ', $conds) : '';
    $st = $pdo->prepare("SELECT COUNT(*) FROM chats $where");
    $st->execute($params);
    $total = (int) $st->fetchColumn();
    $pages = max(1, (int) ceil($total / $per));
    if ($listPage > $pages) {
        $listPage = $pages;
    }
    $offset = ($listPage - 1) * $per;
    $st = $pdo->prepare(
        "SELECT chat_id, queue_address, status, classification, retries, auto_close, updated_at,
                ask_option_id, script_answers
         FROM chats $where
         ORDER BY updated_at DESC
         LIMIT $per OFFSET $offset"
    );
    $st->execute($params);
    $rows = $st->fetchAll();
    $qMap = [];
    foreach ($pdo->query('SELECT option_id, prompt FROM menu_option_questions ORDER BY option_id, sort_order') as $qr) {
        $qMap[(int) $qr['option_id']][] = (string) $qr['prompt'];
    }
    echo '<div class="table-responsive sticky-head"><table class="table table-slim table-hover align-middle bg-white mb-0">';
    echo '<thead><tr><th>chat_id</th><th>Fila</th><th>Status</th><th>Classificação</th><th>Retries</th><th>Auto close</th><th>Atualizado</th><th>Respostas</th></tr></thead><tbody>';
    foreach ($rows as $r) {
        echo '<tr><td>' . h((string) $r['chat_id']) . '</td><td>' . h($r['queue_address']) . '</td><td>' . status_badge((string) $r['status']) . '</td>';
        echo '<td>' . h($r['classification']) . '</td><td>' . (int) $r['retries'] . '</td><td>' . badge_state(bool_on($r['auto_close']), 'sim', 'não') . '</td>';
        echo '<td>' . h(fmt_dt($r['updated_at'])) . '</td>';
        $ansJson = json_decode((string) ($r['script_answers'] ?? '{}'), true);
        if (!is_array($ansJson) || !$ansJson) {
            echo '<td><span class="text-muted">-</span></td></tr>';
        } else {
            $promptsAns = $qMap[(int) ($r['ask_option_id'] ?? 0)] ?? [];
            $cidAns = (int) $r['chat_id'];
            echo '<td><button class="btn btn-outline-secondary btn-sm" type="button" data-bs-toggle="collapse" data-bs-target="#ans-' . $cidAns . '">Ver</button>';
            echo '<div class="collapse mt-1" id="ans-' . $cidAns . '"><div class="small">';
            foreach ($ansJson as $kAns => $vAns) {
                $idxAns = (int) substr((string) $kAns, 1);
                $labAns = $promptsAns[$idxAns - 1] ?? (string) $kAns;
                echo '<div><span class="text-muted">' . h((string) $labAns) . ':</span> ' . h((string) $vAns) . '</div>';
            }
            echo '</div></div></td></tr>';
        }
    }
    echo '</tbody></table></div>';
    $chatPg = [];
    if ($filterStatuses) {
        $chatPg['status'] = array_keys($filterStatuses);
    }
    if ($chatQueues) {
        $chatPg['filas'] = array_keys($chatQueues);
    }
    if ($chDeRaw !== '') {
        $chatPg['de'] = $chDeRaw;
    }
    if ($chAteRaw !== '') {
        $chatPg['ate'] = $chAteRaw;
    }
    echo_pagination('chats', $listPage, $total, $chatPg, $per);
}

if ($page === 'load') {
    $loadPoll = 30;
    $loadAbsent = 2;
    $loadOfflineSec = 120;
    try {
        foreach ($pdo->query('SELECT key, value FROM bot_settings') as $rCfg) {
            $kCfg = (string) $rCfg['key'];
            if ($kCfg === 'load_poll_sec') {
                $loadPoll = max(5, (int) $rCfg['value']);
            } elseif ($kCfg === 'form_absent_readings') {
                $loadAbsent = max(1, (int) $rCfg['value']);
            } elseif ($kCfg === 'offline_requeue_sec') {
                $loadOfflineSec = max(0, (int) $rCfg['value']);
            }
        }
    } catch (Throwable $e) {
        $loadPoll = 30;
    }
    // ---- Filtro de time (multiplo) da aba --------------------------------
    // Vale para os cards, a tabela e o alerta de divergentes. O estado fica na
    // sessao (a pagina tem auto-refresh de 30 s, entao so a URL nao bastaria) e a
    // selecao e sempre limitada ao escopo de times do usuario.
    $ltParams = [];
    $ltWhere = bot_scope_team_where($pdo, 'id', $ltParams);
    $loadTeams = [];
    try {
        $stLt = $pdo->prepare('SELECT id, name FROM teams WHERE 1 = 1' . $ltWhere . ' ORDER BY name');
        $stLt->execute($ltParams);
        foreach ($stLt->fetchAll() as $lt) {
            $loadTeams[(int) $lt['id']] = (string) $lt['name'];
        }
    } catch (Throwable $e) {
        $loadTeams = [];
    }
    $selTimes = [];
    $temFiltro = array_key_exists('teams', $_GET);
    foreach ((array) ($temFiltro ? $_GET['teams'] : ($_SESSION['load_teams'] ?? [])) as $tv) {
        $tv = (int) $tv;
        if (isset($loadTeams[$tv])) {
            $selTimes[$tv] = true;
        }
    }
    $filtroTimes = array_keys($selTimes);
    if ($temFiltro) {
        $_SESSION['load_teams'] = $filtroTimes;
    }
    // Fragmento por COLUNA de time (rotas e ledger).
    $filtroTimeSql = static function (string $col) use ($filtroTimes): string {
        if (!$filtroTimes) {
            return '';
        }
        return ' AND ' . $col . ' IN (' . implode(',', array_fill(0, count($filtroTimes), '?')) . ')';
    };
    // Fragmento por FILA (o chat nao tem team_id: filtra pelas filas do time).
    $filtroFilaSql = static function (string $col) use ($filtroTimes): string {
        if (!$filtroTimes) {
            return '';
        }
        $marc = implode(',', array_fill(0, count($filtroTimes), '?'));
        return ' AND ' . $col . ' IN (SELECT queue_address FROM team_queues WHERE team_id IN (' . $marc . '))';
    };
    $loadParams = [];
    $loadWhere = bot_scope_team_where($pdo, 'l.team_id', $loadParams);
    $loadRows = [];
    $loadErro = '';
    try {
        // ATIVO no roteamento primeiro (maior carga fisica no topo); inativo no
        // time (ou removido dele) vai para o fim da lista, com selo na linha.
        $stLoad = $pdo->prepare(
            'SELECT l.*, t.name AS team_name, COALESCE(tm.active, 0) AS roteando'
            . ' FROM analyst_load l'
            . ' LEFT JOIN teams t ON t.id = l.team_id'
            . ' LEFT JOIN team_members tm ON tm.team_id = l.team_id AND tm.address = l.address'
            . ' WHERE 1 = 1' . $loadWhere . $filtroTimeSql('l.team_id')
            . ' ORDER BY (CASE WHEN COALESCE(tm.active, 0) = 1 THEN 0 ELSE 1 END),'
            . ' (l.chats_toa + l.forms_toa) DESC, l.form_ocioso_min DESC, l.name'
        );
        $stLoad->execute(array_merge($loadParams, $filtroTimes));
        $loadRows = $stLoad->fetchAll();
    } catch (Throwable $e) {
        $loadErro = 'Tabela analyst_load ausente. Reinicie o bot (ou rode o bot no painel) para criar.';
    }
    // Cards: chatbot (chats com o bot), filas e o que esta em atendimento (ledger).
    $botAtende = 0;
    $filaChat = 0;
    $filaForm = 0;
    $chatAtende = 0;
    $formAtende = 0;
    $statusBot = bot_open_chat_statuses();
    try {
        $botParams = [];
        $stBot = $pdo->prepare(
            'SELECT COUNT(*) FROM chats c WHERE c.moved_at IS NULL'
            . ' AND c.status IN (' . implode(',', array_fill(0, count($statusBot), '?')) . ')'
            . bot_scope_queue_where($pdo, 'c.queue_address', $botParams)
            . $filtroFilaSql('c.queue_address')
        );
        $stBot->execute(array_merge($statusBot, $botParams, $filtroTimes));
        $botAtende = (int) $stBot->fetchColumn();
    } catch (Throwable $e) {
        $botAtende = 0;
    }
    try {
        $filaParams = [];
        $stFila = $pdo->prepare(
            "SELECT kind, COUNT(*) AS n FROM pending_routes r"
            . " WHERE r.status = 'waiting'" . bot_scope_team_where($pdo, 'r.team_id', $filaParams)
            . $filtroTimeSql('r.team_id') . ' GROUP BY kind'
        );
        $stFila->execute(array_merge($filaParams, $filtroTimes));
        foreach ($stFila->fetchAll() as $rFila) {
            if ((string) $rFila['kind'] === 'form') {
                $filaForm = (int) $rFila['n'];
            } else {
                $filaChat = (int) $rFila['n'];
            }
        }
    } catch (Throwable $e) {
    }
    try {
        $capParams = [];
        $capWhere = bot_scope_team_where($pdo, 'team_id', $capParams);
        $stCap = $pdo->prepare(
            'SELECT kind, COUNT(*) AS n FROM route_assignments WHERE closed_at IS NULL'
            . $capWhere . $filtroTimeSql('team_id') . ' GROUP BY kind'
        );
        $stCap->execute(array_merge($capParams, $filtroTimes));
        foreach ($stCap->fetchAll() as $rCap) {
            if ((string) $rCap['kind'] === 'form') {
                $formAtende = (int) $rCap['n'];
            } else {
                $chatAtende = (int) $rCap['n'];
            }
        }
    } catch (Throwable $e) {
    }
    $ultimaLeitura = '';
    foreach ($loadRows as $rUlt) {
        if ((string) ($rUlt['at'] ?? '') > $ultimaLeitura) {
            $ultimaLeitura = (string) $rUlt['at'];
        }
    }
    $loadBtn = bot_can($pdo, 'load.manage')
        ? '<form method="post" class="d-inline"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="load_now"><button class="btn btn-primary btn-sm">Atualizar agora</button></form>'
        : '';
    page_header(
        'Carga',
        'Leitura das listas do TOA por analista (chats x formularios) e o que o bot contabiliza.',
        $loadBtn
    );
    echo '<div class="d-none">';
    echo '<h1 class="h3 mb-0">Carga</h1>';
    echo '<form method="post" class="d-inline"><input type="hidden" name="csrf" value="' . h($csrf) . '"><input type="hidden" name="action" value="load_now">';
    echo '<button class="btn btn-primary">Atualizar (reler listas do TOA)</button></form></div>';
    if (($_GET['synced'] ?? '') === '1') {
        echo '<div class="alert alert-success py-2 mt-2 mb-2">Leitura solicitada — o bot relê as listas do TOA em até ~5 s.</div>';
    }
    if ($loadErro !== '') {
        echo '<div class="alert alert-danger py-2 mb-2">' . h($loadErro) . '</div>';
    }
    echo '<div class="card mb-3"><div class="card-body py-2">';
    echo '<form method="get" class="row g-2 align-items-end">';
    echo '<input type="hidden" name="page" value="load">';
    echo '<div class="col-md-5 col-xl-4"><label class="form-label mb-1">Times (vale para os cards e a tabela)</label>';
    echo '<select class="form-select form-select-sm" name="teams[]" multiple size="6">';
    foreach ($loadTeams as $ltId => $ltNome) {
        echo '<option value="' . (int) $ltId . '"' . (isset($selTimes[$ltId]) ? ' selected' : '') . '>' . h($ltNome) . '</option>';
    }
    echo '</select></div>';
    echo '<div class="col-auto"><button class="btn btn-primary btn-sm">Filtrar</button> ';
    echo '<a class="btn btn-outline-secondary btn-sm" href="?page=load&amp;teams=">Limpar</a></div>';
    echo '<div class="col-12"><div class="form-text mb-0">Sem seleção = todos os times do seu acesso. '
        . 'A escolha fica gravada na sessão e vale para os cards, a tabela e os alertas.</div></div>';
    echo '</form></div></div>';
    echo '<div class="row g-3 mb-3">';
    $cards = [
        ['ChatBot — Em atendimento', (string) $botAtende, 'bg-dark'],
        ['Fila de chat', (string) $filaChat, 'bg-warning text-dark'],
        ['Chat em atendimento', (string) $chatAtende, 'bg-secondary'],
        ['Fila de formulário', (string) $filaForm, 'bg-primary'],
        ['Formulário em atendimento', (string) $formAtende, 'bg-info text-dark'],
        ['Última leitura', $ultimaLeitura !== '' ? fmt_dt($ultimaLeitura) : '—', 'bg-light text-dark'],
    ];
    foreach ($cards as $card) {
        echo '<div class="col-6 col-md-4 col-xl-2"><div class="card h-100"><div class="card-body py-2">';
        echo '<div class="text-muted small">' . h($card[0]) . '</div>';
        echo '<div class="fs-4"><span class="badge ' . h($card[2]) . '">' . h($card[1]) . '</span></div>';
        echo '</div></div></div>';
    }
    echo '</div>';
    echo '<p class="text-muted small">Leitura automática a cada <strong>' . (int) $loadPoll . ' s</strong>. '
        . 'Item ausente nas listas do TOA por <strong>' . (int) $loadAbsent . '</strong> leitura(s) com leitura bem sucedida é dado como resolvido '
        . '(<code>form_resolvido_toa</code> para o formulário, <code>chat_resolvido_toa</code> para o chat): o caso fecha, a vaga do analista libera e as rotas são canceladas. '
        . 'Na tabela, <strong>Casos</strong> mostra o <em>físico</em> (TOA chat + TOA form, o que o operador tem aberto no front) e, em cinza, o número de casos '
        . '(solicitantes distintos — form + chat do mesmo autor = 1 caso), que é o número do cap; as reservas em voo somam por cima. '
        . 'Quem está <strong>inativo</strong> no roteamento aparece no fim da lista.</p>';
    $divergentes = 0;
    foreach ($loadRows as $rDiv) {
        if ((int) $rDiv['forms_ledger'] > 0 && !(int) $rDiv['form_presente_toa']) {
            $divergentes++;
        }
    }
    // Analista OFFLINE com caso aberto no ledger: e o que o robo devolve para a
    // fila depois de `offline_requeue_sec` (evento `case_reclaimed`).
    $offlineComCaso = 0;
    $chatsOffline = 0;
    $formsOffline = 0;
    $offlineSemLedger = 0;
    foreach ($loadRows as $rOff) {
        $semOnline = (int) ($rOff['online_ok'] ?? 0) === 1 && (int) $rOff['online'] === 0;
        if (!$semOnline) {
            continue;
        }
        $casosOff = (int) $rOff['chats_ledger'] + (int) $rOff['forms_ledger'];
        if ($casosOff > 0) {
            $offlineComCaso++;
            $chatsOffline += (int) $rOff['chats_ledger'];
            $formsOffline += (int) $rOff['forms_ledger'];
        } else {
            $offlineSemLedger++;
        }
    }
    if (!$loadRows) {
        echo '<p class="text-muted">Nenhuma leitura ainda. Confira se ha time ativo com membros e filas; a primeira leitura sai em ate '
            . (int) $loadPoll . ' s (ou clique em <strong>Atualizar agora</strong>).</p>';
    } else {
        if ($divergentes > 0) {
            echo '<div class="alert alert-warning py-2">' . (int) $divergentes . ' analista(s) com formulario no bot e <strong>ausente das listas do TOA</strong> — devem ser fechados por ausencia na proxima leitura.</div>';
        }
        if ($offlineComCaso > 0) {
            echo '<div class="alert alert-warning py-2">' . (int) $offlineComCaso . ' analista(s) <strong>offline</strong> com caso aberto no bot (chats '
                . (int) $chatsOffline . ', formulários ' . (int) $formsOffline . ') — o robô devolve para a fila depois de <strong>'
                . (int) $loadOfflineSec . ' s</strong> (' . ($loadOfflineSec > 0 ? 'evento <code>case_reclaimed</code>' : 'resgate desligado')
                . '). A devolução é automática pelo robô.</div>';
        }
        echo '<div class="table-responsive sticky-head"><table class="table table-slim table-hover align-middle bg-white mb-0">';
        echo '<thead><tr><th>Analista</th><th>Time</th><th>Online</th><th>Offline há</th><th class="text-end">TOA chat</th><th class="text-end">TOA form</th>'
            . '<th class="text-end">Casos</th><th class="text-end">Reservas</th>'
            . '<th class="text-end">Form + velho</th><th>TOA</th><th>Leitura</th></tr></thead><tbody>';
        foreach ($loadRows as $r) {
            $addr = (string) $r['address'];
            $nome = (string) ($r['name'] !== '' ? $r['name'] : $addr);
            $online = (int) $r['online'] === 1;
            $toaOk = (int) $r['toa_ok'] === 1;
            $pres = (int) $r['form_presente_toa'] === 1;
            $ocioso = (float) $r['form_ocioso_min'];
            // Fisico = o que ele tem aberto no front; casos = solicitantes
            // distintos (form + chat do mesmo autor = 1), que e o numero do cap.
            $fisico = (int) $r['chats_toa'] + (int) $r['forms_toa'];
            $casos = (int) $r['casos_total'];
            $roteando = (int) ($r['roteando'] ?? 0) === 1;
            echo '<tr' . ($roteando ? '' : ' class="text-muted"') . '>';
            echo '<td><strong>' . h($nome) . '</strong>';
            if (!$roteando) {
                echo ' <span class="badge text-bg-warning" title="Inativo no roteamento deste time: nao recebe item novo">inativo</span>';
            }
            echo '<div class="text-muted small"><code>' . h($addr) . '</code></div></td>';
            echo '<td>' . h((string) ($r['team_name'] ?? '')) . '</td>';
            echo '<td>' . badge_state($online, 'sim', 'não') . '</td>';
            // Carimbo do offline: e o que o resgate usa para devolver o caso.
            $desdeOff = substr((string) ($r['offline_since'] ?? ''), 0, 19);
            $minOff = 0;
            if ($desdeOff !== '') {
                $tsOff = strtotime($desdeOff . ' UTC');
                $minOff = $tsOff ? max(0, (int) floor((time() - $tsOff) / 60)) : 0;
            }
            if (!$online && $desdeOff !== '') {
                $corOff = $minOff >= max(1, intdiv((int) $loadOfflineSec, 60)) ? 'bg-danger' : 'bg-secondary';
                echo '<td><span class="badge ' . h($corOff) . '">' . (int) $minOff . ' min</span></td>';
            } else {
                echo '<td><span class="text-muted">—</span></td>';
            }
            echo '<td class="text-end">' . (int) $r['chats_toa'] . '</td>';
            echo '<td class="text-end">' . (int) $r['forms_toa'] . ($pres ? ' <span class="badge bg-info text-dark">presente</span>' : '') . '</td>';
            echo '<td class="text-end"><span class="badge ' . ($fisico > 0 ? 'bg-secondary' : 'bg-light text-dark') . '"'
                . ' title="Fisico: TOA chat + TOA form (o que o operador tem aberto no TOA)">' . $fisico . '</span>'
                . ' <span class="text-muted small">(' . $casos . ' casos)</span></td>';
            echo '<td class="text-end">' . (int) $r['reservas'] . '</td>';
            echo '<td class="text-end">' . ($ocioso > 0 ? h(number_format($ocioso, 0) . ' min') : '<span class="text-muted">—</span>') . '</td>';
            echo '<td>' . badge_state($toaOk, 'ok', 'falhou') . '</td>';
            echo '<td>' . h(fmt_dt($r['at'])) . '</td>';
            echo '</tr>';
        }
        echo '</tbody></table></div>';
    }
}

if ($page === 'users') {
    bot_users_render($pdo, $csrf, (string) ($formError ?? ''));
}

if ($page === 'groups') {
    bot_groups_render($pdo, $csrf, (string) ($formError ?? ''));
}

if ($page === 'dashboard') {
    $tzDash = new DateTimeZone('America/Sao_Paulo');
    $hojeDash = (new DateTimeImmutable('now', $tzDash))->format('Y-m-d');
    // Padrao = HOJE: os cards do topo sao "do dia" (chats/forms atendidos). Os
    // atalhos 7d/30d/90d (ou os campos De/Ate) mudam o recorte de toda a pagina.
    $deDefault = $hojeDash;
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
    // Filtro global por ANALISTA (nome ou endereco): vale para os cards, o donut
    // e a tabela. O curinga digitado pelo usuario sai antes (o `%` e do LIKE).
    $nomeRaw = trim((string) ($_GET['nome'] ?? ''));
    $nomeBusca = str_replace(['%', '_'], '', $nomeRaw);
    $nomeLike = $nomeBusca !== '' ? '%' . $nomeBusca . '%' : '';

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
        $candParams = [];
        $candScope = bot_scope_queue_where($pdo, 'e.queue_address', $candParams);
        $stCand = $pdo->prepare(
            "SELECT DISTINCT e.queue_address, q.name AS queue_name
             FROM bot_events e
             LEFT JOIN queues q ON q.address = e.queue_address
             WHERE e.reason = 'classified' AND e.queue_address IS NOT NULL AND e.queue_address <> ''"
            . $candScope . "
             ORDER BY COALESCE(q.name, e.queue_address), e.queue_address"
        );
        $stCand->execute($candParams);
        $candQueues = $stCand->fetchAll();
    } catch (Throwable $e) {
        $candQueues = [];
    }

    page_header(
        'Dashboard',
        'Cards do dia (chats e formulários atendidos e as duas filas), operadores online x offline e o volume de classificações do período.',
        ''
    );
    echo '<div class="d-none">';
    echo '<h1 class="h3 mb-1">Dashboard</h1>';
    echo '<p class="text-muted">Cards: <strong>TOTAL CHATS/DIA</strong> e <strong>TOTAL FORMS/DIA</strong> são o que os analistas atenderam no período (roteado pelo bot, transferido ou assumido); <strong>FILA DE CHAT</strong> são os chats com o chatbot agora e <strong>FILA FORMS</strong> são os formulários abertos sem operador (esperando na fila do time). O filtro de filas, o período e o nome do analista valem para os cards, o donut e a tabela. Abaixo, o gráfico de classificações por categoria e o resumo do formulário de validação — tudo gerado pelo próprio painel, sem bibliotecas externas.</p></div>';

    $dashQueueOpts = [];
    foreach ($candQueues as $cq) {
        $qaOpt = (string) $cq['queue_address'];
        $nomeOpt = trim((string) ($cq['queue_name'] ?? ''));
        $dashQueueOpts[$qaOpt] = ($nomeOpt !== '' ? $nomeOpt . ' — ' : '') . $qaOpt;
    }
    echo '<div class="card mb-3"><div class="card-body">';
    echo '<form method="get" class="row g-3 align-items-end">';
    echo '<input type="hidden" name="page" value="dashboard">';
    echo '<div class="col-12 col-md-5"><label class="form-label">Filas — nenhuma marcada = todas</label>';
    if ($dashQueueOpts) {
        ui_multiselect('filas', $dashQueueOpts, $selList, 'Todas as filas');
    } else {
        echo '<p class="text-muted small mb-0">Nenhuma fila com eventos de classificação ainda.</p>';
    }
    echo '</div>';
    ui_date_range($deRaw, $ateRaw);
    echo '<div class="col-12 col-md-3"><label class="form-label">Analista — nome ou endereço</label>';
    echo '<input type="text" class="form-control form-control-sm" name="nome" value="' . h($nomeRaw) . '" placeholder="Ex.: Ana"></div>';
    echo '<div class="col-12 col-md-auto"><button class="btn btn-primary btn-sm">Filtrar</button> ';
    echo '<a class="btn btn-outline-secondary btn-sm" href="?page=dashboard">Limpar</a></div>';
    echo '<div class="col-12"><div class="form-text mb-0">O filtro vale para os cards, o donut e a tabela de analistas. '
        . 'Os cards <strong>Fila de chat</strong> e <strong>Fila Forms</strong> mostram o <em>agora</em>; os demais respeitam o período.</div></div>';
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
        $whereDash .= bot_scope_queue_where($pdo, 'e.queue_address', $paramsDash);
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
        $legNome = $nomeBusca !== '' ? 'analista “' . $nomeBusca . '”' : 'todos os analistas';
        $perLeg = $de->format('d/m/Y') . ' a ' . $ate->format('d/m/Y');
        // Filtro por nome: resolve os ENDERECOS dos operadores que casam (nome da
        // leitura, nome no time ou o proprio endereco). Sem isso o ledger, que
        // guarda so o endereco, nao teria como responder ao filtro por nome.
        $nomeConds = '';
        $nomeParams = [];
        if ($nomeBusca !== '') {
            $addrsNome = [];
            try {
                $stNome = $pdo->prepare(
                    'SELECT address FROM analyst_load WHERE ' . bot_like_ci('name')
                    . ' LIKE ? OR ' . bot_like_ci('address') . ' LIKE ?'
                );
                $stNome->execute([$nomeLike, $nomeLike]);
                foreach ($stNome->fetchAll(PDO::FETCH_COLUMN) as $addrNome) {
                    $addrsNome[(string) $addrNome] = true;
                }
                $stNome = $pdo->prepare(
                    'SELECT address FROM team_members WHERE ' . bot_like_ci('name')
                    . ' LIKE ? OR ' . bot_like_ci('address') . ' LIKE ?'
                );
                $stNome->execute([$nomeLike, $nomeLike]);
                foreach ($stNome->fetchAll(PDO::FETCH_COLUMN) as $addrNome) {
                    $addrsNome[(string) $addrNome] = true;
                }
            } catch (Throwable $eNome) {
                $addrsNome = [];
            }
            $addrsNome = array_keys($addrsNome);
            if ($addrsNome) {
                $nomeConds = 'a.assignee IN (' . implode(',', array_fill(0, count($addrsNome), '?')) . ')';
                $nomeParams = $addrsNome;
            } else {
                $nomeConds = bot_like_ci('a.assignee') . ' LIKE ?';
                $nomeParams = [$nomeLike];
            }
        }

        // TOTAL CHATS/DIA e TOTAL FORMS/DIA saem do LEDGER do bot
        // (route_assignments) no periodo: o bot entregou (transferencia/entrega),
        // o tecnico assumiu na mao ou o item foi adotado na fila. Uma linha por
        // caso entregue, aberto ou ja fechado — e o que responde por "atendidos".
        $atendTot = ['chat' => 0, 'form' => 0];
        $atendPor = [];
        try {
            $paramsAt = ['-3 hours', $de->format('Y-m-d'), $ate->format('Y-m-d')];
            $condsAt = ["date(datetime(a.assigned_at, ?)) BETWEEN date(?) AND date(?)"];
            $scopeAt = bot_scope_queue_where($pdo, 'a.queue_address', $paramsAt);
            if ($scopeAt !== '') {
                $condsAt[] = substr($scopeAt, 5);
            }
            if ($selList) {
                $condsAt[] = 'a.queue_address IN (' . implode(',', array_fill(0, count($selList), '?')) . ')';
                foreach ($selList as $qaAt) {
                    $paramsAt[] = $qaAt;
                }
            }
            if ($nomeConds !== '') {
                $condsAt[] = $nomeConds;
                foreach ($nomeParams as $pNome) {
                    $paramsAt[] = $pNome;
                }
            }
            $stAt = $pdo->prepare(
                'SELECT a.assignee, a.kind, COUNT(*) AS n FROM route_assignments a WHERE '
                . implode(' AND ', $condsAt) . ' GROUP BY a.assignee, a.kind'
            );
            $stAt->execute($paramsAt);
            foreach ($stAt->fetchAll() as $rAt) {
                $tipoAt = ((string) $rAt['kind']) === 'form' ? 'form' : 'chat';
                $nAt = (int) $rAt['n'];
                $atendTot[$tipoAt] += $nAt;
                $addrAt = (string) $rAt['assignee'];
                if (!isset($atendPor[$addrAt])) {
                    $atendPor[$addrAt] = ['chat' => 0, 'form' => 0];
                }
                $atendPor[$addrAt][$tipoAt] += $nAt;
            }
        } catch (Throwable $eAt) {
            $atendTot = ['chat' => 0, 'form' => 0];
            $atendPor = [];
        }
        // FILA DE CHAT (agora): chats com o bot dentro — status vivo e `moved_at`
        // nulo, o mesmo criterio do card "ChatBot — Em atendimento" da aba Carga.
        $botAgora = 0;
        $statusBotDash = bot_open_chat_statuses();
        try {
            $paramsBot = [];
            $filtroBot = ' WHERE c.moved_at IS NULL AND c.status IN ('
                . implode(',', array_fill(0, count($statusBotDash), '?')) . ')';
            foreach ($statusBotDash as $stBot) {
                $paramsBot[] = $stBot;
            }
            $filtroBot .= bot_scope_queue_where($pdo, 'c.queue_address', $paramsBot);
            if ($selList) {
                $filtroBot .= ' AND c.queue_address IN (' . implode(',', array_fill(0, count($selList), '?')) . ')';
                foreach ($selList as $qaBot) {
                    $paramsBot[] = $qaBot;
                }
            }
            $stBotDash = $pdo->prepare('SELECT COUNT(*) FROM chats c' . $filtroBot);
            $stBotDash->execute($paramsBot);
            $botAgora = (int) $stBotDash->fetchColumn();
        } catch (Throwable $eBot) {
            $botAgora = 0;
        }
        // FILA FORMS (agora): formulario aberto e SEM operador (esperando na fila
        // do time), o mesmo criterio do card "Fila de formulário" da aba Carga.
        $filaFormAgora = 0;
        try {
            $paramsFilaForm = [];
            $filtroFilaForm = " WHERE r.status = 'waiting' AND r.kind = 'form'"
                . bot_scope_team_where($pdo, 'r.team_id', $paramsFilaForm);
            if ($selList) {
                $filtroFilaForm .= ' AND r.queue_address IN (' . implode(',', array_fill(0, count($selList), '?')) . ')';
                foreach ($selList as $qaFil) {
                    $paramsFilaForm[] = $qaFil;
                }
            }
            $stFilaForm = $pdo->prepare('SELECT COUNT(*) FROM pending_routes r' . $filtroFilaForm);
            $stFilaForm->execute($paramsFilaForm);
            $filaFormAgora = (int) $stFilaForm->fetchColumn();
        } catch (Throwable $eFilaForm) {
            $filaFormAgora = 0;
        }
        // Linhas da tabela: a ultima leitura por analista (aba Carga), na mesma
        // ordem de la — quem esta ativo no roteamento primeiro, maior carga no topo.
        $dashRows = [];
        try {
            $paramsDashLoad = [];
            $condsDashLoad = [];
            $scopeDashLoad = bot_scope_team_where($pdo, 'l.team_id', $paramsDashLoad);
            if ($scopeDashLoad !== '') {
                $condsDashLoad[] = substr($scopeDashLoad, 5);
            }
            if ($selList) {
                // O chat nao tem team_id: a fila escolhida entra pelos times dela.
                $marcaDash = implode(',', array_fill(0, count($selList), '?'));
                $condsDashLoad[] = 'l.team_id IN (SELECT team_id FROM team_queues WHERE queue_address IN (' . $marcaDash . '))';
                foreach ($selList as $qaLoadDash) {
                    $paramsDashLoad[] = $qaLoadDash;
                }
            }
            if ($nomeBusca !== '') {
                $condsDashLoad[] = '(' . bot_like_ci('l.name') . ' LIKE ? OR ' . bot_like_ci('l.address') . ' LIKE ?)';
                $paramsDashLoad[] = $nomeLike;
                $paramsDashLoad[] = $nomeLike;
            }
            $stDashLoad = $pdo->prepare(
                'SELECT l.*, t.name AS team_name, COALESCE(tm.active, 0) AS roteando'
                . ' FROM analyst_load l'
                . ' LEFT JOIN teams t ON t.id = l.team_id'
                . ' LEFT JOIN team_members tm ON tm.team_id = l.team_id AND tm.address = l.address'
                . ($condsDashLoad ? ' WHERE ' . implode(' AND ', $condsDashLoad) : '')
                . ' ORDER BY (CASE WHEN COALESCE(tm.active, 0) = 1 THEN 0 ELSE 1 END),'
                . ' (l.chats_toa + l.forms_toa) DESC, l.form_ocioso_min DESC, l.name'
            );
            $stDashLoad->execute($paramsDashLoad);
            foreach ($stDashLoad->fetchAll() as $rLoadDash) {
                $addrLoadDash = (string) $rLoadDash['address'];
                $nomeLoadDash = trim((string) $rLoadDash['name']);
                $dashRows[$addrLoadDash] = [
                    'address' => $addrLoadDash,
                    'name' => $nomeLoadDash !== '' ? $nomeLoadDash : $addrLoadDash,
                    'team' => (string) ($rLoadDash['team_name'] ?? ''),
                    'online' => (int) $rLoadDash['online'] === 1,
                    'online_ok' => (int) ($rLoadDash['online_ok'] ?? 0) === 1,
                    'toa_chat' => (int) $rLoadDash['chats_toa'],
                    'toa_form' => (int) $rLoadDash['forms_toa'],
                    'casos' => (int) $rLoadDash['casos_total'],
                    'presente' => (int) $rLoadDash['form_presente_toa'] === 1,
                    'toa_ok' => (int) $rLoadDash['toa_ok'] === 1,
                    'form_velho' => (float) $rLoadDash['form_ocioso_min'],
                    'leitura' => (string) $rLoadDash['at'],
                    'roteando' => (int) ($rLoadDash['roteando'] ?? 0) === 1,
                    'fora_leitura' => false,
                ];
            }
        } catch (Throwable $eLoadDash) {
            $dashRows = [];
        }
        // Quem atendeu no periodo mas nao esta na ultima leitura continua na
        // tabela: sem isso a coluna de atendidos nao fecharia com os cards.
        foreach (array_keys($atendPor) as $addrExtra) {
            if (isset($dashRows[$addrExtra])) {
                continue;
            }
            $timeExtra = '';
            try {
                $stTimeExtra = $pdo->prepare(
                    'SELECT t.name FROM team_members tm JOIN teams t ON t.id = tm.team_id'
                    . ' WHERE tm.address = ? ORDER BY tm.team_id LIMIT 1'
                );
                $stTimeExtra->execute([$addrExtra]);
                $timeExtra = (string) ($stTimeExtra->fetchColumn() ?: '');
            } catch (Throwable $eTimeExtra) {
                $timeExtra = '';
            }
            $dashRows[$addrExtra] = [
                'address' => $addrExtra,
                'name' => $addrExtra,
                'team' => $timeExtra,
                'online' => false,
                'online_ok' => false,
                'toa_chat' => 0,
                'toa_form' => 0,
                'casos' => 0,
                'presente' => false,
                'toa_ok' => false,
                'form_velho' => 0.0,
                'leitura' => '',
                'roteando' => false,
                'fora_leitura' => true,
            ];
        }

        // Donut: ONLINE x OFFLINE pela ultima leitura de presenca do TOA. Leitura
        // de presenca que falhou nao vira "offline" (o alerta da aba Carga faz a
        // mesma distincao): entra como "sem leitura".
        $dashOn = 0;
        $dashOff = 0;
        $dashSem = 0;
        foreach ($dashRows as $rDonut) {
            if (!$rDonut['online_ok']) {
                $dashSem++;
            } elseif ($rDonut['online']) {
                $dashOn++;
            } else {
                $dashOff++;
            }
        }
        $fatiasDonut = [['Online', $dashOn, '#198754'], ['Offline', $dashOff, '#b02a37']];
        if ($dashSem > 0) {
            $fatiasDonut[] = ['Sem leitura', $dashSem, '#adb5bd'];
        }
        // Cards do topo. "Atendidos" = ledger do periodo; as duas filas = agora.
        echo '<div class="row g-3 mb-3">';
        $cardsDash = [
            ['TOTAL CHATS/DIA', (string) $atendTot['chat'], 'bg-dark', 'Chats com analista no período: roteado pelo bot, transferido ou assumido.'],
            ['TOTAL FORMS/DIA', (string) $atendTot['form'], 'bg-primary', 'Formulários de validação com analista no período: roteado ou assumido.'],
            ['FILA DE CHAT', (string) $botAgora, 'bg-warning text-dark', 'Chats com o chatbot agora (bot dentro do chat, sem analista).'],
            ['FILA FORMS', (string) $filaFormAgora, 'bg-info text-dark', 'Formulários abertos sem operador (esperando na fila do time).'],
        ];
        foreach ($cardsDash as $cD) {
            echo '<div class="col-6 col-xl-3"><div class="card h-100"><div class="card-body py-2">';
            echo '<div class="text-muted small">' . h($cD[0]) . '</div>';
            echo '<div class="fs-3"><span class="badge ' . h($cD[2]) . '">' . h($cD[1]) . '</span></div>';
            echo '<div class="text-muted small">' . h($cD[3]) . '</div>';
            echo '</div></div></div>';
        }
        echo '</div>';
        // Donut de operadores (online x offline) ao lado da tabela de detalhe.
        echo '<div class="row g-3 mb-3">';
        echo '<div class="col-12 col-lg-4"><div class="card h-100"><div class="card-body">';
        echo '<h2 class="h6">Operadores — online x offline</h2>';
        echo '<p class="text-muted small mb-2">Última leitura de presença por analista — ' . h($legFila) . ', ' . h($legNome) . '.</p>';
        dash_donut($fatiasDonut, (string) count($dashRows), 'analista(s)');
        echo '<p class="text-muted small mb-0">Quem está <strong>inativo</strong> no roteamento não recebe item novo; '
            . '<strong>sem leitura</strong> é presença que falhou na coleta — o analista não vira “offline” por isso.</p>';
        echo '</div></div></div>';
        // Tabela de detalhe: uma linha por analista com o retrato do TOA (agora) e o
        // que ele atendeu no periodo (ledger do bot).
        echo '<div class="col-12 col-lg-8"><div class="card h-100"><div class="card-body">';
        echo '<h2 class="h6">Analistas — carga no TOA e atendidos</h2>';
        if (!$dashRows) {
            echo '<p class="text-muted mb-0">Nenhum analista para esses filtros.</p>';
        } else {
            $totToaChat = 0;
            $totToaForm = 0;
            $totAtChat = 0;
            $totAtForm = 0;
            echo '<div class="table-responsive sticky-head"><table class="table table-slim table-hover align-middle bg-white mb-0">';
            echo '<thead><tr><th>Analista</th><th>Time</th><th>Status</th>'
                . '<th class="text-end">TOA chat</th><th class="text-end">TOA form</th>'
                . '<th class="text-end">Em atend.</th><th class="text-end">Form + velho</th>'
                . '<th>Leitura</th><th class="text-end">Atendidos (chat/form)</th></tr></thead><tbody>';
            foreach ($dashRows as $addrTab => $rTab) {
                $atChat = (int) ($atendPor[$addrTab]['chat'] ?? 0);
                $atForm = (int) ($atendPor[$addrTab]['form'] ?? 0);
                $emAtend = (int) $rTab['toa_chat'] + (int) $rTab['toa_form'];
                $ociosoTab = (float) $rTab['form_velho'];
                $totToaChat += (int) $rTab['toa_chat'];
                $totToaForm += (int) $rTab['toa_form'];
                $totAtChat += $atChat;
                $totAtForm += $atForm;
                echo '<tr' . ($rTab['roteando'] ? '' : ' class="text-muted"') . '>';
                echo '<td><strong>' . h((string) $rTab['name']) . '</strong>';
                if (!$rTab['roteando']) {
                    echo ' <span class="badge text-bg-warning" title="Inativo no roteamento: nao recebe item novo">inativo</span>';
                }
                echo '<div class="text-muted small"><code>' . h((string) $addrTab) . '</code></div></td>';
                echo '<td>' . h((string) $rTab['team']) . '</td>';
                echo '<td>' . badge_state((bool) $rTab['online'], 'sim', 'não');
                if (!$rTab['online_ok']) {
                    echo ' <span class="badge bg-secondary" title="Leitura de presenca sem resultado">sem leitura</span>';
                }
                echo '</td>';
                echo '<td class="text-end">' . (int) $rTab['toa_chat'] . '</td>';
                echo '<td class="text-end">' . (int) $rTab['toa_form']
                    . ($rTab['presente'] ? ' <span class="badge bg-info text-dark">presente</span>' : '') . '</td>';
                echo '<td class="text-end"><span class="badge ' . ($emAtend > 0 ? 'bg-secondary' : 'bg-light text-dark')
                    . '" title="TOA chat + TOA form: o que o operador tem aberto no TOA agora">' . $emAtend . '</span></td>';
                echo '<td class="text-end">' . ($ociosoTab > 0 ? h(number_format($ociosoTab, 0) . ' min') : '<span class="text-muted">—</span>') . '</td>';
                echo '<td>' . ($rTab['leitura'] !== ''
                    ? h(fmt_dt((string) $rTab['leitura'])) . ' ' . badge_state((bool) $rTab['toa_ok'], 'ok', 'falhou')
                    : '<span class="text-muted">fora da leitura</span>') . '</td>';
                echo '<td class="text-end">' . $atChat . ' / ' . $atForm
                    . ' <span class="text-muted small">(' . ($atChat + $atForm) . ')</span></td>';
                echo '</tr>';
            }
            echo '</tbody></table></div>';
            echo '<p class="text-muted small mb-0 mt-2">Total do <em>agora</em>: TOA chat <strong>' . $totToaChat
                . '</strong>, TOA form <strong>' . $totToaForm . '</strong>, em atendimento <strong>' . ($totToaChat + $totToaForm)
                . '</strong>. Atendidos no período <strong>' . h($perLeg) . '</strong>: chats <strong>' . $totAtChat
                . '</strong> e formulários <strong>' . $totAtForm . '</strong> (ledger do bot). '
                . 'A tabela se atualiza sozinha.</p>';
        }
        echo '</div></div></div>';
        echo '</div>';
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
        $formReasons = [
            'closure' => 'recebidos',
            'form_routed' => 'roteados',
            'form_no_agent' => 'sem analista',
            'form_route_retry' => 'nova tentativa',
            'form_no_team' => 'sem time',
            'form_no_queue' => 'sem fila',
        ];
        $whereForm = "e.reason IN ('" . implode("','", array_keys($formReasons)) . "')";
        $paramsForm = [];
        $whereForm .= bot_scope_queue_where($pdo, 'e.queue_address', $paramsForm);
        if ($selList) {
            $whereForm .= ' AND e.queue_address IN (' . implode(',', array_fill(0, count($selList), '?')) . ')';
            foreach ($selList as $qaForm) {
                $paramsForm[] = $qaForm;
            }
        }
        $whereForm .= ' AND date(datetime(e.at, ?)) BETWEEN date(?) AND date(?)';
        $paramsForm[] = '-3 hours';
        $paramsForm[] = $de->format('Y-m-d');
        $paramsForm[] = $ate->format('Y-m-d');
        $contaForm = [];
        try {
            $stForm = $pdo->prepare("SELECT e.reason, COUNT(*) AS total FROM bot_events e WHERE $whereForm GROUP BY e.reason");
            $stForm->execute($paramsForm);
            foreach ($stForm->fetchAll() as $rForm) {
                $contaForm[(string) $rForm['reason']] = (int) $rForm['total'];
            }
        } catch (Throwable $eForm) {
            $contaForm = [];
        }
        $totForm = array_sum($contaForm);
        echo '<div class="card mt-3"><div class="card-body">';
        echo '<h2 class="h6 mb-0">Formulário de validação — ' . h($de->format('d/m/Y')) . ' a ' . h($ate->format('d/m/Y')) . ' (' . h($legFila) . ')</h2>';
        echo '<p class="text-muted mb-2 small">' . $totForm . ' evento(s) de formulário no período. “Roteados” são os entregues a um analista.</p>';
        echo '<div class="d-flex flex-wrap gap-2">';
        foreach ($formReasons as $rkForm => $rotForm) {
            $vForm = $contaForm[$rkForm] ?? 0;
            echo '<span class="badge text-bg-light border' . ($vForm > 0 ? '' : ' opacity-50') . '">';
            echo '<code>' . h($rkForm) . '</code> ' . h($rotForm) . ': <strong>' . $vForm . '</strong></span>';
        }
        echo '</div>';
        if ($totForm === 0) {
            echo '<p class="text-muted small mb-0 mt-2">Nenhum formulário no período. Se chegar formulário e a fila dele não tiver time ativo, ele aparece em “sem time”.</p>';
        }
        echo '</div></div>';

        // Fila de espera do chat: quantos esperam agora e quanto tempo esperaram no periodo.
        $paramsEspera = [];
        $filtroEspera = bot_scope_queue_where($pdo, 's.queue_address', $paramsEspera);
        if ($selList) {
            $filtroEspera .= ' AND s.queue_address IN (' . implode(',', array_fill(0, count($selList), '?')) . ')';
            foreach ($selList as $qaEsp) {
                $paramsEspera[] = $qaEsp;
            }
        }
        $filtroEspera .= ' AND date(datetime(s.at, ?)) BETWEEN date(?) AND date(?)';
        $paramsEspera[] = '-3 hours';
        $paramsEspera[] = $de->format('Y-m-d');
        $paramsEspera[] = $ate->format('Y-m-d');
        $esperaMedia = null;
        $esperaMax = null;
        $entradasFila = 0;
        $saidasFila = 0;
        try {
            $stEnt = $pdo->prepare(
                "SELECT COUNT(*) FROM bot_events s WHERE s.reason = 'queued'" . $filtroEspera
            );
            $stEnt->execute($paramsEspera);
            $entradasFila = (int) $stEnt->fetchColumn();
            $stSai = $pdo->prepare(
                "SELECT COUNT(*) FROM bot_events s WHERE s.reason = 'queued_assigned'" . $filtroEspera
            );
            $stSai->execute($paramsEspera);
            $saidasFila = (int) $stSai->fetchColumn();
            // Espera = tempo entre o "queued" daquele chat e a atribuicao ("queued_assigned").
            $stEsp = $pdo->prepare(
                "SELECT AVG(seg) AS media, MAX(seg) AS maxima, COUNT(*) AS casos FROM (
                    SELECT (julianday(s.at) - julianday(MAX(e.at))) * 86400 AS seg
                      FROM bot_events s
                      JOIN bot_events e ON e.chat_id = s.chat_id AND e.reason = 'queued' AND e.at <= s.at
                     WHERE s.reason = 'queued_assigned'" . $filtroEspera . "
                     GROUP BY s.id
                 )"
            );
            $stEsp->execute($paramsEspera);
            $linhaEsp = $stEsp->fetch();
            if ($linhaEsp && $linhaEsp['media'] !== null && (int) $linhaEsp['casos'] > 0) {
                $esperaMedia = (int) round((float) $linhaEsp['media']);
                $esperaMax = (int) round((float) $linhaEsp['maxima']);
            }
        } catch (Throwable $eEspera) {
            $esperaMedia = null;
            $esperaMax = null;
        }
        $naFilaAgora = 0;
        try {
            $paramsAgora = [];
            $stAgora = $pdo->prepare(
                "SELECT COUNT(*) FROM pending_routes r WHERE r.status = 'waiting' AND r.kind = 'chat'"
                . bot_scope_team_where($pdo, 'r.team_id', $paramsAgora)
            );
            $stAgora->execute($paramsAgora);
            $naFilaAgora = (int) $stAgora->fetchColumn();
        } catch (Throwable $eAgora) {
            $naFilaAgora = 0;
        }
        $fmtEspera = static function (?int $seg): string {
            if ($seg === null) {
                return '-';
            }
            if ($seg < 60) {
                return $seg . ' s';
            }
            if ($seg < 3600) {
                return floor($seg / 60) . ' min ' . ($seg % 60) . ' s';
            }
            return floor($seg / 3600) . ' h ' . floor(($seg % 3600) / 60) . ' min';
        };
        echo '<div class="card mt-3"><div class="card-body">';
        echo '<h2 class="h6 mb-0">Fila de espera do chat — ' . h($de->format('d/m/Y')) . ' a ' . h($ate->format('d/m/Y')) . '</h2>';
        echo '<p class="text-muted mb-2 small">' . $entradasFila . ' entrada(s) na fila e ' . $saidasFila . ' atendida(s) no período. Espera é o tempo entre entrar na fila e receber um analista.</p>';
        echo '<div class="d-flex flex-wrap gap-4">';
        echo '<div><span class="text-muted small d-block">Na fila agora (fora do período)</span><span class="fs-5">' . $naFilaAgora . '</span></div>';
        echo '<div><span class="text-muted small d-block">Espera média</span><span class="fs-5">' . h($fmtEspera($esperaMedia)) . '</span></div>';
        echo '<div><span class="text-muted small d-block">Maior espera</span><span class="fs-5">' . h($fmtEspera($esperaMax)) . '</span></div>';
        echo '</div>';
        echo '<p class="text-muted small mb-0 mt-2">Sem amostra no período: aparece "-". A fila atual e o botão Encerrar espera ficam na aba <a href="?page=routing">Roteamento</a>.</p>';
        echo '</div></div>';
    }
}

layout_end();
