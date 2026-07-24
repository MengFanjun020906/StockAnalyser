# openInvest 可接入组件评估

> 目标：评估本仓库可从 `openInvest/` 借鉴或迁移的组件，明确接入位置、实施优先级、验证方式和不建议迁移的边界。

## 1. 背景与结论

`openInvest/` 是一个自部署 AI 投资委员会工具，核心链路是：

```text
Connectors -> Agents -> Core committee -> Memory / DB -> Jobs / Web
```

它的强项不是单个行情工具，而是以下几类工程方法：

- 多角色委员会：Macro / Quant / Risk / CIO 分工，并在初轮保持信息隔离。
- 确定性后处理：CIO 输出后用代码做仓位、置信度、失败降级、快崩防御等校验。
- Regime 中性化：市场状态只作为事实背景和历史概率参考，不直接变成方向锁。
- 后验复盘：记录委员会 verdict 与未来 7d / 30d 表现，再沉淀为长期 insight。
- 策略级验证：用 paper trading / PnL 曲线评价“长期听系统”效果，而不是只看单次命中率。

本仓库已经有账户感知、四席位选股、Meta-Agent、点位计算、组合配置、反方审查、Judge、Risk Gate、Agent Trace 和 Portfolio Service。直接搬整套 `openInvest` 会制造双数据源和双编排链路。建议采用“按组件吸收”的方式，把高价值机制接入现有链路。

最新同步记录：2026-07-20 已将本地 `openInvest/` 快进到 `origin/main` 快照 `717974a`。相较上一轮总结，新增价值主要来自 `0.31.5` 至 `0.31.7` 的硬化修复：交易状态 CAS 防重复入账、缺价/0 价治理、benchmark 缓存终点新鲜度、回测 win rate 防前视、Web/API token 保护、Docker 排除本地账本、backup skill 安装形态兜底。

## 2. 迁移原则

- 不迁移整仓，不新增与现有 `src/agent/`、`src/services/`、`src/repositories/` 平行的投资系统。
- 不替换现有 Portfolio Service、Agent Trace、FastAPI API、Web 前端和调度入口。
- 优先迁移确定性规则、数据结构和评估方法，少迁移 prompt 文案。
- 所有用户可见结论必须继续经过本仓库现有 Risk Gate / Judge / Trace 审计。
- openInvest 本地 README 标注 MIT，但当前本地目录未见独立 `LICENSE` 文件；直接复制代码前需要确认授权和保留来源说明。
- openInvest 要求 Python 3.13，本仓库为 Python 3.11+；优先按思想重写小模块，避免引入版本约束。

## 3. 优先级总览

| 优先级 | 候选组件 | openInvest 来源 | 本仓库建议落点 | 接入方式 |
| --- | --- | --- | --- | --- |
| P0 | CIO / Judge sanity check | `openInvest/core/committee.py` | `src/agent/risk_gate.py` 或新增 `src/agent/judge_sanity.py` | 提取确定性后处理规则，接在 Judge 输出之后、Risk Gate 之前 |
| P0 | LLM telemetry | `openInvest/core/llm_telemetry.py` | `src/agent/llm_adapter.py`、Agent Trace artifact | 统一记录 role / asset / stage / tokens / latency / cost / tool calls |
| P0 | 交易状态 CAS / 防重复入账 | `openInvest/db/trades_db.py`、`openInvest/connectors/web_api/routers/trades.py` | 未来订单状态流、执行记录、交易导入幂等层 | 状态跃迁必须声明原状态，只有真实 `planned -> executed` 赢得账本副作用 |
| P0 | 缺价与 0 价治理 | `openInvest/utils/fx.py`、`openInvest/services/skill_views.py` | `src/services/portfolio_service.py`、未来 `portfolio_performance_service.py`、回测服务 | `None/NaN/<=0` 分层视为缺价或 fallback，不把哨兵值当有效估值 |
| P1 | 事件新闻哨兵 cron | `openInvest/jobs/event_watch.py`、`openInvest/jobs/event_watch.yml`、`openInvest/scheduler/runner.py` | `src/services/news_signal_scheduler.py`、`src/services/news_signal_service.py`、Agent Trace 触发入口 | 从持仓/自选股生成 watched set，定时多源抓新闻，事件化后只对命中标的和高严重度事件触发分析 |
| P1 | Regime forward probability / reentry reference | `openInvest/core/regime_probability.py` | `src/agent/regime.py`、`src/core/backtest_engine.py`、点位计算层 | 基于历史行情计算 regime 下 forward return 分布和买回点参考 |
| P1 | Dreaming verdict review | `openInvest/docs/wiki/03-dreaming.md`、`openInvest/jobs/verdict_review.py` | `data/agent_traces/`、`src/services/`、可选 `src/agent/memory.py` | 从历史 Trace 生成 verdict 后验复盘和稳定 insight |
| P1 | 信息隔离委员会协议 | `openInvest/core/committee.py`、`openInvest/docs/wiki/02-agents.md` | `src/agent/debate.py`、选股 Meta / Judge 阶段 | 强化 Round 1 角色隔离、Round 2 交叉质询和收敛检测 |
| P1 | 收益率展示 / PnL Dashboard | `openInvest/jobs/pnl_snapshot.py`、`openInvest/core/benchmarks.py`、`openInvest/connectors/web_api.py`、`openInvest/scripts/sync_gui_dist.py` | `src/services/portfolio_service.py`、`api/v1/endpoints/portfolio.py` 或新增 `performance.py`、`apps/dsa-web/` | 接入收益率时间序列、基准对比和展示页；不直接挂独立 `invest-gui` dist |
| P1 | 自托管安全与备份边界 | `openInvest/connectors/web_api/__main__.py`、`.dockerignore`、`plugin/skills/invest-backup/` | Docker / Desktop / 部署文档 / 本地数据备份脚本 | 远端暴露默认要求 token，构建默认排除账本和画像，备份工具兼容非源码安装 |
| P2 | 策略级 reward / paper trading 评估 | `openInvest/core/paper_trade_simulator.py`、`openInvest/core/backtest_reward.py` | `src/core/backtest_engine.py`、`src/services/backtest_service.py` | 借鉴 reward 口径，不直接替换现有回测引擎 |
| P2 | Web/SSE 任务状态 | `openInvest/connectors/web_api.py` | `api/v1/endpoints/agent.py`、Agent Trace 页面 | 仅借鉴阶段状态推送，不迁移 API 层 |

## 4. 可接入组件详情

### 4.1 CIO / Judge sanity check

**价值**

openInvest 的 `parse_cio_memo()` 把 LLM 输出后的关键安全约束放进代码，而不是依赖 prompt 自觉遵守。适合本仓库的 Judge / Risk Gate 链路。

**建议迁移规则**

- `BUY` 且置信度异常高时降级为 `ACCUMULATE`，防止 prompt injection 或过度自信。
- `alloc_cny` / 仓位金额超过上限时 clamp，并保留 `_original_alloc` 方便 Trace 审计。
- 任一关键 worker 输出 `[WORKER_UNAVAILABLE]` 或等价错误标记时，强制 `HOLD / wait / manual_review`，并压低 confidence。
- `TRIM` 必须给出低于现价的买回点或明确再入场条件；缺失时降级为 `HOLD / wait`。
- 快崩防御触发时，只降级买侧动作：`BUY -> ACCUMULATE`，`ACCUMULATE -> HOLD`；不强制卖出。
- `risk_profile=aggressive` 等风险偏好只能作为显式配置，不应由 regime 自动推导。

**建议落点**

- 新增 `src/agent/judge_sanity.py`：纯函数接收 Judge 输出、行情、账户、regime、工具状态，返回修正后的裁决与审计字段。
- 在 `src/agent/stock_selection.py` 或现有 Judge 输出汇合点调用。
- 最终仍传入 `src/agent/risk_gate.py` 做 A 股交易硬约束。

**测试**

- 新增 `tests/test_agent_judge_sanity.py`。
- 覆盖高置信 BUY、alloc clamp、worker unavailable、TRIM 无买回点、快崩买侧降级、审计字段保留。

### 4.2 LLM telemetry

**价值**

当前 Agent Trace 已能展示阶段和工具，但对 LLM 调用成本、耗时、role、round、token 和 tool call 统计可以更透明。

**建议字段**

```json
{
  "ts": "ISO8601",
  "trace_id": "string",
  "agent_role": "planner|tool_etl|meta|pricing|portfolio|adversarial|judge|unknown",
  "symbol": "optional",
  "stage": "string",
  "provider": "string",
  "model": "string",
  "input_tokens": 0,
  "output_tokens": 0,
  "latency_ms": 0,
  "estimated_cost": 0.0,
  "tool_calls": 0,
  "ok": true,
  "error": null
}
```

**建议落点**

- 在 `src/agent/llm_adapter.py` 的统一调用出口记录。（已接入：`src/agent/llm_telemetry.py` 通过 trace scope 写入成功/失败调用，`src/agent/stock_selection.py` 为阶段 LLM 调用补充 `stage/agent_role/symbol`。）
- 每个 trace 下写入 `llm_usage.jsonl`，并可汇总进阶段 JSON。（已接入：`api/v1/endpoints/agent.py` 的 Trace run/stream 会设置 artifact scope。）
- Web 端 Agent Trace 页面展示“本次 token / 费用 / LLM 耗时 / Judge sanity 修正”。（已接入：`AgentTraceRunResponse.llm_telemetry/judge_sanity`、Trace UI “可观测性”层。）

**测试**

- 扩展 `tests/test_llm_usage.py` 或新增 telemetry 测试。
- 验证失败调用也写审计记录，且 telemetry 写入失败不阻断主流程。

### 4.3 Regime forward probability 与买回点参考

**价值**

本仓库已有 A 股 regime 状态机，但目前更偏“环境标签和策略约束”，实际使用上不够好用：工具能告诉后续链路“现在是 `trending_up / risk_off / panic`”，但不能回答“这个状态历史上未来 30 天上涨概率多大、跌破现价概率多大、通常先回踩还是直接涨、如果减仓后什么价位更有把握接回”。结果是后续链路只能拿到一个粗标签和几条 `strategy_hints`，很容易演变成硬降档或泛化提示。

openInvest 的改进点是把 regime 拆成两层：

- **状态识别层**：确定性地给出当前 regime、波动、情绪、冲突和数据质量。
- **后验概率层**：基于历史 OHLC 对每个 `(asset/index, regime)` 统计 forward return、路径形状和买回点参考。

这比继续微调当前 prompt 更有价值。Regime 不应该直接说“该买/该卖”，而应该给后续 Meta / pricing / Judge 提供可校准的概率证据。

**当前工具的主要问题**

- 标签多、概率少：`regime`、`volatility_bucket`、`risk_level` 能表达环境，但缺少“未来 N 天分布”的量化口径。
- 直接影响动作：当前 `risk_off / panic / extreme` 会在选股链路里直接把 open 降为 wait，缺少“历史上这类状态是否真的不适合所有 setup”的回测证据。
- 市场级强、个股级弱：`detect_market_regime` 默认是沪深300等市场代理，难以解释某只股票自己的状态和买回点。
- 路径不可见：同样 30 日收益为正，可能是“先跌后涨”或“直接上涨无回踩”；对分批入场、等待回踩和 TRIM 再接回来说完全不同。
- 样本置信度不可见：后续模型看不到 `n / effective_n / low_confidence`，容易把很少样本的结论当强约束。

**建议能力**

- 按 `(market, symbol 或 index, regime)` 统计未来 7d / 30d / 60d / 90d return 分布。
- 输出：
  - 原始样本数 `n` 和重叠窗口折算后的有效样本数 `effective_n`。
  - 中位收益、均值收益、上涨概率、跌破现价概率、跌超阈值概率。
  - p10 / p20 / p90 等分位数。
  - 窗口内最深回踩 `min_return`、最高冲高 `max_return`、到达谷底/峰值的交易日数。
  - regime 在 forward window 内实际持续天数，避免把切换后的收益错误归因给原 regime。
  - 悲观分位对应的参考买回价 `reentry_price`。
- 样本不足时标记 `low_confidence`，只作为弱参考，不允许触发硬动作。

**建议新增输出结构**

`detect_market_regime` 可以保留现有字段，同时增加可选 `forward_probability` 摘要；更推荐新增独立工具 `get_regime_forward_probability`，避免把市场状态识别工具做得过重。

```json
{
  "symbol": "000300.SH",
  "regime": "range_bound",
  "as_of": "2026-06-11",
  "source": "ohlc_forward_return",
  "windows": {
    "30d": {
      "n": 420,
      "effective_n": 14,
      "median_return_pct": 1.2,
      "mean_return_pct": 0.8,
      "p_up": 0.56,
      "p_below_current": 0.44,
      "p_down_gt_5pct": 0.18,
      "p10_return_pct": -6.4,
      "p20_return_pct": -3.2,
      "p90_return_pct": 8.5,
      "low_confidence": false
    },
    "90d": {
      "n": 390,
      "effective_n": 4,
      "median_return_pct": 3.8,
      "p_below_current": 0.37,
      "low_confidence": true
    }
  },
  "path_profile": {
    "window": "90d",
    "pct_dip_then_up": 0.31,
    "pct_up_no_dip": 0.22,
    "pct_pop_then_down": 0.18,
    "pct_down_no_pop": 0.29,
    "dip_median_pct": -4.1,
    "days_to_trough_median": 12,
    "pop_median_pct": 5.6,
    "days_to_peak_median": 18,
    "regime_persist_median_days": 16,
    "window_median_days": 62
  },
  "reentry_reference": {
    "current_price": 10.0,
    "downside_quantile": 0.2,
    "downside_pct": -3.2,
    "reentry_price": 9.68,
    "p_below_current": 0.44,
    "low_confidence": false
  }
}
```

**算法口径**

- 复用 production `src/agent/regime.py` 的分类逻辑，对历史每一天重新计算当日 regime，不能另写一套分类阈值。
- 对每个历史日期，寻找 `date + 7d / 30d / 60d / 90d` 之后第一个可交易日收盘价，计算 forward return。
- 同时计算窗口内最低价和最高价相对当日收盘的收益，用于判断“先跌后涨 / 直接涨 / 冲高回落 / 一路下跌”。
- 重叠窗口不能直接把 `n` 当独立样本；有效样本量用 `effective_n ~= n / window_days` 或更保守的交易日窗口折算。
- 显著回踩 / 冲高建议用当日 ATR 自校准，而不是写死 3% 或 5%。例如窗口内最低收益 `<= -1 * atr_pct` 视为给过明显低吸点。
- 当前价缺失时不输出 `reentry_price`，只输出 return 分布。
- A 股涨跌停、停牌、除权复权口径要在样本里打标；不可成交样本不能无条件参与买回点统计。

**本仓库适配方式**

先不要改掉现有 `detect_market_regime`，而是新增一层概率工具：

```text
src/agent/regime_probability.py
src/agent/tools/market_tools.py:get_regime_forward_probability
```

第一版只做市场代理指数：

- 默认 `index_code=000300`，兼容 `000001 / 000905 / 000852 / 399006`。
- 使用 Tushare `index_daily` 或本地历史缓存。
- 输出当前市场 regime 对应的 forward probability。

第二版再做单股级别：

- 对深挖候选或持仓标的计算 `symbol_regime_probability`。
- 由 pricing agent 消费，用于入场区间、等待回踩、TRIM 买回点和失效条件。
- 多股扫描时只对进入 deep dive 的 1-5 只股票计算，避免候选池阶段过重。

**在 Agent 链路中的用法**

- `MarketRegime Expert`：只引用概率摘要，不把概率直接翻译成买卖。
- `Meta-Agent`：把概率层写进硬约束包，例如“30d 跌破现价概率高，Breakout 场景必须等待回踩确认”。
- `pricing_agent`：
  - 对 `open / accumulate`：参考 `pct_dip_then_up` 和 `p_below_current` 决定是否给等待回踩价。
  - 对 `trim`：必须给 `reentry_price` 或清楚说明“历史无明显低于现价的买回点，因此 TRIM 不成立或只做止盈减仓”。
  - 对 `wait`：写清楚等待的是概率更优的回踩，不是笼统“市场不明朗”。
- `Judge`：看到 `low_confidence=true` 时不能把该概率作为主要裁决依据。
- `Risk Gate`：只消费最终计划，不直接消费概率层，避免概率工具绕过硬风控。

**建议落点**

- 保留现有 `src/agent/regime.py` 作为 regime 真源。
- 新增 `src/agent/regime_probability.py`，提供纯函数：
  - `compute_regime_return_frame(bars, regime_classifier, windows)`
  - `build_regime_probability(frame, regime, windows)`
  - `build_reentry_reference(frame, regime, current_price)`
  - `format_regime_probability_brief(payload)`
- 在 `src/agent/tools/market_tools.py` 注册 `get_regime_forward_probability` 工具。
- 在 `src/agent/stock_selection.py` 的 `_summarize_market_regime()` 中保留概率摘要字段，供 Trace 和后续阶段读取。
- 点位计算层在生成 `TRIM`、回撤买点、分批入场区间时读取该参考。
- 文档同步 `docs/modules/regime-state-machine.md`，把当前状态机定位为“识别层”，新增概率层说明。

**测试**

- 使用构造 OHLC 数据验证 forward return 分布、样本不足标记、买回价必须低于现价才可用。
- 增加“不允许 regime 直接强制 BUY/SELL”的回归测试。
- 验证历史分类复用 `src/agent/regime.py`，不能在概率层重复硬编码另一套 regime 规则。
- 验证重叠窗口 `effective_n` 小于原始 `n`，样本不足时 `low_confidence=true`。
- 验证路径画像四类比例合计约等于 1。
- 验证缺行情、停牌、尾部 lookahead 不足时 graceful 返回空概率或 low confidence，不阻断主流程。

### 4.4 Dreaming verdict review

**价值**

单个 verdict 命中率很噪，但长期 trace 足够后，可以学到系统自己的稳定偏差。例如“某类 regime 下总是 wait 导致踏空”或“高波动下追涨失败率高”。

**建议数据源**

- `data/agent_traces/<trace-id>/` 中的最终 Judge、候选池、账户约束、市场状态、工具质量、最终报告。
- 后续行情由现有数据源补齐，不依赖用户是否真实交易。

**建议产物**

```text
data/agent_reviews/verdict_review.jsonl
data/agent_reviews/insights/*.md
```

每条 review 至少包含：

- `trace_id`
- `decision_date`
- `symbol`
- `intent`
- `final_action`
- `confidence`
- `regime`
- `data_quality`
- `future_return_7d / 30d`
- `hit / missed_up / avoided_down / wrong_direction`

**接入方式**

- 先做离线脚本：`scripts/build_agent_verdict_reviews.py`。
- 再做 service：`src/services/agent_verdict_review_service.py`。
- Web / API 闭环只做本地样本刷新：`POST /api/v1/agent-verdict-reviews/rebuild?windows=7,30&limit=300` 会扫描本地 Trace 和本地 `StockDaily` 后重写 `data/agent_reviews/verdict_review.jsonl`。
- 离线 insight 先写 Markdown：`scripts/build_agent_verdict_insights.py --min-samples 20` 会从本地 `verdict_review.jsonl` 生成 `data/agent_reviews/insights/agent_verdict_insights.md`，默认只把同分组 20 条以上 completed 样本沉淀为稳定洞察。
- 未来如需把稳定 insight 作为“历史复盘提示”注入 Meta-Agent 或 Judge，必须另设阈值门、灰度开关和 trace_id 追溯；当前版本不注入线上决策。

**防过拟合规则**

- 样本数低于阈值只展示，不注入决策。
- 黑天鹅事件或停牌、涨跌停不可成交样本需要标记，不能直接归因给模型。
- insight 必须写明样本数、窗口、市场状态和适用边界。

### 4.5 信息隔离委员会协议

**价值**

当前本仓库已有四席位候选池、Meta-Agent、反方审查和 Judge。openInvest 的可取之处是“谁能看到什么”定义更严格。

**建议调整**

- 技术/量价角色初轮不看用户亏损和持仓成本，避免被沉没成本污染。
- 风险/组合角色初轮不看技术指标细节，避免被短期信号牵着走。
- 消息/主题角色只评价催化剂真实性和相关性，不直接给买卖建议。
- Round 2 后允许互看结论，但提示词要求只引用对方结论，不复算对方原始字段。
- Judge 看到完整 transcript，并由确定性 sanity check 二次处理。

**建议落点**

- `src/agent/debate.py`：从当前“共享 evidence bundle 的正反方”扩展出可选的分视角 bundle。
- `docs/plans/agent-multi-expert-refactor-plan.md` 后续可引用本协议。
- `data/agent_traces/` 中保留每个角色实际输入摘要，便于审计信息隔离是否生效。

### 4.6 策略级回测与 reward

**价值**

openInvest 的重要经验是：不要只看单次预测对不对，要看“长期按系统动作执行”的 PnL 曲线、最大回撤、Sharpe 和基准超额。

**建议接入**

- 不直接搬 `PaperTradeSimulator`。
- 在现有 `src/core/backtest_engine.py` 和 `src/services/backtest_service.py` 上增加策略级指标：
  - total return
  - annualized return
  - max drawdown
  - Sharpe
  - alpha vs benchmark
  - action distribution
  - skipped due to risk gate / data quality
- reward 仅用于离线评估和参数比较，不直接实时驱动交易动作。

**验证**

- 用固定历史区间和固定 mock verdict 回放，确保 reward 可复现。
- 明确训练 / holdout 时间切分，避免 lookahead bias。

### 4.7 收益率展示 / PnL Dashboard

**价值**

openInvest 有一套部署后可看的收益率展示能力：后台定时生成 PnL 快照、累计收益率折线、基准对比横向柱状图和“跑赢/跑输基准事件”。这类页面适合作为本仓库 Web 工作台的展示面板，让用户直观看到组合表现、系统建议的后验效果和与基准的差距。

需要注意：openInvest 的前端源码不在 `openInvest/` 本仓库内。`openInvest/scripts/sync_gui_dist.py` 会从独立仓库 `longsizhuo/invest-gui` 的 release 下载 `invest-gui-dist.tar.gz` 到 `static/`，再由 `connectors/web_api.py` 挂载为 SPA。因此本仓库不建议直接接入该静态 dist；更稳的方式是在 `apps/dsa-web/` 里按本仓库设计系统重做同类页面，复用其数据设计。

**openInvest 可借鉴的数据链路**

- `openInvest/jobs/pnl_snapshot.py`
  - 定时读取持仓、现金和行情。
  - 写入 `memory/.state/pnl_history.jsonl`。
  - 生成 `docs/pnl_chart.svg`。
  - 生成 outperform events，用于展示“跑赢/跑输某基准”的事件。
- `openInvest/core/benchmarks.py`
  - 管理指数、理财、公募基金、AI 投顾等基准序列。
  - 缓存基准数据，网络失败时跳过单条基准，不影响主图。
- `openInvest/connectors/web_api.py`
  - `GET /api/pnl_chart.svg`
  - `GET /api/pnl_history`
  - `GET /api/outperform_events`

**本仓库建议落点**

- 后端：
  - 新增 `src/services/portfolio_performance_service.py`，基于现有 Portfolio Service 和行情源计算组合净值、累计收益率、分标的贡献和基准对比。
  - 新增 `api/v1/endpoints/performance.py`，提供结构化 JSON，而不是只返回 SVG。
  - 如需持久化，新增轻量 repository 或复用现有 portfolio snapshot 存储，不使用 openInvest 的 `memory/.state`。
- 前端：
  - 在 `apps/dsa-web/` 新增 `PerformancePage`，或先集成到 `PortfolioPage` 的“收益”标签页。
  - 使用 ECharts 渲染收益率曲线、基准对比条形图、标的贡献和回撤曲线。
  - 首页或侧边栏可新增“收益展示 / Performance”入口。
- 调度：
  - 由现有 `scripts/daily_run.sh` 或后端 task service 定时刷新快照。
  - 手动刷新按钮只触发当前用户本地快照，不做自动 git commit / push。

**建议 API 草案**

```text
GET /api/v1/performance/summary
GET /api/v1/performance/history?window=30d
GET /api/v1/performance/benchmarks?window=30d
GET /api/v1/performance/events?limit=20
POST /api/v1/performance/snapshot
```

响应建议以百分比和相对净值为主：

```json
{
  "as_of": "2026-06-11T15:30:00+08:00",
  "window": "30d",
  "portfolio_return_pct": 3.25,
  "max_drawdown_pct": -1.8,
  "benchmarks": [
    {"name": "沪深300", "return_pct": 1.2, "alpha_pct": 2.05}
  ],
  "series": [
    {"date": "2026-06-10", "portfolio_nav": 1.032, "沪深300": 1.011}
  ],
  "events": [
    {"label": "组合近 30 日跑赢沪深300 +2.05%", "type": "outperform"}
  ]
}
```

**隐私与展示边界**

- 本地私有页面可以显示账户绝对金额、持仓市值和浮盈金额。
- 可分享图、公开 SVG 或 README 展示必须只显示百分比、相对净值和基准差，不暴露资产规模、持仓数量、成本价和具体券商。
- 不建议照搬 openInvest 的自动 git push / `pnl-data` 分支机制；本仓库默认应保持本地展示，后续若要公开展示必须单独加显式 opt-in 配置。

**与现有能力的关系**

- `PortfolioPage` 继续负责账户、持仓和流水管理。
- `PerformancePage` 负责收益率、净值曲线、基准对比、回撤和后验展示。
- `BacktestPage` 负责策略模拟；Performance 页面展示真实或录入账户的已发生收益。
- Agent Trace 可在后续关联某次 Judge 决策后的收益事件，用于解释“这个建议后来表现如何”。

**测试**

- 后端单测：无持仓、单账户、多账户、多币种、行情缺失、基准缺失、负收益和现金占比高的场景。
- 前端测试：空状态、加载失败、收益为负、基准缺失、长标签不溢出。
- 构建验证：涉及 `apps/dsa-web/` 时执行 `npm run lint` 和 `npm run build`。

### 4.8 最新增量：状态机、安全边界与评估口径硬化

**价值**

`openInvest` 最新一轮修复的共同点是把“真实资金系统不能犯的错”从经验和文档下沉到代码边界。它对本仓库的参考价值高于普通功能增量，尤其适合 Portfolio、Performance、Backtest 和部署链路。

**本轮新增可借鉴点**

- 交易状态副作用必须用精确原状态 CAS：`planned -> executed` 才能触发组合账本同步，`cancelled -> executed` 不能再次入账。
- 缺价治理要显式区分 `missing`、`stale`、`fallback` 和有效价格；`0.0` 这类哨兵值不能进入总资产、收益率、what-if 或风险指标。
- benchmark 缓存新鲜度要对齐评估窗口终点，而不是窗口起点；否则组合收益每天更新，基准可能冻结数周。
- 回测指标必须防前视：卖出收益只能匹配该卖出之前最近的买入，2Y 窗口必须明确交易日长度，不能拿更长历史冒充两年。
- 自托管 Web/API 只要绑定非 loopback，就必须要求 token；Docker build 默认排除 `db/`、`user_profile.json` 这类本地资产数据。
- ignored state 需要可执行的备份/恢复工具，且工具要兼容源码 checkout 与插件/PyPI 安装形态。
- provider、insight slug、UTC timestamp 等审计字段不能写死或混用本地时区，否则 Trace 与历史洞察会变成“看似存在但不可对账”。

**本仓库落点**

- `src/services/portfolio_service.py` 已有写锁和缺价元信息，后续若加入订单执行状态，应补“原状态 CAS + 副作用唯一执行”的测试。
- 未来 `portfolio_performance_service` 必须复用缺价元信息，不允许把 `0.0` 当作有效实时价或收盘价。
- `entry_execution_backtest`、`agent_verdict_review` 和策略评估应补“只看过去交易”的回归测试，避免后续成交、后续入场或未来窗口污染当前样本。
- Docker、桌面端和公开报告导出要把资产金额、持仓、成本、用户画像和密钥纳入默认排除/脱敏范围。

## 5. 暂不建议迁移的部分

| 组件 | 不建议原因 | 替代方式 |
| --- | --- | --- |
| `core/memory_store.py` Markdown frontmatter 存储 | 本仓库已有 repository / SQLite / Agent Trace；迁入会造成双数据源 | 只学习 atomic write / transaction 思想 |
| `core/portfolio_manager.py` | 与 `src/services/portfolio_service.py` 职责重叠 | 保留现有 Portfolio Service |
| `connectors/web_api.py` | 本仓库已有 FastAPI API、Web、Desktop | 借鉴 SSE task status，不迁移 API |
| `scripts/sync_gui_dist.py` 和独立 `invest-gui` 静态 dist | 前端源码不在本仓库，API/设计系统与 `apps/dsa-web` 不一致 | 在 `apps/dsa-web` 原生实现收益率页面 |
| `agents/sdk_agent.py` | 本仓库已有 LLM adapter / tool 调用层 | 在现有 adapter 记录 telemetry 和失败哨兵 |
| `jobs/*` 整套调度 | 本仓库已有 scheduler / daily_run / task service | 只迁移 verdict review 这类任务语义 |
| DSPy / Optuna 实验链路 | 当前 trace 样本和验证闭环还不足，直接上自动优化风险高 | 先做后验数据集和 holdout 评估 |

## 6. 建议实施路线

### Phase 1：安全与可观测性

1. 新增 Judge sanity check 纯函数。（已接入：`src/agent/judge_sanity.py`，在 `judge_decision` 后处理阶段执行，只降级或截断不合规裁决，并在 `full.sanity_checks` 留审计。）
2. 接入 LLM telemetry 到 Agent Trace。（已接入：`src/agent/llm_telemetry.py`、`src/agent/llm_adapter.py`、Trace artifact `llm_usage.jsonl`。）
3. 在最终报告或 Trace 中展示“LLM 修正前后裁决”和“token / cost / latency”。（已接入：Trace artifact 固化 `llm_telemetry.json` / `judge_sanity.json`，API run/stream/history 返回 `llm_telemetry` / `judge_sanity`，Web Trace “可观测性”层展示调用次数、Token、延迟、估算成本、阶段统计和 sanity 规则修正。）

验收：

- 不改变候选发现结果，只改变不合规裁决的降级和审计。
- 所有 sanity 改动可在 Trace 中看到原始值和修正原因。

### Phase 2：Regime 概率与点位增强

1. 基于现有 regime 和历史行情构建 forward return 统计。（已接入第一版：`src/agent/regime_probability.py` 复用 `detect_market_regime` 分类历史切片，`get_regime_forward_probability` 输出 7/30/60/90 日 forward probability。）
2. 给 pricing agent 提供 reentry / downside reference。（已接入第一版：`stock_selection` 在 market regime 后挂载 `forward_probability`，pricing fallback 输出 `regime_probability` / `reentry_reference`。）
3. 对 `TRIM`、分批入场、等待回踩计划增加历史概率说明。（已接入第一版：pricing prompt 约束 `low_confidence` 只能作弱证据，fallback 将 `reentry_price` 写成参考回踩/买回价，最终报告展示 `Regime 概率证据`。）

验收：

- 样本不足不会生成强结论。
- Regime 概率不直接覆盖 Judge，只作为证据。
- 第一版仍是市场代理指数概率，单股级概率和历史回测校准留到后续阶段。

### Phase 3：后验复盘与长期 insight

1. 从 Agent Trace 构建 verdict review jsonl。（已接入第一版：`src/services/agent_verdict_review_service.py` 和 `scripts/build_agent_verdict_reviews.py` 只读 Trace 与本地 `StockDaily`，选股链路写入 `chain_type=stock_selection`，单股链路写入 `chain_type=single_stock_analysis`。）
2. Web/API 样本刷新闭环。（已接入：`POST /api/v1/agent-verdict-reviews/rebuild` 和 Web `/agent-verdict-reviews` 的“重建样本”按钮可从本地 Trace 刷新 `verdict_review.jsonl`，默认只扫最近 300 个 Trace，不重跑 Agent、不拉外部行情、不注入线上决策。）
3. 生成稳定 insight markdown。（已接入离线第一版：`scripts/build_agent_verdict_insights.py` 从本地 `verdict_review.jsonl` 聚合分组样本，默认至少 20 条 completed 样本才形成稳定洞察，并写入 `data/agent_reviews/insights/agent_verdict_insights.md`；当前只供人工复盘，不注入线上决策。）
4. 只向 Meta-Agent / Judge 注入经过阈值门的 insight。（未接入；第一版明确不影响线上决策。）

验收：

- insight 都能追溯到 trace_id 和行情窗口。
- 能区分模型错误、数据缺失、不可成交和黑天鹅事件。
- 样本不足、缺少起始价或未来行情不足时只输出 `insufficient_data` / `partial`，不强行归因。
- 第一版同时覆盖两条主链路，但 schema 分开：选股链路读取 `candidate_discovery -> portfolio_allocation -> judge_decision`，单股链路读取 `risk_gate`、`operation_advice`、`decision_type` 和 `confidence_level`；单股不会伪造选股链路字段。

### Phase 3.5：收益率展示页面

1. 先实现后端 performance summary / history JSON。
2. 在 `apps/dsa-web` 新增收益率展示页或 Portfolio 子页。
3. 接入组合收益曲线、基准对比、回撤和 outperform events。
4. 后续再把 Agent Trace 决策与收益事件关联。

验收：

- 页面不依赖 openInvest 的独立前端 dist。
- 行情或基准拉取失败时页面能降级展示已有组合数据。
- 默认不产生公开 SVG、不自动 push、不暴露绝对资产规模。

### Phase 4：委员会协议强化

1. 对现有 debate / 多专家输入做信息隔离。
2. 增加 Round 2 cross-challenge 和收敛检测。
3. 对比单轮 Judge 与多轮委员会在回测指标上的差异。

验收：

- 多轮机制必须带来可观测收益，否则保持单轮，避免复杂度膨胀。

## 7. 验证矩阵

| 改动面 | 最低验证 | 建议补充 |
| --- | --- | --- |
| Judge sanity | `python -m py_compile src/agent/judge_sanity.py`、相关单测 | 用真实 trace replay 验证修正字段 |
| LLM telemetry | telemetry 单测 | Agent Trace 页面 smoke |
| Regime probability | 构造行情单测 | 固定历史区间回放 |
| Dreaming review | 脚本单测和 dry-run | 与真实 trace 的 7d / 30d 后验比对 |
| Performance dashboard | 后端 performance service 单测 | Web 页面 lint/build + 空状态/负收益截图检查 |
| Debate 协议 | `tests/test_agent_debate.py` | 同一输入下单轮/多轮成本与结果对比 |
| Backtest reward | 回测服务单测 | train / holdout 时间切分报告 |

## 8. 回滚方式

- Judge sanity：配置开关禁用或在调用点跳过，保留原始 Judge 输出。
- Telemetry：写入失败默认不阻断；可通过配置关闭落盘。
- Regime probability：样本不足或异常时返回空参考，pricing agent 退回现有规则。
- Dreaming review：不注入 insight 即可回到当前行为；历史 review 产物可保留供离线分析。
- Performance dashboard：移除前端路由和 performance API endpoint；不影响持仓管理与 Agent 主链路。
- Debate 协议：保留现有 `src/agent/debate.py` 单轮路径作为默认回退。

## 9. 与现有文档关系

- 多专家角色边界可后续同步到 `docs/plans/agent-multi-expert-refactor-plan.md`。
- Regime 概率接入后需同步 `docs/modules/regime-state-machine.md`。
- 若影响最终报告结构或 Trace 展示，需要同步 `docs/architecture/stock-selection-pipeline.md` 和 `docs/CHANGELOG.md`。
- 本文只记录迁移评估和实施建议，不代表功能已实现。
