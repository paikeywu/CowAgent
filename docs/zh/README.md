<p align="center"><img src= "https://github.com/user-attachments/assets/eca9a9ec-8534-4615-9e0f-96c5ac1d10a3" alt="CowAgent" width="420" /></p>

<p align="center">
  <a href="https://github.com/zhayujie/CowAgent/releases/latest"><img src="https://img.shields.io/github/v/release/zhayujie/CowAgent?cacheSeconds=3600" alt="Latest release" /></a>
  <a href="https://github.com/zhayujie/CowAgent/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT" /></a>
  <a href="https://github.com/zhayujie/CowAgent"><img src="https://img.shields.io/github/stars/zhayujie/CowAgent?style=flat-square&cacheSeconds=3600" alt="Stars" /></a>
  <a href="https://docs.cowagent.ai/zh"><img src="https://img.shields.io/badge/%E6%96%87%E6%A1%A3-cowagent.ai-blue?style=flat&logo=readthedocs&logoColor=white" alt="文档" /></a>
  <a href="https://cdn.link-ai.tech/code/cow/cowagent-wechat-group.png"><img src="https://img.shields.io/badge/%E5%BE%AE%E4%BF%A1-%E4%BA%A4%E6%B5%81%E7%BE%A4-07C160?style=flat&logo=wechat&logoColor=white" alt="微信交流群" /></a>
  <a href="https://discord.gg/9U8eA8v9TR"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat&logo=discord&logoColor=white" alt="Discord" /></a>
</p>

<p align="center">
  <a href="https://trendshift.io/repositories/25763" target="_blank"><img src="https://trendshift.io/api/badge/repositories/25763" alt="zhayujie%2FCowAgent | Trendshift" style="width: 250px; height: 55px;" width="250" height="55"/></a>
</p>

<p align="center">
  [<a href="../../README.md">English</a>] | [中文] | [<a href="README-Hant.md">繁體中文</a>] | [<a href="../ja/README.md">日本語</a>]
</p>

**CowAgent** 是一个开源的超级 AI 助理，能够主动思考和规划任务、操作计算机和外部资源、创造和执行 Skills、构建知识库与长期记忆、创建多智能体团队，并通过自主进化与你一同成长，是 Agent Harness 工程的最佳实践之一。

CowAgent 轻量、易部署、可扩展，自由接入主流大模型，覆盖微信、飞书、钉钉、企微、QQ、Telegram、Slack、网页等消息通道，7×24 运行于个人电脑或服务器中。

<p align="center">
  <a href="https://cowagent.ai/?lang=zh">🌐 官网</a> &nbsp;·&nbsp;
  <a href="https://docs.cowagent.ai/zh/">📖 文档中心</a> &nbsp;·&nbsp;
  <a href="https://docs.cowagent.ai/zh/guide/quick-start">🚀 快速开始</a> &nbsp;·&nbsp;
  <a href="https://skills.cowagent.ai/">🧩 技能广场</a> &nbsp;·&nbsp;
  <a href="https://cowagent.ai/zh/download/">💻 下载客户端</a> &nbsp;·&nbsp;
  <a href="https://link-ai.tech/cowagent/create?utm_source=cowagent&utm_medium=readme&utm_campaign=nav">☁️ 在线体验</a>
</p>

<br/>

## 🎬 演示

<p align="center">
  <video src="https://github.com/user-attachments/assets/a2d8fb12-f294-42d6-a569-c4a6ceedab1d" controls muted playsinline width="720">
    你的浏览器无法播放该视频。
    <a href="https://cowagent.ai/?lang=zh">前往官网观看演示 →</a>
  </video>
</p>

<br/>

## 🌟 核心能力

| 能力 | 说明 |
| :--- | :--- |
| [任务规划](https://docs.cowagent.ai/zh/intro/architecture) | 理解复杂任务并自主分解执行，循环调用工具直到完成目标 |
| [多智能体](https://docs.cowagent.ai/zh/multi-agent/team) | 创建多个 Agent 组成团队，各自拥有独立的记忆、能力与知识，通过分工协作完成任务 |
| [长期记忆](https://docs.cowagent.ai/zh/memory) | 三层记忆架构（上下文 → 天级 → 核心），梦境蒸馏自动整理，支持关键词与向量混合检索 |
| [知识库](https://docs.cowagent.ai/zh/knowledge) | 自动整理结构化知识为 Markdown Wiki，构建持续增长的知识图谱，可视化浏览 |
| [自主进化](https://docs.cowagent.ai/zh/memory/self-evolution) | 自动复盘对话，优化技能、处理未完成事项、沉淀记忆与知识，在使用中持续成长 |
| [技能](https://docs.cowagent.ai/zh/skills) | 从 [Skill Hub](https://skills.cowagent.ai/)、GitHub、ClawHub 等一键安装；也可通过对话创造自定义技能 |
| [工具](https://docs.cowagent.ai/zh/tools) | 内置文件读写、终端、浏览器、定时任务、记忆检索、联网搜索等 10+ 工具，支持 MCP 协议 |
| [通道](https://docs.cowagent.ai/zh/channels) | 一个 Agent 同时接入 Web、微信、飞书、钉钉、企微、QQ、公众号、Telegram、Slack 等多个渠道 |
| 多模态 | 文本、图片、语音、文件全消息类型支持，覆盖识别、生成、收发 |
| [模型](https://docs.cowagent.ai/zh/models) | DeepSeek、Claude、Gemini、GPT、GLM、Qwen、Kimi、MiniMax、Doubao 等主流厂商，配置一行切换 |
| [部署](https://docs.cowagent.ai/zh/guide/quick-start) | 一键脚本安装，Web 控制台统一管理；本地、Docker、服务器多种部署方式 |

<br/>

## 🏗️ 架构总览

<img src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/architecture/zh/architecture.jpg" alt="CowAgent Architecture" width="750"/>

CowAgent 是一个完整的 **Agent Harness**：消息从各类**通道**进入，**Agent Core** 结合记忆、知识库与可用工具/技能进行任务规划与决策，调用**模型**生成结果，再回传至原通道。各模块解耦清晰，按需扩展。

详见 [项目架构](https://docs.cowagent.ai/zh/intro/architecture)。

<br/>

## 🚀 快速开始

项目提供一键安装脚本，自动完成依赖安装、配置和启动：

**Linux / macOS：**

```bash
bash <(curl -fsSL https://cdn.link-ai.tech/code/cow/run.sh)
```

**Windows（PowerShell）：**

```powershell
irm https://cdn.link-ai.tech/code/cow/run.ps1 | iex
```

**Docker：**

```bash
curl -O https://cdn.link-ai.tech/code/cow/docker-compose.yml
docker compose up -d
```

启动成功后访问 `http://localhost:9899` 进入 **Web 控制台**，在控制台内即可完成模型配置、渠道接入、技能安装等全部操作。

> 服务器部署且需要公网访问控制台时，请在 `config.json` 中将 `web_host` 设为 `0.0.0.0`（同时强烈建议设置 `web_password` 启用鉴权），然后访问 `http://<server-ip>:9899`，并确保防火墙/安全组放行 `9899` 端口。

> 📖 详细安装指南：[快速开始](https://docs.cowagent.ai/zh/guide/quick-start) · [源码安装](https://docs.cowagent.ai/zh/guide/manual-install) · [升级](https://docs.cowagent.ai/zh/guide/upgrade)

安装后可使用 `cow` [CLI 命令](https://docs.cowagent.ai/zh/cli) 管理服务：

```bash
cow start | stop | restart        # 服务管理
cow status | logs                  # 状态和日志
cow update                         # 拉取最新代码并重启
cow skill install <名称>           # 安装技能
cow install-browser                # 安装浏览器工具
```

> 💻 桌面客户端：前往 **[下载 CowAgent 桌面客户端](https://cowagent.ai/zh/download/)**（macOS / Windows），内置 Agent 运行环境，开箱即用。

<br/>

## 🖥️ 在 Ubuntu 服务器上部署

在一台无桌面的 Ubuntu 主机上从源码运行 CowAgent。下面的步骤在 Ubuntu 26.04 LTS（Python 3.14.4）上实测通过，按顺序复制执行即可，每段代码块都可以单独复制粘贴。

这里给两条部署路径：手工步骤（1-6）用 `nohup` 跑控制台；部署脚本则装一个 `systemd` 服务并自动验收。第 5 步会对比两者。

### 前置条件

| 项目 | 取值 | 原因 |
| :--- | :--- | :--- |
| 操作系统 | Ubuntu 26.04 LTS | 第 1 步用 `apt-get` 装包 |
| Python | 3.14（`python3 -V`） | `python3 -m venv` 给出隔离的解释器 |
| 磁盘 | 约 2 GB 空闲 | 约 390 MB 虚拟环境 + 约 40 MB 代码检出 |
| 包索引 | `https://pypi.tuna.tsinghua.edu.cn/simple` | 官方 PyPI 在部分网络下慢到不可用 |
| 出网 | `github.com`、`pypi.tuna.tsinghua.edu.cn` | 克隆代码 + 装依赖 |
| 放行端口 | `9899/tcp` | Web 控制台 |

> 模型密钥**不要**写进 `config.json`，放在第 4 步的 `.env` 里，权限 `600`。控制台密码**在** `config.json` 里，因此该文件同样保持 `600`。

下面的检查要用到 `curl` 和 `ss`：`curl` 负责向控制台发请求，`ss` 负责报告端口被哪个进程占用。最小化安装的 Ubuntu 镜像通常没有 `curl`，`ss` 则随 `iproute2` 一起安装。

### 1. 获取代码

```bash
sudo apt-get update && sudo apt-get install -y git curl python3-venv lsof iproute2
git clone --branch live-memory https://github.com/paikeywu/CowAgent.git ~/CowAgent
cd ~/CowAgent
git rev-parse --abbrev-ref HEAD     # 必须输出：live-memory
git rev-parse HEAD                  # 记下这个值 —— 就是你部署的提交
```

只写 `git clone` 会检出默认分支（`master`），**不是**这些步骤对应的分支 —— `--branch live-memory` 才把要部署的代码放上磁盘。部署实测基于提交 `5df9d27052996923fa05d1cd3c4c6c98ec1bc297`；上面打印的哈希是 `live-memory` 当前指向的提交。截至撰写时，实测提交之后只有一个 `42ca91ab`，它只补充了这份说明、没有改动代码 —— 所以步骤仍与你即将运行的代码一致。若两个哈希之间相差的不止文档提交，先读 diff 再决定是否信任这些步骤。

`python3-venv` 在这个包列表里，是因为 Ubuntu 的系统解释器默认不带它。缺这个包时，第 2 步的 `python3 -m venv` 会报 `ensurepip is not available`。`lsof` 是故障排查章节查端口用的。

### 2. 创建虚拟环境并安装依赖

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
  -i https://pypi.tuna.tsinghua.edu.cn/simple        # 不在 requirements.txt 里；第 6 步要用
```

**用虚拟环境，不要用系统 Python。** 现在的 Ubuntu 把系统解释器标记为 externally managed（PEP 668），往里 `pip install` 会失败并提示你加 `--break-system-packages`。那个开关绕过了操作系统对自身包的保护，可能搞坏系统 Python —— 不要用。虚拟环境就是隔离机制，不需要额外装包。

`pip install -e .` 把本仓库装成可编辑包，`cow` 命令由此进入 PATH（`cow start`、`cow status`、`cow logs`），也就是上文「快速开始」里提到的那个命令。它会在检出目录里生成 `cowagent.egg-info/`，`.gitignore` 已经排除了它，工作区保持干净。

### 3. 把运行时状态放到源码树之外

```bash
mkdir -p ~/cow-workspace
```

`agent_workspace` 必须指向**不在** git 检出目录内的位置。运行时状态（记忆索引、调度器存储、skills）属于运行它的人，不属于仓库 —— 一次 clone 绝不能把别人的 `memory/long-term/index.db` 交给新用户。

这一点在启动时被强制检查。`agent_workspace` 落在检出目录里，进程会拒绝启动：

```
common.state_dir.SourceTreeWorkspaceError: 工作区位于源码树内，拒绝把运行时状态写进源码树：'/home/<user>/CowAgent' 在 '/home/<user>/CowAgent' 之内。
  运行时状态（memory/long-term/index.db 等）属于运行它的人，不属于仓库；它一旦进入版本库，clone 的人会拿到别人的记忆索引。
  请把 agent_workspace / workspace 指向一个工作区目录（例如 %USERPROFILE%\cow），而不是源码目录。
```

这个检查会从工作区一路向上找 `.git` 祖先目录，所以检出目录下的任何位置都会被拒绝。把它指向 `~/cow-workspace`（或任何树外的目录）即可启动。

### 4. 写 `config.json` 和凭据文件

源码部署时，`config.json` 就在检出目录旁边（`~/CowAgent/config.json`）。如果你要把用户数据放到别处，用 `COW_DATA_DIR` 覆盖。

这个路径不是约定，而是代码就是这么做的：`config.py` 用 `os.path.join(get_data_root(), "config.json")` 拼路径，而 `COW_DATA_DIR` 未设置时 `get_data_root()` 回落到仓库根目录。桌面客户端会设 `COW_DATA_DIR=~/.cow`，因此 **`~/.cow/config.json` 是桌面客户端的文件，源码部署永远不会读它**。源码部署的 `config.json` 要放在检出目录根下。

一条命令写完整个文件并打印控制台密码：

```bash
cd ~/CowAgent
python3 - <<'PY'
import json, os, secrets

# 想自己指定就填这里；留空则自动生成。
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

把这个密码记下来，或者以后再读出来 —— `config.json` 权限是 `600`，只有你自己的用户能看：

```bash
python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/CowAgent/config.json')))['web_password'])"
```

**还有一样东西能覆盖这个密码。** 启动时 `config.py` 会遍历所有环境变量，把名字小写后，凡是命中 `available_setting` 表里的键，就用环境变量的值替换 `config.json` 里的值。`web_password`、`web_port`、`agent_workspace`、`model`、`bot_type`、`cow_lang` 都在这张表里 —— 所以服务器上若已经 export 了 `WEB_PASSWORD`，控制台要的就是**那个**密码，不是 `config.json` 里这个。发密码之前先查一下：

```bash
env | grep -E '^(WEB_PASSWORD|WEB_PORT|AGENT_WORKSPACE|MODEL|BOT_TYPE|COW_LANG)='
```

`web_host` 不在这张表里，`WEB_HOST` 覆盖不了任何东西。

> MiniMax 的域名是 `api.minimaxi.com`，`api.minimax.com` 解析不了。

模型密钥写进环境文件，不写进 `config.json`：

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

两份、都是 `600` 权限，是有意的。`~/.cow/.env` 是 agent 自己的凭据工具读的那份，`~/cow-workspace/.env` 是第 5 步导出到进程环境的那份。密钥写一次、复制到两个位置 —— 少了 `~/.cow/.env`，控制台照样能开，但 agent 找不到自己的凭据。

在指望 agent 能对话之前，把占位符换成你供应商给的密钥。没有密钥时控制台和记忆索引照常工作，对话不行 —— 每一次模型调用都会被拒。

`web_host` 决定绑定地址，`web_password` 决定控制台要不要登录。设成 `web_host: 0.0.0.0` 就会绑定所有网卡，与有没有密码无关 —— 密码留空就是局域网里的一个裸奔控制台，启动日志会明说：

```
[WebChannel] ⚠️  Listening on 0.0.0.0 without web_password set; set an access password in config.json for public deployment
```

要么两个都设，要么绑 `127.0.0.1` 并用 SSH 隧道访问控制台。

### 5. 启动控制台

```bash
cd ~/CowAgent
set -a; . ~/cow-workspace/.env; set +a
nohup setsid .venv/bin/python app.py > nohup.out 2>&1 &
```

`nohup.out` 和运行时的 `run.log` 都落在检出目录根下 —— 前者是因为上面的重定向，后者是因为日志写在数据根目录旁边。`.gitignore` 已经排除了 `*.log`、`nohup.out` 和 `config.json`，所以它们都不会让工作区变脏。

确认它起来了：

```bash
pgrep -af app.py
ss -ltnp | grep 9899
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9899/     # 200
grep -c "SourceTreeWorkspaceError" nohup.out run.log
```

最后一行是**每个文件各打印一个计数** —— `nohup.out:0` 和 `run.log:0`，不是单独的 `0`。两个都要是零。若 `run.log` 还不存在，这条命令会以 `2` 退出并为那个文件报错；读 `nohup.out` 的计数即可。

然后打开 `http://<server-ip>:9899/`，用第 4 步打印的控制台密码登录。

**上面的命令把控制台跑在 `nohup` 下，重启机器后不会自己起来。** 两条部署路径的差别不止这一条：

| | 手工步骤（上面的 1-6） | `deploy_cowagent_live_memory.sh` |
| :--- | :--- | :--- |
| 重启后能自起 | 不能 | 能 —— `systemd`，已设开机自启 |
| 能否重复执行 | 不能 —— 每一步都要重来 | 能 —— 幂等设计 |
| 结果如何验收 | 手工逐条看 | 9 条判据，任何一条不过退出码非零 |
| 是否需要先把脚本拷到服务器 | 不需要 | 需要 —— 先拷过去 |

这个脚本是独立文件，不在本仓库里。把它拷到服务器、**保持 LF 行尾**，然后**用将来拥有这个服务的那个用户**运行：

```bash
chmod +x deploy_cowagent_live_memory.sh
./deploy_cowagent_live_memory.sh \
  --repo https://github.com/paikeywu/CowAgent.git \
  --branch live-memory \
  --install /home/<user>/CowAgent \
  --workspace /home/<user>/cow-workspace \
  --port 9899
```

**不要用 `sudo` 跑它。** 脚本只对它需要的 `systemctl`、`tee` 这几条命令自行提权，而 `~/.cow/.env` 和服务的 `HOME` 取自当前环境 —— 在 `sudo` 下它们会变成 root 的家目录，服务用户读不到。

它做的就是上面 1-6 步的事，另外装一个 `systemd` unit，并且只有全部判据通过才退出 `0`。从 Windows 拷过去的文件带 CRLF 行尾，bash 解析不了；脚本第一行会先查这件事，直接给出明确提示，而不是抛一句语法错。

在 `systemd` 下服务由 systemd 托管，挂了会自己起来。unit 长这样：

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

`StartLimitIntervalSec=` 和 `StartLimitBurst=` 是 `[Unit]` 段的指令。写在 `[Service]` 段里 systemd 不认识 `StartLimitIntervalSec=`，会直接忽略 —— 启动频率限制就回落到 manager 的默认值（`DefaultStartLimitIntervalSec=`，默认 10 秒），而不是你写的那个间隔；配置写错时 `Restart=always` 撞限额的次数会比预期快好几倍。

unit 启动的是 `~/cow-workspace/start-cow.sh` —— 一个很短的包装脚本，负责 export `~/cow-workspace/.env` 再 `exec` 应用。辅助脚本要放在工作区，**不要**放进检出目录根下 —— `git status` 必须保持干净。

常用命令：

```bash
sudo systemctl status cowagent                    # 起来了没有，什么时候起的
sudo systemctl show -p MainPID --value cowagent   # systemd 托管的那个 PID
ss -ltnp | grep 9899                              # 占着端口的那个 PID
sudo journalctl -u cowagent -n 50                 # 它自己记了什么
```

这两个 PID 必须是同一个进程 —— 这才说明控制台跑的是你刚部署的代码，而不是上一轮遗留的旧实例。

### 6. 冒烟测试记忆系统

记忆子系统才值得验证 —— 进程跑着并不能说明检索能用。

```bash
cd ~/CowAgent
.venv/bin/python -m pytest tests/ --collect-only -q      # 0 collection errors
.venv/bin/python -m pytest tests/test_memory_facade.py -q \
  --deselect tests/test_memory_facade.py::test_tc_b1_junction_to_shared_knowledge \
  --deselect tests/test_memory_facade.py::test_tc_b2_dangling_junction \
  --deselect tests/test_memory_facade.py::test_tc_b3_junction_to_default_memory
```

这三个用例通过 `cmd /c mklink /J` 建 NTFS junction，在 Linux 上跑不了。不加 `--deselect` 它们会失败并报 `FileNotFoundError: [Errno 2] No such file or directory: 'cmd'`。这是分支本身的测试可移植性限制，不是部署坏了。

通过运行时写入路径写一条记忆、建索引、再读回来：

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

输出 `hits: 1 | marker returned: True` 说明记忆写进去了、建索引了、也从真实索引里读回来了。

### 故障排查

**9899 端口被占用**

```bash
ss -ltnp | grep 9899                      # 谁占着
lsof -i :9899                             # 或者这个
```

把那个进程停掉，或改 `config.json` 里的 `web_port`。桌面客户端的端口取自 `COW_WEB_PORT`，不会和源码运行撞车。

**依赖装不上**

```bash
.venv/bin/python -m pip install -r requirements.txt \
  -i https://pypi.tuna.tsinghua.edu.cn/simple -v      # -v 能看到真实报错
```

下载慢或卡住是索引的问题，不是你的网络：换清华镜像。报 `error: externally-managed-environment` 说明装到了系统 Python 上 —— 用 `.venv/bin/python`，别直接用 `pip`。

**`python3 -m venv .venv` 报 "ensurepip is not available"**

缺 `python3-venv` 包。装上再重跑：

```bash
sudo apt-get install -y python3-venv
```

**从 Windows 拷过去的脚本报 `syntax error near unexpected token $'in\r'`**

文件是 CRLF 行尾。Linux bash 解析不了 —— 每行结尾都带一个回车符，`case ... in` 变成了 `in\r`。先转成 LF 再执行：

```bash
sed -i 's/\r$//' deploy.sh
```

执行前可以先看一眼：

```bash
file deploy.sh          # CRLF 文件会显示 "with CRLF line terminators"
```

顺带一提，CRLF 文件里每个空行都会变成只含一个回车符的行，bash 会报 `$'\r': command not found`。这些是噪声 —— 真正的错误是 `case ... in` 那一条。

**`git clone` 失败，或到 GitHub 的网络不稳**

clone 本身没有降级逻辑。走代理的机器可能能访问 `pypi.tuna.tsinghua.edu.cn` 却访问不了 `github.com`。在有网络的机器上 clone 再拷过去：

```bash
git clone --branch live-memory https://github.com/paikeywu/CowAgent.git CowAgent
rsync -a CowAgent/ server:/home/<user>/CowAgent/
```

如果检出目录已经存在、只是 `git fetch` 失败，不必让整个部署失败，用本地分支继续即可：

```bash
cd ~/CowAgent
git fetch origin live-memory        # 可能失败；没关系
git checkout live-memory            # 继续用你手上这份
```

**`git status` 里有你没创建的文件**

放进检出目录根、且 `.gitignore` 没覆盖的文件都会显示为未跟踪 —— 辅助脚本、`start.sh`、笔记文件都算。`.gitignore` 覆盖了 `config.json`、`nohup.out`、`*.log` 和 `*.egg-info/`，但**不覆盖 `*.sh`**。辅助脚本放 `~/cow-workspace`，或者把它加进 `.gitignore`。

**进程带着 `SourceTreeWorkspaceError` 退出**

```bash
grep "SourceTreeWorkspaceError" nohup.out run.log
```

`agent_workspace` 解析到了检出目录里面。把它移出源码树 —— 第 4 步已经指向 `~/cow-workspace`，只有你手改过 `config.json` 才会出这问题。先看现在是什么：

```bash
grep agent_workspace ~/CowAgent/config.json
```

重启前先验证：

```bash
cd ~/CowAgent
.venv/bin/python -c "
import sys; sys.path.insert(0, '.')
from common.state_dir import assert_not_source_tree
assert_not_source_tree('/home/<user>/cow-workspace'); print('workspace OK')"
```

**第 4 步打印的控制台密码登不进去**

有环境变量把它覆盖了。启动时 `config.py` 会遍历所有环境变量，把名字小写后，凡是命中 `available_setting` 表里的键，就用环境变量的值替换 `config.json` 里的值 —— `web_password`、`web_port`、`agent_workspace`、`model`、`bot_type`、`cow_lang` 都在这张表里：

```bash
env | grep -E '^(WEB_PASSWORD|WEB_PORT|AGENT_WORKSPACE|MODEL|BOT_TYPE|COW_LANG)='
```

`web_host` 不在这张表里，`WEB_HOST` 覆盖不了任何东西。

**`systemd-analyze verify` 报 `Unknown key name 'StartLimitIntervalSec'`**

`StartLimitIntervalSec=` 和 `StartLimitBurst=` 属于 `[Unit]` 段。写在 `[Service]` 段里，systemd 会忽略 `StartLimitIntervalSec=`，启动频率限制悄悄回落到 manager 默认值。把这两行挪进 `[Unit]`。

**服务重启循环之后停在 `failed`**

启动频率限额被打满了。清掉计数再起：

```bash
sudo systemctl reset-failed cowagent
sudo systemctl restart cowagent
```

**控制台返回 401**

`web_password` 设了，而你没带凭据。先登录 —— 登录接口是 `POST /auth/login`，body 为 `{"password": "..."}`，返回一个 token 并设置 `cow_auth_token` cookie。

**控制台能开，但每条消息都报错**

模型密钥还是占位符，或者压根没加载。检查文件并重启服务：

```bash
grep MINIMAX_API_KEY ~/cow-workspace/.env
```
<br/>

## 👥 多智能体团队

支持创建多个 Agent 组成团队，每个成员拥有独立的记忆、能力、知识与工作空间，既能专注各自领域，也能在同一会话中通过协作完成复杂任务。多 Agent 团队支持在 Web 端、桌面客户端、IM 通道接入端使用。

<img src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/screenshots/zh/agent-team-overview.png" alt="CowAgent 多智能体团队" width="800"/>

> 详见 [Agent 团队](https://docs.cowagent.ai/zh/multi-agent/team)。

<br/>

## 🤖 模型支持

CowAgent 支持国内外主流厂商的大语言模型。**文本对话、图像理解、图像生成、语音识别/合成、向量** 等能力均可独立配置厂商。

| 厂商 | 代表模型 | 文本 | 图像理解 | 图像生成 | 语音识别 | 语音合成 | 向量 |
| --- | --- | :-: | :-: | :-: | :-: | :-: | :-: |
| [DeepSeek](https://docs.cowagent.ai/zh/models/deepseek) | deepseek-flash (V4.1) / pro | ✅ | ✅ | | | | |
| [Claude](https://docs.cowagent.ai/zh/models/claude) | claude-opus-5-5 / fable-5.1 | ✅ | ✅ | | | | |
| [OpenAI](https://docs.cowagent.ai/zh/models/openai) | gpt-6.1-sol / gpt-6 系列 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Gemini](https://docs.cowagent.ai/zh/models/gemini) | gemini-3.8-flash | ✅ | ✅ | ✅ | | | |
| [MiniMax](https://docs.cowagent.ai/zh/models/minimax) | MiniMax-M3 | ✅ | ✅ | ✅ | | ✅ | |
| [智谱 GLM](https://docs.cowagent.ai/zh/models/glm) | glm-5.3-flash、glm-5v-turbo | ✅ | ✅ | | ✅ | | ✅ |
| [通义千问](https://docs.cowagent.ai/zh/models/qwen) | qwen3.8-flash | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Kimi](https://docs.cowagent.ai/zh/models/kimi) | kimi-k3 | ✅ | ✅ | | | | |
| [豆包 Doubao](https://docs.cowagent.ai/zh/models/doubao) | doubao-seed-2.1 系列 | ✅ | ✅ | ✅ | | | ✅ |
| [百度ERNIE](https://docs.cowagent.ai/zh/models/qianfan) | ernie-5.1 | ✅ | ✅ | | | | |
| [小米 MiMo](https://docs.cowagent.ai/zh/models/mimo) | mimo-v2.6-pro / v2.6-flash | ✅ | ✅ | | ✅ | ✅ | |
| [LinkAI](https://docs.cowagent.ai/zh/models/linkai) | 一个 Key 接入 100+ 模型 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [自定义](https://docs.cowagent.ai/zh/models/custom) | 本地模型 / 三方代理 | ✅ | | | | | |

> 推荐通过 Web 控制台在线配置，无需手动编辑文件。手动配置请参考各厂商文档，详见 [模型概览](https://docs.cowagent.ai/zh/models)。

<br/>

## 💬 通道接入

一个 Agent 实例可同时接入多个渠道，启动时通过 `channel_type` 切换或并行运行。

| 通道 | 文本 | 图片 | 文件 | 语音 | 群聊 |
| --- | :-: | :-: | :-: | :-: | :-: |
| [Web 控制台](https://docs.cowagent.ai/zh/channels/web)（默认） | ✅ | ✅ | ✅ | ✅ | |
| [微信](https://docs.cowagent.ai/zh/channels/weixin) | ✅ | ✅ | ✅ | ✅ | |
| [飞书](https://docs.cowagent.ai/zh/channels/feishu) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [钉钉](https://docs.cowagent.ai/zh/channels/dingtalk) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [企微智能机器人](https://docs.cowagent.ai/zh/channels/wecom-bot) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [QQ](https://docs.cowagent.ai/zh/channels/qq) | ✅ | ✅ | ✅ | | ✅ |
| [企业微信应用](https://docs.cowagent.ai/zh/channels/wecom) | ✅ | ✅ | ✅ | ✅ | |
| [微信客服](https://docs.cowagent.ai/zh/channels/wechat-kf) | ✅ | ✅ | ✅ | ✅ | |
| [微信公众号](https://docs.cowagent.ai/zh/channels/wechatmp) | ✅ | ✅ | | ✅ | |
| [Telegram](https://docs.cowagent.ai/zh/channels/telegram) | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Slack](https://docs.cowagent.ai/zh/channels/slack) | ✅ | ✅ | ✅ | | ✅ |
| [Discord](https://docs.cowagent.ai/zh/channels/discord) | ✅ | ✅ | ✅ | | ✅ |

> 飞书、企微智能机器人支持在 Web 控制台内**扫码一键接入**，无需公网 IP。详见 [通道概览](https://docs.cowagent.ai/zh/channels)。

<img src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/screenshots/zh/web-console-chat.png" alt="CowAgent Web 控制台" width="800"/>

*Web 控制台是默认通道，也是统一的 Agent 配置和管理入口*

<br/>

## 🧠 记忆与知识库

**长期记忆**采用三层架构：对话上下文（短期）→ 天级记忆（中期）→ MEMORY.md（长期）。每日自动执行**梦境蒸馏（Deep Dream）**，将分散记忆整合为精炼的长期记忆并生成叙事日记。详见 [长期记忆](https://docs.cowagent.ai/zh/memory) · [梦境蒸馏](https://docs.cowagent.ai/zh/memory/deep-dream)。

**个人知识库** 与按时间记录的记忆不同，以**主题为维度**组织结构化知识。Agent 在对话中自动整理有价值信息，维护交叉引用与索引，Web 控制台可可视化浏览知识图谱。详见 [个人知识库](https://docs.cowagent.ai/zh/knowledge)。

<table>
  <tr>
    <td width="50%">
      <img src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/screenshots/zh/web-console-memory.png" alt="长期记忆" />
      <p align="center"><em>长期记忆 · 三层记忆 + 梦境蒸馏</em></p>
    </td>
    <td width="50%">
      <img src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/screenshots/zh/web-console-knowledge.png" alt="个人知识库" />
      <p align="center"><em>个人知识库 · 自动整理的 Markdown Wiki</em></p>
    </td>
  </tr>
</table>

<br/>


## 🔧 工具与技能

**工具（Tools）** 是 Agent 操作系统资源的原子能力，**技能（Skills）** 是基于说明文件的高级工作流，可组合多个工具完成复杂任务。

### 工具系统

**内置工具** 涵盖文件读写（`read` / `write` / `edit` / `ls`）、终端（`bash`）、文件发送（`send`）、记忆检索（`memory`）、环境变量（`env_config`）、网页获取（`web_fetch`）、定时任务（`scheduler`）、联网搜索（`web_search`）、图像识别（`vision`）、浏览器自动化（`browser`）等常用能力。

**MCP 协议** 通过 [Model Context Protocol](https://modelcontextprotocol.io) 接入开放生态中的各种 MCP 服务，配置一次 `mcp.json` 即用即得，支持 stdio / SSE 协议、热更新、零代码接入。

详见 [工具概览](https://docs.cowagent.ai/zh/tools) · [MCP 集成](https://docs.cowagent.ai/zh/tools/mcp)。

### 技能系统

- **[Skill Hub](https://skills.cowagent.ai/)** — 开源的技能广场，浏览、搜索、一键安装
- **GitHub / ClawHub / URL 等** — 任意来源一键安装
- **对话创造** — 通过 `skill-creator` 用对话快速生成自定义技能，可将工作流程或第三方接口直接固化为技能

```bash
/skill list                   # 查看当前技能
/skill search <关键词>         # 在技能广场搜索
/skill install <名称>          # 一键安装
```

详见 [技能概览](https://docs.cowagent.ai/zh/skills) · [创建技能](https://docs.cowagent.ai/zh/skills/create)。

<br/>

## 🏷 更新日志

> **2026.10.14：** [v2.2.1](https://github.com/zhayujie/CowAgent/releases/tag/2.2.1) — 新增产物中心与自定义菜单、开放 Agent 对话 API、模型缓存与上下文成本优化、工具、通道与安全性完善

> **2026.09.30：** [v2.2.0](https://github.com/zhayujie/CowAgent/releases/tag/2.2.0) — 新增能力中心、多 Agent 协作优化、Web 控制台重构、模型缓存命中率提升、稳定性与安全全面加强

> **2026.09.14：** [v2.1.9](https://github.com/zhayujie/CowAgent/releases/tag/2.1.9) — 多 Agent 协作体验优化、模型列表配置与多兜底模型、通道接入修复、新增图像模型（gpt-image-2.5）、语音修复

> **2026.09.10：** [v2.1.8](https://github.com/zhayujie/CowAgent/releases/tag/2.1.8) — 推出多 Agent 团队、定时任务支持手动创建、上下文用量可视化、模型和搜索工具接入、工作空间文件编辑

> **2026.08.20：** [v2.1.7](https://github.com/zhayujie/CowAgent/releases/tag/2.1.7) — 多工作空间与会话隔离、会话级权限模式、任务通知、桌面端优化、新模型接入

> **2026.08.12：** [v2.1.6](https://github.com/zhayujie/CowAgent/releases/tag/2.1.6) — 子 Agent 并行任务分配、推理强度配置、记忆向量库可插拔、体验和安全优化

> **2026.07.29：** [v2.1.5](https://github.com/zhayujie/CowAgent/releases/tag/2.1.5) — 工作空间与文件预览、基础工具优化（文件搜索、写入校验、命令后台运行）、上下文压缩（`/compact`）、提示词一键优化、安全加固

> **2026.07.20：** [v2.1.4](https://github.com/zhayujie/CowAgent/releases/tag/2.1.4) — 桌面客户端体验优化、MCP 支持 OAuth 授权、飞书通道能力增强、定时任务优化与数据备份、新模型接入

> **2026.07.08：** [v2.1.3](https://github.com/zhayujie/CowAgent/releases/tag/2.1.3) — [桌面客户端](https://cowagent.ai/zh/download/)正式发布（macOS / Windows）、知识库文档管理增强、MCP 工具智能检索、繁体中文支持、新模型接入

> **2026.06.18：** [v2.1.2](https://github.com/zhayujie/CowAgent/releases/tag/2.1.2) — Web 控制台升级（定时任务管理、知识库分类、多模型自定义厂商）、自主进化优化、新模型接入（kimi-k2.7-code、glm-5.2）、安全加固和体验优化

> **2026.06.09：** [v2.1.1](https://github.com/zhayujie/CowAgent/releases/tag/2.1.1) — 自进化能力、Web 控制台升级（消息管理、多会话并行）、新模型接入（MiniMax-M3、qwen3.7-plus）、Python 3.13 支持

> **2026.06.01：** [v2.1.0](https://github.com/zhayujie/CowAgent/releases/tag/2.1.0) — 国际化支持、新增通道（Telegram、Discord、Slack、微信客服）、命令行交互升级、一键安装脚本优化、MCP Streamable HTTP 支持、新模型接入（claude-opus-4-8、MiMo）

完整更新历史：[Release Notes](https://docs.cowagent.ai/zh/releases)

<br/>

## 🤝 社区与支持

微信扫码添加小助手，加入 CowAgent 开源项目交流群：

<img width="130" src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/community/open-community.png" />

也欢迎在 GitHub [提交 Issue](https://github.com/zhayujie/CowAgent/issues)，或加入 [**Discord 服务器**](https://discord.gg/9U8eA8v9TR) 交流：

<a href="https://discord.gg/9U8eA8v9TR"><img src="https://img.shields.io/badge/Discord-加入社区-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="加入 Discord" /></a>

还可通过以下方式获取支持：

- 🐛 [提交 Issue](https://github.com/zhayujie/CowAgent/issues)
- 🤖 在线 AI 助手：[项目小助手](https://link-ai.tech/app/Kv2fXJcH?utm_source=cowagent&utm_medium=readme&utm_campaign=support)（基于项目知识库）

<br/>

## 🔗 相关项目

- **[Cow Skill Hub](https://github.com/zhayujie/cow-skill-hub)** — 开源的 AI Agent 技能广场，支持 CowAgent、OpenClaw、Claude Code 等多种 Agent
- **[bot-on-anything](https://github.com/zhayujie/bot-on-anything)** — 轻量大模型应用框架，支持 Slack、Telegram、Discord、Gmail 等海外平台
- **[AgentMesh](https://github.com/MinimalFuture/AgentMesh)** — 开源多智能体（Multi-Agent）框架，通过团队协同解决复杂问题

<br/>

## 🏢 企业服务

<a href="https://link-ai.tech/?utm_source=cowagent&utm_medium=readme&utm_campaign=enterprise" target="_blank"><img width="650" src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/community/link-ai-intro.png" /></a>

> [LinkAI](https://link-ai.tech/?utm_source=cowagent&utm_medium=readme&utm_campaign=enterprise) 是面向企业和个人的一站式 AI 智能体平台，为 CowAgent 提供云端托管和企业级支持：
>
> - **🚀 免部署在线运行**：无需服务器即可创建 [CowAgent 在线助理](https://link-ai.tech/cowagent/create?utm_source=cowagent&utm_medium=readme&utm_campaign=enterprise)，1 分钟拥有专属 Agent
> - **🧠 Agent 基础设施**：聚合主流大模型、知识库、数据库、技能、工作流，提供开箱即用的 Agent 能力扩展
> - **🏢 企业级协作**：提供团队协作、权限分级、审计日志、私有化部署等能力，让 Agent 安全落地企业场景

**产品咨询和企业服务** 可联系产品客服：

<img width="130" src="https://cdn.jsdelivr.net/gh/zhayujie/cowagent-assets@main/community/customer-service.png" />

<br/>

## 🛠️ 开发与贡献

欢迎各种形式的贡献：新功能、Bug 修复、性能优化、文档完善，或向 [Skill Hub](https://skills.cowagent.ai/submit) 分享你的技能。请先阅读 [CONTRIBUTING.md](../../CONTRIBUTING.md) 了解如何开始，然后提交 Issue 讨论或直接发起 PR。

欢迎 ⭐ Star 支持项目，并通过 Watch → Custom → Releases 订阅新版本通知。也欢迎提交 PR、Issue 进行反馈。

## 🌟 贡献者

![cow contributors](https://contrib.rocks/image?repo=zhayujie/CowAgent&max=1000)

<br/>

## ⚠️ 声明

1. 本项目遵循 [MIT 开源协议](../../LICENSE)，主要用于技术研究和学习。使用时请遵守所在地法律法规及相关政策，因使用本项目所产生的一切后果由使用者自行承担。
2. **成本与安全：** Agent 模式 Token 消耗显著高于普通对话，请根据效果与成本权衡选择模型；Agent 具备访问本地操作系统的能力，请谨慎选择部署环境。
3. CowAgent 项目专注于开源技术开发，不会参与、授权或发行任何加密货币。

<br/>

## 📌 项目更名说明

本项目原名 `chatgpt-on-wechat`，于 2026.04.13 正式更名为 **CowAgent**。原 GitHub 地址已自动重定向，老用户可选择执行 `git remote set-url origin https://github.com/zhayujie/CowAgent.git` 更新本地远程地址。
