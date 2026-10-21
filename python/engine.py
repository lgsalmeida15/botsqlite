from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import db
from . import form_open
from . import resource_profile
from . import routing
from .resume import resume_action
# A lógica vive em routing.py; aqui ficam reexportados para o resto do engine
# e para os testes de janela de plantão.
from .routing import (  # noqa: F401
    TZ_BRT,
    _addr_eq,
    _as_clock,
    _in_route_window,
    _member_login,
    _member_login_raw,
    _window_label,
)
from .scripts import registry
from .scripts.criar_apoio import BUDGET_SEC, parse_started_at
from .toa import ToaSocket

log = logging.getLogger("helptoa.engine")
MEM: dict[int, dict] = {}

# Teto de tempo que um formulario fica no indice sem nenhum sinal novo (secao 4.1):
# e so a rede de seguranca para um payload de fim que nunca chegou.
_FORM_INDEX_TTL_SEC = 12 * 3600.0

# De quanto em quanto tempo revalidamos o estado de UM formulario do indice
# (pendente / em atendimento / passado) no TOA.
_FORM_STATE_TTL_SEC = 120.0

# Regras de vinculo chat<->formulario com identidade CONFIRMADA (address do
# payload, AUTOR das mensagens do formulario ou nome). So elas podem reescrever
# o caso do formulario (`relink_case`): `fila` e palpite por recencia e movia o
# dono do formulario de outro tecnico para este caso.
_REGRAS_IDENTIDADE = ("address", "autor", "name")

# Status em que o bot espera resposta do tecnico. A lista e uma so de proposito:
# ficava repetida e foi esquecer o status novo em um dos pontos que engoliu a
# resposta do tecnico.
_REPLY_STATUSES = (
    "awaiting_reply",
    "awaiting_script_answer",
    "awaiting_script_confirm",
)

# Quantos chats o bot DEVOLVE ao quadro por tick quando nao pode atuar (fila
# desabilitada no painel ou bot desligado). Cada devolucao e um `leave_chat` no
# TOA: o limite evita rajada quando o operador desliga uma fila com muita gente
# dentro — o resto sai nos ciclos seguintes.
_LEAVE_BUDGET = 10


def _parts(chat: dict) -> list[dict]:
    p = chat.get("participants") or {}
    if isinstance(p, dict):
        return list(p.values())
    return list(p) if isinstance(p, list) else []


def is_new(chat: dict, own: str) -> bool:
    if not (chat.get("helpdesk_address") or chat.get("channel_addr")):
        return False
    for p in _parts(chat):
        if "Moderator" in str(p.get("role_desc") or "") and p.get("present"):
            return False
        if p.get("address") == own and p.get("present"):
            return False
    return True


def _online_logins_from_ack(ack: dict) -> list[str] | None:
    """Lista que o TOA devolveu, ou None se timeout/resposta inútil."""
    if not isinstance(ack, dict) or ack.get("timeout"):
        return None
    d = ack.get("d") if isinstance(ack.get("d"), dict) else ack
    raw = d.get("result") if isinstance(d, dict) else None
    if not isinstance(raw, list):
        return None
    return [str(x).strip() for x in raw if x]


def queue_of(chat: dict) -> str | None:
    return chat.get("helpdesk_address") or chat.get("channel_addr")


def closure_chat(c: dict | None) -> bool:
    """Replica closureChat de app/app.js — formulário de encerramento, não chat de fila."""
    return form_open.is_validation_form(c)


def remember(chat: dict) -> int | None:
    cid = chat.get("chat_id") or chat.get("chatId")
    if cid is None:
        return None
    cid = int(cid)
    prev = MEM.get(cid, {})
    raw = dict(prev.get("raw") or {})
    raw.update(chat)
    # Nome de quem abriu o chat, pego do payload desde a 1a leitura (sem esperar
    # ledger/rota/formulario). Pegajoso como o started_addr: uma vez resolvido,
    # nao se perde se uma leitura seguinte vier sem participante/nome.
    started_name = prev.get("started_name") or form_open.chat_opener(raw).get("name")
    MEM[cid] = {
        "started_addr": chat.get("started_addr") or prev.get("started_addr") or raw.get("started_addr"),
        "started_name": started_name,
        "queue_address": queue_of(chat) or prev.get("queue_address"),
        "originator_spoke": prev.get("originator_spoke", False),
        "bot_present": prev.get("bot_present", False),
        "raw": raw,
    }
    return cid


def _msg_epoch(v) -> float | None:
    if v is None:
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n > 1e12:
        n /= 1000.0
    return n


def _as_utc(v) -> datetime | None:
    """Data do banco (ISO com ou sem timezone) como datetime UTC."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if v is None or v == "":
        return None
    try:
        ts = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _last_activity(row: dict) -> datetime | None:
    """Marca mais recente entre a ultima pergunta do bot e a ultima resposta do tecnico."""
    marcas = [t for t in (_as_utc(row.get("last_prompt_at")), _as_utc(row.get("last_reply_at"))) if t]
    return max(marcas) if marcas else None


def _offline_seconds(valor) -> float:
    """Segundos desde o carimbo de offline (0 quando nao da para ler a data)."""
    quando = _as_utc(valor)
    if quando is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - quando).total_seconds())


def _silence_expired(row: dict, seconds: int, *, now: datetime | None = None) -> bool:
    """Silencio do tecnico por `seconds`.

    Vale a marca mais recente entre a ultima pergunta do bot e a ultima resposta
    do tecnico: quem respondeu (mesmo resposta invalida) nao pode ser encerrado
    por timeout por causa de uma resposta que o bot perdeu.
    """
    quando = _last_activity(row)
    if quando is None:
        return False
    limite = (now or datetime.now(timezone.utc)) - timedelta(seconds=max(int(seconds), 1))
    return quando < limite


def _messages_from_ack(ack) -> list[dict]:
    """Mensagens de um ACK de get_chat_messages (usado no join e no resume)."""
    if not isinstance(ack, dict) or ack.get("timeout"):
        return []
    d = ack.get("d") if isinstance(ack.get("d"), dict) else ack
    if not isinstance(d, dict):
        return []
    raw = d.get("messages") or d.get("result") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    return [m for m in raw if isinstance(m, dict)]


def _before_menu(m: dict, row: dict) -> bool:
    sent = row.get("menu_sent_at")
    if not sent:
        return False
    t = _msg_epoch(m.get("time"))
    if t is None:
        return False
    sent_ts = sent.timestamp() if hasattr(sent, "timestamp") else _msg_epoch(sent)
    if sent_ts is None:
        return False
    return t < sent_ts - 0.5


def _script_answers(row: dict) -> dict:
    try:
        data = json.loads(row.get("script_answers") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _field_choices(field: dict) -> list[str]:
    raw = field.get("choices")
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _field_prompt(field: dict) -> str:
    prompt = str(field.get("prompt") or "")
    if str(field.get("type") or "text") != "choice":
        return prompt
    choices = _field_choices(field)
    if not choices:
        return prompt
    lines = [prompt] if prompt else []
    for i, label in enumerate(choices, start=1):
        lines.append(f"{i}) {label}")
    return "\n".join(lines)


def _validate_script_value(field: dict, text: str) -> str | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    typ = str(field.get("type") or "text")
    if typ == "int":
        if not raw.isdigit():
            return None
        return str(int(raw))
    if typ == "choice":
        choices = _field_choices(field)
        if not choices or not raw.isdigit():
            return None
        n = int(raw)
        if n < 1 or n > len(choices):
            return None
        return choices[n - 1]
    return raw


def _confirm_text(spec: dict, answers: dict, note: str = "") -> str:
    lines = ["Confirme as informações:", ""]
    for f in spec.get("fields") or []:
        if f.get("confirm") is False:
            continue
        key = str(f.get("key") or "")
        label = str(f.get("label") or f.get("prompt") or key).rstrip(" ?")
        lines.append(f"{label}: {answers.get(key, '')}.")
    lines.append("")
    if str(note or "").strip():
        lines.append(str(note).strip())
        lines.append("")
    lines.append("Digite:")
    lines.append("1) Confirmar")
    lines.append("2) Corrigir")
    return "\n".join(lines)


def _sql_epoch(valor) -> float | None:
    """Converte 'YYYY-MM-DD HH:MM:SS' (UTC, como o SQLite grava) em epoch."""
    txt = str(valor or "").strip()
    if not txt:
        return None
    try:
        return datetime.strptime(txt[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (TypeError, ValueError):
        return None


def _case_key_formulario(req: tuple, *fontes: str, autores=()) -> tuple[str, str]:
    """(chave, origem) do caso de um formulario, sem "balde" de identidade.

    Ordem: address ja ligado ao caso (rota/pedido — é o vínculo chat↔formulário)
    → address do payload → AUTOR das mensagens do formulário (a identidade que
    sobra quando o payload nao traz o address do tecnico) → chave `chat:` ja
    ligada → vazio (o chamador usa `chat:<id do formulario>`). Chave de **nome
    generico de agente** (`n:message scenario agent`) nunca vira caso: era ela
    que juntava tecnicos diferentes no mesmo dono e concentrava tudo num analista.
    """
    do_payload = str((req[0] if req else "") or "").strip()
    lista = [str(f or "").strip() for f in fontes]
    for k in lista:
        if k.startswith("a:"):
            return k, "address"
    if do_payload.startswith("a:"):
        return do_payload, "address"
    for a in autores or ():
        k = str(a or "").strip().lower()
        if k.startswith("a:"):
            return k, "autor"
    for k in lista + [do_payload]:
        if k.startswith("chat:"):
            return k, "chat"
    return "", ""


def _form_shape(chat: dict) -> str:
    """Forma do payload do formulario (diagnostico, sem texto de conversa)."""
    fm = chat.get("first_message")
    fm = fm if isinstance(fm, dict) else {}
    return (
        "mode=%s; lifecycle=%s; ended_time=%s; first_message=%s; subject=%s"
        % (
            chat.get("mode") if chat.get("mode") is not None else "—",
            chat.get("lifecycle") if chat.get("lifecycle") is not None else "—",
            chat.get("ended_time") if chat.get("ended_time") is not None else "—",
            "sim" if fm else "nao",
            (fm.get("subject") or "—") if fm else "—",
        )
    )


def _choice_of(text: str) -> str:
    """Escolha da abertura form-aware: '1' (formulario), '2' (menu) ou ''."""
    t = str(text or "").strip().lower()
    if not t:
        return ""
    primeiro = t.split()[0]
    if primeiro in ("1", "1)", "1.", "1-"):
        return "1"
    if primeiro in ("2", "2)", "2.", "2-"):
        return "2"
    if t.startswith("falar"):
        return "1"
    if t.startswith("abrir") or t.startswith("menu"):
        return "2"
    return ""


def _presentes(chat: dict) -> list[str]:
    """Enderecos PRESENTES no chat (`participants` com `present`).

    Aceita as duas formas do TOA: dict address->participante (o address sai da
    CHAVE quando o payload nao o repete dentro) e lista de participantes.
    """
    parts = chat.get("participants") or {}
    if isinstance(parts, dict):
        itens = list(parts.items())
    elif isinstance(parts, list):
        itens = [(p.get("address"), p) for p in parts if isinstance(p, dict)]
    else:
        return []
    vistos: list[str] = []
    for chave, p in itens:
        if not isinstance(p, dict):
            p = {"present": True}
        if not p.get("present"):
            continue
        addr = str(p.get("address") or chave or "")
        if addr:
            vistos.append(addr)
    return vistos


def _own_present_in(chat: dict, own: str) -> bool:
    """Replica precisaAssumir invertido: participants[own].present."""
    if not own:
        return False
    parts = chat.get("participants") or {}
    if isinstance(parts, dict):
        items = list(parts.items())
    elif isinstance(parts, list):
        items = [(p.get("address"), p) for p in parts if isinstance(p, dict)]
    else:
        return False
    for addr, p in items:
        if not isinstance(p, dict):
            p = {"address": addr, "present": True}
        if not p.get("present"):
            continue
        if _addr_eq(p.get("address") or addr, own):
            return True
    return False


class Engine:
    def __init__(self, conn, sock: ToaSocket):
        self.conn = conn
        self.sock = sock
        self.cfg = db.settings(conn)
        self.tpl = db.templates(conn)
        self.enabled = db.enabled_queues(conn)
        self._joining: int | None = None
        self._open_forms: dict[int, dict] = {}
        self._form_seen: dict[int, float] = {}
        self._form_not_open: set[int] = set()
        self._form_state: dict[int, str] = {}
        self._form_checked: dict[int, float] = {}
        # Identidade viva do formulario, lida das MENSAGENS dele (sem chamada
        # extra): autores humanos (o tecnico escreve ali) e o analista do time
        # que entrou (`user_joined`).
        self._form_authors: dict[int, list[str]] = {}
        self._form_owner_msg: dict[int, str] = {}
        # Relogio da vida do formulario: epoca da ultima mensagem lida (o indice
        # `_form_seen`, monotonico, e o fallback quando o TOA nao devolve `time`).
        self._form_last_msg: dict[int, float] = {}
        # Outras referencias de vida: criacao do formulario (`started_time` do
        # closure) e abertura do caso no ledger (`assigned_at`). A janela de vida
        # usa a MAIS RECENTE — sem isso um formulario de horas atras cairia no
        # relogio do boot e nunca fecharia.
        self._form_started: dict[int, float] = {}
        self._form_case_at: dict[int, float] = {}
        # Leituras consecutivas sem o formulario nas listas do TOA (fechamento por
        # ausencia: e assim que o caso resolvido no TOA libera a vaga do analista).
        self._form_absent: dict[int, int] = {}
        # Mesma regua para o caso de CHAT: ausente das listas do TOA por N leituras
        # boas = resolvido (`chat_resolvido_toa`).
        self._chat_absent: dict[int, int] = {}
        self._msg_ignored: set[int] = set()
        self._script_inflight: set[int] = set()
        self._route_lock = asyncio.Lock()
        self._reserve: dict[str, list] = {}
        self._recent: dict[int, list[dict]] = {}
        # Fila em duas linhas (chat e formulario): de qual delas o tick comeca.
        self._turno_fila = 0

    def reload_config(self) -> None:
        self.cfg = db.settings(self.conn)
        self.tpl = db.templates(self.conn)
        self.enabled = db.enabled_queues(self.conn)
        self._sync_own_address()

    @property
    def own(self) -> str:
        return self.sock.own_address

    def _sync_own_address(self) -> None:
        """Publica no banco o endereco do PROPRIO bot.

        Ele nunca pode ser alvo de rota: o caso voltaria para o bot, que so
        repassa (e a carga dele nunca aparece no TOA). O painel usa este valor
        para recusar o cadastro do bot como operador de um time.
        """
        own = str(self.own or "").strip()
        if not own or str(self.cfg.get("bot_own_address") or "") == own:
            return
        try:
            db.set_setting(self.conn, "bot_own_address", own)
        except Exception:
            log.exception("gravando bot_own_address")
            return
        self.cfg["bot_own_address"] = own

    @property
    def bot_on(self) -> bool:
        return (self.cfg.get("bot_enabled") or "true") == "true"

    def _mark_present(self, cid: int | None) -> None:
        if cid is None:
            cid = self._joining
        if cid is None:
            return
        MEM.setdefault(int(cid), {})["bot_present"] = True

    def _join_payload_is_self(self, d: dict | None) -> bool:
        if not isinstance(d, dict):
            return False
        if not d.get("present"):
            return False
        return _addr_eq(d.get("address"), self.own)

    def _note_join_response(self, d: dict | None) -> None:
        if not self._join_payload_is_self(d):
            return
        cid = d.get("chat_id") or d.get("chatId") or self._joining
        self._mark_present(int(cid) if cid is not None else None)

    async def on_action(self, a: str, d: dict) -> None:
        if a == "get_helpdesk_details":
            self._ingest_queues(d)
        elif a in ("connect", "reconnect") and d.get("helpdesks"):
            self._ingest_queues(d["helpdesks"])
        elif a == "join_chat":
            self._note_join_response(d)
        elif a == "get_helpdesk_chats":
            for c in d.get("chats") or d.get("result") or []:
                await self.consider(c)
        elif a == "get_active_chats":
            for c in d.get("chats") or d.get("result") or []:
                if not isinstance(c, dict):
                    continue
                remember(c)
                self._note_open_form(c, "active")
                if _own_present_in(c, self.own):
                    kid = c.get("chat_id") or c.get("chatId")
                    if kid is not None:
                        self._mark_present(int(kid))
        elif a == "get_contact_list":
            raw = d.get("contacts") or d.get("contact_list") or d.get("result") or d
            items = raw if isinstance(raw, list) else list(raw.values()) if isinstance(raw, dict) else []
            for c in items:
                if not isinstance(c, dict):
                    continue
                addr = str(c.get("address") or "")
                if addr.startswith("h:"):
                    db.upsert_queue(self.conn, addr, c.get("name") or c.get("description") or "")
            self.enabled = db.enabled_queues(self.conn)
        elif a == "search_contact":
            await self._ingest_search_contact(d)

    async def on_note(self, n: str, d: dict) -> None:
        if n == "helpdesk_chat_received" and d.get("chat"):
            await self.consider(d["chat"])
        elif n == "message_received":
            await self.on_message(d)
        elif n == "helpdesk_chat_removed" and d.get("chat_id"):
            cid = int(d["chat_id"])
            # Aviso de FILA (o `move_chat` para o analista gera um destes): o
            # formulario continua aberto — nao sai do indice por isso.
            if cid in self._open_forms:
                self._emit("form_queue_left", cid, None, "saiu da fila do helpdesk; indice mantido")
            if self._joining is not None and cid == self._joining:
                self._mark_present(cid)
        elif n == "chat_removed" and d.get("chat_id"):
            cid = int(d["chat_id"])
            self._forget_open_form(cid, "chat_removed")
            self._close_case(cid, "chat_removed")
            db.finish_routes_for_chat(self.conn, cid, "failed", "chat removido")
            self._recent.pop(cid, None)
            q = MEM.get(cid, {}).get("queue_address")
            db.upsert_chat(self.conn, cid, status="left", left_at=datetime.now(timezone.utc))
            db.log_event(self.conn, "left", chat_id=cid, queue_address=q, queue_enabled=q in self.enabled if q else None, detail="chat_removed")

    def _ingest_queues(self, raw) -> None:
        items = []
        if isinstance(raw, list):
            items = [(q.get("address") or q.get("helpdesk_address"), q) for q in raw]
        elif isinstance(raw, dict):
            if raw.get("address"):
                items = [(raw.get("address"), raw)]
            else:
                items = list(raw.items())
        for addr, q in items:
            if addr and "@" in str(addr):
                db.upsert_queue(self.conn, str(addr), q.get("name") or q.get("description") or "")
        self.enabled = db.enabled_queues(self.conn)

    async def _ingest_search_contact(self, d: dict) -> None:
        raw = d.get("result") if isinstance(d.get("result"), dict) else d
        if not isinstance(raw, dict):
            return
        n_ops, n_queues = await asyncio.to_thread(db.ingest_search_contact, raw)
        self.enabled = db.enabled_queues(self.conn)
        log.info("search_contact: %s operadores em %s filas", n_ops, n_queues)

    def _author_do_chat(self, cid: int) -> tuple[str, str]:
        """(endereco, nome) do autor do chat/formulario para gravar no evento.

        Ordem: ledger aberto (ja tem identidade gravada) -> rota pendente
        (requester_addr) -> indice de formularios abertos (payload vivo) ->
        snapshot de entrada do chat (started_addr/started_name, gravado pelo
        `remember` desde a 1a leitura — cobre a triagem inicial, antes de
        existir ledger/rota/formulario). Sem identidade confiavel devolve
        vazio: o painel cai no endereco ou mostra "-" — nunca inventa nome.
        """
        try:
            linha = db.open_assignment(self.conn, cid)
            if linha:
                addr = str(linha.get("requester_addr") or "")
                nome = str(linha.get("requester_name") or "")
                if addr or nome:
                    return addr, nome
        except Exception:
            pass
        try:
            rota = self.conn.execute(
                "SELECT requester_addr FROM pending_routes"
                " WHERE chat_id = ? AND requester_addr != '' ORDER BY id DESC LIMIT 1",
                (int(cid),),
            ).fetchone()
            if rota and str(rota.get("requester_addr") or ""):
                return str(rota["requester_addr"]), ""
        except Exception:
            pass
        payload = self._open_forms.get(int(cid))
        if payload:
            req = form_open.requester_key(payload, self.own)
            if req[1] or req[2]:
                return str(req[1] or ""), str(req[2] or "")
        mem = MEM.get(int(cid)) or {}
        addr = str(mem.get("started_addr") or "")
        nome = str(mem.get("started_name") or "")
        if addr or nome:
            return addr, nome
        return "", ""

    def _emit(self, reason: str, cid: int | None, q: str | None, detail: str = "") -> None:
        on = bool(q and q in self.enabled)
        addr, nome = self._author_do_chat(int(cid)) if cid is not None else ("", "")
        db.log_event(
            self.conn, reason, chat_id=cid, queue_address=q,
            queue_enabled=on if q else None, detail=detail,
            author_addr=addr, author_name=nome,
        )

    def _note_open_form(self, chat: dict | None, origem: str = "closure") -> None:
        """Mantem o indice de formularios acompanhados (abertura form-aware).

        Tolerante de proposito: so sai do indice com **fim explicito** ou com o
        `chat_removed`. Payload pobre (sem `mode`/`first_message`) nao derruba
        mais o formulario, e `helpdesk_chat_removed` — aviso de FILA, que o
        `move_chat` para o analista gera — tambem nao.
        """
        if not chat:
            return
        kid = chat.get("chat_id") or chat.get("chatId")
        if kid is None:
            return
        cid = int(kid)
        if form_open.is_trackable_form(chat):
            novo = cid not in self._open_forms
            self._open_forms[cid] = dict(chat)
            # O TTL conta INATIVIDADE: cada sinal novo renova o relogio.
            self._form_seen[cid] = time.monotonic()
            # `started_time` do closure: relogio de criacao do formulario (a
            # referencia que sobra quando as mensagens nao trazem `time`).
            inicio = form_open.form_started_epoch(chat)
            if inicio and not self._form_started.get(cid):
                self._form_started[cid] = float(inicio)
            if novo:
                self._emit(
                    "form_indexed", cid, queue_of(chat),
                    f"origem={origem}; {_form_shape(chat)}",
                )
            return
        if form_open.is_validation_form(chat):
            # Formulario com fim explicito: sai do indice e fecha o caso.
            if cid in self._open_forms:
                self._emit("form_dropped", cid, queue_of(chat), f"origem={origem}; fim explicito")
            self._open_forms.pop(cid, None)
            self._form_seen.pop(cid, None)
            if cid not in self._form_not_open:
                self._form_not_open.add(cid)
                self._emit(
                    "form_not_open", cid, queue_of(chat),
                    f"origem={origem}; {_form_shape(chat)}",
                )
            self._close_case(cid, "form_done")
            return
        if cid in self._open_forms and cid not in self._form_not_open:
            # Payload sem sinal de formulario: o antigo descartava o indice aqui
            # (era uma das causas silenciosas da abertura nao acontecer).
            self._form_not_open.add(cid)
            self._emit(
                "form_ignored_payload", cid, queue_of(chat),
                f"origem={origem}; payload sem sinal de formulario; indice mantido",
            )

    def _forget_open_form(self, cid: int | None, motivo: str = "") -> None:
        """Tira o formulario do indice (remocao real): o caso segue o fluxo dele."""
        if cid is None:
            return
        cid = int(cid)
        if self._open_forms.pop(cid, None) is not None:
            self._form_seen.pop(cid, None)
            self._form_authors.pop(cid, None)
            self._form_owner_msg.pop(cid, None)
            self._form_last_msg.pop(cid, None)
            self._form_started.pop(cid, None)
            self._form_case_at.pop(cid, None)
            self._emit("form_forget", cid, None, f"motivo={motivo or 'nao informado'}")

    def _prune_open_forms(self) -> int:
        """Tira do indice o que nao tem mais o que oferecer.

        Duas redes: formulario no indice ha tempo demais (TTL) e formulario cujo
        caso no ledger ja foi **encerrado de verdade** (`form_done`/`chat_removed`).
        Caso expirado por TTL/`stale_boot` NAO descarta: o formulario pode estar
        aberto no TOA (`db.form_case_state` devolve "unknown" nesses casos).
        """
        agora = time.monotonic()
        velhos = [fid for fid, visto in self._form_seen.items() if agora - visto >= _FORM_INDEX_TTL_SEC]
        for fid in velhos:
            self._forget_open_form(fid, "ttl_indice")
        # Sem isso o indice so crescia e a varredura seguia lendo no TOA
        # formularios que a abertura ja descartava (`encerrados=` no diagnostico).
        fantasmas = [
            fid for fid in list(self._open_forms)
            if db.form_case_state(self.conn, int(fid)) == "done"
        ]
        for fid in fantasmas:
            self._forget_open_form(fid, "caso_encerrado")
        return len(velhos) + len(fantasmas)

    async def _refresh_form_state(self, fid: int, *, motivo: str = "", forcar: bool = False) -> str | None:
        """Le no TOA o estado do formulario: pendente / em atendimento / passado.

        Le SO as mensagens do chat dele (`get_chat_messages`) — nao entra, nao
        envia, nao encerra o formulario. Sem resposta do TOA devolve None e nao
        decide nada (nao derruba formulario vivo por falha de leitura).
        """
        agora = time.monotonic()
        if not forcar and (agora - self._form_checked.get(fid, 0.0)) < _FORM_STATE_TTL_SEC:
            return self._form_state.get(fid)
        self._form_checked[fid] = agora
        linha = db.open_assignment(self.conn, fid) or {}
        tecnico = str(linha.get("requester_addr") or "")
        msgs = await self._chat_messages(fid, 20)
        # Sem caso no ledger, a fila vem do payload indexado (o time da fila e o
        # que separa tecnico de analista nas mensagens).
        fila = linha.get("queue_address") or queue_of(self._open_forms.get(int(fid)) or {})
        ops = self._operadores_da_fila(fila)
        estado = form_open.form_state_from_messages(msgs, self.own, tecnico)
        # Identidade viva deste formulario (o payload pode nao trazer o address do
        # tecnico): autores humanos e o analista do time que entrou. Sem chamada
        # extra — as mensagens ja foram lidas acima.
        autores = form_open.form_authors(msgs, self.own, ops)
        if autores:
            self._form_authors[fid] = autores
        dono_msg = form_open.form_dono_from_messages(msgs, self.own, ops)
        if dono_msg:
            self._form_owner_msg[fid] = dono_msg
        ultima = form_open.last_message_time(msgs)
        if ultima:
            self._form_last_msg[fid] = max(float(ultima), float(self._form_last_msg.get(fid) or 0.0))
        aberto = _sql_epoch(linha.get("assigned_at"))
        if aberto:
            self._form_case_at[fid] = max(float(aberto), float(self._form_case_at.get(fid) or 0.0))
        if estado is None:
            return None
        self._form_state[fid] = estado
        self._emit(
            "form_state", fid, linha.get("queue_address"),
            f"estado={estado}; msgs={len(msgs)}; motivo={motivo or 'checagem'}",
        )
        if estado == form_open.FORM_PAST:
            self._close_form(fid, "passado (analista saiu/atendimento concluido)")
        return estado

    def _close_form(self, fid: int, motivo: str) -> None:
        """Formulario passado/encerrado: sai do indice, fecha o caso, libera a fila."""
        no_indice = self._open_forms.pop(int(fid), None) is not None
        self._form_seen.pop(int(fid), None)
        self._form_state.pop(int(fid), None)
        self._form_checked.pop(int(fid), None)
        self._form_authors.pop(int(fid), None)
        self._form_owner_msg.pop(int(fid), None)
        self._form_last_msg.pop(int(fid), None)
        self._form_started.pop(int(fid), None)
        self._form_case_at.pop(int(fid), None)
        # Motivo no ledger: a ausencia nas listas do TOA e o sinal mais forte (o
        # formulario resolvido desaparece da lista) — vale registrar como tal.
        self._close_case(int(fid), "form_resolvido_toa" if str(motivo).startswith("form_resolvido_toa") else "form_done")
        # Formulario encerrado nao tem mais o que entregar: sem isso uma rota
        # `waiting` seguia tentando `move_chat` num chat que o TOA ja recusa —
        # era o retry infinito dos formularios antigos (13..32 tentativas).
        paradas = db.finish_routes_for_chat(
            self.conn, int(fid), "cancelled", f"formulario encerrado: {motivo}"
        )
        if paradas:
            self._emit(
                "form_route_cancelled", int(fid), None,
                f"rotas encerradas={paradas}; motivo={motivo}",
            )
        if no_indice or motivo:
            self._emit("form_closed", int(fid), None, f"motivo={motivo}")

    async def _validate_open_forms(self, motivo: str = "boot") -> int:
        """Confere no TOA cada formulario reidratado: passado nao fica no indice."""
        fechados = 0
        for fid in list(self._open_forms):
            estado = await self._refresh_form_state(fid, motivo=motivo, forcar=True)
            if estado == form_open.FORM_PAST:
                fechados += 1
        if fechados:
            log.info("formularios passados descartados no boot: %s", fechados)
        return fechados

    async def _sweep_form_states(self) -> None:
        """Mantem a visao viva: revalida UM formulario por tick (o mais velho)."""
        self._prune_open_forms()
        agora = time.monotonic()
        for fid in list(self._open_forms):
            if (agora - self._form_checked.get(fid, 0.0)) < _FORM_STATE_TTL_SEC:
                continue
            await self._refresh_form_state(fid, motivo="varredura")
            # Depois de ler, uma olhada na vida dele: formulario morto (o TOA nao
            # avisa a saida do analista) nao pode ficar ocupando fila e cap.
            self._encerra_form_ocioso(fid)
            return

    def _form_idle_min(self, fid: int) -> float:
        """Minutos desde o sinal de vida MAIS RECENTE do formulario.

        Ordem: ultima mensagem -> criacao (`started_time` do closure) -> abertura
        do caso no ledger (`assigned_at`) -> relogio do indice (boot, ultimo
        recurso). O mais recente vence: um formulario de horas atras nao pode
        parecer novo so porque o bot reiniciou.
        """
        fid = int(fid)
        vistos = [
            float(t) for t in (
                self._form_last_msg.get(fid),
                self._form_started.get(fid),
                self._form_case_at.get(fid),
            ) if t
        ]
        if vistos:
            return max(0.0, (time.time() - max(vistos)) / 60.0)
        mono = self._form_seen.get(int(fid))
        if mono:
            return max(0.0, (time.monotonic() - float(mono)) / 60.0)
        return 0.0

    def _cfg_min(self, chave: str, padrao: float) -> float:
        try:
            return max(0.0, float(self.cfg.get(chave) or padrao))
        except (TypeError, ValueError):
            return padrao

    def _form_idle_offer_min(self) -> float:
        """Acima disso o formulario deixa de ser OFERECIDO na abertura."""
        return self._cfg_min("form_idle_offer_min", 30.0)

    def _form_idle_close_min(self) -> float:
        """Acima disso o formulario esquecido sai do indice e o caso fecha."""
        return self._cfg_min("form_idle_close_min", 120.0)

    def _form_idle_owner_close_min(self) -> float:
        """Régua (maior) para fechar formulario PARADO **com analista do time dentro**.

        O TOA nao avisa a saida do analista: o formulario fica "em atendimento"
        para sempre e segurava a vaga dele e a fila. Aqui a ociosidade fecha o
        caso mesmo com alguem dentro (`form_ocioso_analista`).
        """
        return self._cfg_min("form_idle_owner_close_min", 240.0)

    def _form_ocioso(self, fid: int, minutos: float) -> bool:
        """Sem mensagem nova ha `minutos` E sem analista do time dentro.

        Analista do time dentro (`_form_owner_msg`) mantem o formulario vivo, por
        mais tempo que passe: e atendimento em andamento, so silencioso. Vale
        para a OFERTA — o fechamento tem regua propria (ver `_encerra_form_ocioso`).
        """
        if self._form_owner_msg.get(int(fid)):
            return False
        if minutos <= 0:
            return False
        return self._form_idle_min(fid) >= minutos

    def _encerra_form_ocioso(self, fid: int) -> bool:
        """Formulario sem sinal de vida por muito tempo: sai do indice e o caso fecha.

        Duas reguas: **sem** analista do time dentro vale `form_idle_close_min`
        (formulario esquecido na fila); **com** alguem dentro vale
        `form_idle_owner_close_min` — o TOA nao avisa a saida dele, entao um
        formulario "em atendimento" e parado ha horas ainda ocupava a vaga do
        analista e a fila.
        """
        fid = int(fid)
        com_dono = bool(self._form_owner_msg.get(fid))
        limite = self._form_idle_owner_close_min() if com_dono else self._form_idle_close_min()
        if limite <= 0 or self._form_idle_min(fid) < limite:
            return False
        motivo = "form_ocioso_analista" if com_dono else "form_ocioso"
        self._close_form(fid, "%s (%.0f min sem mensagem)" % (motivo, self._form_idle_min(fid)))
        return True


    def _close_case(self, cid: int, reason: str = "") -> bool:
        """Fecha o caso no ledger (com motivo) e devolve a reserva: a vaga volta na hora.

        O motivo e obrigatorio na pratica (A1): fechar sem explicacao foi o que
        deixou casos "sumirem" do ledger sem ninguem saber por que.
        """
        row = self.conn.execute(
            "SELECT assignee, kind, case_key, team_id FROM route_assignments"
            " WHERE chat_id = ? AND closed_at IS NULL",
            (int(cid),),
        ).fetchone()
        db.assign_close(self.conn, int(cid), reason)
        if not row:
            return False
        prefixo = str(row.get("kind") or "") + '|' + str(row.get("assignee") or "") + '|'
        chave = str(row.get("case_key") or "")
        for k in [p for p in list(self._reserve.keys()) if p.startswith(prefixo)]:
            if k[len(prefixo):] in (chave, ""):
                self._reserve.pop(k, None)
        # Vaga liberada: acorda quem aguarda o retry natural (ate 60s por padrao)
        # em vez de deixar o proximo tick esperar o backoff vencer sozinho.
        db.wake_team_routes(self.conn, row.get("team_id"))
        self._emit(
            "case_closed", int(cid), MEM.get(int(cid), {}).get("queue_address"),
            f"{row.get('assignee') or ''}; motivo={reason or 'nao informado'}",
        )
        return True

    def _fecha_chats_ausentes(self, fontes: dict[int, dict], toa_ok: bool, filas: list) -> int:
        """Caso de CHAT resolvido no TOA = ausente das listas por N leituras boas.

        `chats.status` NAO serve de sinal: `closed` e justamente o que o bot grava
        quando entrega o chat e sai — o caso segue com o analista. O que diz que o
        caso acabou e o item sair das listas do TOA (mesma regua do formulario,
        `form_resolvido_toa`). Só vale para chats cuja fila foi lida neste ciclo e
        nunca com leitura falha.
        """
        if not toa_ok:
            return 0
        limite = self._form_absent_readings()
        lidas = {str(q) for q in filas if q}
        fechados = 0
        for row in db.open_chat_assignments(self.conn):
            cid = int(row.get("chat_id"))
            q = str(row.get("queue_address") or "")
            if q and q not in lidas:
                continue  # fila fora do time: sem base para dizer que o item sumiu
            if cid in fontes:
                self._chat_absent.pop(cid, None)
                continue
            self._chat_absent[cid] = int(self._chat_absent.get(cid, 0)) + 1
            if self._chat_absent[cid] >= limite:
                self._emit(
                    "chat_resolvido_toa", cid, q,
                    f"ausente nas listas do TOA em {self._chat_absent[cid]} leituras; "
                    f"caso={row.get('case_key') or '-'}; analista={row.get('assignee') or '-'}",
                )
                self._close_case(cid, "chat_resolvido_toa")
                self._chat_absent.pop(cid, None)
                fechados += 1
        return fechados

    def _queue_chat_snapshot(self, cid: int, row: dict) -> dict:
        mem = MEM.get(cid, {})
        snap = dict(mem.get("raw") or {})
        if mem.get("started_addr") and not snap.get("started_addr"):
            snap["started_addr"] = mem["started_addr"]
        if row.get("queue_address") and not snap.get("helpdesk_address"):
            snap["helpdesk_address"] = row.get("queue_address")
        return snap

    async def consider(self, chat: dict) -> None:
        cid = remember(chat)
        q = queue_of(chat)
        if not self.bot_on:
            if cid is not None:
                self._emit("bot_off", cid, q, "bot desligado")
            return
        if cid is None:
            return
        if closure_chat(chat):
            self._note_open_form(chat, "closure")
            self._emit("closure", cid, q, "formulário de validação")
            await self.route_form(cid, chat, q)
            return
        if not q:
            self._emit("queue_unknown", cid, None, "chat sem fila")
            return
        if q not in self.enabled:
            if q.startswith("h:"):
                db.upsert_queue(self.conn, q, "")
            self._emit("queue_off", cid, q, "fila desabilitada")
            return
        # Fila habilitada mas sem menu ATIVO (ou sem opcoes) / sem time ATIVO: o
        # bot NAO assume. Antes ele assumia, mandava o menu e so descobria na
        # entrega — o chat terminava em "menu vazio" (join_failed) ou encerrado
        # por falta de time (closed_no_agent), sem transferir ninguem.
        if not db.queue_menu_options(self.conn, q):
            self._emit("queue_no_menu", cid, q, "fila habilitada sem menu ativo (ou sem opções)")
            return
        if not db.team_for_queue(self.conn, q):
            self._emit("queue_no_team", cid, q, "fila habilitada sem time ativo")
            return
        if not is_new(chat, self.own):
            self._emit("not_new", cid, q, "já tem analista ou bot")
            return
        with db.tx(self.conn) as cx:
            row = db.lock_chat(cx, cid)
            if row and row["status"] not in ("detected", "skipped"):
                return
            # Ciclo novo do mesmo chat: zera a marca de move (transferencia parcial).
            db.upsert_chat(cx, cid, queue_address=q, status="detected", moved_at=None)
        try:
            await self.join_and_greet(cid, q)
        except Exception as e:
            log.exception("join_and_greet %s", cid)
            self._emit("join_failed", cid, q, str(e))

    async def _join_until_present(self, cid: int) -> bool:
        prev = self._joining
        self._joining = cid
        try:
            try:
                ack0 = await self.sock.command(
                    "get_chat_messages",
                    {"chat_id": cid, "count": 10, "direction": "desc"},
                )
                msgs = _messages_from_ack(ack0)
                if msgs:
                    # Guardado para o replay do resume (é este ACK que o join pedia e descartava).
                    self._recent[int(cid)] = msgs
            except Exception as e:
                log.warning("get_chat_messages pré-join %s: %s", cid, e)
            ack = await self.sock.command("join_chat", {"chat_id": cid})
            blob = ack.get("d") if isinstance(ack.get("d"), dict) else ack
            log.info(
                "join_chat %s timeout=%s e=%s present=%s address=%s",
                cid,
                ack.get("timeout"),
                ack.get("e"),
                blob.get("present") if isinstance(blob, dict) else None,
                blob.get("address") if isinstance(blob, dict) else None,
            )
            self._note_join_response(blob)
            for _ in range(40):
                if MEM.get(cid, {}).get("bot_present"):
                    break
                await asyncio.sleep(0.25)
            if not MEM.get(cid, {}).get("bot_present"):
                try:
                    ack2 = await self.sock.command("get_active_chats", {"contact_status": False})
                    d2 = ack2.get("d") if isinstance(ack2.get("d"), dict) else ack2
                    for c in (d2.get("chats") or d2.get("result") or []):
                        if not isinstance(c, dict):
                            continue
                        remember(c)
                        kid = c.get("chat_id") or c.get("chatId")
                        if kid is not None and int(kid) == cid and _own_present_in(c, self.own):
                            self._mark_present(cid)
                except Exception as e:
                    log.warning("get_active_chats pós-join %s: %s", cid, e)
            return bool(MEM.get(cid, {}).get("bot_present"))
        finally:
            self._joining = prev

    async def join_and_greet(self, cid: int, queue_address: str) -> None:
        if not await self._join_until_present(cid):
            self._emit("join_failed", cid, queue_address, "sem presença no chat")
            return
        db.upsert_chat(self.conn, cid, queue_address=queue_address, status="joined")
        self._emit("assumed", cid, queue_address, "chat assumido")
        # Sem abertura form-aware: o chat de fila sempre recebe o MENU da fila
        # (a verificacao de formulario aberto foi removida — associava errado).
        opts = db.queue_menu_options(self.conn, queue_address)
        if not opts:
            self._emit("join_failed", cid, queue_address, "menu vazio")
            return
        # O estado entra ANTES do envio: o tecnico pode responder durante o ACK do
        # send_message (ate 8 s quando o TOA nao devolve eco) e a resposta se perderia.
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            queue_address=queue_address,
            status="awaiting_reply",
            retries=0,
            menu_node_id=None,
            menu_sent_at=now,
            last_prompt_at=now,
        )
        await self.send_guarded(cid, self._menu_text(opts, root=True), queue_address)
        self._emit("menu_sent", cid, queue_address, "menu enviado")

    async def rejoin_silent(self, cid: int, queue_address: str) -> None:
        if not await self._join_until_present(cid):
            self._emit("join_failed", cid, queue_address, "resume sem presença")
            return
        log.info("rejoin_silent %s fila=%s", cid, queue_address)

    def _operadores_da_fila(self, queue_address: str | None) -> set[str]:
        """Enderecos dos analistas do time da fila (separa tecnico de analista)."""
        if not queue_address:
            return set()
        team, _settings = self._team_settings(queue_address, "form")
        if not team:
            return set()
        return {
            str(m.get("address") or "")
            for m in db.team_members(self.conn, team["id"])
            if m.get("address")
        }

    def _autores_form(self, fid: int) -> list[str]:
        """Chaves `a:<address>` dos autores do formulário — só com UM autor.

        Com dois autores (técnico + analista, ou dois técnicos) o autor não
        identifica o solicitante: aí o formulário fica no caso dele (`chat:<id>`)
        em vez de arriscar juntar gente diferente no mesmo dono.
        """
        autores = [str(a).strip() for a in (self._form_authors.get(int(fid)) or []) if str(a).strip()]
        if len(autores) != 1:
            return []
        return ["a:" + autores[0].lower()]

    @staticmethod
    def _move_falhas(detail: str) -> int:
        """Quantas vezes o `move_chat` do formulario ja foi recusado (vem no detalhe)."""
        for parte in str(detail or "").split("; "):
            if parte.startswith("falhas="):
                try:
                    return int(parte.split("=", 1)[1])
                except (TypeError, ValueError):
                    return 0
        return 0

    def _move_max_fail(self) -> int:
        """Teto de `move_chat` recusado antes de encerrar a rota (nao gira para sempre).

        Vale para formulario (historico `form_move_max_fail`) e para chat; a chave
        generica `move_max_fail` tem precedencia quando configurada.
        """
        try:
            valor = self.cfg.get("move_max_fail")
            if valor in (None, ""):
                valor = self.cfg.get("form_move_max_fail")
            return max(1, int(valor or 3))
        except (TypeError, ValueError):
            return 3

    def _hydrate_open_forms(self) -> int:
        """Reconstroi o indice de formularios abertos a partir do ledger.

        O bot nao e participante do formulario: depois de restart ele nao volta
        sozinho para `_open_forms` (so com um `closure` novo), e sem indice a
        abertura form-aware nao acontece.
        """
        novos = 0
        for row in db.form_assignments_open(self.conn):
            fid = int(row["chat_id"])
            if fid in self._open_forms:
                continue
            self._note_open_form(
                {
                    "chat_id": fid,
                    "mode": "message_scenario",
                    "lifecycle": "pending",
                    "helpdesk_address": row.get("queue_address"),
                },
                "hydrate",
            )
            # Sem o payload original, a referencia de vida e a abertura do caso no
            # ledger (`assigned_at`): e o que impede o formulario de horas atras
            # de parecer recem-chegado depois do restart.
            aberto = _sql_epoch(row.get("assigned_at"))
            if aberto:
                self._form_case_at[fid] = aberto
            novos += 1
        if novos:
            log.info("formularios abertos reidratados do ledger: %s", novos)
        return novos

    async def _chat_messages(self, cid: int, count: int = 10) -> list[dict]:
        """Ultimas mensagens do chat; cai no cache do join se o TOA nao responder."""
        try:
            ack = await self.sock.command(
                "get_chat_messages", {"chat_id": cid, "count": count, "direction": "desc"}
            )
        except Exception as e:
            log.warning("get_chat_messages %s: %s", cid, e)
            ack = {}
        msgs = _messages_from_ack(ack)
        if msgs:
            self._recent[int(cid)] = msgs
            return msgs
        return list(self._recent.get(int(cid)) or [])

    async def _last_inbound(self, cid: int, autor: str | None) -> dict | None:
        """Mensagem mais recente do solicitante (nunca do bot nem de terceiros)."""
        candidatas: list[tuple[float, dict]] = []
        for m in await self._chat_messages(cid):
            dono = m.get("author")
            if not dono or _addr_eq(dono, self.own):
                continue
            if autor:
                if not _addr_eq(dono, autor):
                    continue
            elif form_open.is_system_or_staff({"address": dono}, (MEM.get(cid) or {}).get("raw"), self.own):
                continue
            if not str(m.get("text") or "").strip():
                continue
            quando = _msg_epoch(m.get("time"))
            if quando is None:
                # Sem data confiavel vale a ordem do TOA (mais recente primeiro).
                candidatas.append((float(len(candidatas)) * -1.0, m))
                continue
            candidatas.append((quando, m))
        if not candidatas:
            return None
        return max(candidatas, key=lambda par: par[0])[1]

    async def replay_pending_reply(self, cid: int, row: dict) -> bool:
        """Restart/reciclagem: processa a resposta que chegou com o bot fora do chat.

        Sem isso a resposta perdida virava `closed_timeout` cinco minutos depois.
        Idempotente: a marcacao de `last_reply_at` impede reprocessar a mesma mensagem.
        """
        st = str(row.get("status") or "")
        if st not in _REPLY_STATUSES:
            return False
        autor = (MEM.get(cid) or {}).get("started_addr")
        msg = await self._last_inbound(cid, autor)
        if not msg:
            return False
        quando = _msg_epoch(msg.get("time"))
        if quando is None:
            return False
        visto = (
            _as_utc(row.get("last_reply_at"))
            or _as_utc(row.get("menu_sent_at"))
            or _as_utc(row.get("last_prompt_at"))
        )
        if visto is not None and quando <= visto.timestamp():
            return False
        texto = str(msg.get("text") or "").strip()
        if not texto:
            return False
        log.info("resume replay chat=%s st=%s", cid, st)
        self._emit(
            "reply_replayed", cid, row.get("queue_address"),
            "resposta recebida enquanto o bot estava fora do chat",
        )
        db.mark_reply(self.conn, cid)
        fresh = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone() or row
        await self.handle_reply(cid, fresh, texto)
        return True

    async def resume_open_chats(self) -> None:
        active_by_id: dict[int, dict] = {}
        fetched_ok = False
        try:
            ack = await self.sock.command("get_active_chats", {"contact_status": False})
            if not (isinstance(ack, dict) and ack.get("timeout")):
                d = ack.get("d") if isinstance(ack.get("d"), dict) else ack
                for c in (d.get("chats") or d.get("result") or []):
                    if not isinstance(c, dict):
                        continue
                    remember(c)
                    self._note_open_form(c, "resume")
                    kid = c.get("chat_id") or c.get("chatId")
                    if kid is None:
                        continue
                    cid = int(kid)
                    active_by_id[cid] = c
                    if _own_present_in(c, self.own):
                        self._mark_present(cid)
                fetched_ok = True
        except Exception as e:
            log.warning("resume get_active_chats: %s", e)
        if not fetched_ok:
            log.warning("resume_open_chats: sem lista ativa; adia")
            return
        now = datetime.now(timezone.utc)
        for row in db.resume_chats(self.conn):
            cid = int(row["chat_id"])
            q = row.get("queue_address")
            action = resume_action(row, active_by_id.get(cid), self.own)
            if action == "rehydrate":
                remember(active_by_id[cid])
                self._note_open_form(active_by_id[cid], "rehydrate")
                self._mark_present(cid)
                log.info("resume rehydrate chat=%s", cid)
                await self.replay_pending_reply(cid, row)
                await self._resume_script_run(row)
                await self._resume_ask(row)
            elif action == "rejoin_silent":
                try:
                    await self.rejoin_silent(cid, q or "")
                    fresh = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone() or row
                    await self.replay_pending_reply(cid, fresh)
                    await self._resume_script_run(fresh)
                    await self._resume_ask(fresh)
                except Exception as e:
                    log.exception("rejoin_silent %s", cid)
                    self._emit("join_failed", cid, q, str(e))
            elif action == "stale":
                # A rota tem de sair do caminho AQUI: sem isto ela ficava `waiting`
                # para sempre e o tick tentava `move_chat` num chat que o bot ja nao
                # conduz (TOA recusa com e=10) — girando em loop pela fila.
                db.finish_routes_for_chat(self.conn, cid, "failed", "chat sumiu do TOA")
                db.upsert_chat(self.conn, cid, status="left", left_at=now)
                if not self._case_recent(cid):
                    self._close_case(cid, "stale_boot")
                self._emit("resume_stale", cid, q, "chat sumiu do TOA")
        self._sweep_stale_cases(active_by_id)

    def _case_recent(self, cid: int) -> bool:
        """Caso atribuido ha menos de `case_stale_confirm_sec`?

        O TOA pode nao ter refletido a entrega de agora (a lista ativa e do momento
        do comando): perto disso, nao fechar caso nenhum.
        """
        limite = int(self.cfg.get("case_stale_confirm_sec") or 300)
        if limite <= 0:
            return False
        row = self.conn.execute(
            "SELECT assigned_at FROM route_assignments WHERE chat_id = ? AND closed_at IS NULL",
            (int(cid),),
        ).fetchone()
        quando = _as_utc((row or {}).get("assigned_at"))
        if quando is None:
            return False
        return (datetime.now(timezone.utc) - quando).total_seconds() < limite

    def _sweep_stale_cases(self, active_by_id: dict[int, dict]) -> int:
        """Ledger x TOA no boot: fecha caso aberto cujo item sumiu da sessao.

        Usa a lista ativa ja buscada pelo resume (nao gasta outro comando) e so age
        depois de `case_stale_confirm_sec`, para nao fechar uma entrega recem-feita
        que o TOA ainda nao refletiu.

        Caso de **formulario** fica fora: o bot nao e participante dele, entao ele
        nunca aparece em `get_active_chats` e a varredura fechava formulario vivo
        como `stale_boot` (a abertura form-aware morria a cada restart). O TTL
        (`route_assign_ttl_sec`) continua fechando o caso orfao.
        """
        limite = int(self.cfg.get("case_stale_confirm_sec") or 300)
        agora = datetime.now(timezone.utc)
        fechados = 0
        for row in db.open_assignments(self.conn):
            if str(row.get("kind") or "") == "form":
                continue
            cid = int(row["chat_id"])
            if cid in active_by_id:
                continue
            quando = _as_utc(row.get("assigned_at"))
            if quando is not None and (agora - quando).total_seconds() < limite:
                continue
            if self._close_case(cid, "stale_boot"):
                fechados += 1
        if fechados:
            log.info("varredura de boot: %s caso(s) fechado(s) por ausencia no TOA", fechados)
        return fechados

    def _menu_text(self, opts: list[dict], *, root: bool = True, title: str | None = None) -> str:
        if root:
            lines = [self.tpl.get("greeting") or "", self.tpl.get("menu") or ""]
        else:
            lines = [title or ""]
        for o in opts:
            lines.append(f"{o['sort_order']}) {o['label']}")
        if not root:
            lines.append("0) Voltar")
        return "\n".join(x for x in lines if x)

    def _menu_node_id(self, row: dict) -> int | None:
        node = row.get("menu_node_id")
        if node is None or node == "":
            return None
        return int(node)

    async def _prompt_menu(self, cid: int, row: dict, parent_id: int | None, title: str | None = None) -> None:
        q = row.get("queue_address")
        opts = db.menu_options(self.conn, q, parent_id)
        if parent_id is not None and title is None:
            parent = db.menu_option(self.conn, parent_id)
            title = (parent or {}).get("label") or ""
        # Grava antes de enviar: resposta que chega durante o ACK nao se perde.
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            queue_address=q,
            status="awaiting_reply",
            retries=0,
            menu_node_id=parent_id,
            last_prompt_at=now,
        )
        await self.send_guarded(cid, self._menu_text(opts, root=parent_id is None, title=title), q)

    def _claim_token(self) -> str:
        """Token de posse da entrega: uma execucao por vez, inclusive no processo."""
        return uuid.uuid4().hex

    def _claim_lease(self) -> int:
        return int(self.cfg.get("delivery_claim_ttl_sec") or 300)

    async def _ext_op(
        self,
        op: str,
        payload: dict,
        *,
        kind: str = "",
        chat_id: int | None = None,
        route_id: int | None = None,
        target: str = "",
    ) -> dict:
        """Chamada externa de entrega instrumentada (uma linha por tentativa).

        `result` separa `ok` de `timeout`: hoje o timeout segue como sucesso no
        fluxo, mas fica medido (FASE A) para decidir a FASE B (UNKNOWN).
        """
        started = time.monotonic()
        try:
            ack = await self.sock.command(op, payload)
        except Exception as e:
            db.log_attempt(
                self.conn, op, kind=kind, chat_id=chat_id, route_id=route_id, target=target,
                result="error", latency_ms=int((time.monotonic() - started) * 1000), error=str(e),
            )
            raise
        result = "timeout" if isinstance(ack, dict) and ack.get("timeout") else "ok"
        db.log_attempt(
            self.conn, op, kind=kind, chat_id=chat_id, route_id=route_id, target=target,
            request_id=str((ack or {}).get("r") or ""), result=result,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return ack

    async def send(self, cid: int, text: str) -> None:
        await self.sock.command(
            "send_message",
            {"chat_id": cid, "mark_read": False, "message": {"delivery_id": int(time.time() * 1000), "text": text}},
        )

    async def send_guarded(self, cid: int, text: str, q: str | None = None) -> bool:
        """Envio que nao derruba o fluxo: registra `send_failed` e devolve False."""
        try:
            await self.send(cid, text)
            return True
        except Exception as e:
            log.warning("send_message %s: %s", cid, e)
            self._emit("send_failed", cid, q, str(e)[:200])
            return False

    def _note_ignored_message(self, cid: int, status, q: str | None) -> None:
        """Mensagem do tecnico em status que o bot nao trata: registra.

        Antes era silencioso — foi assim que a resposta "1" da abertura
        form-aware se perdeu sem deixar rastro no log.
        """
        if cid in self._msg_ignored:
            return
        self._msg_ignored.add(cid)
        self._emit("message_ignored", cid, q, f"status={status or '—'}")

    async def on_message(self, m: dict) -> None:
        cid = m.get("chat_id")
        if cid is None:
            return
        cid = int(cid)
        remember({"chat_id": cid, "started_addr": MEM.get(cid, {}).get("started_addr")})
        sysm = m.get("system_message")
        if sysm == "user_joined":
            part = m.get("participant")
            addr = part.get("address") if isinstance(part, dict) else None
            auth = m.get("author")
            if isinstance(auth, dict):
                auth = auth.get("address")
            quem = str(addr or auth or "")
            if _addr_eq(quem, self.own):
                self._mark_present(cid)
                return
            # Terceiro entrou no chat: se for analista do time e o chat estiver na
            # fila, o caso e dele — o bot para de avisar posicao e sai.
            await self._note_join_analista(cid, quem)
            return
        if sysm == "user_left" and m.get("author"):
            await self.on_left(cid, m["author"])
            return
        if sysm:
            return
        if m.get("author") == self.own or _addr_eq(m.get("author"), self.own):
            return
        mem = MEM.get(cid, {})
        origin = mem.get("started_addr")
        autor = m.get("author")
        if origin and not _addr_eq(autor, origin):
            return
        if autor:
            mem["originator_spoke"] = True
        row = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone()
        if not row:
            return
        st = row.get("status")
        if st not in _REPLY_STATUSES:
            self._note_ignored_message(cid, st, row.get("queue_address"))
            return
        if st == "awaiting_reply" and _before_menu(m, row):
            return
        # O solicitante falou: zera o relogio do silencio. Resposta que o bot nao
        # consegue tratar nao pode virar `closed_timeout` em cima do tecnico.
        db.mark_reply(self.conn, cid)
        text = str(m.get("text") or "").strip()
        if not text:
            return
        await self.handle_reply(cid, row, text)

    async def handle_reply(self, cid: int, row: dict, text: str) -> None:
        st = row.get("status")
        if st == "awaiting_script_answer":
            if row.get("ask_option_id"):
                await self.handle_ask_answer(cid, row, text)
                return
            await self.handle_script_answer(cid, row, text)
            return
        if st == "awaiting_script_confirm":
            if row.get("ask_option_id"):
                await self.handle_ask_confirm(cid, row, text)
                return
            await self.handle_script_confirm(cid, row, text)
            return
        q = row.get("queue_address")
        parent_id = self._menu_node_id(row)
        if parent_id is not None and text.strip() == "0":
            await self._prompt_menu(cid, row, None)
            return
        opts = db.menu_options(self.conn, q, parent_id)
        chosen = None
        if text.isdigit():
            n = int(text)
            chosen = next((o for o in opts if int(o["sort_order"]) == n), None)
        if chosen:
            if chosen.get("action") == "submenu":
                kids = db.menu_options(self.conn, q, int(chosen["id"]))
                if not kids:
                    await self._invalid_reply(cid, row)
                    return
                await self._prompt_menu(cid, row, int(chosen["id"]), title=chosen.get("label") or "")
                return
            await self.apply_option(cid, row, chosen)
            return
        await self._invalid_reply(cid, row)

    async def _invalid_reply(self, cid: int, row: dict) -> None:
        max_r = int(self.cfg.get("max_invalid_retries") or 3)
        retries = int(row["retries"] or 0)
        q = row.get("queue_address")
        if retries < max_r:
            # Contador e relogio antes do envio: resposta que chega durante o ACK
            # ja encontra o retry certo.
            now = datetime.now(timezone.utc)
            db.upsert_chat(self.conn, cid, status="awaiting_reply", retries=retries + 1, last_prompt_at=now)
            self._emit("ignored_reply", cid, q, "opção inválida")
            await self.send_guarded(cid, self.tpl.get("retry") or "", q)
            node = self._menu_node_id(row)
            if node is not None:
                parent = db.menu_option(self.conn, node)
                opts = db.menu_options(self.conn, q, node)
                await self.send_guarded(
                    cid,
                    self._menu_text(opts, root=False, title=(parent or {}).get("label") or ""),
                    q,
                )
            return
        await self.close_out(cid, "closed_invalid")

    async def _deny_script(self, cid: int, row: dict, slug: str, result: dict) -> None:
        login = str(result.get("login") or "")
        detail = resource_profile.deny_detail(slug, login, result)
        q = row.get("queue_address")
        await self.send(cid, resource_profile.DENIED_MESSAGE)
        self._emit("script_denied", cid, q, detail)
        await self.leave(cid, "closed")

    async def start_script(self, cid: int, row: dict, opt: dict) -> None:
        slug = str(opt.get("script_slug") or "")
        fields = registry.fields_for(slug, {})
        if not fields:
            await self._invalid_reply(cid, row)
            return
        if slug in ("criar_apoio", "remanejamento_rota"):
            addr = (MEM.get(cid) or {}).get("started_addr") or ""
            login = _member_login_raw(addr)
            page = getattr(self.sock, "page", None)
            result = await resource_profile.lookup(page, login)
            if not result.get("allowed"):
                await self._deny_script(cid, row, slug, result)
                return
        # Nome do autor do chat (capturado na abertura) vai junto do script: o
        # criar_apoio envia como `nome_solicitante`. Fica no estado interno do
        # wizard (chave `_`) para sobreviver ao resume (o reinicio de um script
        # pelo menu "corrigir" continua zerando as respostas).
        inicio: dict = {}
        if slug == "criar_apoio":
            nome = form_open.requester_key(self._queue_chat_snapshot(cid, row), self.own)[2].strip()
            inicio = {"_nome_solicitante": nome}
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            queue_address=row.get("queue_address"),
            status="awaiting_script_answer",
            option_id=opt.get("id"),
            script_slug=slug,
            script_field_index=0,
            script_answers=json.dumps(inicio),
            retries=0,
            last_prompt_at=now,
        )
        prompt = _field_prompt(fields[0])
        if prompt:
            await self.send(cid, prompt)

    async def _restart_script(self, cid: int, row: dict) -> None:
        slug = str(row.get("script_slug") or "")
        fields = registry.fields_for(slug, {})
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            status="awaiting_script_answer",
            script_field_index=0,
            script_answers="{}",
            retries=0,
            last_prompt_at=now,
        )
        if fields:
            prompt = _field_prompt(fields[0])
            if prompt:
                await self.send(cid, prompt)

    async def handle_script_answer(self, cid: int, row: dict, text: str) -> None:
        slug = str(row.get("script_slug") or "")
        answers = _script_answers(row)
        fields = registry.fields_for(slug, answers)
        try:
            idx = int(row.get("script_field_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if not fields or idx < 0 or idx >= len(fields):
            await self._restart_script(cid, row)
            return
        field = fields[idx]
        norm = registry.normalize_answer(slug, field, text, answers)
        now = datetime.now(timezone.utc)
        if norm is None:
            value = _validate_script_value(field, text)
        else:
            value = norm["value"]
            if norm["state"]:
                # Estado interno do script (chaves `_`): vai para o banco mesmo
                # quando a resposta e recusada — e assim a lista de sugestoes de
                # hub chega na proxima pergunta.
                answers.update(norm["state"])
            if norm["message"]:
                await self.send_guarded(cid, norm["message"], row.get("queue_address"))
        if value is None:
            # A pergunta pode ter mudado com o estado da recusa (texto -> lista
            # numerada de sugestoes): reenvia o campo atual, nao o antigo.
            atual = registry.fields_for(slug, answers) or fields
            prompt = _field_prompt(atual[idx]) if 0 <= idx < len(atual) else ""
            if prompt:
                await self.send(cid, prompt)
            payload = json.dumps(answers, ensure_ascii=False)
            if norm is not None and not norm["retry"]:
                # Sugestao apresentada nao consome tentativa.
                db.upsert_chat(
                    self.conn, cid,
                    status="awaiting_script_answer",
                    script_answers=payload,
                    last_prompt_at=now,
                )
                return
            retries = int(row.get("retries") or 0)
            max_r = int(self.cfg.get("max_invalid_retries") or 3)
            if retries < max_r:
                db.upsert_chat(
                    self.conn, cid, status="awaiting_script_answer",
                    script_answers=payload, retries=retries + 1, last_prompt_at=now,
                )
                self._emit("ignored_reply", cid, row.get("queue_address"), "campo inválido")
                return
            await self.close_out(cid, "closed_invalid")
            return
        answers[str(field.get("key") or "")] = value
        nxt = idx + 1
        payload = json.dumps(answers, ensure_ascii=False)
        after = registry.fields_for(slug, answers)
        if nxt >= len(after):
            db.upsert_chat(
                self.conn, cid,
                status="awaiting_script_confirm",
                script_answers=payload,
                retries=0,
                last_prompt_at=now,
            )
            await self._send_blocks(
                cid,
                _confirm_text({"fields": after}, answers, note=registry.confirm_note(slug)),
                q=row.get("queue_address"),
            )
            return
        db.upsert_chat(
            self.conn, cid,
            status="awaiting_script_answer",
            script_field_index=nxt,
            script_answers=payload,
            retries=0,
            last_prompt_at=now,
        )
        next_prompt = _field_prompt(after[nxt])
        if next_prompt:
            await self.send(cid, next_prompt)

    async def handle_script_confirm(self, cid: int, row: dict, text: str) -> None:
        choice = text.strip()
        slug = str(row.get("script_slug") or "")
        if choice == "2":
            await self._restart_script(cid, row)
            return
        if choice == "1":
            await self._execute_script(cid, row, resume=False)
            return
        answers = _script_answers(row)
        fields = registry.fields_for(slug, answers)
        retries = int(row.get("retries") or 0)
        max_r = int(self.cfg.get("max_invalid_retries") or 3)
        if fields and retries < max_r:
            now = datetime.now(timezone.utc)
            db.upsert_chat(self.conn, cid, status="awaiting_script_confirm", retries=retries + 1, last_prompt_at=now)
            self._emit("ignored_reply", cid, row.get("queue_address"), "confirmação inválida")
            await self._send_blocks(
                cid,
                _confirm_text({"fields": fields}, answers, note=registry.confirm_note(slug)),
                q=row.get("queue_address"),
            )
            return
        await self.close_out(cid, "closed_invalid")

    # ------------------------------------------------------------------ #
    # "Pedir info e transferir" com perguntas (menu_option_questions).      #
    # Mesmo wizard dos scripts; a fonte das perguntas e o banco e o final   #
    # e a transferencia, com a rota enfileirada (sobrevive a restart).      #
    # ------------------------------------------------------------------ #
    def _ask_opt(self, row: dict) -> dict:
        return db.menu_option(self.conn, row.get("ask_option_id") or row.get("option_id")) or {}

    def _ask_fields(self, row: dict) -> list[dict]:
        return db.option_questions(self.conn, self._ask_opt(row).get("id"))

    def _ask_summary(self, fields: list[dict], answers: dict) -> str:
        """Resumo das respostas confirmadas.

        O texto `ask_transferring` nao entra aqui: ele e enviado em `_finish_leaf_action`,
        e so quando existe analista elegivel — no trilho da fila quem fala e o aviso de
        posicao (`queue_position`), para nao prometer uma transferencia que vai esperar.
        """
        linhas = [str(self.tpl.get("ask_received") or "Registrei as informações:")]
        for f in fields:
            key = str(f.get("key") or "")
            prompt = str(f.get("prompt") or key).rstrip(" ?")
            linhas.append("%s: %s." % (prompt, answers.get(key, "")))
        return "\n".join(x for x in linhas if x)

    async def _send_ask_transferring(self, cid: int, q: str | None) -> None:
        """Texto de transferencia do wizard, enviado junto do move_chat/invite."""
        extra = str(self.tpl.get("ask_transferring") or "").strip()
        if extra:
            await self.send_guarded(cid, extra, q)

    async def _send_blocks(self, cid: int, texto: str, limite: int = 1500, q: str | None = None) -> None:
        """Envia texto longo em blocos (mensagem muito grande pode truncar no TOA).

        O rodape do texto (ex.: "1) Confirmar / 2) Corrigir") fica no ultimo bloco,
        porque o corte e feito linha a linha, na ordem. Envio tolerante: falha de
        transporte vira evento `send_failed` sem derrubar a coleta."""
        linhas = str(texto or "").split("\n")
        atual: list[str] = []
        tamanho = 0
        for linha in linhas:
            if atual and tamanho + len(linha) + 1 > limite:
                await self.send_guarded(cid, "\n".join(atual), q)
                atual, tamanho = [], 0
            atual.append(linha)
            tamanho += len(linha) + 1
        if atual:
            await self.send_guarded(cid, "\n".join(atual), q)

    async def start_ask(self, cid: int, row: dict, opt: dict, fields: list[dict]) -> None:
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            queue_address=row.get("queue_address"),
            status="awaiting_script_answer",
            option_id=opt.get("id"),
            ask_option_id=opt.get("id"),
            script_slug="",
            script_field_index=0,
            script_answers="{}",
            retries=0,
            last_prompt_at=now,
        )
        intro = str(opt.get("reply_text") or "").strip()
        if intro:
            await self.send(cid, intro)
        prompt = _field_prompt(fields[0])
        if prompt:
            await self.send(cid, prompt)

    async def _restart_ask(self, cid: int, row: dict, fields: list[dict]) -> None:
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            status="awaiting_script_answer",
            script_field_index=0,
            script_answers="{}",
            retries=0,
            last_prompt_at=now,
        )
        if not fields:
            await self._invalid_reply(cid, row)
            return
        prompt = _field_prompt(fields[0])
        if prompt:
            await self.send(cid, prompt)

    async def handle_ask_answer(self, cid: int, row: dict, text: str) -> None:
        fields = self._ask_fields(row)
        try:
            idx = int(row.get("script_field_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if not fields or idx < 0 or idx >= len(fields):
            await self._restart_ask(cid, row, fields)
            return
        field = fields[idx]
        value = _validate_script_value(field, text)
        now = datetime.now(timezone.utc)
        if value is None:
            prompt = _field_prompt(field)
            if prompt:
                await self.send(cid, prompt)
            retries = int(row.get("retries") or 0)
            max_r = int(self.cfg.get("max_invalid_retries") or 3)
            if retries < max_r:
                db.upsert_chat(
                    self.conn, cid, status="awaiting_script_answer",
                    retries=retries + 1, last_prompt_at=now,
                )
                self._emit("ignored_reply", cid, row.get("queue_address"), "pergunta invalida")
                return
            await self.close_out(cid, "closed_invalid")
            return
        answers = _script_answers(row)
        answers[str(field.get("key") or "")] = value
        nxt = idx + 1
        payload = json.dumps(answers, ensure_ascii=False)
        if nxt >= len(fields):
            db.upsert_chat(
                self.conn, cid,
                status="awaiting_script_confirm",
                script_answers=payload,
                retries=0,
                last_prompt_at=now,
            )
            await self._send_blocks(cid, _confirm_text({"fields": fields}, answers))
            return
        db.upsert_chat(
            self.conn, cid,
            status="awaiting_script_answer",
            script_field_index=nxt,
            script_answers=payload,
            retries=0,
            last_prompt_at=now,
        )
        next_prompt = _field_prompt(fields[nxt])
        if next_prompt:
            await self.send(cid, next_prompt)

    async def handle_ask_confirm(self, cid: int, row: dict, text: str) -> None:
        fields = self._ask_fields(row)
        answers = _script_answers(row)
        choice = text.strip()
        if choice == "2":
            await self._restart_ask(cid, row, fields)
            return
        if choice == "1":
            await self._finish_ask(cid, row, fields, answers)
            return
        retries = int(row.get("retries") or 0)
        max_r = int(self.cfg.get("max_invalid_retries") or 3)
        if fields and retries < max_r:
            now = datetime.now(timezone.utc)
            db.upsert_chat(
                self.conn, cid, status="awaiting_script_confirm",
                retries=retries + 1, last_prompt_at=now,
            )
            self._emit("ignored_reply", cid, row.get("queue_address"), "confirmacao invalida")
            await self._send_blocks(cid, _confirm_text({"fields": fields}, answers), q=row.get("queue_address"))
            return
        await self.close_out(cid, "closed_invalid")

    async def _finish_ask(self, cid: int, row: dict, fields: list[dict], answers: dict) -> None:
        """Confirmado: resumo no chat, evento e rota de transferencia duravel."""
        opt = self._ask_opt(row)
        q = row.get("queue_address")
        resumo = self._ask_summary(fields, answers)
        if resumo:
            await self._send_blocks(cid, resumo)
        detalhe = "; ".join(
            "%s=%s" % (str(f.get("key") or ""), answers.get(str(f.get("key") or ""), "")) for f in fields
        )
        self._emit("ask_collected", cid, q, detalhe[:800])
        # Rota aberta (restart/reconfirmacao) e reaproveitada; sem rota nova a entrada
        # na fila acontece aqui. A espera NAO vence sozinha: a rota nasce sem prazo e
        # so sai da fila por entrega, pelo autor sair do chat, pelo chat ser removido,
        # por ausencia nas listas do TOA ou pelo botao "Encerrar espera" do painel.
        rota = db.waiting_route(self.conn, "chat", cid)
        rid = int(rota["id"]) if rota else None
        if rid is None and not db.route_seen(self.conn, "chat", cid):
            team = db.team_for_queue(self.conn, q)
            rid = db.enqueue_route(
                self.conn, "chat", cid, q, int(team["id"]) if team else None,
                detail=detalhe[:800] or "ask_collected", delay_sec=0,
            )
        await self._finish_leaf_action(
            cid, row, opt, "transfer_only", reply="", route_id=rid, from_ask=True
        )

    async def _resume_ask(self, row: dict) -> None:
        """Restart no meio da coleta: reenvia a pergunta (ou a confirmacao) atual."""
        if not row.get("ask_option_id"):
            return
        st = str(row.get("status") or "")
        if st not in ("awaiting_script_answer", "awaiting_script_confirm"):
            return
        cid = int(row["chat_id"])
        fields = self._ask_fields(row)
        if not fields:
            return
        limite = int(self.cfg.get("reply_timeout_sec") or 300)
        quando = _last_activity(row)
        if quando is not None and (datetime.now(timezone.utc) - quando).total_seconds() > limite:
            return
        answers = _script_answers(row)
        if st == "awaiting_script_confirm":
            await self._send_blocks(cid, _confirm_text({"fields": fields}, answers))
            db.upsert_chat(
                self.conn, cid,
                status="awaiting_script_confirm",
                last_prompt_at=datetime.now(timezone.utc),
            )
            return
        try:
            idx = int(row.get("script_field_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if 0 <= idx < len(fields):
            prompt = _field_prompt(fields[idx])
            if prompt:
                await self.send(cid, prompt)
                db.upsert_chat(
                    self.conn, cid,
                    status="awaiting_script_answer",
                    last_prompt_at=datetime.now(timezone.utc),
                )

    def _persist_script_uuid(self, cid: int, uid: str) -> None:
        db.upsert_chat(self.conn, cid, script_uuid=str(uid or ""), status="awaiting_script_run")

    async def _execute_script(self, cid: int, row: dict, *, resume: bool) -> None:
        slug = str(row.get("script_slug") or "")
        opt = db.menu_option(self.conn, row.get("option_id")) or {}
        now = datetime.now(timezone.utc)
        started = row.get("script_started_at") or now.strftime("%Y-%m-%d %H:%M:%S")
        if not resume:
            db.upsert_chat(
                self.conn, cid,
                status="awaiting_script_run",
                script_started_at=started,
                last_prompt_at=now,
            )
            row = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone() or row
        answers = _script_answers(row)
        nome = form_open.requester_key(self._queue_chat_snapshot(cid, row), self.own)[2].strip()
        if not nome:
            nome = str(answers.get("_nome_solicitante") or "").strip()
        ctx = {
            "chat_id": cid,
            "queue_address": row.get("queue_address"),
            "answers": answers,
            "option": opt,
            "persist_job": lambda uid: self._persist_script_uuid(cid, uid),
            "started_at": row.get("script_started_at") or started,
            "nome_solicitante": nome,
        }
        if resume:
            uid = str(row.get("script_uuid") or "").strip()
            if uid:
                ctx["resume_uuid"] = uid
        self._script_inflight.add(cid)
        try:
            result = await registry.run(slug, ctx)
        finally:
            self._script_inflight.discard(cid)
        fresh = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone() or row
        await self.apply_script_result(cid, fresh, result)

    async def _resume_script_run(self, row: dict) -> None:
        if str(row.get("status") or "") != "awaiting_script_run":
            return
        cid = int(row["chat_id"])
        if cid in self._script_inflight:
            return
        uid = str(row.get("script_uuid") or "").strip()
        started = parse_started_at(row.get("script_started_at"))
        age = (datetime.now(timezone.utc) - started).total_seconds()
        if not uid or age >= BUDGET_SEC:
            await self.apply_script_result(cid, row, {
                "ok": False,
                "message": registry.error_message(str(row.get("script_slug") or "")),
                "action": "ask_and_transfer",
                "detail": "resume_timeout" if uid else "resume_no_uuid",
            })
            return
        await self._execute_script(cid, row, resume=True)

    async def apply_script_result(self, cid: int, row: dict, result: dict) -> None:
        action = str(result.get("action") or "stay")
        message = str(result.get("message") or "")
        q = row.get("queue_address")
        reason = "script_ok" if result.get("ok") else "script_error"
        # slug no detail: sem ele so ficava "http 200"/"http_error" e era
        # impossivel saber QUAL script falhou na aba Atividade.
        slug = str(row.get("script_slug") or "").strip()
        det = str(result.get("detail") or "")
        self._emit(reason, cid, q, (f"slug={slug}; {det}" if slug else det)[:800])
        if message:
            await self.send(cid, message)
        if action == "retry_form":
            await self._restart_script(cid, row)
            return
        if action == "stay":
            db.upsert_chat(self.conn, cid, status="monitoring")
            return
        if action in ("reply_and_close", "ask_and_transfer", "transfer_only"):
            opt = db.menu_option(self.conn, row.get("option_id")) or {}
            await self._finish_leaf_action(cid, row, opt, action, reply="")
            return
        db.upsert_chat(self.conn, cid, status="monitoring")

    async def apply_option(self, cid: int, row: dict, opt: dict) -> None:
        if opt.get("action") == "submenu":
            await self._invalid_reply(cid, row)
            return
        if str(opt.get("action") or "") == "ask_and_transfer":
            perguntas = db.option_questions(self.conn, opt.get("id"))
            if perguntas:
                await self.start_ask(cid, row, opt, perguntas)
                return
        if str(opt.get("action") or "") == "run_script":
            await self.start_script(cid, row, opt)
            return
        action = str(opt.get("action") or "")
        reply = str(opt.get("reply_text") or "")
        await self._finish_leaf_action(cid, row, opt, action, reply=reply)

    async def _finish_leaf_action(
        self, cid: int, row: dict, opt: dict, action: str, reply: str = "",
        route_id: int | None = None, from_ask: bool = False
    ) -> None:
        """Folha de acao (resposta e/ou transferencia).

        `from_ask=True` = veio do wizard de perguntas (`_finish_ask`), que ja mandou o
        resumo sem o "Ja estou transferindo": o texto vai aqui, e so quando ha analista
        elegivel (no trilho da fila quem fala e o aviso de posicao).
        """
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            status="classified",
            classification=opt.get("classification") or "",
            option_id=opt.get("id"),
            classified_at=now,
            auto_close=action == "reply_and_close",
        )
        if action in ("reply_and_close", "ask_and_transfer") and reply:
            await self.send_guarded(cid, reply, row.get("queue_address"))
        q = row.get("queue_address")
        detail = opt.get("classification") or ""
        pid = opt.get("parent_id")
        if pid:
            parent = db.menu_option(self.conn, int(pid))
            if parent:
                detail = f"{parent.get('label') or ''} > {opt.get('label') or ''}".strip(" >")
        self._emit("classified", cid, q, detail)
        if action == "reply_and_close":
            await self.leave(cid, "closed")
            return
        if action in ("ask_and_transfer", "transfer_only"):
            await self._deliver_classified_chat(
                cid, row, q, "",
                detail=f"classificado: {detail[:120]}",
                from_ask=from_ask,
                route_id=route_id,
            )
            return

    async def _deliver_classified_chat(
        self,
        cid: int,
        row: dict,
        q: str | None,
        case_key: str,
        *,
        force_assignee: str = "",
        detail: str = "",
        from_ask: bool = False,
        route_id: int | None = None,
        sibling: bool = True,
    ) -> bool:
        """Entrega do chat ja classificado: claim, move, invite e ledger.

        Devolve True quando o chat foi entregue. `force_assignee` prende a entrega
        ao dono do caso (abertura form-aware). Sem analista elegivel o chat NAO e
        encerrado: a rota fica na fila de espera e o tick tenta de novo.
        """
        req = form_open.requester_key(self._queue_chat_snapshot(cid, row), self.own)
        case_key = str(case_key or "").strip() or req[0]
        team = db.team_for_queue(self.conn, q)
        token = self._claim_token()
        claimed = db.claim_delivery(
            self.conn, "chat", cid, q, int(team["id"]) if team else None, token,
            detail=detail, lease_sec=self._claim_lease(),
        )
        if claimed is None:
            self._emit(
                "delivery_duplicate_blocked", cid, q,
                "entrega do chat ja em andamento por outra execucao",
            )
            return False
        rid = int(route_id) if route_id else int(claimed["id"])
        db.set_delivery_marks(self.conn, rid, case_key=case_key, requester_addr=req[1])
        rota = db.delivery_marks(self.conn, rid)
        picked, route = await self.pick_assignee(
            q, case_key=case_key, force_assignee=force_assignee
        )
        entregou = False
        if picked:
            if from_ask:
                await self._send_ask_transferring(cid, q)
            entregou, route = await self._deliver_chat(
                cid, q, picked, route, case_key, req=req, route_id=rid, token=token
            )
        if entregou:
            db.finish_route(self.conn, rid, "done", route)
            if sibling:
                await self._deliver_sibling(case_key, str(picked.get("address") or ""), team)
            return True
        # Transferencia parcial (move_chat ja feito, invite recusado): o chat esta na
        # fila e o tecnico ja viu o "Transferido para ...". Avisar posicao aqui inverteria
        # a ordem das mensagens, entao o aviso fica reservado ao trilho abaixo.
        movido = db.chat_moved(self.conn, cid) or bool(rota.get("move_sent_at"))
        if not movido:
            self._emit("no_agent", cid, q, route or "nenhum analista disponível no time")
        settings_ask = self._team_settings(q, "chat")[1]
        tentativas = int(rota.get("attempts") or 0) + 1
        db.retry_route(self.conn, rid, tentativas, settings_ask["retry_interval_sec"], route or "")
        if self._queue_on() and not movido:
            await self._queue_notify(cid, rid, q)
        return False

    async def _deliver_sibling(self, case_key: str, assignee: str, team: dict | None) -> None:
        """Caso unico: entrega a peca irma (chat <-> formulario) ao mesmo analista.

        Chamado depois de uma entrega bem-sucedida. Se a peca irma ainda nao
        existe, nao faz nada: quando ela chegar, a afinidade (`case_owner`) ja a
        entrega para o dono do caso.
        """
        if not case_key or not assignee:
            return
        irmao_form = db.pending_route_for_case(self.conn, case_key, "form")
        if irmao_form:
            fid = int(irmao_form["chat_id"])
            if db.open_assignment(self.conn, fid):
                return
            token = self._claim_token()
            claimed = db.claim_delivery(
                self.conn, "form", fid, irmao_form.get("queue_address"),
                int(team["id"]) if team else None, token,
                detail="peca irma do caso", lease_sec=self._claim_lease(),
            )
            if claimed is None:
                return
            rid_form = int(claimed["id"])
            pick = {
                "address": assignee,
                "name": assignee,
                "team_id": int(team["id"]) if team else None,
            }
            ok, detail = await self._deliver_form(
                fid, irmao_form.get("queue_address"), pick,
                "peca irma do caso", case_key, route_id=rid_form,
            )
            if ok:
                db.finish_route(self.conn, rid_form, "done", detail)
                self._emit(
                    "case_sibling_delivered", fid, irmao_form.get("queue_address"),
                    f"formulario para {assignee}; caso={case_key}",
                )
            return
        irmao_chat = db.pending_route_for_case(self.conn, case_key, "chat")
        if not irmao_chat:
            return
        cid = int(irmao_chat["chat_id"])
        if db.open_assignment(self.conn, cid):
            return
        q = irmao_chat.get("queue_address")
        token = self._claim_token()
        claimed = db.claim_delivery(
            self.conn, "chat", cid, q, int(team["id"]) if team else None, token,
            detail="peca irma do caso", lease_sec=self._claim_lease(),
        )
        if claimed is None:
            return
        rid = int(claimed["id"])
        req = form_open.requester_key(self._queue_chat_snapshot(cid, {}), self.own)
        db.set_delivery_marks(self.conn, rid, case_key=case_key, requester_addr=req[1])
        pick = {
            "address": assignee,
            "name": assignee,
            "team_id": int(team["id"]) if team else None,
        }
        ok, detail = await self._deliver_chat(
            cid, q, pick, "peca irma do caso", case_key, req=req, route_id=rid, token=token
        )
        if ok:
            db.finish_route(self.conn, rid, "done", detail)
            self._emit("case_sibling_delivered", cid, q, f"chat para {assignee}; caso={case_key}")

    def _alvo_proibido(self, marks: dict, alvos: list) -> str:
        """Algum alvo da entrega e o PROPRIO bot? Devolve o endereco, se for.

        Ultima guarda antes de qualquer `move_chat`/`invite_to_chat`: entregar ao
        bot nao atende ninguem e ainda cria um caso fantasma no ledger. O
        `invite_target` gravado na rota entra na checagem porque ele manda nas
        retentativas (um alvo errado ja gravado seria reusado).
        """
        if not str(self.own or "").strip():
            return ""
        for alvo in list(alvos) + [marks.get("invite_target")]:
            if _addr_eq(str(alvo or ""), self.own):
                return str(alvo or "")
        return ""

    async def _deliver_chat(
        self, cid: int, q: str | None, picked: dict, route: str, case_key: str,
        req: tuple | None = None, route_id: int | None = None, token: str = "",
    ) -> tuple[bool, str]:
        '''move_chat + invite_to_chat + ledger. Usado no fluxo inline e no tick das rotas.

        Guardas da FASE A: o alvo escolhido fica gravado na rota e nao muda entre
        tentativas (invite_target), a marca do move sobrevive a restart
        (move_sent_at, alem do legado `chats.moved_at`) e um caso ja atribuido nao
        e reentregue (open_assignment).
        '''
        del token  # a posse e liberada pelo chamador (db.release_delivery)
        if req is None:
            req = form_open.requester_key(self._queue_chat_snapshot(cid, {}), self.own)
        marks = db.delivery_marks(self.conn, route_id)
        case_key = (
            str(case_key or "").strip()
            or str(marks.get("case_key") or "").strip()
            or (req[0] if req else "")
        )
        proprio_bot = self._alvo_proibido(marks, [picked.get("address")])
        if proprio_bot:
            self._reserve_drop(str(picked.get("address") or ""), "chat", case_key)
            self._emit(
                "self_target_blocked", cid, q,
                f"{proprio_bot}; alvo e o proprio bot; entrega cancelada",
            )
            return False, f"{route}; alvo proibido={proprio_bot}"
        if not q:
            self._reserve_drop(str(picked.get("address") or ""), "chat", case_key)
            return False, f"{route}; move_falhou=sem fila"
        ja_atribuido = db.open_assignment(self.conn, cid)
        if ja_atribuido:
            self._emit(
                "delivery_duplicate_blocked", cid, q,
                f"{ja_atribuido.get('assignee') or ''}; caso ja atribuido e aberto",
            )
            # Caso ja confirmado: se o bot ficou dentro do chat (queda entre o
            # `assign_open` e o `leave`), sai agora — o atendimento e do analista.
            linha_chat = self.conn.execute(
                "SELECT status FROM chats WHERE chat_id = ?", (int(cid),)
            ).fetchone() or {}
            if str(linha_chat.get("status") or "") == "classified":
                await self.leave(cid, "closed")
            return True, f"{route}; ja_atribuido={ja_atribuido.get('assignee') or ''}"
        addr = str(picked.get("address") or "")
        alvo = str(marks.get("invite_target") or "").strip()
        if alvo and not _addr_eq(alvo, addr):
            self._emit("delivery_target_locked", cid, q, f"alvo mantido={alvo}; escolhido={addr}")
            addr = alvo
        label = picked.get("name") or addr
        ja_movido = bool(marks.get("move_sent_at")) or db.chat_moved(self.conn, cid)
        move_state = "ja_movido"
        if not ja_movido:
            try:
                ack = await self._ext_op(
                    "move_chat",
                    {
                        "chat_id": cid,
                        "channel_address": q,
                        "reason": f"Transferido para {label}",
                        "to_operator": True,
                    },
                    kind="chat", chat_id=cid, route_id=route_id, target=addr,
                )
                move_state = "timeout" if isinstance(ack, dict) and ack.get("timeout") else "ok"
            except Exception as e:
                log.warning("move_chat %s %s: %s", cid, q, e)
                self._reserve_drop(addr, "chat", case_key)
                if not str(marks.get("exclude_assignee") or "").strip():
                    # Conta a recusa: o tick encerra a rota no teto, senao o chat
                    # gira para sempre tentando mover um chat que o bot nao conduz.
                    falhas = db.bump_move_fails(self.conn, route_id)
                    return False, f"{route}; move_falhou={e}; falhas={falhas}"
                # Resgate: o chat ja estava com o analista que ficou offline e o
                # TOA recusou um novo move — segue para o convite do novo alvo.
                self._emit(
                    "reclaim_invite_only", cid, q,
                    f"{marks.get('exclude_assignee')}; move recusado={e}",
                )
                db.set_delivery_marks(self.conn, route_id, invite_target=addr)
            db.set_delivery_marks(self.conn, route_id, move_sent_at=True, invite_target=addr)
            db.mark_chat_moved(self.conn, cid)
        elif not alvo:
            db.set_delivery_marks(self.conn, route_id, invite_target=addr)
        try:
            ack2 = await self._ext_op(
                "invite_to_chat",
                {"chat_id": cid, "address": addr},
                kind="chat", chat_id=cid, route_id=route_id, target=addr,
            )
            invite_state = "timeout" if isinstance(ack2, dict) and ack2.get("timeout") else "ok"
        except Exception as e:
            log.warning("invite_to_chat %s %s: %s", cid, addr, e)
            self._reserve_drop(addr, "chat", case_key)
            if not ja_movido:
                # Transferencia parcial: o chat ja esta na fila, entao avisar posicao aqui
                # contradiz o "Transferido para ..." que o tecnico acabou de ver.
                self._emit(
                    "transferred_partial", cid, q,
                    f"{label}; move={move_state}; invite_falhou={e}",
                )
            return False, f"{route}; move={move_state}; invite_falhou={e}"
        tid = picked.get("team_id")
        db.set_delivery_marks(
            self.conn,
            route_id,
            invite_target=addr,
            case_key=case_key,
            requester_addr=(req[1] if len(req) > 1 else ""),
            team_id=int(tid) if tid is not None else None,
        )
        if tid is not None:
            db.set_last_assigned(self.conn, int(tid), "chat", addr)
        db.assign_open(
            self.conn,
            "chat",
            cid,
            q,
            int(tid) if tid is not None else None,
            addr,
            case_key,
            req[1] if len(req) > 1 else "",
            req[2] if len(req) > 2 else "",
            (req[3] if len(req) > 3 else "") or "address",
        )
        detalhe = f"{label}; move={move_state}; invite={invite_state}; {route}"
        self._emit("transferred", cid, q, detalhe)
        await self.leave(cid, "closed")
        return True, detalhe

    async def _apply_chat_fallback(self, cid: int, q: str | None) -> None:
        '''Sem analista: fallback do time (monitor fica no chat; senao o bot sai).'''
        if self._team_settings(q, "chat")[1]["fallback"] == "monitor":
            log.warning("transferência %s sem analista; bot permanece em monitoramento", cid)
            db.upsert_chat(self.conn, cid, status="monitoring")
            return
        log.warning("transferência %s sem analista; bot sai do chat", cid)
        await self.close_out(cid, "closed_no_agent")

    # ---------------- Fila de espera (Fase 2, so chat) -------------------- #
    def _queue_on(self) -> bool:
        valor = str(self.cfg.get("queue_wait_enabled") or "true").strip().lower()
        return valor not in ("false", "0", "no")

    def _form_priority_always(self) -> bool:
        """Formulario sempre primeiro na disputa por vaga de analista (vs. alternar com chat).

        Desligavel por config (sem redeploy) se precisar voltar a alternancia
        50/50 antiga entre as duas linhas.
        """
        valor = str(self.cfg.get("form_priority_always") or "true").strip().lower()
        return valor not in ("false", "0", "no")

    def _fila_desligada(self, q: str | None) -> bool:
        """A fila deste item esta DESABILITADA no painel (o bot nao atua nela)?"""
        return bool(q) and q not in self.enabled

    def _motivo_bloqueio(self, q: str | None) -> tuple[str, str]:
        """Por que o bot NAO pode atuar nesta fila: (chave, texto) — vazio se pode.

        `chave` vai para o ledger (`route_assignments`) e `texto` para o
        evento/log. Duas situacoes: o bot foi DESLIGADO no painel (nao atua em
        nada) ou a fila esta DESABILITADA (nao atua nesta fila).
        """
        if not self.bot_on:
            return "bot_desligado", "bot desligado no painel"
        if self._fila_desligada(q):
            return "fila_desabilitada", "fila desabilitada no painel"
        return "", ""

    async def _sair_do_chat_bloqueado(self, cid: int, q: str | None, chave: str, texto: str) -> None:
        """Sai do chat em silencio e devolve o atendimento ao quadro (com motivo).

        Sem mensagem de despedida: o operador desligou a fila (ou o bot) e o
        cliente/tecnico nao deve ver o bot "falando" justamente o que foi
        desligado no painel.
        """
        detail = f"{texto}; bot saiu do chat"
        self._emit("queue_off", cid, q, detail)
        # Encerra a espera ABERTA com o motivo antes de sair: `leave` grava apenas
        # o status dele na rota (`left`) e o motivo do bloqueio se perderia.
        db.finish_routes_for_chat(self.conn, cid, "failed", detail)
        self._close_case(cid, chave)
        await self.leave(cid, "left")

    async def _abandonar_rota(self, row: dict, kind: str, cid: int, q: str | None) -> None:
        """Bot bloqueado nesta fila: encerra a rota e sai do chat.

        Antes o item era SEGURADO ate a fila religar: ficava `classified` com uma
        rota `failed`, sem evento e sem recuperacao automatica (nem `due_routes`
        nem `classified_without_route` o viam de volta). Agora a rota e encerrada
        com o motivo e o chat volta para o quadro, livre para alguem assumir; o
        formulario apenas para de ser oferecido (nao ha chat para sair).
        """
        chave, texto = self._motivo_bloqueio(q)
        if not chave:
            return
        if kind == "form":
            detail = f"{texto}; formulario segue disponivel para assumir"
            self._emit("form_queue_off", cid, q, detail)
            db.finish_route(self.conn, int(row["id"]), "failed", detail)
            return
        # O chat sai por `_sair_do_chat_bloqueado`, que encerra a espera com o
        # motivo (fazer isso aqui de novo seria so um UPDATE repetido).
        await self._sair_do_chat_bloqueado(cid, q, chave, texto)

    async def _devolver_chats_ao_quadro(self, limite: int = _LEAVE_BUDGET) -> int:
        """Sai dos chats que o bot ainda conduz em fila bloqueada (ou todos, bot off).

        Um chat ja entregue nao tem mais rota em espera, mas o bot segue DENTRO
        dele: sem esta varredura ele ficaria para sempre num quadro que o operador
        tirou do bot. Limitada por tick porque cada saida e um `leave_chat` no TOA.
        """
        saiu = 0
        for row in db.resume_chats(self.conn):
            if saiu >= max(int(limite), 1):
                break
            cid = int(row["chat_id"])
            q = row.get("queue_address")
            chave, texto = self._motivo_bloqueio(q)
            if not chave:
                continue
            await self._sair_do_chat_bloqueado(cid, q, chave, texto)
            saiu += 1
        return saiu

    async def _encerra_atuacao_bot_off(self) -> None:
        """Bot DESLIGADO no painel: encerra as rotas em espera e sai dos chats.

        Mesma regra da fila desabilitada — e por isso que ligar/desligar o bot no
        painel devolve os atendimentos em vez de guarda-los: quem esta esperando
        volta para o quadro e nao fica pendurado esperando o bot religar.
        """
        for row in db.waiting_routes(self.conn, _LEAVE_BUDGET):
            await self._abandonar_rota(
                row, routing.kind_of(row.get("kind")),
                int(row["chat_id"]), row.get("queue_address"),
            )
        await self._devolver_chats_ao_quadro()

    def _queue_top_pos(self) -> int:
        """A partir de qual posicao o chat ouve o NUMERO da fila (`0` = sempre)."""
        try:
            return max(0, int(self.cfg.get("queue_top_pos") or 5))
        except (TypeError, ValueError):
            return 5

    def _queue_show_pos(self) -> bool:
        """Fala a POSICAO ao tecnico? `false` (padrao) = so "avancou na fila"."""
        valor = str(self.cfg.get("queue_show_pos") or "false").strip().lower()
        return valor in ("true", "1", "yes", "sim")

    async def _notify_queue_expired(self, cid: int, q: str | None, settings: dict) -> None:
        """Avisa o tecnico que a espera na fila venceu.

        Com fallback `close` o texto de encerramento ja informa (`closing`), entao
        o aviso da fila sairia em dobro; com `monitor` e o unico aviso que ele tem.
        """
        if str(settings.get("fallback") or "") == "close":
            return
        texto = self._queue_text("queue_expired", 0)
        if texto:
            await self.send_guarded(cid, texto, q)

    def _queue_expired(self, row: dict) -> bool:
        """A rota tem `expires_at` vencido? (so o botao "Encerrar espera" grava isso)

        A fila NAO tem prazo: `_queue_expires_at` nao existe mais e as rotas nascem
        sem vencimento. Este teste existe apenas para o encerramento **manual** do
        painel (que grava o `expires_at` vencido na rota) e para rotas antigas que
        porventura ainda tenham prazo no banco.
        """
        expira = row.get("expires_at")
        if not expira:
            return False
        try:
            ts = datetime.fromisoformat(str(expira))
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= ts

    def _wait_seconds(self, row: dict) -> int:
        criado = row.get("created_at")
        if not criado:
            return 0
        try:
            ts = datetime.fromisoformat(str(criado))
        except ValueError:
            return 0
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - ts).total_seconds()))

    def _queue_text(self, slug: str, pos: int) -> str:
        bruto = str(self.tpl.get(slug) or "").strip()
        if not bruto:
            return ""
        return bruto.replace("{pos}", str(int(pos)))

    async def _queue_notify(self, cid: int, route_id: int, q: str | None) -> None:
        """Avisa a posicao na fila: na entrada e quando ela melhora (nunca repete).

        Formulario de validacao NAO recebe mensagem (silencioso, como sempre foi).
        """
        if route_id < 1:
            return
        # Numero anunciado conta SO chats: o tecnico nao sabe dos formularios que
        # esperam na frente dele (a fila interna segue unica para a entrega).
        pos = db.queue_position(self.conn, route_id, so_chat=True)
        if pos < 1:
            return
        row = self.conn.execute(
            "SELECT kind, last_position FROM pending_routes WHERE id = ?", (int(route_id),)
        ).fetchone() or {}
        # Antes de falar de fila: alguem ja entrou no chat? Entao o caso e dele —
        # o bot para de avisar posicao, registra o dono e sai (sem isso ele
        # seguiria mandando "voce avancou" e poderia entregar o mesmo chat).
        if routing.kind_of(row.get("kind")) != "form":
            presente = self._analista_presente(cid, q)
            if presente:
                await self._adota_chat_na_fila(cid, q, presente, origem="participante")
                return
        anterior = int(row.get("last_position") or 0)
        if pos == anterior:
            return
        if anterior > 0 and pos > anterior:
            return
        db.set_route_position(self.conn, route_id, pos)
        if anterior < 1:
            self._emit("queued", cid, q, "posicao %d na fila" % pos)
        else:
            self._emit("queue_position", cid, q, "posicao %d -> %d" % (anterior, pos))
        if routing.kind_of(row.get("kind")) == "form":
            return
        # Numero so quando o painel manda falar posicao (`queue_show_pos`) E a
        # fila esta no topo (`queue_top_pos`). Fora disso o tecnico ouve que
        # AVANCOU — contar posicao alta soa como fila que nunca anda.
        if self._queue_show_pos() and pos <= self._queue_top_pos():
            texto = self._queue_text("queue_position", pos)
        else:
            texto = self._queue_text("queue_progress", 0)
        if texto:
            # `send_guarded`: falha de transporte no aviso de posicao nao pode
            # derrubar o tick nem o handler do handler que a chamou.
            await self.send_guarded(cid, texto, q)

    def _analista_presente(self, cid: int, q: str | None) -> str:
        """Analista do time da fila ja dentro do chat (entrou por conta propria)?

        Le os `participants` do payload mais recente do chat (`MEM.raw`): o TOA
        reenvia a lista do helpdesk a cada poucos segundos, entao ela e a visao
        viva de quem esta dentro. Ignora o proprio bot e o tecnico que abriu.
        """
        operadores = self._operadores_da_fila(q)
        if not operadores:
            return ""
        chat = self._queue_chat_snapshot(int(cid), {"queue_address": q})
        origin = str((MEM.get(int(cid)) or {}).get("started_addr") or "")
        for addr in _presentes(chat):
            if _addr_eq(addr, self.own) or (origin and _addr_eq(addr, origin)):
                continue
            for op in operadores:
                if _addr_eq(addr, op):
                    return op
        return ""

    async def _adota_chat_na_fila(self, cid: int, q: str | None, addr: str, origem: str = "") -> bool:
        """Analista entrou no chat que estava na fila: o caso e dele e o bot sai.

        Fecha a rota `waiting` (o aviso de posicao para na hora), registra o caso
        no ledger para ele (cap/caso e a afinidade do formulario irmao passam a
        contar o dono) e o bot deixa o chat. Sem isso o bot seguia avisando
        posicao e podia entregar o MESMO chat para outro analista.
        """
        cid = int(cid)
        row = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone() or {}
        if str(row.get("status") or "") != "classified":
            return False
        rota = db.waiting_route(self.conn, "chat", cid)
        if not rota:
            return False
        rid = int(rota["id"])
        case_key = str(rota.get("case_key") or "").strip()
        req = form_open.requester_key(self._queue_chat_snapshot(cid, {"queue_address": q}), self.own)
        req_addr = str(rota.get("requester_addr") or req[1])
        team, _settings = self._team_settings(q, "chat")
        db.set_delivery_marks(self.conn, rid, case_key=case_key, requester_addr=req_addr)
        db.assign_open(
            self.conn, "chat", cid, q, int(team["id"]) if team else None,
            addr, case_key or req[0], req_addr, req[2], "adocao",
        )
        db.finish_route(self.conn, rid, "done", f"adotado: {addr} entrou no chat ({origem or 'entrou'})")
        self._emit("chat_adopted", cid, q, f"{addr}; entrou no chat da fila ({origem or 'entrou'})")
        if team:
            # Caso unico: o formulario do mesmo tecnico vai junto para o mesmo dono.
            await self._deliver_sibling(case_key or req[0], addr, team)
        await self.leave(cid, "closed")
        return True

    async def _note_join_analista(self, cid: int, addr: str) -> None:
        """`user_joined` de terceiro: e analista do time entrando num chat na fila?"""
        if not addr or _addr_eq(addr, self.own):
            return
        row = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (int(cid),)).fetchone() or {}
        if str(row.get("status") or "") != "classified":
            return
        q = row.get("queue_address") or (MEM.get(int(cid)) or {}).get("queue_address")
        for op in self._operadores_da_fila(q):
            if _addr_eq(addr, op):
                await self._adota_chat_na_fila(cid, q, op, origem="user_joined")
                return

    def _team_settings(self, queue_address: str | None, kind: str = "chat") -> tuple[dict | None, dict]:
        """(time, ajustes resolvidos) da fila — herança team_routing → bot_settings."""
        kind = routing.kind_of(kind)
        team = db.team_for_queue(self.conn, queue_address)
        if not team:
            return None, routing.resolve_settings(self.cfg, None, kind)
        return team, routing.resolve_settings(self.cfg, db.team_routing(self.conn, int(team["id"])), kind)

    def _reserve_add(self, addr: str, kind: str, case_key: str) -> None:
        """Reserva: o que acabamos de entregar conta como carga, mesmo antes do TOA."""
        if not addr:
            return
        ttl = float(self.cfg.get("route_reserve_ttl_sec") or 300)
        self._reserve[kind + '|' + addr + '|' + case_key] = [time.monotonic() + ttl, kind, addr, case_key]


    def _reserve_drop(self, addr: str, kind: str, case_key: str) -> None:
        """Entrega falhou: devolve a reserva para o analista nao ficar bloqueado."""
        self._reserve.pop(kind + '|' + addr + '|' + case_key, None)


    def _reserved(self) -> list:
        agora = time.monotonic()
        vivos = []
        for chave, item in list(self._reserve.items()):
            if item[0] <= agora:
                self._reserve.pop(chave, None)
                continue
            vivos.append(item)
        return vivos


    async def _chats_toa(self, queue_address: str | None, fonte: str) -> tuple[list, bool]:
        """Chats com participantes: sessao (active) e/ou fila (helpdesk in_process)."""
        vistos: dict[int, dict] = {}
        ok = False
        if fonte in ("active", "both"):
            try:
                ack = await self.sock.command("get_active_chats", {"contact_status": False})
                d = ack.get("d") if isinstance(ack.get("d"), dict) else ack
                ok = not (isinstance(ack, dict) and ack.get("timeout"))
                for c in d.get("chats") or d.get("result") or []:
                    if not isinstance(c, dict):
                        continue
                    cid = c.get("chat_id") or c.get("chatId")
                    if cid is None:
                        continue
                    vistos[int(cid)] = c
                    ok = True
            except Exception as e:
                log.warning("get_active_chats carga: %s", e)
        if queue_address and fonte in ("helpdesk", "both"):
            try:
                ack2 = await self.sock.command(
                    "get_helpdesk_chats",
                    {"helpdesk_address": queue_address, "chat_type": "in_process", "count": 200, "from": 0},
                )
                d2 = ack2.get("d") if isinstance(ack2.get("d"), dict) else ack2
                ok = ok or not (isinstance(ack2, dict) and ack2.get("timeout"))
                for c in d2.get("chats") or d2.get("result") or []:
                    if not isinstance(c, dict):
                        continue
                    cid = c.get("chat_id") or c.get("chatId")
                    if cid is None:
                        continue
                    vistos[int(cid)] = c
                    ok = True
            except Exception as e:
                log.warning("get_helpdesk_chats carga: %s", e)
        return list(vistos.values()), ok

    async def _capacidade(
        self, members: list[dict], kind: str, queue_address: str | None
    ) -> tuple[dict[str, int], dict[str, int], dict]:
        """Carga do analista = o FISICO do TOA (por autor) + reservas em voo.

        Regra do roteamento (a mesma da aba Carga): conta o que o operador tem
        aberto no front — `TOA chat + TOA form` — deduplicado por **autor** (form +
        chat do mesmo solicitante = 1 caso), somando so as **reservas** (entrega ja
        feita e ainda nao refletida no TOA).

        O ledger do bot e a fila do item servem de **fallback** por analista: valem
        quando a leitura daquele analista nao esta disponivel (velha ou com
        `toa_ok=0`), para nunca decidir as cegas. Sem isso, a mesma peca podia
        entrar com duas chaves (`a:u:...` no ledger e `chat:<id>` na leitura) e o
        analista ficava "cheio" um item antes do real.
        """
        operadores = {str(m["address"]) for m in members}
        casos: dict[str, set] = {str(m["address"]): set() for m in members}
        por_tipo: dict[str, int] = {str(m["address"]): 0 for m in members}
        # 1. Fisico: a leitura completa (todas as filas do time), por analista.
        do_fisico: set[str] = set()
        for m in members:
            addr = str(m["address"])
            lido = db.load_reading(self.conn, addr, fresh_sec=self._load_fresh_sec())
            if lido is None:
                continue
            casos[addr] |= db.load_case_keys(lido)
            por_tipo[addr] = int(lido["chats_toa"] if kind == "chat" else lido["forms_toa"])
            do_fisico.add(addr)
        fonte = str(self.cfg.get("route_capacity_source") or "both")
        chats, toa_ok = await self._chats_toa(queue_address, fonte)
        for c in chats:
            form = form_open.is_validation_form(c)
            req = form_open.requester_key(c, self.own, operadores)
            chave = req[0] or ("chat:" + str(c.get("chat_id") or c.get("chatId") or ""))
            for p in _parts(c):
                if not isinstance(p, dict) or not p.get("present"):
                    continue
                pa = p.get("address") or ""
                for m in members:
                    if not _addr_eq(pa, m["address"]):
                        continue
                    addr = str(m["address"])
                    if addr in do_fisico:
                        continue  # a leitura ja conta esta fila tambem
                    casos[addr].add(chave)
                    if (kind == "form") == bool(form):
                        por_tipo[addr] = por_tipo.get(addr, 0) + 1
        for row in db.open_assignments(self.conn):
            addr = str(row.get("assignee") or "")
            if addr not in casos or addr in do_fisico:
                continue  # com o fisico disponivel o ledger nao soma
            chave = str(row.get("case_key") or "") or ("chat:" + str(row.get("chat_id")))
            if chave in casos[addr]:
                continue
            casos[addr].add(chave)
            if str(row.get("kind") or "") == kind:
                por_tipo[addr] = por_tipo.get(addr, 0) + 1
        # 2. Reservas: entrega em andamento que o TOA ainda nao refletiu. Entra
        # sempre (e so nao duplica quando a chave ja esta na conta).
        for item in self._reserved():
            _exp, kres, ares, chave = item
            if ares not in casos:
                continue
            novo = chave not in casos[ares]
            casos[ares].add(chave)
            if novo and kres == kind:
                por_tipo[ares] = por_tipo.get(ares, 0) + 1
        total = {a: len(s) for a, s in casos.items()}
        return total, por_tipo, {"toa_ok": toa_ok, "chats_toa": len(chats)}


    # ------------------------------------------------------------------
    # Carga por analista (medicao) e resolucao por AUSENCIA no TOA
    # ------------------------------------------------------------------
    def _load_poll_sec(self) -> int:
        try:
            return max(5, int(self.cfg.get("load_poll_sec") or 30))
        except (TypeError, ValueError):
            return 30

    def _form_absent_readings(self) -> int:
        """Leituras consecutivas de ausencia para dar o formulario por resolvido."""
        try:
            return max(1, int(self.cfg.get("form_absent_readings") or 2))
        except (TypeError, ValueError):
            return 2

    def _load_keep(self) -> int:
        try:
            return max(10, int(self.cfg.get("analyst_load_keep") or 200))
        except (TypeError, ValueError):
            return 200

    def _load_fresh_sec(self) -> float:
        """Validade da ultima leitura de carga (2 ciclos da aba Carga, minimo 60 s).

        A decisao usa a leitura como carga FISICA do analista (todas as filas do
        time); se ela estiver mais velha que isso, ou se uma das listas falhou
        (`toa_ok=0`), o calculo volta a ser o da fila do item.
        """
        try:
            poll = float(self.cfg.get("load_poll_sec") or 30)
        except (TypeError, ValueError):
            poll = 30.0
        return max(poll * 2.0, 60.0)

    async def _listas_toa(self, queue_address: str | None) -> tuple[dict[int, dict], bool, set[int]]:
        """O que o TOA lista para a fila: `in_process` + `pending` (+ sessao).

        `in_process` e a lista do front (chats E formularios em atendimento) e
        `pending` e a fila. A uniao e a visao autoritativa de "existe no TOA" — e
        o que permite fechar por AUSENCIA o que ja foi resolvido la.

        O segundo valor (`ok`) so e True quando as DUAS listas do front
        responderam: sem essa garantia a ausencia nao pode decidir nada (um
        timeout no `pending` esconderia um formulario que so espera na fila).

        O terceiro valor e o conjunto de ids vistos na lista `pending` (a FILA
        do TOA para aquela fila): e a fonte do card FILA FORMS do dashboard —
        forms presentes na fila, sem depender de `pending_routes` (rotas orfas
        de form inflavam o numero).
        """
        vistos: dict[int, dict] = {}
        pendentes: set[int] = set()
        fonte = str(self.cfg.get("route_capacity_source") or "both")
        if fonte in ("active", "both"):
            try:
                ack = await self.sock.command("get_active_chats", {"contact_status": False})
                d = ack.get("d") if isinstance(ack.get("d"), dict) else ack
                for c in d.get("chats") or d.get("result") or []:
                    if not isinstance(c, dict):
                        continue
                    cid = c.get("chat_id") or c.get("chatId")
                    if cid is not None:
                        vistos[int(cid)] = c
            except Exception as e:
                log.warning("get_active_chats carga: %s", e)
        if not queue_address:
            return vistos, False, pendentes
        ok = True
        for tipo in ("in_process", "pending"):
            try:
                ack = await self.sock.command(
                    "get_helpdesk_chats",
                    {"helpdesk_address": queue_address, "chat_type": tipo, "count": 200, "from": 0},
                )
                d = ack.get("d") if isinstance(ack.get("d"), dict) else ack
                if isinstance(ack, dict) and ack.get("timeout"):
                    ok = False
                for c in d.get("chats") or d.get("result") or []:
                    if not isinstance(c, dict):
                        continue
                    cid = c.get("chat_id") or c.get("chatId")
                    if cid is None:
                        continue
                    vistos[int(cid)] = c
                    if tipo == "pending":
                        pendentes.add(int(cid))
            except Exception as e:
                ok = False
                log.warning("get_helpdesk_chats %s tipo=%s carga: %s", queue_address, tipo, e)
        return vistos, ok, pendentes

    async def snapshot_load(self, motivo: str = "timer") -> int:
        """Grava a carga por analista e fecha o formulario ja resolvido no TOA.

        Le as listas do TOA por fila do time, cruza com ledger/reservas/online e
        grava `analyst_load` (+ historico). Formulario com caso aberto que NAO
        aparece em nenhuma lista do TOA, com leitura bem sucedida, e contado por
        leitura: ao atingir `form_absent_readings` ele e dado como RESOLVIDO
        (`form_resolvido_toa`) — e isso que libera a vaga do analista.
        """
        fontes: dict[int, dict] = {}
        ok_geral = False
        pendentes_vistos: set[int] = set()
        filas: list[str | None] = []
        equipes: list[tuple[int, str]] = [
            (int(t["id"]), str(t["name"] or t["id"]))
            for t in self.conn.execute("SELECT id, name FROM teams WHERE active = 1").fetchall()
        ]
        membros_por_time: dict[int, list[dict]] = {}
        for tid, _nome in equipes:
            membros_por_time[tid] = db.team_members(self.conn, tid)
            for r in self.conn.execute(
                "SELECT queue_address FROM team_queues WHERE team_id = ?", (tid,)
            ).fetchall():
                if r["queue_address"] and r["queue_address"] not in filas:
                    filas.append(r["queue_address"])
        for q in (filas or [None]):
            vistos, ok, pendentes = await self._listas_toa(q)
            fontes.update(vistos)
            ok_geral = ok_geral or ok
            if q:
                # Fila do TOA (card FILA FORMS): o que a lista `pending` mostrou
                # PARA ESTA fila, deduplicado por chat_id — inclusive entre
                # filas do MESMO snapshot (um chat em duas filas nao pode virar
                # dois no total do card). `ok=False` grava a leitura como falha:
                # o painel mostra "sem coleta recente" em vez de numero velho.
                n_forms = n_chats = 0
                for cid in pendentes:
                    if cid in pendentes_vistos:
                        continue
                    pendentes_vistos.add(cid)
                    chat = vistos.get(cid)
                    if form_open.is_validation_form(chat):
                        n_forms += 1
                    else:
                        n_chats += 1
                db.queue_load_upsert(
                    self.conn, q,
                    pending_forms=n_forms, pending_chats=n_chats,
                    toa_ok=1 if ok else 0,
                    detail=f"motivo={motivo}; pendentes={len(pendentes)}",
                )
        logins = sorted({
            _member_login_raw(str(m["address"]))
            for ms in membros_por_time.values() for m in ms if m.get("address")
        })
        operadores = {str(m["address"]) for ms in membros_por_time.values() for m in ms if m.get("address")}
        online: set[str] = set()
        online_ok = False
        if logins:
            try:
                ack = await self.sock.command("get_online_users", {"users": logins}, timeout=12.0)
                parsed = _online_logins_from_ack(ack)
                if parsed is not None:
                    # O TOA devolve como digitado (F119537); normaliza para casar
                    # com member_login(addr) — mesma regra do roteamento.
                    online = {_member_login(x) for x in parsed}
                    online_ok = True
            except Exception as e:
                log.warning("get_online_users carga: %s", e)
        ledger: dict[str, dict] = {}
        for row in db.open_assignments(self.conn):
            addr = str(row.get("assignee") or "")
            if not addr:
                continue
            info = ledger.setdefault(addr, {"chats": 0, "forms": 0, "casos": set(), "forms_ids": []})
            chave = str(row.get("case_key") or "") or ("chat:" + str(row.get("chat_id")))
            info["casos"].add(chave)
            if str(row.get("kind") or "") == "form":
                info["forms"] += 1
                info["forms_ids"].append(int(row.get("chat_id")))
            else:
                info["chats"] += 1
        reservas: dict[str, int] = {}
        for _exp, _kind, addr, _chave in self._reserved():
            reservas[str(addr)] = reservas.get(str(addr), 0) + 1
        total = 0
        for tid, _nome in equipes:
            for m in membros_por_time[tid]:
                addr = str(m.get("address") or "")
                if not addr:
                    continue
                chats_toa = forms_toa = 0
                casos_toa: set[str] = set()
                for c in fontes.values():
                    if not self._participante_presente(c, addr):
                        continue
                    form = form_open.is_validation_form(c)
                    req = form_open.requester_key(c, self.own, operadores)
                    casos_toa.add(req[0] or ("chat:" + str(c.get("chat_id") or c.get("chatId") or "")))
                    if form:
                        forms_toa += 1
                    else:
                        chats_toa += 1
                info = ledger.get(addr, {"chats": 0, "forms": 0, "casos": set(), "forms_ids": []})
                # O numero da tabela e do CAP e o FISICO do TOA, por autor (form +
                # chat do mesmo solicitante = 1 caso). O ledger so entra como piso
                # quando a leitura falhou — senao a tabela zeraria com caso aberto.
                casos = set(casos_toa) if ok_geral else set(info["casos"])
                ocioso = 0.0
                presente = 0
                for fid in info["forms_ids"]:
                    ocioso = max(ocioso, self._form_idle_min(fid))
                    if fid in fontes:
                        presente = 1
                db.load_upsert(
                    self.conn, addr,
                    name=str(m.get("name") or addr), team_id=tid,
                    janela=_window_label(m),
                    online=1 if _member_login(addr) in online else 0,
                    online_ok=1 if online_ok else 0,
                    chats_toa=chats_toa, forms_toa=forms_toa,
                    chats_ledger=int(info["chats"]), forms_ledger=int(info["forms"]),
                    reservas=int(reservas.get(addr, 0)), casos_total=len(casos),
                    form_ocioso_min=round(float(ocioso), 1), form_presente_toa=presente,
                    toa_ok=1 if ok_geral else 0,
                    # Chaves do TOA desta leitura: a decisao usa isso como carga
                    # FISICA do analista (todas as filas do time, nao so a do item).
                    casos_toa_keys=json.dumps(sorted(casos_toa)),
                    detail=(
                        f"motivo={motivo}; filas={len(filas)}; toa_ok={bool(ok_geral)}; "
                        f"vistos={len(fontes)}; online={len(online)}; ledger={len(info['casos'])}"
                    ),
                )
                # Presenca: carimba desde quando o analista esta offline (e limpa
                # quando volta). So vale com leitura BOA do `get_online_users` —
                # `online_ok=0` = caiu a leitura, nao da para afirmar ausencia.
                db.set_presence(
                    self.conn, addr,
                    online=_member_login(addr) in online,
                    online_ok=bool(online_ok),
                )
                db.load_add_history(
                    self.conn, addr,
                    chats_toa=chats_toa, forms_toa=forms_toa,
                    chats_ledger=int(info["chats"]), forms_ledger=int(info["forms"]),
                    casos_total=len(casos), toa_ok=ok_geral,
                )
                total += 1
        db.load_trim_history(self.conn, self._load_keep())
        resolvidos = self._fecha_ausentes(fontes, ok_geral)
        resolvidos += self._fecha_chats_ausentes(fontes, ok_geral, filas)
        self._emit(
            "analyst_load", None, None,
            f"motivo={motivo}; analistas={total}; vistos_no_toa={len(fontes)}; "
            f"toa_ok={bool(ok_geral)}; resolvidos={resolvidos}",
        )
        return total

    def _participante_presente(self, chat: dict, addr: str) -> bool:
        """O analista aparece como participante PRESENTE (ou `assignedUser`) do chat."""
        alvo = str(addr or "")
        if not alvo:
            return False
        atribuido = chat.get("assignedUser") or chat.get("assigned_user") or {}
        if isinstance(atribuido, dict) and atribuido.get("active"):
            if _addr_eq(str(atribuido.get("address") or ""), alvo):
                return True
        for p in _parts(chat):
            if not isinstance(p, dict) or not p.get("present"):
                continue
            if _addr_eq(str(p.get("address") or ""), alvo):
                return True
        return False

    def _fecha_ausentes(self, fontes: dict[int, dict], toa_ok: bool) -> int:
        """Formulario resolvido no TOA = ausente das listas por N leituras.

        Nunca decide com leitura falha (`toa_ok=False`): ausencia so vale quando o
        TOA respondeu. Contador em memoria (reinicia no restart, sem risco).
        """
        if not toa_ok:
            return 0
        limite = self._form_absent_readings()
        fechados = 0
        for row in db.open_assignments(self.conn):
            if str(row.get("kind") or "") != "form":
                continue
            fid = int(row.get("chat_id"))
            if fid in fontes:
                self._form_absent.pop(fid, None)
                continue
            self._form_absent[fid] = int(self._form_absent.get(fid, 0)) + 1
            if self._form_absent[fid] >= limite:
                self._emit(
                    "form_resolvido_toa", fid, row.get("queue_address"),
                    f"ausente nas listas do TOA em {self._form_absent[fid]} leituras; "
                    f"caso={row.get('case_key') or '-'}; analista={row.get('assignee') or '-'}",
                )
                self._close_form(fid, "form_resolvido_toa")
                self._form_absent.pop(fid, None)
                fechados += 1
        # Indice sem caso: formulario que sumiu do TOA tambem sai de cena.
        for fid in list(self._open_forms):
            if fid in fontes:
                self._form_absent.pop(fid, None)
                continue
            if db.open_assignment(self.conn, fid):
                continue  # o caso aberto ja foi tratado acima
            self._form_absent[fid] = int(self._form_absent.get(fid, 0)) + 1
            if self._form_absent[fid] >= limite:
                self._forget_open_form(fid, "ausente_no_toa")
                self._form_absent.pop(fid, None)
        return fechados

    async def pick_assignee(
        self,
        queue_address: str | None,
        *,
        kind: str = "chat",
        retry_online: bool = True,
        case_key: str = "",
        force_assignee: str = "",
        exclude_address: str = "",
    ) -> tuple[dict | None, str]:
        """Escolhe o analista da fila (kind='chat' ou 'form').

        `retry_online=False` faz uma única consulta de online: o retry
        assíncrono do formulário fica em pending_routes, sem travar o handler.
        `force_assignee` (abertura form-aware) prende a entrega ao dono do caso:
        se ele não estiver na janela, devolve None e quem entrega é a fila.
        `exclude_address` é o resgate por analista offline: a rota socorrida não
        pode voltar para quem a perdeu (nem pela afinidade). O endereço do próprio
        bot é sempre excluído — ele nunca é alvo de rota.
        """
        kind = routing.kind_of(kind)
        team, settings = self._team_settings(queue_address, kind)
        if not team:
            log.warning("fila %s sem time ativo", queue_address)
            return None, "fila sem time ativo"
        members = db.team_members(self.conn, team["id"])
        if not members:
            log.warning("time %s sem membros", team.get("name") or team["id"])
            return None, "time sem membros"
        # Ninguem recebe o caso: nem o proprio bot, nem o analista resgatado.
        proibidos = [x for x in (self.own, exclude_address) if str(x or "").strip()]
        if proibidos:
            restantes = [
                m for m in members
                if not any(_addr_eq(m.get("address"), x) for x in proibidos)
            ]
            if not restantes:
                return None, "time sem elegivel: so o proprio bot/excluido"
            members = restantes
        if kind == "form" and not settings["form_enabled"]:
            log.info("time %s desabilitado para formulário", team.get("name") or team["id"])
            return None, "time desabilitado para formulário"
        mrouting = db.team_member_routing(self.conn, int(team["id"]))
        tr = db.team_routing(self.conn, int(team["id"])) or {}
        last_key = "last_assigned_form" if kind == "form" else "last_assigned_chat"
        last = str(tr.get(last_key) or "")
        if not last and kind == "chat":
            last = str(team.get("last_assigned") or "")
        logins = [_member_login_raw(m["address"]) for m in members]
        raw_online: list[str] = []
        attempts = 0
        if settings["require_online"]:
            for attempt in range(1, settings["attempts"] + 1):
                attempts = attempt
                parsed: list[str] | None = None
                try:
                    ack = await self.sock.command(
                        "get_online_users", {"users": logins}, timeout=12.0
                    )
                    parsed = _online_logins_from_ack(ack)
                except Exception as e:
                    log.warning("get_online_users: %s", e)
                if parsed is not None:
                    raw_online = parsed
                if raw_online or not retry_online:
                    break
                if attempt < settings["attempts"]:
                    await asyncio.sleep(settings["retry_interval_sec"])
        online = {_member_login(x) for x in raw_online}
        casos, por_tipo, info = await self._capacidade(members, kind, queue_address)
        if force_assignee and _addr_eq(force_assignee, self.own):
            # O dono gravado e o proprio bot (config antiga): nao entrega — o caso
            # iria para quem so repassa.
            return None, f"dono_do_caso={force_assignee} proprio_bot"
        if force_assignee:
            for m in members:
                if not _addr_eq(m["address"], force_assignee):
                    continue
                if not _in_route_window(m, datetime.now(TZ_BRT)):
                    return None, f"dono_do_caso={force_assignee} fora_da_janela"
                # Afinidade NAO e excecao ao online: dono offline nao recebe.
                if settings.get("require_online", True) and _member_login(m["address"]) not in online:
                    return None, f"dono_do_caso={force_assignee} sem_online"
                self._reserve_add(str(m["address"]), kind, case_key)
                return m, f"dono_do_caso={force_assignee}; caso do formulario"
            return None, f"dono_do_caso={force_assignee} fora_do_time"
        dono = db.case_owner(self.conn, case_key) if (case_key and settings.get("affinity")) else ""
        if dono and _addr_eq(dono, self.own):
            # Afinidade apontando para o bot: nunca (ele nao atende).
            dono = ""
        if dono and exclude_address and _addr_eq(dono, exclude_address):
            # Resgate: a afinidade aponta para quem ficou offline — nao vale.
            dono = ""
        aviso_afinidade = ""
        if dono:
            for m in members:
                if not _addr_eq(m["address"], dono):
                    continue
                if not _in_route_window(m, datetime.now(TZ_BRT)):
                    aviso_afinidade = "afinidade=" + dono + " fora_da_janela"
                elif settings.get("require_online", True) and _member_login(m["address"]) not in online:
                    aviso_afinidade = "afinidade=" + dono + " sem_online"
                else:
                    self._reserve_add(str(m["address"]), kind, case_key)
                    return m, (
                        "afinidade=" + dono + "; caso do solicitante ja com ele; casos="
                        + str(casos.get(str(m["address"]), 0))
                    )
                break
        picked, detail = routing.decide(
            members,
            kind=kind,
            settings=settings,
            mrouting=mrouting,
            loads=casos,
            loads_kind=por_tipo,
            online=online,
            last_assigned=last,
            now=datetime.now(TZ_BRT),
            team_name=str(team.get("name") or team["id"]),
            attempts_used=attempts,
            # Dupla guarda: o bot nao entra na escolha nem se alguem o readicionar
            # ao time depois do filtro acima.
            exclude={x for x in (self.own, exclude_address) if str(x or "").strip()},
        )
        if picked:
            self._reserve_add(str(picked["address"]), kind, case_key)
        toa_s = "indisponivel" if not info.get("toa_ok") else str(info.get("chats_toa"))
        detail = (
            f"{detail}; toa_online={','.join(raw_online) if raw_online else 'vazio'}; carga_toa={toa_s}"
            + (f"; {aviso_afinidade}" if aviso_afinidade else "")
        )
        if not picked:
            log.warning("time %s sem elegível: %s", team.get("name") or team["id"], detail)
        return picked, detail

    async def _deliver_form(
        self, cid: int, queue_address: str, pick: dict, route: str, case_key: str = "",
        route_id: int | None = None,
    ) -> tuple[bool, str]:
        """Entrega o formulario ao analista: move para a fila e convida.

        O formulario NAO entra no chat, NAO recebe mensagem e NAO tem timeout
        conversacional: aqui existem so move/invite/confirmacao. A FASE A grava o
        alvo (imutavel) e a marca do move na propria rota — sem isso uma queda no
        meio repetia o `move_chat` e podia convidar outro analista.
        """
        marks = db.delivery_marks(self.conn, route_id)
        proprio_bot = self._alvo_proibido(marks, [pick.get("address")])
        if proprio_bot:
            self._reserve_drop(str(pick.get("address") or ""), "form", case_key)
            self._emit(
                "self_target_blocked", cid, queue_address,
                f"{proprio_bot}; alvo e o proprio bot; entrega cancelada",
            )
            return False, f"{route}; alvo proibido={proprio_bot}"
        req = form_open.requester_key(self._open_forms.get(int(cid)) or {}, self.own)
        # Leitura viva ANTES de entregar: (a) nunca oferece formulario ja passado
        # (o encerrado saia para a fila) e (b) e a fonte da identidade por AUTOR
        # das mensagens — o payload do `message_scenario` costuma vir sem address.
        if not self._form_state.get(int(cid)) or not self._form_authors.get(int(cid)):
            estado_pre = await self._refresh_form_state(int(cid), motivo="pre-entrega", forcar=True)
            if estado_pre == form_open.FORM_PAST:
                self._emit(
                    "form_skip_past", cid, queue_address,
                    "formulario passado/encerrado; entrega cancelada",
                )
                return True, f"{route}; formulario passado"
        anterior = str(marks.get("case_key") or "").strip()
        case_key, origem = _case_key_formulario(
            req, case_key, anterior, autores=self._autores_form(int(cid))
        )
        if not case_key:
            case_key, origem = "chat:%d" % int(cid), "chat"
        if anterior and anterior != case_key:
            # A chave da rota nao manda mais: registro para auditoria (era assim
            # que a chave do "balde" se propagava e pinava o mesmo analista).
            self._emit(
                "case_key_divergente", cid, queue_address,
                f"rota={anterior}; usado={case_key}; origem={origem}",
            )
        ja_atribuido = db.open_assignment(self.conn, cid)
        if ja_atribuido:
            self._emit(
                "delivery_duplicate_blocked", cid, queue_address,
                f"{ja_atribuido.get('assignee') or ''}; caso ja atribuido e aberto",
            )
            return True, f"{route}; ja_atribuido={ja_atribuido.get('assignee') or ''}"
        addr = str(pick.get("address") or "")
        alvo = str(marks.get("invite_target") or "").strip()
        if alvo and not _addr_eq(alvo, addr):
            self._emit(
                "delivery_target_locked", cid, queue_address,
                f"alvo mantido={alvo}; escolhido={addr}",
            )
            addr = alvo
        label = pick.get("name") or addr
        tid = pick.get("team_id")
        # O formulario JA esta com um analista do time? O TOA recusa um novo
        # `move_chat` nesse caso (`e=10`), e o retry ficava batendo para sempre.
        # As mensagens dizem quem entrou (`user_joined`): adota esse dono,
        # registra o caso e encerra a rota — a carga passa a contar a verdade.
        if not marks.get("move_sent_at"):
            dono_msg = str(self._form_owner_msg.get(int(cid)) or "")
            if dono_msg and _addr_eq(dono_msg, str(marks.get("exclude_assignee") or "")):
                # Resgate: o dono que o TOA mostra e justamente quem ficou
                # offline — adotar aqui devolveria o caso para ele.
                dono_msg = ""
            if not dono_msg and not db.open_assignment(self.conn, int(cid)):
                await self._refresh_form_state(int(cid), motivo="pre-move", forcar=True)
                dono_msg = str(self._form_owner_msg.get(int(cid)) or "")
            if dono_msg and not db.open_assignment(self.conn, int(cid)):
                # Identidade do formulario por AUTOR quando ele existe: a adocao
                # tambem precisa do requisitante para a afinidade e a auditoria.
                autores = [a for a in (self._form_authors.get(int(cid)) or []) if str(a).strip()]
                req_addr = str(req[1] if len(req) > 1 else "") or (autores[0] if autores else "")
                db.assign_open(
                    self.conn, "form", int(cid), queue_address,
                    int(tid) if tid is not None else None, dono_msg, case_key,
                    req_addr, "", "adocao",
                )
                db.set_delivery_marks(
                    self.conn, route_id,
                    case_key=case_key,
                    requester_addr=req_addr,
                    invite_target=dono_msg,
                    team_id=int(tid) if tid is not None else None,
                )
                self._emit(
                    "form_adopted", cid, queue_address,
                    f"form ja com {dono_msg}; move nao repetido; caso={case_key}",
                )
                return True, f"{route}; ja_em_atendimento={dono_msg}"
        move_state = "ja_movido"
        if not marks.get("move_sent_at"):
            try:
                ack = await self._ext_op(
                    "move_chat",
                    {
                        "chat_id": cid,
                        "channel_address": queue_address,
                        "reason": f"Validação para {label}",
                        "to_operator": True,
                    },
                    kind="form", chat_id=cid, route_id=route_id, target=addr,
                )
                move_state = "timeout" if isinstance(ack, dict) and ack.get("timeout") else "ok"
            except Exception as e:
                log.warning("form move_chat %s %s: %s", cid, queue_address, e)
                self._reserve_drop(addr, "form", case_key)
                if str(marks.get("exclude_assignee") or "").strip():
                    # Resgate: o dono offline ainda aparece dentro no TOA (e=10).
                    # Nao consome o teto de falhas — a rota segue esperando ele
                    # sair, em vez de encerrar o caso.
                    return False, f"{route}; move_falhou={e}; dono_offline"
                falhas = db.bump_move_fails(self.conn, route_id)
                return False, f"{route}; move_falhou={e}; falhas={falhas}"
            db.set_delivery_marks(
                self.conn, route_id,
                move_sent_at=True,
                invite_target=addr,
                case_key=case_key,
                requester_addr=(req[1] if len(req) > 1 else ""),
                team_id=int(tid) if tid is not None else None,
            )
        try:
            ack2 = await self._ext_op(
                "invite_to_chat",
                {"chat_id": cid, "address": addr},
                kind="form", chat_id=cid, route_id=route_id, target=addr,
            )
            invite_state = "timeout" if isinstance(ack2, dict) and ack2.get("timeout") else "ok"
        except Exception as e:
            log.warning("form invite_to_chat %s %s: %s", cid, addr, e)
            self._reserve_drop(addr, "form", case_key)
            return False, f"{route}; invite_falhou={e}"
        db.set_delivery_marks(
            self.conn, route_id,
            invite_target=addr,
            case_key=case_key,
            requester_addr=(req[1] if len(req) > 1 else ""),
            team_id=int(tid) if tid is not None else None,
        )
        if tid is not None:
            db.set_last_assigned(self.conn, int(tid), "form", addr)
        db.assign_open(
            self.conn,
            "form",
            cid,
            queue_address,
            int(tid) if tid is not None else None,
            addr,
            case_key,
            req[1],
            req[2],
            origem or req[3] or "address",
        )
        detail = f"{label}; move={move_state}; invite={invite_state}; {route}"
        self._emit("form_routed", cid, queue_address, detail)
        return True, detail

    def _mark_route_done(self, cid: int, queue_address: str | None, team_id, detail: str) -> None:
        """Registra a rota concluída (também serve de trava contra re-roteamento)."""
        rid = db.enqueue_route(
            self.conn, "form", cid, queue_address, int(team_id) if team_id is not None else None,
            detail=detail, delay_sec=0,
        )
        db.finish_route(self.conn, rid, "done", detail)

    async def route_form(self, cid: int, chat: dict, queue_address: str | None) -> None:
        """Formulário de validação: roteia para o time da fila, como o chat livre.

        MESMAS REGRAS DO CHAT: a fila precisa estar HABILITADA no painel e ter
        time ATIVO que aceite formulário (`team_routing.form_enabled`). Com a
        fila desligada o formulário apenas entra no índice (o painel mostra) e
        NÃO é movido — é o que impede um bot ligado só para observar de mexer
        nos quadros reais.
        """
        if (self.cfg.get("form_routing_enabled") or "true") != "true":
            return
        if not queue_address:
            self._emit("form_no_queue", cid, None, "formulário sem fila")
            return
        if queue_address not in self.enabled:
            self._emit("form_queue_off", cid, queue_address, "fila desabilitada")
            return
        if db.route_seen(self.conn, "form", cid):
            log.info("form %s já roteado; ignorando repetição", cid)
            return
        team, settings = self._team_settings(queue_address, "form")
        if not team:
            self._emit("form_no_team", cid, queue_address, "fila sem time ativo")
            return
        if not settings["form_enabled"]:
            self._emit("form_no_team", cid, queue_address, "time sem formulário habilitado")
            return
        req = form_open.requester_key(chat, self.own)
        case_key = req[0]
        picked, route = await self.pick_assignee(
            queue_address, kind="form", retry_online=False, case_key=case_key
        )
        if picked:
            token = self._claim_token()
            claimed = db.claim_delivery(
                self.conn, "form", cid, queue_address, int(team["id"]), token,
                detail="formulario recebido", lease_sec=self._claim_lease(),
            )
            if claimed is None:
                # Alguma execucao (tick/handler) ja esta entregando este formulario.
                log.info("form %s ja esta em entrega por outra execucao", cid)
                return
            rid = int(claimed["id"])
            db.set_delivery_marks(self.conn, rid, case_key=case_key, requester_addr=req[1])
            ok, detail = await self._deliver_form(
                cid, queue_address, picked, route, case_key, route_id=rid
            )
            if ok:
                db.finish_route(self.conn, rid, "done", detail)
                # Caso unico: o chat que esperava este formulario vai junto.
                await self._deliver_sibling(case_key, str(picked.get("address") or ""), team)
                return
            route = detail
            if self._move_falhas(detail) >= self._move_max_fail():
                motivo = f"move_chat recusado pelo TOA ({self._move_falhas(detail)}x); rota encerrada"
                db.finish_route(self.conn, rid, "failed", f"{motivo}; {detail}")
                self._emit("form_move_refused", cid, queue_address, f"{motivo}; {detail}")
                return
            db.retry_route(self.conn, rid, 1, settings["retry_interval_sec"], detail)
            self._emit("form_route_retry", cid, queue_address, f"tentativa 1; {detail}")
            return
        detail = route or "nenhum analista disponível no time"
        if settings["fallback"] == "monitor" and not self._queue_on():
            # Sem retry: fica registrado para ação manual (com a FILA ligada o
            # formulario entra na fila normal e espera a vez, como o chat).
            self._emit("form_no_agent", cid, queue_address, f"{detail}; fallback=monitor")
            return
        db.enqueue_route(
            self.conn, "form", cid, queue_address, int(team["id"]),
            detail=detail, delay_sec=settings["retry_interval_sec"],
        )
        # A fila ligada ENFILEIRA (nao falha): o evento tem que dizer isso — antes
        # saia `form_no_agent` e o painel mostrava "sem analista" para um item que
        # estava esperando a vez (19 eventos em 3 h no banco real).
        if self._queue_on():
            self._emit("queued", cid, queue_address, f"sem analista elegivel; na fila; {detail}")
        else:
            self._emit("form_no_agent", cid, queue_address, detail)

    def _recover_classified(self) -> int:
        """Chats classificados sem nenhuma rota (crash entre classificar e entregar).

        Recria a rota para o tick entregar. Nao mexe em quem ja tem rota — nem nas
        `failed`/`done` — para nao reciclar caso encerrado nem criar laco.
        """
        recuperados = 0
        for row in db.classified_without_route(self.conn):
            cid = int(row["chat_id"])
            q = row.get("queue_address")
            if self._fila_desligada(q):
                # Fila desligada no painel: nao recria rota — o bot nao atua nela.
                # Este chat e DEVOLVIDO ao quadro por `_devolver_chats_ao_quadro`
                # (uma rota nova nasceria e morreria na hora).
                continue
            team = db.team_for_queue(self.conn, q)
            try:
                db.enqueue_route(
                    self.conn, "chat", cid, q, int(team["id"]) if team else None,
                    detail="recuperado: classificou sem entrega", delay_sec=0,
                )
            except Exception:
                log.warning("recuperacao do chat %s falhou", cid, exc_info=True)
                continue
            recuperados += 1
            self._emit("delivery_recovered", cid, q, "classificado sem rota; rota recriada")
        if recuperados:
            log.info("recuperacao: %s chat(s) classificado(s) sem rota", recuperados)
        return recuperados

    async def tick_routes(self) -> None:
        """Drena a fila de roteamento em DUAS linhas: chat e formulario.

        Cada linha e FIFO por conta propria. Por padrao (`form_priority_always`)
        o formulario comeca sempre primeiro: quando os dois disputam a mesma
        vaga de analista no mesmo tick, o formulario ganha. Com a config
        desligada, volta a alternancia 50/50 antiga entre as duas linhas. De
        um jeito ou de outro, as duas linhas sao tentadas na mesma passada —
        um item enviado depois de 10 da outra linha nao espera ela andar
        inteira.
        """
        if not self.bot_on:
            # Bot DESLIGADO no painel: nada e entregue (nem recuperado). Mesma
            # regra da fila desabilitada — o bot sai dos chats e encerra as
            # esperas, em vez de guarda-las: quem esperava volta para o quadro e
            # nao fica pendurado ate o bot religar.
            await self._encerra_atuacao_bot_off()
            return
        self._recover_classified()
        # Fila desabilitada DEPOIS da entrega: o bot nao permanece dentro do chat
        # de um quadro que o operador tirou dele (nao ha rota em espera para o
        # loop acima ver).
        await self._devolver_chats_ao_quadro()
        linhas = db.due_routes(self.conn)
        if self._form_priority_always():
            # Formulario sempre primeiro, por inteiro: todas as rotas de form
            # devidas neste tick sao tentadas antes de qualquer chat — nao so a
            # cabeca. E o que garante "sempre vai pro topo" mesmo quando sobra
            # mais de 1 vaga disputada no mesmo tick (ver teste
            # test_dois_formularios_ganham_as_duas_vagas_antes_do_chat).
            fila: list[dict] = list(linhas.get("form") or []) + list(linhas.get("chat") or [])
        else:
            # Flag desligada: comportamento antigo — alterna quem comeca e
            # intercala item a item (chat, form, chat, form...), so para nao
            # perder o fallback historico sem redeploy.
            if self._turno_fila % 2 == 0:
                primeira = list(linhas.get("chat") or [])
                segunda = list(linhas.get("form") or [])
            else:
                primeira = list(linhas.get("form") or [])
                segunda = list(linhas.get("chat") or [])
            fila = []
            for i in range(max(len(primeira), len(segunda))):
                if i < len(primeira):
                    fila.append(primeira[i])
                if i < len(segunda):
                    fila.append(segunda[i])
        self._turno_fila += 1
        for row in fila:
            rid = int(row["id"])
            atual = db.delivery_marks(self.conn, rid)
            if str(atual.get("status") or "") != "waiting":
                # Ja resolvida nesta passada (peca irma entregue junto ou outra
                # execucao resolveu): nao recriar rota nem entregar de novo.
                continue
            kind = routing.kind_of(row.get("kind"))
            cid = int(row["chat_id"])
            q = row.get("queue_address")
            used = int(row.get("attempts") or 0)
            if self._fila_desligada(q):
                # Fila desabilitada no painel: o bot nao atua nela. Encerra a rota
                # com o motivo e sai do chat em silencio — o atendimento volta
                # para o quadro em vez de ficar esperando a fila religar.
                await self._abandonar_rota(row, kind, cid, q)
                continue
            team, settings = self._team_settings(q, kind)
            if not team:
                detail = "fila sem time ativo"
                db.finish_route(self.conn, rid, "failed", detail)
                # Antes o tick apenas encerrava a rota em silencio: o chat ficava
                # `classified` para sempre, sem evento e sem timeout.
                self._emit("form_no_team" if kind == "form" else "delivery_failed_no_team", cid, q, detail)
                if kind != "form":
                    await self._apply_chat_fallback(cid, q)
                continue
            # Fila ligada vale para os DOIS tipos: o formulario tambem espera a vez
            # (sem avisar posicao, porque o bot nao fala no formulario) e vai sendo
            # liberado conforme a carga dos analistas diminui — mesma dinamica do
            # chat. Tentativas/fallback do formulario ficam para a fila desligada.
            fila = self._queue_on()
            movido = kind != "form" and (db.chat_moved(self.conn, cid) or bool(row.get("move_sent_at")))
            # Com a fila ligada o chat espera sem limite — mas isso vale para "sem analista".
            # Se o `move_chat` ja foi feito e o convite esta sendo retentado, as tentativas
            # contam normalmente (senao o bot arrastaria o chat para sempre).
            if (not fila or movido) and used >= settings["attempts"]:
                detail = (
                    f"convite falhou {used}x com o chat ja na fila; fallback={settings['fallback']}"
                    if movido
                    else f"tentativas esgotadas ({used}); fallback={settings['fallback']}"
                )
                db.finish_route(self.conn, rid, "failed", detail)
                self._emit("form_no_agent" if kind == "form" else "no_agent", cid, q, detail)
                if kind != "form":
                    await self._apply_chat_fallback(cid, q)
                continue
            if fila and self._queue_expired(row):
                # Sem prazo automatico: chegar aqui significa que o painel pediu o
                # encerramento ("Encerrar espera") ou que a rota e antiga.
                detail = "espera encerrada no painel (Encerrar espera)"
                db.finish_route(self.conn, rid, "failed", detail)
                self._emit("queue_expired", cid, q, detail)
                if kind == "form":
                    # Formulario nao tem chat para avisar: a rota para aqui (vai
                    # para "rotas com falha" no painel) em vez de girar para sempre.
                    self._emit(
                        "form_no_agent", cid, q,
                        f"{detail}; fallback={settings['fallback']}",
                    )
                    continue
                await self._notify_queue_expired(cid, q, settings)
                await self._apply_chat_fallback(cid, q)
                continue
            if kind == "form":
                req = form_open.requester_key(self._open_forms.get(cid) or {}, self.own)
                anterior = str(row.get("case_key") or "").strip()
                case_key, origem = _case_key_formulario(
                    req, anterior, autores=self._autores_form(int(cid))
                )
                if not case_key:
                    case_key, origem = "chat:%d" % int(cid), "chat"
            else:
                # O caso do chat vem da propria sessao (nunca do indice de
                # formularios): era aqui que a entrega gravava case_key vazio.
                req = form_open.requester_key(self._queue_chat_snapshot(cid, {}), self.own)
                anterior = ""
                case_key = str(row.get("case_key") or "").strip() or (req[0] if req else "")
                if not case_key:
                    case_key = "chat:%d" % int(cid)
            db.set_delivery_marks(
                self.conn, rid,
                case_key=case_key,
                requester_addr=(req[1] if len(req) > 1 else ""),
            )
            if anterior and anterior != case_key:
                self._emit(
                    "case_key_divergente", cid, q,
                    f"rota={anterior}; usado={case_key}; origem={kind}",
                )
            token = self._claim_token()
            claimed = db.claim_delivery(
                self.conn, kind, cid, q, row.get("team_id"), token,
                detail=row.get("detail") or "", lease_sec=self._claim_lease(),
            )
            if claimed is None:
                log.info("rota %s (%s chat=%s) em entrega por outra execucao; adiando", rid, kind, cid)
                continue
            row = claimed
            q = row.get("queue_address")
            picked, route = await self.pick_assignee(
                q, kind=kind, retry_online=False, case_key=case_key,
                exclude_address=str(row.get("exclude_assignee") or ""),
            )
            if not picked:
                db.retry_route(self.conn, rid, used + 1, settings["retry_interval_sec"], route or "")
                if fila and not movido:
                    await self._queue_notify(cid, rid, q)
                else:
                    # Fila ligada mas o chat ja foi movido: a espera virou retentativa de
                    # convite, entao o evento vai para o log (o tecnico nao recebe posicao).
                    self._emit(
                        "form_route_retry" if kind == "form" else "route_retry",
                        cid, q, f"tentativa {used + 1}; {route}",
                    )
                continue
            if kind != "form":
                ok, detail = await self._deliver_chat(
                    cid, q, picked, route, case_key, req=req, route_id=rid, token=token
                )
                if ok:
                    espera = self._wait_seconds(row)
                    db.finish_route(self.conn, rid, "done", f"{detail}; espera={espera}s")
                    self._emit("queued_assigned", cid, q, f"espera={espera}s; {detail}")
                    await self._deliver_sibling(case_key, str(picked.get("address") or ""), team)
                    continue
                if self._move_falhas(detail) >= self._move_max_fail():
                    # Chat com `move_chat` recusado no teto: encerra (com o fallback do
                    # time) em vez de girar para sempre — mesmo tratamento do formulario.
                    motivo = f"move_chat recusado pelo TOA ({self._move_falhas(detail)}x); rota encerrada"
                    db.finish_route(self.conn, rid, "failed", f"{motivo}; {detail}")
                    self._emit("chat_move_refused", cid, q, f"{motivo}; {detail}")
                    await self._apply_chat_fallback(cid, q)
                    continue
                db.retry_route(self.conn, rid, used + 1, settings["retry_interval_sec"], detail)
                # Releitura da marca: o move pode ter acontecido agora, dentro da entrega.
                if fila and not db.chat_moved(self.conn, cid):
                    await self._queue_notify(cid, rid, q)
                else:
                    self._emit("route_retry", cid, q, f"tentativa {used + 1}; {detail}")
                continue
            ok, detail = await self._deliver_form(cid, q, picked, route, case_key, route_id=rid)
            if ok:
                db.finish_route(self.conn, rid, "done", detail)
                await self._deliver_sibling(case_key, str(picked.get("address") or ""), team)
                continue
            if self._move_falhas(detail) >= self._move_max_fail():
                motivo = f"move_chat recusado pelo TOA ({self._move_falhas(detail)}x); rota encerrada"
                db.finish_route(self.conn, rid, "failed", f"{motivo}; {detail}")
                self._emit("form_move_refused", cid, q, f"{motivo}; {detail}")
                continue
            db.retry_route(self.conn, rid, used + 1, settings["retry_interval_sec"], detail)
            self._emit("form_route_retry", cid, q, f"tentativa {used + 1}; {detail}")

    async def on_left(self, cid: int, author: str) -> None:
        mem = MEM.get(cid, {})
        origin = mem.get("started_addr")
        row = self.conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone()
        if not row:
            return
        if origin and _addr_eq(author, origin) and mem.get("originator_spoke"):
            await self.leave(cid, "closed_originator_left")
            return
        if row["status"] == "monitoring" and not _addr_eq(author, self.own) and not _addr_eq(author, origin):
            await self.leave(cid, "left")

    async def close_out(self, cid: int, status: str) -> None:
        await self.send_guarded(
            cid,
            self.tpl.get("closing") or "Chat está sendo encerrado",
            (MEM.get(cid) or {}).get("queue_address"),
        )
        await self.leave(cid, status)

    async def leave(self, cid: int, status: str) -> None:
        linha = self.conn.execute(
            "SELECT kind, id FROM pending_routes WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (int(cid),),
        ).fetchone() or {}
        try:
            await self._ext_op(
                "leave_chat",
                {"chat_id": cid},
                kind=str(linha.get("kind") or ""),
                chat_id=cid,
                route_id=linha.get("id"),
            )
        except Exception as e:
            log.warning("leave_chat %s: %s", cid, e)
        now = datetime.now(timezone.utc)
        db.upsert_chat(self.conn, cid, status=status, auto_close=True, left_at=now)
        if status != "closed":
            # Entrega normal (closed) fecha a rota como `done` no chamador; qualquer
            # outro encerramento mata a espera para o tick nao insistir num chat morto.
            db.finish_routes_for_chat(self.conn, cid, "failed", status)
        q = MEM.get(cid, {}).get("queue_address")
        if status == "closed_timeout":
            reason = "closed_timeout"
        elif status == "closed_invalid":
            reason = "closed_invalid"
        elif status == "closed_no_agent":
            reason = "closed_no_agent"
        else:
            reason = "left"
        self._emit(reason, cid, q, status)

    async def _reclaim_offline(self, limite: int = 5) -> int:
        """Devolve para a fila o CHAT preso com analista OFFLINE.

        O caso sai do ledger (`analista_offline` — solta cap, reserva e
        afinidade) e ganha rota nova com o antigo dono **excluido**; o tecnico e
        avisado (`requeue_notice`) e o bot volta a falar no chat.

        **Só chat.** O formulário de validação é um link pessoal do analista:
        re-roteá-lo move o caso para outra pessoa e deixa o quadro dos dois
        mexido (era o efeito de 28/09). O formulário fica com o dono até o TTL de
        caso órfão fechar o registro.

        Roda sozinho no `tick_timeouts` (a cada tick), no maximo `limite` casos
        por tick: nao ha acao no painel (o painel so configura a regua).
        """
        global_settings = self._team_settings(None, "chat")[1]
        if not bool(global_settings.get("offline_requeue_enabled", True)):
            return 0
        sec = int(global_settings.get("offline_requeue_sec") or 0)
        if sec <= 0:
            return 0
        candidatos = db.offline_open_assignments(self.conn, sec)
        devolvidos = 0
        for row in candidatos:
            if devolvidos >= max(int(limite), 1):
                break
            cid = int(row["chat_id"])
            kind = routing.kind_of(row.get("kind"))
            addr = str(row.get("assignee") or "")
            q = row.get("queue_address")
            if kind != "chat":
                # Formulario NAO se re-roteia (ver docstring).
                continue
            if self._fila_desligada(q):
                # Fila desligada no painel: nao cria rota nova (o tick a seguraria).
                continue
            team, settings_fila = self._team_settings(q, kind)
            if not bool(settings_fila.get("offline_requeue_enabled", True)):
                continue
            if int(settings_fila.get("offline_requeue_sec") or 0) <= 0:
                # O time desligou o resgate (0 = nao devolve).
                continue
            if not q:
                self._emit("reclaim_no_queue", cid, None, f"{addr}; caso sem fila para devolver")
                continue
            # 1) A rota antiga sai do caminho (indice unico ativo por kind+chat).
            db.finish_routes_for_chat(self.conn, cid, "failed", f"resgatado: {addr} offline")
            # 2) Fecha o caso no ledger: solta cap, reserva e afinidade.
            self._close_case(cid, "analista_offline")
            offline_s = _offline_seconds(row.get("offline_since"))
            db.upsert_chat(
                self.conn, cid, status="classified", moved_at=None, auto_close=False
            )
            await self._join_until_present(cid)
            texto = str(self.tpl.get("requeue_notice") or "").strip()
            if texto:
                await self.send_guarded(cid, texto, q)
            # 3) Rota nova, com o antigo dono excluido.
            rid = db.enqueue_route(
                self.conn, kind, cid, q, int(team["id"]) if team else None,
                detail=f"resgatado: {addr} offline ha {offline_s:.0f}s",
                delay_sec=0, exclude_assignee=addr,
            )
            db.set_delivery_marks(
                self.conn, rid,
                case_key=str(row.get("case_key") or ""),
                requester_addr=str(row.get("requester_addr") or ""),
                exclude_assignee=addr,
            )
            self._emit("case_reclaimed", cid, q, f"{addr}; offline={offline_s:.0f}s; {kind}")
            devolvidos += 1
        if devolvidos:
            log.info("resgate: %s caso(s) devolvido(s) por analista offline", devolvidos)
        return devolvidos

    async def tick_timeouts(self) -> None:
        db.assign_close_stale(self.conn, int(self.cfg.get("route_assign_ttl_sec") or 43200))
        # Visao viva dos formularios (pendente/em atendimento/passado): um por tick.
        try:
            await self._sweep_form_states()
        except Exception:
            log.exception("sweep_form_states")
        # Caso preso com analista offline: volta para a fila (limite por tick).
        try:
            await self._reclaim_offline()
        except Exception:
            log.exception("reclaim_offline")
        # Rota de chat orfa (chat ja saiu do bot): encerra para nao girar `move_chat`.
        try:
            orfas = db.finish_orphan_routes(self.conn)
            if orfas:
                self._emit("orphan_route_closed", None, None, f"{orfas} rota(s) de chat encerrada(s)")
        except Exception:
            log.exception("finish_orphan_routes")
        sec = int(self.cfg.get("reply_timeout_sec") or 300)
        for row in db.awaiting_timeouts(self.conn):
            if not _silence_expired(row, sec):
                continue
            await self.close_out(int(row["chat_id"]), "closed_timeout")
        for row in db.script_run_timeouts(self.conn, int(BUDGET_SEC)):
            cid = int(row["chat_id"])
            if cid in self._script_inflight:
                continue
            await self._resume_script_run(row)

    async def bootstrap(self) -> None:
        MEM.clear()
        self._recent.clear()
        log.info("bootstrap início")
        await self.discover_catalog()
        await self.process_pending_jobs()
        for addr in self.enabled:
            try:
                await self.sock.command(
                    "get_helpdesk_chats",
                    {"chat_type": "pending", "count": 50, "from": 0, "helpdesk_address": addr},
                )
            except Exception as e:
                log.warning("pending %s: %s", addr, e)
        try:
            await self.sock.command("get_active_chats", {"contact_status": False})
        except Exception:
            pass
        try:
            self._hydrate_open_forms()
        except Exception:
            log.exception("hydrate_open_forms")
        try:
            await self._validate_open_forms("boot")
        except Exception:
            log.exception("validate_open_forms")
        try:
            await self.resume_open_chats()
        except Exception:
            log.exception("resume_open_chats")
        log.info("bootstrap fim filas_habilitadas=%s", len(self.enabled))

    async def discover_catalog(self) -> None:
        """Coleta o catálogo do TOA: filas e operadores (append-only)."""
        searches = (
            ("get_helpdesk_details", {}, 8.0),
            (
                "get_contact_list",
                {"count": 500, "from": 0, "load_all_helpdesks": True},
                8.0,
            ),
            ("search_contact", {"count": -1, "from": 0, "substring": "", "where": "ho"}, 30.0),
        )
        for a, payload, timeout in searches:
            try:
                await self.sock.command(a, payload, timeout=timeout)
            except Exception as e:
                log.warning("%s: %s", a, e)

    async def process_pending_jobs(self) -> None:
        """Consome um pedido do painel (bot_jobs) por chamada."""
        job = db.claim_pending_job(self.conn)
        if not job:
            return
        jid = int(job["id"])
        kind = str(job.get("kind") or "")
        try:
            if kind == "catalog_sync":
                await self.discover_catalog()
            elif kind == "load_snapshot":
                # Botao "Atualizar agora" da aba Carga: le as listas do TOA na hora.
                await self.snapshot_load("painel")
            else:
                raise ValueError(f"job kind desconhecido: {kind}")
            db.finish_job(self.conn, jid, "done")
            db.log_event(self.conn, "catalog_synced", detail=f"job={jid}; kind={kind}")
        except Exception as e:
            log.warning("bot_jobs %s falhou: %s", jid, e)
            db.finish_job(self.conn, jid, "error", str(e)[:500])
            db.log_event(self.conn, "sync_error", detail=f"job={jid}; kind={kind}: {e}")
