@echo off
REM ===========================================================================
REM  start-backend-service.cmd - launch the NEW (live-memory) backend for the
REM  WINDOWS SERVICE (WinSW).
REM
REM  WHY THIS FILE EXISTS AND WHY IT IS NOT start-backend-task.cmd
REM  The task launcher starts the backend DETACHED (a second cmd in the
REM  background) and then returns, so the scheduler's own repeat tick acts as
REM  the supervisor. A service must instead keep its child in the FOREGROUND:
REM  WinSW monitors that process, and if it exits the service is "stopped".
REM  So this launcher runs the backend in the foreground, like
REM  start-backend.cmd does, and the SCM's failure actions are the supervisor.
REM
REM  WHY USERPROFILE IS SET EXPLICITLY
REM  The service runs as LocalSystem. Without this, %USERPROFILE% would be
REM  C:\\Windows\\System32\\config\\systemprofile and:
REM    - launch_backend.py would look for the LAN settings in
REM      C:\\Windows\\System32\\config\\systemprofile\\.cow\\lan-access.cmd
REM      (not found -> the backend would silently fall back to loopback-only)
REM    - every "~" in the code would resolve to the systemprofile
REM  os.path.expanduser("~") on Windows follows USERPROFILE, so setting it
REM  makes "~" resolve to C:\\Users\\User exactly as it does in an interactive
REM  session. Verified: expanduser("~") -> C:\\Users\\User.
REM
REM  THE FOUR ENVIRONMENT DECISIONS
REM  Same as start-backend.cmd, but with ABSOLUTE paths instead of
REM  %USERPROFILE%-relative ones, because under LocalSystem %USERPROFILE%
REM  is not the user's profile.
REM
REM    AGENT_WORKSPACE = C:\\Users\\User\\cow
REM        The memory store. Must not be the source tree: config.py raises
REM        SourceTreeWorkspaceError, and state_dir.assert_not_source_tree()
REM        rejects the write of memory/long-term/index.db.
REM
REM    COW_WEB_PORT = 9902
REM        The old desktop backend owns 9876.
REM
REM    COW_DATA_DIR = C:\\Users\\User\\.cow
REM        config.json and run.log.
REM
REM    COW_DESKTOP is deliberately NOT set. Setting it to 1 turns on desktop
REM    mode (no browser pop-up, plugin denylist, tighter permission).
REM
REM  LAN ACCESS
REM  The bind address and the access password come from
REM  C:\\Users\\User\\.cow\\lan-access.cmd, read by launch_backend.py. They are
REM  read ONCE at startup, so a password change needs a service restart.
REM
REM  PID FILE
REM  app.py writes <repo>\\.cow.pid, and `cow stop` / `cow restart` kill
REM  whatever PID sits there. The development instance shares that file, so a
REM  `cow stop` aimed at the developer would kill the SERVICE. The pid
REM  switch is released once the port is bound (release-pid-switch.cmd).
REM ===========================================================================
setlocal

set "AGENT_WORKSPACE=C:\\Users\\User\\cow"
set "COW_WEB_PORT=9902"
set "COW_DESKTOP="
set "COW_DATA_DIR=C:\\Users\\User\\.cow"

REM Make "~" resolve to the interactive user's profile under LocalSystem.
REM This is what keeps launch_backend.py and every expanduser("~") in the
REM code pointing at the real workspace instead of the systemprofile.
set "USERPROFILE=C:\\Users\\User"
set "HOME=C:\\Users\\User"

REM APPDATA / LOCALAPPDATA ARE NOT REDUNDANT WITH USERPROFILE.
REM Python resolves the USER site-packages directory from %APPDATA%:
REM     %APPDATA%\\Python\\PythonXY\\site-packages
REM Under LocalSystem, %APPDATA% points at the systemprofile, so the
REM backend could not see yaml / requests / web (they are installed in
REM the interactive user's site-packages). The web channel then failed to
REM construct and the service came up RUNNING with NOTHING listening on
REM 9902 - a running service and a blank browser page at the same time.
REM Pinning APPDATA here is the second line of defence: the WinSW
REM <env> block pins it too, so neither path can lose it.
set "APPDATA=C:\\Users\\User\\AppData\\Roaming"
set "LOCALAPPDATA=C:\\Users\\User\\AppData\\Local"

set "PYTHON=C:\\Python315\\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

set "REPO=C:\\Workspace\\AI-Agent\\CowAgent"
set "LOGDIR=C:\\Users\\User\\cow\\backend-logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "PIDFILE=%REPO%\\.cow.pid"

REM --- guard: never start a second instance ---------------------------
REM If something already answers on 9902, a second copy would only fight
REM over the pid file and the database.
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":9902 .*LISTENING"') do (
    echo [skip] port 9902 already held by pid %%P - nothing to do.
    exit /b 0
)

echo [..] starting backend on port %COW_WEB_PORT% at %DATE% %TIME%
cd /d "%REPO%"

REM Run the backend in the FOREGROUND of this cmd process. WinSW monitors
REM this cmd, so the service's lifetime is the backend's lifetime.
"%PYTHON%" "%~dp0launch_backend.py"

REM The backend writes .cow.pid during load_config(), which is BEFORE the
REM socket is bound. app.py's own exit path normalises a zero exit code to
REM 1 (the SCM treats 0 as a clean stop and never applies the failure
REM actions), so a crash still restarts the service.
call "%~dp0release-pid-switch.cmd"
exit /b 1
