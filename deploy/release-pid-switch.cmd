@echo off
REM ===========================================================================
REM  release-pid-switch.cmd - remove the shared .cow.pid switch.
REM
REM  THE PROBLEM
REM
REM  app.py:_register_pid_file() writes <project_root>\\.cow.pid, and
REM  `cow stop` / `cow restart` / `cow update` kill whatever PID sits in
REM  there. Two instances in one source tree share one pid file, so a
REM  `cow stop` aimed at the development instance would kill the SERVICE
REM  instead - and the symptom would be "the service vanished for no
REM  reason", which is exactly the failure that sent us looking.
REM
REM  WHY A SEPARATE STEP AND NOT "delete it in the launcher"
REM
REM  The backend writes the pid file during load_config(), which happens
REM  BEFORE the socket is bound. A launcher that waits for the port and
REM  then deletes the file is racing the backend: measured twice, the
REM  file was back a moment later. The order is not observable from
REM  outside, so the deletion has to be independent of it.
REM
REM  THIS STEP IS IDEMPOTENT AND REPEATABLE
REM
REM  It only removes the file when it names a process that is NOT the
REM  service. The service's PID is discovered from the port it owns, so
REM  the check is "is the pid in the file the pid listening on 9902?".
REM  If it is, the file is removed - the backend does not rewrite it
REM  (it writes once, at startup). If it is not (the development
REM  instance wrote it), the file is left alone, so `cow stop` keeps
REM  working for the developer.
REM
REM  RUN BY: deploy\\start-backend-task.cmd, and by the scheduled task's
REM  own repeated ticks, so the switch stays released even if something
REM  writes it again later.
REM ===========================================================================
setlocal

set "PIDFILE=%~dp0..\\.cow.pid"
set "PORT=9902"

if not exist "%PIDFILE%" (
    echo [pid] no pid file - nothing to release.
    exit /b 0
)

REM --- who owns the port? -------------------------------------------
set "SERVICEPID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    set "SERVICEPID=%%P"
    goto :havepid
)
:havepid
if not defined SERVICEPID (
    echo [pid] port %PORT% has no listener - leaving the pid file alone.
    exit /b 0
)

REM --- is the file naming the service? --------------------------------
set "FILEPID="
for /f "usebackq delims=" %%P in ("%PIDFILE%") do set "FILEPID=%%P"

if not defined FILEPID (
    echo [pid] pid file is empty - removing it.
    del /q "%PIDFILE%" >nul 2>&1
    exit /b 0
)

if "%FILEPID%"=="%SERVICEPID%" (
    echo [pid] pid file names the service ^(%%FILEPID%^) - releasing it.
    del /q "%PIDFILE%" >nul 2>&1
) else (
    echo [pid] pid file names %%FILEPID%, service is %%SERVICEPID%^ - leaving it alone.
)

exit /b 0
