@echo off
REM ============================================================================
REM  Watchdog de rotas orfas - mantem o roteamento de chats fluindo.
REM
REM  Encerra as rotas `pending_routes.waiting` (kind=chat) cujo chat ja saiu do
REM  bot / sumiu do TOA. Sem isso elas giram para sempre em `move_chat` (e=10),
REM  entopem a fila e martelam o TOA. Veja bin\watchdog_rotas_orfas.py.
REM
REM  Uso:  watchdog_rotas_orfas.bat                 -> usa o .env da raiz
REM        watchdog_rotas_orfas.bat .env.prod-x     -> usa outro arquivo de ambiente
REM        watchdog_rotas_orfas.bat .env 30         -> intervalo de 30s
REM
REM  Para parar: Ctrl+C (o watchdog encerra limpo).
REM  Saida: watchdog_rotas_orfas.log e rotas_orfas_fechadas.jsonl (na raiz).
REM ============================================================================
setlocal
chcp 65001 >nul

set "ROOT=%~dp0.."
set "ENV_FILE=%~1"
if "%ENV_FILE%"=="" set "ENV_FILE=.env"
set "INTERVAL=%~2"
if "%INTERVAL%"=="" set "INTERVAL=60"

set "PY=%PYTHON%"
if "%PY%"=="" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "LOG=%~dp0..\watchdog_rotas_orfas.log"
set "REPORT=%~dp0..\rotas_orfas_fechadas.jsonl"

cd /d "%ROOT%"
echo ============================================================ >> "%LOG%"
echo [%date% %time%] watchdog rotas orfas: iniciando (env=%ENV_FILE% interval=%INTERVAL%s) >> "%LOG%"
echo watchdog rotas orfas iniciado (env=%ENV_FILE%, intervalo %INTERVAL%s). Ctrl+C para parar.
echo Log:    %LOG%
echo Relatorio: %REPORT%

:loop
"%PY%" "%~dp0watchdog_rotas_orfas.py" --env "%ENV_FILE%" --apply --watch --interval %INTERVAL% --report "%REPORT%" >> "%LOG%" 2>&1
set "CODE=%errorlevel%"
if "%CODE%"=="0" goto fim
echo [%date% %time%] watchdog saiu com codigo %CODE%; reiniciando em 15s >> "%LOG%"
echo watchdog saiu com codigo %CODE%; reiniciando em 15s (Ctrl+C para parar)
timeout /t 15 /nobreak >nul
goto loop

:fim
echo [%date% %time%] watchdog encerrado. >> "%LOG%"
echo watchdog encerrado.
endlocal
