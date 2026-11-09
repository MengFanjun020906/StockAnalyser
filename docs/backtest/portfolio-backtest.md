# 组合回测（P0 / P3）使用与实现说明

状态：P0-A/B/C 已实现（`recorded_plan_replay`），但 Web 创建入口已下线，仅保留 API/历史只读兼容；P3 历史 Agent 研究为默认创建模式（`historical_agent_research`）。P2 历史规则策略未实现。

完整设计目标与硬门槛见 [`docs/plans/agent-backtest-system-design.md`](../plans/agent-backtest-system-design.md)，HTTP 契约见 [`docs/api/backtest-runs.md`](../api/backtest-runs.md)。本页说明当前可执行边界、配置方式、数据合同和运维入口。

## 1. 已实现的边界

> 产品层：Web 回测中心只提供 P3 创建入口，默认 P3；P0 不再出现在创建表单中。P0 的 API、`PlanAdapter`、已有 run 详情/结果保留，用于兼容和历史查看；P0 仍可通过 API 创建，但不作为默认产品流程。

- 只做 A 股、只做多、空仓开始；不支持融资融券、ETF T+0、集合竞价队列和逐笔成交。
- P0 输入只接受结构化价格 artifact + account decision + Risk Gate 结果；旧 Trace 自然语言计划进入排除清单，不做猜测成交。
- P3 不消费历史执行计划：显式标的池 + 月度 as-of 决策，选股/确认 Agent 在回测内生成计划，价格由确定性研究计划代码生成。
- 资金账户：现金、冻结、批次持仓、T+1、佣金最低收费、过户费、印花税、滑点、成交量参与率、部分成交、期末盯市。
- 数据：不可变 per-run 快照（SQLite）、manifest、覆盖/缺口、公司行为、交易规则、基准；纯回测不访问网络。
- 并发：中央 SQLite 只存 run 元数据/租约；每个 run 独立 SQLite 快照与账本；多进程 worker 并行执行。
- 任务：持久队列、租约、心跳、取消、中断恢复、fingerprint 复用。
- 产品：`/api/v1/backtest/runs` API、预检/分钟线同步 job、Web 回测中心。
- P0 不读 Portfolio 真账本，不修改真实账户，不自动启用托管。

## 2. 两种样本来源（`source_pipeline`）

| pipeline | 含义 | 可执行条件 |
| --- | --- | --- |
| `single_stock` | 单股询问直接产生结构化计划 | 有结构化 artifact + account decision |
| `selection_confirmed` | 选股 Trace → 单股确认 → 结构化计划 | 必须有显式 `selection_links` 父链；缺链排除 |
| `research` | P3 历史 Agent 研究；计划在回测内生成 | 必须 `mode=historical_agent_research` + `research` 配置 |
| `any` | 仅保留为适配器诊断枚举 | 实验禁止混算，`ExperimentConfig` 会拒绝 |

“选股/单股”是 P0 的样本来源，不是三种回测 `mode`。P0 实现 `recorded_plan_replay`，P3 实现 `historical_agent_research`。

## 3. 风险档位与资金竞争

风险策略会冻结进实验配置（`RiskPolicy`），不是读取实时画像。默认档位：

| 档位 | 单票上限 | 总仓位上限 | 最大持仓 | 最低现金 | 单笔风险预算 | 选股 Top N |
| --- | --- | --- | --- | --- | --- | --- |
| conservative | 10% | 40% | 5 | 20% | 0.5% | 10 |
| balanced | 20% | 80% | 5 | 10% | 1.0% | 10 |
| aggressive | 40% | 100% | 3 | 0% | 2.0% | 10 |

下单时按以下顺序取最小值：

```text
target = equity × min(plan.target_position_pct, risk_policy.max_single_position_pct)
risk_cap = equity × risk_per_trade_pct / ((entry_high - stop_loss) / entry_high)
approved = min(target, risk_cap, 总仓位剩余额度, 可用现金)
```

同时受 `max_concurrent_positions` 和 `min_cash_pct` 限制；超限计划进入 `max_positions_reached` / `insufficient_cash` 等排除/拒绝原因。实际请求仓位、批准仓位和 cap 原因记录在 plan state 中。

## 4. 数据合同

- 记录区分 `event_time` / `available_at` / `ingested_at`；策略只能通过 as-of 视图看到 `available_at <= clock` 的数据。
- 5 分钟 bar 时间戳统一为区间结束（09:35…11:30、13:05…15:00，共 48 根）；数据缺失按 session 标记，不用“一根 bar 算全天覆盖”。
- 日线只用于确认、估值和规则参考；5 分钟严格模式缺关键 bar 不成交。显式 `daily_downgrade` 会为当日生成新 fingerprint，并标记 `mixed_granularity`。
- 快照包含：sessions、symbols、minute_bars、daily_bars、rules、corporate_actions、benchmark、coverage、plans；内容 hash 不含 run_id/created_at，保证同数据可复用。
- 成交价使用不复权价；日线价格口径未验证时标 `daily_price_basis_unverified`，运行结果可为 partial。
- 公司行为缺失且策略为 `strict` 时标 `corporate_action_coverage_unavailable`；`assume_none` 是显式假设，仍会标 `corporate_action_assumed_none`。

## 5. 撮合与账本要点

- 卖出优先撮合；买单按 `(rank, available_at, symbol, plan_key, order_id)` 稳定排序竞争现金。
- 买入限价：`open <= L` 参考 open，否则 `low <= L` 参考 L；滑点后不得高于 L；open 跳空低于入场区间下沿不自动买入。
- 卖出限价镜像；stop-market 跳空按 open 扣滑点，bar 内触发按 stop 扣滑点；止盈按预先存在限价单语义。
- 同 bar 止损/止盈双触发使用 stop-first 并记 `ambiguous`，不推测 OHLC 路径。
- 一字涨停不模拟买入，一字跌停不模拟卖出；其他触板成交带流动性假设标签。
- 5 分钟/日线 bar 的成交量按参与率共享同标的容量，允许部分成交；capacity 按股数标准化。
- 佣金最低收费按“委托/交易日”结算，日终做费用补差/退款；所有内部金额 Decimal，输出 pct 为百分数。
- 订单有效期按交易日历计数；买入当日数量次交易日可卖；期末默认撤未成交单、持仓按最后有效收盘价 mark-to-market，可选 `liquidate`。
- 核心不变量：可用现金非负、冻结不超过现金、可卖不超过持仓、成交不超过订单剩余和 bar 容量、权益 = 现金 + 持仓市值、已实现+未实现与净值变化扣费一致。

## 6. 存储与 worker

默认目录：`<DATABASE_PATH 所在目录>/backtest_runs/<run_id>/`，可用 `BACKTEST_RUNS_DIR` 覆盖。

```text
<run_id>/
  config.json
  manifest.json
  snapshot.sqlite        # 不可变输入快照
  ledger.sqlite          # orders/fills/ledger_events/position_lots/equity_points/closed_trades
  plans.json
  inputs.json
  metrics.json
  diagnostics.json
  result.json
  checksums.json
```

中央 SQLite 新增表：

- `backtest_runs`：run 元数据、status、progress、fingerprint、attempt、租约、取消、结果摘要
- `backtest_run_inputs`：纳入/排除输入审计
- `backtest_data_jobs`：预检/分钟线同步 job
- `backtest_corporate_actions`：版本化公司行为输入

启动 worker（建议独立进程，不放在 API 进程）：

```bash
python -m src.backtest.worker --workers 4
```

P3 历史 Agent 研究使用独立队列：

```bash
python -m src.backtest.worker --kind research --workers 1
```

本地调试只处理一个 run：

```bash
python -m src.backtest.worker --once
```

API 进程只负责入队/查询/预检/同步；纯回测 worker 不访问网络。预检/同步 job 目前由 API 进程内线程池执行，按单 API 进程设计；多 API 实例部署前应改为独立 job worker。

## 7. 配置项

新增环境变量（同步在 `.env.example`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BACKTEST_RUNS_DIR` | 空 | run 产物目录；空 = 数据库同目录/backtest_runs |
| `BACKTEST_WORKER_CONCURRENCY` | 4 | worker 进程数 |
| `BACKTEST_WORKER_POLL_SECONDS` | 1.0 | 队列轮询间隔 |
| `BACKTEST_WORKER_LEASE_SECONDS` | 180 | 租约时长；超时标 interrupted |
| `BACKTEST_DATA_JOB_WORKERS` | 2 | API 内预检/同步线程数 |
| `BACKTEST_MAX_DATE_RANGE_DAYS` | 3650 | 单实验最大自然日跨度 |
| `BACKTEST_MAX_PLANS` | 2000 | 单实验最大计划数 |

实验参数（本金、单票/总仓位、滑点、参与率、费用版本、粒度、基准、公司行为策略等）保存在 `config_json` 与指纹中，不需要每个参数加环境变量。

## 8. 运行与验收

```bash
python -m pytest -q tests/test_backtest_ledger.py tests/test_backtest_execution.py \
  tests/test_backtest_data_view.py tests/test_backtest_metrics.py \
  tests/test_backtest_plan_adapter.py tests/test_backtest_runner.py \
  tests/test_backtest_worker.py tests/test_backtest_runs_api.py
python -m py_compile src/backtest/*.py

cd apps/dsa-web
npm run lint
npm run build
npx vitest run src/pages/__tests__/BacktestRunsPage.test.tsx
npx playwright test e2e/backtest-runs.spec.ts   # 需先安装 Playwright 浏览器
```

手算金标准：100 股 10 元买入、100 股 11 元卖出，最低佣金 5 元、卖出印花税 0.05%，买入现金减少 1005，卖出增加 1094.45，净利润 89.45，10000 期末 10089.45。见 `tests/test_backtest_ledger.py`。

## 9. P3 历史 Agent 研究（首版）

P3 与 P0 的语义不同：**计划不是输入，而是回测过程中由 Agent 生成的**。首版实现范围：

- `mode=historical_agent_research`，`source_pipeline=research`。
- 候选范围两种模式：
  - `universe_mode=explicit`：显式 `research.universe`（≤50），用于受控对照。
  - `universe_mode=auto_local_coverage`：从全市场 Sequoia 日线库扫描所有有足够 as-of 历史的 A 股，按成交额/历史 bar 数做确定性预筛，取 `auto_universe_limit`（默认 100）交给选股 Agent；仍标 `local_coverage_universe` + `survivor_bias`。
- 全市场数据源：`SEQUOIA_CANDIDATE_DB_PATH`（默认 `vendor/Sequoia-X/data/sequoia_v2.db`），当前覆盖 **5,599 个标的、2025-10-14 → 2026-11-06**，最后完整日 2026-11-06。主库 `stock_daily` 只有少量代码，不作为 P3 全市场来源。
- Sequoia OHLC 为 `legacy_hfq_bridge`；P3 用成交额/成交量回推到现金价格尺度，标 `research_price_basis_rebased` / `research_price_basis_unverified`，manifest 价格基准降为 `daily_basis_unverified`。
- 月度决策，取每月第一个交易日；决策使用上一交易日收盘为止的 as-of 日线快照；不会读取当前 session 的未来 bar。
- `min_history_bars` 默认 40（Sequoia 数据起点为 2025-10-14，60 根会把 2026-01 决策日全部排除）。
- 选股 Agent：对候选集做一次批量 LLM 调用，返回 rank/top N；只使用技术快照，不编造基本面/新闻。
- 单股确认两种模式：
  - `confirmation_mode=auto_accept`（默认）：选股后直接生成确定性计划，不再逐只调用确认 LLM；适合观察“纯 AI 选股 + 机器风控”结果，标 `auto_accept_selection`。
  - `confirmation_mode=llm`：对每个入选标的，基于确定性研究计划做 accept/reject；计划价格仍由确定性代码生成，LLM 不写价格。
- 无论哪种确认模式，资金/风险硬门槛、限价单和撮合规则都保留；accept 不代表一定成交。
- 资金硬门槛：同一套 `RiskPolicy` + 执行内核；现金/总仓位/单票/最大持仓/单笔风险预算由引擎执行。
- LLM 调用预算 `max_llm_calls`（默认 500）；`auto_accept` 每次决策只消耗 1 次选股调用，`llm` 模式还会为每个入选标的增加确认调用。输出缓存写入 per-run `research_cache.sqlite`，同 key 复用，不重复计费。
- `research.model_ref` 只作为研究标签记录，不切换 LiteLLM 实际路由；实际模型记录在结果 `model_ref`，不一致时预检给 `research_model_ref_not_applied` 警告。
- 工具策略：日线/日历/交易规则/公司行为来自冻结快照；基本面、新闻、图谱在首版标 `tool_gap`，不伪造；实时行情/搜索不可达。
- 结果固定标记：`survivor_bias`、`model_knowledge_leakage_risk`、`llm_nondeterminism`、`research_not_production`；auto 模式额外标 `local_coverage_universe`；状态为 `partial`。

P3 预检不检查“过去有没有执行计划”，因此不会出现 `no_included_plans`；它检查 universe、历史 bar、决策日、LLM 可用性、调用预算和工具缺口。auto 模式会返回实际解析的候选代码、数据源、覆盖区间和价格基准；不是把今天的股票名单直接回填过去。

### 多指数对比曲线

权益图额外加载上证指数、创业板指、科创50、沪深300 四条展示曲线，统一按 run 初始权益归一化。它们只用于对比，不参与撮合/仓位/现金/盈亏；首次加载通过 Tushare `index_daily`，本地 Sequoia 可兜底，并缓存到 run 目录 `benchmarks.json`。

启动研究 worker（允许网络/LLM，独立于标准 worker）：

```bash
python -m src.backtest.worker --kind research --workers 1
```

标准 worker 不会领取 P3 run：

```bash
python -m src.backtest.worker --workers 4
```

### 点击创建自动执行

通过 `python server.py`、`python main.py --serve` 或 `uvicorn server:app` 启动时，API lifespan 会拉起进程内自动调度：

- 1 个 `standard` 队列线程；
- 1 个 `research` 队列线程；
- 它们走同一套 lease/claim/finalize，和 CLI worker 并存安全，不会重复执行同一个 run。

因此 Web 点击“创建实验”后，run 入队即会被自动领取，不需要再手工启动 worker。`uvicorn api.app:app` 不会启用自动调度；要自动执行请用 `server:app`。需要更多并发时，额外运行 CLI worker 即可。

P3 首版限制：

- 选股只使用技术快照，基本面/新闻/图谱是 gap；不能把结果当成生产选股能力。
- Sequoia 全市场覆盖从 2025-10-14 才开始，2025-10 之前没有可用日线；它是“全市场标的 + 有限历史”，不是多年全市场 PIT 库。
- Sequoia 现金价格是由成交额/成交量回推的近似值，不是交易所原始未复权 OHLC；不能拿它做精确成交/复权核算。
- auto 模式的 `local_coverage_universe` 仍不是严格 PIT 股票池：没有历史上市/退市/ST/停牌/板块元数据，仍有存活偏差。
- 当前模型知道历史后续事件，即使工具隔离也无法证明无泄漏；只能作为研究结论。
- 不从今天起前向记录/模拟盘验证，不能把 P3 结果当成可托管收益。
- 缓存是 per-run；跨 attempt 复用尚未实现。
- P3 预检会初始化现有 LLM 适配器以判断可用性；LiteLLM 可能尝试拉取模型定价表，失败会回退本地。纯 P0 回测 worker 不访问网络。

## 10. 常见预检结果

- `no_included_plans`：不是行情覆盖问题，而是输入筛选后没有可执行计划。常见原因：计划/Trace 筛选无匹配；artifact 不是 `generated`；最新 account decision 缺失或为 `observe_only` / `risk_blocked`；没有 `selected_scenario_id` 或场景不可用；`selection_confirmed` 缺少匹配的 `selection_links`；计划有效期不在实验区间内。预检结果里的 `inputs[].exclusion_reason` 会给出逐条原因。
- `incomplete_5m_coverage` / `incomplete_daily_coverage`：行情覆盖不足；严格模式会阻断，5 分钟可显式选择 `daily_downgrade` 或先同步分钟线。
- `market_rules_unverified`：缺少可验证的 ST/上市/品种规则，结果会标 partial。
- `corporate_action_coverage_unavailable`：缺少公司行为数据；持仓跨除权日时结果不可信，标 partial。

## 11. 明确限制

- `selection_confirmed` 依赖显式父链，当前没有自动推断；没有 `selection_links` 时选股样本会被排除。父链时间由调用方保证在选股之后，当前未自动读取 selection trace 时间戳做二次校验。
- 公司行为、ST/上市状态、指数基准依赖本地数据；缺失时标 partial/unavailable，不伪造完整成功。
- 单次 run 的事件循环仍按单一逻辑时钟顺序执行；并行度来自多 run + 多进程，单 run 内部不做会改变事件顺序的并发。
- 首期没有提现功能，账本只区分可用现金/冻结现金，未单独建模“可提现 vs 可交易”两个现金桶；A 股卖出资金当日可用于买入。
- `rerun` 生成新 attempt；数据未变且前次 succeeded 时 worker 会按 fingerprint 复用结果。断点续算暂不支持。
- P2/P3 未实现；不得把 P0 结论表述为历史选股策略收益或 Agent 历史决策能力。
