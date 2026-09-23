# -*- coding: utf-8 -*-
"""Formulário de validação: detecção e identidade do solicitante (roteamento)."""
from __future__ import annotations

import unicodedata


def _deaccent(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).casefold()


def _parts(chat: dict | None) -> list[dict]:
    if not chat:
        return []
    p = chat.get("participants") or {}
    if isinstance(p, dict):
        return [v for v in p.values() if isinstance(v, dict)]
    return [x for x in p if isinstance(x, dict)] if isinstance(p, list) else []


def _addr(p) -> str:
    if isinstance(p, dict):
        return str(p.get("address") or "")
    return str(p or "")


def _login(v) -> str:
    x = str(v or '').strip().lower()
    if x.startswith('u:'):
        x = x[2:]
    at = x.find('@')
    return x[:at] if at >= 0 else x


def _is_operador(p, operadores) -> bool:
    """Participante e operador conhecido do time (nao e o solicitante)."""
    if not operadores:
        return False
    a = _addr(p)
    if not a:
        return False
    alvo = a.strip().lower()
    log = _login(alvo)
    for op in operadores:
        o = str(op or '').strip().lower()
        if not o:
            continue
        if o == alvo or _login(o) == log:
            return True
    return False


def is_system_or_staff(p, chat: dict | None = None, own: str | None = None) -> bool:
    addr = _addr(p)
    if not addr:
        return True
    if own and addr == own:
        return True
    if "CREATEDBY" in addr:
        return True
    role = ""
    if isinstance(p, dict):
        role = str(p.get("role_desc") or "")
    if "Moderator" in role:
        return True
    if chat:
        helpdesk = chat.get("helpdesk_address") or chat.get("channel_addr")
        if helpdesk and addr == str(helpdesk):
            return True
    return False


def is_validation_form(c: dict | None) -> bool:
    """Equivalente a closure_chat: formulário de encerramento/validação, não chat de fila."""
    if not c:
        return False
    if c.get("mode") == "message_scenario":
        return True
    fm = c.get("first_message")
    if not fm:
        return False
    if (fm.get("subject") or "") != "Solicitação de Encerramento":
        return False
    if "CREATEDBYMESSAGESCENARIO" in str(fm.get("author") or ""):
        return True
    if c.get("lifecycle") == "removed":
        return False
    ended = c.get("ended_time")
    if ended is not None:
        try:
            if float(ended) > 0:
                return False
        except (TypeError, ValueError):
            pass
    try:
        open_ended = ended is not None and float(ended) == -1
    except (TypeError, ValueError):
        open_ended = False
    return open_ended or c.get("lifecycle") in ("pending", "active")


def is_form_done(c: dict | None) -> bool:
    if not c:
        return True
    if c.get("lifecycle") == "removed":
        return True
    ended = c.get("ended_time")
    if ended is None:
        return False
    try:
        return float(ended) > 0
    except (TypeError, ValueError):
        return False


def is_open_validation_form(chat: dict | None) -> bool:
    return bool(chat) and is_validation_form(chat) and not is_form_done(chat)


def _ended_epoch(v) -> float | None:
    """`ended_time` que é um TIMESTAMP de verdade (segundos ou milissegundos).

    O payload do `message_scenario` às vezes traz números pequenos em campos de
    data (0, -1, 1, 2) — tratar qualquer `> 0` como "encerrado" derrubava
    formulário vivo do índice. Aqui só vale epoch plausível (>= 2001).
    """
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n > 1e12:
        n /= 1000.0
    return n if n >= 1e9 else None


def is_form_ended(c: dict | None) -> bool:
    """Fim EXPLÍCITO do formulário: lifecycle removido/finalizado ou data real."""
    if not c:
        return True
    ciclo = str(c.get("lifecycle") or "").strip().lower()
    if ciclo in ("removed", "finished", "closed", "ended"):
        return True
    return _ended_epoch(c.get("ended_time")) is not None


def is_trackable_form(c: dict | None) -> bool:
    """Formulário que ainda vale acompanhar (abertura form-aware e correlação).

    Mais tolerante que `is_open_validation_form`: só desiste com fim explícito,
    porque o payload do TOA pode não trazer `ended_time` confiável.
    """
    return is_validation_form(c) and not is_form_ended(c)


# Estados do formulário (espelham o `closureState` da extensão do TOA):
# pendente (ninguém pegou), em atendimento (analista entrou) e passado (analista
# saiu / encerrado). Só os dois primeiros interessam para a abertura.
FORM_PENDING = "pending"
FORM_DOING = "doing"
FORM_PAST = "past"


def _eh_humano(addr, own: str | None = None, tecnico: str | None = None) -> bool:
    """Endereço de gente (nem o bot, nem o agente de cenário, nem o técnico)."""
    a = str(addr or "").strip().lower()
    if not a:
        return False
    if own and a == str(own).strip().lower():
        return False
    if "createdby" in a:
        return False
    if tecnico and a == str(tecnico).strip().lower():
        return False
    return True


def form_state_from_messages(msgs, own: str | None = None, tecnico: str | None = None) -> str | None:
    """Estado do formulário a partir das MENSAGENS do chat dele (só leitura).

    Entrada do analista (`system_message: user_joined`) = em atendimento; saída
    do analista (`user_left`) = passado. Sem ninguém entrando, pendente.
    Devolve None quando não há mensagem nenhuma (TOA não respondeu): nesse caso
    quem chama NÃO deve decidir nada.
    """
    vistos = [m for m in (msgs or []) if isinstance(m, dict)]
    if not vistos:
        return None
    # A leitura vem em ordem descrescente: ordena para o ÚLTIMO evento valer.
    ordem = sorted(vistos, key=lambda m: (_epoch(m.get("time")) or 0.0, str(m.get("sequence_number") or "")))
    estado = FORM_PENDING
    for m in ordem:
        sysm = str(m.get("system_message") or "").strip().lower()
        if sysm not in ("user_joined", "user_left"):
            continue
        autor = m.get("author")
        if isinstance(autor, dict):
            autor = autor.get("address")
        if not autor and isinstance(m.get("participant"), dict):
            autor = m["participant"].get("address")
        if not _eh_humano(autor, own, tecnico):
            continue
        estado = FORM_DOING if sysm == "user_joined" else FORM_PAST
    return estado


def _msg_author(m) -> str:
    """Address de quem escreveu a mensagem (o TOA às vezes manda dict)."""
    if not isinstance(m, dict):
        return ""
    autor = m.get("author")
    if isinstance(autor, dict):
        autor = autor.get("address")
    if not autor and isinstance(m.get("participant"), dict):
        autor = m["participant"].get("address")
    return str(autor or "").strip()


def _ordem_msgs(msgs) -> list[dict]:
    """Mensagens em ordem cronológica (a leitura do TOA vem decrescente)."""
    vistos = [m for m in (msgs or []) if isinstance(m, dict)]
    return sorted(
        vistos,
        key=lambda m: (_epoch(m.get("time")) or 0.0, str(m.get("sequence_number") or "")),
    )


def _operadores_set(operadores) -> set[str]:
    return {str(o or "").strip().lower() for o in (operadores or ()) if str(o or "").strip()}


def form_authors(msgs, own: str | None = None, operadores=()) -> list[str]:
    """Técnicos que escreveram no formulário (autores humanos, ordem cronológica).

    É a identidade que sobra quando o payload do `message_scenario` chega SEM o
    address do técnico: ele responde no formulário, então aparece como autor.
    Staff, o bot, a fila (`h:`) e os analistas do time ficam de fora — senão o
    vínculo casaria o chat com o analista em vez do solicitante.
    """
    ops = _operadores_set(operadores)
    out: list[str] = []
    vistos: set[str] = set()
    for m in _ordem_msgs(msgs):
        a = _msg_author(m)
        baixo = a.lower()
        if not a or baixo in vistos or baixo in ops:
            continue
        if baixo.startswith("h:") or not _eh_humano(a, own):
            continue
        vistos.add(baixo)
        out.append(a)
    return out


def form_dono_from_messages(msgs, own: str | None = None, operadores=()) -> str:
    """Analista do time que ENTROU no formulário (`user_joined`), se houver.

    Mesmo dado que a extensão do TOA usa (`participants[].present`), lido do
    stream de mensagens: serve para o bot adotar o dono quando a entrega dele
    não foi registrada (o `move_chat` de um formulário já em atendimento é
    recusado pelo TOA com `e=10`).
    """
    ops = _operadores_set(operadores)
    if not ops:
        return ""
    dono = ""
    for m in _ordem_msgs(msgs):
        if str(m.get("system_message") or "").strip().lower() != "user_joined":
            continue
        a = _msg_author(m)
        if a and a.lower() in ops:
            dono = a
    return dono


def last_message_time(msgs) -> float | None:
    """Maior `time` (epoch de verdade) entre as mensagens — relógio de vida dele.

    Usa a mesma régua do `ended_time` (epoch >= 2001): `time` pequeno (0, 1, 2)
    não é data — devolve None e quem chama usa o relógio do índice, senão um
    payload sem timestamp faria o formulário parecer ter décadas e ser fechado.
    """
    maior = None
    for m in _ordem_msgs(msgs):
        t = _ended_epoch(m.get("time"))
        if t is not None and (maior is None or t > maior):
            maior = t
    return maior


def chat_opener(chat: dict | None, own: str | None = None) -> dict:
    """Quem abriu o chat de fila (o técnico) — alias público de `_chat_opener`."""
    return _chat_opener(chat, own)


def form_fid(form) -> int:
    """`chat_id` do formulário (0 quando o payload não traz id numérico)."""
    if not isinstance(form, dict):
        return 0
    try:
        return int(form.get("chat_id") or form.get("chatId") or 0)
    except (TypeError, ValueError):
        return 0


def form_requester(form: dict | None, own: str | None = None) -> dict:
    """Quem pediu o formulário: addresses confiáveis + nome de exibição.

    O formulário do TOA (`message_scenario`) normalmente NÃO traz o address do
    técnico; quando isso acontece `addrs` fica vazio e só o nome sobra (que no
    caso do cenário é o nome do agente, não do técnico).
    """
    addrs: list[str] = []
    vistos: set[str] = set()
    for p in _parts(form):
        if not p or is_system_or_staff(p, form, own):
            continue
        a = _addr(p)
        if a and a.strip().lower() not in vistos:
            addrs.append(a)
            vistos.add(a.strip().lower())
    inicio = str((form or {}).get("started_addr") or "")
    if inicio and not is_system_or_staff({"address": inicio}, form, own) and inicio.strip().lower() not in vistos:
        addrs.append(inicio)
        vistos.add(inicio.strip().lower())
    nome = _display_name(form)
    if nome == "Solicitação de Encerramento" or nome.startswith("Sem identificação"):
        nome = ""
    return {"addrs": addrs, "name": nome}


def _epoch(v) -> float | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n / 1000.0 if n > 1e12 else n


def _form_recency(form: dict) -> float:
    """Recência do formulário: data do TOA quando existir, senão o chat_id."""
    for chave in ("started_time", "started_at", "created_time", "time"):
        v = _epoch((form or {}).get(chave))
        if v is not None:
            return v
    try:
        return float((form or {}).get("chat_id") or (form or {}).get("chatId") or 0)
    except (TypeError, ValueError):
        return 0.0


def _queue_of(chat: dict | None) -> str:
    return str((chat or {}).get("helpdesk_address") or (chat or {}).get("channel_addr") or "")


def _chat_opener(chat: dict | None, own: str | None = None) -> dict:
    """Quem abriu o chat de fila (o técnico), com o nome só quando é confiável."""
    started = str((chat or {}).get("started_addr") or "")
    address = started if started and not is_system_or_staff({"address": started}, chat, own) else ""
    nome = _display_name(chat)
    fila = _queue_of(chat)
    canal = str((chat or {}).get("channel_name") or "")
    if not nome or nome.startswith("Sem identificação") or nome in (fila, canal):
        nome = ""
    return {"address": address, "name": nome}


def form_candidates(queue_chat: dict | None, forms, *, linked=(), own: str | None = None) -> list[dict]:
    """Formulários abertos que podem ser do mesmo técnico: mesma fila e livres.

    `linked` são os formulários já amarrados a um chat aberto (não podem ser
    reusados). Sem fila no chat não há candidato: o vínculo seria adivinhação.
    """
    fila = _queue_of(queue_chat)
    if not fila:
        return []
    presos = {int(x) for x in (linked or ()) if x not in (None, "")}
    out: list[dict] = []
    for f in forms or []:
        if not is_trackable_form(f):
            continue
        try:
            fid = int(f.get("chat_id") or f.get("chatId"))
        except (TypeError, ValueError):
            continue
        if fid in presos:
            continue
        if _queue_of(f) != fila:
            continue
        out.append(f)
    return sorted(out, key=_form_recency)


def form_by_technician(
    queue_chat: dict | None,
    forms,
    own: str | None = None,
    *,
    match_name: bool = False,
    linked=(),
    authors: dict | None = None,
    sem_dono=None,
) -> tuple[dict | None, str]:
    """Formulário aberto do MESMO técnico que abriu o chat: (formulário, regra).

    Ordem: address do payload → AUTOR das mensagens do formulário (a identidade
    que sobra quando o `message_scenario` chega sem o address do técnico) →
    nome (quando `affinity_match_name`) → fila, e a fila **só com um único
    candidato livre e SEM responsável**.

    `sem_dono` é o conjunto de ids ainda sem analista (nem no ledger, nem pelas
    mensagens). Formulário já em atendimento só entra por identidade: usá-lo no
    palpite de recência entregava ao técnico o formulário de outra pessoa (o
    caso que "trocava de analista" no painel).

    Com dois ou mais candidatos sem identidade não há vínculo: o motivo da
    desistência vai no diagnóstico (`ambiguo:<n>` ou `em_atendimento:<n>`).
    """
    candidatos = form_candidates(queue_chat, forms, linked=linked, own=own)
    if not candidatos:
        return None, ""
    abertura = _chat_opener(queue_chat, own)
    alvo = str(abertura.get("address") or "").strip().lower()
    if alvo:
        for f in candidatos:
            if alvo in {a.strip().lower() for a in form_requester(f, own)["addrs"]}:
                return f, "address"
        mapa = authors or {}
        for f in candidatos:
            if alvo in {str(a).strip().lower() for a in (mapa.get(form_fid(f)) or ())}:
                return f, "autor"
    if match_name and abertura.get("name"):
        nome = _deaccent(abertura["name"])
        for f in candidatos:
            outro = form_requester(f, own)["name"]
            if outro and _deaccent(outro) == nome:
                return f, "name"
    if sem_dono is None:
        livres = list(candidatos)
    else:
        filtro = {int(x) for x in sem_dono}
        livres = [f for f in candidatos if form_fid(f) in filtro]
    if len(livres) == 1:
        return livres[0], "fila"
    if not livres and len(candidatos) == 1:
        # Único candidato da fila e já em atendimento: é o técnico abrindo o chat
        # para falar do próprio formulário (o texto diz com quem ele está).
        return candidatos[0], "fila"
    if not livres:
        return None, "em_atendimento:%d" % len(candidatos)
    return None, "ambiguo:%d" % len(livres)


def _display_name(chat: dict | None) -> str:
    if not chat:
        return ""
    for key in ("person", "name", "started_name"):
        v = str(chat.get(key) or "").strip()
        if v:
            return v
    started = str(chat.get("started_addr") or "")
    for p in _parts(chat):
        if _addr(p) == started:
            n = str(p.get("name") or "").strip()
            if n:
                return n
    for p in _parts(chat):
        n = str(p.get("name") or "").strip()
        if n:
            return n
    return ""


# Nomes que NAO identificam uma pessoa: o agente de cenario do TOA e os rotulos
# de "sem identificacao". Usar esses nomes como chave de caso juntava tecnicos
# DIFERENTES no mesmo caso — era o "balde" `n:message scenario agent` que fazia
# a afinidade entregar tudo para o mesmo analista.
_NOMES_GENERICOS = (
    "message scenario agent",
    "mensagem de cenario",
    "solicitacao de encerramento",
    "sem identificacao",
)


def _nome_generico(nome: str) -> bool:
    n = _deaccent(str(nome or "")).strip()
    if not n:
        return True
    return any(n == g or n.startswith(g) for g in _NOMES_GENERICOS)


def nome_generico(nome: str) -> bool:
    """Nome que NÃO identifica pessoa (agente de cenário, "sem identificação")."""
    return _nome_generico(nome)


def requester_key(chat: dict | None, own: str | None = None, operadores=()) -> tuple:
    """Identidade do caso: quem pediu, nunca staff nem operador do time.

    Devolve (case_key, address, nome, origem). Formulario e chat do MESMO
    tecnico produzem a mesma chave, que amarra o caso (afinidade e carga).
    Sem identidade de gente (payload do formulario sem o address do tecnico, ou
    so com o nome do agente de cenario), a chave e **por chat** (`chat:<id>`):
    cada item vira um caso proprio e nunca se mistura com outro solicitante.
    """
    if not chat:
        return '', '', '', ''
    inicio = str(chat.get("started_addr") or '')
    if inicio and not is_system_or_staff({"address": inicio}, chat, own) and not _is_operador({"address": inicio}, operadores):
        return 'a:' + inicio.lower(), inicio, _display_name(chat), 'address'
    for p in _parts(chat):
        if not p or is_system_or_staff(p, chat, own) or _is_operador(p, operadores):
            continue
        a = _addr(p)
        if a:
            return 'a:' + a.lower(), a, _display_name(chat), 'address'
    nome = _display_name(chat)
    cid = chat.get("chat_id") or chat.get("chatId")
    cid_txt = ''
    if cid is not None:
        try:
            cid_txt = str(int(cid))
        except (TypeError, ValueError):
            cid_txt = ''
    if _nome_generico(nome):
        if cid_txt:
            return 'chat:' + cid_txt, '', nome, 'chat'
        return '', '', nome, ''
    return 'n:' + _deaccent(nome), '', nome, 'name'
