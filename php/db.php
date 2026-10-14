<?php
declare(strict_types=1);

/**
 * Banco do painel: SQLite (padrao) e MySQL 8 (secao 5 do `sqliteformysql.md`).
 *
 * O painel inteiro (`index.php`, `auth.php`, `pages/*`, `bin/create-user.php`)
 * continua escrito no dialeto SQLite: quem traduz e o `BotPDO`, no choke point
 * de `prepare()`/`query()`/`exec()`. Duas pecas:
 *
 *  - `bot_sql_traduz()`: as regras da 5.1 (familia de tempo, upsert,
 *    `INSERT OR IGNORE`, `sqlite_master`, `PRAGMA`, `||`, DDL, indices parciais,
 *    FK inline e a colacao `utf8mb4_bin` das colunas de igualdade);
 *  - `BotStatement::execute()`: aplica o "plano" de parametros que a traducao
 *    montou (instante UTC calculado aqui e modificador de data virando
 *    segundos), porque o `prepare()` nao recebe valores.
 *
 * Com o backend `sqlite` a traducao e identidade: nada aqui muda o
 * comportamento atual do painel.
 */

/** Config: ambiente primeiro (o runner de teste exporta), depois o `.env`. */
function bot_db_config(string $chave, ?string $padrao = null): ?string
{
    $valor = getenv($chave);
    if ($valor !== false && $valor !== '') {
        return $valor;
    }
    if (function_exists('bot_env')) {
        return bot_env($chave, $padrao);
    }
    return $padrao;
}

/** `MYSQL_TEST=1`: modo de teste (backend MySQL obrigatorio, base com "test"). */
function bot_db_test_mode(): bool
{
    $valor = getenv('MYSQL_TEST');
    if ($valor === false) {
        return false;
    }
    return !in_array(strtolower(trim($valor)), ['', '0', 'false', 'no', 'off'], true);
}

/** Backend do banco: `sqlite` (padrao) ou `mysql`. `MYSQL_TEST=1` liga o MySQL. */
function bot_db_backend(): string
{
    if (bot_db_test_mode()) {
        return 'mysql';
    }
    return strtolower(trim((string) bot_db_config('DB_BACKEND', 'sqlite'))) === 'mysql'
        ? 'mysql'
        : 'sqlite';
}

/** Credenciais do MySQL com as mesmas chaves do `.env` que o bot Python usa. */
function bot_mysql_config(): array
{
    $db = '';
    $faltando = [];
    if (bot_db_test_mode()) {
        // Trava dura: em modo de teste a base vem de `MYSQL_TEST_DATABASE` e
        // precisa parecer base de teste — o painel nunca aponta para producao
        // por um `MYSQL_TEST=1` esquecido no ambiente.
        $db = trim((string) getenv('MYSQL_TEST_DATABASE'));
        if ($db === '') {
            throw new RuntimeException('MYSQL_TEST=1 exige MYSQL_TEST_DATABASE (base de teste).');
        }
        if (stripos($db, 'test') === false) {
            throw new RuntimeException(
                'MYSQL_TEST recusa a base "' . $db . '": o nome tem de conter "test".'
            );
        }
    }
    $cfg = [
        'host' => trim((string) bot_db_config('IP_MYSQL', '')),
        'port' => trim((string) bot_db_config('PORTA_MYSQL', '3306')),
        'user' => trim((string) bot_db_config('USUARIO_MYSQL', '')),
        'pass' => (string) bot_db_config('SENHA_MYSQL', ''),
        'db' => $db !== '' ? $db : trim((string) bot_db_config('DATABASE_MYSQL', '')),
    ];
    if ($cfg['port'] === '') {
        $cfg['port'] = '3306';
    }
    $chaves = ['host' => 'IP_MYSQL', 'user' => 'USUARIO_MYSQL'];
    foreach ($chaves as $campo => $chave) {
        if ($cfg[$campo] === '') {
            $faltando[] = $chave;
        }
    }
    if ($cfg['db'] === '') {
        $faltando[] = bot_db_test_mode() ? 'MYSQL_TEST_DATABASE' : 'DATABASE_MYSQL';
    }
    if ($faltando) {
        throw new RuntimeException(
            'DB_BACKEND=mysql mas o .env esta sem valor em: ' . implode(', ', array_unique($faltando))
        );
    }
    return $cfg;
}

/** Handshake da sessao MySQL (espelho de `sqlcompat.connect_mysql`). */
function bot_mysql_handshake(): array
{
    return [
        "SET time_zone = '+00:00'",
        'SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED',
        'SET SESSION innodb_lock_wait_timeout = 5',
        "SET SESSION sql_mode = ''",
    ];
}

/**
 * Colacao para `LIKE`/busca por nome: o SQLite e case-insensitive e as colunas
 * de chave nascem `utf8mb4_bin` no MySQL — sem o `COLLATE` a busca perderia
 * resultado com maiuscula/minuscula.
 */
function bot_like_ci(string $expressao): string
{
    return bot_db_backend() === 'mysql'
        ? $expressao . ' COLLATE utf8mb4_0900_ai_ci'
        : $expressao;
}

/** Carimbo UTC no mesmo formato textual que o SQLite gravava (5.4). */
function bot_utc_stamp(?int $deslocamentoSegundos = null): string
{
    return gmdate('Y-m-d H:i:s', time() + (int) $deslocamentoSegundos);
}

// ------------------------------------------------------------------- mascara

/**
 * Copia do SQL com literais/comentarios virados espaco, do mesmo tamanho.
 *
 * Manter o comprimento e o que permite achar `datetime(`, `?`, `||` e as
 * virgulas de nivel 0 por posicao e ler o texto ORIGINAL daquele trecho.
 */
function bot_sql_mask(string $sql): string
{
    $saida = $sql;
    $n = strlen($sql);
    $i = 0;
    while ($i < $n) {
        $c = $sql[$i];
        if ($c === "'" || $c === '"' || $c === '`') {
            $j = $i + 1;
            while ($j < $n) {
                if ($sql[$j] === '\\' && ($c === "'" || $c === '"')) {
                    $j += 2;
                    continue;
                }
                if ($sql[$j] === $c) {
                    if ($j + 1 < $n && $sql[$j + 1] === $c) {   // '' / "" escapado
                        $j += 2;
                        continue;
                    }
                    break;
                }
                $j++;
            }
            for ($k = $i + 1; $k < min($j, $n); $k++) {
                if ($saida[$k] !== "\n") {
                    $saida[$k] = ' ';
                }
            }
            $i = $j + 1;
        } elseif ($c === '-' && $i + 1 < $n && $sql[$i + 1] === '-') {
            $j = strpos($sql, "\n", $i);
            $j = $j === false ? $n : $j;
            for ($k = $i; $k < $j; $k++) {
                $saida[$k] = ' ';
            }
            $i = $j;
        } elseif ($c === '/' && $i + 1 < $n && $sql[$i + 1] === '*') {
            $j = strpos($sql, '*/', $i + 2);
            $j = $j === false ? $n : $j + 2;
            for ($k = $i; $k < $j; $k++) {
                if ($saida[$k] !== "\n") {
                    $saida[$k] = ' ';
                }
            }
            $i = $j;
        } else {
            $i++;
        }
    }
    return $saida;
}

/** Tira espaco/comentario do comeco do comando (o cabecalho do `schema.sql`). */
function bot_sql_sem_comentario(string $sql): string
{
    while (true) {
        $texto = ltrim($sql);
        if (strpos($texto, '--') === 0) {
            $quebra = strpos($texto, "\n");
            if ($quebra === false) {
                return '';
            }
            $sql = substr($texto, $quebra + 1);
            continue;
        }
        if (strpos($texto, '/*') === 0) {
            $fim = strpos($texto, '*/');
            if ($fim === false) {
                return '';
            }
            $sql = substr($texto, $fim + 2);
            continue;
        }
        return $texto;
    }
}

/** Indice do `)` que fecha o `(` de `$abre` (a mascara esconde os literais). */
function bot_sql_fecha_paren(string $mask, int $abre): int
{
    $profundidade = 0;
    $n = strlen($mask);
    for ($i = $abre; $i < $n; $i++) {
        if ($mask[$i] === '(') {
            $profundidade++;
        } elseif ($mask[$i] === ')') {
            $profundidade--;
            if ($profundidade === 0) {
                return $i;
            }
        }
    }
    throw new RuntimeException('parentese nao fechado: ' . substr($mask, 0, 80));
}

/** Separa os itens/argumentos de nivel 0 (virgulas de fora dos parenteses). */
function bot_sql_split_args(string $texto, string $mask): array
{
    $partes = [];
    $inicio = 0;
    $profundidade = 0;
    $n = min(strlen($mask), strlen($texto));
    for ($i = 0; $i < $n; $i++) {
        $c = $mask[$i];
        if ($c === '(') {
            $profundidade++;
        } elseif ($c === ')') {
            $profundidade--;
        } elseif ($c === ',' && $profundidade === 0) {
            $partes[] = substr($texto, $inicio, $i - $inicio);
            $inicio = $i + 1;
        }
    }
    $partes[] = substr($texto, $inicio);
    return $partes;
}

/** Quantos `?` ha fora de literal/comentario. */
function bot_sql_conta_marcadores(string $texto): int
{
    return substr_count(bot_sql_mask($texto), '?');
}

const BOT_SQL_SEGUNDOS_POR_UNIDADE = [
    'second' => 1,
    'seconds' => 1,
    'minute' => 60,
    'minutes' => 60,
    'hour' => 3600,
    'hours' => 3600,
    'day' => 86400,
    'days' => 86400,
    'week' => 604800,
    'weeks' => 604800,
];

/**
 * `'-3 hours'` / `'-300 seconds'` / `'-1 day'` -> segundos (modificador SQLite).
 *
 * Falha alto para o que nao for modificador constante (`'-03:00'` e offset
 * numerico, nao modificador): o painel so usa modificador nos filtros de data.
 */
function bot_sql_modificador_segundos($modificador): int
{
    $texto = $modificador === null ? '' : strtolower(trim((string) $modificador));
    if (preg_match('/^[+-]?\d+(?:\.\d+)?$/', $texto) === 1) {
        return (int) round((float) $texto);
    }
    $padrao = '/^(?P<sinal>[+-])?\s*(?P<valor>\d+(?:\.\d+)?)\s*(?P<unidade>[a-z]+)$/';
    if (preg_match($padrao, $texto, $m) !== 1 || !isset(BOT_SQL_SEGUNDOS_POR_UNIDADE[$m['unidade']])) {
        throw new RuntimeException(
            'modificador de tempo nao suportado: ' . var_export($modificador, true)
        );
    }
    $total = (float) $m['valor'] * BOT_SQL_SEGUNDOS_POR_UNIDADE[$m['unidade']];
    return (int) round($m['sinal'] === '-' ? -$total : $total);
}

// --------------------------------------------------------- familia de tempo

const BOT_SQL_OFFSET_LITERAL = "/^'[+-]\\d{1,2}:\\d{2}'$/";

/** Acrescenta ao plano os `?` comuns de um trecho ja emitido (ordem preservada). */
function bot_sql_plano_comuns(string $texto, array &$plano, int &$k): void
{
    $quantos = bot_sql_conta_marcadores($texto);
    for ($i = 0; $i < $quantos; $i++) {
        $plano[] = ['comum', $k];
        $k++;
    }
}

/** Carimbo UTC como literal SQL (o instante vem da aplicacao, regras 3 e 4). */
function bot_sql_literal_agora(?string $modificador = null): string
{
    $segundos = $modificador === null ? 0 : bot_sql_modificador_segundos($modificador);
    return "'" . bot_utc_stamp($segundos) . "'";
}

/**
 * Traduz a familia `datetime()`/`julianday()` de um comando.
 *
 * Devolve `[sql, plano]`; o plano tem uma entrada por `?` do SQL traduzido (na
 * ordem), porque `datetime(<coluna>, ?)` so vira `INTERVAL` **no execute**, com o
 * valor em maos. Literais (`datetime('now')`, `datetime('now', '-1 day')`) entram
 * como texto e nao mexem na contagem de parametros.
 */
function bot_sql_tempo(string $sql): array
{
    $plano = [];
    $k = 0;
    $texto = bot_sql_tempo_em($sql, $plano, $k);
    return [$texto, $plano];
}

/**
 * Passe unico da familia de tempo.
 *
 * A leitura esquerda->direita e o que ordena o consumo dos parametros: um mesmo
 * comando pode ter mais de uma data (o filtro "de/ate" tem duas).
 */
function bot_sql_tempo_em(string $seg, array &$plano, int &$k): string
{
    $mask = bot_sql_mask($seg);
    $partes = [];
    $i = 0;
    $n = strlen($seg);
    $padrao = '/\b(datetime|julianday)\s*\(/i';
    while ($i < $n && preg_match($padrao, $mask, $m, PREG_OFFSET_CAPTURE, $i) === 1) {
        $ini = (int) $m[0][1];
        $abre = strpos($mask, '(', $ini + strlen($m[1][0]));
        $fecha = bot_sql_fecha_paren($mask, $abre);
        $antes = substr($seg, $i, $ini - $i);
        if (strtolower($m[1][0]) === 'julianday') {
            [$iniSpan, $fimSpan, $a, $b] = bot_sql_julianday_span($seg, $mask, $ini, $abre, $fecha);
            $antes = substr($seg, $i, $iniSpan - $i);
            bot_sql_plano_comuns($antes, $plano, $k);
            $partes[] = $antes;
            $partes[] = bot_sql_julianday_texto($a, $b);
            $i = $fimSpan;
            continue;
        }
        bot_sql_plano_comuns($antes, $plano, $k);
        $partes[] = $antes;
        $partes[] = bot_sql_datetime_args(substr($seg, $abre + 1, $fecha - $abre - 1), $plano, $k);
        $i = $fecha + 1;
    }
    $resto = substr($seg, $i);
    bot_sql_plano_comuns($resto, $plano, $k);
    $partes[] = $resto;
    return implode('', $partes);
}

/** Conteudo de um `datetime(...)` (os parenteses ja ficaram fora). */
function bot_sql_datetime_args(string $args, array &$plano, int &$k): string
{
    $texto = trim($args);
    if (strtolower(substr($texto, 0, 5)) === "'now'") {
        $resto = ltrim(substr($texto, 5));
        if ($resto === '') {
            return bot_sql_literal_agora();
        }
        if ($resto[0] !== ',') {
            throw new RuntimeException('datetime() mal formado: ' . var_export($args, true));
        }
        return bot_sql_datetime_modificador(trim(substr($resto, 1)), $plano, $k);
    }
    if (strpos(bot_sql_mask($args), ',') !== false) {
        return bot_sql_datetime_offset($args, $plano, $k);
    }
    // `datetime(<coluna>)`: identidade — a coluna ja esta no formato canonico.
    return '(' . trim(bot_sql_tempo_em($args, $plano, $k)) . ')';
}

/** `datetime('now', <modificador>)` -> literal com o instante calculado aqui. */
function bot_sql_datetime_modificador(string $mod, array &$plano, int &$k): string
{
    if (preg_match("/^\\s*'([^']*)'\\s*$/", $mod, $m) === 1) {
        return bot_sql_literal_agora($m[1]);
    }
    if (trim(bot_sql_mask($mod)) === '?') {
        $plano[] = ['agora', $k];      // resolvido no execute
        $k++;
        return '?';
    }
    throw new RuntimeException(
        'datetime(..., <modificador>) nao suportado (use literal ou `?`): ' . var_export($mod, true)
    );
}

/**
 * `datetime(<coluna>, <modificador|offset>)`.
 *
 * O painel passa o modificador por parametro (`'-3 hours'`) e o valor so existe
 * no `execute`: o SQL sai `DATE_ADD(<coluna>, INTERVAL ? SECOND)` e o plano marca
 * a posicao para virar segundos. Offset literal (`'-03:00'`) vira `CONVERT_TZ`.
 */
function bot_sql_datetime_offset(string $args, array &$plano, int &$k): string
{
    $mask = bot_sql_mask($args);
    $partes = bot_sql_split_args($args, $mask);
    if (count($partes) !== 2) {
        throw new RuntimeException(
            'datetime() com ' . count($partes) . ' argumentos: ' . var_export($args, true)
        );
    }
    $coluna = trim(bot_sql_tempo_em($partes[0], $plano, $k));
    $off = trim($partes[1]);
    if (trim(bot_sql_mask($off)) === '?') {
        $plano[] = ['segundos', $k];
        $k++;
        return 'DATE_ADD(' . $coluna . ', INTERVAL ? SECOND)';
    }
    if (preg_match(BOT_SQL_OFFSET_LITERAL, $off) === 1) {
        return "CONVERT_TZ(" . $coluna . ", '+00:00', " . $off . ')';
    }
    if (preg_match("/^'([^']*)'$/", $off, $m) === 1) {
        $segundos = bot_sql_modificador_segundos($m[1]);
        return 'DATE_ADD(' . $coluna . ', INTERVAL ' . $segundos . ' SECOND)';
    }
    throw new RuntimeException(
        "datetime(<coluna>, ...) so aceita modificador ('-3 hours') ou offset ('-03:00'): "
        . var_export($args, true)
    );
}

/** `(julianday(a) - julianday(b)) * 86400`: intervalo a trocar e as duas pontas. */
function bot_sql_julianday_span(string $seg, string $mask, int $ini, int $abre, int $fecha): array
{
    // `\G` (e nao `^`): com `$offset` no `preg_match`, o `^` continua ancorado no
    // inicio do sujeito e a busca a partir do `)` nunca casaria.
    if (preg_match('/\G\s*-\s*julianday\s*\(/i', $mask, $m, 0, $fecha + 1) !== 1) {
        throw new RuntimeException('julianday() solto, sem `- julianday(...)`: ' . substr($seg, 0, 80));
    }
    $abre2 = strpos($mask, '(', $fecha + 1);
    if ($abre2 === false) {
        throw new RuntimeException('julianday() sem parentese: ' . substr($seg, 0, 80));
    }
    $fecha2 = bot_sql_fecha_paren($mask, $abre2);
    if (preg_match('/\G\s*\)\s*\*\s*86400/i', $mask, $m2, 0, $fecha2 + 1) !== 1) {
        throw new RuntimeException(
            'julianday(): faltou `) * 86400` na diferenca: ' . substr($seg, 0, 80)
        );
    }
    $inicio = $ini;
    $j = $ini - 1;
    while ($j >= 0 && ctype_space($mask[$j])) {
        $j--;
    }
    if ($j >= 0 && $mask[$j] === '(') {
        $inicio = $j;
    }
    return [
        $inicio,
        $fecha2 + 1 + strlen($m2[0]),
        trim(substr($seg, $abre + 1, $fecha - $abre - 1)),
        trim(substr($seg, $abre2 + 1, $fecha2 - $abre2 - 1)),
    ];
}

/** `(julianday(a) - julianday(b)) * 86400` -> `TIMESTAMPDIFF(SECOND, b, a)`. */
function bot_sql_julianday_texto(string $a, string $b): string
{
    $lado = static function (string $expr): string {
        return strtolower(trim($expr)) === "'now'" ? bot_sql_literal_agora() : $expr;
    };
    return 'TIMESTAMPDIFF(SECOND, ' . $lado($b) . ', ' . $lado($a) . ')';
}

// ------------------------------------------------------------------ DDL

const BOT_SQL_DDL = '/^\s*(?:CREATE|ALTER|DROP|RENAME)\b/i';
const BOT_SQL_COLACAO_BIN = 'COLLATE utf8mb4_bin';
// 5.5(c): colunas de igualdade que **nao** sao chave (as chaves entram pela
// propria declaracao; a coluna filha de FK entra porque o pai e sempre chave).
const BOT_SQL_COLUNAS_IGUALDADE = [
    'login',
    'slug',
    'address',
    'queue_address',
    'case_key',
    'perm_key',
    'status',
    'kind',
    'source',
    'result',
    'op',
];
const BOT_SQL_TIPO_TEXTO = '(?:VARCHAR|TEXT|CHAR|NVARCHAR|CLOB)(?:\s*\(\s*\d+\s*\))?';
// O nome da coluna pode vir cru (`case_key`) ou escapado (`` `key` ``): o nome
// real so existe no texto NAO mascarado, porque a mascara apaga o interior.
const BOT_SQL_NOME_COLUNA = '/^(?:`(?P<citado>[^`]+)`|(?P<declarado>[A-Za-z_][\w$]*))(?:\s+|$)/';
const BOT_SQL_CREATE_TABELA =
    '/CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(?P<tabela>[A-Za-z_][\w$]*)\s*(?P<abre>\()/i';
const BOT_SQL_ITEM_TABELA = '/^\s*(?:CONSTRAINT\s+[A-Za-z_][\w$]*\s+)?'
    . '(?:PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|KEY|INDEX)\s*\(/i';
const BOT_SQL_CHAVE_TABELA = '/^\s*(?:CONSTRAINT\s+[A-Za-z_][\w$]*\s+)?'
    . '(?:PRIMARY\s+KEY|UNIQUE(?:\s+(?:KEY|INDEX))?(?:\s+[A-Za-z_][\w$]*)?|FOREIGN\s+KEY'
    . '|(?:KEY|INDEX)(?:\s+[A-Za-z_][\w$]*)?)\s*\((?P<colunas>[^)]*)\)/i';
const BOT_SQL_CHAVE_NA_COLUNA = '/\b(?:PRIMARY\s+KEY|UNIQUE)\b/i';
const BOT_SQL_COLACAO_RE = '/\bCOLLATE\b/i';
const BOT_SQL_REFERENCES_INLINE = '/\bREFERENCES\s+(?P<tabela>[A-Za-z_][\w$]*)\s*'
    . '\(\s*(?P<ref>[A-Za-z_][\w$]*)\s*\)'
    . '(?P<acoes>(?:\s+ON\s+(?:DELETE|UPDATE)\s+'
    . '(?:SET\s+NULL|SET\s+DEFAULT|CASCADE|RESTRICT|NO\s+ACTION)){0,2})/i';
const BOT_SQL_ADD_COLUNA = '/\bADD\s+COLUMN\s+(?P<col>`?[A-Za-z_][\w$]*`?)\s+(?P<tipo>'
    . BOT_SQL_TIPO_TEXTO . ')/i';
const BOT_SQL_ADD_FK = '/ADD\s+COLUMN\s+(?P<col>[A-Za-z_][\w$]*)\s+(?:INTEGER|BIGINT)\s+'
    . 'REFERENCES\s+(?P<tabela>[A-Za-z_][\w$]*)\s*\(\s*(?P<ref>[A-Za-z_][\w$]*)\s*\)'
    . '(?P<acao>\s+ON\s+DELETE\s+(?:SET\s+NULL|CASCADE|RESTRICT|NO\s+ACTION))?/i';
const BOT_SQL_ALTER_TABELA = '/ALTER\s+TABLE\s+(?P<tabela>[A-Za-z_][\w$]*)/i';
const BOT_SQL_CRIA_INDICE = '/^\s*CREATE\s+(?P<unico>UNIQUE\s+)?INDEX\s+'
    . '(?:IF\s+NOT\s+EXISTS\s+)?(?P<nome>`?[A-Za-z_][\w$]*`?)\s+ON\s+'
    . '(?P<tabela>`?[A-Za-z_][\w$]*`?)\s*\((?P<colunas>.*?)\)\s*'
    . '(?:WHERE\s+(?P<condicao>.+?))?\s*;?\s*$/is';
const BOT_SQL_DROPA_INDICE =
    '/^\s*DROP\s+INDEX\s+(?:IF\s+EXISTS\s+)?(?P<nome>`?[A-Za-z_][\w$]*`?)\s*;?\s*$/i';
const BOT_SQL_SO_NULO = '/^\s*(?P<col>[A-Za-z_][\w$.]*)\s+IS\s+NOT\s+NULL\s*$/i';

/** `TEXT ... PRIMARY KEY/UNIQUE` -> `VARCHAR(191)` (TEXT nao pode ser chave). */
function bot_sql_texto_para_varchar(array $m): string
{
    $meio = $m['meio'] ?? '';
    return 'VARCHAR(191) ' . $meio . strtoupper(preg_replace('/\s+/', ' ', $m['chave']));
}

/** Colunas de texto que precisam de `utf8mb4_bin` (chave, filha de FK ou lista). */
function bot_sql_colunas_de_igualdade(string $corpo, string $maskCorpo): array
{
    $nomes = [];
    foreach (bot_sql_split_args($corpo, $maskCorpo) as $item) {
        $definicao = trim($item);
        if ($definicao === '') {
            continue;
        }
        $maskDef = bot_sql_mask($definicao);
        $inicio = strlen($maskDef) - strlen(ltrim($maskDef));
        $mascarado = substr($maskDef, $inicio);
        if (preg_match(BOT_SQL_ITEM_TABELA, $mascarado) === 1) {
            if (preg_match(BOT_SQL_CHAVE_TABELA, $mascarado, $chave) === 1) {
                foreach (explode(',', $chave['colunas']) as $pedaco) {
                    if (preg_match(BOT_SQL_NOME_COLUNA, trim($pedaco), $c) === 1) {
                        $nomes[bot_sql_nome_citado($c)] = true;
                    }
                }
            }
            continue;
        }
        if (preg_match(BOT_SQL_NOME_COLUNA, substr($definicao, $inicio), $c) !== 1) {
            continue;
        }
        $nome = bot_sql_nome_citado($c);
        if (in_array($nome, BOT_SQL_COLUNAS_IGUALDADE, true)
            || preg_match(BOT_SQL_CHAVE_NA_COLUNA, $mascarado) === 1
            || preg_match(BOT_SQL_REFERENCES_INLINE, $mascarado) === 1
        ) {
            $nomes[$nome] = true;
        }
    }
    return $nomes;
}

/** Nome lido de um `preg_match` de identificador (citado por crase ou cru). */
function bot_sql_nome_citado(array $casado): string
{
    $pega = static function (string $chave) use ($casado): string {
        if (!isset($casado[$chave])) {
            return '';
        }
        $valor = $casado[$chave];
        if (is_array($valor)) {              // veio com PREG_OFFSET_CAPTURE
            $valor = (string) ($valor[0] ?? '');
        }
        return (string) $valor;
    };
    $citado = $pega('citado');
    $nome = $citado !== '' ? $citado : $pega('declarado');
    return strtolower(trim($nome, '`'));
}

/** Quantos caracteres de comentario/espaco abrem o item (a mascara tem o texto). */
function bot_sql_offset_util(string $mask): int
{
    return strlen($mask) - strlen(ltrim($mask));
}

/**
 * `COLLATE utf8mb4_bin` nas colunas de igualdade/chave (4.6.8, 5.5c).
 *
 * Roda **depois** do `TEXT`->`VARCHAR(191)` (senao `TEXT COLLATE ... PRIMARY KEY`
 * nao casaria mais o regex) e escreve o `COLLATE` logo depois do tipo, que e a
 * ordem canonica do MySQL (`VARCHAR(191) COLLATE utf8mb4_bin PRIMARY KEY`).
 */
function bot_sql_colacao(string $sql): string
{
    if (preg_match(BOT_SQL_CREATE_TABELA, $sql) === 1) {
        return bot_sql_colacao_no_create($sql);
    }
    return bot_sql_colacao_no_alter($sql);
}

/** `CREATE TABLE`: chave (`PRIMARY KEY`/`UNIQUE`), filha de FK e lista 5.5(c). */
function bot_sql_colacao_no_create(string $sql): string
{
    if (preg_match(BOT_SQL_CREATE_TABELA, $sql, $cabeca, PREG_OFFSET_CAPTURE) !== 1) {
        return $sql;
    }
    $abre = (int) $cabeca['abre'][1];
    $mask = bot_sql_mask($sql);
    $fecha = bot_sql_fecha_paren($mask, $abre);
    $corpo = substr($sql, $abre + 1, $fecha - $abre - 1);
    $maskCorpo = substr($mask, $abre + 1, $fecha - $abre - 1);
    $nomes = bot_sql_colunas_de_igualdade($corpo, $maskCorpo);
    if (!$nomes) {
        return $sql;
    }
    $itens = [];
    $mudou = false;
    foreach (bot_sql_split_args($corpo, $maskCorpo) as $item) {
        $limpo = trim($item);
        if ($limpo === '') {
            continue;
        }
        $inicio = bot_sql_offset_util(bot_sql_mask($limpo));
        $alvo = [];
        $achou = preg_match(
            '/^(?:`(?P<citado>[^`]+)`|(?P<declarado>[A-Za-z_][\w$]*))\s+(?P<tipo>'
            . BOT_SQL_TIPO_TEXTO . ')/i',
            substr($limpo, $inicio),
            $alvo,
            PREG_OFFSET_CAPTURE
        );
        if ($achou === 1
            && isset($nomes[bot_sql_nome_citado($alvo)])
            && preg_match(BOT_SQL_COLACAO_RE, bot_sql_mask($limpo)) !== 1
        ) {
            $fim = $inicio + (int) $alvo['tipo'][1] + strlen($alvo['tipo'][0]);
            $limpo = substr($limpo, 0, $fim) . ' ' . BOT_SQL_COLACAO_BIN . substr($limpo, $fim);
            $mudou = true;
        }
        $itens[] = $limpo;
    }
    if (!$mudou) {
        return $sql;
    }
    return substr($sql, 0, $abre + 1) . "\n  " . implode(",\n  ", $itens) . "\n" . substr($sql, $fecha);
}

/**
 * `ALTER TABLE ... ADD COLUMN` de coluna de igualdade ganha a colacao.
 *
 * As migracoes de banco antigo criam colunas fora do `CREATE TABLE` (por ex.
 * `analyst_load.casos_toa_keys`): sem isto a coluna nascida por `ALTER` ficaria
 * `ai_ci` e divergiria do schema novo. Sem lista de chaves a mao, so o nome da
 * coluna decide — e o unico sinal disponivel.
 */
function bot_sql_colacao_no_alter(string $sql): string
{
    if (preg_match(BOT_SQL_ADD_COLUNA, bot_sql_mask($sql)) !== 1) {
        return $sql;
    }
    $mask = bot_sql_mask($sql);
    $itens = [];
    $mudou = false;
    foreach (bot_sql_split_args($sql, $mask) as $item) {
        $limpo = trim($item);
        $achado = [];
        if (preg_match(BOT_SQL_ADD_COLUNA, bot_sql_mask($limpo), $achado, PREG_OFFSET_CAPTURE) === 1) {
            $nome = strtolower(trim($achado['col'][0], '`'));
            if (in_array($nome, BOT_SQL_COLUNAS_IGUALDADE, true)) {
                $fim = (int) $achado['tipo'][1] + strlen($achado['tipo'][0]);
                $limpo = substr($limpo, 0, $fim) . ' ' . BOT_SQL_COLACAO_BIN . substr($limpo, $fim);
                $mudou = true;
            }
        }
        $itens[] = $limpo;
    }
    if (!$mudou) {
        return $sql;
    }
    return implode(', ', $itens);
}

/**
 * `REFERENCES` inline do `CREATE TABLE` -> `CONSTRAINT ... FOREIGN KEY`.
 *
 * O MySQL **ignora em silencio** o `REFERENCES t(c)` escrito na coluna: a tabela
 * nasce sem FK e o `ON DELETE` nunca dispara. Toda referencia inline vira
 * restricao de tabela explicita (nome `fk_<tabela>_<coluna>`, unico por base),
 * que e a forma que o InnoDB aplica de fato.
 */
function bot_sql_fk_de_coluna(string $sql): string
{
    if (preg_match(BOT_SQL_CREATE_TABELA, $sql, $cabeca, PREG_OFFSET_CAPTURE) !== 1) {
        return $sql;
    }
    $mask = bot_sql_mask($sql);
    $abre = (int) $cabeca['abre'][1];
    $fecha = bot_sql_fecha_paren($mask, $abre);
    $corpo = substr($sql, $abre + 1, $fecha - $abre - 1);
    $maskCorpo = substr($mask, $abre + 1, $fecha - $abre - 1);
    $itens = [];
    $fks = [];
    foreach (bot_sql_split_args($corpo, $maskCorpo) as $item) {
        $limpo = trim($item);
        if ($limpo === '') {
            continue;
        }
        $achado = [];
        $temFk = preg_match(BOT_SQL_REFERENCES_INLINE, bot_sql_mask($limpo), $achado, PREG_OFFSET_CAPTURE);
        $temColuna = preg_match('/^\s*(?P<col>[A-Za-z_][\w$]*)\b/', $limpo, $coluna);
        if ($temFk !== 1 || $temColuna !== 1 || preg_match(BOT_SQL_ITEM_TABELA, $limpo) === 1) {
            $itens[] = $limpo;
            continue;
        }
        $iniRef = (int) $achado[0][1];
        $fimRef = $iniRef + strlen($achado[0][0]);
        $definicao = substr($limpo, 0, $iniRef) . substr($limpo, $fimRef);
        // A ponta filha tem de ter o tipo da ponta referenciada (o pai virou BIGINT).
        $definicao = preg_replace(
            '/^(?P<col>[A-Za-z_][\w$]*)\s+(?:INT|INTEGER)\b/i',
            '$1 BIGINT',
            $definicao
        );
        $definicao = trim(preg_replace('/\s+/', ' ', $definicao));
        $fks[] = 'CONSTRAINT fk_' . $cabeca['tabela'][0] . '_' . $coluna['col']
            . ' FOREIGN KEY (' . $coluna['col'] . ') REFERENCES ' . $achado['tabela'][0]
            . '(' . $achado['ref'][0] . ')' . $achado['acoes'][0];
        if ($definicao !== '') {
            $itens[] = $definicao;
        }
    }
    if (!$fks) {
        return $sql;
    }
    return substr($sql, 0, $abre + 1) . "\n  " . implode(",\n  ", array_merge($itens, $fks))
        . "\n" . substr($sql, $fecha);
}

/** `ADD COLUMN x INTEGER REFERENCES t(c)` -> coluna + `ADD CONSTRAINT` separado. */
function bot_sql_adiciona_fk(string $sql): string
{
    if (preg_match(BOT_SQL_ADD_FK, $sql) !== 1) {
        return $sql;
    }
    $tabela = preg_match(BOT_SQL_ALTER_TABELA, $sql, $alter) === 1 ? $alter['tabela'] : 't';
    $troca = static function (array $m) use ($tabela): string {
        $acao = isset($m['acao']) ? trim($m['acao']) : '';
        return 'ADD COLUMN ' . $m['col'] . ' BIGINT NULL, ADD CONSTRAINT fk_' . $tabela . '_'
            . $m['col'] . ' FOREIGN KEY (' . $m['col'] . ') REFERENCES ' . $m['tabela'] . '('
            . $m['ref'] . ')' . ($acao !== '' ? ' ' . $acao : '');
    };
    return preg_replace_callback(BOT_SQL_ADD_FK, $troca, $sql);
}

/** Colunas de um `CREATE INDEX` (lista simples, separada por virgula). */
function bot_sql_colunas_indice(string $texto): array
{
    $colunas = [];
    foreach (explode(',', $texto) as $coluna) {
        $coluna = trim($coluna);
        if ($coluna !== '') {
            $colunas[] = $coluna;
        }
    }
    return $colunas;
}

/** `WHERE col IS NOT NULL` (e `col` na chave) nao precisa de funcao. */
function bot_sql_so_coluna_nula(string $condicao, array $colunas): bool
{
    if (preg_match(BOT_SQL_SO_NULO, $condicao, $m) !== 1) {
        return false;
    }
    $alvo = strtolower($m['col']);
    foreach ($colunas as $coluna) {
        $pedacos = preg_split('/\s+/', trim($coluna));
        if (strtolower($pedacos[0]) === $alvo) {
            return true;
        }
    }
    return false;
}

/**
 * `CREATE [UNIQUE] INDEX ... [WHERE ...]` do SQLite para o MySQL (5.3).
 *
 * O MySQL nao tem indice parcial: a condicao entra como **indice funcional**
 * (`CASE WHEN ... END` devolve NULL para a linha que nao participa e o UNIQUE
 * ignora chave com NULL — o mesmo "fora do indice" do SQLite). A clausula
 * `IF NOT EXISTS` e resolvida na fachada (`BotPDO::exec`).
 */
function bot_sql_recria_indices(string $sql): string
{
    if (preg_match(BOT_SQL_CRIA_INDICE, $sql, $m) !== 1) {
        return $sql;
    }
    $unico = ($m['unico'] ?? '') !== '' ? 'UNIQUE ' : '';
    $nome = $m['nome'];
    $tabela = $m['tabela'];
    $colunas = bot_sql_colunas_indice($m['colunas']);
    $condicao = trim($m['condicao'] ?? '');
    if ($condicao === '' || bot_sql_so_coluna_nula($condicao, $colunas)) {
        return 'CREATE ' . $unico . 'INDEX ' . $nome . ' ON ' . $tabela
            . ' (' . implode(', ', $colunas) . ')';
    }
    if ($unico === '' && count($colunas) === 1) {
        return 'CREATE INDEX ' . $nome . ' ON ' . $tabela . ' ((CASE WHEN ' . $condicao
            . ' THEN ' . $colunas[0] . ' END))';
    }
    return 'CREATE ' . $unico . 'INDEX ' . $nome . ' ON ' . $tabela . ' ('
        . implode(', ', $colunas) . ', ((CASE WHEN ' . $condicao . ' THEN 1 END)))';
}

/** Ajustes de DDL (regras 19-21 + indices da 5.3), na ordem do Python. */
function bot_sql_ddl(string $sql): string
{
    $sql = preg_replace(
        "/DEFAULT\\s*\\(\\s*datetime\\s*\\(\\s*'now'\\s*\\)\\s*\\)/i",
        'DEFAULT (UTC_TIMESTAMP())',
        $sql
    );
    $sql = preg_replace('/DEFAULT\s*\(\s*CURRENT_TIMESTAMP\s*\)/i', 'DEFAULT (UTC_TIMESTAMP())', $sql);
    $sql = preg_replace(
        '/\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b/i',
        'BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY',
        $sql
    );
    $sql = preg_replace('/\bAUTOINCREMENT\b/i', 'AUTO_INCREMENT', $sql);
    $sql = preg_replace_callback(
        '/\bALTER\s+TABLE\s+(?P<tabela>[A-Za-z_][\w$]*)\s+RENAME\s+TO\s+(?P<novo>[A-Za-z_][\w$]*)/i',
        static fn(array $m): string => 'RENAME TABLE ' . $m['tabela'] . ' TO ' . $m['novo'],
        $sql
    );
    $sql = preg_replace_callback(
        '/\bTEXT\s+(?P<meio>(?:NOT\s+NULL\s+)?)(?P<chave>PRIMARY\s+KEY|UNIQUE)\b/i',
        'bot_sql_texto_para_varchar',
        $sql
    );
    $sql = bot_sql_colacao($sql);
    $sql = preg_replace_callback(
        "/DEFAULT\\s+'((?:[^']|'')*)'/",
        static fn(array $m): string => "DEFAULT ('" . $m[1] . "')",
        $sql
    );
    $sql = bot_sql_fk_de_coluna($sql);
    $sql = bot_sql_recria_indices($sql);
    return bot_sql_adiciona_fk($sql);
}

// -------------------------------------------------------------------- DML

const BOT_SQL_INSERT_OR_IGNORE = '/\bINSERT\s+OR\s+IGNORE\b/i';
const BOT_SQL_INSERT_OR_REPLACE = '/\bINSERT\s+OR\s+REPLACE\b/i';
const BOT_SQL_UPSERT = '/ON\s+CONFLICT\s*\((?P<cols>[^)]*)\)\s*DO\s+UPDATE(?:\s+SET\b)?/i';
const BOT_SQL_DO_NOTHING = '/ON\s+CONFLICT\s*\((?P<cols>[^)]*)\)\s*DO\s+NOTHING/i';
const BOT_SQL_ON_CONFLICT = '/\bON\s+CONFLICT\b/i';
const BOT_SQL_EXCLUDED = '/\bexcluded\./i';
const BOT_SQL_BEGIN = '/^\s*BEGIN\s*(?:IMMEDIATE|EXCLUSIVE|DEFERRED)?\s*;?\s*$/i';
const BOT_SQL_PRAGMA = '/\bPRAGMA\b/i';
const BOT_SQL_PRAGMA_FK = '/^\s*PRAGMA\s+foreign_keys\s*=\s*(ON|OFF|1|0)\s*;?\s*$/i';
const BOT_SQL_PRAGMA_INFO = '/^\s*PRAGMA\s+table_info\s*\(\s*([^)]*?)\s*\)\s*;?\s*$/i';
const BOT_SQL_PRAGMA_BUSY = '/^\s*PRAGMA\s+busy_timeout\s*=\s*(\d+)\s*;?\s*$/i';
const BOT_SQL_PRAGMA_ALGUM = '/^\s*PRAGMA\s+[A-Za-z_]\w*\s*(?:=\s*[^;]*?)?\s*;?\s*$/i';
const BOT_SQL_MASTER_TABELA = '/\bSELECT\s+1\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*\'table\''
    . '\s+AND\s+name\s*=\s*\?/i';
const BOT_SQL_MASTER_DDL = '/\bSELECT\s+sql\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*\'table\''
    . '\s+AND\s+name\s*=\s*\'([^\']*)\'/i';
const BOT_SQL_MASTER = '/\bsqlite_master\b/i';
const BOT_SQL_ID_RESERVADO = '/(?<![`\w])key(?![`\w])/i';
const BOT_SQL_ID_ANTES = '/(?:\b(?:PRIMARY|FOREIGN|UNIQUE|DUPLICATE|INDEX|PARTITION)\s*)$/i';
const BOT_SQL_TABELAS_MYSQL = "SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() "
    . "AND table_type = 'BASE TABLE' AND table_name = ?";

/** `ON CONFLICT (…) DO NOTHING` -> `ON DUPLICATE KEY UPDATE c = c` (regra 13). */
function bot_sql_do_nothing(string $sql): string
{
    return preg_replace_callback(
        BOT_SQL_DO_NOTHING,
        static function (array $m): string {
            $primeira = trim(explode(',', $m['cols'])[0]);
            return 'ON DUPLICATE KEY UPDATE ' . $primeira . ' = ' . $primeira;
        },
        $sql
    );
}

/** `PRAGMA table_info(x)` -> `information_schema.columns` (regra 15). */
function bot_sql_info_colunas(string $bruto): string
{
    $nome = str_replace("'", "''", trim($bruto, " \t\"'`"));
    return "SELECT c.column_name AS name, c.column_type AS type,"
        . " IF(c.is_nullable = 'NO', 1, 0) AS `notnull`,"
        . " c.column_default AS dflt_value,"
        . " IF(c.column_key = 'PRI', 1, 0) AS pk"
        . " FROM information_schema.columns c"
        . " WHERE c.table_schema = DATABASE() AND c.table_name = '" . $nome . "'"
        . " ORDER BY c.ordinal_position";
}

/**
 * `sqlite_master` -> `information_schema` (regra 14).
 *
 * Duas leituras existem no painel: saber se a tabela existe e conferir o DDL do
 * `menu_options` (que tem de citar `run_script`). A segunda devolve as clausulas
 * `CHECK` mais os nomes das colunas, que e o que a checagem do boot procura.
 */
function bot_sql_lista_tabelas(string $sql): string
{
    if (preg_match(BOT_SQL_MASTER_TABELA, $sql) === 1) {
        return preg_replace_callback(
            BOT_SQL_MASTER_TABELA,
            static fn(): string => BOT_SQL_TABELAS_MYSQL,
            $sql
        );
    }
    if (preg_match(BOT_SQL_MASTER_DDL, $sql, $m) === 1) {
        $tabela = str_replace("'", "''", $m[1]);
        $consulta = "SELECT CONCAT(COALESCE((SELECT GROUP_CONCAT(cc.check_clause SEPARATOR ' ')"
            . " FROM information_schema.check_constraints cc"
            . " JOIN information_schema.table_constraints tc"
            . " ON tc.constraint_schema = cc.constraint_schema"
            . " AND tc.constraint_name = cc.constraint_name"
            . " WHERE cc.constraint_schema = DATABASE() AND tc.table_name = '" . $tabela . "'), ''),"
            . " ' ', COALESCE((SELECT GROUP_CONCAT(c.column_name SEPARATOR ' ')"
            . " FROM information_schema.columns c"
            . " WHERE c.table_schema = DATABASE() AND c.table_name = '" . $tabela . "'), '')) AS `sql`";
        return preg_replace_callback(BOT_SQL_MASTER_DDL, static fn(): string => $consulta, $sql);
    }
    throw new RuntimeException(
        'sqlite_master so e traduzido para checar tabela/DDL do menu_options: ' . substr($sql, 0, 120)
    );
}

/** PRAGMA -> equivalente (ou nada): `journal_mode` nao existe no MySQL. */
function bot_sql_pragma(string $sql): string
{
    $corpo = bot_sql_sem_comentario($sql);
    if (preg_match(BOT_SQL_PRAGMA_FK, $corpo, $m) === 1) {
        return 'SET FOREIGN_KEY_CHECKS = '
            . (in_array(strtoupper($m[1]), ['ON', '1'], true) ? '1' : '0');
    }
    if (preg_match(BOT_SQL_PRAGMA_INFO, $corpo, $m) === 1) {
        return bot_sql_info_colunas($m[1]);
    }
    if (preg_match(BOT_SQL_PRAGMA_BUSY, $corpo, $m) === 1) {
        // `busy_timeout` do SQLite = quanto esperar pelo lock; no MySQL e o
        // `innodb_lock_wait_timeout` da sessao (em segundos).
        return 'SET SESSION innodb_lock_wait_timeout = ' . max(1, (int) ceil(((int) $m[1]) / 1000));
    }
    if (preg_match(BOT_SQL_PRAGMA_ALGUM, $corpo) === 1) {
        return '';
    }
    throw new RuntimeException('PRAGMA nao suportado: ' . var_export($corpo, true));
}

// --------------------------------------------------------------- texto/`||`

const BOT_SQL_OPERANDO_ESQ = "/(?:'(?:[^']|'')*'|\\?|[A-Za-z_][A-Za-z0-9_.]*|\\([^()]*\\))\\s*$/";
const BOT_SQL_OPERANDO_DIR = "/^\\s*(?:'(?:[^']|'')*'|\\?|[A-Za-z_][A-Za-z0-9_.]*|\\([^()]*\\))/";

/** `a || b` -> `CONCAT(a, b)` (regra 10: no MySQL `||` e OR logico). */
function bot_sql_concat(string $sql): string
{
    $guarda = 0;
    while (strpos(bot_sql_mask($sql), '||') !== false) {
        if (++$guarda > 50) {
            throw new RuntimeException('`||` demais para reescrever: ' . substr($sql, 0, 120));
        }
        $mask = bot_sql_mask($sql);
        $pos = (int) strpos($mask, '||');
        if (preg_match(BOT_SQL_OPERANDO_ESQ, substr($sql, 0, $pos), $esq) !== 1) {
            throw new RuntimeException('`||` sem operando a esquerda: ' . substr($sql, 0, 120));
        }
        $inicio = $pos - strlen($esq[0]);
        $cauda = substr($sql, $pos + 2);
        if (preg_match(BOT_SQL_OPERANDO_DIR, $cauda, $dir) !== 1) {
            throw new RuntimeException('`||` sem operando a direita: ' . substr($sql, 0, 120));
        }
        $sql = substr($sql, 0, $inicio) . 'CONCAT(' . trim($esq[0]) . ', ' . trim($dir[0]) . ')'
            . substr($cauda, strlen($dir[0]));
    }
    return $sql;
}

/**
 * `key` (coluna de `bot_settings`) -> `` `key` ``: e reservada no MySQL.
 *
 * Ficam de fora o `KEY` de `PRIMARY KEY`/`FOREIGN KEY`/`ON DUPLICATE KEY UPDATE`
 * e o texto dentro de literal/comentario, que a mascara esconde.
 */
function bot_sql_escapa_identificadores(string $sql): string
{
    $mask = bot_sql_mask($sql);
    if (preg_match_all(BOT_SQL_ID_RESERVADO, $mask, $achados, PREG_OFFSET_CAPTURE) < 1) {
        return $sql;
    }
    $partes = [];
    $anterior = 0;
    $mudou = false;
    foreach ($achados[0] as $casado) {
        $pos = (int) $casado[1];
        if (preg_match(BOT_SQL_ID_ANTES, substr($mask, 0, $pos)) === 1) {
            continue;
        }
        $partes[] = substr($sql, $anterior, $pos - $anterior);
        $partes[] = '`' . substr($sql, $pos, strlen($casado[0])) . '`';
        $anterior = $pos + strlen($casado[0]);
        $mudou = true;
    }
    if (!$mudou) {
        return $sql;
    }
    $partes[] = substr($sql, $anterior);
    return implode('', $partes);
}

/** Garante que a regra nao pegou texto de dentro de literal/comentario. */
function bot_sql_exige_codigo(string $mask, string $sql, string $padrao): void
{
    if (preg_match_all($padrao, $sql, $achados, PREG_OFFSET_CAPTURE) < 1) {
        return;
    }
    foreach ($achados[0] as $casado) {
        $ini = (int) $casado[1];
        $pedaco = substr($sql, $ini, strlen($casado[0]));
        if (substr($mask, $ini, strlen($casado[0])) !== $pedaco) {
            throw new RuntimeException('regra casada dentro de literal/comentario: ' . $pedaco);
        }
    }
}

/** Divide um script nos comandos pelo `;` de nivel 0 (a mascara respeita texto). */
function bot_sql_split_script(string $script): array
{
    $mask = bot_sql_mask($script);
    $comandos = [];
    $inicio = 0;
    $n = strlen($mask);
    for ($i = 0; $i < $n; $i++) {
        if ($mask[$i] === ';') {
            $pedaco = trim(substr($script, $inicio, $i - $inicio));
            if ($pedaco !== '') {
                $comandos[] = $pedaco;
            }
            $inicio = $i + 1;
        }
    }
    $resto = trim(substr($script, $inicio));
    if ($resto !== '') {
        $comandos[] = $resto;
    }
    return $comandos;
}

/**
 * Traduz um comando do dialeto SQLite para MySQL 8 (secoes 5.1 a 5.5).
 *
 * Devolve `[sql, plano]`: `plano === null` significa "os parametros seguem como
 * vieram"; com plano, cada `?` do SQL final tem uma entrada na ordem
 * (`comum` = passa o original, `segundos` = modificador -> segundos, `agora` =
 * modificador -> carimbo UTC deslocado). Com `sqlite` devolve tudo intacto.
 */
function bot_sql_traduz(string $sql, bool $mysql): array
{
    if (!$mysql) {
        return [$sql, null];
    }
    $sql = bot_sql_sem_comentario($sql);
    if ($sql === '') {
        return ['', null];
    }
    if (preg_match(BOT_SQL_DDL, $sql) === 1) {
        $sql = bot_sql_ddl($sql);
    }
    $plano = null;
    if (preg_match('/\b(?:datetime|julianday)\s*\(/i', $sql) === 1) {
        [$sql, $plano] = bot_sql_tempo($sql);
    }
    $mask = bot_sql_mask($sql);
    if (preg_match(BOT_SQL_INSERT_OR_REPLACE, $mask) === 1) {
        throw new RuntimeException(
            '`INSERT OR REPLACE` nao tem equivalente honesto no MySQL '
            . '(o REPLACE apaga e reinsere, disparando CASCADE): reescreva a consulta.'
        );
    }
    if (preg_match(BOT_SQL_INSERT_OR_IGNORE, $mask) === 1) {
        bot_sql_exige_codigo($mask, $sql, BOT_SQL_INSERT_OR_IGNORE);
        $sql = preg_replace(BOT_SQL_INSERT_OR_IGNORE, 'INSERT IGNORE', $sql);
    }
    if (preg_match(BOT_SQL_DO_NOTHING, $mask) === 1) {
        $sql = bot_sql_do_nothing($sql);
    }
    if (preg_match(BOT_SQL_UPSERT, $mask) === 1) {
        bot_sql_exige_codigo($mask, $sql, BOT_SQL_UPSERT);
        bot_sql_exige_codigo($mask, $sql, BOT_SQL_EXCLUDED);
        $sql = preg_replace(BOT_SQL_UPSERT, 'AS new ON DUPLICATE KEY UPDATE', $sql);
        $sql = preg_replace(BOT_SQL_EXCLUDED, 'new.', $sql);
    }
    if (preg_match(BOT_SQL_ON_CONFLICT, bot_sql_mask($sql)) === 1) {
        throw new RuntimeException('ON CONFLICT em formato nao suportado: ' . substr($sql, 0, 120));
    }
    if (preg_match(BOT_SQL_MASTER, bot_sql_mask($sql)) === 1) {
        $sql = bot_sql_lista_tabelas($sql);
    }
    if (preg_match(BOT_SQL_PRAGMA, bot_sql_mask($sql)) === 1) {
        $sql = bot_sql_pragma($sql);
    }
    if ($sql === '') {
        return ['', $plano];
    }
    if (preg_match(BOT_SQL_BEGIN, $sql) === 1) {
        $sql = 'START TRANSACTION';
    }
    if (strpos(bot_sql_mask($sql), '||') !== false) {
        $sql = bot_sql_concat($sql);
    }
    $sql = bot_sql_escapa_identificadores($sql);
    if ($plano !== null && bot_sql_conta_marcadores($sql) !== count($plano)) {
        throw new RuntimeException(
            'traducao com ' . bot_sql_conta_marcadores($sql) . ' marcador(es) e ' . count($plano)
            . ' no plano: ' . substr($sql, 0, 160)
        );
    }
    return [$sql, $plano];
}

// ---------------------------------------------------------------------- PDO

/** Valor de um parametro pronto para o driver (`datetime` -> texto, `bool` -> 0/1). */
function bot_sql_param_normalizado($valor)
{
    if ($valor instanceof DateTimeInterface) {
        return $valor->format('Y-m-d H:i:s');
    }
    if (is_bool($valor)) {
        return $valor ? 1 : 0;
    }
    return $valor;
}

/** Parametro na posicao pedida pelo plano (falha alto se nao chegou). */
function bot_sql_param_de(array $params, int $indice)
{
    if (!array_key_exists($indice, $params)) {
        throw new RuntimeException(
            'parametro #' . $indice . ' pedido pelo SQL traduzido nao chegou ao execute().'
        );
    }
    return bot_sql_param_normalizado($params[$indice]);
}

/**
 * Monta a lista final de parametros a partir do plano da traducao.
 *
 * O plano tem uma entrada por `?` do SQL traduzido: `comum` passa o valor
 * original, `segundos` converte o modificador (`'-3 hours'` -> -10800) e `agora`
 * calcula o carimbo UTC (com o modificador, quando veio por parametro).
 */
function bot_sql_params_do_plano(array $plano, ?array $params): array
{
    if ($params === null) {
        throw new RuntimeException(
            'SQL traduzido resolve data no execute(): execute() precisa receber os parametros.'
        );
    }
    $novos = [];
    foreach ($plano as $item) {
        $tipo = $item[0];
        if ($tipo === 'comum') {
            $novos[] = bot_sql_param_de($params, (int) $item[1]);
        } elseif ($tipo === 'segundos') {
            $novos[] = bot_sql_modificador_segundos(bot_sql_param_de($params, (int) $item[1]));
        } elseif ($tipo === 'agora') {
            $novos[] = bot_utc_stamp(bot_sql_modificador_segundos(bot_sql_param_de($params, (int) $item[1])));
        } else {
            $novos[] = bot_utc_stamp();
        }
    }
    return $novos;
}

/** Statement do painel: aplica o plano de parametros que a traducao montou. */
class BotStatement extends PDOStatement
{
    /** @var array<int, array<int, mixed>>|null */
    public ?array $botPlano = null;

    public function execute(?array $params = null): bool
    {
        if ($this->botPlano !== null) {
            $params = bot_sql_params_do_plano($this->botPlano, $params);
        }
        return parent::execute($params);
    }
}

/**
 * Conexao do painel: traduz no choke point e devolve `BotStatement`.
 *
 * Com `sqlite` e um `PDO` comum (a traducao e identidade); com `mysql` a DSN
 * ganha `utf8mb4`, `EMULATE_PREPARES=false` (o driver nao devolve tudo texto),
 * `FOUND_ROWS=true` (o `rowCount()` do painel conta linhas casadas, como o
 * `changes()` do SQLite) e o handshake da sessao.
 */
class BotPDO extends PDO
{
    private bool $botMysql = false;

    public function __construct(
        string $dsn,
        ?string $usuario = null,
        ?string $senha = null,
        ?array $opcoes = null,
        bool $mysql = false
    ) {
        parent::__construct($dsn, $usuario, $senha, $opcoes);
        $this->botMysql = $mysql;
    }

    public function botMysql(): bool
    {
        return $this->botMysql;
    }

    public function prepare(string $query, array $options = []): PDOStatement|false
    {
        [$sql, $plano] = bot_sql_traduz($query, $this->botMysql);
        if ($sql === '') {
            throw new RuntimeException('comando vazio depois da traducao: ' . substr($query, 0, 120));
        }
        $st = parent::prepare($sql, $options);
        if ($st instanceof BotStatement) {
            $st->botPlano = $plano;
        }
        return $st;
    }

    public function query(string $query, ?int $fetchMode = null, mixed ...$fetchModeArgs): PDOStatement|false
    {
        [$sql] = bot_sql_traduz($query, $this->botMysql);
        if ($sql === '') {
            throw new RuntimeException('comando vazio depois da traducao: ' . substr($query, 0, 120));
        }
        return parent::query($sql, $fetchMode, ...$fetchModeArgs);
    }

    public function exec(string $statement): int|false
    {
        [$sql] = bot_sql_traduz($statement, $this->botMysql);
        if ($sql === '') {
            return 0;        // PRAGMA que nao existe no MySQL (`journal_mode`, ...)
        }
        if ($this->botMysql) {
            $ajustado = $this->botAjustaIndice($statement, $sql);
            if ($ajustado === null) {
                return 0;    // indice que ja existe (ou drop de indice que nao existe)
            }
            $sql = $ajustado;
        }
        return parent::exec($sql);
    }

    /**
     * `CREATE/DROP INDEX ... IF [NOT] EXISTS`, que o MySQL nao expressa (5.3).
     *
     * O SQLite da os dois de graca; aqui o catalogo decide: `IF NOT EXISTS` de
     * indice existente vira no-op e `DROP INDEX IF EXISTS <nome>` (sem tabela!)
     * so vira `DROP INDEX ... ON ...` se o indice existir.
     */
    private function botAjustaIndice(string $original, string $sql): ?string
    {
        // Sem comentario: as regexes de indice sao ancoradas (`^\s*CREATE`/`^\s*DROP`)
        // e um `-- comentario` antes da instrucao (comum no schema.sql, que roda a
        // cada bot_ensure_schema) faria o "ja existe" nunca bater — a 2a chamada
        // tentaria recriar o indice e o MySQL derrubaria a pagina com 1061.
        $semComentario = bot_sql_sem_comentario($original);
        if (preg_match(BOT_SQL_CRIA_INDICE, $semComentario, $m) === 1
            && stripos($original, 'IF NOT EXISTS') !== false
            && $this->botIndiceExiste($m['tabela'], $m['nome'])
        ) {
            return null;
        }
        if (preg_match(BOT_SQL_DROPA_INDICE, trim($semComentario), $m) === 1
            && stripos($original, 'IF EXISTS') !== false
        ) {
            $tabela = $this->botTabelaDoIndice($m['nome']);
            if ($tabela === null) {
                return null;
            }
            return 'DROP INDEX `' . trim($m['nome'], '`') . '` ON `' . $tabela . '`';
        }
        return $sql;
    }

    private function botIndiceExiste(string $tabela, string $indice): bool
    {
        $st = parent::prepare(
            'SELECT index_name FROM information_schema.statistics'
            . ' WHERE table_schema = DATABASE() AND table_name = ? AND index_name = ?'
        );
        $st->execute([trim($tabela, '`'), trim($indice, '`')]);
        return (bool) $st->fetch();
    }

    private function botTabelaDoIndice(string $indice): ?string
    {
        $st = parent::prepare(
            'SELECT table_name FROM information_schema.statistics'
            . ' WHERE table_schema = DATABASE() AND index_name = ?'
        );
        $st->execute([trim($indice, '`')]);
        $linha = $st->fetch(PDO::FETCH_COLUMN);
        return $linha === false || $linha === null || $linha === '' ? null : (string) $linha;
    }
}

/** A conexao do painel e MySQL? (usado pelo boot, para o DDL) */
function bot_pdo_is_mysql(PDO $pdo): bool
{
    return $pdo instanceof BotPDO && $pdo->botMysql();
}

/**
 * Abre a conexao do backend configurado.
 *
 * SQLite: arquivo (`$sqlitePath`, com a pasta criada se faltar) e os PRAGMAs do
 * boot, que o chamador executa. MySQL: DSN do `.env` + handshake da sessao.
 */
function bot_db_abre(?string $sqlitePath = null): BotPDO
{
    $opcoes = [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        PDO::ATTR_STATEMENT_CLASS => [BotStatement::class],
    ];
    if (bot_db_backend() !== 'mysql') {
        $path = (string) ($sqlitePath ?? '');
        $dir = dirname($path);
        if (!is_dir($dir) && !mkdir($dir, 0775, true) && !is_dir($dir)) {
            throw new RuntimeException('Não foi possível criar a pasta do SQLite: ' . $dir);
        }
        return new BotPDO('sqlite:' . $path, null, null, $opcoes, false);
    }
    $cfg = bot_mysql_config();
    $opcoes[PDO::ATTR_EMULATE_PREPARES] = false;
    $opcoes[PDO::ATTR_STRINGIFY_FETCHES] = false;
    $opcoes[PDO::MYSQL_ATTR_FOUND_ROWS] = true;
    $pdo = new BotPDO(
        sprintf(
            'mysql:host=%s;port=%s;dbname=%s;charset=utf8mb4',
            $cfg['host'],
            $cfg['port'],
            $cfg['db']
        ),
        $cfg['user'],
        $cfg['pass'],
        $opcoes,
        true
    );
    foreach (bot_mysql_handshake() as $comando) {
        $pdo->exec($comando);
    }
    return $pdo;
}

/**
 * Executa um script multi-comando (o `schema.sql`).
 *
 * No MySQL a checagem de FK fica desligada durante o DDL (as tabelas nascem fora
 * da ordem das FKs e o `PRAGMA foreign_keys = ON` do script derrubaria o boot
 * com 1824) — por isso as linhas de `PRAGMA foreign_keys` saem do script aqui.
 */
function bot_executa_script(PDO $pdo, string $script): int
{
    $mysql = bot_pdo_is_mysql($pdo);
    if ($mysql) {
        $pdo->exec('SET FOREIGN_KEY_CHECKS = 0');
    }
    $feitos = 0;
    try {
        foreach (bot_sql_split_script($script) as $comando) {
            if ($mysql && preg_match(BOT_SQL_PRAGMA_FK, bot_sql_sem_comentario($comando)) === 1) {
                continue;
            }
            $pdo->exec($comando);
            $feitos++;
        }
    } finally {
        if ($mysql) {
            $pdo->exec('SET FOREIGN_KEY_CHECKS = 1');
        }
    }
    return $feitos;
}
