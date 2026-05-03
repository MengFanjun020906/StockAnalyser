# Agent 用户上下文与分阶段改造计划

本文档记录 Agent 从“通用问股助手”升级为“账户感知交易决策助手”的第一阶段设计。当前阶段只定义契约和实施边界，不改变现有分析执行链路。

## 背景

当前系统已经具备：

- 普通单股分析流水线
- Agent ReAct 工具调用模式
- 多 Agent 编排模式
- Web 持仓管理、账户、交易、现金流水和快照
- Agent 工具 `get_portfolio_snapshot`
- 确定性技术信号与 LLM 决策仪表盘

下一步希望改善三个方向：

1. Agent 执行改为 planning -> execute 格式。
2. Agent 不只回答用户 prompt，而是结合用户账户类型、持仓数量、持仓成本、融资融券状态等上下文给出个性化建议。
3. 支持多类型报告：已持仓报告和选股/入场报告采用不同分析目标和输出结构。

## 第一阶段目标

第一阶段只做“上下文契约”和“实施计划”：

- 定义投资者画像、账户上下文、持仓上下文、报告意图 schema。
- 明确这些 schema 与现有 `PortfolioService` 的关系。
- 明确 planning-execute 后续接入点。
- 不改变当前 Agent、Pipeline、API、Web 的实际行为。

新增 schema 位于：

```text
src/schemas/agent_context.py
```

该文件是后续 API、配置导入、Agent Prompt、Planner 输入的统一契约。

同时新增 planning Agent system prompt 契约：

```text
src/agent/planning_prompts.py
```

该文件先沉淀角色定义、核心原则、任务分类、planning -> execute 协议、账户感知规则、事件触发规则和输出约束。当前阶段暂不接入运行时。

Prompt 中已包含股票/账户领域的 `ANALYSIS_DIMENSIONS`，将能力域划分为：

- 技术面 `technical_analysis`
- 实时行情与量价 `realtime_quote`
- 筹码与成本结构 `chip_distribution`
- 资金面 `capital_flow`
- 基本面与估值 `fundamental_analysis`
- 板块与行业 `sector_industry`
- 市场状态 `regime_detection`
- 消息面与事件 `news_event`
- 舆情与情绪 `sentiment_analysis`
- 账户与持仓 `portfolio_context`
- 历史表现与回测 `backtest_memory`

并补充了意图识别、市场状态识别、信号生成、账户化决策、风险评估和事件触发判断等决策能力域。

## 为什么不重建持仓系统

项目现有持仓管理已经覆盖：

- 账户创建与维护
- 买卖交易
- 现金流水
- 公司行动
- FIFO / AVG 成本法
- 持仓快照
- 汇率缓存
- 风险摘要
- CSV 导入

因此后续 Agent 个性化分析应该复用现有持仓账本，而不是再设计一套独立的账户/持仓存储。

本阶段新增的 `AgentUserContext` 是“决策上下文”，不是账本系统。它可以来自：

- 现有 `PortfolioService.get_portfolio_snapshot()`
- 用户临时输入
- 配置文件
- Web 表单
- API 请求

## 核心 schema

### `InvestorProfile`

描述用户的长期偏好和风险约束：

| 字段 | 含义 |
| --- | --- |
| `profile_id` | 用户画像 ID |
| `display_name` | 显示名 |
| `risk_preference` | `conservative` / `balanced` / `aggressive` |
| `trading_horizon` | `intraday` / `short_term` / `swing` / `medium_term` / `long_term` |
| `preferred_markets` | 偏好市场：`cn` / `hk` / `us` / `mixed` |
| `max_single_position_pct` | 单票最大仓位 |
| `max_total_equity_exposure_pct` | 总权益仓位上限 |
| `max_acceptable_drawdown_pct` | 最大可接受回撤 |
| `default_stop_loss_pct` | 默认止损比例 |
| `allow_margin` | 是否允许融资 |
| `allow_short_selling` | 是否允许融券/做空 |
| `notes` | 其他备注 |

### `AccountContext`

描述账户约束：

| 字段 | 含义 |
| --- | --- |
| `account_id` | 对应现有持仓账户 ID |
| `account_name` | 账户名称 |
| `broker` | 券商 |
| `account_type` | `cash` / `margin` / `simulated` / `retirement` / `other` |
| `margin_mode` | `none` / `margin` / `short` / `margin_and_short` |
| `market` | 账户主要市场 |
| `base_currency` | 本位币 |
| `total_equity` | 总权益 |
| `available_cash` | 可用现金 |
| `total_market_value` | 持仓市值 |
| `margin_debt` | 融资负债 |
| `maintenance_ratio` | 维持担保或维持保证金比例 |
| `risk_line_ratio` | 警戒线/平仓线参考 |
| `cost_method` | `fifo` / `avg` |

### `PositionContext`

描述单个持仓：

| 字段 | 含义 |
| --- | --- |
| `symbol` | 股票代码 |
| `market` | 市场 |
| `account_id` | 所属账户 |
| `quantity` | 持仓数量 |
| `avg_cost` | 平均成本 |
| `total_cost` | 总成本 |
| `last_price` | 当前价或快照价 |
| `market_value` | 市值 |
| `unrealized_pnl` | 浮盈亏 |
| `unrealized_pnl_pct` | 浮盈亏比例 |
| `position_pct` | 仓位占比 |
| `holding_days` | 持仓天数 |
| `stop_loss` | 用户已有止损位 |
| `take_profit` | 用户已有目标位 |
| `thesis` | 原始买入逻辑 |
| `notes` | 备注 |

### `ReportContext`

描述本次报告意图：

| 字段 | 含义 |
| --- | --- |
| `intent` | `auto` / `position_review` / `entry_analysis` / `watchlist_scan` / `risk_review` / `event_impact` / `qa` |
| `analysis_mode` | `normal` / `planning_execute` / `agent_react` / `multi_agent` |
| `target_symbols` | 目标股票列表 |
| `primary_symbol` | 主分析股票 |
| `language` | `zh` / `en` |
| `include_entry_plan` | 是否输出入场计划 |
| `include_position_plan` | 是否输出持仓处理计划 |
| `include_risk_review` | 是否输出风控评估 |
| `include_watchlist_ranking` | 是否输出候选排序 |
| `user_prompt` | 用户原始问题 |

### `AgentUserContext`

顶层结构：

```json
{
  "schema_version": "2026-05-02",
  "investor": {},
  "accounts": [],
  "positions": [],
  "report": {},
  "metadata": {}
}
```

## Planning -> Execute 目标形态

后续 planner 的职责不是直接给结论，而是先决定“怎么分析”。

建议的输出结构：

```json
{
  "intent": "position_review",
  "primary_symbol": "600519",
  "has_position": true,
  "required_tools": [
    "get_realtime_quote",
    "get_daily_history",
    "analyze_trend",
    "get_portfolio_snapshot",
    "get_capital_flow",
    "search_comprehensive_intel"
  ],
  "risk_checks": [
    "position_size",
    "drawdown",
    "margin_pressure",
    "stop_loss_distance",
    "negative_news"
  ],
  "expected_output": "position_review_report"
}
```

执行器再按计划调用工具，最后交给决策 Agent 输出报告。

### Capability -> Tools 映射层

当前项目没有 `get_tools_for_capability` 这类函数。Agent 工具是通过 `ToolRegistry` 按工具名注册和调用的。

后续 planning-execute 可以新增一层能力域到工具列表的映射，让 Planner 先选择能力域，再由执行器展开为实际工具。建议第一版映射：

| Capability | Tools |
| --- | --- |
| `technical_analysis` | `analyze_trend`, `calculate_ma`, `get_volume_analysis`, `analyze_pattern` |
| `realtime_quote` | `get_realtime_quote` |
| `portfolio_context` | `get_portfolio_snapshot` |
| `news_event` | `search_comprehensive_intel`, `search_stock_news` |
| `capital_flow` | `get_capital_flow` |
| `fundamental_analysis` | `get_stock_info` |
| `chip_distribution` | `get_chip_distribution` |
| `regime_detection` | `detect_market_regime`，第一版可由 `get_market_indices`, `get_sector_rankings`, `get_volume_analysis` 组合实现 |
| `market_context` | `get_market_indices`, `get_sector_rankings` |
| `backtest_memory` | `get_skill_backtest_summary`, `get_strategy_backtest_summary`, `get_stock_backtest_summary` |

这样可以避免在 prompt 里硬编码过多工具细节，也方便后续按账户类型、市场、任务意图裁剪工具集。

### 市场状态识别能力

`regime_detection` 不作为“大盘报告”能力使用，而是作为 Agent 内部决策约束，用来判断当前市场环境是否支持把个股信号转化为真实动作。

建议输出结构保持短而可判定：

```json
{
  "market": "cn",
  "regime": "trending_up | trending_down | range_bound | high_volatility | risk_off | event_driven | unknown",
  "risk_level": "low | medium | high",
  "index_alignment": {},
  "breadth": {},
  "sector_rotation": {},
  "liquidity": {},
  "evidence": [],
  "conflicts": [],
  "data_quality": "sufficient | limited | insufficient"
}
```

使用边界：

- 用户问“能不能买、要不要加仓、要不要卖、某事件对持仓影响”时，Planner 应默认纳入 `regime_detection`。
- 用户只问财报解释、概念解释、历史复盘时，可以不调用，避免稀释核心问题。
- `regime_detection` 只调整仓位、入场激进程度、止损纪律和信号有效期，不直接覆盖账户约束和个股证据。
- 当市场处于 `risk_off` 或 `high_volatility` 时，即使个股信号偏多，也应降低仓位上限、提高确认条件、明确失效价位。
- 当市场处于 `range_bound` 时，优先考虑回踩、箱体边界和等待确认，避免追高型入场。
- 当市场处于 `event_driven` 时，提高消息面、资金面和事件时效权重，并说明技术指标可能滞后。

在 Debate Agent 中，`regime_detection` 应进入双方共享的 `Shared Evidence Bundle`：

- 主观点 Agent 可以引用它证明市场环境支持执行。
- 反方 Agent 可以引用它挑战入场/加仓的风险收益比。
- Judge Agent 用它做最终动作的风险调节，而不是机械折中。

## 报告类型规划

### `position_review`

适用于已持仓。

重点问题：

- 现在是继续持有、加仓、减仓、止盈还是止损？
- 当前价相对持仓成本是否安全？
- 浮盈浮亏是否触发风控？
- 仓位是否过重？
- 融资融券账户是否有杠杆压力？
- 原始买入逻辑是否仍成立？

建议输出：

- 持仓动作：持有 / 加仓 / 减仓 / 止盈 / 止损
- 成本安全垫
- 仓位建议
- 止损位
- 止盈/移动止盈
- 风险触发条件
- 持仓检查清单

### `entry_analysis`

适用于选股或准备开仓。

重点问题：

- 这只股票是否值得加入候选？
- 当前能不能买？
- 如果不能买，等什么条件？
- 第一笔仓位多大？
- 止损和目标位在哪里？

建议输出：

- 是否入选候选池
- 理想入场点
- 次优入场点
- 禁止追高线
- 首仓比例
- 加仓条件
- 止损位
- 目标位
- 淘汰条件

### `watchlist_scan`

适用于多股筛选。

重点输出：

- 候选排序
- 每只股票的入选/淘汰原因
- 最优先观察对象
- 今日不适合交易对象

### `risk_review`

适用于账户风控。

重点输出：

- 单票集中度
- 总仓位
- 回撤
- 止损接近度
- 融资压力
- 行业集中度

## 与现有持仓模块的关系

后续推荐转换路径：

```text
PortfolioService.get_portfolio_snapshot()
  -> AccountContext[]
  -> PositionContext[]
  -> AgentUserContext
  -> Planner
  -> Tool execution
  -> Decision report
```

如果用户没有录入持仓：

- `positions=[]`
- `intent=entry_analysis` 或 `auto`
- Agent 进入选股/入场分析模式

如果用户已有持仓：

- `positions` 包含对应股票
- `intent=position_review` 或 `auto`
- Agent 进入持仓诊断模式

## 隐私和安全边界

账户和持仓信息属于敏感信息。后续接入时应遵守：

- 不把账户数据写进日志明文，尤其是总资产、融资负债、持仓成本。
- Web/API 响应按当前认证状态控制访问。
- 对外部 LLM 发送前尽量只传决策必要字段。
- 允许用户关闭账户上下文注入。
- 默认保留无账户模式，不能强迫用户配置真实资产。

## 后续阶段建议

### 第二阶段：Planner 外壳（已完成）

- 新增 planner 输入 `AgentUserContext`。
- 复用 `src/agent/planning_prompts.py` 中的 system prompt。
- 新增 capability -> tools 映射层，将 `technical_analysis`、`portfolio_context`、`regime_detection`、`news_event` 等能力域展开为当前 ToolRegistry 中的实际工具。
- 输出工具执行计划。
- 不改变现有工具实现。
- 普通单股分析先支持 `analysis_mode=planning_execute` 实验开关。
- 第一版 `regime_detection` 可以先复用现有指数、板块、量价和市场宽度数据生成结构化市场状态；后续再独立沉淀为 `detect_market_regime` 工具。

当前实现：

- `src/agent/planner.py` 提供确定性 Planner 外壳和 `CAPABILITY_TOOL_MAP`。
- `build_planning_result()` 会根据 `AgentUserContext.report.intent`、`primary_symbol` 和是否已有持仓选择能力域、风险检查项和期望输出类型。
- 映射层只展开当前 `ToolRegistry` 已注册的工具，缺失工具记录在 `missing_tools`，不假设工具一定存在。
- `AGENT_ANALYSIS_MODE=planning_execute` 作为实验开关；默认 `normal`，不改变现有 Agent 行为。
- 第一版 `regime_detection` 仍按计划由 `get_market_indices`、`get_sector_rankings`、`get_volume_analysis` 组合表达，暂不新增独立工具。

### 第三阶段：持仓上下文接入（已完成）

- 从 `PortfolioService` 生成 `AgentUserContext`。
- 支持按 `account_id`、`symbol`、`cost_method` 构造上下文。
- 将持仓上下文注入 Agent Prompt。

当前实现：

- `src/agent/context_builder.py` 将 `PortfolioService.get_portfolio_snapshot()` 输出转换为 `AgentUserContext`。
- 普通单股 Agent 分析在 `AGENT_ANALYSIS_MODE=planning_execute` 时，会 best-effort 构造账户上下文并注入 Agent prompt。
- 上下文包含账户、现金、总权益、持仓数量、成本、市值、浮盈亏、仓位占比和价格可用性备注。
- 构造失败会回退到无账户上下文，不阻断分析主流程，也不向日志明文输出账户明细。

### 第四阶段：双报告类型

- 新增 `position_review` 和 `entry_analysis` 输出约束。
- `position_review` 的 prompt 草案已先落到 `src/agent/planning_prompts.py`，覆盖结论、持仓位置、关键价格、证据摘要、行动计划和风险缺口。
- 已持仓时默认用持仓诊断。
- 无持仓时默认用入场分析。

### 第五阶段：Web 配置入口

- 在设置或持仓页补充投资者画像。
- 支持风险偏好、交易周期、单票上限、默认止损。
- 支持报告类型选择。

### 第六阶段：对抗式 Debate Agent

单 Agent 自我反思容易变成形式化反思，实际输出仍倾向于支持自己的初始结论。后续可以引入独立反方 Agent，让主观点和反观点基于同一份证据进行对抗式辩论，再由 Judge Agent 做最终裁决。

采用的模式是“强制反向立场辩论 + Judge 最终裁决”：

- 主观点 Agent 先给出 primary thesis，例如看多、持有、加仓、开仓。
- 反方 Agent 必须站到相反方向，例如看空、减仓、不入场、等待。
- 反方 Agent 不能编造数据，只能基于同一份工具证据构造最强反证。
- 双方都必须给出自己的失效条件，而不是只证明自己正确。
- Judge Agent 不做简单折中，而是按证据强弱、账户风险、数据可靠性和用户目标裁决。

建议角色：

| 角色 | 职责 |
| --- | --- |
| `PrimaryThesisAgent` | 基于 planner 和工具证据生成主观点、主动作、入场/持仓计划和失效条件 |
| `AdversarialThesisAgent` | 强制站在相反方向，构造最强反证、反向动作计划和主观点失效条件 |
| `DebateJudgeAgent` | 对双方证据和矛盾点做裁决，输出最终动作、接受/驳回的论点和风控条件 |
| `RiskGateAgent` | 可选后置风控门，检查最终动作是否违反仓位、杠杆、止损和账户约束 |

建议流程：

```text
AgentUserContext
  -> Planner
  -> Shared Evidence Bundle
  -> PrimaryThesisAgent
  -> AdversarialThesisAgent
  -> Debate rounds
  -> DebateJudgeAgent
  -> Final account-aware action plan
```

核心约束：

- 双方必须使用同一份 `Shared Evidence Bundle`，避免各自选择性找数据。
- 反方 Agent 的职责是构造最强 opposing case，不是为了反对而编造理由。
- 每轮辩论必须围绕证据、反证、矛盾数据、失效条件和账户影响展开。
- 工具 confidence 仍只作内部可靠性判断，最终输出不得展示 confidence 字段。
- 如果双方证据冲突且 Judge 无法裁决，最终结果应为 `no_trade` 或 `insufficient_data`，而不是强行给买卖建议。

建议结构化状态：

```json
{
  "primary_thesis": {
    "direction": "bullish",
    "action": "hold | add | open",
    "evidence": [],
    "failure_conditions": []
  },
  "opposing_thesis": {
    "direction": "bearish",
    "action": "reduce | wait | reject",
    "evidence": [],
    "failure_conditions": []
  },
  "debate_rounds": [],
  "unresolved_conflicts": [],
  "judge_decision": {
    "winner": "primary | opposing | no_trade | insufficient_data",
    "final_action": "",
    "accepted_arguments": [],
    "rejected_arguments": [],
    "risk_controls": []
  }
}
```

这个阶段的目标不是让输出更长，而是让最终结论经受独立反方检验。对于已持仓报告，反方重点挑战“继续持有/加仓”的安全性；对于入场报告，反方重点挑战“现在入场”的风险收益比。

## 当前阶段验收标准

- schema 能被 Python 编译。
- 文档清楚说明字段语义和后续接入方式。
- 当前分析流程、API 和 Web 行为不改变。
- README 只说明本 fork 的定位，不堆具体字段细节。
