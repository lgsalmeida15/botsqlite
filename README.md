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
- **Analista entra sozinho no chat que esta na fila:** o bot detecta (nota `user_joined` de um membro do
  time **ou** alguem presente no payload do chat antes de falar de fila), registra o caso no ledger para
  ele (`identity_source=adocao`, evento **`chat_adopted`**), fecha a rota como `done` e **sai do chat** —
  antes ele seguia mandando "você avançou na fila" e podia entregar o mesmo chat para outro analista.

### Abertura form-aware (chat com formulário aberto) — **REMOVIDA em 28/09/2026**

O bot **não** verifica mais se o técnico tem formulário de validação em andamento: assumiu o
chat da fila, ele **sempre** recebe o menu da fila (`greeting` + `menu`).

Motivo: a correlação acertava pouco (`form_aware_skip` na maioria) e, quando errada, misturava
formulário de outro técnico no caso. Saíram junto: o status `awaiting_form_choice`, os textos
`form_pending`, `form_pending_wait` e `form_waiting` (apagados da tabela `templates` pela
migração), os eventos `form_aware_greeting`, `form_aware_choice`, `form_aware_wait`,
`form_aware_skip` e `case_linked`, e a rota presa ao dono (`pending_routes.expects_owner`, que
fica só como coluna legada).

**O que continua:** o formulário de validação segue roteado para o time da fila (fila habilitada
+ time ativo + `team_routing.form_enabled`), com afinidade pelo caso (`case_key`), peça irmã
(`case_sibling_delivered`) e fechamento por ausência no TOA / ociosidade. A coluna
`chats.linked_form_chat_id` também fica (legado, sem uso).
### Analista offline: o caso volta para a fila

Quando o operador que **esta com o caso** fica offline (leitura de `get_online_users` boa, `analyst_load.online_ok=1`,
`online=0` e carimbo `offline_since` mais antigo que **`offline_requeue_sec`** — 120 s por padrão), o robô devolve o
caso para a fila e re-roteia:

- encerra a rota e fecha o caso no ledger (`analista_offline`), liberando cap/reserva/**afinidade**;
- **chat**: avisa “Operador ficou offline, transferindo para outro operador” (template `requeue_notice`), volta o chat
  para `classified` e segue avisando a posição na fila;
- **formulário**: devolução silenciosa;
- a rota nova carrega `exclude_assignee` = o antigo dono (não volta para ele, nem com `require_online=false`);
- limite de **5 casos por tick**; o botão **Devolver à fila** (aba Carga) força na hora pelo mesmo caminho.

Liga/desliga e segundos ficam na aba **Roteamento** (`offline_requeue_enabled`, `offline_requeue_sec`) e, por time,
`offline_requeue_sec` na linha do time (**0 desliga**). A aba Carga mostra a coluna **Offline há** e o alerta
“analista(s) offline com caso aberto”. Eventos: `case_reclaimed`, `reclaim_invite_only`, `reclaim_no_queue`,
`reclaim_empty`.

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
(`chat:<id>`), então a afinidade não junta solicitantes diferentes .

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


