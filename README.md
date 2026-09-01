# HELPTOA Bot (SQLite)

Daemon Playwright + painel PHP no **mesmo servidor**, com banco **SQLite** local. Sem PostgreSQL e sem CDN.

Este diretório é um subprojeto autônomo (pode ir para um repositório separado).

## Requisitos

- Python 3.10+ com `pip` (Playwright + Chromium)
- PHP 7.4.3+ com extensão `pdo_sqlite`
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
| `BOT_API_TOKEN` | se usar a API | Header `X-Bot-Token` em `/api/v1/classifications` |

O arquivo SQLite é criado na primeira conexão (bot ou painel). Banco começa vazio, com templates e ajustes padrão.

Crie o usuário do painel:

```bash
php bin/create-user.php admin senha-aqui
```

## Painel PHP

Document root: pasta `php/`.

O layout usa Bootstrap 5.3.3 em `php/assets/` (sem internet). A conta do PHP e a do serviço Python precisam gravar em `data/` (incluindo `*.sqlite-wal` e `*.sqlite-shm`).

## Bot

Na raiz deste diretório:

```bash
.venv\Scripts\python run.py
```

Na primeira vez o Chromium abre: complete o login/SSO do TOA. O perfil fica em `.chrome-profile/`.

Habilite **uma fila** no painel e cadastre opções do menu antes de testar.

O banco guarda uma linha por `chat_id` (status, classificação, retries, timestamps). Sem texto de conversa.

## O que não vai para o Git

`.env`, `.venv/`, `.chrome-profile/`, `data/*.sqlite*`, `.bot.lock`.
