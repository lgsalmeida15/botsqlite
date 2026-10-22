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
// Evento com autor gravado pelo bot (coluna AUTOR da propria aba Atividade).
$pdo->exec("INSERT INTO bot_events (chat_id, queue_address, reason, detail, author_addr, author_name) VALUES (10, 'h:q1', 'classified', 'apoio', 'u:tec@claro.com', 'Tec Um')");
$pdo->exec("INSERT INTO pending_routes (kind, chat_id, queue_address, status, detail) VALUES ('chat', 10, 'h:q1', 'failed', 'tentativas esgotadas; fallback=close')");
$pdo->exec("INSERT INTO delivery_attempts (kind, chat_id, op, target, result) VALUES ('chat', 10, 'move_chat', 'u:ana@claro.com', 'ok')");
$pdo->exec("INSERT INTO route_assignments (kind, chat_id, queue_address, team_id, assignee, case_key) VALUES ('form', 99, 'h:q1', 1, 'u:ana@claro.com', 'a:u:tec@claro.com')");
$pdo->exec("INSERT INTO route_assignments (kind, chat_id, queue_address, team_id, assignee, case_key) VALUES ('chat', 10, 'h:q1', 1, 'u:ana@claro.com', 'a:u:tec@claro.com')");
// Uma rota esperando: exercita o card "Fila de espera (agora)" (posicao por tipo+time).
$pdo->exec("INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status, detail) VALUES ('chat', 12, 'h:q1', 1, 'waiting', 'na fila')");
// Formulario esperando: exercita a fila de atendimento mostrando tambem
// formulario (nao so chat) e a coluna Categoria fixa "Validação". h:q1 tem
// queue_load fresco com pending_forms=2 (ver abaixo) — este formulario conta
// como "confirmado pelo TOA" (rn_form=1 <= 2).
$pdo->exec("INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status, detail) VALUES ('form', 13, 'h:q1', 1, 'waiting', 'formulario na fila')");
// Formulario ORFAO: fila 'h:velha' so tem leitura VELHA do TOA (10 min atras,
// fora da janela de frescor) — tem que ficar de fora da fila de atendimento,
// mesma regra do card FILA FORMS (nao conta pending_routes cru).
$pdo->exec("INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status, detail) VALUES ('form', 14, 'h:velha', 1, 'waiting', 'formulario orfao havia semanas')");
// Fila 'h:q1b' com leitura FRESCA mas so 1 vaga confirmada (pending_forms=1) e
// 2 formularios candidatos: um antigo (chat_id=20, inserido primeiro = id
// menor) e um recente (chat_id=21, id maior). So o RECENTE pode entrar no
// corte — contar do mais antigo pra frente garantiria vaga pro orfao.
$pdo->exec("INSERT INTO queues (address, name, enabled) VALUES ('h:q1b', 'Fila 1b', 1)");
$pdo->exec("INSERT INTO team_queues (team_id, queue_address) VALUES (1, 'h:q1b')");
$pdo->exec("INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status, detail) VALUES ('form', 20, 'h:q1b', 1, 'waiting', 'formulario antigo na h:q1b')");
$pdo->exec("INSERT INTO pending_routes (kind, chat_id, queue_address, team_id, status, detail) VALUES ('form', 21, 'h:q1b', 1, 'waiting', 'formulario recente na h:q1b')");
$pdo->exec("INSERT INTO queue_load (queue_address, at, pending_forms, pending_chats, toa_ok) VALUES ('h:q1b', datetime('now'), 1, 0, 1)");
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
// Fila do TOA (card FILA FORMS do dashboard): 2 forms na fila de h:q1. A fila
// velha (10 min) nao pode entrar no total — e o que o card mostra como "-".
$pdo->exec("INSERT INTO queue_load (queue_address, at, pending_forms, pending_chats, toa_ok) VALUES ('h:q1', datetime('now'), 2, 1, 1)");
$pdo->exec("INSERT INTO queue_load (queue_address, at, pending_forms, pending_chats, toa_ok) VALUES ('h:velha', datetime('now', '-10 minutes'), 5, 0, 1)");

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

// Resgate por analista offline: coluna e alerta ficam na aba Carga (relatorio).
// O botao manual saiu: a devolucao e automatica, o painel so configura a regua.
ok(strpos($htmlCarga, '<th>Offline há</th>') !== false, 'aba load: coluna Offline há');
ok(strpos($htmlCarga, 'analista(s) <strong>offline</strong> com caso aberto') !== false, 'aba load: alerta de offline com caso aberto');
ok(strpos($htmlCarga, 'A devolução é automática') !== false, 'aba load: alerta diz que a devolucao e automatica');
ok(strpos($htmlCarga, 'value="requeue_offline"') === false, 'aba load: botao Devolver a fila removido');
ok(strpos($htmlCarga, 'Devolver à fila') === false, 'aba load: rotulo do botao manual removido');
ok(strpos($htmlCarga, 'Leitura</th><th></th>') === false, 'aba load: cabecalho da tabela sem coluna extra');
// Tabela alinhada: 11 <th> no cabecalho = 11 <td> em cada linha (a "Leitura"
// e a ultima coluna depois de sair o botao manual).
$iniTh = strpos($htmlCarga, '<th>Analista</th>');
$fimTh = $iniTh === false ? false : strpos($htmlCarga, '</tr></thead>', $iniTh);
ok(
    $iniTh !== false && $fimTh !== false && substr_count(substr($htmlCarga, $iniTh, $fimTh - $iniTh), '<th') === 11,
    'aba load: 11 colunas no cabecalho da tabela'
);
$iniTd = $posAna === false ? false : strrpos(substr($htmlCarga, 0, $posAna), '<tr');
$fimTd = $iniTd === false ? false : strpos($htmlCarga, '</tr>', $iniTd);
ok(
    $iniTd !== false && $fimTd !== false && substr_count(substr($htmlCarga, $iniTd, $fimTd - $iniTd), '<td') === 11,
    'aba load: 11 celulas por linha (alinhado ao cabecalho)'
);

// Aba Dashboard: cards do topo, donut de operadores (online x offline), tabela
// de analistas e o filtro por nome — SVG puro, sem bibliotecas externas.
$htmlDash = (string) ($htmls['dashboard'] ?? '');
ok(strpos($htmlDash, 'TOTAL CHATS/DIA') !== false, 'aba dashboard: card TOTAL CHATS/DIA');
ok(strpos($htmlDash, 'TOTAL FORMS/DIA') !== false, 'aba dashboard: card TOTAL FORMS/DIA');
ok(strpos($htmlDash, '>FILA DE CHAT<') !== false, 'aba dashboard: card FILA DE CHAT');
ok(strpos($htmlDash, '>FILA FORMS<') !== false, 'aba dashboard: card FILA FORMS');
// Card FILA FORMS: fonte e queue_load (fila do TOA), nao pending_routes.
// h:q1 (2, fresca) + h:q1b (1, fresca) somam 3; h:velha (5, 10 min) fica de fora.
ok(
    preg_match('/badge bg-info text-dark"[^>]*>3</', $htmlDash) === 1,
    'aba dashboard: FILA FORMS soma so a coleta fresca (3, nao 7 nem 8)'
);
ok(
    strpos($htmlDash, 'Formulários presentes na fila') !== false,
    'aba dashboard: FILA FORMS explica que vem da fila do TOA (agora como title do badge)'
);
ok(strpos($htmlDash, 'class="dash-donut"') !== false, 'aba dashboard: donut de operadores (SVG puro)');
// Legenda do donut foi removida de proposito (pedido de UI): so sobra o SVG com
// <title> por fatia (hover) — sem lista de cores ao lado.
ok(strpos($htmlDash, 'class="dash-legend"') === false, 'aba dashboard: sem legenda em lista ao lado do donut');
ok(strpos($htmlDash, '<span class="dot"') === false, 'aba dashboard: sem bolinhas de cor da legenda antiga');
ok(strpos($htmlDash, 'online x offline') !== false, 'aba dashboard: rotulo do donut');
ok(strpos($htmlDash, 'Analistas') !== false, 'aba dashboard: tabela de analistas');
ok(strpos($htmlDash, 'name="nome"') !== false, 'aba dashboard: filtro por analista (nome/endereco)');
ok(strpos($htmlDash, 'Ex.: Ana') !== false, 'aba dashboard: exemplo do filtro por nome');
ok(strpos($htmlDash, 'Devolver') === false, 'aba dashboard: sem botao manual de devolucao');
// Filtro "mostrar inativos e fora da janela": por padrao desmarcado, so o
// ativo (Ana) aparece na tabela — o inativo no roteamento fica de fora.
ok(strpos($htmlDash, 'name="mostrar_inativos"') !== false, 'aba dashboard: filtro de inativos/fora da janela presente');
ok(strpos($htmlDash, 'name="mostrar_inativos" value="1" checked') === false, 'aba dashboard: filtro de inativos vem desmarcado por padrao');
ok(
    strpos($htmlDash, 'u:ana@claro.com') !== false && strpos($htmlDash, 'u:inativo@claro.com') === false,
    'aba dashboard: tabela de analistas oculta o inativo no roteamento por padrao'
);

// Nova tabela "Fila de atendimento — agora": chat_id=10 esta com o bot (status
// awaiting_reply, sem rota 'waiting' — a dele e 'failed', classification='TESTE');
// chat_id=12 tem uma pending_route 'waiting' do time 1 (sem prioridade) — e quem
// exercita a acao de priorizar; chat_id=13 e um FORMULARIO 'waiting' do mesmo
// time — exercita a visao de formulario na fila e a categoria fixa "Validação".
ok(strpos($htmlDash, 'Fila de atendimento — agora') !== false, 'aba dashboard: nova tabela de fila de atendimento');
ok(strpos($htmlDash, '>com bot<') !== false, 'aba dashboard: chat com bot aparece na fila');
ok(strpos($htmlDash, '>aguardando<') !== false, 'aba dashboard: chat aguardando roteamento aparece na fila');
ok(strpos($htmlDash, 'value="queue_prioritize"') !== false, 'aba dashboard: acao de priorizar presente na fila');
ok(strpos($htmlDash, '>Priorizar<') !== false, 'aba dashboard: botao Priorizar (sem prioridade ainda)');
ok(strpos($htmlDash, '<th>Tipo</th>') !== false, 'aba dashboard: coluna Tipo presente na fila');
ok(strpos($htmlDash, '<th>Categoria</th>') !== false, 'aba dashboard: coluna Categoria presente na fila');
ok(strpos($htmlDash, '>Formulário<') !== false, 'aba dashboard: formulário aparece na fila de atendimento');
ok(strpos($htmlDash, '>Validação<') !== false, 'aba dashboard: categoria do formulário é sempre Validação');
ok(strpos($htmlDash, '>TESTE<') !== false, 'aba dashboard: categoria do chat mostra a classification real');
ok(strpos($htmlDash, '<td>13</td>') !== false, 'aba dashboard: formulário confirmado pelo TOA (queue_load fresco) aparece');
ok(strpos($htmlDash, '<td>14</td>') === false, 'aba dashboard: formulário órfão (sem leitura fresca do TOA) não aparece');
ok(strpos($htmlDash, '<td>21</td>') !== false, 'aba dashboard: com 1 vaga confirmada e 2 candidatos, o formulário RECENTE aparece');
ok(strpos($htmlDash, '<td>20</td>') === false, 'aba dashboard: o formulário mais ANTIGO fica de fora quando só cabe 1 (corte prioriza o recente)');

// POST queue_prioritize a partir do Dashboard: route_id = id da pending_route
// do chat_id=12 (unica 'waiting' do fixture).
$ridChat12 = (int) $pdo->query("SELECT id FROM pending_routes WHERE chat_id = 12 AND status = 'waiting'")->fetchColumn();
putenv('PANEL_RENDER_POST=action=queue_prioritize&route_id=' . $ridChat12 . '&priority=1');
$cmdPrioriza = escapeshellarg(PHP_BINARY) . ' ' . escapeshellarg(__DIR__ . DIRECTORY_SEPARATOR . '_panel_render.php')
    . ' dashboard ' . $uid . ' ' . escapeshellarg('') . ' 2>&1';
$outPrioriza = [];
$codePrioriza = 0;
exec($cmdPrioriza, $outPrioriza, $codePrioriza);
putenv('PANEL_RENDER_POST');
// POST com sucesso redireciona (Post/Redirect/Get, mesmo padrao ja usado pela
// acao identica na aba Roteamento) — exit antes de renderizar qualquer pagina,
// entao $queueMsg nunca aparece nesta mesma resposta. O que da pra conferir
// aqui e so o codigo de saida e o estado persistido no banco.
ok($codePrioriza === 0, 'aba dashboard (POST priorizar): renderizou sem erro');
ok(
    (int) $pdo->query("SELECT priority FROM pending_routes WHERE id = {$ridChat12}")->fetchColumn() === 1,
    'aba dashboard: POST queue_prioritize grava priority=1 na rota'
);
// Nova renderizacao (GET): confirma que o estado persistido aparece na tabela.
[$htmlDashPriorizado] = render_dashboard('', $uid);
ok(strpos($htmlDashPriorizado, '>Remover prioridade<') !== false, 'aba dashboard: botao vira Remover prioridade apos priorizar');
ok(strpos($htmlDashPriorizado, '>prioritário<') !== false, 'aba dashboard: badge prioritario aparece apos priorizar');

/** Renderiza o dashboard com filtros extras (argv[3] do harness). */
function render_dashboard(string $extra, int $uid): array
{
    $cmd = escapeshellarg(PHP_BINARY) . ' ' . escapeshellarg(__DIR__ . DIRECTORY_SEPARATOR . '_panel_render.php')
        . ' dashboard ' . escapeshellarg((string) $uid) . ' ' . escapeshellarg($extra) . ' 2>&1';
    $out = [];
    $code = 0;
    exec($cmd, $out, $code);
    return [implode("\n", $out), $code];
}

// Filtro por nome: volta no campo e restringe a tabela (a leitura do Inativo sai).
[$htmlDashNome, $codeDashNome] = render_dashboard('nome=Ana', $uid);
ok($codeDashNome === 0, 'aba dashboard (filtro por nome): renderizou sem erro');
ok(strpos($htmlDashNome, 'value="Ana"') !== false, 'aba dashboard: nome digitado volta no campo');
ok(
    strpos($htmlDashNome, 'u:ana@claro.com') !== false && strpos($htmlDashNome, 'u:inativo@claro.com') === false,
    'aba dashboard: filtro por nome restringe a tabela ao analista'
);

// Marcando "mostrar inativos e fora da janela": o inativo volta a aparecer.
[$htmlDashTodos, $codeDashTodos] = render_dashboard('mostrar_inativos=1', $uid);
ok($codeDashTodos === 0, 'aba dashboard (mostrar inativos): renderizou sem erro');
ok(strpos($htmlDashTodos, 'name="mostrar_inativos" value="1" checked') !== false, 'aba dashboard: filtro marcado volta marcado');
ok(
    strpos($htmlDashTodos, 'u:ana@claro.com') !== false && strpos($htmlDashTodos, 'u:inativo@claro.com') !== false,
    'aba dashboard: com o filtro marcado o inativo no roteamento volta a aparecer'
);
ok(strpos($htmlDashTodos, '>inativo</span>') !== false, 'aba dashboard: selo de inativo no roteamento na tabela de analistas');

// Aba Atividade: coluna AUTOR (nome do autor do chat/form na movimentacao).
$htmlAtiv = (string) ($htmls['activity'] ?? '');
ok(strpos($htmlAtiv, '<th>Autor</th>') !== false, 'aba activity: coluna AUTOR no cabecalho');
ok(strpos($htmlAtiv, 'Tec Um') !== false, 'aba activity: nome do autor exibido');
ok(
    strpos($htmlAtiv, '<code class="small">u:tec@claro.com</code>') !== false,
    'aba activity: endereco do autor junto do nome'
);

// Outro evento sem autor (para o filtro ter o que excluir).
$pdo->exec("INSERT INTO bot_events (chat_id, queue_address, reason, detail) VALUES (10, 'h:q1', 'not_new', 'sem identidade')");

function render_activity(string $extra, int $uid): array
{
    $cmd = escapeshellarg(PHP_BINARY) . ' ' . escapeshellarg(__DIR__ . DIRECTORY_SEPARATOR . '_panel_render.php')
        . ' activity ' . escapeshellarg((string) $uid) . ' ' . escapeshellarg($extra) . ' 2>&1';
    $out = [];
    $code = 0;
    exec($cmd, $out, $code);
    return [implode("\n", $out), $code];
}

// So o corpo da tabela: o <select> de eventos lista TODOS os reasons (inclusive
// "not_new") como <option>, entao checar a pagina inteira daria falso negativo.
function activity_tbody(string $html): string
{
    return preg_match('/<tbody>(.*?)<\/tbody>/s', $html, $m) ? $m[1] : '';
}

// Filtro por autor: volta no campo, restringe a tabela e pausa o auto-refresh.
[$htmlAtivAutor, $codeAtivAutor] = render_activity('autor=Tec', $uid);
ok($codeAtivAutor === 0, 'aba activity (filtro por autor): renderizou sem erro');
ok(strpos($htmlAtivAutor, 'value="Tec"') !== false, 'aba activity: autor digitado volta no campo');
$tbodyAtivAutor = activity_tbody($htmlAtivAutor);
ok(
    strpos($tbodyAtivAutor, 'Tec Um') !== false && strpos($tbodyAtivAutor, 'já atendido') === false,
    'aba activity: filtro por autor restringe a tabela ao evento com identidade'
);
ok(strpos($htmlAtivAutor, 'data-auto-refresh') === false, 'aba activity: filtro por autor pausa o auto-refresh');

// Sem urlencode: '@' nao precisa (so & = quebram parse_str), e o % do urlencode
// vira espaco no escapeshellarg do Windows (protecao contra expansao do cmd.exe).
[$htmlAtivAutorAddr] = render_activity('autor=tec@claro', $uid);
ok(
    strpos(activity_tbody($htmlAtivAutorAddr), 'Tec Um') !== false,
    'aba activity: filtro por autor tambem acha pelo endereco'
);

// Filtro por chat_id: exato, so digitos, restringe a tabela a esse chat.
[$htmlAtivChat, $codeAtivChat] = render_activity('chat_id=11', $uid);
ok($codeAtivChat === 0, 'aba activity (filtro por chat_id): renderizou sem erro');
ok(strpos($htmlAtivChat, 'value="11"') !== false, 'aba activity: chat_id digitado volta no campo');
$tbodyAtivChat = activity_tbody($htmlAtivChat);
ok(
    strpos($tbodyAtivChat, 'detalhe bem longo') !== false && strpos($tbodyAtivChat, 'Tec Um') === false,
    'aba activity: filtro por chat_id restringe a tabela ao chat informado'
);
ok(strpos($htmlAtivChat, 'data-auto-refresh') === false, 'aba activity: filtro por chat_id pausa o auto-refresh');

// Lixo (nao-digito) no chat_id e ignorado, sem quebrar a pagina nem filtrar.
[$htmlAtivChatLixo, $codeAtivChatLixo] = render_activity('chat_id=' . rawurlencode('abc'), $uid);
ok($codeAtivChatLixo === 0, 'aba activity (chat_id invalido): renderizou sem erro');
ok(
    strpos(activity_tbody($htmlAtivChatLixo), 'Tec Um') !== false,
    'aba activity: chat_id nao numerico e ignorado (sem filtrar)'
);

// Ajustes do resgate: global no Roteamento, por time na aba Times (consolidado
// la junto com estrategia, capacidade, demanda e janelas).
$htmlRoutingOff = (string) ($htmls['routing'] ?? '');
$htmlTeamsOff = (string) ($htmls['teams'] ?? '');
ok(strpos($htmlRoutingOff, 'name="offline_requeue_enabled"') !== false, 'aba routing: liga/desliga do resgate');
ok(strpos($htmlRoutingOff, 'name="offline_requeue_sec"') !== false, 'aba routing: segundos de offline para devolver (global)');
ok(strpos($htmlTeamsOff, 'name="offline_requeue_sec"') !== false, 'aba teams: segundos de offline para devolver (por time)');
ok(strpos($htmlTeamsOff, '0 = este time nao devolve') !== false, 'aba teams: explica o 0 do resgate por time');

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

// Aba Roteamento: casos/posicao globais. Aba Times: distribuicao por time e
// por analista (capacidade, demanda atendida, janelas) - consolidado la.
$htmlRouting = (string) ($htmls['routing'] ?? '');
$htmlTeams = (string) ($htmls['teams'] ?? '');
ok(strpos($htmlRouting, 'name="max_cases_per_agent"') !== false, 'aba routing: campo Casos global');
ok(strpos($htmlRouting, 'name="cases_all_teams"') !== false, 'aba routing: "fazer valer para todos os times"');
ok(strpos($htmlTeams, 'name="cases_all_members"') !== false, 'aba teams: replicar casos aos operadores do time');
ok(strpos($htmlRouting, 'name="queue_show_pos"') !== false, 'aba routing: falar (ou nao) a posicao ao tecnico');
ok(strpos($htmlTeams, 'name="kinds_all_members"') !== false, 'aba teams: replicar o tipo de atendimento');
ok(strpos($htmlTeams, 'name="member_kinds"') !== false, 'aba teams: demanda padrao do time');
ok(strpos($htmlTeams, 'name="kinds"') !== false, 'aba teams: tipo de atendimento por operador');
ok(strpos($htmlRouting, 'linhas apartadas') !== false, 'aba routing: explica as duas linhas da fila');
ok(strpos($htmlRouting, 'value="queue_expire"') !== false, 'aba routing: Encerrar espera do chat na fila');
ok(
    strpos($htmlRouting, 'name="form_window_margin_min"') !== false,
    'aba routing: margem de fim de janela do formulario (global)'
);
ok(
    strpos($htmlTeams, 'name="form_window_margin_min"') !== false,
    'aba teams: margem de fim de janela do formulario (por time)'
);
ok(strpos($htmlTeams, 'name="sch_kind[]"') !== false, 'aba teams: editor de janelas (turno/descanso)');
ok(strpos($htmlTeams, 'name="sch_days[]"') !== false, 'aba teams: bitmask de dias da janela');

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