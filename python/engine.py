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

# Status em que o bot espera resposta do tecnico. Ficava repetido em dois pontos
# e foi exatamente esquecer o status novo em um deles que engoliu o "1" da
# abertura form-aware: agora a lista e uma so.
_REPLY_STATUSES = (
    "awaiting_reply",
    "awaiting_script_answer",
    "awaiting_script_confirm",
    "awaiting_form_choice",
)


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
    MEM[cid] = {
        "started_addr": chat.get("started_addr") or prev.get("started_addr") or raw.get("started_addr"),
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


def _confirm_text(spec: dict, answers: dict) -> str:
    lines = ["Confirme as informações:", ""]
    for f in spec.get("fields") or []:
        key = str(f.get("key") or "")
        prompt = str(f.get("prompt") or key).rstrip(" ?")
        lines.append(f"{prompt}: {answers.get(key, '')}.")
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

    @property
    def own(self) -> str:
        return self.sock.own_address

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

    def _emit(self, reason: str, cid: int | None, q: str | None, detail: str = "") -> None:
        on = bool(q and q in self.enabled)
        db.log_event(self.conn, reason, chat_id=cid, queue_address=q, queue_enabled=on if q else None, detail=detail)

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
            self._unblock_linked_chats(cid)
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
        self._unblock_linked_chats(int(fid))
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
            "SELECT assignee, kind, case_key FROM route_assignments WHERE chat_id = ? AND closed_at IS NULL",
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
        if await self._form_aware_greet(cid, queue_address):
            return
        opts = db.menu_options(self.conn, queue_address, None)
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

    # ------------------------------------------------------------------
    # Abertura form-aware: o tecnico abre o chat e ja tem formulario aberto
    # ------------------------------------------------------------------
    def _match_open_form(
        self, cid: int, queue_address: str | None
    ) -> tuple[dict, str, str, str] | None:
        """(formulario, regra, dono, diagnostico) do formulario aberto do tecnico.

        Antes de casar, poda o indice (TTL) e descarta candidato cujo caso no
        ledger ja foi encerrado de verdade (`form_done`/`chat_removed`).
        """
        self._prune_open_forms()
        if not self._open_forms:
            return None
        chat = self._queue_chat_snapshot(cid, {})
        if queue_address and not (chat.get("helpdesk_address") or chat.get("channel_addr")):
            chat["helpdesk_address"] = queue_address
        settings = self._team_settings(queue_address, "chat")[1]
        vinculados = db.open_linked_forms(self.conn)
        candidatos = form_open.form_candidates(
            chat, list(self._open_forms.values()), linked=vinculados, own=self.own
        )
        vivos: list[dict] = []
        encerrados = 0
        ociosos = 0
        sem_dono: set[int] = set()
        oferta = self._form_idle_offer_min()
        for f in candidatos:
            fid = form_open.form_fid(f)
            if db.form_case_state(self.conn, fid) == "done":
                encerrados += 1
                continue
            # Ocioso (sem mensagem nova e sem analista do time dentro) nao e
            # oferecido: era o formulario morto que enchia a fila e desligava a
            # abertura por ambiguidade.
            if self._form_ocioso(fid, oferta):
                ociosos += 1
                continue
            if not db.open_assignment(self.conn, fid) and not self._form_owner_msg.get(fid):
                sem_dono.add(fid)
            vivos.append(f)
        info = (
            f"abertos={len(self._open_forms)}; na_fila={len(candidatos)};"
            f" vinculados={len(vinculados)}; encerrados={encerrados}; ociosos={ociosos}"
        )
        if not vivos:
            return None, "", "", info
        form, regra = form_open.form_by_technician(
            chat,
            vivos,
            self.own,
            match_name=bool(settings.get("affinity_match_name")),
            linked=vinculados,
            authors=self._form_authors,
            sem_dono=sem_dono,
        )
        if not form:
            # `regra` traz o motivo da desistencia (ex.: `ambiguo:3`).
            return None, "", "", (f"{info}; {regra}" if regra else info)
        fid = int(form.get("chat_id") or form.get("chatId"))
        dono = str((db.open_assignment(self.conn, fid) or {}).get("assignee") or "")
        return form, regra, dono, info

    def _case_identity(self, cid: int, form: dict | None = None) -> tuple:
        """Identidade do CASO: o chat manda (o formulario costuma vir sem address)."""
        req = form_open.requester_key(self._queue_chat_snapshot(cid, {}), self.own)
        if req and req[0]:
            return req
        if form:
            req = form_open.requester_key(form, self.own)
            if req and req[0]:
                return req
        return ('chat:' + str(int(cid)), '', '', '')

    def _link_case(self, cid: int, fid: int, case_key: str, req: tuple, regra: str = "") -> int:
        """Amarra chat e formulario no MESMO caso (ledger, rota e marca no chat).

        O vinculo no CHAT (`linked_form_chat_id`) vale sempre. A reescrita do caso
        do formulario (`relink_case`) exige identidade: confirmada (address/autor/
        nome) ou, no palpite por fila, um formulario cuja identidade propria NAO
        conflite com quem abriu o chat (`_form_identidade_conflita`). Era o
        vinculo cego por recencia que movia o formulario de OUTRO tecnico para
        este caso — o caso "trocava de analista" no painel.
        """
        db.set_linked_form(self.conn, cid, int(fid))
        if str(regra or "") not in _REGRAS_IDENTIDADE:
            # Auditoria do vinculo por palpite de fila (o chamador ja recusou
            # conflito de identidade). Era este caminho que passava despercebido.
            self._emit(
                "case_link_unconfirmed", cid, MEM.get(int(cid), {}).get("queue_address"),
                f"form={int(fid)}; regra={regra or '—'}; unificado por fila",
            )
        mudou = 0
        if db.open_assignment(self.conn, int(fid)):
            mudou = db.relink_case(
                self.conn, int(fid),
                str(case_key or ""),
                str(req[1] if len(req) > 1 else ""),
                str(req[2] if len(req) > 2 else ""),
                str((req[3] if len(req) > 3 else "") or "address"),
            )
        for linha in self.conn.execute(
            """SELECT id, case_key FROM pending_routes
                WHERE kind = 'form' AND chat_id = ?
                  AND status NOT IN ('done', 'failed', 'cancelled')""",
            (int(fid),),
        ).fetchall():
            if str(linha.get("case_key") or "") == str(case_key or ""):
                continue
            db.set_delivery_marks(
                self.conn, int(linha["id"]),
                case_key=str(case_key or ""),
                requester_addr=str(req[1] if len(req) > 1 else ""),
            )
        return mudou

    async def _fill_form_authors(self, cid: int, queue_address: str | None, limite: int = 3) -> bool:
        """Le as mensagens dos candidatos para achar o AUTOR do formulario.

        O payload do `message_scenario` costuma vir sem o address do tecnico; o
        autor das mensagens dele e a identidade que sobra (sem isso o vinculo
        caia em `regra=fila`, palpite por recencia). Le no maximo `limite`
        candidatos (os mais recentes) e devolve True quando algum ganhou autor.
        """
        chat = self._queue_chat_snapshot(cid, {})
        if queue_address and not (chat.get("helpdesk_address") or chat.get("channel_addr")):
            chat["helpdesk_address"] = queue_address
        candidatos = form_open.form_candidates(
            chat, list(self._open_forms.values()),
            linked=db.open_linked_forms(self.conn), own=self.own,
        )
        novo = False
        for f in (candidatos[-int(limite):] if limite else []):
            fid = form_open.form_fid(f)
            if not fid or self._form_authors.get(fid):
                continue
            await self._refresh_form_state(fid, motivo="autores", forcar=True)
            if self._form_authors.get(fid):
                novo = True
        return novo

    def _form_pending_text(self, dono: str, queue_address: str | None = None) -> str:
        if not dono:
            return str(self.tpl.get("form_pending_wait") or "").strip()
        nome = dono
        membro = self._member_of_queue(queue_address, dono)
        if membro:
            nome = str(membro.get("name") or dono).strip() or dono
        return str(self.tpl.get("form_pending") or "").replace("{analista}", nome).strip()

    def _member_of_queue(self, queue_address: str | None, addr: str) -> dict | None:
        team, _ = self._team_settings(queue_address, "chat")
        if not team:
            return None
        for m in db.team_members(self.conn, team["id"]):
            if _addr_eq(m.get("address"), addr):
                return m
        return None

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

    def _form_identidade_conflita(self, fid: int, cid: int) -> str:
        """O formulario do palpite por fila e de OUTRO tecnico? Devolve o motivo.

        A chave do caso do formulario (`a:<address>`) e a identidade dele. Quando
        ela aponta para alguem diferente de quem abriu o chat, o vinculo por
        recencia unificaria casos de pessoas diferentes — era assim que um caso
        "trocava de analista" no painel.
        """
        chave = str((db.open_assignment(self.conn, int(fid)) or {}).get("case_key") or "").strip().lower()
        if not chave:
            for r in self.conn.execute(
                "SELECT case_key FROM pending_routes WHERE kind = 'form' AND chat_id = ?"
                " AND status = 'waiting'",
                (int(fid),),
            ).fetchall():
                chave = str(r.get("case_key") or "").strip().lower()
                if chave:
                    break
        if not chave.startswith("a:"):
            return ""
        alvo = form_open.chat_opener(self._queue_chat_snapshot(int(cid), {}), self.own)
        addr = str(alvo.get("address") or "").strip().lower()
        if not addr or chave[2:] == addr:
            return ""
        return f"form={int(fid)} e de {chave[2:]}; chat de {addr}"

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
        """Teto de `move_chat` recusado antes de encerrar a rota (nao gira para sempre)."""
        try:
            return max(1, int(self.cfg.get("form_move_max_fail") or 3))
        except (TypeError, ValueError):
            return 3

    def _form_owner(self, row: dict) -> str:
        """Analista que atende o formulario vinculado ao chat (`linked_form_chat_id`)."""
        fid = row.get("linked_form_chat_id")
        if not fid:
            return ""
        return str((db.open_assignment(self.conn, int(fid)) or {}).get("assignee") or "")

    async def _form_aware_greet(self, cid: int, queue_address: str | None) -> bool:
        """Oferece 1) falar sobre o formulario / 2) abrir o menu.

        Devolve True quando assume a conversa com as opcoes (o menu normal nao e
        enviado). Sem formulario vinculavel, devolve False e nada muda.
        """
        achado = None
        for _tentativa in range(3):
            candidato = self._match_open_form(cid, queue_address)
            if not candidato:
                self._emit("form_aware_skip", cid, queue_address, "nenhum formulario no indice")
                return False
            if not candidato[0]:
                # Sem identidade no payload: le as mensagens dos candidatos para
                # achar o AUTOR (o tecnico escreve no formulario) e decide de novo.
                # So depois de esgotar isso o vinculo e recusado.
                if _tentativa == 0 and await self._fill_form_authors(cid, queue_address):
                    continue
                # Diagnostico do porque nao casou (antes isso era silencioso).
                self._emit("form_aware_skip", cid, queue_address, candidato[3] or "sem candidato na fila")
                return False
            form, regra, dono, info = candidato
            fid_cand = form_open.form_fid(form)
            if regra == "fila":
                # Palpite por fila: so vale sem conflito de identidade. Com o
                # formulario de OUTRO tecnico, o vinculo unificaria casos de
                # pessoas diferentes (era o caso que "trocava de analista").
                conflito = self._form_identidade_conflita(fid_cand, cid)
                if conflito:
                    self._emit("form_aware_skip", cid, queue_address, conflito)
                    return False
            # Visao viva (como a extensao do TOA): formulario passado nao entra.
            # Leitura FORCADA: a resposta ao tecnico nao pode sair de cache.
            estado = await self._refresh_form_state(fid_cand, motivo="abertura", forcar=True)
            if estado == form_open.FORM_PAST:
                self._emit("form_aware_skip", cid, queue_address, f"form={fid_cand} passado; {info}")
                continue
            # A leitura acima traz o relogio da vida dele: formulario sem mensagem
            # nova (e sem analista do time dentro) nao e oferecido.
            if self._form_ocioso(fid_cand, self._form_idle_offer_min()):
                self._emit(
                    "form_aware_skip", cid, queue_address,
                    f"form={fid_cand} ocioso ({self._form_idle_min(fid_cand):.0f} min); {info}",
                )
                continue
            if regra == "fila" and _tentativa == 0:
                # A leitura acima pode ter revelado o AUTOR das mensagens: com
                # identidade o vinculo deixa de ser palpite (e unifica o caso).
                novo = self._match_open_form(cid, queue_address)
                if novo and novo[0] and str(novo[1]) in _REGRAS_IDENTIDADE:
                    achado = novo
                    break
            achado = candidato
            break
        if not achado:
            return False
        form, regra, dono, _info = achado
        fid = form_open.form_fid(form)
        req = self._case_identity(cid, form)
        mudou = self._link_case(cid, fid, req[0], req, regra=regra)
        self._emit(
            "case_linked", cid, queue_address,
            f"form={fid}; regra={regra}; dono={dono or '—'}; case={req[0] or '—'}; ledger={mudou}",
        )
        txt = self._form_pending_text(dono, queue_address)
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            queue_address=queue_address,
            status="awaiting_form_choice",
            retries=0,
            menu_node_id=None,
            menu_sent_at=now,
            last_prompt_at=now,
            linked_form_chat_id=fid,
        )
        await self.send_guarded(cid, txt, queue_address)
        self._emit(
            "form_aware_greeting", cid, queue_address,
            f"form={fid}; 1=falar sobre o formulario; 2=menu" + (f"; dono={dono}" if dono else ""),
        )
        return True

    def _unblock_linked_chats(self, fid: int) -> int:
        """Formulario encerrado: a rota presa ao dono volta a ser rota normal."""
        ids = [
            int(r["chat_id"])
            for r in self.conn.execute(
                "SELECT chat_id FROM chats WHERE linked_form_chat_id = ?", (int(fid),)
            ).fetchall()
        ]
        livres = 0
        for cid in ids:
            cur = self.conn.execute(
                """UPDATE pending_routes SET expects_owner = 0
                    WHERE chat_id = ? AND kind = 'chat' AND status = 'waiting'""",
                (cid,),
            )
            livres += int(cur.rowcount or 0)
        if livres:
            log.info("form %s encerrado: %s rota(s) de chat liberadas do dono", fid, livres)
        return livres

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
            if _addr_eq(addr or auth, self.own):
                self._mark_present(cid)
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
        if st == "awaiting_form_choice":
            await self.handle_form_choice(cid, row, text)
            return
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

    async def handle_form_choice(self, cid: int, row: dict, text: str) -> None:
        """Abertura form-aware: 1 = falar sobre o formulario, 2 = abrir o menu."""
        escolha = _choice_of(text)
        q = row.get("queue_address")
        if escolha == "2":
            self._emit("form_aware_choice", cid, q, "2; abrir o menu")
            await self._prompt_menu(cid, row, None)
            return
        if escolha == "1":
            self._emit("form_aware_choice", cid, q, "1; falar sobre o formulario")
            await self._form_choice_transfer(cid, row)
            return
        await self._invalid_form_choice(cid, row)

    async def _invalid_form_choice(self, cid: int, row: dict) -> None:
        """Resposta fora de 1/2 na abertura: reenvia as opcoes (mesmo contador)."""
        max_r = int(self.cfg.get("max_invalid_retries") or 3)
        retries = int(row.get("retries") or 0)
        q = row.get("queue_address")
        if retries >= max_r:
            await self.close_out(cid, "closed_invalid")
            return
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            status="awaiting_form_choice",
            retries=retries + 1,
            last_prompt_at=now,
        )
        self._emit("ignored_reply", cid, q, "opção inválida na abertura form-aware")
        await self.send_guarded(cid, self.tpl.get("retry") or "", q)
        await self.send_guarded(cid, self._form_pending_text(self._form_owner(row), q), q)

    async def _form_choice_transfer(self, cid: int, row: dict) -> None:
        """Ramo 1: encaminha o chat ao analista que atende o formulario.

        O chat vai junto com o historico (o tecnico ve "Transferido para ..." e o
        analista recebe a conversa). Sem dono ainda — ou dono fora da janela — o
        chat entra na fila de espera PRESO ao caso: o tick entrega assim que o
        formulario tiver um analista elegivel.
        """
        q = row.get("queue_address")
        req = self._case_identity(cid, None)
        case_key = req[0]
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            status="classified",
            classification="Falar sobre o formulário",
            classified_at=now,
            auto_close=False,
        )
        self._emit("classified", cid, q, "Falar sobre o formulário")
        if not row.get("linked_form_chat_id"):
            # Estado antigo/limite (abertura sem vinculo): nao prender o chat em
            # um caso que nao existe — vale a rota normal (afinidade ou fila).
            await self._deliver_classified_chat(
                cid, row, q, case_key,
                detail="abertura form-aware sem vinculo de formulario",
            )
            return
        dono = self._form_owner(row)
        if dono:
            membro = self._member_of_queue(q, dono)
            if membro and _in_route_window(membro, datetime.now(TZ_BRT)):
                await self._deliver_classified_chat(
                    cid, row, q, case_key,
                    force_assignee=dono,
                    detail="abertura form-aware: falar sobre o formulario",
                )
                return
            self._emit(
                "form_aware_wait", cid, q,
                f"dono={dono} fora_da_janela; o chat espera o caso",
            )
        else:
            self._emit("form_aware_wait", cid, q, "formulario ainda sem analista; o chat espera o caso")
        await self._park_chat_for_owner(cid, row, case_key)

    async def _park_chat_for_owner(self, cid: int, row: dict, case_key: str) -> None:
        """Chat do ramo 1 esperando o dono do caso (nao consome tentativas)."""
        q = row.get("queue_address")
        team, settings = self._team_settings(q, "chat")
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            status="classified",
            classification="Falar sobre o formulário",
            classified_at=now,
            auto_close=False,
        )
        rid = db.enqueue_route(
            self.conn, "chat", cid, q, int(team["id"]) if team else None,
            detail="aguardando o analista do formulario",
            delay_sec=settings["retry_interval_sec"],
            expects_owner=True,
        )
        db.set_delivery_marks(self.conn, rid, case_key=case_key)
        texto = str(self.tpl.get("form_waiting") or "").strip()
        if texto:
            await self.send_guarded(cid, texto, q)
        self._emit("queued", cid, q, "espera o analista do formulario (mesmo caso)")

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
        spec = registry.get_spec(slug)
        fields = spec.get("fields") if spec else None
        if not spec or not fields:
            await self._invalid_reply(cid, row)
            return
        if slug == "criar_apoio":
            addr = (MEM.get(cid) or {}).get("started_addr") or ""
            login = _member_login_raw(addr)
            page = getattr(self.sock, "page", None)
            result = await resource_profile.lookup(page, login)
            if not result.get("allowed"):
                await self._deny_script(cid, row, slug, result)
                return
        now = datetime.now(timezone.utc)
        db.upsert_chat(
            self.conn, cid,
            queue_address=row.get("queue_address"),
            status="awaiting_script_answer",
            option_id=opt.get("id"),
            script_slug=slug,
            script_field_index=0,
            script_answers="{}",
            retries=0,
            last_prompt_at=now,
        )
        prompt = _field_prompt(fields[0])
        if prompt:
            await self.send(cid, prompt)

    async def _restart_script(self, cid: int, row: dict) -> None:
        spec = registry.get_spec(row.get("script_slug"))
        fields = spec.get("fields") if spec else None
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
        spec = registry.get_spec(row.get("script_slug"))
        fields = spec.get("fields") if spec else []
        try:
            idx = int(row.get("script_field_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if not fields or idx < 0 or idx >= len(fields):
            await self._restart_script(cid, row)
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
                db.upsert_chat(self.conn, cid, status="awaiting_script_answer", retries=retries + 1, last_prompt_at=now)
                self._emit("ignored_reply", cid, row.get("queue_address"), "campo inválido")
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
            await self.send(cid, _confirm_text(spec, answers))
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

    async def handle_script_confirm(self, cid: int, row: dict, text: str) -> None:
        choice = text.strip()
        if choice == "2":
            await self._restart_script(cid, row)
            return
        if choice == "1":
            await self._execute_script(cid, row, resume=False)
            return
        spec = registry.get_spec(row.get("script_slug"))
        answers = _script_answers(row)
        retries = int(row.get("retries") or 0)
        max_r = int(self.cfg.get("max_invalid_retries") or 3)
        if spec and retries < max_r:
            now = datetime.now(timezone.utc)
            db.upsert_chat(self.conn, cid, status="awaiting_script_confirm", retries=retries + 1, last_prompt_at=now)
            self._emit("ignored_reply", cid, row.get("queue_address"), "confirmação inválida")
            await self.send_guarded(cid, _confirm_text(spec, answers), row.get("queue_address"))
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
        ctx = {
            "chat_id": cid,
            "queue_address": row.get("queue_address"),
            "answers": _script_answers(row),
            "option": opt,
            "persist_job": lambda uid: self._persist_script_uuid(cid, uid),
            "started_at": row.get("script_started_at") or started,
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
        self._emit(reason, cid, q, str(result.get("detail") or "")[:800])
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
                return False, f"{route}; move_falhou={e}"
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
            "SELECT kind, last_position, expects_owner FROM pending_routes WHERE id = ?", (int(route_id),)
        ).fetchone() or {}
        # Rota presa ao dono do caso (abertura form-aware): quem ja foi avisado
        # (`form_waiting`) nao recebe posicao de fila por cima.
        if int(row.get("expects_owner") or 0):
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

    async def _listas_toa(self, queue_address: str | None) -> tuple[dict[int, dict], bool]:
        """O que o TOA lista para a fila: `in_process` + `pending` (+ sessao).

        `in_process` e a lista do front (chats E formularios em atendimento) e
        `pending` e a fila. A uniao e a visao autoritativa de "existe no TOA" — e
        o que permite fechar por AUSENCIA o que ja foi resolvido la.

        O segundo valor (`ok`) so e True quando as DUAS listas do front
        responderam: sem essa garantia a ausencia nao pode decidir nada (um
        timeout no `pending` esconderia um formulario que so espera na fila).
        """
        vistos: dict[int, dict] = {}
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
            return vistos, False
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
            except Exception as e:
                ok = False
                log.warning("get_helpdesk_chats %s tipo=%s carga: %s", queue_address, tipo, e)
        return vistos, ok

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
            vistos, ok = await self._listas_toa(q)
            fontes.update(vistos)
            ok_geral = ok_geral or ok
        logins = sorted({
            _member_login_raw(str(m["address"]))
            for ms in membros_por_time.values() for m in ms if m.get("address")
        })
        operadores = {str(m["address"]) for ms in membros_por_time.values() for m in ms if m.get("address")}
        online: set[str] = set()
        if logins:
            try:
                ack = await self.sock.command("get_online_users", {"users": logins}, timeout=12.0)
                parsed = _online_logins_from_ack(ack)
                if parsed is not None:
                    # O TOA devolve como digitado (F119537); normaliza para casar
                    # com member_login(addr) — mesma regra do roteamento.
                    online = {_member_login(x) for x in parsed}
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
                    janela=_window_label(m), online=1 if _member_login(addr) in online else 0,
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
    ) -> tuple[dict | None, str]:
        """Escolhe o analista da fila (kind='chat' ou 'form').

        `retry_online=False` faz uma única consulta de online: o retry
        assíncrono do formulário fica em pending_routes, sem travar o handler.
        `force_assignee` (abertura form-aware) prende a entrega ao dono do caso:
        se ele não estiver na janela, devolve None e quem entrega é a fila.
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
        if force_assignee:
            for m in members:
                if not _addr_eq(m["address"], force_assignee):
                    continue
                if not _in_route_window(m, datetime.now(TZ_BRT)):
                    return None, f"dono_do_caso={force_assignee} fora_da_janela"
                if settings.get("affinity_require_online") and _member_login(m["address"]) not in online:
                    return None, f"dono_do_caso={force_assignee} sem_online"
                self._reserve_add(str(m["address"]), kind, case_key)
                return m, f"dono_do_caso={force_assignee}; caso do formulario"
            return None, f"dono_do_caso={force_assignee} fora_do_time"
        dono = db.case_owner(self.conn, case_key) if (case_key and settings.get("affinity")) else ""
        aviso_afinidade = ""
        if dono:
            for m in members:
                if not _addr_eq(m["address"], dono):
                    continue
                if not _in_route_window(m, datetime.now(TZ_BRT)):
                    aviso_afinidade = "afinidade=" + dono + " fora_da_janela"
                elif settings.get("affinity_require_online") and _member_login(m["address"]) not in online:
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

        Não depende de queues.enabled: o formulário chega ao bot mesmo com a
        fila desligada para atendimento. A permissão é form_routing_enabled
        (global) + team_routing.form_enabled (por time).
        """
        if (self.cfg.get("form_routing_enabled") or "true") != "true":
            return
        if not queue_address:
            self._emit("form_no_queue", cid, None, "formulário sem fila")
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

        Cada linha e FIFO por conta propria: a cada tick o bot comeca por uma
        delas (alternando) e tenta a cabeca das duas na mesma passada — assim um
        formulario enviado depois de 10 chats e o 1o da linha dele, nao o 11.
        """
        self._recover_classified()
        linhas = db.due_routes(self.conn)
        if self._turno_fila % 2 == 0:
            primeira = list(linhas.get("chat") or [])
            segunda = list(linhas.get("form") or [])
        else:
            primeira = list(linhas.get("form") or [])
            segunda = list(linhas.get("chat") or [])
        self._turno_fila += 1
        # Alterna as linhas dentro do MESMO tick (chat, form, chat, form...): a
        # cabeca do formulario nao espera a linha do chat andar inteira.
        fila: list[dict] = []
        for i in range(max(len(primeira), len(segunda))):
            if i < len(primeira):
                fila.append(primeira[i])
            if i < len(segunda):
                fila.append(segunda[i])
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
            # Abertura form-aware: rota presa ao dono do caso (analista do
            # formulario). Sem dono ainda ela so espera — sem tentativa e sem
            # fallback — e sai quando o formulario for atribuido (peca irma).
            dono_caso = ""
            if int(row.get("expects_owner") or 0):
                dono_caso = db.case_owner(self.conn, str(row.get("case_key") or ""))
                if not dono_caso:
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
                q, kind=kind, retry_online=False, case_key=case_key, force_assignee=dono_caso
            )
            if not picked:
                if dono_caso:
                    # Dono existe mas nao esta elegivel agora (janela/online): o
                    # chat continua esperando o caso, sem gastar tentativa.
                    continue
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

    async def tick_timeouts(self) -> None:
        db.assign_close_stale(self.conn, int(self.cfg.get("route_assign_ttl_sec") or 43200))
        # Visao viva dos formularios (pendente/em atendimento/passado): um por tick.
        try:
            await self._sweep_form_states()
        except Exception:
            log.exception("sweep_form_states")
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
