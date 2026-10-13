@echo off
REM ============================================================================
REM  Watchdog de rotas orfas - EXECUCAO UNICA (para Agendador de Tarefas).
REM
REM  Encerra as rotas `pending_routes.waiting` (kind=chat) cujo chat ja saiu do
REM  bot / sumiu do TOA, e SAI. Ideal para agendar (ex.: a cada 15 min).
REM
REM  Uso manual:
REM     watchdog_rotas_orfas_once.bat                  -> usa o .env da raiz
REM     watchdog_rotas_orfas_once.bat .env.prod-x      -> usa outro ambiente
REM
REM  Saida: rotas_orfas_fechadas.jsonl e watchdog_rotas_orfas.log (na raiz).
REM  Codigo de saida = o do watchdog (0 = ok). O Agendador registra como resultado.
REM ============================================================================
setlocal
chcp 65001 >nul

set "ROOT=%~dp0.."
set "ENV_FILE=%~1"
if "%ENV_FILE%"=="" set "ENV_FILE=.env"

set "PY=%PYTHON%"
if "%PY%"=="" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "LOG=%~dp0..\watchdog_rotas_orfas.log"
set "REPORT=%~dp0..\rotas_orfas_fechadas.jsonl"

cd /d "%ROOT%"
echo [%date% %time%] once: iniciando (env=%ENV_FILE%) >> "%LOG%"
"%PY%" "%~dp0watchdog_rotas_orfas.py" --env "%ENV_FILE%" --apply --report "%REPORT%" >> "%LOG%" 2>&1
set "CODE=%errorlevel%"
echo [%date% %time%] once: fim (codigo %CODE%) >> "%LOG%"
endlocal & exit /b %CODE%
