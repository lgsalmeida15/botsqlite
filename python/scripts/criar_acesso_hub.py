# -*- coding: utf-8 -*-
"""Acesso ao HUB/site: coleta hub + tecnicos (1..4) + motivo e posta no webhook.

Duas camadas:

1. `fields_for(answers)` monta a lista de perguntas **do momento** (o wizard do
   engine pergunta campo a campo, gravando o progresso no banco). Como a lista e
   funcao das respostas ja coletadas, o numero de tecnicos informado (1 a 4)
   define quantos blocos sao pedidos e, quando o hub digitado nao casa com
   `bases/hub.json`, a pergunta vira uma lista numerada com ate 10 sugestoes.
2. `normalize_answer(key, text, answers)` valida/normaliza cada resposta e pode
   guardar estado interno (chaves `_`) usado pela proxima pergunta.
3. `run(ctx)` monta o texto e envia para o webhook `WEBHOOK_ACESSO_HUB`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from python.config import env
from python.scripts.criar_infra_rede import _post_json

log = logging.getLogger("helptoa.scripts.hub")

SLUG = "criar_acesso_hub"
LABEL = "Criar acesso ao hub"
# Espelho estatico das perguntas fixas (usado pelo `manifest.json`/aba Menus).
# A lista real do wizard vem de `fields_for`: os blocos de tecnico sao dinamicos.
FIELDS = [
    {
        "key": "hub",
        "type": "text",
        "prompt": "Qual hub/site? Ex: VMN ou VILA MARIANA",
        "label": "Hub/site",
    },
    {
        "key": "incidente",
        "type": "text",
        "prompt": "Qual número do Incidente? (RAL/REC/INM)",
        "label": "Incidente",
    },
    {
        "key": "qtd_tecnicos",
        "type": "int",
        "prompt": "Quantos técnicos acessarão o local? Responda apenas o número (1 a 4)",
        "label": "Técnicos",
    },
    {
        "key": "motivo",
        "type": "text",
        "prompt": "Informe o motivo do acesso",
        "label": "Motivo",
    },
]
CONFIRM_NOTE = "Qualquer inconsistência pode ocasionar na recusa."
SUCCESS_MESSAGE = (
    "Solicitação de acesso ao HUB enviada com sucesso! "
    "Para mais solicitações inicie um novo chat."
)
ERROR_MESSAGE = "Erro ao solicitar acesso ao HUB, direcionando para um atendente."
HTTP_TIMEOUT = 15.0
DETAIL_MAX = 800
MAX_SUGESTOES = 10
MIN_TEC = 1
MAX_TEC = 4
INC_MAX = 15
TEC_KEYS = ("empresa", "nome", "rg", "cpf")
TEC_LABELS = {"empresa": "Empresa", "nome": "Nome Completo", "rg": "RG", "cpf": "CPF"}
HUBS_PATH = Path(__file__).with_name("bases") / "hub.json"

_hubs_cache: tuple[float, list[dict]] | None = None


def load_hubs(path: Path | str | None = None) -> list[dict]:
    """Base de hubs/distritos (`bases/hub.json`), com cache por data de modificacao."""
    global _hubs_cache
    alvo = Path(path) if path else HUBS_PATH
    try:
        mtime = alvo.stat().st_mtime
    except OSError:
        log.warning("base de hubs ausente: %s", alvo)
        return []
    if path is None and _hubs_cache is not None and _hubs_cache[0] == mtime:
        return _hubs_cache[1]
    try:
        raw = json.loads(alvo.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("base de hubs ilegivel: %s", alvo, exc_info=True)
        return []
    rows: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        hub = str(item.get("hub") or "").strip()
        distrito = str(item.get("distrito") or "").strip()
        if hub and distrito:
            rows.append({
                "hub": hub,
                "distrito": distrito,
                "nome_uf": str(item.get("nome_uf") or "").strip(),
            })
    if path is None:
        _hubs_cache = (mtime, rows)
    return rows


def _fold(texto: str) -> str:
    """Texto comparavel: sem acento, maiusculo, sem pontuacao e com espaco unico."""
    base = unicodedata.normalize("NFD", str(texto or ""))
    base = "".join(c for c in base if unicodedata.category(c) != "Mn").upper()
    limpo = "".join(c if c.isalnum() else " " for c in base)
    return " ".join(limpo.split())


def _compact(texto: str) -> str:
    return _fold(texto).replace(" ", "")


def rotulo(row: dict) -> str:
    """Como o hub aparece na lista de sugestoes e no webhook: HUBSAM - SANTO AMARO."""
    return f"{row.get('hub', '')} - {row.get('distrito', '')}"


def _pontuar(row: dict, alvo: str) -> int:
    """Aproximacao entre o que o tecnico digitou e um hub/distrito (0 = nao serve).

    Ordem: igual → compacto (sem espaco) → prefixo → todas as palavras batem →
    trecho ("VMN" acha HEVMN/HEDVMN) → semelhanca de escrita (typo).
    """
    if not alvo:
        return 0
    hub = _fold(row.get("hub"))
    dist = _fold(row.get("distrito"))
    hub_c = _compact(row.get("hub"))
    dist_c = _compact(row.get("distrito"))
    alvo_c = alvo.replace(" ", "")
    if alvo in (hub, dist):
        return 100
    if alvo_c and alvo_c in (hub_c, dist_c):
        return 95
    if alvo_c and (hub_c.startswith(alvo_c) or dist_c.startswith(alvo_c)):
        return 90
    palavras = [p for p in f"{hub} {dist}".split() if p]
    tokens = alvo.split()
    if tokens and all(
        any(p.startswith(t) or t.startswith(p) for p in palavras) for t in tokens
    ):
        return 85
    if len(alvo_c) >= 3 and (alvo_c in hub_c or alvo_c in dist_c):
        return 80
    ratio = max(
        SequenceMatcher(None, alvo, hub).ratio(),
        SequenceMatcher(None, alvo, dist).ratio(),
        SequenceMatcher(None, alvo_c, hub_c).ratio(),
        SequenceMatcher(None, alvo_c, dist_c).ratio(),
    )
    return int(ratio * 69)  # teto abaixo das faixas acima


def buscar_hub(texto: str, *, min_score: int = 50) -> tuple[str, list[str]]:
    """Casa o texto com a base: `(rotulo_exato, [])` ou `('', [sugestoes])`.

    So e "exato" quando **um unico** registro bate: "BUTANTA" (HEDBUT e HEBUT) e
    "CAMBUCI" (HEDVMN e HUBTAT) sao ambiguos de proposito e viram lista.
    """
    alvo = _fold(texto)
    if len(alvo) < 2:
        return "", []
    pontuados: list[tuple[int, str]] = []
    for row in load_hubs():
        score = _pontuar(row, alvo)
        if score >= min_score:
            pontuados.append((score, rotulo(row)))
    if not pontuados:
        return "", []
    pontuados.sort(key=lambda p: (-p[0], p[1]))
    exatos = [lab for sc, lab in pontuados if sc >= 100]
    if len(exatos) == 1:
        return exatos[0], []
    sugestoes: list[str] = []
    for _, lab in pontuados:
        if lab not in sugestoes:
            sugestoes.append(lab)
        if len(sugestoes) >= MAX_SUGESTOES:
            break
    return "", sugestoes


# --------------------------------------------------------------------------- #
# Perguntas do wizard (`fields_for`)
# --------------------------------------------------------------------------- #
def _hub_field(answers: dict) -> dict:
    """Texto livre ou lista numerada de sugestoes, conforme o estado da busca."""
    opcoes = answers.get("_hub_opcoes")
    bruto = str(answers.get("_hub_raw") or "").strip()
    if "hub" not in answers and isinstance(opcoes, list) and opcoes:
        prompt = "Escolha o hub/site por aproximação:"
        if bruto:
            prompt = f'Não localizei "{bruto}". Escolha o hub/site por aproximação:'
        return {
            "key": "hub",
            "type": "choice",
            "prompt": prompt,
            "choices": [str(x) for x in opcoes],
            "label": "Hub/site",
        }
    prompt = "Qual hub/site? Ex: VMN ou VILA MARIANA"
    if "hub" not in answers and bruto:
        prompt = f'Não localizei "{bruto}". {prompt}'
    return {"key": "hub", "type": "text", "prompt": prompt, "label": "Hub/site"}


def _incidente_field() -> dict:
    return {
        "key": "incidente",
        "type": "text",
        "prompt": "Qual número do Incidente? (RAL/REC/INM)",
        "label": "Incidente",
    }


def _qtd_field() -> dict:
    return {
        "key": "qtd_tecnicos",
        "type": "int",
        "prompt": "Quantos técnicos acessarão o local? Responda apenas o número (1 a 4)",
        "label": "Técnicos",
    }


def _motivo_field() -> dict:
    return {
        "key": "motivo",
        "type": "text",
        "prompt": "Informe o motivo do acesso",
        "label": "Motivo",
    }


def _tec_field(i: int, parte: str) -> dict:
    prompt = {
        "empresa": "Empresa? Ex: Claro, Procisa",
        "nome": "Nome Completo",
        "rg": "RG",
        "cpf": "CPF",
    }[parte]
    if parte == "empresa":
        prompt = f"Técnico {i}:\n{prompt}"
    return {
        "key": f"tec_{i}_{parte}",
        "type": "text",
        "prompt": prompt,
        "label": f"Técnico {i} — {TEC_LABELS[parte]}",
    }


def _tec_count(answers: dict) -> int:
    try:
        n = int(str(answers.get("qtd_tecnicos") or "").strip())
    except (TypeError, ValueError):
        return 0
    return n if MIN_TEC <= n <= MAX_TEC else 0


def fields_for(answers: dict) -> list[dict]:
    """Perguntas do momento: hub → nº de tecnicos → blocos de tecnico → motivo.

    A lista so cresce (os blocos entram depois do `qtd_tecnicos` respondido), por
    isso o indice guardado pelo engine (`script_field_index`) continua valido.
    """
    respostas = answers if isinstance(answers, dict) else {}
    fields = [_hub_field(respostas)]
    if "hub" not in respostas:
        return fields
    fields.append(_incidente_field())
    fields.append(_qtd_field())
    n = _tec_count(respostas)
    if not n:
        return fields
    for i in range(1, n + 1):
        for parte in TEC_KEYS:
            fields.append(_tec_field(i, parte))
    fields.append(_motivo_field())
    return fields


# --------------------------------------------------------------------------- #
# Validacao das respostas (`normalize_answer`)
# --------------------------------------------------------------------------- #
def cpf_valido(digitos: str) -> bool:
    """CPF com 11 digitos e digitos verificadores corretos."""
    d = "".join(c for c in str(digitos or "") if c.isdigit())
    if len(d) != 11 or d == d[0] * 11:
        return False
    for i in (9, 10):
        soma = sum(int(d[n]) * ((i + 1) - n) for n in range(i))
        if (soma * 10) % 11 % 10 != int(d[i]):
            return False
    return True


def formatar_cpf(digitos: str) -> str:
    d = "".join(c for c in str(digitos or "") if c.isdigit())
    if len(d) != 11:
        return d
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


def _aceita(value: str, state: dict | None = None) -> dict:
    return {"value": value, "state": state or {}, "retry": False, "message": ""}


def _recusa(message: str, state: dict | None = None) -> dict:
    return {"value": None, "state": state or {}, "retry": True, "message": message}


def _norm_hub(text: str, answers: dict) -> dict:
    bruto = str(text or "").strip()
    opcoes = answers.get("_hub_opcoes")
    if isinstance(opcoes, list) and opcoes and bruto.isdigit():
        n = int(bruto)
        if n < 1 or n > len(opcoes):
            return _recusa(f"Opção inválida. Escolha de 1 a {len(opcoes)}.")
        return _aceita(str(opcoes[n - 1]), {"_hub_opcoes": [], "_hub_raw": ""})
    # Com a lista na tela o tecnico pode ter digitado o nome de novo: refaz o
    # casamento como texto livre em vez de recusar (a lista volta se preciso).
    if len(_fold(bruto)) < 2:
        return _recusa("Informe o nome do hub/site (ex.: HUBSAM, SANTO AMARO).")
    exato, sugestoes = buscar_hub(bruto)
    if exato:
        return _aceita(exato, {"_hub_opcoes": [], "_hub_raw": ""})
    if sugestoes:
        # Sugestao apresentada NAO consome tentativa: vira lista numerada.
        return {
            "value": None,
            "state": {"_hub_opcoes": sugestoes, "_hub_raw": bruto},
            "retry": False,
            "message": "",
        }
    # Nada parecido: avisa pelo texto da propria pergunta e conta tentativa.
    return _recusa("", {"_hub_opcoes": [], "_hub_raw": bruto})


def _norm_qtd(text: str) -> dict:
    bruto = str(text or "").strip()
    if not bruto.isdigit():
        return _recusa("Informe apenas o número de técnicos (1 a 4).")
    n = int(bruto)
    if n < MIN_TEC or n > MAX_TEC:
        return _recusa(f"O limite é de {MAX_TEC} técnicos por acesso. Informe de 1 a {MAX_TEC}.")
    return _aceita(str(n))


def _norm_incidente(text: str) -> dict:
    """Texto livre (o tecnico escreve RAL/REC/INM como quiser), ate 15 caracteres."""
    valor = " ".join(str(text or "").split())
    if not valor:
        return _recusa("Informe o número do incidente (ex.: RAL-569474/2026).")
    if len(valor) > INC_MAX:
        return _recusa(f"O número do incidente deve ter até {INC_MAX} caracteres.")
    return _aceita(valor)


def _norm_motivo(text: str) -> dict:
    valor = " ".join(str(text or "").split())
    if len(valor) < 5:
        return _recusa("Informe o motivo do acesso (mínimo 5 caracteres).")
    return _aceita(valor)


def _norm_tecnico(parte: str, text: str) -> dict:
    bruto = " ".join(str(text or "").split())
    if parte == "empresa":
        if len(bruto) < 2:
            return _recusa("Informe a empresa do técnico (ex.: Claro, Procisa).")
        return _aceita(bruto)
    if parte == "nome":
        if len(bruto) < 5 or not any(c.isalpha() for c in bruto):
            return _recusa("Informe o nome completo do técnico.")
        return _aceita(bruto)
    if parte == "rg":
        valor = bruto.replace(" ", "").upper()
        if len(valor) < 4 or len(valor) > 15 or not any(c.isalnum() for c in valor):
            return _recusa("Informe um RG válido (ex.: 14253236-x).")
        return _aceita(valor)
    digitos = "".join(c for c in bruto if c.isdigit())
    if not cpf_valido(digitos):
        return _recusa("CPF inválido. Informe os 11 dígitos (ex.: 025.555.652-85).")
    return _aceita(formatar_cpf(digitos))


def normalize_answer(key: str, text: str, answers: dict) -> dict:
    """Valida/normaliza a resposta do wizard (contrato do `registry`)."""
    respostas = answers if isinstance(answers, dict) else {}
    chave = str(key or "")
    if chave == "hub":
        return _norm_hub(text, respostas)
    if chave == "qtd_tecnicos":
        return _norm_qtd(text)
    if chave == "incidente":
        return _norm_incidente(text)
    if chave == "motivo":
        return _norm_motivo(text)
    partes = chave.split("_")
    if len(partes) == 3 and partes[0] == "tec" and partes[2] in TEC_KEYS:
        return _norm_tecnico(partes[2], text)
    return _recusa("")


# --------------------------------------------------------------------------- #
# Texto do webhook e execucao (`run`)
# --------------------------------------------------------------------------- #
def _valor(answers: dict, key: str) -> str:
    return str((answers or {}).get(key) or "").strip()


def build_message(answers: dict) -> str:
    """Corpo do post, no mesmo layout usado pelo HUB DE ACESSO.

    Os dois espacos depois de "HUB DE ACESSO:" e "Tecnico N:" sao propositais
    (quebra de linha em Markdown).
    """
    respostas = answers if isinstance(answers, dict) else {}
    linhas = [
        f"HUB DE ACESSO:  {_valor(respostas, 'hub')}",
        "",
        f"Incidente: {_valor(respostas, 'incidente')}",
        "",
        f"Motivo: {_valor(respostas, 'motivo')}",
    ]
    for i in range(1, MAX_TEC + 1):
        prefixo = f"tec_{i}_"
        if not _valor(respostas, prefixo + "empresa"):
            break
        linhas.append("")
        linhas.append(f"Técnico {i}:  ")
        linhas.append(f"Empresa: {_valor(respostas, prefixo + 'empresa')}")
        linhas.append(f"Nome Completo: {_valor(respostas, prefixo + 'nome')}")
        linhas.append(f"RG: {_valor(respostas, prefixo + 'rg')}")
        linhas.append(f"CPF: {_valor(respostas, prefixo + 'cpf')}")
    return "\n".join(linhas)


def payload_ok(answers: dict) -> bool:
    """Só posta com hub, motivo e o bloco completo de todos os tecnicos."""
    if not _valor(answers, "hub") or not _valor(answers, "incidente"):
        return False
    if not _valor(answers, "motivo"):
        return False
    n = _tec_count(answers)
    if not n:
        return False
    for i in range(1, n + 1):
        for parte in TEC_KEYS:
            if not _valor(answers, f"tec_{i}_{parte}"):
                return False
    return True


def _ok(detail: str) -> dict:
    return {
        "ok": True,
        "message": SUCCESS_MESSAGE,
        "action": "reply_and_close",
        "detail": detail[:200],
    }


def _fail(detail: str) -> dict:
    return {
        "ok": False,
        "message": ERROR_MESSAGE,
        "action": "ask_and_transfer",
        "detail": str(detail or "")[:DETAIL_MAX],
    }


def _detail_api(code, body) -> str:
    prefix = f"http {code}"
    if not isinstance(body, dict) or not body:
        return prefix
    try:
        snippet = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return prefix
    if snippet in ("{}", "[]", "null"):
        return prefix
    return f"{prefix} {snippet}"[:DETAIL_MAX]


def _unpack(raw) -> tuple[int | None, dict]:
    if not isinstance(raw, tuple) or len(raw) < 2:
        return None, {}
    code, body = raw[0], raw[1]
    if code is not None:
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
    return code, body if isinstance(body, dict) else {}


async def _call_post(post, url: str, payload: dict, headers: dict, timeout: float):
    try:
        result = post(url, payload, headers, timeout)
    except TypeError:
        result = post(url, payload, headers)
    if asyncio.iscoroutine(result):
        return await result
    return result


def _erro_sem_url(erro: Exception) -> str:
    """Texto do erro sem URL: o webhook carrega token na propria URL (`sig`)."""
    texto = str(erro)
    for pedaco in texto.split():
        if "://" in pedaco:
            texto = texto.replace(pedaco, "<url>")
    return texto[:200]


async def run(ctx: dict, http_post=None) -> dict:
    """POST no webhook: 2xx encerra; falha transfere (webhook nao tem idempotencia)."""
    answers = ctx.get("answers") if isinstance(ctx.get("answers"), dict) else {}
    if not payload_ok(answers):
        return _fail("bad_answers")
    url = (env("WEBHOOK_ACESSO_HUB") or "").strip()
    if not url:
        return _fail("config")
    payload = {"text": build_message(answers)}
    headers = {"Content-Type": "application/json", "accept": "application/json"}
    post = http_post or _post_json
    try:
        if post is _post_json:
            code, body = await asyncio.to_thread(
                _post_json, url, payload, headers, HTTP_TIMEOUT
            )
        else:
            code, body = _unpack(await _call_post(post, url, payload, headers, HTTP_TIMEOUT))
    except Exception as e:
        log.warning("webhook acesso hub falhou: %s", _erro_sem_url(e))
        return _fail("http_error")
    if code is None:
        return _fail("http_error")
    if 200 <= code < 300:
        return _ok(f"http {code}")
    return _fail(_detail_api(code, body))
