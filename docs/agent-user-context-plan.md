# Agent 用户上下文与分阶段改造计划

本文档记录 Agent 从“通用问股助手”升级为“账户感知交易决策助手”的分阶段设计与落地状态。第一阶段已完成契约定义；第二、三阶段已接入运行时；后续阶段按本文档继续推进。

## 当前进度概览

| 阶段 | 主题 | 状态 | 说明 |
| --- | --- | --- | --- |
| 第一阶段 | 上下文契约与 planning prompt 契约 | 已完成 | `AgentUserContext` schema 与 planning prompt 已落地，并有测试覆盖。 |
| 第二阶段 | Planner 外壳 | 已完成 | 已提供确定性 Planner、capability -> tools 映射和 `AGENT_ANALYSIS_MODE=planning_execute` 实验开关。 |
| 第三阶段 | 持仓上下文接入 | 已完成 | 已从 `PortfolioService` 构造账户/持仓上下文，并在 planning_execute 模式下注入 Agent。 |
| 第四阶段 | 双报告类型 | 已完成 | 已支持 `position_review`/`entry_analysis` 意图识别；未持仓入场报告采用可见 Planning -> Execute -> 入场决策格式。 |
| 第五阶段 | Web 配置入口 | 已完成（开发调试模式） | `/agent-trace` 已支持账户、报告意图、风险偏好、交易周期、仓位/回撤/止损约束和备注调试输入；正式设置页暂缓产品化。 |
| 调试增强 | Trace UI、SSE 和本地落盘 | 已完成 | `/agent-trace`、SSE 事件流、浏览器历史和 `data/agent_traces/` 调试产物已落地。 |
| 第六阶段 | 对抗式 Debate Agent | 已完成（开发调试模式） | planning_execute 在工具证据形成后追加强制反向立场辩论和 Judge 裁决；`/agent-trace` 与 `debate.json` 可复盘。 |
| 第七阶段 | 阶段化选股流水线 | 已完成（开发调试模式） | `watchlist_scan` 优先走候选发现、初筛、单股深度分析、组合配置、反方审查和 Judge 裁决；`SelectionRunContext` 以 `summary/full/full_ref` 管理上下文并落盘 `stock_selection.json`。 |
| 长期路线图 | 工具补全、连续对话、方案托管、模拟盘、自进化、回测、regime、策略库和量化交易 | 规划中 | 目标是从“账户感知分析助手”升级为“可复盘、可托管、可验证、可迭代的 A 股交易研究系统”。 |

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

## 长期目标：从问答助手到交易研究系统

后续建设不应只围绕“多加几个工具”展开，而应围绕一条完整闭环：

```text
市场感知
  -> 用户连续对话与上下文理解
  -> planning_execute 生成可执行方案
  -> 方案落库与知识图谱记忆
  -> 模拟盘托管执行与跟踪
  -> 回测/复盘评估
  -> 策略库沉淀
  -> 自进化优化
  -> 量化交易系统
```

对 A 股新手来说，这条链路的价值是把“今天感觉可以买/不能买”拆成可验证步骤：为什么选这只、什么时候买、买多少、错了怎么办、后来有没有按计划执行、这个策略历史上靠不靠谱。

### 长期方向总览

| 方向 | 解决的问题 | 初版目标 | 关键风险 |
| --- | --- | --- | --- |
| 工具缺口补全 | Agent 看不到市场情绪、宏观、地缘风险、跨资产冲击等外部环境 | 补齐市场情绪、地缘风险、跨资产、板块催化和组合压力测试工具 | 工具太多但信号质量不稳定，导致模型误判 |
| 连续对话 | Agent 不记得用户前面说过什么、上次为什么这么判断 | 保留 session 级上下文、用户偏好、待跟踪计划和未完成问题 | 记忆污染、过期观点继续影响新判断 |
| 每次链路方案保存与模拟盘托管 | 报告说完就结束，无法跟踪是否按计划执行 | 将入场/持仓/风控方案结构化保存，并在模拟盘按条件执行或提醒 | 不能越过风控直接自动交易；初期只做模拟盘 |
| 自进化系统 | 系统无法从错误报告和失败交易里改进 | 基于回测、模拟盘、用户反馈和 Trace 复盘优化 prompt、工具权重和策略参数 | 避免让模型自行改规则后不可控 |
| 回测系统 | 不知道策略历史上是否有效 | 对每次信号、策略和组合方案做历史验证与事后评估 | A 股数据质量、复权、涨跌停、停牌、滑点会显著影响结果 |
| Regime 识别 | 同一策略在牛市、震荡、熊市、恐慌环境下表现不同 | 将市场状态作为内部约束，影响仓位、入场条件和止损纪律 | regime 不能变成泛泛大盘点评，必须服务交易动作 |
| 策略库建设 | 经验散落在 prompt 和报告里，无法复用和评估 | 沉淀可版本化策略：适用环境、入场条件、退出条件、风控和回测指标 | 策略命名很多但边界不清，容易重复和失真 |
| 量化交易系统 | 从人工读报告走向规则化执行和研究 | 先做信号生成、模拟撮合、风控网关和审计日志，再考虑真实交易接口 | 真实自动交易风险高，必须晚于模拟盘和回测闭环 |

### 阶段 A：工具缺口补全

当前工具强在个股行情、技术面、基础资金面、新闻搜索和账户上下文，但对外部风险感知不足。后续工具补全应优先服务三个问题：

1. 现在市场适不适合主动开仓？
2. 有没有战争、制裁、政策、汇率、能源、流动性等系统性冲击？
3. 这些事件会影响哪些板块、哪些持仓、哪些候选股？

建议优先补齐：

| 能力 | 建议工具 | 说明 |
| --- | --- | --- |
| 市场情绪 | `get_market_sentiment_snapshot` | 输出 `risk_on` / `neutral` / `risk_off` / `panic`，作为入场和选股前置风控 |
| 地缘风险 | `scan_geopolitical_risk_news` | 专门扫描战争、制裁、冲突、航运中断、能源供给冲击 |
| 跨资产风险 | `get_cross_asset_risk_signals` | 观察黄金、原油、美元、离岸人民币、美债、VIX、A50 等 |
| 事件到板块映射 | `map_event_to_sectors` | 判断事件利好/利空哪些板块和产业链 |
| 板块热度与拥挤 | `get_sector_heat_breadth` | 区分板块真实扩散和少数龙头脉冲 |
| 组合压力测试 | `stress_test_portfolio_by_event` | 把外部事件映射到用户持仓风险 |

第一版可以复用现有搜索服务和规则打分，不必一开始追求完整数据供应商。重点是让工具输出结构化字段：事件类型、严重度、影响市场、影响板块、可信度、证据来源、动作约束。

### 阶段 B：连续对话与用户记忆

连续对话不是简单把聊天记录塞回 prompt，而是要分层保存：

| 记忆类型 | 保存内容 | 用途 | 过期策略 |
| --- | --- | --- | --- |
| Session 记忆 | 当前对话目标、已分析股票、用户补充条件、未完成问题 | 同一轮对话不重复问、不重复查 | 会话结束后归档 |
| 用户偏好 | 风险偏好、交易周期、禁买行业、最大仓位、默认止损 | 让后续报告更贴近用户 | 用户可编辑，长期有效 |
| 方案记忆 | 每次入场/持仓/选股方案、触发条件、失效条件 | 后续跟踪“上次计划是否仍成立” | 到期、失效或被新方案覆盖 |
| 事实记忆 | 股票、板块、事件、结论之间的关系 | Graphiti 检索历史分析与事件演化 | 随时间更新 summary |

实现建议：

- `agent-trace` 继续作为主要链路，所有重要对话都应能落盘和入图。
- Graphiti 存事实和关系；关系型表存结构化方案、状态机和模拟交易记录。
- Prompt 中要区分“历史事实”和“历史观点”。历史观点只能作为参考，不能自动继承为当前结论。

### 阶段 C：方案保存与模拟盘托管

每次链路不能只保存最终 Markdown，还要保存机器可读的“交易方案”。建议新增 `AgentPlan` / `TradePlan` 概念：

```json
{
  "plan_id": "plan_xxx",
  "source_trace_id": "trace_xxx",
  "symbol": "688469",
  "intent": "entry_analysis",
  "status": "draft | active | triggered | executed | invalidated | expired | archived",
  "action": "open | add | reduce | hold | sell | wait",
  "entry_rules": [],
  "exit_rules": [],
  "risk_rules": [],
  "position_sizing": {
    "first_position_pct": 10,
    "max_position_pct": 20
  },
  "valid_until": "YYYY-MM-DD",
  "evidence_refs": [],
  "human_approval_required": true
}
```

托管到模拟盘的第一版边界必须保守：

- 默认只托管到模拟盘，不接真实券商交易。
- 默认需要用户确认后才把方案设为 `active`。
- 自动动作只允许在明确规则触发时执行，例如“价格回踩到区间且未跌破止损”。
- 每一次模拟买入、卖出、撤单、失效都必须写审计日志。
- 如果行情、资金、情绪或风控工具失败，不能自动执行，只能提醒。

模拟盘托管的核心不是“自动赚钱”，而是验证 Agent 的计划有没有执行价值：计划是否清晰、触发是否合理、止损是否有效、回撤是否可控。

### 阶段 D：回测系统

回测系统要分两层：

1. 信号回测：某个策略信号出现后，未来 N 天收益、最大回撤、胜率如何。
2. 方案回测：按 Agent 给出的入场区间、止损、止盈、仓位规则模拟执行。

建议先做事件驱动的轻量回测：

| 对象 | 指标 | 说明 |
| --- | --- | --- |
| 单次计划 | 是否触发、触发价、退出原因、收益率、最大回撤 | 判断这次 Agent 方案是否可执行 |
| 单个策略 | 胜率、平均收益、盈亏比、最大回撤、信号频率 | 判断策略是否值得保留 |
| 市场状态 | 不同 regime 下的策略表现 | 判断策略适合牛市、震荡还是风险释放 |
| 工具组合 | 有/无资金面、有/无情绪工具时表现差异 | 判断哪个工具真的提升质量 |

A 股回测要特别注意：

- 前复权/后复权口径。
- 涨跌停导致买不进/卖不出。
- 停牌、复牌、除权除息。
- 滑点、手续费、印花税。
- 不能用未来数据，例如用收盘后数据假装盘中可见。

### 阶段 E：Regime 识别

`regime_detection` 应继续作为内部决策输入，不做花哨的大盘报告。建议输出保持短而硬：

```json
{
  "market": "cn",
  "regime": "trending_up | range_bound | trending_down | high_volatility | risk_off | event_driven | panic | unknown",
  "risk_level": "low | medium | high | critical",
  "position_bias": "increase_allowed | neutral | reduce_only | no_new_entry",
  "evidence": [],
  "constraints": []
}
```

它影响的是：

- 是否允许新开仓。
- 首仓比例上限。
- 是否必须等待回踩确认。
- 止损是否收紧。
- 选股偏进攻还是防守。
- Debate Judge 是否应默认偏保守。

Regime 输入建议来自：

- 指数趋势与市场宽度。
- 板块轮动和涨跌停结构。
- 成交额和流动性。
- 北向/融资/ETF 等资金指标。
- 市场情绪与地缘风险工具。
- 跨资产风险信号。

### 阶段 F：策略库建设

策略库不是 prompt 片段合集，而是可版本化、可回测、可复盘的交易方法集合。

每个策略至少包含：

| 字段 | 含义 |
| --- | --- |
| `strategy_id` | 稳定 ID |
| `name` | 策略名称 |
| `style` | 趋势、回踩、低吸、事件驱动、价值、防守等 |
| `suitable_regimes` | 适用市场状态 |
| `entry_conditions` | 入场条件 |
| `exit_conditions` | 退出条件 |
| `risk_controls` | 止损、仓位、禁用条件 |
| `required_tools` | 必须使用的工具 |
| `backtest_metrics` | 回测指标 |
| `version` | 策略版本 |
| `status` | experimental / active / deprecated |

建议先建设少量清晰策略，而不是一次性堆很多名字：

- 强势板块回踩策略。
- 放量突破但不追高策略。
- 低波动价值防守策略。
- 事件冲击避险策略。
- 业绩改善拐点策略。

策略库要和回测系统绑定。没有回测和模拟盘表现的策略，最多只能是 experimental。

### 阶段 G：自进化系统

自进化不是让模型自己随便改代码，而是建立“提出改进 -> 离线验证 -> 人审 -> 上线”的闭环。

可进化对象：

- Prompt 输出结构。
- Planner 的能力域选择。
- 工具调用顺序。
- 策略参数，例如乖离率阈值、首仓比例、止损比例。
- Regime 下的仓位上限。
- Debate Judge 的裁决权重。

输入信号：

- 回测表现。
- 模拟盘收益和回撤。
- 用户反馈：有用/无用、执行/未执行。
- Trace 中的工具失败、数据缺口、模型误判。
- 真实市场后验：计划是否触发、是否失效。

安全边界：

- 自进化系统只能生成候选改动，不能直接改生产策略。
- 每个改动必须附带证据：改了什么、为什么、在哪些样本上提升、在哪些场景变差。
- 所有策略参数变更必须保留版本和回滚路径。
- 真实交易前必须有人审；模拟盘可以自动试验。

### 阶段 H：量化交易系统

量化交易系统应放在后期，不应跳过前面的模拟盘、回测和风控。

建议分层：

```text
Signal Layer       生成候选信号和置信约束
Strategy Layer     将信号转换为策略规则
Risk Layer         仓位、止损、总暴露、禁买条件
Portfolio Layer    多标的组合分配
Execution Layer    模拟撮合或真实下单
Audit Layer        记录每次决策、订单、成交、撤单和原因
Review Layer       回测、复盘、自进化反馈
```

真实交易接口必须晚于以下条件：

- 模拟盘稳定运行一段时间。
- 回测覆盖主要策略和典型市场状态。
- 每个策略有明确禁用条件。
- 风控网关可阻止超仓、追高、数据缺失、恐慌状态下开仓。
- 每笔真实交易都支持人工确认或至少支持一键熔断。

对当前项目来说，合理顺序是：

1. 先把 Agent 方案结构化保存。
2. 再做模拟盘托管。
3. 再做回测和策略库。
4. 再做自进化闭环。
5. 最后才考虑量化交易和真实下单。

## 第一阶段目标（已完成）

第一阶段只做“上下文契约”和“实施计划”，当前已完成：

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

该文件先沉淀角色定义、核心原则、任务分类、planning -> execute 协议、账户感知规则、事件触发规则和输出约束。后续阶段已把该 prompt 契约接入 `planning_execute` 运行时。

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

执行约束：

- 如果用户没有提供股票代码或候选池，Planner 必须先进入 `watchlist_discovery`，调用 `discover_watchlist_candidates` 生成候选股池。
- 候选发现只代表“可继续分析的种子列表”，不代表推荐；最终排序必须基于候选后续的行情、技术、消息和资金证据。
- 如果候选发现失败或候选为空，最终报告必须明确写“候选池不足，无法完成选股排序”，不能只基于指数/板块排行给出具体组合。

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

### 第四阶段：双报告类型（已完成）

计划任务：

- [x] 新增 `position_review` 和 `entry_analysis` 意图识别。
- [x] 已持仓时默认进入持仓诊断。
- [x] 无持仓且用户问“能不能买/是否适合入场”时默认进入入场分析。
- [x] `position_review` 的 prompt 输出约束已落到 `src/agent/planning_prompts.py`，覆盖结论、持仓位置、关键价格、证据摘要、未来价格情景、分层行动计划和风险缺口。
- [x] 持仓报告约束已补充休市/非交易日行情口径说明，避免把最近交易日涨跌幅误写成“今日涨跌幅”。
- [x] 持仓报告约束已要求结合账户成本、仓位、现金和风险偏好给出动作建议，避免只输出一句“持有/不急着加仓”。
- [x] `entry_analysis` 已补齐专项输出规范，采用可见 Planning -> Execute -> 入场决策格式，并包含入场决策表格、理想/次优入场区间、禁止追高线、首仓比例、加仓条件、止损位、目标位、淘汰条件和复查触发。
- [x] 从用户角度复核后，普通聊天/分析页暂不强制暴露报告类型选择；默认由 Planner 根据持仓上下文和用户问题自动识别，避免增加用户理解成本。后续若用户明确需要手动覆盖，再作为 Web 产品化增强项处理。

当前实现：

- `src/agent/planner.py` 已根据 `AgentUserContext` 和用户问题输出 `position_review_report` 或 `entry_analysis_report`。
- `src/agent/planning_prompts.py` 已包含 `## 持仓报告输出规范（position_review）` 和 `## 入场报告输出规范（entry_analysis）`；其中 `entry_analysis` 明确要求输出 Planning 摘要、Execute 证据摘要和入场决策表格。
- `tests/test_agent_planner.py`、`tests/test_planning_prompts.py` 已覆盖持仓/非持仓意图、持仓报告输出约束和入场报告输出约束。

### 第五阶段：Web 配置入口（已完成，保留开发调试模式）

计划任务：

- [x] 新增 `/agent-trace` 开发者调试页面，可选择账户并输入风险偏好、交易周期和用户画像备注。
- [x] `/agent-trace` 支持报告意图覆盖：自动识别、持仓诊断、入场分析、账户风控和事件影响。
- [x] `/agent-trace` 支持调试单票上限、总权益仓位上限、最大可接受回撤和默认止损。
- [x] `/agent-trace` 顶部 `Context In Use` 会展示本次实际注入的账户、目标持仓、成本、仓位、浮盈亏和画像摘要。
- [x] `/agent-trace` 支持 SSE 流式展示 context、planner、thinking、tool_start、tool_done 和 done/error，避免运行期间黑箱等待。
- [x] `/agent-trace` 支持浏览器本地历史，便于回看已完成的执行链路。
- [x] `/agent-trace` 状态栏会展示后端 `Artifact` 路径，方便定位本次落盘调试产物。
- [x] 从当前目标看，正式设置页/持仓页画像配置暂缓产品化；第五阶段保留开发调试模式，避免为普通用户增加额外表单负担。

当前实现：

- `apps/dsa-web/src/pages/AgentTracePage.tsx` 提供 Agent Trace 调试界面。
- `api/v1/endpoints/agent.py` 的 `AgentTraceRunRequest` 支持 `account_id`、`stock_code`、`report_intent`、`risk_preference`、`trading_horizon`、`max_single_position_pct`、`max_total_equity_exposure_pct`、`max_acceptable_drawdown_pct`、`default_stop_loss_pct` 和 `investor_notes`。
- `apps/dsa-web/src/components/layout/SidebarNav.tsx` 已加入“链路”入口。

### 调试增强：Trace UI 与本地落盘（已完成）

计划任务：

- [x] 新增 `/api/v1/agent/trace/run` 和 `/api/v1/agent/trace/stream`。
- [x] 前端单独提供 `/agent-trace` 界面，展示 context、planner、工具调用、事件时间线和最终 Markdown 输出。
- [x] 后端按 session 落盘调试产物到 `data/agent_traces/<timestamp>-<session_id>/`。
- [x] 落盘文件包括 `request.json`、`context.json`、`planner.json`、`events.ndjson`、`tool_calls.json`、`evidence_ledger.json`、`debate.json`、`final.md`、`todo.md` 和 `summary.json`。
- [x] `todo.md` 会在初始化时写入计划，在执行结束后补充工具成功/失败、参数、结果预览、未调用计划工具和 Execute Protocol 复核状态。
- [x] `evidence_ledger.json` 会按工具调用整理 `tool`、`arguments`、`status`、`evidence`、`limitation` 和 `impact`，便于离线复盘。

当前实现：

- `api/v1/endpoints/agent.py` 提供 Trace API、SSE 和 `TraceArtifactWriter`。
- `src/agent/planning_prompts.py` 已新增独立 `Execute Protocol`，明确 Evidence Ledger、工具失败降级、停止条件、Trace artifacts 和最终输出审计门槛。
- `tests/test_agent_models_api.py` 已覆盖 Trace run/stream、落盘文件和 `todo.md` 执行状态。

### 第六阶段：对抗式 Debate Agent

单 Agent 自我反思容易变成形式化反思，实际输出仍倾向于支持自己的初始结论。本阶段已引入独立反方 Agent，让主观点和反观点基于同一份证据进行对抗式辩论，再由 Judge Agent 做最终裁决。

采用的模式是“强制反向立场辩论 + Judge 最终裁决”：

- 主观点 Agent 先给出 primary thesis，例如看多、持有、加仓、开仓。
- 反方 Agent 必须站到相反方向，例如看空、减仓、不入场、等待。
- 反方 Agent 不能编造数据，只能基于同一份工具证据构造最强反证。
- 双方都必须给出自己的失效条件，而不是只证明自己正确。
- Judge Agent 不做简单折中，而是按证据强弱、账户风险、数据可靠性和用户目标裁决。

当前角色：

| 角色 | 职责 |
| --- | --- |
| `PrimaryThesisAgent` | 基于 planner 和工具证据生成主观点、主动作、入场/持仓计划和失效条件 |
| `AdversarialThesisAgent` | 强制站在相反方向，构造最强反证、反向动作计划和主观点失效条件 |
| `DebateJudgeAgent` | 对双方证据和矛盾点做裁决，输出最终动作、接受/驳回的论点和风控条件 |
| `RiskGateAgent` | 暂未单独实现；当前由 `DebateJudgeAgent` 的 risk_controls 覆盖基础风控条件 |

当前流程：

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

当前实现：

- `src/agent/debate.py` 提供 `PrimaryThesisAgent`、`AdversarialThesisAgent` 和 `DebateJudgeAgent` 的轻量运行时编排。
- `src/agent/executor.py` 只在 `planning_execute`、存在 `AgentUserContext` 且已有成功工具证据时触发 Debate；`normal` 模式不受影响。
- `src/agent/tools/market_tools.py` 提供 `discover_watchlist_candidates`，用于 watchlist_scan 在无用户候选股票代码时先生成候选池；`src/agent/stock_selection.py` 在 planning_execute 的 watchlist_scan 下优先执行阶段化选股流水线，并通过 `SelectionRunContext` 管理候选发现、初筛、深度分析、组合配置、反方审查和 Judge 裁决；`src/agent/runner.py` 仍保留审计兜底，阻止空候选 watchlist_scan 直接输出最终选股结论。
- Debate 三方共用同一份 `Shared Evidence Bundle`，包含用户问题、主报告、`AgentUserContext`、Planner 和工具 Evidence Ledger。
- `api/v1/endpoints/agent.py` 的 Trace 响应和 SSE `done` 事件会返回 `debate` 字段，并在调试目录写入 `debate.json`。
- `apps/dsa-web/src/pages/AgentTracePage.tsx` 新增 `Debate Judge` 模块，展示主观点、反方观点、Judge 裁决、分维度证据、采纳/驳回论点和风控条件；Judge 输出会显式区分账户风险、技术面、资金面、消息面、基本面和数据质量。
- 开发调试模式下，`debate.debug_outputs` 会保留同一 session 内的原始主报告输出、Primary Thesis 原始输出、Opposing Thesis 原始输出、Judge 原始输出和最终合并输出；这些是模型可见输出和结构化 JSON，不包含隐藏思维链。
- SSE 事件会记录 `debate_start`、`debate_primary_done`、`debate_opposing_done` 和 `debate_judge_done`，便于在 Evidence Timeline 中定位 Debate 每一步。

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

## 当前验收状态

- [x] schema 能被 Python 编译。
- [x] 文档清楚说明字段语义和后续接入方式。
- [x] `planning_execute` 默认仍由实验开关控制，不改变 `normal` 默认行为。
- [x] Planner 能输出能力域、工具执行计划、缺失工具、风险检查和期望报告类型。
- [x] 普通单股 Agent 分析能在 `planning_execute` 模式下注入 `AgentUserContext`。
- [x] `/agent-trace` 能展示账户上下文、Planner、SSE 事件、工具调用和最终输出。
- [x] Trace 运行能落盘 request/context/planner/events/tool calls/evidence ledger/debate/final/todo/summary。
- [x] README 未继续膨胀，细节保留在专题文档中。
- [x] `entry_analysis` 专项输出规范已补齐到与 `position_review` 同等级的可执行程度，并使用可见 Planning -> Execute 格式。
- [x] 第五阶段按开发调试模式收口；正式用户画像配置入口暂缓产品化，不作为当前阶段阻塞项。
- [x] Debate Agent 已按“强制反向立场辩论 + Judge 最终裁决”接入 planning_execute，覆盖持仓模式和选股/入场模式。
