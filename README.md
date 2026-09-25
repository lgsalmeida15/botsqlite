# HELPTOA Bot (SQLite)

Daemon Playwright + painel PHP no **mesmo servidor**, com banco **SQLite** local. Sem CDN.

Este diretório é um subprojeto autônomo (pode ir para um repositório separado).

## Requisitos

- Python 3.10+ com `pip` (Playwright + Chromium)
- PHP 7.4.3+ com extensão `pdo_sqlite` (obrigatória). `mbstring` é **opcional**: o painel não depende dela (o corte de texto usa `ui_clip`, com alternativa própria quando a extensão não está habilitada)
- Bot e painel no mesmo host, com escrita na pasta `data/`

## Configuração

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium
copy .env.example .env
```

No Linux use `python3 -m venv`, `source .venv/bin/activate` e `cp .env.example .env`.

Edite `.env`:

| Variável | Obrigatória | Descrição |
|---|---|---|
| `SQLITE_PATH` | não | Padrão `data/helptoa.sqlite` (relativo a esta pasta) |
| `TOA_ORIGIN` | não | Padrão `https://clarosa.etadirect.com` |
| `TOA_USER` | sim em headless | Login do TOA |
| `TOA_PASS` | sim em headless | Senha |
| `TOA_HEADLESS` | não | `1` em produção (sem janela) |
| `TOA_NAV_TIMEOUT_MS` | não | Tempo máximo (ms) de navegação no TOA. Padrão `60000` |
| `BOT_API_TOKEN` | se usar a API | Header `X-Bot-Token` em `/api/v1/classifications` |

O arquivo SQLite é criado na primeira conexão (bot ou painel). Banco começa vazio, com templates e ajustes padrão.

Crie o usuário do painel:

```bash
php bin/create-user.php admin senha-aqui
php bin/create-user.php joao senha-aqui "Super Admin"   # grupo por nome (ou por id)
```

O painel tem grupos de permissão e escopo por time (`permission_groups`,
`group_permissions`, `user_teams`, `user_queues`). A base nasce com o grupo
**Super Admin** e o primeiro usuário criado entra nele automaticamente; quem é
criado depois fica **sem grupo** (sem acesso) até ser associado a um grupo.

## Painel PHP

Document root: pasta `php/`. A página inicial é o **Dashboard**.

O layout usa Bootstrap 5.3.3 em `php/assets/` (sem internet). A conta do PHP e a do serviço Python precisam gravar em `data/` (incluindo `*.sqlite-wal` e `*.sqlite-shm`).

### Usuarios, grupos e permissoes (RBAC)

- **Usuarios** (super-only): cria/edita contas, reseta senha e escolhe o **grupo**. O que o
  usuario enxerga nao fica no usuario: vem do grupo.
- **Grupos** (super-only): o grupo tem duas partes.
  1. **Permissoes** - quais paginas e acoes de escrita valem (`page.*`, `*.manage`) e as chaves
     `scope.all_queues`, `scope.all_teams` e `scope.all_menus` (ver tudo naquele eixo).
  2. **Escopo de dados** - quais **times**, **filas** e **menus** o grupo enxerga.
- Como o escopo e resolvido:
  - **Filas visiveis** = filas dos times marcados em `group_teams` (via `team_queues`) mais as
    filas marcadas em `group_queues` (permite liberar filas que nao estao em nenhum time).
  - **Menus visiveis** = `group_menus`. Menu e configuracao: **nao** concede as filas dele.
  - **Times visiveis** = `group_teams` (vale para as abas Times e Roteamento).
  - **Negar por padrao**: sem marcacao e sem `scope.all_*` o grupo nao ve dado nenhum, e a tela
    de Grupos mostra um aviso.
  - Ao criar um menu ou time, o item entra no escopo do grupo de quem criou (senao ele
    desapareceria da tela logo depois).
- Escrita em time/menu tambem exige o item no escopo do grupo, alem da permissao de acao.
- As tabelas `user_teams`/`user_queues` (escopo por usuario da primeira versao) ficaram como
  legado: a migracao copia o conteudo para o grupo uma unica vez (flag `rbac_scope_groups`).
- Toda pagina e toda acao POST sao validadas no backend (`php/auth.php`): sem permissao o
  painel responde **403**, mesmo por URL direta ou POST forjado.

### Perguntas antes de transferir

Cada folha **Pedir info e transferir** pode ter perguntas cadastradas na aba Menus (tipo texto,
numero ou escolha; 1 a 20 por opcao). Sem perguntas, a opcao continua funcionando como antes.

Com perguntas, o bot pergunta uma a uma, o tecnico confirma (1) ou corrige (2) e so entao a
transferencia acontece. O resumo fica no chat, as respostas aparecem na aba **Chats** (coluna
Respostas) e a transferencia vai pela fila (`pending_routes`), sobrevivendo a kill/restart do bot.
Os textos `ask_received` e `ask_transferring` sao editaveis na aba Templates.

- **Ordem das mensagens:** o resumo (`ask_received`) **nao** traz mais o `ask_transferring`. Com analista
  elegivel o bot envia `ask_transferring` e entrega na sequencia; sem analista, a mensagem que o tecnico
  recebe e o aviso `queue_position` — nunca "estou transferindo" seguido de "voce e o Nº 1 da fila".
- **`move_chat` uma vez por chat:** a marca fica em `chats.moved_at`. Se o `invite_to_chat` for recusado
  depois do move (ex.: analista que nao e operador da fila no TOA), a rota registra `transferred_partial`
  e as tentativas seguintes **apenas convidam**, sem arrastar o chat de novo.

### Fila de espera com posição (chat e formulário)

Quando ninguém elegível está online, o item **não é descartado**: ele entra na fila (`pending_routes`) e
o técnico é avisado por mensagem (editáveis em Templates). Isso vale para **qualquer** opção de
transferência (com ou sem perguntas) **e para o formulário** — o formulário entra na mesma fila, só que
**em silêncio** (não existe chat para avisar). A cada 5 s o bot tenta atribuir, respeitando
cap/janela/online; ao conseguir, entrega e registra `queued_assigned` com o tempo de espera.

- **Duas linhas:** chat e formulário têm **filas apartadas** — a posição é contada só entre iguais, então
  um formulário enviado depois de 10 chats é o **1º da linha dele** (não o 11º) e é tentado no mesmo tick
  em que a cabeça da linha de chat é tentada.
- **Aviso padrão:** o técnico ouve apenas **"Você avançou na fila"** (`queue_progress`) na entrada e quando
  a posição melhora — **sem número**. Para voltar a falar o número, escolha *Falar o número da fila* em
  Roteamento → Globais (`queue_show_pos`), e ele aparece enquanto a posição estiver dentro de
  `queue_top_pos` (padrão **5**); acima disso volta o texto de progresso — antes disso, com 18 formulários
  na fila, um chat com 2 chats à frente ouvia "Nº 20".
- **Sem prazo:** a espera **não vence sozinha** — o item só sai da fila por **entrega**, porque o **técnico
  saiu do chat** (ou o chat foi removido), porque **sumiu das listas do TOA** (resolvido:
  `chat_resolvido_toa`/`form_resolvido_toa`) ou pelo botão **Encerrar espera** (aí o bot aplica o fallback do
  time). O antigo `queue_max_wait_sec` foi **removido** — ele encerrava chats sem transferência quando o time
  ficava saturado (caso real: 30 min de espera, técnico em 1º lugar, ninguém elegível e o bot fechando o chat).
- **Formulário:** mesma régua — sem prazo, não gasta tentativas e não cai no fallback com a fila ligada
  (tentativas e fallback valem com a fila desligada). A rota dele também termina com `form_no_agent` quando
  o item some das listas do TOA.
- **Entrar na fila não é falha:** com a fila ligada o evento é `queued` (com o motivo "sem analista
  elegível") para chat **e** formulário; `form_no_agent` fica reservado para a fila desligada/fallback
  *monitorar* — antes o painel mostrava "sem analista" para item que estava esperando a vez.
- Com a fila **ligada** o formulário também espera e **não gasta** tentativas; `form_route_attempts`/fallback do formulário valem com a fila **desligada**.
- O card **Fila de espera (agora)** na aba Roteamento mostra posição, tempo, tentativas e o prazo
  ("sem prazo" quando não há), com o botão **Encerrar espera**.
- No **Dashboard**, o card "Fila de espera do chat" mostra quantos esperam agora, a espera média e a
  maior espera do período (espera = do "queued" até a atribuição a um analista).
- Em Roteamento, **Fila de espera (chat) = Desligada** volta ao comportamento antigo: apenas
  `transfer_attempts` tentativas com `transfer_retry_interval_sec` entre elas e, ao esgotar, o fallback.
- **Excecao da espera sem limite:** se o `move_chat` **ja foi feito** e o `invite_to_chat` esta falhando,
  o chat nao fica "pulando": as tentativas contam (`transfer_attempts`/`transfer_retry_interval_sec`),
  o evento `route_retry` aparece na aba Atividade e, ao esgotar, vale o fallback do time.

### Abertura form-aware (chat com formulário aberto)

Se o técnico abre um chat e **já tem um formulário de validação aberto**, o bot não manda o menu: ele
oferece **1) Falar sobre o formulário** / **2) Abrir o menu** (templates `form_pending`, ou
`form_pending_wait` quando o formulário ainda não tem analista).

- **1** → o chat é **encaminhado ao analista que atende o formulário** (`move_chat` + `invite_to_chat`, com
  o histórico junto: o técnico vê "Transferido para …"). Sem analista ainda, ou com o dono fora da
  janela/offline, o bot responde `form_waiting` e o chat **espera preso ao caso** — sem gastar tentativa e
  sem fallback — até o formulário ser assumido.
- **2** → menu normal; a folha de transferência usa o **mesmo caso** e cai no analista do formulário
  (afinidade) ou na fila.
- **Um caso só:** chat e formulário do mesmo técnico compartilham o `case_key` no ledger, então
  **encaminhado um, o outro vai junto** (evento `case_sibling_delivered`). A vinculação fica em
  `chats.linked_form_chat_id` e é **definitiva**: um formulário já oferecido a um chat (mesmo encerrado)
  não é oferecido de novo — era o falso positivo em que o técnico abria outro chat e a abertura reaparecia.
- **Identidade do caso (sem "balde"):** chave de caso **nunca** sai de nome genérico. Formulário que chega
  sem o address do técnico vira um caso **por item** (`chat:<id>`) — antes todos caíam na mesma chave
  (`n:message scenario agent`) e a afinidade entregava tudo ao mesmo analista. Na entrega, a identidade do
  payload manda sobre a chave que já estava na rota (divergência vira evento `case_key_divergente`).
- **Três estados do formulário** (iguais aos da extensão do TOA): **pendente**, **em atendimento** (o analista
  entrou) e **passado** (o analista saiu / atendimento concluído). O bot lê isso das **mensagens do
  formulário** (`get_chat_messages`, só leitura — nunca entra nem envia no formulário), no boot, na abertura
  (**leitura forçada**, para a resposta ao técnico nunca sair de cache), **antes de cada entrega** e numa
  varredura de 1 formulário por tick; quando o formulário vira passado ele **sai de cena**
  (`form_closed`): sai do índice, o caso fecha, as rotas presas são encerradas (`form_route_cancelled`) e
  quem esperava por ele volta para a fila. Formulário passado **não entra na fila**
  (`form_skip_past`). Sem resposta do TOA o bot não decide nada (formulário vivo não cai por falha de leitura).
- **Correlação**, nesta ordem: `address` do payload → **AUTOR das mensagens do formulário** (o técnico
  escreve lá; é a identidade que sobra quando o payload vem sem address) → nome quando
  `affinity_match_name = true` → **fila, só com um único candidato SEM responsável** (sem caso aberto no
  ledger e sem analista do time dentro). Com dois ou mais candidatos sem identidade o vínculo é recusado
  (`form_aware_skip` com `ambiguo:<n>` ou `em_atendimento:<n>`), e mesmo no palpite por fila o vínculo é
  recusado se o formulário é de **outro** técnico. Um único candidato **já em atendimento** ainda casa: é o
  técnico abrindo o chat para falar do próprio formulário. Antes o bot sempre pegava o formulário mais
  recente da fila (`regra=fila`, 28 de 28 vinculações no banco real): era assim que o caso de um técnico era
  unificado com o formulário de outro — o caso "trocava de analista" no painel. O vínculo vindo de palpite
  fica registrado em `case_link_unconfirmed`.
- **Janela de vida (`form_ocioso`):** o TOA **não avisa a saída do analista** (`user_left` não chega), então
  formulário morto ficava no índice e no caso até o TTL de 12 h — enchia a fila (a abertura caía em
  `ambiguo`) e segurava a vaga do analista (cap cheio sem trabalho real). Agora:
  - **Oferta:** sem mensagem nova por `form_idle_offer_min` (30 min) e sem analista do time dentro, ele
    **deixa de ser oferecido** (`form_aware_skip … ociosos=1`); com analista dentro a oferta continua.
  - **Fechamento:** `form_idle_close_min` (120 min) → caso fecha com motivo `form_ocioso`; **com analista do
    time dentro** vale `form_idle_owner_close_min` (240 min) → motivo `form_ocioso_analista` (o TOA nunca
    mostra a saída dele — era o estado real dos casos que travavam os caps). Rotas canceladas e vaga liberada.
  - O relógio é o `time` da última mensagem; sem `time` confiável vale o relógio do índice (após restart,
    fecha no máximo 120/240 min depois do boot).
- **Índice enxuto:** a poda (também na varredura) descarta o formulário cujo caso no ledger **já encerrou de
  verdade** (`form_done`/`chat_removed` → `form_forget motivo=caso_encerrado`); caso expirado por
  TTL/`stale_boot` não descarta (pode estar aberto no TOA).
- **Formulário já com analista:** se as mensagens mostram um analista do time dentro (`user_joined`), o bot
  **adota** esse dono (`form_adopted`) em vez de repetir o `move_chat` — o TOA recusa mover um formulário que
  já está com alguém (`e=10`) e a rota ficava retentando para sempre (32 tentativas, sempre no mesmo
  analista). No teto de recusas (`form_move_max_fail`, padrão 3) a rota é encerrada (`form_move_refused`).
- **Indice e diagnóstico:** o formulário fica no índice em memória até um **fim explícito** (payload com
  `lifecycle` finalizado ou `ended_time` de verdade), `chat_removed`, TTL de 12 h, ou caso já encerrado no
  ledger (`form_done`/`chat_removed`). Aviso de **fila** (`helpdesk_chat_removed`, o que o `move_chat` para o
  analista gera) **não** tira o formulário — era por ali que a abertura se perdia em produção. Eventos que
  tornam a decisão auditável: `form_indexed`, `form_not_open`, `form_dropped`, `form_ignored_payload`,
  `form_queue_left`, `form_forget` e `form_aware_skip` (`abertos=`, `na_fila=`, `vinculados=`, `encerrados=`).
- **Restart:** o índice de formulários abertos vive em memória, então o boot o reconstrói do ledger **antes**
  do resume; a varredura de boot não fecha caso de formulário (o bot não é participante dele, então ele nunca
  aparece em `get_active_chats`) — o TTL continua fechando caso órfão.
- Eventos: `case_linked`, `form_aware_greeting`, `form_aware_choice`, `form_aware_wait`, `form_aware_skip`,
  `case_sibling_delivered`. Status novo do chat: `awaiting_form_choice`.

**Conferindo na aba Atividade quando a abertura NÃO acontece** (antes isso era silencioso):

| O que aparece | O que significa |
|---|---|
| `form_aware_skip … abertos=0` | Nenhum formulário no índice. Procure `form_not_open` / `form_dropped` / `form_forget` para ver por que ele saiu (ou nem entrou) e `form_indexed` para confirmar que entrou. |
| `form_aware_skip … na_fila=0` | Existe formulário no índice, mas não é da fila deste chat — ou já está vinculado a outro chat (`vinculados=`). |
| `form_aware_skip … ambiguo:N` | Há N formulários livres na fila e nenhum com a identidade deste técnico: o bot **não adivinha** (antes pegava o mais recente e unia casos de pessoas diferentes). |
| `form_aware_skip … em_atendimento:N` | Os N candidatos da fila **já têm responsável** (caso aberto ou analista dentro): o palpite não usa formulário em atendimento de terceiro. |
| `form_aware_skip … ociosos=N` | Os candidatos ficaram **sem sinal de vida** (`form_idle_offer_min`): deixam de ser oferecidos; no fechamento vale 120 min sem analista dentro ou 240 min com ele (`form_ocioso` / `form_ocioso_analista`). |
| `form_closed … motivo=form_ocioso_analista` | Formulário parado há muito tempo **com analista do time dentro**: o caso fechou e a vaga voltou (o TOA não mostra a saída dele). |
| `form_forget … motivo=caso_encerrado` | O caso do formulário já estava encerrado de verdade: saiu do índice (a varredura para de ler no TOA). |
| `form_aware_skip … ocioso (N min)` | O formulário lido na abertura estava ocioso — a leitura viva agora vale antes de responder ao técnico. |
| `form_aware_skip … chat de` | O formulário mais recente da fila é de **outro** técnico (a chave do caso dele aponta para outro address): vínculo recusado. |
| `case_link_unconfirmed` | Vínculo feito pelo **palpite** de fila (sem identidade confirmada), registrado para auditoria. |
| `form_adopted` | O formulário já estava com um analista do time (`user_joined` nas mensagens): o bot adotou o dono e **não** repetiu o `move_chat`. |
| `form_skip_past` | A leitura antes da entrega mostrou o formulário **encerrado**: a rota foi cancelada em vez de mover um formulário morto. |
| `form_move_refused` | O TOA recusou o `move_chat` no teto de tentativas (`move_fails`): a rota para de girar e vai para o painel como falha. |
| `form_aware_skip … encerrados=1` | O formulário foi encerrado de verdade (`form_done`/`chat_removed`) e não vale mais para a abertura. |
| `form_queue_left` | O formulário saiu da **fila** do helpdesk (é o aviso que o `move_chat` para o analista gera) e continua acompanhado — não é motivo para a abertura falhar. |
| `form_not_open` | O payload trouxe fim explícito (`lifecycle` finalizado ou `ended_time` de verdade); o detalhe mostra `mode=`/`lifecycle=`/`ended_time=` do formulário. |
| `form_state` | Estado lido do formulário: `estado=pending/doing/past` + quantas mensagens foram lidas + o motivo da checagem. |
| `form_closed` | O formulário virou **passado** (analista saiu / atendimento concluído): saiu do índice e o caso fechou. |
| `message_ignored` | O técnico mandou mensagem em um status que o bot não trata (`status=…` no detalhe). Antes isso era silencioso — foi assim que um "1" se perdeu. |

### Timeout do menu e resposta perdida

O `reply_timeout_sec` (padrao 300 s) mede o **silencio do tecnico**, nao o tempo desde a ultima pergunta:
qualquer mensagem do solicitante (`last_reply_at`) reinicia o relogio, inclusive resposta invalida. Se o
bot reiniciar ou reciclar o Chromium e o tecnico tiver respondido enquanto ele estava fora, o bot
**relê as mensagens do chat** (`get_chat_messages`) e processa a resposta pendente (evento
`reply_replayed`) em vez de encerrar em `closed_timeout`.

### Entrega robusta (FASE A)

A entrega (chat e formulário) tem **registro único no banco** em `pending_routes`, com **posse**
(claim atômico: `claimed_by`/`claim_expires_at`), **alvo imutável** (`invite_target`) e a marca do
`move_chat` (`move_sent_at`) — sinais que sobrevivem ao restart e impedem arrastar/convidar duas
vezes o mesmo atendimento (o índice único `pending_routes_active` garante no banco uma única
operação ativa por `(kind, chat_id)`).

O caso aberto no ledger (`route_assignments`) fecha com **motivo** em `close_reason`
(`chat_removed`, `stale_boot`, `form_done`, `ttl`, `manual`), e a varredura de boot fecha o caso
quando o TOA não mostra mais o item (respeitando `case_stale_confirm_sec`). Rota que morria em
silêncio agora gera evento (`delivery_failed_no_team`) e aplica o fallback do time; chat
`classified` sem entrega é recuperado pelo tick (`delivery_recovered`).

Cada chamada externa de entrega fica medida em `delivery_attempts` (operação, alvo, `r` do TOA,
resultado `ok`/`timeout`/`error`, latência) e o detalhe do evento `transferred` passou a trazer
`move=`/`invite=`. Na aba **Roteamento** há os cards **Entregas paradas e sem dono** e **Casos
abertos (ledger) e tentativas de entrega (24 h)**, com *Reprocessar* (rota falha) e *Liberar*
(caso preso no ledger).

## Bot

Na raiz deste diretório:

```bash
.venv\Scripts\python run.py
```

Na primeira vez o Chromium abre: complete o login/SSO do TOA. O perfil fica em `.chrome-profile/`.

Habilite **uma fila** no painel e cadastre opções do menu antes de testar.

O banco guarda uma linha por `chat_id` (status, classificação, retries, timestamps). Sem texto de conversa.

## Roteamento (chat e formulário)

A aba **Roteamento** concentra cap por analista, estratégia (`rodízio`, `prioridade` por peso,
`menor carga`), tentativas/intervalo, fallback, **fila de espera** (`queue_wait_enabled`) e os ajustes
por time e por membro. A herança é **membro → time → global** (campo vazio = herda). O diagnóstico da
aba mostra quem está dentro da janela de plantão agora; online e carga são lidos no TOA a cada
transferência.

Distribuição de carga:

- **Casos** é o número que a escolha usa: formulário e chat do **mesmo autor** contam **um caso**;
  autores distintos contam um caso cada. O campo existe no global (`max_cases_per_agent`), no time e no
  operador — com os checkboxes **fazer valer para todos os times** / **e para todos os operadores** (limpa
  os overrides; `NULL` = herda) e **replicar para todos os operadores** no bloco do time.
- **Tipo de atendimento por operador** (`kinds`): **ambos** (padrão), **só chat** ou **só formulário**.
  O time tem um **padrão dos operadores** (`member_kinds`) aplicado a quem não tem ajuste individual, com
  a opção de replicar para todos. Quem atende só chat não recebe formulário e vice-versa (`so_chat`/
  `so_form` no log da decisão). O antigo "recebe formulário" continua valendo como espelho.

O **formulário de validação** do TOA não é chat de menu: quando chega, o bot o roteia para o time
da fila dele com a mesma dinâmica do chat (janela de plantão, online, cap e estratégia), sem
entrar no formulário e sem encerrá-lo. Cada tentativa é agendada em `pending_routes` (drenada pelo
tick do bot), então o atendimento não fica esperando; o mesmo formulário nunca é roteado duas
vezes, inclusive após restart. Eventos na aba Atividade: `closure`, `form_routed`, `form_no_agent`,
`form_route_retry`, `form_no_team`, `form_no_queue`.

Uma fila desabilitada para chat **continua** roteando formulário: o que manda é o global
`form_routing_enabled` e o campo "recebe formulário" do time.

## Desenho do fluxo

Documentacao visual do fluxo completo - atendimento, menu, roteamento por caso, formulario de validacao, timers e banco: [docs/desenho-bot.md](docs/desenho-bot.md).

Fluxogramas do atendimento como ele esta hoje, para imprimir ou apresentar: [docs/fluxograma-atendimento.html](docs/fluxograma-atendimento.html) e [docs/fluxograma-atendimento.pdf](docs/fluxograma-atendimento.pdf).

## O que não vai para o Git

`.env`, `.venv/`, `.chrome-profile/`, `data/*.sqlite*`, `.bot.lock`.

~
### Carga por CASO (formulario + chat do mesmo tecnico = 1 caso)

O limite principal e **max_cases_per_agent** (aba Roteamento, campo **Casos**): conta
**solicitantes distintos** com atendimento aberto. O formulario de validacao e o chat do **mesmo
tecnico** contam como **um caso** — por isso o chat de quem ja tem formulario vai **direto para o
mesmo analista** (afinidade), mesmo com o cap cheio. `Cap chat` e `Cap form` seguem como tetos
extras por tipo.

A contagem e o **cenario fisico** do operador: a decisao usa a **ultima leitura da aba Carga**, que varre
**todas as filas do time** (`analyst_load.casos_toa_keys`, deduplicado por **autor**) e **nao** o ledger do
bot. O numero do cap e, portanto, `fisico (TOA, por autor) + reservas em voo` — a mesma peca nao entra duas
vezes por ter duas chaves (`a:u:...` no ledger e `chat:<id>` na leitura). A leitura vale por
`2 x load_poll_sec` (minimo 60 s); se estiver velha ou com `toa_ok=0`, valem a fila do item, o ledger e as
reservas (fallback — nunca decide as cegas).

O cap vale para o **rodízio**: quem está no limite não recebe item novo — ele **espera na fila** (chat e
formulário na mesma linha) e vai sendo entregue conforme a carga dos analistas diminui. E a identidade do
caso **nunca** sai de nome genérico: formulário sem o address do técnico vira um caso **por item**
(`chat:<id>`), então a afinidade não junta solicitantes diferentes (ver "Abertura form-aware").

A carga soma tres fontes, deduplicadas por solicitante: `get_active_chats` (sessao) e
`get_helpdesk_chats in_process` (chats em atendimento **na fila**, controlado por
`route_capacity_source`), o ledger `route_assignments` (o que o bot entregou e ainda nao fechou) e as
reservas das entregas em voo. Sem resposta do TOA a carga **nao zera**: vale o ledger, e o evento
registra `carga_toa=indisponivel`.

### Carga por analista (aba Carga) e fechamento por AUSENCIA no TOA

A aba **Carga** mostra a **leitura das listas do TOA por analista** (`analyst_load`): chats e formularios
vistos no TOA, o que o ledger do bot conta, casos, reservas, formulario mais antigo (`form_ocioso_min`),
online, `toa_ok` e a hora da leitura. Uma linha por endereco, mais um historico curto
(`analyst_load_history`, podado por `analyst_load_keep`, padrao 200); a pagina se atualiza a cada 30 s e
tem o botao **Atualizar agora** (job `load_snapshot`), que forca a leitura na hora. O card de divergencia
aponta o analista com formulario no bot **ausente das listas** — candidato a fechamento por ausencia.

O TOA e a **fonte autoritativa**: o formulario resolvido **desaparece da lista** do front. Por isso um
formulario com caso aberto que fica **ausente de todas as listas** por `form_absent_readings` leituras
consecutivas **com leitura bem sucedida** (padrao 2) e dado como resolvido no TOA: o caso fecha com
`close_reason=form_resolvido_toa`, as rotas presas sao canceladas e a **vaga do analista volta na hora**.
Leitura falha (timeout) **nunca** fecha nada, e se o formulario reaparecer o contador zera (o contador
vive em memoria, entao o restart tambem zera).

A **mesma regua vale para o caso de CHAT** (`close_reason=chat_resolvido_toa`): quando o item sai das
listas do TOA por 2 leituras boas, o caso fecha e a vaga volta. `chats.status='closed'` **nao** serve de
sinal — e o que o bot grava ao entregar o chat e sair, com o caso ainda nas maos do analista. So vale para
chats cuja **fila foi lida** naquele ciclo (fila fora do time nao gera falso positivo). Sem isso o caso
ficava contando na carga ate o TTL de 12 h e a distribuicao travava com o time "cheio" (caso real: 15 de
15 casos de chat abertos com o chat ja encerrado).
**Na tela.** Os cards seguem a ordem **ChatBot — Em atendimento** (chats com o bot: status vivo e
`moved_at` nulo), **Fila de chat**, **Chat em atendimento**, **Fila de formulário**, **Formulário em
atendimento** e **Última leitura**. A tabela por analista mostra `TOA chat`/`TOA form`, **Casos** = físico
(`TOA chat + TOA form`, o que ele tem aberto no front) com o número de **solicitantes distintos** em cinza
ao lado (form + chat do mesmo autor = 1 caso, que é o número do cap), reservas, formulário mais antigo, TOA
e a hora da leitura — as antigas colunas "Bot chat"/"Bot form" saíram da visualização (o dado continua
alimentando o alerta de divergentes). Quem está **inativo no roteamento** aparece **no fim** da tabela, com
selo `inativo`, mesmo com carga maior. O **filtro de time (múltiplo)** vale para todos os cards, a tabela e
os alertas, e a escolha fica **gravada na sessão** (sobrevive ao auto-refresh de 30 s e à navegação).


