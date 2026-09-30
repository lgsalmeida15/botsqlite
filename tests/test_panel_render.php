<?php
declare(strict_types=1);

/**
 * Render das abas do painel: sem erro fatal, HTML balanceado e conteudo visivel.
 *
 * Cobre duas regressoes reais de 22/09:
 *   - `mb_substr` sem a extensao mbstring derrubava a aba Roteamento;
 *   - `<div class="d-none">` sem fechamento escondia Atividade/Chats/Dashboard
 *     inteiros (aparecia so o cabecalho).
 *
 * Roda contra um banco TEMPORARIO (SQLITE_PATH no ambiente, ver bot_sqlite_path),
 * nunca contra data/helptoa.sqlite.
 */
require __DIR__ . '/../php/bootstrap.php';
require __DIR__ . '/../php/ui.php';

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

// ui_clip: o corte de texto do painel nao pode depender de mbstring.
ok(ui_clip('abcdef', 3) === 'abc', 'ui_clip corta texto simples');
ok(ui_clip('acao', 99) === 'acao', 'ui_clip nao corta quando cabe');
ok(ui_clip('ação e coração', 4) === 'ação', 'ui_clip corta por caractere (acentos) sem mbstring');
ok(ui_clip('x', 0) === '', 'ui_clip com tamanho 0 devolve vazio');
ok(function_exists('mb_substr') || ui_clip('Ação', 3) === 'Açã', 'ui_clip funciona sem a extensao mbstring');

$path = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'helptoa_panel_' . uniqid('', true) . '.sqlite';
$pdo = new PDO('sqlite:' . $path, null, null, [
    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
]);
$pdo->exec('PRAGMA foreign_keys = ON');
bot_ensure_schema($pdo);

// Super admin (acesso total) + dados minimos para as abas renderizarem conteudo.
$pdo->exec("INSERT INTO permission_groups (name, is_super) VALUES ('Super', 1)");
$gid = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO bot_users (login, password_hash, group_id, display_name) VALUES ('teste', 'x', {$gid}, 'Teste')");
$uid = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q1', 'Fila 1', 1)");
$pdo->exec("INSERT INTO menus (name, active) VALUES ('M1', 1)");
$pdo->exec("INSERT INTO menu_queues (menu_id, queue_address) VALUES (1, 'h:q1')");
$pdo->exec("INSERT INTO menu_options (menu_id, sort_order, label, classification, action, reply_text) VALUES (1, 1, 'Validação', 'TESTE', 'reply_and_close', 'ok')");
$pdo->exec("INSERT INTO teams (name, active) VALUES ('T1', 1)");
$pdo->exec("INSERT INTO team_queues (team_id, queue_address) VALUES (1, 'h:q1')");
$pdo->exec("INSERT INTO team_members (team_id, address, name, active) VALUES (1, 'u:ana@claro.com', 'Ana', 1)");
$pdo->exec("INSERT INTO chats (chat_id, queue_address, status, classification) VALUES (10, 'h:q1', 'awaiting_reply', 'TESTE')");
$pdo->exec("INSERT INTO chats (chat_id, queue_address, status, linked_form_chat_id) VALUES (11, 'h:q1', 'closed', 99)");
$pdo->exec("INSERT INTO bot_events (chat_id, queue_address, reason, detail) VALUES (10, 'h:q1', 'form_aware_skip', 'abertos=1; na_fila=0')");
$pdo->exec("INSERT INTO bot_events (chat_id, queue_address, reason, detail) VALUES (11, 'h:q1', 'transferido', 'detalhe bem longo para exercitar o corte de texto do painel sem mbstring')");
$pdo->exec("INSERT INTO pending_routes (kind, chat_id, queue_address, status, detail) VALUES ('chat', 10, 'h:q1', 'failed', 'tentativas esgotadas; fallback=close')");
$pdo->exec("INSERT INTO delivery_attempts (kind, chat_id, op, target, result) VALUES ('chat', 10, 'move_chat', 'u:ana@claro.com', 'ok')");
$pdo->exec("INSERT INTO route_assignments (kind, chat_id, queue_address, team_id, assignee, case_key) VALUES ('form', 99, 'h:q1', 1, 'u:ana@claro.com', 'a:u:tec@claro.com')");
$pdo->exec("INSERT INTO route_assignments (kind, chat_id, queue_address, team_id, assignee, case_key) VALUES ('chat', 10, 'h:q1', 1, 'u:ana@claro.com', 'a:u:tec@claro.com')");
// Uma rota esperando: exercita o card "Fila de espera (agora)" (posicao por tipo+time).
$pdo->exec("INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status, detail) VALUES ('chat', 12, 'h:q1', 1, 'waiting', 'na fila')");
$pdo->exec(
    "INSERT INTO analyst_load (address, name, team_id, janela, online, chats_toa, forms_toa, chats_ledger,"
    . " forms_ledger, reservas, casos_total, form_ocioso_min, form_presente_toa, toa_ok, detail)"
    . " VALUES ('u:ana@claro.com', 'Ana', 1, '09:00-18:00', 1, 2, 1, 2, 1, 0, 2, 25.0, 1, 1, 'motivo=teste')"
);
// Analista inativo no roteamento com carga fisica MAIOR: a tabela tem que deixar
// a linha dele no fim (e sem o "presente" no TOA form). Ele tambem esta OFFLINE
// ha 5 min com caso no ledger: e o alvo do resgate (coluna "Offline ha" + botao).
$pdo->exec("INSERT INTO team_members (team_id, address, name, active) VALUES (1, 'u:inativo@claro.com', 'Inativo', 0)");
$pdo->exec(
    "INSERT INTO analyst_load (address, name, team_id, janela, online, online_ok, offline_since, chats_toa, forms_toa, chats_ledger,"
    . " forms_ledger, reservas, casos_total, form_ocioso_min, form_presente_toa, toa_ok, detail)"
    . " VALUES ('u:inativo@claro.com', 'Inativo', 1, '', 0, 1, datetime('now', '-5 minutes'), 9, 0, 9, 0, 0, 9, 0.0, 0, 1, 'motivo=teste')"
);

// Trava da aba Filas: habilitar exige Menu ATIVO + Time ATIVO vinculados.
// Sem isso o bot assumia o chat, mandava o menu e fechava depois (closed_no_agent).
// uma fila = um menu e um time (UNIQUE(queue_address) nas duas tabelas).
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q2', 'Fila sem vinculos', 0)");
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q3', 'Fila so com menu', 0)");
$pdo->exec("INSERT INTO menu_queues (menu_id, queue_address) VALUES (1, 'h:q3')");
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q4', 'Fila com vinculos inativos', 0)");
$pdo->exec("INSERT INTO menus (name, active) VALUES ('Menu inativo', 0)");
$menuInativo = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO menu_queues (menu_id, queue_address) VALUES ({$menuInativo}, 'h:q4')");
$pdo->exec("INSERT INTO teams (name, active) VALUES ('Time inativo', 0)");
$timeInativo = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO team_queues (team_id, queue_address) VALUES ({$timeInativo}, 'h:q4')");
// Menu ATIVO porem sem nenhuma opcao: tambem bloqueia (o chat cairia em "menu vazio").
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q5', 'Fila com menu vazio', 0)");
$pdo->exec("INSERT INTO menus (name, active) VALUES ('Menu vazio', 1)");
$menuVazio = (int) $pdo->lastInsertId();
$pdo->exec("INSERT INTO menu_queues (menu_id, queue_address) VALUES ({$menuVazio}, 'h:q5')");
$pdo->exec("INSERT INTO team_queues (team_id, queue_address) VALUES (1, 'h:q5')");

ok(queue_enable_block($pdo, 'h:q1') === '', 'fila com menu e time ativos: libera habilitar');
ok(
    strpos(queue_enable_block($pdo, 'h:q5'), 'opção') !== false,
    'menu ativo sem opcao cadastrada: bloqueia pela opcao'
);
$bloqSemNada = queue_enable_block($pdo, 'h:q2');
ok(
    strpos($bloqSemNada, 'Menu') !== false && strpos($bloqSemNada, 'Time') !== false,
    'fila sem menu e sem time: bloqueia pelos dois'
);
$bloqSoMenu = queue_enable_block($pdo, 'h:q3');
ok(
    strpos($bloqSoMenu, 'Time') !== false && strpos($bloqSoMenu, 'Menu') === false,
    'fila so com menu: bloqueia pelo time'
);
$bloqInativos = queue_enable_block($pdo, 'h:q4');
ok(
    strpos($bloqInativos, 'Menu') !== false && strpos($bloqInativos, 'Time') !== false,
    'vinculo inativo de menu/time nao libera a fila'
);
ok(queue_enable_block($pdo, 'h:nada') === 'fila não encontrada', 'fila inexistente: motivo explicito');

// Marcador de conteudo por aba: se o d-none ficar aberto, o marcador some.
$marcadores = [
    'dashboard' => 'Fila de espera do chat',
    'queues' => 'Novas filas entram desabilitadas',
    'operators' => 'Cadastrar',
    'menus' => 'Novo menu',
    'teams' => 'Plantão: pessoas + filas',
    'routing' => 'Rotas sem entrega',
    'settings' => 'Timeout de resposta',
    'templates' => 'greeting',
    'activity' => 'Todos os eventos',
    'load' => 'Leitura automática a cada',
    'chats' => 'Filtrar',
];

putenv('SQLITE_PATH=' . $path);
putenv('SQLITE_PATH_OVERRIDE=' . $path);
$php = PHP_BINARY;
$harness = __DIR__ . DIRECTORY_SEPARATOR . '_panel_render.php';

$htmls = [];
foreach ($marcadores as $pPage => $marca) {
    $cmd = escapeshellarg($php) . ' ' . escapeshellarg($harness) . ' ' . escapeshellarg($pPage) . ' ' . $uid . ' 2>&1';
    $out = [];
    $code = 0;
    exec($cmd, $out, $code);
    $html = implode("\n", $out);
    $htmls[$pPage] = $html;
    $erro = (stripos($html, 'Fatal error') !== false)
        || (stripos($html, 'Uncaught') !== false)
        || (stripos($html, 'Warning:') !== false)
        || (stripos($html, 'Parse error') !== false);
    ok($code === 0 && !$erro, "aba {$pPage}: renderizou sem erro fatal/warning");
    if ($code !== 0 || $erro) {
        $primeira = '';
        foreach ($out as $linha) {
            if (stripos($linha, 'error') !== false || stripos($linha, 'Uncaught') !== false) {
                $primeira = trim($linha);
                break;
            }
        }
        echo "      " . $primeira . "\n";
        continue;
    }
    $abre = substr_count($html, '<div');
    $fecha = substr_count($html, '</div>');
    ok($abre === $fecha, "aba {$pPage}: <div> balanceado (abre={$abre} fecha={$fecha})");
    $pos = strpos($html, '<div class="d-none">');
    if ($pos !== false) {
        $fimOculto = strpos($html, '</div>', $pos);
        $posMarca = strpos($html, $marca);
        ok(
            $fimOculto !== false && $posMarca !== false && $posMarca > $fimOculto,
            "aba {$pPage}: conteudo fora do bloco oculto (marcador depois do fechamento)"
        );
    } else {
        ok(strpos($html, $marca) !== false, "aba {$pPage}: conteudo esperado presente");
    }
}

// Aba Filas: a trava de habilitacao precisa aparecer (selo + botao desabilitado)
// sem esconder a acao de desabilitar das filas ja ligadas.
$htmlFilas = (string) ($htmls['queues'] ?? '');
ok(strpos($htmlFilas, 'sem menu ativo') !== false, 'aba queues: selo de fila sem menu ativo');
ok(strpos($htmlFilas, 'sem time ativo') !== false, 'aba queues: selo de fila sem time ativo');
ok(
    strpos($htmlFilas, 'Vincule um Menu e um Time ativos antes de habilitar') !== false,
    'aba queues: explica a pendencia no botao Habilitar'
);
ok(strpos($htmlFilas, 'disabled title="Vincule um Menu') !== false, 'aba queues: Habilitar desabilitado sem os vinculos');
ok(strpos($htmlFilas, '>Desabilitar<') !== false, 'aba queues: Desabilitar continua liberado');

// A aba Carga precisa listar a leitura semeada em analyst_load. Sem isso o
// harness estaria lendo outro banco (era o caso quando o `.env` vencia o
// SQLITE_PATH do teste) e a tabela viria vazia.
$htmlCarga = (string) ($htmls['load'] ?? '');
ok(strpos($htmlCarga, 'u:ana@claro.com') !== false, 'aba load: lista o analista da leitura (analyst_load)');
ok(strpos($htmlCarga, '25 min') !== false, 'aba load: ociosidade do formulario mais antigo');
ok(strpos($htmlCarga, 'leitura(s) com leitura bem sucedida') !== false, 'aba load: explica a regra de ausencia com o ajuste vigente');
ok(strpos($htmlCarga, 'value="load_now"') !== false, 'aba load: botao Atualizar agora (job load_snapshot)');

// Aba Carga: cards na ordem pedida, filtro de time e tabela sem as colunas do bot.
ok(strpos($htmlCarga, 'ChatBot — Em atendimento') !== false, 'aba load: card ChatBot em atendimento');
ok(strpos($htmlCarga, 'Fechados na ultima hora') === false, 'aba load: card de fechados removido');
ok(strpos($htmlCarga, 'Resolvidos por ausencia') === false, 'aba load: card de resolvidos por ausencia removido');
$ordemCards = [
    'ChatBot — Em atendimento',
    'Fila de chat',
    'Chat em atendimento',
    'Fila de formulário',
    'Formulário em atendimento',
    'Última leitura',
];
$posAnt = -1;
$ordemOk = true;
foreach ($ordemCards as $rotuloCard) {
    $posCard = strpos($htmlCarga, $rotuloCard);
    if ($posCard === false || $posCard < $posAnt) {
        $ordemOk = false;
        break;
    }
    $posAnt = $posCard;
}
ok($ordemOk, 'aba load: cards na ordem pedida (chatbot, fila de chat, chat, fila de form, form, ultima leitura)');
ok(strpos($htmlCarga, 'name="teams[]"') !== false, 'aba load: filtro de time multiplo');
ok(strpos($htmlCarga, '>Limpar<') !== false, 'aba load: botao Limpar do filtro');
ok(strpos($htmlCarga, 'Bot chat') === false, 'aba load: coluna Bot chat fora da tabela');
ok(strpos($htmlCarga, 'Bot form') === false, 'aba load: coluna Bot form fora da tabela');
ok(strpos($htmlCarga, '(2 casos)') !== false, 'aba load: Casos mostra o fisico com os casos distintos em cinza');
ok(strpos($htmlCarga, 'title="Fisico: TOA chat + TOA form') !== false, 'aba load: explicacao do fisico no titulo da celula');
$posAna = strpos($htmlCarga, 'u:ana@claro.com');
$posInativo = strpos($htmlCarga, 'u:inativo@claro.com');
ok($posAna !== false && $posInativo !== false && $posAna < $posInativo, 'aba load: inativo no roteamento vai para o fim da tabela');
ok(strpos($htmlCarga, '>inativo</span>') !== false, 'aba load: selo de inativo no roteamento');

// Resgate por analista offline: coluna, alerta e botao manual na aba Carga.
ok(strpos($htmlCarga, '<th>Offline há</th>') !== false, 'aba load: coluna Offline há');
ok(strpos($htmlCarga, 'analista(s) <strong>offline</strong> com caso aberto') !== false, 'aba load: alerta de offline com caso aberto');
ok(strpos($htmlCarga, 'value="requeue_offline"') !== false, 'aba load: botao Devolver a fila (job requeue_offline)');
ok(strpos($htmlCarga, 'Devolver à fila') !== false, 'aba load: rotulo do botao manual');

// Ajustes do resgate no Roteamento: globais + por time (linha de cada time).
$htmlRoutingOff = (string) ($htmls['routing'] ?? '');
ok(strpos($htmlRoutingOff, 'name="offline_requeue_enabled"') !== false, 'aba routing: liga/desliga do resgate');
ok(strpos($htmlRoutingOff, 'name="offline_requeue_sec"') !== false, 'aba routing: segundos de offline para devolver');
ok(
    substr_count($htmlRoutingOff, 'name="offline_requeue_sec"') >= 2,
    'aba routing: resgate global + por time (linha do time)'
);
ok(strpos($htmlRoutingOff, '0 = este time nao devolve') !== false, 'aba routing: explica o 0 do resgate por time');

// Filtro de time: aplica na requisicao, grava na sessao e sobrevive a proxima
// requisicao (duas execucoes do harness com o MESMO id de sessao).
function render_load(string $extra, int $uid, string $dumpKey = ''): array
{
    $cmd = escapeshellarg(PHP_BINARY) . ' ' . escapeshellarg(__DIR__ . DIRECTORY_SEPARATOR . '_panel_render.php')
        . ' load ' . escapeshellarg((string) $uid) . ' '
        . escapeshellarg($extra) . ' ' . escapeshellarg($dumpKey) . ' 2>&1';
    $out = [];
    $code = 0;
    exec($cmd, $out, $code);
    return [implode("\n", $out), $code];
}

putenv('PANEL_RENDER_SESSION=loadfilter' . getmypid());
[$htmlFiltro, $codeFiltro] = render_load('teams[]=1', $uid, 'load_teams');
ok($codeFiltro === 0, 'aba load (filtro): renderizou sem erro');
ok(strpos($htmlFiltro, '<!-- session:load_teams=[1] -->') !== false, 'aba load: filtro gravado na sessao');
ok(strpos($htmlFiltro, '<option value="1" selected>') !== false, 'aba load: time selecionado marcado no select');
ok(strpos($htmlFiltro, 'u:bia@claro.com') === false, 'aba load: tabela filtrada so traz o time escolhido');
[$htmlSemParam, $codeSemParam] = render_load('', $uid, 'load_teams');
ok($codeSemParam === 0, 'aba load (sem filtro na URL): renderizou sem erro');
ok(
    strpos($htmlSemParam, '<!-- session:load_teams=[1] -->') !== false
    && strpos($htmlSemParam, '<option value="1" selected>') !== false,
    'aba load: estado do filtro sobrevive a proxima requisicao'
);
[$htmlLimpo, $codeLimpo] = render_load('teams=', $uid, 'load_teams');
ok($codeLimpo === 0 && strpos($htmlLimpo, '<!-- session:load_teams=[] -->') !== false, 'aba load: Limpar zera o filtro na sessao');

// Aba Roteamento: distribuicao (casos global/time/operador) e tipo de atendimento.
$htmlRouting = (string) ($htmls['routing'] ?? '');
ok(strpos($htmlRouting, 'name="max_cases_per_agent"') !== false, 'aba routing: campo Casos global');
ok(strpos($htmlRouting, 'name="cases_all_teams"') !== false, 'aba routing: "fazer valer para todos os times"');
ok(strpos($htmlRouting, 'name="cases_all_members"') !== false, 'aba routing: replicar casos aos operadores do time');
ok(strpos($htmlRouting, 'name="queue_show_pos"') !== false, 'aba routing: falar (ou nao) a posicao ao tecnico');
ok(strpos($htmlRouting, 'name="kinds_all_members"') !== false, 'aba routing: replicar o tipo de atendimento');
ok(strpos($htmlRouting, 'name="member_kinds"') !== false, 'aba routing: tipo padrao dos operadores do time');
ok(strpos($htmlRouting, 'name="kinds"') !== false, 'aba routing: tipo de atendimento por operador');
ok(strpos($htmlRouting, 'linhas apartadas') !== false, 'aba routing: explica as duas linhas da fila');
ok(strpos($htmlRouting, 'value="queue_expire"') !== false, 'aba routing: Encerrar espera do chat na fila');
ok(
    strpos($htmlRouting, 'name="form_window_margin_min"') !== false,
    'aba routing: margem de fim de janela do formulario'
);
ok(
    substr_count($htmlRouting, 'name="form_window_margin_min"') >= 2,
    'aba routing: margem global + por time (linha do time)'
);

// Endereço do PRÓPRIO bot: o engine publica em bot_settings e o painel RECUSA
// cadastrá-lo como operador (o caso voltaria para quem só repassa).
$pdo->exec(
    "INSERT INTO bot_settings (key, value) VALUES ('bot_own_address', 'u:bot@claro.com')
     ON CONFLICT (key) DO UPDATE SET value = excluded.value"
);
ok(bot_own_address($pdo) === 'u:bot@claro.com', 'aba teams: endereco do bot publicado pelo engine');

/** Renderiza UMA pagina com POST simulado (argv[5] do harness). */
function render_post(string $page, int $uid, string $post): array
{
    $cmd = escapeshellarg(PHP_BINARY) . ' ' . escapeshellarg(__DIR__ . DIRECTORY_SEPARATOR . '_panel_render.php')
        . ' ' . escapeshellarg($page) . ' ' . escapeshellarg((string) $uid) . ' '
        . escapeshellarg('') . ' ' . escapeshellarg('') . ' ' . escapeshellarg($post) . ' 2>&1';
    $out = [];
    $code = 0;
    exec($cmd, $out, $code);
    return [implode("\n", $out), $code];
}

[$htmlBot, $codeBot] = render_post(
    'teams', $uid, 'action=add_member&team_id=1&address=u:bot@claro.com&member_name=Bot'
);
ok($codeBot === 0, 'aba teams (POST): renderizou sem erro fatal');
ok(strpos($htmlBot, 'PRÓPRIO bot') !== false, 'aba teams: recusa cadastrar o endereco do proprio bot');
ok(
    (int) $pdo->query("SELECT COUNT(*) FROM team_members WHERE address = 'u:bot@claro.com'")->fetchColumn() === 0,
    'aba teams: o bot nao entra em team_members'
);

[$htmlNovo, $codeNovo] = render_post(
    'teams', $uid, 'action=add_member&team_id=1&address=u:novo@claro.com&member_name=Novo'
);
ok($codeNovo === 0, 'aba teams (POST): operador comum segue aceito');
ok(
    (int) $pdo->query("SELECT COUNT(*) FROM team_members WHERE address = 'u:novo@claro.com'")->fetchColumn() === 1,
    'aba teams: operador comum e cadastrado normalmente'
);

@unlink($path);
exit($failed === 0 ? 0 : 1);