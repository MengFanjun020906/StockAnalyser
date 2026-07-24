# 迁移到一台全新 Mac 电脑

本文档面向「目标机器是一台空白 Mac，没有 Homebrew、Python、Node、uv」的场景，把 StockAnalyser 部署到新机器。

约束（与 [迁移到一台全新 Windows 电脑](./deploy-to-new-windows.md) 一致）：
- 包含 `.env` 配置文件迁移
- **不**迁移本地数据库（`data/stock_analysis.db`、`Sequoia-X/data/sequoia_v2.db` 等）
- **不**部署 graphiti / Neo4j（强制禁用）

支持的 Mac：
- Intel（x86_64） — 默认目标，Homebrew 装在 `/usr/local`
- Apple Silicon（arm64） — 脚本自动识别，Homebrew 装在 `/opt/homebrew`

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

打包示例（macOS 或 Linux 旧机器）：

```bash
cd <项目父目录>
tar --exclude='StockAnalyser/.venv' \
    --exclude='StockAnalyser/apps/dsa-web/node_modules' \
    --exclude='StockAnalyser/apps/dsa-desktop/node_modules' \
    --exclude='StockAnalyser/data' \
    --exclude='StockAnalyser/logs' \
    --exclude='StockAnalyser/.cache' \
    --exclude='StockAnalyser/Sequoia-X/data/*.db' \
    --exclude='*/__pycache__' --exclude='*/.pytest_cache' \
    -czf stockanalyser.tar.gz StockAnalyser
# 拷贝 stockanalyser.tar.gz 到 U 盘 / 网盘 / scp 到新机器
```

旧机器在 Windows PowerShell：

```powershell
cd <项目父目录>
Compress-Archive -Path StockAnalyser -DestinationPath stockanalyser.zip `
    -Force -CompressionLevel Optimal
```

## 1. 新 Mac 准备

1. 系统设置 → 通用 → 软件更新，把 macOS 升到最新次版本，避免 Xcode CLT 装不上。
2. 打开「终端」（`Cmd + Space`，输入 `Terminal` 回车）。
3. 把上一步的 `stockanalyser.tar.gz` 放到比如 `~/Projects/` 下：

   ```bash
   mkdir -p ~/Projects && cd ~/Projects
   # 假设 stockanalyser.tar.gz 在下载目录
   tar -xzf ~/Downloads/stockanalyser.tar.gz
   cd StockAnalyser
   ```

   或者直接 `git clone`：

   ```bash
   mkdir -p ~/Projects && cd ~/Projects
   git clone https://github.com/MengFanjun020906/StockAnalyser.git
   cd StockAnalyser
   # 别忘了手动把旧机器的 .env 拷过来
   ```

## 2. 一键安装依赖

```bash
cd ~/Projects/StockAnalyser
bash scripts/install-mac.sh
```

脚本会按顺序：

1. 检查 Xcode Command Line Tools（缺失时弹出系统安装窗口，安装完后重跑脚本）。
2. 装 Homebrew，并把 `brew shellenv` 写进 `~/.zprofile` 或 `~/.bash_profile`。
3. `brew install python@3.11 node@22 uv git sqlite`。
4. 用 Python 3.11 建 `.venv`，并 `uv pip install`（自动剔除 `graphiti`、`neo4j` 行）。
5. 在 `apps/dsa-web/` 跑 `npm ci`。
6. 建空目录 `data/`、`logs/`、`Sequoia-X/data/`、`.cache/candidate_experts_v2/`。
7. 没有 `.env` 时 `cp .env.example .env`，并强制追加 `GRAPHITI_ENABLED=false`。
8. 可选下载 Sequoia 候选 DB（设置 `SEQUOIA_DB_URL` 后才会触发）。
9. 给 `scripts/start-*.sh` / `scripts/start-*.command` 加可执行位。

常用环境变量覆盖：

```bash
SKIP_NPM=1 bash scripts/install-mac.sh          # 暂时不装前端
SKIP_DESKTOP=0 bash scripts/install-mac.sh      # 也装 dsa-desktop 依赖
SEQUOIA_DB_URL=https://example.com/sequoia_v2.db bash scripts/install-mac.sh
```

## 3. 填写 .env

如果是从旧机器拷过来的 `.env`，跳过本步；否则编辑 `.env` 填好至少：

- `TUSHARE_TOKEN=...`
- `DEEPSEEK_API_KEY=...`（或其他 LLM 密钥）
- 保留 `GRAPHITI_ENABLED=false`

## 4. 启动后端 + 前端

打开两个终端窗口（或两个 Terminal 标签页）。

**终端 A — 后端**：

```bash
cd ~/Projects/StockAnalyser
source .venv/bin/activate
bash scripts/start-backend.sh
# 监听 http://0.0.0.0:8000，日志写到 logs/dev/backend.log
```

**终端 B — 前端**：

```bash
cd ~/Projects/StockAnalyser
bash scripts/start-web.sh
# 监听 http://0.0.0.0:5173
```

浏览器打开 <http://localhost:5173>。

### 不想敲命令？双击启动

Finder 进入 `scripts/`，双击：

- `start-backend.command` — 启动后端
- `start-web.command` — 启动前端

第一次双击如果 macOS 提示「无法打开未验证开发者的文件」，在「系统设置 → 隐私与安全性」点「仍要打开」即可。`.command` 文件内部会自动 `eval brew shellenv`，所以即使从 Finder 启动也能找到 brew 装的 Python / Node。

## 5. 排障速查

| 现象 | 原因 / 处理 |
| --- | --- |
| `xcode-select: error: ...` | 先在弹出的图形窗口装完 Command Line Tools，然后重跑 `bash scripts/install-mac.sh` |
| `brew: command not found`（新开终端后） | 跑一次 `source ~/.zprofile`（zsh）或 `source ~/.bash_profile`（bash），或重启终端 |
| `python3.11: command not found` | `brew --prefix python@3.11` 看看是否装上；脚本使用 `$(brew --prefix python@3.11)/bin/python3.11` 而不依赖 PATH |
| `node -v` 不是 v22 | 跑 `brew link --overwrite --force node@22`，然后 `which node` 应指向 `$(brew --prefix node@22)/bin/node` |
| 前端打不开 | 确认终端 B 还在跑 vite，且没被防火墙拦住 `5173` 端口；后端代理目标见 `apps/dsa-web/vite.config.ts` |
| `.env` 不生效 | 后端通过 `python main.py --serve-only` 启动，会自动加载项目根的 `.env`；改完需要重启后端 |
| 想关闭 agent 模式 | `AGENT_MODE=false bash scripts/start-backend.sh` |

## 6. 不在本流程范围内的事

- **数据库迁移**：旧机器的 `data/stock_analysis.db` 和 `Sequoia-X/data/sequoia_v2.db` 不会带过来。新机器空库启动后会按需重新生成。
- **graphiti / Neo4j**：脚本强制 `GRAPHITI_ENABLED=false`，并在 `requirements.txt` 安装阶段直接 `grep -v` 掉 `graphiti` / `neo4j` 行。如果某天确实需要，请单独走 graphiti 的安装文档，而不是改本脚本。
- **桌面端（dsa-desktop）**：默认 `SKIP_DESKTOP=1`，需要时再 `SKIP_DESKTOP=0 bash scripts/install-mac.sh`。
- **CI / 自动 tag / Docker**：仅影响本地开发环境，不动远端流水线。
