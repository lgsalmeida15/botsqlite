# Plano de correção — rotas órfãs (chats perdidos no restart)

Diagnóstico de produção em **2026-10-10** (base `chat_bot_toa`). Este documento é o
plano para corrigir a causa raiz no código. Enquanto ele não é aplicado, o
`bin/watchdog_rotas_orfas.py` (e o `.bat`) fazem a contenção.

> **Status (2026-10-10):** **M1 + M2 + M3 implementados** no repositório, com testes
> novos em `tests/test_resume.py` e `tests/test_queue.py` (suite completa verde) e
> `docs/desenho-bot.md` atualizado. **M4 pendente** (opcional, depende de evidência).
> Falta só o **deploy em produção** (ver *Deploy em produção*).

## Problema

- **Sintoma:** chats entram na fila (`pending_routes.waiting`) e alguns nunca são
  roteados.
- **Evidência:** 91 rotas de chat `waiting` com o chat fora do estado aberto
  (87 `left`); `move_chat` erro `e=10` **161.406x em 30 dias**; rotas com milhares
  de tentativas (ex.: id 6618, `attempts` 11.772, 5.437 `move_chat` sem nenhum OK).
- **Causa raiz:** no boot, `resume_open_chats` / `resume_action == "stale"`
  ([engine.py:1213](../python/engine.py#L1213)) marca o **chat** como `left` mas
  **não encerra a rota `waiting`**. A rota fica órfã: o tick escolhe analista e
  chama `move_chat` num chat que o bot não conduz → TOA recusa (`e=10`) →
  retentativa infinita (para **chat** não há teto de falhas de movimentação).
- Comparar: os outros desfechos (`on_left`, `chat_removed`, `leave`, `_adota_chat_na_fila`)
  **fecham a rota**; só o `stale` não fechava.

## Objetivo

1. Fechar a brecha (nenhuma rota fica `waiting` sem chat conduzido).
2. Endurecer a entrega para não girar para sempre quando o TOA recusa o `move_chat`.
3. Tornar o bot auto-suficiente para que o watchdog externo deixe de ser necessário.

---

## M1 — P0: encerrar a rota no `resume_stale` (obrigatória)

**Arquivo:** `python/engine.py`, ramo `elif action == "stale":` (~L1213).

Antes:
```python
elif action == "stale":
    db.upsert_chat(self.conn, cid, status="left", left_at=now)
    if not self._case_recent(cid):
        self._close_case(cid, "stale_boot")
    self._emit("resume_stale", cid, q, "chat sumiu do TOA")
```
Depois:
```python
elif action == "stale":
    # SEM isto a rota seguia `waiting` para sempre: o tick tentava `move_chat`
    # num chat que o bot ja nao conduz (TOA recusa com e=10) e a fila entupia.
    db.finish_routes_for_chat(self.conn, cid, "failed", "chat sumiu do TOA")
    db.upsert_chat(self.conn, cid, status="left", left_at=now)
    if not self._case_recent(cid):
        self._close_case(cid, "stale_boot")
    self._emit("resume_stale", cid, q, "chat sumiu do TOA")
```

- Alinha com o que `leave()` ([engine.py:3603](../python/engine.py#L3603)) e o
  `chat_removed` ([engine.py:525](../python/engine.py#L525)) já fazem.
- `_recover_classified` não recupera esse chat (`status != 'classified'`), então
  fechar a rota é o correto: o chat realmente sumiu.

**Risco:** baixo (1 linha, alinhada ao resto do fluxo).

---

## M2 — P1: teto de falhas de `move_chat` para CHAT (recomendada)

Hoje, com fila ligada e chat não movido, a rota de chat **nunca** encerra por
recusa de `move_chat` ([engine.py:3555](../python/engine.py#L3555)). Reusar o
mecanismo que já existe para formulário.

**Arquivos:** `python/engine.py`.

1. Em `_deliver_chat`, no `except` do `move_chat` ([engine.py:2241](../python/engine.py#L2241)),
   contar a recusa em vez de apenas devolver `False`:
```python
except Exception as e:
    log.warning("move_chat %s %s: %s", cid, q, e)
    self._reserve_drop(addr, "chat", case_key)
    if str(marks.get("exclude_assignee") or "").strip():
        # resgate: o chat ja estava com o dono offline; segue para o convite
        self._emit("reclaim_invite_only", cid, q, f"{marks.get('exclude_assignee')}; move recusado={e}")
        db.set_delivery_marks(self.conn, route_id, invite_target=addr)
    else:
        falhas = db.bump_move_fails(self.conn, route_id)          # NOVO
        return False, f"{route}; move_falhou={e}; falhas={falhas}"  # detalhe com falhas=
```
2. Em `tick_routes`, no ramo de chat (após `_deliver_chat` devolver `ok == False`),
   encerrar quando o teto for atingido — como o formulário
   ([engine.py:3574](../python/engine.py#L3574)):
```python
if self._move_falhas(detail) >= self._move_max_fail():
    motivo = f"move_chat recusado pelo TOA ({self._move_falhas(detail)}x); rota encerrada"
    db.finish_route(self.conn, rid, "failed", f"{motivo}; {detail}")
    self._emit("chat_move_refused", cid, q, f"{motivo}; {detail}")
    await self._apply_chat_fallback(cid, q)
    continue
db.retry_route(self.conn, rid, used + 1, settings["retry_interval_sec"], detail)
```

- **Só conta recusa real** de `move_chat` (exceção), nunca "sem analista elegível" —
  preserva a espera sem prazo da fila (o caso legítimo continua esperando).
- Reusa `db.bump_move_fails` ([db.py:1509](../python/db.py#L1509)),
  `_move_falhas` / `_move_max_fail` ([engine.py:1041](../python/engine.py#L1041)).
  Avaliar renomear a chave `form_move_max_fail` → genérica (ex.: `move_max_fail`),
  com fallback para não quebrar configuração existente.

**Risco:** médio — muda o desfecho de chats cujo `move_chat` é recusado N vezes
(passa a fechar em vez de esperar). Coberto por testes.

---

## M3 — P1: contenção dentro do bot (varredura de órfãs)

Mesmo com M1, uma saída nova de chat sem fechar rota voltaria a acumular. Adicionar
uma varredura barata que replica o watchdog por dentro.

**Arquivos:** `python/db.py` + `python/engine.py`.

```python
# db.py
def finish_orphan_routes(conn, status: str = "failed",
                         detail: str = "rota orfa: chat fora do TOA") -> int:
    cur = conn.execute(
        """UPDATE pending_routes
              SET status = ?, detail = ?, next_try_at = NULL,
                  claimed_by = '', claim_expires_at = NULL
            WHERE status = 'waiting' AND kind = 'chat'
              AND chat_id IN (SELECT chat_id FROM chats
                               WHERE status NOT IN
                                 ('joined','menu_sent','awaiting_reply','classified',
                                  'monitoring','awaiting_script_answer',
                                  'awaiting_script_confirm','awaiting_script_run'))
              AND (COALESCE(claimed_by,'') = ''
                   OR claim_expires_at IS NULL
                   OR claim_expires_at <= datetime('now'))""",
        (str(status), detail),
    )
    return int(cur.rowcount or 0)
```
Chamar em `tick_timeouts` ([engine.py:3710](../python/engine.py#L3710)) e emitir
`orphan_route_closed` quando `> 0`.

**Risco:** baixo.

---

## M4 — P2 (opcional): tratar `e=10` no `move_chat` de chat como "já movido"

Há chats `classified` (vivos) que também recebem `e=10` no `move_chat`. Se a
evidência confirmar que `e=10` em chat = "já está na fila/atendimento", então
`_deliver_chat` deve **pular o move e ir para `invite_to_chat`** (espelhando a
adoção do formulário em [engine.py:3180](../python/engine.py#L3180)). **Só ligar
depois de validar com dados reais** — a semântica de `e=10` para chat precisa de
confirmação.

---

## Testes (obrigatórios antes do deploy)

Suíte offline (SQLite temporário), conforme README:

```powershell
Get-ChildItem tests\test_*.py | % { python $_.FullName }   # 24 arquivos, 501 casos
```

Testes novos a adicionar:
- `tests/test_resume.py` — chat `classified` com rota `waiting` ausente da lista
  ativa do TOA ⇒ `resume_stale` deixa a rota `failed` e o chat `left`.
- `tests/test_queue.py` (ou `test_delivery_*`) — `move_chat` recusado N vezes ⇒ rota
  encerrada (não gira para sempre) e fallback aplicado; e "sem analista" **não**
  consome o teto.
- `tests/test_queue.py` — `finish_orphan_routes` fecha só rotas de chat com chat
  fora do estado aberto e respeita lease ativa.

## Documentação

- `docs/desenho-bot.md` (mermaid L562 e lista de eventos L683): o `resume_stale`
  passa a "status left **+ encerra a rota** + evento `resume_stale`"; incluir o
  evento novo `chat_move_refused`/`orphan_route_closed`.

## Deploy em produção (replicar)

1. **Backup** da base: `mysqldump chat_bot_toa > chat_bot_toa_<data>.sql`.
2. Parar o bot (evita corrida entre patch e tick).
3. Aplicar `engine.py` / `db.py` (M1 obrigatório; M2/M3 recomendados).
4. Limpar as órfãs existentes (idempotente):
   `python bin\watchdog_rotas_orfas.py --env .env --apply` no host de produção.
5. Subir o bot; conferir no `bot_events`: `resume_stale` agora com rota fechada e
   queda de `delivery_attempts` `move_chat e=10`.
6. Aposentar o watchdog externo (`bin\watchdog_rotas_orfas.bat`) — não será mais necessário.

## Rollback

- `git revert` dos commits; religar `bin\watchdog_rotas_orfas.bat` como contenção.

## Contenção interina (sem mexer no código)

Enquanto M1–M4 não entram em produção:

- `bin/watchdog_rotas_orfas.py` — script (dry-run por padrão; `--apply` escreve).
- `bin/watchdog_rotas_orfas.bat` — watchdog **contínuo** (`--watch`).
- `bin/watchdog_rotas_orfas_once.bat` — **execução única**, para o Agendador de Tarefas.

Cadência recomendada (impacto × simplicidade):

| Cadência | Efeito |
|---|---|
| Contínuo (`--watch`) | Melhor: limpa quase de imediato. |
| Agendado a cada 10–15 min (`once`) | Quase tão bom; sobrevive a reboot sem processo aberto. |
| 1x por dia | Paliativo: as órfãs voltam a cada restart do bot. |

As órfãs nascem em **rajadas nos restarts** (`resume_stale`) — na produção houve
dias com 52, 15 e 12 eventos. O watchdog **não resolve capacidade**: chats
`classified` esperando analista elegível continuam esperando, mesmo no contínuo.

### Agendador de Tarefas (Windows)

GUI (`taskschd.msc` → **Criar Tarefa**, não a Básica):
- **Geral:** “Executar estando o usuário conectado ou não” + “Executar com privilégios mais altos”.
- **Disparadores:** Diariamente → “Repetir a tarefa a cada” **15 minutos** → “por” **1 dia**.
- **Ações:** programa = `...\bin\watchdog_rotas_orfas_once.bat`; “Iniciar em” = a pasta do projeto.
- **Configurações:** “Se a tarefa falhar, reiniciar a cada 5 min”.

Linha de comando (`schtasks`):

```
schtasks /Create /TN "Watchdog rotas orfas" ^
  /TR "\"<RAIZ>\bin\watchdog_rotas_orfas_once.bat\"" ^
  /SC MINUTE /MO 15 /RL HIGHEST /F
```

## Arquivos afetados

| Arquivo | Mudança |
|---|---|
| `python/engine.py` | M1 (stale), M2 (`_deliver_chat`/`tick_routes`), M3 (chamada da varredura) |
| `python/db.py` | M3 (`finish_orphan_routes`) |
| `tests/*` | testes novos |
| `docs/desenho-bot.md` | atualização do fluxo/evento |
