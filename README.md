<p align="center"><img src="https://github.com/user-attachments/assets/eca9a9ec-8534-4615-9e0f-96c5ac1d10a3" alt="CowAgent" width="420" /></p>

<p align="center">
  <a href="https://github.com/zhayujie/CowAgent/releases/latest"><img src="https://img.shields.io/github/v/release/zhayujie/CowAgent?cacheSeconds=3600" alt="Latest release"></a>
  <a href="https://github.com/zhayujie/CowAgent/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"></a>
  <a href="https://github.com/zhayujie/CowAgent"><img src="https://img.shields.io/github/stars/zhayujie/CowAgent?style=flat-square&cacheSeconds=3600" alt="Stars"></a>
  <a href="https://docs.cowagent.ai/"><img src="https://img.shields.io/badge/Docs-cowagent.ai-blue?style=flat&logo=readthedocs&logoColor=white" alt="Docs"></a>
  <a href="https://cdn.link-ai.tech/code/cow/cowagent-wechat-group.png"><img src="https://img.shields.io/badge/WeChat-Group-07C160?style=flat&logo=wechat&logoColor=white" alt="WeChat Group"></a>
  <a href="https://discord.gg/9U8eA8v9TR"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat&logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <a href="https://trendshift.io/repositories/25763" target="_blank"><img src="https://trendshift.io/api/badge/repositories/25763" alt="zhayujie%2FCowAgent | Trendshift" style="width: 250px; height: 55px;" width="250" height="55"/></a>
</p>

<p align="center">
  [English] | [<a href="docs/zh/README.md">中文</a>] | [<a href="docs/zh/README-Hant.md">繁體中文</a>] | [<a href="docs/ja/README.md">日本語</a>]
</p>

**CowAgent** is an open-source super AI assistant that proactively plans tasks, controls your computer and external services, creates and runs Skills, builds a personal knowledge base and long-term memory, forms multi-agent teams, and grows alongside you through self-evolution — a reference implementation of Agent Harness engineering.

CowAgent is lightweight, easy to deploy, and built to extend. Plug in any major LLM provider and run it 24/7 on a personal computer or server, across the web and all major IM platforms.

<p align="center">
  <a href="https://cowagent.ai/">🌐 Website</a> &nbsp;·&nbsp;
  <a href="https://docs.cowagent.ai/intro/index">📖 Docs</a> &nbsp;·&nbsp;
  <a href="https://docs.cowagent.ai/guide/quick-start">🚀 Quick Start</a> &nbsp;·&nbsp;
  <a href="https://skills.cowagent.ai/">🧩 Skill Hub</a> &nbsp;·&nbsp;
  <a href="https://cowagent.ai/download/">💻 Download</a> &nbsp;·&nbsp;
  <a href="https://link-ai.tech/cowagent/create?utm_source=cowagent&utm_medium=readme&utm_campaign=nav">☁️ Try Online</a>
</p>

<br/>

## 🎬 Demo

<p align="center">
  <video src="https://github.com/user-attachments/assets/8625a19f-615c-4343-8be8-3707ce4d4d4e" controls muted playsinline width="720">
    Your browser can't play this video.
    <a href="https://cowagent.ai/">Watch the demo on our website →</a>
  </video>
</p>

<br/>

## 🌟 Highlights

| Capability | Description |
| :--- | :--- |
| [Planning](https://docs.cowagent.ai/intro/architecture) | Decomposes complex tasks and executes them step by step, looping over tools until the goal is reached |
| [Multi-Agent](https://docs.cowagent.ai/multi-agent/team) | Build a team of Agents, each with its own role, model, skills, and knowledge, collaborating within a shared conversation |
| [Memory](https://docs.cowagent.ai/memory/index) | Three-tier architecture (context → daily → core), automatic Deep Dream distillation, hybrid keyword + vector retrieval |
| [Knowledge](https://docs.cowagent.ai/knowledge/index) | Auto-curates structured knowledge into a Markdown wiki, builds an evolving knowledge graph with visual browsing |
| [Evolution](https://docs.cowagent.ai/memory/self-evolution) | Self-Evolution reviews conversations automatically to improve skills, follow up on unfinished tasks, and consolidate memory and knowledge, growing through everyday use |
| [Skills](https://docs.cowagent.ai/skills/index) | One-click install from [Skill Hub](https://skills.cowagent.ai/), GitHub, ClawHub; or create custom skills via natural-language conversation |
| [Tools](https://docs.cowagent.ai/tools/index) | Built-in file I/O, terminal, browser, scheduler, memory retrieval, web search, and 10+ more tools — with native MCP integration |
| [Channels](https://docs.cowagent.ai/channels/index) | Integrates with Web, WeChat, Feishu, DingTalk, WeCom, QQ, Official Accounts, Telegram, and Slack |
| Multimodal | First-class support for text, images, voice, and files — recognition, generation, and delivery |
| [Models](https://docs.cowagent.ai/models/index) | Claude, GPT, Gemini, DeepSeek, Qwen, GLM, Kimi, MiniMax, Doubao, and more — swap providers from the Web console with one click |
| [Deploy](https://docs.cowagent.ai/guide/quick-start) | One-line installer, unified Web console, multiple deployment modes (local, Docker, server) |

<br/>

## 🏗️ Architecture

<img src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/architecture/en/architecture.jpg" alt="CowAgent Architecture" width="750"/>

CowAgent is a complete **Agent Harness**: messages flow in through **Channels**; the **Agent Core** plans and reasons over memory, knowledge, and the available tools and skills; **Models** generate the response, which is sent back through the originating channel. Every layer is decoupled and independently extensible.

Read more in [Architecture](https://docs.cowagent.ai/intro/architecture).

<br/>

## 🚀 Quick Start

A one-line installer takes care of dependencies, configuration, and startup:

**Linux / macOS:**

```bash
bash <(curl -fsSL https://cdn.link-ai.tech/code/cow/run.sh)
```

**Windows (PowerShell):**

```powershell
irm https://cdn.link-ai.tech/code/cow/run.ps1 | iex
```

**Docker:**

```bash
curl -O https://cdn.link-ai.tech/code/cow/docker-compose.yml
docker compose up -d
```

Once started, open `http://localhost:9899` to access the **Web console** — your one-stop hub to chat with the Agent, configure models, connect channels, and install skills.

> Deploying on a server? Set `web_host` to `0.0.0.0` in `config.json` to make the console reachable from outside, and set `web_password` to protect it. Don't forget to open port `9899` in your firewall or security group.

> 📖 Detailed guides: [Quick Start](https://docs.cowagent.ai/guide/quick-start) · [Install from Source](https://docs.cowagent.ai/guide/manual-install) · [Upgrade](https://docs.cowagent.ai/guide/upgrade)

After installation, manage the service with the [cow CLI](https://docs.cowagent.ai/cli/index):

```text
cow start | stop | restart        # service control
cow status | logs                  # status and logs
cow update                         # pull latest code and restart
cow skill install <name>           # install a skill
cow install-browser                # install browser automation
```

> 💻 Desktop client: download the **[CowAgent Desktop client](https://cowagent.ai/download/)** (macOS / Windows) — the backend is bundled, ready to use out of the box.

<br/>

## 🖥️ Deploy on an Ubuntu Server

Run CowAgent from source on a headless Ubuntu host. The steps below were run on
an Ubuntu 26.04 LTS server (Python 3.14.4). Copy the blocks in order; each one
is complete and copy-pasteable on its own.

Two ways to deploy are described here: the manual steps (1-6), which run the
console under `nohup`, and the deployment script, which installs a `systemd`
service and verifies the result. Step 5 compares the two.

### Prerequisites

| Requirement | Value | Why |
| :--- | :--- | :--- |
| OS | Ubuntu 26.04 LTS | step 1 installs packages with `apt-get` |
| Python | 3.14 (`python3 -V`) | `python3 -m venv` gives an isolated interpreter |
| Disk | ~2 GB free | ~390 MB virtualenv + ~40 MB checkout |
| Package index | `https://pypi.tuna.tsinghua.edu.cn/simple` | the official PyPI can be unusably slow from some networks |
| Outbound HTTPS | `github.com`, `pypi.tuna.tsinghua.edu.cn` | clone + dependencies |
| Open port | `9899/tcp` | the Web console |

> The model key never goes into `config.json`. Keep it in the `.env` file
> described in step 4, with mode `600`. The console password does live in
> `config.json`, so that file stays mode `600` as well.

`curl` and `ss` are on the package list because the checks further down need
them: `curl` sends every request to the console, and `ss` reports which
process holds the port. On a minimal Ubuntu image `curl` is often absent and
`ss` arrives with `iproute2`.

### 1. Get the code

```bash
sudo apt-get update && sudo apt-get install -y git curl python3-venv lsof iproute2
git clone --branch live-memory https://github.com/paikeywu/CowAgent.git ~/CowAgent
cd ~/CowAgent
git rev-parse --abbrev-ref HEAD     # must print: live-memory
git rev-parse HEAD                  # record this -- it is the commit you deployed
```

`git clone` on its own checks out the default branch (`master`), which is
**not** the branch these steps were written against — `--branch live-memory` is
what puts the deployed code on disk. The deployment was verified against commit
`5df9d27052996923fa05d1cd3c4c6c98ec1bc297`; the hash printed above is whatever
`live-memory` points at right now. Several commits landed on this branch after the
verified one, and one of them (`025e80af`) changes `config.py` — it reworks
credential masking in the startup log. List them and read the diff before
trusting the steps:

```bash
git log --oneline 5df9d27052996923fa05d1cd3c4c6c98ec1bc297..HEAD
git diff 5df9d27052996923fa05d1cd3c4c6c98ec1bc297..HEAD -- config.py
```

```bash
git diff 5df9d27052996923fa05d1cd3c4c6c98ec1bc297..HEAD -- config.py
```

The commands in the steps below are unchanged by those commits, but the
password-masking behaviour described in step 4 is.

`python3-venv` is in that package list because Ubuntu ships the system
interpreter without it. Without the package, `python3 -m venv` in step 2 fails
with `ensurepip is not available`. `lsof` is what the port check in the
troubleshooting section uses.

### 2. Create a virtualenv and install the dependencies

```bash
cd ~/CowAgent
python3 -m venv .venv
.venv/bin/python -V                                   # Python 3.14.x
.venv/bin/python -m pip install --upgrade pip setuptools wheel \
  -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv/bin/python -m pip install -r requirements.txt \
  -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv/bin/python -m pip install -e . \
  -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv/bin/python -m pip check                         # No broken requirements found.
.venv/bin/python -m pip install pytest \
  -i https://pypi.tuna.tsinghua.edu.cn/simple        # not in requirements.txt; step 6 needs it
```

**Use the virtualenv, not the system Python.** On a current Ubuntu the system
interpreter is marked externally managed (PEP 668), so `pip install` into it
fails with `error: externally-managed-environment` and tells you to pass
`--break-system-packages`. That flag bypasses the operating system's protection
of its own packages and can break the system Python — do not use it. The
virtualenv is the isolation mechanism, and it needs no extra package.

`pip install -e .` installs this repository as an editable package. That is
what puts the `cow` command on your PATH (`cow start`, `cow status`,
`cow logs`), which the Quick Start section above refers to. It creates a
`cowagent.egg-info/` directory in the checkout; `.gitignore` already excludes
it, so the working tree stays clean.

### 3. Point the runtime state outside the source tree

```bash
mkdir -p ~/cow-workspace
```

`agent_workspace` must point at a directory that is **not** inside the git
checkout. The runtime state (the memory index, scheduler store, skills) belongs
to whoever runs the app, not to the repository — a clone must never hand
someone else's `memory/long-term/index.db` to a new user.

This is enforced at startup. With `agent_workspace` pointing inside the
checkout, the process refuses to start with:

```
common.state_dir.SourceTreeWorkspaceError: 工作区位于源码树内，拒绝把运行时状态写进源码树：'/home/<user>/CowAgent' 在 '/home/<user>/CowAgent' 之内。
  运行时状态（memory/long-term/index.db 等）属于运行它的人，不属于仓库；它一旦进入版本库，clone 的人会拿到别人的记忆索引。
  请把 agent_workspace / workspace 指向一个工作区目录（例如 %USERPROFILE%\cow），而不是源码目录。
```

The check walks up from the workspace looking for a `.git` ancestor, so a
workspace anywhere under a checkout is rejected. Point it at `~/cow-workspace`
(or any directory outside the tree) and the app boots.

### 4. Write `config.json` and the credentials file

`config.json` lives next to the checkout (`~/CowAgent/config.json`) for source
deployments. `COW_DATA_DIR` overrides it if you keep user data elsewhere.

That path is not a convention — it is what the code does. `config.py` builds the
path as `os.path.join(get_data_root(), "config.json")`, and `get_data_root()`
falls back to the repository root when `COW_DATA_DIR` is unset. The desktop
client sets `COW_DATA_DIR=~/.cow`, so **`~/.cow/config.json` is the desktop
client's file and a source deployment never reads it**. Put the source
deployment's `config.json` in the checkout root.

One command writes the whole file and prints the console password, so you have
it in front of you:

```bash
cd ~/CowAgent
python3 - <<'PY'
import json, os, secrets

# Set your own password here if you prefer one you can type from memory.
# Leave it empty and one is generated for you.
CHOOSE_YOUR_PASSWORD = ""

path = os.path.expanduser("~/CowAgent/config.json")
pwd = CHOOSE_YOUR_PASSWORD or secrets.token_urlsafe(24)
cfg = {
    "cow_lang": "en",
    "channel_type": "web",
    "model": "MiniMax-M3",
    "bot_type": "minimax",
    "minimax_api_base": "https://api.minimaxi.com/v1",
    "minimax_api_key": "",
    "web_host": "0.0.0.0",
    "web_port": 9899,
    "web_password": pwd,
    "agent": True,
    "agent_workspace": os.path.expanduser("~/cow-workspace"),
}
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod(path, 0o600)
print("console password:", pwd)
PY
```

Write that password down, or read it back later — `config.json` is mode `600`,
so only your own user can see it:

```bash
python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/CowAgent/config.json')))['web_password'])"
```

**One thing can still override that password.** At startup `config.py` walks
every environment variable, lowercases the name, and any name that matches a
key in its `available_setting` table replaces the value from `config.json`.
`web_password`, `web_port`, `agent_workspace`, `model`, `bot_type` and
`cow_lang` are all in that table — so if the server already exports
`WEB_PASSWORD`, the console asks for *that* password and not the one in
`config.json`. Check before you hand the password out:

```bash
env | grep -E '^(WEB_PASSWORD|WEB_PORT|WEB_HOST|AGENT_WORKSPACE|MODEL|BOT_TYPE|COW_LANG)='
```

`web_host` is in that table as well, so `WEB_HOST` overrides the bind address
in `config.json`. If it is set to `127.0.0.1` the console listens on the local
interface only — the deployment looks healthy but the console is unreachable
from another machine.

> The MiniMax endpoint is `api.minimaxi.com`. `api.minimax.com` does not
> resolve.

The model key goes into an environment file, not into `config.json`:

```bash
mkdir -p ~/.cow ~/cow-workspace
cat > ~/cow-workspace/.env <<'EOF'
# CowAgent credentials -- mode 600, never pasted into config.json.
MINIMAX_API_KEY=sk-REPLACE_WITH_YOUR_KEY
EOF
chmod 600 ~/cow-workspace/.env
cp ~/cow-workspace/.env ~/.cow/.env
chmod 600 ~/.cow/.env
```

Two copies, both mode `600`, on purpose. `~/.cow/.env` is the copy the agent's
own credential tool reads; `~/cow-workspace/.env` is the one step 5 exports
into the process environment. Write the key once and copy it to both — a
missing `~/.cow/.env` leaves the console up but the agent unable to find its
credentials.

Replace the placeholder with the key from your provider before you expect the
agent to answer. The console and the memory index work without it; chat does
not, because every model call is rejected.

`web_host` decides the bind address; `web_password` decides whether the console
asks for a login. Setting `web_host: 0.0.0.0` binds every interface whether or
not a password is set — leave the password empty and that is an open console on
your LAN, and the startup log says so:

```
[WebChannel] ⚠️  Listening on 0.0.0.0 without web_password set; set an access password in config.json for public deployment
```

Set both, or bind `127.0.0.1` and reach the console through an SSH tunnel.

### 5. Start the console

```bash
cd ~/CowAgent
set -a; . ~/cow-workspace/.env; set +a
nohup setsid .venv/bin/python app.py > nohup.out 2>&1 &
```

`nohup.out` and the runtime `run.log` both land in the checkout root — the
first because the command above redirects there, the second because the logger
writes it next to the data root. `.gitignore` already excludes `*.log`,
`nohup.out` and `config.json`, so none of them makes the working tree dirty.

Check that it came up:

```bash
pgrep -af app.py
ss -ltnp | grep 9899
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9899/     # 200
grep -c "SourceTreeWorkspaceError" nohup.out run.log
```

The last line prints one count per file — `nohup.out:0` and `run.log:0` — not a
bare `0`. You want zero on both. If `run.log` does not exist yet the command
still exits `2` and prints an error for that file; read the `nohup.out` count.

Then open `http://<server-ip>:9899/` and sign in with the console password
printed in step 4.

**The command above runs the console under `nohup`, so it does not come back
after a reboot.** The two deployment paths differ in more than that:

| | Manual steps (1-6 above) | `deploy_cowagent_live_memory.sh` |
| :--- | :--- | :--- |
| Survives a reboot | no | yes - `systemd`, enabled at boot |
| Re-runnable | no - every step again | yes - idempotent by design |
| Result verified | by hand | 10 checks (9 fail the run; 1 reports status only) |
| Needs the script on the server | no | yes - copy it over first |

The script is a standalone file, not part of this repository. Its single source
of truth is `~/cow/scripts/deploy_cowagent_live_memory.sh` on the operator's
machine; copy that one file to the server, keep its line endings as LF, and run
it **as the user that will own the service**. The unit shown below is written by
the script itself, so the two stay in step:

```bash
chmod +x deploy_cowagent_live_memory.sh
./deploy_cowagent_live_memory.sh \
  --repo https://github.com/paikeywu/CowAgent.git \
  --branch live-memory \
  --install /home/<user>/CowAgent \
  --workspace /home/<user>/cow-workspace \
  --port 9899
```

Do not run it with `sudo`. The script elevates only the individual
`systemctl` and `tee` calls it needs, and it writes `~/.cow/.env` and the
service's `HOME` from the current environment - under `sudo` those become
root's home, where the service user cannot read them.

It performs the same steps 1-6 do, installs a `systemd` unit, and exits `0`
only when every check passes. A file copied from Windows arrives with CRLF
line endings, which bash cannot parse; the script's first line checks for that
and stops with a clear message instead of a syntax error.

Under `systemd` the service is supervised and comes back on its own. The unit
looks like this:

```ini
[Unit]
Description=CowAgent (live-memory branch)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=<user>
WorkingDirectory=/home/<user>/CowAgent
ExecStart=/home/<user>/cow-workspace/start-cow.sh
Restart=always
RestartSec=5
UMask=0077
StandardOutput=append:/home/<user>/cow-workspace/server.log
StandardError=append:/home/<user>/cow-workspace/server.log
Environment=HOME=/home/<user>

[Install]
WantedBy=multi-user.target
```

`StartLimitIntervalSec=` and `StartLimitBurst=` are `[Unit]` directives. In
`[Service]` systemd does not know `StartLimitIntervalSec=` and ignores it, so
the start rate limit falls back to the manager default
(`DefaultStartLimitIntervalSec=`, 10 seconds) rather than the interval you
wrote - and `Restart=always` with a broken configuration then trips the limit
several times faster than intended.

The unit starts `~/cow-workspace/start-cow.sh`, a short wrapper that exports
`~/cow-workspace/.env` and then `exec`s the app. Helper scripts belong in the
workspace, not in the checkout root - `git status` has to stay clean.

Useful commands:

```bash
sudo systemctl status cowagent                    # is it up, and since when
sudo systemctl show -p MainPID --value cowagent   # the PID systemd supervises
ss -ltnp | grep 9899                              # the PID holding the port
sudo journalctl -u cowagent -n 50                 # what it logged
```

Those two PIDs must be the same process. That is what proves the console is
serving the code you just deployed and not a stale instance left over from an
earlier run.

### 6. Smoke-test the memory system

The memory subsystem is the part worth verifying — a running process proves
nothing about retrieval.

```bash
cd ~/CowAgent
.venv/bin/python -m pytest tests/ --collect-only -q      # 0 collection errors
.venv/bin/python -m pytest tests/test_memory_facade.py -q
```

All cases in this file run on Linux. The three link cases build a junction
through `cmd /c mklink /J` on Windows and a symlink elsewhere, so they exercise
the same path-escape guard on both platforms.

Write a memory through the runtime write path, index it, and read it back:

```bash
cd ~/CowAgent
.venv/bin/python - <<'PY'
import asyncio, sys, uuid
from datetime import datetime
from pathlib import Path
sys.path.insert(0, ".")
from agent.memory.summarizer import ensure_daily_memory_file
from agent.memory.config import MemoryConfig
from agent.memory.manager import MemoryManager

WS = Path.home() / "cow-workspace"
MARKER = "COWDEPLOY-" + uuid.uuid4().hex[:8]

daily = ensure_daily_memory_file(WS)
with open(daily, "a", encoding="utf-8") as f:
    f.write("\n## Session Notes (%s)\n\n%s\n" % (datetime.now().strftime("%H:%M"), MARKER))

mgr = MemoryManager(MemoryConfig(workspace_root=str(WS)),
                    embedding_provider=None, reranker=None)
try:
    asyncio.run(mgr.sync(force=True))
    hits = asyncio.run(mgr.search(MARKER, max_results=10, min_score=0.0))
    blob = "\n".join(str(getattr(h, "snippet", "") or "") for h in hits)
    print("hits:", len(hits), "| marker returned:", MARKER in blob)
    if not hits or MARKER not in blob:
        sys.exit(1)
finally:
    try:
        mgr.storage.close()
    except Exception:
        pass
PY
```

`hits: 1 | marker returned: True` means the memory was written, indexed, and
read back out of the real index.

### Troubleshooting

**Port 9899 already in use**

```bash
ss -ltnp | grep 9899                      # which process holds it
lsof -i :9899                             # or this
```

Stop that process, or change `web_port` in `config.json`. The desktop client
picks its port from `COW_WEB_PORT`, so it never collides with a source run.

**Dependencies will not install**

```bash
.venv/bin/python -m pip install -r requirements.txt \
  -i https://pypi.tuna.tsinghua.edu.cn/simple -v      # -v shows the real error
```

A slow or stalled download is the index, not your network: switch to the
Tsinghua mirror. `error: externally-managed-environment` means the install
ran against the system Python — use `.venv/bin/python`, never `pip` on its own.

**`python3 -m venv .venv` fails with "ensurepip is not available"**

The `python3-venv` package is missing. Install it and run the command again:

```bash
sudo apt-get install -y python3-venv
```

**A shell script you copied from Windows fails with `syntax error near unexpected token $'in\r'`**

The file has CRLF line endings. Linux bash cannot parse it — every line ends
with a carriage return, and the `case ... in` keyword becomes `in\r`. Convert
the file before running it:

```bash
sed -i 's/\r$//' deploy.sh
```

Check before you run:

```bash
file deploy.sh          # a CRLF file says "with CRLF line terminators"
```

Note that a CRLF file also makes every blank line into a line holding only a
carriage return, which bash reports as `$'\r': command not found`. Those
messages are noise — the real error is the `case ... in` one.

**`git clone` fails, or the network to GitHub is unreliable**

Clone does not have a fallback built in, and a server behind a proxy may reach
`pypi.tuna.tsinghua.edu.cn` but not `github.com`. Clone on a machine that has
access and copy the directory over:

```bash
git clone --branch live-memory https://github.com/paikeywu/CowAgent.git CowAgent
rsync -a CowAgent/ server:/home/<user>/CowAgent/
```

If the checkout already exists and only `git fetch` is failing, you can keep
the local branch instead of aborting the whole deployment:

```bash
cd ~/CowAgent
git fetch origin live-memory        # may fail; that is fine
git checkout live-memory            # serve what you already have
```

**`git status` shows files you did not create**

Anything you put in the checkout root that `.gitignore` does not cover shows up
as untracked — a helper shell script, a `start.sh`, a notes file. `.gitignore`
covers `config.json`, `nohup.out`, `*.log` and `*.egg-info/`, but not `*.sh`.
Keep helper scripts in `~/cow-workspace`, or add the file to `.gitignore`.

**The app dies with `SourceTreeWorkspaceError`**

```bash
grep "SourceTreeWorkspaceError" nohup.out run.log
```

`agent_workspace` resolves inside the git checkout. Move it out of the tree —
step 4 already points it at `~/cow-workspace`, so this only happens if you
edited `config.json` by hand. Check what it says now:

```bash
grep agent_workspace ~/CowAgent/config.json
```

Verify before restarting:

```bash
cd ~/CowAgent
.venv/bin/python -c "
import sys; sys.path.insert(0, '.')
from common.state_dir import assert_not_source_tree
assert_not_source_tree('/home/<user>/cow-workspace'); print('workspace OK')"
```

**The console password printed in step 4 does not work**

An environment variable overrides it. At startup `config.py` walks every
environment variable and replaces any `config.json` value whose lowercased name
is in its `available_setting` table - `web_password`, `web_port`,
`agent_workspace`, `model`, `bot_type` and `cow_lang` are all in it:

```bash
env | grep -E '^(WEB_PASSWORD|WEB_PORT|WEB_HOST|AGENT_WORKSPACE|MODEL|BOT_TYPE|COW_LANG)='
```

`web_host` is in that table as well, so `WEB_HOST` overrides the bind address.

**`systemd-analyze verify` reports `Unknown key name 'StartLimitIntervalSec'`**

`StartLimitIntervalSec=` and `StartLimitBurst=` belong to the `[Unit]`
section. Placed in `[Service]`, systemd ignores `StartLimitIntervalSec=` and
the start rate limit silently falls back to the manager default. Move both
lines into `[Unit]`.

**The service sits in `failed` after a crash loop**

The start rate limit has been reached. Clear the counter and start it again:

```bash
sudo systemctl reset-failed cowagent
sudo systemctl restart cowagent
```

**The console returns 401**

`web_password` is set and you are sending no credentials. Sign in first — the
login endpoint is `POST /auth/login` with `{"password": "..."}`, which returns
a token and a `cow_auth_token` cookie.

**The console loads, but every message comes back as an error**

The model key is still the placeholder, or it was never loaded. Check the file
and restart the service:

```bash
grep MINIMAX_API_KEY ~/cow-workspace/.env
```

<br/>
## 👥 Multi-Agent Team

Create a team of Agents, each with its own memory, capabilities, knowledge, and workspace. Each can focus on its own domain, or collaborate within a shared conversation to tackle complex tasks together. Multi-Agent teams are available on the Web, the desktop client, and IM channel integrations.

<img src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/screenshots/en/agent-team-overview.png" alt="CowAgent Multi-Agent Team" width="800"/>

> See [Agent Team](https://docs.cowagent.ai/multi-agent/team) for details.

<br/>

## 🤖 Models

CowAgent supports all mainstream LLM providers. **Chat, vision, image generation, ASR/TTS, and embeddings** can each be routed to a different vendor. Providers are configured directly in the Web console — no manual file editing required.

| Provider | Featured Models | Chat | Vision | Image Gen | ASR | TTS | Embedding |
| --- | --- | :-: | :-: | :-: | :-: | :-: | :-: |
| [DeepSeek](https://docs.cowagent.ai/models/deepseek) | deepseek-flash (V4.1) / pro | ✅ | ✅ | | | | |
| [Claude](https://docs.cowagent.ai/models/claude) | claude-opus-5-5 / fable-5.1 | ✅ | ✅ | | | | |
| [OpenAI](https://docs.cowagent.ai/models/openai) | gpt-6.1-sol / gpt-6 series | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Gemini](https://docs.cowagent.ai/models/gemini) | gemini-3.8-flash | ✅ | ✅ | ✅ | | | |
| [MiniMax](https://docs.cowagent.ai/models/minimax) | MiniMax-M3 | ✅ | ✅ | ✅ | | ✅ | |
| [GLM](https://docs.cowagent.ai/models/glm) | glm-5.3-flash, glm-5v-turbo | ✅ | ✅ | | ✅ | | ✅ |
| [Qwen](https://docs.cowagent.ai/models/qwen) | qwen3.8-flash | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Kimi](https://docs.cowagent.ai/models/kimi) | kimi-k3 | ✅ | ✅ | | | | |
| [Doubao](https://docs.cowagent.ai/models/doubao) | doubao-seed-2.1 series | ✅ | ✅ | ✅ | | | ✅ |
| [ERNIE](https://docs.cowagent.ai/models/qianfan) | ernie-5.1 | ✅ | ✅ | | | | |
| [MiMo](https://docs.cowagent.ai/models/mimo) | mimo-v2.6-pro / flash | ✅ | ✅ | | ✅ | ✅ | |
| [LinkAI](https://docs.cowagent.ai/models/linkai) | One key for 100+ models | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Custom](https://docs.cowagent.ai/models/custom) | Local models / third-party proxy | ✅ | | | | | |

> For details on each provider, see the [Models overview](https://docs.cowagent.ai/models/index).

<br/>

## 💬 Channels

A single Agent instance can serve multiple channels in parallel. Most channels can be onboarded right from the Web console.

| Channel | Text | Image | File | Voice | Group |
| --- | :-: | :-: | :-: | :-: | :-: |
| [Web Console](https://docs.cowagent.ai/channels/web) (default) | ✅ | ✅ | ✅ | ✅ | |
| [Telegram](https://docs.cowagent.ai/channels/telegram) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Slack](https://docs.cowagent.ai/channels/slack) | ✅ | ✅ | ✅ | | ✅ |
| [Discord](https://docs.cowagent.ai/channels/discord) | ✅ | ✅ | ✅ | | ✅ |
| [WeChat](https://docs.cowagent.ai/channels/weixin) | ✅ | ✅ | ✅ | ✅ | |
| [Feishu / Lark](https://docs.cowagent.ai/channels/feishu) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [DingTalk](https://docs.cowagent.ai/channels/dingtalk) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [WeCom Bot](https://docs.cowagent.ai/channels/wecom-bot) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [QQ](https://docs.cowagent.ai/channels/qq) | ✅ | ✅ | ✅ | | ✅ |
| [WeCom App](https://docs.cowagent.ai/channels/wecom) | ✅ | ✅ | ✅ | ✅ | |
| [WeChat Customer Service](https://docs.cowagent.ai/channels/wechat-kf) | ✅ | ✅ | ✅ | ✅ | |
| [WeChat Official Account](https://docs.cowagent.ai/channels/wechatmp) | ✅ | ✅ | | ✅ | |

> See the [Channels overview](https://docs.cowagent.ai/channels/index) for setup details.

<img src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/screenshots/en/web-console-chat.png" alt="CowAgent Web Console" width="800"/>

*The Web console is the default channel and the unified entry point to configure models, channels, skills, memory, and more.*

<br/>

## 🧠 Memory & Knowledge Base

**Long-term memory** uses a three-tier architecture: conversation context (short-term) → daily memory (mid-term) → MEMORY.md (long-term). A nightly **Deep Dream** pass distills scattered memories into refined long-term entries and a narrative journal. See [Long-term Memory](https://docs.cowagent.ai/memory/index) · [Deep Dream](https://docs.cowagent.ai/memory/deep-dream).

**Personal knowledge base** complements the time-ordered memory by organizing structured knowledge **by topic**. The Agent automatically curates valuable information from conversations, maintains cross-references and indexes, and the Web console offers an interactive knowledge-graph view. See [Personal Knowledge Base](https://docs.cowagent.ai/knowledge/index).

<table>
  <tr>
    <td width="50%">
      <img src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/screenshots/en/web-console-memory.png" alt="Long-term Memory" />
      <p align="center"><em>Long-term Memory · Three-tier architecture + Deep Dream</em></p>
    </td>
    <td width="50%">
      <img src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/screenshots/en/web-console-knowledge.png" alt="Personal Knowledge Base" />
      <p align="center"><em>Knowledge Base · Auto-curated Markdown wiki</em></p>
    </td>
  </tr>
</table>

<br/>

## 🔧 Tools & Skills

**Tools** are atomic capabilities the Agent uses to interact with system resources. **Skills** are higher-level workflows defined by a manifest file that compose multiple tools to accomplish complex tasks.

### Tool System

**Built-in tools** cover file I/O (`read` / `write` / `edit` / `ls`), terminal (`bash`), file sending (`send`), memory retrieval (`memory`), environment variables (`env_config`), web fetching (`web_fetch`), scheduling (`scheduler`), web search (`web_search`), vision (`vision`), and browser automation (`browser`).

**MCP protocol** integrates the open ecosystem of [Model Context Protocol](https://modelcontextprotocol.io) servers. A single `mcp.json` is enough — supports stdio / SSE transports, hot reload, and zero-code integration.

Learn more: [Tools overview](https://docs.cowagent.ai/tools/index) · [MCP integration](https://docs.cowagent.ai/tools/mcp).

### Skills System

- **[Skill Hub](https://skills.cowagent.ai/)** — open skill marketplace: browse, search, install in one click
- **GitHub / ClawHub / URL and more** — install skills from any source
- **Conversational authoring** — generate custom skills through dialogue with `skill-creator`; turn any workflow or third-party API into a reusable skill

```text
/skill list                   # list installed skills
/skill search <keyword>        # search the marketplace
/skill install <name>          # one-click install
```

Learn more: [Skills overview](https://docs.cowagent.ai/skills/index) · [Creating Skills](https://docs.cowagent.ai/skills/create).

<br/>

## 🏷 Changelog

> **2026.10.14:** [v2.2.1](https://github.com/zhayujie/CowAgent/releases/tag/2.2.1) — Artifacts center and custom menu, open Agent Chat API, prompt caching and context cost savings, tool, channel and security improvements.

> **2026.09.30:** [v2.2.0](https://github.com/zhayujie/CowAgent/releases/tag/2.2.0) — Capabilities center, multi-Agent collaboration improvements, Web console rebuild, higher model cache hit rates, broad stability and security hardening.

> **2026.09.14:** [v2.1.9](https://github.com/zhayujie/CowAgent/releases/tag/2.1.9) — Multi-Agent collaboration refinements, configurable model lists and multiple fallback models, channel integration fixes, new image models (gpt-image-2.5), plus voice fixes.

> **2026.09.10:** [v2.1.8](https://github.com/zhayujie/CowAgent/releases/tag/2.1.8) — Multi-Agent teams, manual scheduled task creation, context-usage visualization, new models and search providers, workspace file editing.

> **2026.08.20:** [v2.1.7](https://github.com/zhayujie/CowAgent/releases/tag/2.1.7) — Multiple workspaces, session-level permission modes, task notifications, desktop improvements, plus new model support.

> **2026.08.12:** [v2.1.6](https://github.com/zhayujie/CowAgent/releases/tag/2.1.6) — Sub agents for parallel task delegation, reasoning-effort settings, a pluggable memory vector backend, plus experience and security improvements.

> **2026.07.29:** [v2.1.5](https://github.com/zhayujie/CowAgent/releases/tag/2.1.5) — Workspace with file preview, core tool improvements (file search, write-time validation, background commands), context compaction (`/compact`), one-click prompt optimization, security hardening.

> **2026.07.20:** [v2.1.4](https://github.com/zhayujie/CowAgent/releases/tag/2.1.4) — Desktop experience improvements, MCP OAuth authorization, Lark channel enhancements, scheduler improvements and data backup, new models.

> **2026.07.08:** [v2.1.3](https://github.com/zhayujie/CowAgent/releases/tag/2.1.3) — [Desktop client](https://cowagent.ai/download/) for macOS / Windows, knowledge base document management, on-demand MCP tool retrieval, Traditional Chinese support, new models.

> **2026.06.18:** [v2.1.2](https://github.com/zhayujie/CowAgent/releases/tag/2.1.2) — Web console upgrades (scheduled task management, knowledge base categories, multiple custom model providers), Self-Evolution improvements, new models (kimi-k2.7-code, glm-5.2), security hardening and refinements.

> **2026.06.09:** [v2.1.1](https://github.com/zhayujie/CowAgent/releases/tag/2.1.1) — Self-Evolution, Web console upgrades (message management, parallel sessions), cross-platform MCP enhancements with concurrent calls, new models (MiniMax-M3, qwen3.7-plus), Python 3.13 support.

> **2026.06.01:** [v2.1.0](https://github.com/zhayujie/CowAgent/releases/tag/2.1.0) — Internationalization, new channels (Telegram, Discord, Slack, WeChat Customer Service), CLI interaction upgrades, streamlined one-line install, MCP Streamable HTTP support, new models (claude-opus-4-8, MiMo).

Full history: [Release Notes](https://docs.cowagent.ai/releases/overview)

<br/>

## 🤝 Community & Support

Join our [**Discord server**](https://discord.gg/9U8eA8v9TR) to ask questions, share skills, and follow development:

<a href="https://discord.gg/9U8eA8v9TR"><img src="https://img.shields.io/badge/Discord-Join%20the%20community-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Join our Discord"></a>

You can also [file an issue](https://github.com/zhayujie/CowAgent/issues) on GitHub, or scan the QR code below to join our WeChat group:

<img width="130" src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/community/open-community.png">

<br/>

## 🔗 Related Projects

- **[Cow Skill Hub](https://github.com/zhayujie/cow-skill-hub)** — open skill marketplace for AI Agents; works with CowAgent, OpenClaw, Claude Code, and more
- **[bot-on-anything](https://github.com/zhayujie/bot-on-anything)** — lightweight LLM application framework with integrations for Slack, Telegram, Discord, Gmail, and more
- **[AgentMesh](https://github.com/MinimalFuture/AgentMesh)** — open-source multi-agent framework for solving complex problems through team collaboration

<br/>

## 🏢 Enterprise Services

[**LinkAI**](https://link-ai.tech/?utm_source=cowagent&utm_medium=readme&utm_campaign=enterprise) is an all-in-one AI Agent platform for enterprises and developers, offering managed hosting and enterprise-grade support for CowAgent:

- **🚀 Zero-deployment hosted runtime** — spin up a [CowAgent online assistant](https://link-ai.tech/cowagent/create?utm_source=cowagent&utm_medium=readme&utm_campaign=enterprise) in under a minute, no server required
- **🧠 Agent infrastructure** — unified access to LLMs, knowledge bases, databases, skills, and workflows; plug-and-play building blocks that extend what CowAgent can do
- **🏢 Team & enterprise features** — workspaces, role-based access, audit logs, and private deployment for production use cases

For enterprise inquiries: sales@simple-future.tech or [scan the QR code](https://cdn.link-ai.tech/contact/customer-support.png) to reach our team on WeChat.

<br/>

## 🛠️ Development & Contributing

All kinds of contributions are welcome — new features, bug fixes, performance improvements, docs, or sharing your own skills on the [Skill Hub](https://skills.cowagent.ai/submit). See [CONTRIBUTING.md](CONTRIBUTING.md) to get started, then open an Issue to discuss or send a PR directly.

⭐ Star the project to show your support, and Watch → Custom → Releases to get notified of new versions. PRs and Issues are always welcome.

## 🌟 Contributors

![cow contributors](https://contrib.rocks/image?repo=zhayujie/CowAgent&max=1000)

<br/>

## ⚠️ Disclaimer

1. This project is licensed under the [MIT License](LICENSE) and is intended for technical research and learning. You are responsible for complying with applicable laws and regulations in your jurisdiction; the maintainers assume no liability for any consequences arising from use of this project.
2. **Cost & safety:** Agent mode consumes substantially more tokens than regular chat — pick models that balance quality and cost. The Agent has access to your local operating system, so only deploy it in trusted environments.
3. CowAgent is a pure open-source project and does not participate in, authorize, or issue any cryptocurrency.

<br/>

## 📌 Project Renaming Notice

This project was previously named `chatgpt-on-wechat` and is now officially **CowAgent**. The old GitHub URL redirects automatically; existing users may optionally run `git remote set-url origin https://github.com/zhayujie/CowAgent.git` to update the local remote.
