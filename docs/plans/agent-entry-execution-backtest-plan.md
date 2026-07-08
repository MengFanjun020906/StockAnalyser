# Agent 入场执行回测方案

本文档设计一个面向选股报告最终输出的离线执行回测方案。目标是验证 AI 给出的入场点位、止盈、止损和等待条件在真实日线行情中的可执行性，并专门诊断“AI 入场点位过于保守，股票大多等不到这个价格”的问题。

## 背景

当前仓库已有 Agent 后验复盘链路，主要回答 AI 的方向判断是否正确，例如某个标的是 `hit`、`missed_up`、`avoided_down` 还是 `wrong_direction`。这类复盘适合看判断方向，但不能回答以下问题：

- AI 给出的入场区间是否真的成交。
- 成交后是否先触发止盈或止损。
- 如果没有成交，股票是否已经上涨并形成错失收益。
- 如果持有超时，强制卖出的收益如何。
- 当前入场点位是否系统性过保守。

因此需要单独设计“入场执行回测”，把 AI 报告中的交易脚本转成可回放的条件单。

## 设计目标

1. 只评估选股报告最终输出的标的，不评估候选池里的所有股票。
2. 最终输出标的通常不超过 4 只，第一版按全部输出标的等权统计。
3. 不设置本金和资金占用，重点统计胜率、成交率、收益率、止盈止损和错失收益。
4. 同时评估严格 AI 入场和若干对照策略，用数据判断“选股有效但入场太保守”还是“选股本身无效”。
5. 第一版基于本地 Agent Trace 与本地行情缓存，不重跑 Agent、不自动注入线上决策；分钟线行情由用户在前端手动触发 baostock 同步后落入本地数据库，回测优先使用分钟线，缺失时再退回日线缓存。

## 非目标

- 不模拟真实账户本金、现金占用、融资融券、税费和滑点。
- 不直接改变线上 Agent 的推荐逻辑。
- 不把回测结论自动写回 Judge、Meta-Agent 或实盘建议。
- 不把候选发现、四席位中间候选全部当成交易信号。

## 输入范围

第一版只读取选股链路的最终报告产物：

- `data/agent_traces/<trace_id>/final_report.json`
- 或 `data/agent_traces/<trace_id>/stock_selection.json` 中的 `final_report_json`

候选来源优先级：

1. `portfolio_allocation.full.positions_plan[]`
2. `pricing_agent.full.if_then_order_matrix[]`
3. `single_stock_deep_dive.full.results[]` 中的入场质量字段作为补充

纳入回测的标的必须满足：

- 出现在最终报告正文的推荐/观察输出中。
- 有可解析的股票代码。
- 有可解析的入场条件，或能从点位计算矩阵中找到对应场景。
- 没有被 Judge 明确 `reject` 或硬排除。

不纳入第一版回测的内容：

- 候选池全部股票。
- 未进入最终报告的 deep dive 失败项。
- 单股问答报告。
- 没有本地行情的股票。若是沪深 A 股，可在前端“入场”页手动同步 baostock 分钟线后再重建样本。

## TradePlan 结构

每条最终输出标的先规范化为一条 `TradePlan`：

```json
{
  "schema_version": "agent_entry_execution_backtest.v1",
  "trace_id": "20260614-xxxx",
  "decision_date": "2026-06-14",
  "ts_code": "600001.SH",
  "name": "示例股票",
  "rank": 1,
  "source_stage": "portfolio_allocation",
  "entry_rule": "pullback",
  "entry_zone_low": 10.0,
  "entry_zone_high": 10.2,
  "breakout_trigger": null,
  "stop_loss_price": 9.7,
  "take_profit_price": 11.2,
  "entry_expiry_days": 5,
  "max_hold_days": 30,
  "execution_mode": "conditional_open",
  "final_action": "wait",
  "parse_status": "ok"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `decision_date` | Trace 对应的决策日，撮合从下一交易日开始 |
| `entry_rule` | `pullback`、`breakout`、`immediate`、`conditional` 或 `unknown` |
| `entry_zone_low/high` | AI 给出的入场区间 |
| `breakout_trigger` | 突破确认价，供突破对照策略使用 |
| `stop_loss_price` | 止损价 |
| `take_profit_price` | 止盈价 |
| `entry_expiry_days` | 等待入场最长交易日数，默认 5；缺失有效期时只默认买入触发窗口，超过窗口未买入则作废 |
| `max_hold_days` | 成交后最长持有交易日数，默认 30 |
| `parse_status` | `ok`、`partial`、`failed`，解析失败不得静默丢弃 |

历史 Trace 中部分点位是自然语言，第一版可以做 best-effort 解析，但必须保留 `parse_status` 与 `parse_warnings`。后续应推动点位计算层直接输出结构化数值字段，减少自然语言解析误差。

## 撮合规则

### 时间锚点

- 从 `decision_date` 后第一个交易日开始检查入场。
- 如果 `decision_date` 本身不是交易日，先映射到后续第一个可用交易日，再从该日开始。
- 行情默认使用本地 `stock_minute_bars` / `StockDaily` 缓存；只有用户点击“同步分钟线”时才通过 baostock 拉取沪深 A 股分钟线。

### 入场规则

严格 AI 入场策略 `strict_ai_entry`：

1. 如果有入场区间：
   - 当日 `low <= entry_zone_high` 且 `high >= entry_zone_low`，视为触及区间。
   - 默认成交价取 `entry_zone_high`，即保守假设只要回落到区间上沿才成交。
2. 如果只有单一入场价：
   - 当日 `low <= entry_price <= high`，按 `entry_price` 成交。
3. 如果是立即开仓：
   - 按下一交易日开盘价成交。
4. 如果超过 `entry_expiry_days=5` 仍未触发：
   - 标记为 `not_filled`。
   - 不计入成交后胜率，但计入成交率和错失收益。

### 出场规则

成交后逐日检查：

1. 当日触及止损价，按止损价退出。
2. 当日触及止盈价，按止盈价退出。
3. 如果同一天同时触及止盈和止损，第一版按保守的止损优先处理，并标记 `ambiguous_bar=true`。
4. 如果持有满 `max_hold_days=30` 仍未触发止盈或止损，按超时日收盘价强制卖出。

收益计算：

```text
pnl_pct = exit_price / entry_price - 1
```

不设置本金时，每条成交信号按 1 单位等权统计。

## 入场过保守诊断

只看成交后的 `win_rate` 会有明显偏差，因为最保守的入场价只会筛掉大量未成交信号。第一版必须把“没买到”作为核心指标。

### 核心指标

| 指标 | 说明 |
| --- | --- |
| `signal_count` | 最终报告输出的可评估信号数 |
| `parse_failed_count` | 点位无法解析的信号数 |
| `fill_rate` | 入场成交数 / 可解析信号数 |
| `conditional_win_rate` | 成交后止盈或盈利退出的比例 |
| `avg_pnl_when_filled` | 成交样本平均收益 |
| `median_pnl_when_filled` | 成交样本中位收益 |
| `timeout_rate` | 成交后超时卖出的比例 |
| `stop_loss_rate` | 成交后止损比例 |
| `take_profit_rate` | 成交后止盈比例 |
| `missed_alpha_avg` | 未成交信号在等待/持有窗口内的平均错失收益 |
| `entry_gap_pct_avg` | AI 入场价相对决策价的平均折价 |
| `opportunity_capture_rate` | 实际成交收益 / 对照策略可获得收益 |

### 未成交信号的错失收益

未成交时不能简单记为 0，也不能算作亏损。建议同时输出三类机会成本：

```text
next_open_return_20d     次日开盘买入并持有 20 日的收益
max_high_return_20d      未来 20 日最高价相对次日开盘的最大涨幅
close_to_close_return_20d 决策后 20 日收盘收益
```

如果 `strict_ai_entry` 的 `fill_rate` 很低，但这些机会成本长期为正，说明 AI 选股可能有效，问题集中在入场点位过保守。

## 四套对照策略

每条 `TradePlan` 同时跑四套策略。

### 1. strict_ai_entry

完全按 AI 给出的入场区间、止盈、止损执行。用于衡量报告本身的可执行性。

### 2. next_open_baseline

如果最终报告给出可关注或条件入场标的，则按下一交易日开盘价买入，仍使用 AI 的止盈、止损和 30 日超时退出。

用途：

- 判断 AI 选股本身是否有 alpha。
- 和 strict 策略对比，识别“选对但等不到”的情况。

### 3. atr_elastic_entry

在 AI 入场区间基础上放宽半个 ATR 或固定比例，但不越过追高线或失效约束。

第一版建议：

```text
elastic_entry_high = min(no_chase_line, entry_zone_high + 0.5 * ATR_14)
elastic_entry_low = entry_zone_low
```

如果没有 ATR 或追高线：

```text
elastic_entry_high = entry_zone_high * 1.02
```

用途：

- 测试“略微放宽入场”是否显著提升成交率。
- 如果成交率提升且收益不恶化，说明后续 prompt 应要求输出“核心入场区 + 弹性入场区”。

### 4. breakout_fallback_entry

如果股票没有回踩到 AI 入场区间，但向上突破关键价位，则允许按突破价或次日开盘入场。

第一版突破触发优先级：

1. 使用 AI 输出的 `breakout_trigger`。
2. 使用 `no_chase_line` 下方的最近高点。
3. 无法解析时不运行该策略，标记 `strategy_skipped`。

用途：

- 识别“强势股不给回踩”的情况。
- 如果 breakout 表现好，后续报告应要求 AI 输出“回踩买点 + 突破买点”双路径。

## 结果解释

建议按以下规则解释结果：

| 现象 | 结论 |
| --- | --- |
| `strict_ai_entry.fill_rate` 低，`next_open_baseline.avg_pnl` 高 | AI 选股有效，但入场太保守 |
| `strict_ai_entry.fill_rate` 低，`atr_elastic_entry.pnl` 高 | 需要放宽入场区间，优先引入 ATR 弹性入场 |
| `breakout_fallback_entry.pnl` 高，strict 未成交多 | 强势票缺少突破追随脚本 |
| 四套策略都差 | 选股质量或止损止盈结构有问题 |
| strict 胜率高但 fill_rate 极低 | 交易脚本过窄，不能只宣传胜率 |

最终报告中必须把成交率和胜率并列展示，避免形成“只看买到的样本，所以胜率很高”的幸存者偏差。

## 输出文件

建议新增离线产物：

```text
data/agent_reviews/entry_execution_backtest.jsonl
data/agent_reviews/insights/agent_entry_execution_backtest.md
```

单条 JSONL 结构：

```json
{
  "schema_version": "agent_entry_execution_backtest.v1",
  "trace_id": "20260614-xxxx",
  "decision_date": "2026-06-14",
  "ts_code": "600001.SH",
  "rank": 1,
  "trade_plan": {},
  "strategies": {
    "strict_ai_entry": {
      "status": "filled",
      "entry_date": "2026-06-17",
      "entry_price": 10.2,
      "exit_date": "2026-06-24",
      "exit_price": 11.2,
      "exit_reason": "take_profit",
      "holding_days": 5,
      "pnl_pct": 0.098,
      "ambiguous_bar": false
    },
    "next_open_baseline": {},
    "atr_elastic_entry": {},
    "breakout_fallback_entry": {}
  },
  "missed_opportunity": {},
  "warnings": []
}
```

Markdown insight 聚合维度：

- 按策略汇总。
- 按月份汇总。
- 按市场 regime 汇总。
- 按 `execution_mode` 汇总。
- 按 `rank=1/2/3` 汇总。
- 按最终动作 `open/wait/monitor` 汇总。
- 按是否有 `symbol_regime_probability` 汇总。

## 回测要求

拉单股的小时级别k线，在界面写好回测获取数据的按钮，因为每天只会产出1-2支所以拉数据并不会很耗时，保存到专门的本地数据库，数据库的表结构你来设计，前端需要展示出指标，pnl等基本信息都需要有，如果有问题先去问我。

当前实现按 baostock 支持的分钟线执行，默认使用 5 分钟 K：

- 数据源：`baostock.query_history_k_data_plus(..., frequency="5", adjustflag="3")`
- 字段：`date,time,code,open,high,low,close,volume,amount,adjustflag`
- 本地表：`stock_minute_bars`
- 唯一键：`code + bar_datetime + frequency + adjustflag`
- 前端按钮：Web“入场”页的“同步分钟线”
- 同步范围：只扫描选股报告最终输出的最多 4 只标的，不扫描候选池
- 回测优先级：本地分钟线 > 本地日线

## 建议实现路径

### 阶段一：离线最小闭环

新增：

```text
src/services/agent_entry_execution_backtest_service.py
scripts/build_agent_entry_execution_backtests.py
tests/test_agent_entry_execution_backtest_service.py
```

能力：

- 扫描最近 N 个本地 Trace。
- 提取最终报告输出标的。
- 规范化 `TradePlan`。
- 跑四套策略。
- 写入 JSONL。
- 生成聚合 Markdown。

### 阶段一补充：前端跟踪页

新增独立前端入口：

```text
GET /api/v1/agent-entry-execution-backtests
POST /api/v1/agent-entry-execution-backtests/rebuild
POST /api/v1/agent-entry-execution-backtests/minute-bars/sync
apps/dsa-web/src/pages/AgentEntryExecutionBacktestsPage.tsx
```

页面职责：

- 从 `entry_execution_backtest.jsonl` 读取样本，不重跑 Agent。
- 通过“重建样本”按钮从本地 Trace 与本地 `StockDaily` 刷新 JSONL。
- 通过“同步分钟线”按钮按最终报告标的拉取 baostock 分钟 K，写入 `stock_minute_bars` 后自动重建 JSONL。
- 展示严格 AI 入场成交率。
- 展示 `strict_ai_entry`、`next_open_baseline`、`atr_elastic_entry`、`breakout_fallback_entry` 四套策略平均收益。
- 表格逐条展示最终报告标的、入场区间、止盈止损、行情粒度、当前策略状态、收益、退出原因和 Trace。
- 表格内交互日 K 默认只标注当前策略的入场/出场点，并在图下展示图例；`next_open_baseline` 等对照策略保留在汇总指标和策略切换里，避免多个策略点位叠在同一张小图上造成误读。

导航策略：

- 侧边栏新增“入场”标签，指向 `/agent-entry-execution-backtests`。
- 旧候选池页面不作为主导航入口继续暴露；候选池质量跟踪由 Seed Pool 质量页承担。
- 保留旧 `/candidate-pool` 路由，避免历史深链立即失效。

### 阶段二：结构化点位字段

让 `pricing_agent.full.if_then_order_matrix[]` 稳定输出：

```json
{
  "entry_zone_low": 10.0,
  "entry_zone_high": 10.2,
  "stop_loss_price": 9.7,
  "take_profit_price": 11.2,
  "breakout_trigger": 10.8,
  "entry_expiry_days": 5,
  "max_hold_days": 30
}
```

自然语言字段继续保留给报告展示，但回测优先读结构化数值。

### 阶段三：只读 API 与页面

在离线产物稳定后再接：

```text
GET /api/v1/agent-entry-execution-backtests
POST /api/v1/agent-entry-execution-backtests/rebuild
```

页面展示：

- 严格入场成交率。
- 严格入场胜率。
- 对照策略收益。
- 未成交错失收益。
- 入场折价分布。
- 触发止盈/止损/超时比例。

### 阶段四：反哺 Prompt，但不自动交易

只有在样本量足够后，才把结论反哺 prompt：

- 如果 `atr_elastic_entry` 稳定优于 strict，要求 AI 输出“核心入场区 + 弹性入场区”。
- 如果 `breakout_fallback_entry` 稳定优于 strict，要求 AI 输出“回踩买点 + 突破买点”。
- 如果 next-open 表现也不好，不优化入场，优先回到选股质量问题。

## 默认参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `entry_expiry_days` | 5 | 等待入场最长交易日；缺失有效期时默认 5 个交易日内未触发买入则失效 |
| `max_hold_days` | 30 | 成交后最长持有交易日 |
| `same_day_bar_order` | `stop_first` | 同日触发止盈止损时按止损优先 |
| `position_weighting` | `equal_per_signal` | 不设本金，每条最终输出等权 |
| `max_symbols_per_trace` | 4 | 只评估最终报告输出，通常不超过 4 条 |
| `data_mode` | `manual_baostock_minute_then_local` | 前端手动同步 baostock 分钟线，回测只读本地缓存 |

## 风险与边界

- 分钟线比日线更适合判断盘中触发先后；缺分钟线退回日线时，仍需对同日触发止盈止损标记 `ambiguous_bar`。
- baostock 分钟线不覆盖指数、港股、美股和北交所；这些标的会跳过分钟同步，必要时退回日线缓存或标记行情不足。
- 自然语言点位解析可能误读，必须暴露 `parse_status`。
- 没有滑点、手续费和流动性模拟，收益只用于比较策略，不等同真实收益。
- 未成交信号不能简单算输，也不能忽略，必须单独统计错失收益。
- 样本不足时只输出描述统计，不形成 prompt 调参结论。

## 第一版验收标准

1. 能从本地选股 Trace 生成 `entry_execution_backtest.jsonl`。
2. 每个 Trace 最多纳入最终报告输出的 4 只标的。
3. 能区分 `filled`、`not_filled`、`parse_failed`、`insufficient_price_data`。
4. 四套策略结果可并列比较。
5. 汇总报告同时展示成交率、成交后胜率、平均收益、止盈止损比例和错失收益。
6. 全流程不重跑 Agent、不自动拉取外部行情、不修改线上决策；外部分钟线只在用户手动点击“同步分钟线”时拉取并缓存。
