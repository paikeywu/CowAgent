@echo off
REM ---------------------------------------------------------------------------
REM Start the BGE Reranker v2-m3 HTTP service (Windows, no Docker needed).
REM
REM The service must be running BEFORE CowAgent starts a session that uses
REM rerank_provider=http. If it is down, CowAgent degrades to un-reranked
REM order instead of failing, but you lose the reranking.
REM
REM Usage:
REM   start-reranker.bat                  detached (interactive / Startup)
REM   start-reranker.bat --foreground      run in THIS console, in the
REM                                        foreground, so a supervising
REM                                        process (the Windows service
REM                                        wrapper) can track the real
REM                                        lifetime of the server.
REM   start-reranker.bat --port 9899      override the configured port
REM                                        (used by the verification script)
REM
REM THIS FILE IS THE ONE PLACE THAT KNOWS HOW TO LAUNCH THE SERVER.
REM The Windows service runs this same file, so a manual start and a
REM service start execute identical logic.
REM
REM The parameters (model dir, port, threads) live in reranker.conf, the
REM single source of truth, and are read by reranker_config.py -- not
REM parsed here. Parsing INI inside cmd is where quoting bugs live; this
REM file only needs to know how to find itself and how to find the
REM interpreter.
REM ---------------------------------------------------------------------------
setlocal EnableExtensions EnableDelayedExpansion

REM ---------------------------------------------------------------------------
REM Locate ourselves, so the script works from any working directory (a
REM Windows service starts in C:\Windows\System32 by default).
REM ---------------------------------------------------------------------------
set "RERANKER_DIR=%~dp0"
if "%RERANKER_DIR:~-1%"=="\" set "RERANKER_DIR=%RERANKER_DIR:~0,-1%"

set "VENV_PY=C:\Users\User\cow\reranker-venv\Scripts\python.exe"
set "CONF=%RERANKER_DIR%\reranker.conf"
set "FOREGROUND="
set "PORT_OVERRIDE="
set "LOG=C:\Users\User\cow\reranker-server.log"

REM ---------------------------------------------------------------------------
REM Argument parsing. Only the launch MODE and an optional port override
REM are decided here; everything else comes from the config file.
REM ---------------------------------------------------------------------------
:parse_args
if "%~1"=="" goto after_args
if /I "%~1"=="--foreground" (
    set "FOREGROUND=1"
    shift
    goto parse_args
)
if /I "%~1"=="--port" (
    set "PORT_OVERRIDE=%~2"
    shift
    shift
    goto parse_args
)
echo [ERROR] unknown argument: %~1
echo         usage: %~nx0 [--foreground] [--port N]
exit /b 2

:after_args

REM ---------------------------------------------------------------------------
REM Preconditions. Fail loudly and early rather than starting a server that
REM looks healthy but fails on every request.
REM ---------------------------------------------------------------------------
if not exist "%RERANKER_DIR%\serve.py" (
    echo [ERROR] serve.py not found next to this script: %RERANKER_DIR%
    exit /b 1
)

if not exist "%VENV_PY%" (
    echo [ERROR] venv python not found: %VENV_PY%
    echo         Create it first:  uv venv --python 3.12 C:\Users\User\cow\reranker-venv
    exit /b 1
)

if not exist "%CONF%" (
    echo [ERROR] config file not found: %CONF%
    echo         The service parameters live in that file; without it the
    echo         server would fall back to a /models/... path that does not
    echo         exist on Windows, and every request would fail.
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM Read the parameters from the config file. The Python helper prints
REM "key=value" lines; this loop turns them into %RERANKER_KEY% variables.
REM Doing the parsing in Python avoids all INI-in-cmd quoting hazards.
REM
REM The helper output goes to a TEMP file and is parsed from there
REM instead of being captured in a for /f backtick. Capturing it
REM directly does not work: cmd then treats the whole quoted string as a
REM single literal path and fails with "The filename, directory name, or
REM volume label syntax is incorrect" -- and the real cause (a config
REM error) is nowhere in that message. The file form also survives
REM paths that contain spaces.
REM ---------------------------------------------------------------------------
set "CFG_TMP=%TEMP%\reranker-config-%RANDOM%.txt"
set "CFG_ERR=%TEMP%\reranker-config-%RANDOM%.err"

"%VENV_PY%" "%RERANKER_DIR%\reranker_config.py" --config "%CONF%" > "%CFG_TMP%" 2> "%CFG_ERR%"
if errorlevel 1 (
    echo [ERROR] could not read the config file: %CONF%
    if exist "%CFG_ERR%" type "%CFG_ERR%"
    del "%CFG_TMP%" >nul 2>&1
    del "%CFG_ERR%" >nul 2>&1
    exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%A in ("%CFG_TMP%") do set "RERANKER_%%A=%%B"
del "%CFG_TMP%" >nul 2>&1
del "%CFG_ERR%" >nul 2>&1

if not defined RERANKER_PORT (
    echo [ERROR] could not read 'port' from %CONF%
    exit /b 1
)
if not defined RERANKER_MODEL_DIR (
    echo [ERROR] could not read 'model_dir' from %CONF%
    exit /b 1
)

if defined PORT_OVERRIDE (
    set "RERANKER_PORT=%PORT_OVERRIDE%"
)

if not exist "%RERANKER_MODEL_DIR%" (
    echo [ERROR] model directory not found: %RERANKER_MODEL_DIR%
    exit /b 1
)

set "PORT=%RERANKER_PORT%"

REM ---------------------------------------------------------------------------
REM FOREGROUND MODE: run the server in THIS process and stay here.
REM
REM Two things are deliberately different from the detached mode:
REM
REM  1. No "already running, exit 0" shortcut. In the foreground the
REM     lifetime of this process IS the lifetime of the service. Exiting 0
REM     right away would make the Windows service report itself as stopped
REM     while the server is still running. If the port is taken, uvicorn
REM     fails to bind and exits non-zero, which the SCM sees as a genuine
REM     failure and retries with the configured backoff.
REM
REM  2. No health polling. Polling is for the detached mode, where
REM     somebody has to decide whether the launch succeeded. Here the
REM     supervising process does that.
REM ---------------------------------------------------------------------------
if "%FOREGROUND%"=="1" (
    "%VENV_PY%" "%RERANKER_DIR%\serve.py" --config "%CONF%" --host 127.0.0.1 --port %PORT%
    exit /b %ERRORLEVEL%
)

REM ---------------------------------------------------------------------------
REM Detached mode (interactive / Startup folder).
REM
REM Already up? The health check requires status=="ok"; /health answers 200
REM even when the model failed to load, so checking the status code alone
REM would report a broken service as healthy.
REM ---------------------------------------------------------------------------
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%PORT%/health' -TimeoutSec 3; if ($r.Content -match '\"status\"\s*:\s*\"ok\"') { exit 0 } else { exit 1 } } catch { exit 1 }"
if %ERRORLEVEL% equ 0 (
    echo [OK] reranker already listening on 127.0.0.1:%PORT%
    exit /b 0
)

echo [..] starting reranker service on 127.0.0.1:%PORT%

REM The server reads its own parameters from the config file, so the
REM command line stays short and no path with spaces is embedded in
REM nested cmd quoting.
start "cow-reranker" /min cmd /c ""%VENV_PY%" "%RERANKER_DIR%\serve.py" --config "%CONF%" --host 127.0.0.1 --port %PORT% > "%LOG%" 2>&1"

REM Wait for the model to finish loading (weights are ~2.3GB, first load
REM is slow). ping -n 2 sleeps ~2s without needing an interactive stdin
REM (timeout /t fails when stdin is redirected, e.g. when invoked from a
REM scheduler or a script).
set /a tries=0
:wait
set /a tries+=1
ping -n 2 127.0.0.1 >nul
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%PORT%/health' -TimeoutSec 3; if ($r.Content -match '\"status\"\s*:\s*\"ok\"') { exit 0 } else { exit 1 } } catch { exit 1 }"
if %ERRORLEVEL% equ 0 (
    echo [OK] reranker is up:  http://127.0.0.1:%PORT%
    exit /b 0
)
if %tries% lss 60 (
    goto wait
)

echo [ERROR] reranker did not become healthy in time. See %LOG%
exit /b 1
 
