# As-built 架构说明（单一入口）

- 状态：按当前代码核对（2026-11-17）
- 适用范围：仓库整体链路、模块边界、关键契约、失败降级与验证方式
- 口径：**以代码实际行为为准**。本文只描述"现在是什么"，不描述"计划做什么"；设计动机与演进方案见文末关联文档。

## 1. 怎么用这份文档

- 想快速定位"某个能力在哪个文件"：看第 2 节链路和第 3 节模块边界。
- 想确认"配置从哪来、数据源怎么降级、报告/API 契约是什么"：看第 4 节。
- 想确认"某一步失败会不会拖垮主流程"：看第 5 节降级矩阵。
- 想跑起来或判断 CI 覆盖了什么：看第 6 节。
- 想知道哪些能力只在特定条件下成立：看第 7 节已知缺口。

关联文档：

- [仓库布局与命名规范](repository-layout.md)
- [选股链路说明](stock-selection-pipeline.md)
- [Agent 候选池多专家架构方案](agent-candidate-expert-architecture.md)
- [完整指南](../full-guide.md)
- [更新日志](../CHANGELOG.md)

## 2. 整体链路

主链路是"数据抓取 → 选股/候选 → 分析 → 报告 → 通知"，由 `main.py` 或 `server.py` 触发，核心编排在 `src/core/pipeline.py`。

### 2.1 入口

| 入口 | 文件 | 启动方式 | 职责 |
| --- | --- | --- | --- |
| CLI 主入口 | `main.py` | `python main.py` | 解析参数、交易日判断、并发调度、个股分析、大盘复盘、报告与通知、飞书文档、自动回测 |
| API 服务 | `server.py` → `api/app.py` | `python server.py` / `python main.py --serve` / `--serve-only` | FastAPI 应用、`/api/v1` 路由、鉴权、静态前端托管 |
| 定时任务 | `src/scheduler.py` | `python main.py --schedule` | 每日任务、后台任务、优雅退出 |
| Bot | `bot/dispatcher.py`、`bot/platforms/` | API 服务启动后由 `main.py:start_bot_stream_clients` 拉起 | 飞书/钉钉/Discord 长连接、命令分发、限流 |
| Web 前端 | `apps/dsa-web/` | `npm run dev` / `npm run build` | 管理页面、信号预览、Agent Trace、回测与复盘 |
| 桌面端 | `apps/dsa-desktop/` | `npm run build` | Electron 壳，加载本机后端 `http://127.0.0.1:<port>/` |

### 2.2 数据抓取

- 编排：`src/core/pipeline.py:fetch_and_save_stock_data` / `process_single_stock`。
- 数据源管理：`data_provider/base.py` 的 `DataFetcherManager`，按 `priority` 升序尝试，失败切换下一个源，并带熔断（circuit breaker）。
- 具体源：`data_provider/*_fetcher.py`（efinance、akshare、pytdx、tushare、baostock、yfinance、longbridge、tickflow）。
- 落库：`src/repositories/stock_repo.py`；本地运行产物写入 `data/`、`logs/`。
- 实时行情：独立于日线链路，按 `REALTIME_SOURCE_PRIORITY` 排序，支持全市场预取（股票数 ≥5 时）。

### 2.3 选股 / 专家打分

- 主编排：`src/agent/candidate_experts_v2/committee.py`。
  1. 构建 seed pool（本地量价、估值、资金流、涨停池、龙虎榜、主题/新闻、外部 provider 等）；
  2. 用 `priority_score` 排序并截断，控制进入席位的候选量；
  3. 各 desk 读取 `FactSheet` 做入席过滤，由 LLM 给出支撑/观察/反对与理由；
  4. `aggregator.py` 按多席共识、置信度、regime 配额生成最终候选与深挖名单。
- 量化事实：`fact_sheet.py` 计算 MA、RSI14、区间位置、量比、5 日涨幅、偏离度、流动性等。
- 否决：`veto_gate.py` 在 seed facts 之后、desks 之前执行，**默认关闭**（`AGENT_VETO_GATE_ENABLED=false`）；开启后按 hard risk / 可选软红线 / 可选流动性否决（详见第 7 节）。
- 策略技能：`strategies/*.yaml` 由 `src/agent/skills/base.py` 加载，仅作为 LLM prompt 的策略说明，不是可执行的量化条件。
- 单股深挖与最终判断：`src/agent/` 内 deep-dive、Judge、Meta-Agent；入口由 `pipeline._analyze_with_agent` 调度。
- 旧版选股：`src/agent/stock_selection.py` 等仍保留，是否启用取决于配置与调用方。

### 2.4 分析

- LLM 分析：`src/analyzer.py` 的 `GeminiAnalyzer.analyze`（通过 LiteLLM 路由多模型）。
- Prompt 组成：系统提示 + 市场上下文 + 个股数据 + 技能片段（`_get_skill_prompt_sections`）。
- 新闻/检索：`src/search_service.py`、`src/market_context.py`。
- 输出校验：`src/schemas/report_schema.py`（Pydantic），`analyzer.py` 内做完整性检查、缺失字段补全与重试。
- 结果模型：`src/analyzer.py:AnalysisResult`，再转成 `AnalysisResult`/报告结构。

### 2.5 报告

- 聚合报告：`src/notification.py` 的 `NotificationService.generate_aggregate_report`，按 `report_type`（simple/brief/full）分支。
- 格式化：`src/formatters.py`。
- 本地留档：`src/core/pipeline.py:_save_local_report` → `reports/`。
- 结构化契约：`src/schemas/report_schema.py`、`src/schemas/composite_verdict.py`、`src/schemas/agent_signal.py`。

### 2.6 通知

- 统一入口：`src/notification.py`（渠道检测、Markdown 转图片、按渠道裁剪）。
- 渠道实现：`src/notification_sender/*.py`，覆盖企业微信、飞书、Telegram、邮件、Pushover、PushPlus、Server酱3、自定义 Webhook、Discord、Slack、AstrBot。
- 发送策略：汇总推送或单股推送（`single_stock_notify`）；Markdown 转图片失败时回退文本。
- 每个渠道独立捕获异常，单渠道失败不影响其他渠道。

## 3. 模块边界

| 目录 | 负责 | 不负责 |
| --- | --- | --- |
| `main.py` / `server.py` | 进程入口、参数解析、服务启动、模式切换 | 具体业务实现 |
| `src/core/` | 主流程编排、调度、交易日历、市场复盘与策略、配置管理 | 数据源细节、LLM 调用细节 |
| `src/services/` | 业务服务（回测服务、新闻信号、Agent 相关服务等） | HTTP 路由、页面渲染 |
| `src/repositories/` | 本地/SQL 数据访问 | 业务判断 |
| `src/schemas/` | Pydantic 契约与校验 | 业务编排 |
| `src/agent/` | 候选召回、专家席位、聚合、深挖、Judge、技能加载 | 数据抓取、通知发送 |
| `src/backtest/` | 回测引擎、规则、因子评估器 | 实盘选股编排 |
| `src/notification*.py` | 报告生成、渠道分发 | 分析结论生成 |
| `data_provider/` | 多数据源适配、优先级、熔断、字段标准化 | 业务决策、报告格式 |
| `api/` | FastAPI 应用、路由、鉴权、请求/响应模型 | 业务逻辑实现 |
| `bot/` | 消息平台接入、命令分发、限流 | 分析主流程 |
| `apps/dsa-web/` | Web 管理端 | 后端计算 |
| `apps/dsa-desktop/` | Electron 桌面壳与本地后端拉起 | 业务逻辑 |
| `scripts/` | 运维、构建、评估、检查脚本 | 运行时主链路 |
| `vendor/` | 第三方项目只读引用 | 业务代码、适配层、测试 |

## 4. 关键契约

### 4.1 配置入口

- 环境变量：`.env` 由 `src/config.py:setup_env` 加载；`get_config()` 返回单例 `Config`。
- 配置项真源：`src/core/config_registry.py`（名称、默认值、类型、说明）；新增配置必须同步 `.env.example` 与相关文档。
- 运行时覆盖：`main.py` 支持从当前 `.env` 重载，并保留进程环境变量优先级。
- 在线配置：`/api/v1/system` 由 `api/v1/endpoints/system_config.py` 提供。
- 因子评估配置：`src/backtest/factor_eval/factors.yaml`，与运行配置分离；`status`/`note` 是登记元数据，不参与 `definition_hash`。

### 4.2 数据源优先级与降级

默认优先级（数字越小越优先，均可通过环境变量覆盖）：

| 数据源 | 默认优先级 | 说明 |
| --- | --- | --- |
| efinance | 0 | `EFINANCE_PRIORITY` |
| akshare | 1 | `AKSHARE_PRIORITY` |
| pytdx | 2 | `PYTDX_PRIORITY` |
| tushare | 2（动态） | `TUSHARE_PRIORITY`；配置 Token 时按配置动态调整 |
| baostock | 3 | `BAOSTOCK_PRIORITY` |
| yfinance | 4 | `YFINANCE_PRIORITY` |
| longbridge | 5 | `LONGBRIDGE_PRIORITY` |
| tickflow | 99 | 仅特定场景（如大盘指数）优先 |

降级行为：

- 单个源失败 → 记日志并切换下一优先级；连续失败触发熔断，熔断期内跳过该源。
- 字段级结果可能是 `success` / `partial` / `failed` / `not_supported`；调用方必须按状态处理，不能把缺失当 0。
- 实时行情走独立优先级 `REALTIME_SOURCE_PRIORITY`；配置 Tushare Token 且未显式设置时，`tushare` 会自动插到最前。
- 换源可能改变复权、成交量口径或字段覆盖范围；跨源结果不做静默拼接。

### 4.3 报告结构

- 单股结果：`AnalysisResult`（核心结论、趋势、价格位置、量能、风险、操作建议、评分等）。
- 聚合报告：`NotificationService.generate_aggregate_report` 生成决策仪表盘；`report_type` 控制详略。
- 结构化校验：`src/schemas/report_schema.py`；LLM 输出缺字段时先补全/重试，仍缺失则占位并记录。
- 留档：本地 `reports/`；API 通过 `stocks` / `signal-preview` / `agent` 等路由读取；Bot 与 Web 复用同一份报告文本。

### 4.4 API 与 Web / Desktop 对接

- 基址：`/api/v1`，聚合在 `api/v1/router.py`。
- 主要路由：`auth`、`agent`、`candidate-pool`、`seed-pool-quality`、`agent-verdict-reviews`、`signal-preview`、`agent-entry-execution-backtests`、`news-signals`、`kronos`、`stocks`、`backtest`、`system`、`usage`、`portfolio`。
- 鉴权：`api/v1/endpoints/auth.py` + `src/auth.py`；Web 登录态与 Cookie 由前端 `contexts/` 管理。
- 静态托管：生产模式下后端托管 Web 构建产物；`main.py --serve` 启动前会检查/构建前端资源。
- 桌面端：Electron 启动本地后端后 `loadURL("http://127.0.0.1:<port>/")`，因此桌面端依赖本机后端可用。

## 5. 失败与降级矩阵

| 环节 | 失败情形 | 实际行为 | 是否中断主流程 |
| --- | --- | --- | --- |
| 交易日检查 | 非交易日 | 默认跳过；`--force-run` 强制运行 | 正常跳过，不报错 |
| 单股数据抓取 | 所有源都失败 | 该股票记失败，其他股票继续 | 否 |
| 单数据源 | 超时/限流/字段缺失 | 切换下一优先级、熔断、字段状态降级 | 否 |
| 单股 LLM 分析 | 所有模型失败（`_AllModelsFailedError`） | 该股票失败，不计入汇总 | 否 |
| LLM 输出不完整 | 缺字段/JSON 异常 | 完整性重试、占位补全、记录日志 | 否（可能降级报告质量） |
| 专家席位 | 单 seed 超时/失败 | 跳过该 seed；连续超时提前结束该席 | 否 |
| 委员会整体 | 超过整体时限 | 返回已有结果或降级结果 | 否 |
| 报告生成 | 聚合报告异常 | 捕获并记录，通知阶段跳过 | 否 |
| 通知 | 单渠道失败 | 记录日志，继续其他渠道 | 否 |
| 推送图片 | Markdown 转图失败 | 回退文本发送 | 否 |
| 大盘复盘 | 数据或 LLM 失败 | 捕获并记录，个股结果仍输出 | 否 |
| 飞书云文档 | 创建失败 | 捕获并记录 | 否 |
| 自动回测 | 执行失败 | 捕获并记录，不影响当日分析 | 否 |
| 定时任务 | 任务异常 | `Scheduler._safe_run_task` 捕获，进程继续 | 否 |
| API 服务 | 前端资源未就绪 | 仍启动 API，页面可能不可用 | 服务可用性降级 |
| Web / Desktop | 后端不可达 | 前端报错；桌面端显示错误页 | 是（该客户端不可用） |
| 因子评估 | 基准缺失/样本不足/覆盖不足 | 三态判定 `not_evaluable`，不出结论 | 独立任务，不影响主链路 |

## 6. 验证与运行

### 6.1 常用命令

```bash
python main.py
python main.py --debug
python main.py --dry-run
python main.py --stocks 600519,00700,AAPL
python main.py --market-review
python main.py --schedule
python main.py --serve
uvicorn server:app --reload --host 0.0.0.0 --port 8000

./scripts/ci_gate.sh
python -m pytest -m "not network"
python -m py_compile <changed_python_files>

cd apps/dsa-web && npm ci && npm run lint && npm run build
cd apps/dsa-desktop && npm install && npm run build
```

### 6.2 CI 实际覆盖

| Job | 触发 | 实际检查 |
| --- | --- | --- |
| `ai-governance` | 每次 PR | `python scripts/check_ai_assets.py` |
| `backend-gate` | 后端相关改动 | `ci_gate.sh syntax` / `flake8` / `deterministic` / `offline-tests` |
| `docker-build` | Docker/依赖相关 | 镜像构建 + 关键模块导入 smoke |
| `web-gate` | `apps/dsa-web` 改动 | `npm ci` + `npm run lint` + `npm run build` |
| `network-smoke` | 定时/手动 | 网络测试与 quick smoke，非阻断 |
| `pr-review` | PR | 静态检查、AI 审查、自动标签，辅助 |

未覆盖：Desktop 打包、真实行情端到端、LLM 在线稳定性、通知渠道实发。

### 6.3 一次成功运行的验收项

1. 交易日判断通过，日志出现股票列表与并发数；
2. 每只股票至少一个数据源返回可用日线，失败股票有明确原因；
3. 选股/候选阶段产出非空候选，或明确说明当日无候选；
4. LLM 分析结果通过 schema 校验，核心结论与操作建议非占位；
5. 聚合报告生成成功，本地 `reports/` 有对应文件；
6. 至少一个通知渠道发送成功，或日志明确记录所有渠道失败原因。

以上为功能性验收，不代表收益、胜率或 SLA；性能与效果需单独评估。

## 7. 已知缺口（已核对代码）

1. **否决门默认关闭**：`apply_veto` 已接入 `run_thesis_desk_committee`（seed facts 之后、desks 之前），但 `AGENT_VETO_GATE_ENABLED` 默认 false，行为与接线前一致；开启后，`hard_risk_flags` 非空即否决，`AGENT_VETO_ENFORCE_LIQUIDITY=true` 时额外否决 `liquidity_ok=False`。`hard_risk_flags` 由 recall 上游的 `hard_risk.build_hard_risk_snapshot` 填充（ST/退市/停牌），每个 flag 的 `source`/`as_of`/`detail` 记录在 `FactSheet.hard_risk_evidence`；来源缺失时记 `hard_risk_unknown`，不默认放行。
2. **`gain_5d` 已降级为事实字段**：动量席默认不再用 `gain_5d >= 5%` 入席，`AGENT_MOMENTUM_GAIN5D_GATE=true` 仅用于与旧行为做 A/B 对比；`momentum_5d` 仍是 fail，`gain_5d` 只在提示中展示。
3. **策略 YAML 不是可执行策略**：`strategies/*.yaml` 只把自然语言规则注入 LLM prompt；`sentiment_score ±N` 是文案，不是代码分支。默认仅 `bull_trend` 激活。
4. **流动性否决默认关闭**：`FactSheet.liquidity_ok` 已计算，但 `AGENT_VETO_ENFORCE_LIQUIDITY` 默认 false；即使否决门开启，流动性也不构成红线，除非显式打开该开关。
5. **硬风险数据依赖本地缓存/在线补数**：`stock_basic` 读 `data/factor_eval_cache/stock_basic_LD.csv`；`namechange`、`suspend_d` 使用同目录快照并在缺失时由委员会链路 best-effort 在线刷新。任一来源不可用时对应 flag 为 `unknown` 而非 `clear`；停牌判定只在 `suspend_d` 参考交易日命中时触发，本地日线落后仅作辅助证据。
6. **口径分层**：选股链路（本地日线、无统一 T+1/成本/ST 处理）与因子评估器（非重叠 cohort、T+1 开盘、20bp、中证全指基准）不是同一把尺子，选股侧结论不能直接引用评估器判定。
7. **数据源换源不等于同口径**：不同源的复权、成交量、字段覆盖可能不同；字段级 `partial` 需要调用方自行处理。
8. **外部策略是黑盒**：`alphasift`、`sequoia` 的因子在 `vendor/` 内，只能通过候选结果观察，无法直接按统一协议评估。
9. **多路由同前缀**：`api/v1/router.py` 中 `backtest` 与 `backtest_runs` 都挂在 `/backtest` 前缀，新增路由时需确认路径不冲突。
10. **桌面端依赖本机后端**：Electron 加载 `127.0.0.1`，后端未启动时只能显示错误页，不是纯离线应用。

## 8. 维护约定

- 本文是架构单一入口；链路细节优先更新 `stock-selection-pipeline.md`、`repository-layout.md`，再回来改本文的导航与边界。
- 代码与本文不一致时，以代码为准，并同步修正本文。
- 不把易变数值（阈值、超时、权重）写死在本文；需要时指向代码常量或配置文件。
- 用户可见行为、部署方式、报告结构变化时，同步更新 `docs/CHANGELOG.md`。
