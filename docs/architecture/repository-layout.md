# 仓库布局与命名规范

本文定义 StockAnalyser 的顶层目录边界、第三方仓库规则和文件命名约定。新增目录或迁移路径时先对照本文，避免再次出现“代码、第三方仓库、运行数据、文档混在同一层”的问题。

## 1. 顶层布局

```text
StockAnalyser/
├── main.py server.py analyzer_service.py webui.py   # 可执行入口
├── src/ api/ bot/ data_provider/ patch/              # 业务代码
├── apps/dsa-web/ apps/dsa-desktop/                   # Web / Desktop 客户端
├── tests/                                            # pytest 回归测试
├── docs/                                             # 文档、示例、品牌素材
├── scripts/ docker/ .github/                         # 本地脚本、部署、CI
├── strategies/ templates/                            # 业务策略与报告模板
├── vendor/                                           # 第三方项目（只读）
├── data/ logs/ reports/                              # 运行产物（不提交）
├── AGENTS.md README.md LOOP.md STATE.md loop-*.md    # 治理与协作资产
└── .env.example requirements*.txt pyproject.toml ...  # 配置与依赖
```

## 2. 目录职责

| 目录 | 职责 | 约束 |
| --- | --- | --- |
| `src/` | 主流程、服务、仓储、Schema、报告 | 业务逻辑唯一落点，不向 `vendor/` 写代码 |
| `src/backtest/` | 组合回测内核、快照、账本、指标、worker、API 服务 | 纯内核不访问网络/LLM/Portfolio 真账本；实验产物写入 `data/backtest_runs/` |
| `api/` | FastAPI 路由与请求/响应 Schema | 与 `src/` 通过服务层交互 |
| `bot/` | 机器人接入 | 不复制业务实现 |
| `data_provider/` | 多数据源适配与 fallback | 单源失败不影响主流程 |
| `apps/dsa-web/` | React / Vite 前端 | 构建产物写入 `static/` |
| `apps/dsa-desktop/` | Electron 桌面端 | 复用 Web 构建产物 |
| `tests/` | 后端与前端回归测试 | 测试文件统一 `test_*.py` / `*.test.ts(x)` |
| `docs/` | 架构、模块、部署、示例、素材 | 只放文档与静态素材 |
| `scripts/` | 本地开发、安装、构建、数据维护脚本 | 不承载长期运行的服务逻辑 |
| `vendor/` | 第三方仓库 | 只读引用，升级需单独评审 |
| `data/` | SQLite、Trace、回测数据、`backtest_runs/` 实验快照/账本 | 不提交；备份/迁移单独处理 |
| `logs/` | 运行日志 | 不提交；旧日志按月归档 |
| `reports/` | 生成的报告产物 | 不提交；旧报告按年归档 |

## 3. vendor/ 第三方仓库

| 路径 | 引入方式 | 用途 |
| --- | --- | --- |
| `vendor/Kronos` | 本地克隆（gitignore） | Kronos K 线预测模型与示例 |
| `vendor/Sequoia-X` | git submodule | Sequoia 候选池与 `stock_daily` 数据 |
| `vendor/alphasift` | git submodule | AlphaSift 策略 YAML |
| `vendor/graphiti` | git submodule / editable 依赖 | Graphiti 时序知识图谱 |
| `vendor/openInvest` | 本地克隆（gitignore） | 外部投资委员会项目参考实现 |

规则：

- `vendor/` 下不提交业务改动；需要定制时在 `src/` 新增适配层或 provider。
- 三个 submodule 通过 `.gitmodules` 固定到具体 commit，不自动跟随上游。
- 初始化或同步 submodule：

```bash
git submodule update --init --recursive
git submodule status
```

- `requirements.txt` 通过 `-e ./vendor/graphiti` 安装 Graphiti；Dockerfile 显式 `COPY vendor/graphiti/`。
- 新增第三方依赖时，优先使用包管理器；确需 vendoring 时放入 `vendor/<name>/`，并在本文登记。

## 4. 运行产物

- `data/`、`logs/`、`reports/` 保持在仓库根目录，便于本地脚本、Docker volume 和现有配置复用。
- 三者均被 `.gitignore` 忽略，禁止提交数据库、日志和报告。
- 旧日志归档到 `logs/archive/<YYYY-MM>/`，旧报告归档到 `reports/archive/<YYYY>/`。
- 清理缓存时只删除可再生成的内容（`__pycache__`、`.pytest_cache`、`.DS_Store`），不动 `data/` 下的数据库。

## 5. 命名规范

| 类型 | 规则 | 示例 |
| --- | --- | --- |
| Python 模块 / 包 | `snake_case` | `candidate_pool_store.py` |
| Python 测试 | `test_<module>.py` | `test_signal_lifecycle.py` |
| Shell / PowerShell | `kebab-case` | `build-desktop-macos.sh`、`run-desktop.ps1` |
| 文档目录 | `kebab-case` | `docs/serenity-candidates/` |
| 文档文件 | `kebab-case`；中文文档可用中文，但用 `-` 连接 | `kronos-overview.md`、`选股链路重构-实施方案.md` |
| 品牌 / 截图素材 | `kebab-case` 或保留上游文件名 | `docs/assets/dsa-vi/` |
| 运行报告 | `<type>_YYYYMMDD.md` | `report_20260721.md` |
| 第三方仓库 | 保留上游目录名 | `vendor/Sequoia-X/` |

禁止：

- 目录或文档名包含空格、制表符。
- 新增 `CamelCase` 或 `snake_case` 的文档目录（历史 `docs/paper/`、`docs/report/` 可保留原名）。
- 在根目录新增业务模块、临时脚本或数据文件；脚本放 `scripts/`，数据放 `data/`。

## 6. 迁移对照

| 旧路径 | 新路径 |
| --- | --- |
| `Kronos/` | `vendor/Kronos/` |
| `Sequoia-X/` | `vendor/Sequoia-X/` |
| `alphasift/` | `vendor/alphasift/` |
| `graphiti/` | `vendor/graphiti/` |
| `openInvest/` | `vendor/openInvest/` |
| `sources/` | `docs/assets/` |
| `review.md` | `docs/reviews/2026-03-19-project-review.md` |
| `docs/kronos/class imbalance.md` 等含空格文件 | `docs/kronos/class-imbalance.md` 等 |
| `docs/SerenityCandidates/` | `docs/serenity-candidates/` |

相关配置默认值已同步：

- `KRONOS_REPO_PATH` 默认 `vendor/Kronos`
- `SEQUOIA_CANDIDATE_DB_PATH` 默认 `vendor/Sequoia-X/data/sequoia_v2.db`
- `ALPHASIFT_STRATEGY_DIR` 默认 `vendor/alphasift/alphasift/strategies`

已有 `.env` 中的显式绝对路径仍然优先；迁移机器时需要按新布局检查这些覆盖值。

## 7. 校验

```bash
git submodule status
python -m pytest -m "not network" --ignore=vendor
./scripts/ci_gate.sh
docker build -f docker/Dockerfile .
```

Docker 构建需要能访问 `vendor/graphiti`；本地未安装 Docker 时，至少在交付说明中标注该项未验证。
