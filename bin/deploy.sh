#!/usr/bin/env bash
# Setup do painel PHP + (re)start do bot Python após deploy.
# Não instala nada — assume dependências (php-sqlite3, venv, playwright) já presentes no servidor.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$PROJECT_DIR/.venv/bin/python"
BOT_LOCK="/var/lock/botsqlite_bot.lock"
LOG_FILE="$PROJECT_DIR/data/bot.log"
WEB_GROUP="www-data"

echo "== [1/4] Git: atualizando a partir da main =="
cd "$PROJECT_DIR"
git fetch origin
git checkout main
git pull --ff-only origin main

echo "== [2/4] PHP: permissões =="
sudo chgrp "$WEB_GROUP" "$PROJECT_DIR" "$PROJECT_DIR/php"
sudo chmod 750 "$PROJECT_DIR" "$PROJECT_DIR/php"
sudo chgrp "$WEB_GROUP" "$PROJECT_DIR/schema.sql" "$PROJECT_DIR/.env"
sudo chmod 640 "$PROJECT_DIR/schema.sql" "$PROJECT_DIR/.env"
sudo mkdir -p "$PROJECT_DIR/data"
sudo chgrp -R "$WEB_GROUP" "$PROJECT_DIR/data"
sudo chmod -R 770 "$PROJECT_DIR/data"

echo "== [3/4] PHP: reload apache =="
sudo apache2ctl configtest
sudo systemctl reload apache2

echo "== [4/4] Python: reiniciando bot =="
if pgrep -f "$PROJECT_DIR/run.py" >/dev/null 2>&1; then
    echo "Parando instância anterior (PID $(pgrep -f "$PROJECT_DIR/run.py" | tr '\n' ' '))..."
    pkill -TERM -f "$PROJECT_DIR/run.py" || true
    for _ in $(seq 1 20); do
        pgrep -f "$PROJECT_DIR/run.py" >/dev/null 2>&1 || break
        sleep 1
    done
    if pgrep -f "$PROJECT_DIR/run.py" >/dev/null 2>&1; then
        echo "Não encerrou a tempo, forçando kill -9"
        pkill -KILL -f "$PROJECT_DIR/run.py" || true
    fi
else
    echo "Nenhuma instância anterior rodando."
fi

echo "Subindo bot em background..."
nohup flock -n "$BOT_LOCK" timeout -k 110s 6h "$VENV_PY" "$PROJECT_DIR/run.py" >> "$LOG_FILE" 2>&1 &
disown

sleep 2
if pgrep -f "$PROJECT_DIR/run.py" > /dev/null; then
    echo "Bot rodando (PID $(pgrep -f "$PROJECT_DIR/run.py"))."
else
    echo "AVISO: bot não apareceu no processo — confira $LOG_FILE"
fi

echo "== Deploy concluído =="
