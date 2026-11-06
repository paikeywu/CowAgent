@echo off
REM ===========================================================================
REM  start-backend-task.cmd - launch the backend as a SCHEDULED TASK.
REM
REM  Why a separate file from start-backend.cmd: a scheduled task's
REM  "Task To Run" string has a length limit (260 chars) and quoting rules
REM  that make an inline command fragile. The task points at this file.
REM
REM  This is the entry point the scheduled task uses. It:
REM    1. refuses to start a SECOND instance if one is already listening
REM    2. sets the four environment decisions (see start-backend.cmd)
REM    3. runs the backend in the foreground of this cmd process
REM    4. releases the shared .cow.pid switch once the service is up
REM
REM  THE .cow.pid PROBLEM THIS STEP 4 SOLVES
REM
REM  app.py:_register_pid_file() writes <project_root>\\.cow.pid, and
REM  `cow stop` / `cow restart` / `cow update` kill whatever PID sits in
REM  there. Two instances in one source tree share one pid file, so a
REM  `cow stop` aimed at the development instance would kill the SERVICE
REM  instead - and the symptom would be "the service vanished for no
REM  reason", which is exactly the failure that sent us looking.
REM
REM  The service releases the switch as soon as it is up: after the port
REM  is bound, the pid file is deleted. From then on `cow stop` finds no
REM  pid, reports "not running", and touches nothing - the service keeps
REM  running. The development instance writes the file itself when it
REM  starts, so `cow stop` still works for it.
REM
REM  WHY THE ACCOUNT MATTERS
REM
REM  The task runs as the interactive user, so %USERPROFILE% and "~"
REM  resolve to C:\\Users\\User - which is where the memory store lives.
REM  Running as LocalSystem would resolve them to systemprofile and the
REM  backend would come up against an EMPTY workspace: healthy, serving,
REM  and remembering nothing. The same silent failure as the old reader.
REM ===========================================================================
setlocal

set "AGENT_WORKSPACE=%USERPROFILE%\\cow"
set "COW_WEB_PORT=9902"
set "COW_DESKTOP="
set "COW_DATA_DIR=%USERPROFILE%\\.cow"

REM --- LAN access (optional, see deploy\\lan-access\\README.md) ----------
REM The bind address and the access password are injected from
REM %USERPROFILE%\\.cow\\lan-access.env, NOT from the shared config.json.
REM
REM Why not config.json: the old desktop backend reads the SAME file. A
REM web_host/web_password written there would make the old backend bind
REM 0.0.0.0 and demand a login on its next restart. Keeping the LAN
REM settings in a separate file means only this service is affected.
REM
REM If the file is absent, the service stays on 127.0.0.1 (local only).

set "PYTHON=C:\\Python315\\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

set "REPO=C:\\Workspace\\AI-Agent\\CowAgent"
set "LOGDIR=%USERPROFILE%\\cow\\backend-logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "PIDFILE=%REPO%\\.cow.pid"

REM --- guard: never start a second instance ---------------------------
REM The port is the health check: if something already answers on 9902,
REM the backend is up and a second copy would only fight over the pid
REM file and the database.
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":9902 .*LISTENING"') do (
    echo [skip] port 9902 already held by pid %%P - nothing to do.
    exit /b 0
)

echo [..] starting backend on port %COW_WEB_PORT% at %DATE% %TIME%
echo [..] bind address: (see the log line "[WebChannel] ... Listening on")
cd /d "%REPO%"

REM Run the backend in a second cmd so this script can keep going and
REM release the pid file as soon as the port is bound.
start "CowAgentBackend" /min cmd /c ""%PYTHON%" "%~dp0launch_backend.py" >> "%LOGDIR%\\backend.out.log" 2>&1"

REM --- wait for the port, then release the shared pid switch ----------
REM Bounded: if the backend fails to come up we give up rather than
REM hang forever, and the scheduler's next tick retries.
REM
REM The release must happen AFTER the backend has written its own pid
REM file, otherwise the backend writes it again a moment later and the
REM switch is back - measured: deleting at bind time left .cow.pid
NAMED the service again. app.py writes the file during load_config(),
which is BEFORE the socket is bound, so by the time the port answers the
REM file is already there and can be removed for good.
set /a waited=0
:waitport
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":9902 .*LISTENING"') do (
    echo [ok] backend is listening on 9902 ^(pid %%P^)
    goto :release
)
timeout /t 2 /nobreak >nul
set /a waited+=2
if !waited! GEQ 90 (
    echo [FAIL] backend did not bind 9902 within 90s - leaving the log for inspection.
    exit /b 1
)
goto :waitport

:release
REM The backend owns <repo>\\.cow.pid for as long as it runs. Release it
REM so that `cow stop` in the development tree cannot kill the service.
REM The release is a separate, idempotent step: it decides by comparing
REM the pid in the file with the pid that owns the port, so it is correct
REM no matter which of the two wrote the file and when.
call "%~dp0release-pid-switch.cmd"

REM The backend runs in the child cmd started above. This script's job
REM is done; the scheduler's next tick is the supervisor.
exit /b 0
