<?php
declare(strict_types=1);

/** Componentes de interface compartilhados pelo painel (Bootstrap 5 local, sem CDN). */

/**
 * Corta texto em N CARACTERES (não bytes), sem exigir a extensão mbstring.
 *
 * Usar mb_substr direto derrubava a aba Roteamento com "Call to undefined
 * function mb_substr()" quando o PHP não tinha mbstring habilitada.
 */
function ui_clip(string $s, int $len): string
{
    $len = max(0, $len);
    if ($len === 0) {
        return '';
    }
    if (function_exists('mb_substr')) {
        return mb_substr($s, 0, $len, 'UTF-8');
    }
    if (preg_match('/^.{0,' . $len . '}/us', $s, $m) === 1) {
        return $m[0];
    }
    return substr($s, 0, $len);
}

function page_header(string $title, string $sub = "", string $actions = ""): void
{
    echo "<div class=\"page-head\"><div><h1>" . h($title) . "</h1>";
    if ($sub !== "") {
        echo "<p class=\"sub\">" . $sub . "</p>";
    }
    echo "</div>";
    if ($actions !== "") {
        echo "<div class=\"d-flex flex-wrap gap-2 align-items-center\">" . $actions . "</div>";
    }
    echo "</div>";
}

/**
 * Multi-select local (sem CDN): <details> abre sem JS; o JS apenas melhora
 * (busca, todos/limpar e resumo no rotulo). Envia name[] com os valores marcados.
 *
 * @param array<string,string> $options value => label
 * @param array<int,string> $selected
 */
function ui_multiselect(string $name, array $options, array $selected = [], string $placeholder = "Todos"): void
{
    $sel = [];
    foreach ($selected as $s) {
        $sel[(string) $s] = true;
    }
    $marcados = array_intersect_key($options, $sel);
    $on = count($marcados);
    if ($on === 0) {
        $label = $placeholder;
    } elseif ($on === 1) {
        $label = (string) reset($marcados);
    } else {
        $label = $on . " selecionados";
    }
    echo "<details class=\"ms-picker\" data-empty=\"" . h($placeholder) . "\">";
    echo "<summary class=\"btn btn-outline-secondary btn-sm w-100 text-start\"><span class=\"ms-label\">" . h($label) . "</span></summary>";
    echo "<div class=\"dropdown-menu p-2 shadow-sm\">";
    echo "<input type=\"search\" class=\"form-control form-control-sm mb-2 ms-search\" placeholder=\"Filtrar...\" autocomplete=\"off\">";
    echo "<div class=\"ms-options\">";
    foreach ($options as $value => $lab) {
        $value = (string) $value;
        $id = "ms-" . preg_replace("/[^a-z0-9_]/i", "_", $name . "-" . $value);
        echo "<label class=\"form-check\" data-filter=\"" . h($value . " " . $lab) . "\">"
            . "<input class=\"form-check-input\" type=\"checkbox\" name=\"" . h($name) . "[]\" value=\"" . h($value) . "\""
            . (isset($sel[$value]) ? " checked" : "") . " data-label=\"" . h($lab) . "\" id=\"" . h($id) . "\"> "
            . "<span class=\"form-check-label\">" . h($lab) . "</span></label>";
    }
    echo "</div>";
    echo "<div class=\"d-flex gap-2 mt-2\"><button type=\"button\" class=\"btn btn-sm btn-outline-secondary ms-all\">Todos</button>"
        . "<button type=\"button\" class=\"btn btn-sm btn-outline-secondary ms-none\">Limpar</button></div>";
    echo "</div></details>";
}

/** Campos de periodo (de/ate) + atalhos que apenas preenchem as datas (data-range). */
function ui_date_range(string $de, string $ate): void
{
    $hoje = (new DateTimeImmutable("now", new DateTimeZone("America/Sao_Paulo")))->format("Y-m-d");
    echo "<div class=\"col-6 col-md-2\"><label class=\"form-label\">De</label>"
        . "<input type=\"date\" class=\"form-control form-control-sm\" name=\"de\" value=\"" . h($de) . "\" data-range-de></div>";
    echo "<div class=\"col-6 col-md-2\"><label class=\"form-label\">Ate</label>"
        . "<input type=\"date\" class=\"form-control form-control-sm\" name=\"ate\" value=\"" . h($ate) . "\" data-range-ate data-today=\"" . h($hoje) . "\"></div>";
    echo "<div class=\"col-12 col-md-auto\"><label class=\"form-label d-none d-md-block\">&nbsp;</label>"
        . "<div class=\"btn-group btn-group-sm\" role=\"group\">";
    foreach (["Hoje" => 0, "7d" => 6, "30d" => 29, "90d" => 89] as $lab => $dias) {
        echo "<button type=\"button\" class=\"btn btn-outline-secondary\" data-range=\"" . (int) $dias . "\">" . h($lab) . "</button>";
    }
    echo "</div></div>";
}

/** Valida de/ate (AAAA-MM-DD, vazio permitido). @return array{0:?DateTimeImmutable,1:?DateTimeImmutable,2:?string} */
function ui_parse_period(string $deRaw, string $ateRaw): array
{
    $parse = static function (string $v): ?DateTimeImmutable {
        $v = trim($v);
        if ($v === "") {
            return null;
        }
        $d = DateTimeImmutable::createFromFormat("!Y-m-d", $v);
        return $d === false ? null : $d;
    };
    $de = $parse($deRaw);
    $ate = $parse($ateRaw);
    if ((trim($deRaw) !== "" && $de === null) || (trim($ateRaw) !== "" && $ate === null)) {
        return [null, null, "Datas invalidas. Use o formato AAAA-MM-DD."];
    }
    if ($de !== null && $ate !== null) {
        if ($de > $ate) {
            return [null, null, "A data inicial deve ser menor ou igual a final."];
        }
        if ($de->diff($ate)->days > 730) {
            return [null, null, "Periodo maximo de 2 anos."];
        }
    }
    return [$de, $ate, null];
}