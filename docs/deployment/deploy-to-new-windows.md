# 迁移到一台全新 Windows 电脑

本文档面向「目标机器是一台空白 Windows，没有 Python、Node、uv、WSL」的场景，把 StockAnalyser 部署到新机器。

约束：
- 包含 `.env` 配置文件迁移
- **不**迁移本地数据库（`data/stock_analysis.db`、`Sequoia-X/data/sequoia_v2.db` 等）
- **不**部署 graphiti / Neo4j（强制禁用）

## 0. 前置条件（旧机器侧）

在旧机器上准备一个压缩包，至少包含：

```
StockAnalyser/
  ├── .env                    # 当前可用的密钥/配置
  ├── requirements.txt
  ├── apps/dsa-web/
  ├── src/
  ├── scripts/
  └── ... (整个项目源码)
```

排除项（**不要**带过去）：

- `.venv/`、`apps/dsa-web/node_modules/`、`apps/dsa-desktop/node_modules/`
- `data/`、`logs/`、`.cache/`、`Sequoia-X/data/*.db`
- `__pycache__/`、`*.pyc`、`.pytest_cache/`

打包示例（旧机器 PowerShell）：

```powershell
cd <项目父目录>
Compress-Archive -Path StockAnalyser -DestinationPath stockanalyser.zip `
    -Force -CompressionLevel Optimal
# 拷贝 stockanalyser.zip 到 U 盘 / 网盘 / SCP 到新机器
```

或在 WSL/Linux 旧机器：

```bash
tar --exclude='.venv' --exclude='node_modules' \
    --exclude='data' --exclude='logs' --exclude='.cache' \
    --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='Sequoia-X/data/*.db' \
    -czf stockanalyser.tar.gz StockAnalyser/
```

## 1. 新机器 — 安装 WSL2 + Ubuntu

**步骤**（需管理员）：

1. 用「以管理员身份运行」打开 PowerShell。
2. 临时允许脚本执行：
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass -Force
   ```
3. 解压项目到任意位置，例如 `C:\Users\<you>\Downloads\StockAnalyser`。
4. 运行：
   ```powershell
   cd C:\Users\<you>\Downloads\StockAnalyser
   .\scripts\install-windows.ps1
   ```
   该脚本会：
   - 启用 `Microsoft-Windows-Subsystem-Linux` 和 `VirtualMachinePlatform` 两个 Windows 功能
   - 设置 WSL 默认版本为 2
   - 安装 Ubuntu 发行版
5. 按提示重启 Windows（首次启用 WSL 必须重启）。
6. 重启后，再次运行 `install-windows.ps1`（脚本是幂等的）确认 WSL 安装完成。
7. 第一次从开始菜单打开 Ubuntu，按提示设置 Linux 用户名 + 密码。

可选参数：
```powershell
.\scripts\install-windows.ps1 -Distro Ubuntu -SkipReboot
```

## 2. 把项目复制进 WSL 文件系统

**重要**：放在 WSL 的 `~/code/` 下而不是 `/mnt/c/`，否则 IO 会很慢、文件权限混乱。

在 Ubuntu shell 中：

```bash
mkdir -p ~/code
cd ~/code
# 假设你把 zip 放在 Windows 的 Downloads
cp -r /mnt/c/Users/<你的Windows用户名>/Downloads/StockAnalyser .
# 或解压
# unzip /mnt/c/Users/<你>/Downloads/stockanalyser.zip -d .
cd StockAnalyser
ls -la .env requirements.txt scripts/bootstrap-wsl.sh
```

确认 `.env` 已带过来。如果你想重新基于模板创建，可以 `cp .env.example .env` 然后手工填。

## 3. 一键安装 WSL 内依赖

```bash
cd ~/code/StockAnalyser
bash scripts/bootstrap-wsl.sh
```

该脚本会做 9 步：

| 步骤 | 内容 |
| --- | --- |
| 1 | `apt update` + 基础工具（build-essential、curl、git、sqlite3、libssl-dev …） |
| 2 | 安装 Python 3.11（22.04 用 deadsnakes PPA，24.04 默认 3.12 也兼容） |
| 3 | 安装 `uv`（fast pip replacement） |
| 4 | 安装 Node.js 22（NodeSource，对齐 `.nvmrc`） |
| 5 | 创建 `.venv` + 安装 `requirements.txt`（**先 grep 掉 graphiti / neo4j 行**） |
| 6 | `apps/dsa-web` 执行 `npm ci`（可用 `SKIP_NPM=1` 跳过） |
| 7 | 创建空目录 `data/ logs/ Sequoia-X/data/ .cache/candidate_experts_v2/` |
| 8 | 若 `.env` 不存在则从 `.env.example` 复制；强制写入 `GRAPHITI_ENABLED=false` |
| 9 | 可选下载 Sequoia 候选 DB（见下一节） |

可选环境变量：

```bash
SKIP_NPM=1 bash scripts/bootstrap-wsl.sh            # 跳过 npm ci
SKIP_DESKTOP=0 bash scripts/bootstrap-wsl.sh        # 同时安装 apps/dsa-desktop（默认跳过）
SEQUOIA_DB_URL=https://your-host/sequoia_v2.db \
    bash scripts/bootstrap-wsl.sh                   # 下载已有的 Sequoia DB
```

## 4. 配置 `.env`

新机器跑起来前，至少确认以下键已填：

- `TUSHARE_TOKEN`：Tushare Pro Token，候选池/资金面工具依赖
- 至少一个 LLM 密钥：`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GEMINI_API_KEYS`、`XIAOMI_MIMO_KEY` 等其中一个或多个
- `LITELLM_MODEL`（主分析模型）和 `AGENT_LITELLM_MODEL`（Agent 模型）按你的 Key 选模型

确认 `.env` 已包含或自动追加：

```
GRAPHITI_ENABLED=false
```

可用 `python test_env.py` 做 smoke check（不需要 `--graph`，因为没装 Neo4j）。

## 5. 数据库 / 数据目录

`bootstrap-wsl.sh` 只创建空目录，所以新机器开起来时：

- **主分析历史**：`data/stock_analysis.db` 不存在，运行时会自动建库；历史报告从空开始。
- **持仓账本、回测、Agent Trace artifact**：同样从空开始。
- **Sequoia 候选池**：`Sequoia-X/data/sequoia_v2.db` 默认不存在。两条路径：
  1. 通过 `SEQUOIA_DB_URL` 自动下载（见 §3）。
  2. 本地生成：
     ```bash
     source .venv/bin/activate
     python scripts/update_sequoia_candidates.py --help
     # 按需选择 --days / --resume / --force
     ```
- **基本面候选池**：
  ```bash
  source .venv/bin/activate
  python scripts/update_fundamental_candidates.py --help
  ```

如果新机器只是想先把 Web 跑通，不做候选池相关功能，这两个 DB 都可以暂不准备。

## 6. 启动服务

打开两个 WSL shell：

**Shell 1 — 后端：**

```bash
cd ~/code/StockAnalyser
source .venv/bin/activate
bash scripts/start-backend.sh
# 默认 http://0.0.0.0:8000，可通过 BACKEND_PORT 覆盖
```

**Shell 2 — 前端：**

```bash
cd ~/code/StockAnalyser
bash scripts/start-web.sh
# 默认 http://0.0.0.0:5173
```

然后在 **Windows 宿主**用浏览器访问：

- 前端：`http://localhost:5173`
- 后端健康检查：`http://localhost:8000/api/health`

WSL2 默认会把 `0.0.0.0` 端口透传给 Windows，所以无需额外配置端口转发。

> 如果你更想用现有的组合启动脚本，可以用：
> ```bash
> START_NEO4J=false bash start_all.sh
> ```
> `START_NEO4J=false` 这一项**必须**显式带上，因为本部署明确禁用 graphiti。

## 7. 排错

| 现象 | 排查方向 |
| --- | --- |
| `install-windows.ps1` 报 `ERROR: please run this script in an elevated PowerShell` | 必须用「以管理员身份」打开 PowerShell |
| 重启后 `wsl --install` 仍不可用 | 进 BIOS 确认 CPU 虚拟化（Intel VT-x / AMD-V）已开启；并启用 Windows 功能里的「Hyper-V」「虚拟机平台」 |
| `bootstrap-wsl.sh` 在 apt 阶段失败 | 多半是新机器还没换 apt 源；可手动 `sudo apt-get update` 看具体错误 |
| `pip install` 卡住或失败 | 检查 WSL 内代理；或先 `unset http_proxy https_proxy` 重试；必要时用国内镜像（不要写入仓库脚本） |
| `npm ci` 报 `EACCES` | 不要把项目放在 `/mnt/c/` 下；放在 WSL 原生路径 `~/code/` |
| 浏览器访问 5173 打不开 | 确认 `start-web.sh` 用的是 `--host 0.0.0.0`；防火墙允许 WSL 子网；尝试 `http://127.0.0.1:5173` |
| 后端启动后 `/api/health` 一直 5xx | 看 `logs/dev/backend.log`；最常见是 `.env` 缺 LLM Key 或 `TUSHARE_TOKEN` |
| 想清空当前部署重来 | 删 `.venv/`、`apps/dsa-web/node_modules/`、`data/`、`logs/`，重跑 `scripts/bootstrap-wsl.sh` |

## 8. 不在迁移范围内的能力

以下功能在新机器上**默认不可用**，需要你按需自行启用，不在本一键流程范围内：

- Graphiti 知识图谱 / Neo4j（已强制 `GRAPHITI_ENABLED=false`）
- Electron 桌面端（`apps/dsa-desktop`，可 `SKIP_DESKTOP=0 bash scripts/bootstrap-wsl.sh` 安装）
- 任何依赖旧机器历史 DB 的回测复盘 / Trace artifact 复看（历史本来就没带过来）
- 跨机器同步分析任务队列 / 通知渠道状态

## 9. 后续

新机器跑起来后，第一次建议：

1. 用 `python test_env.py` 跑一遍连通性检查
2. 在 Web 设置页确认 LLM 主模型 + Agent 主模型可用
3. 用 `/api/v1/agent/runtime-config` 确认 `AGENT_ORCHESTRATION_MODE` 等运行时配置和预期一致
4. 用一个低风险代码（例如 `600519`）跑一次 watchlist_scan，验证候选池链路与最终报告输出

---

相关脚本：

- `scripts/install-windows.ps1`：Windows 宿主启用 WSL2 + 装 Ubuntu
- `scripts/bootstrap-wsl.sh`：WSL 内一键装 Python 3.11、Node 22、uv、依赖
- `scripts/start-backend.sh`：前台启动 FastAPI 后端
- `scripts/start-web.sh`：前台启动 Vite 前端
- 现有 `start_all.sh`：组合启动器（必须配合 `START_NEO4J=false`）
