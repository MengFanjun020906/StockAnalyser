# Agent 用户上下文与分阶段改造计划

本文档记录 Agent 从“通用问股助手”升级为“账户感知交易决策助手”的分阶段设计、当前落地状态和后续路线图。状态口径截至 2026-05-10：早期的上下文、Planner、Trace、Debate、阶段化选股和确定性 `risk_gate` 已经进入开发调试链路；方案保存、模拟盘托管、回测/Trust Score 和真实交易仍是后续阶段。

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
| 第八阶段 | A 股硬风控与结构化协议 | 已完成底座 | `src/schemas/agent_signal.py` 与 `RiskGateEvaluator` 已覆盖 L1/L2/L3、TradePlan、T+1、涨跌停、特殊股票、止损、数据质量、仓位和现金约束；Trace 会落盘 `risk_gate.json`。 |
| 候选池增强 | Sequoia 风格量化候选池 | 已完成开发链路 | `discover_watchlist_candidates` 的 `auto` 模式已合并 Sequoia 量化候选、强势板块成分股、用户种子和 fallback，并向 Trace 暴露候选来源、策略标签、评分和理由。 |
| 知识图谱记忆 | Graphiti 最小集成 | 已完成最小链路 | 已提供可选 Neo4j/Graphiti 配置、分析结果入图和 Agent 知识图谱检索工具；仍属于增强能力，不是 planning_execute 的硬依赖。 |
| 长期路线图 | 工具补全、连续对话、方案托管、模拟盘、自进化、回测、regime、策略库和量化交易 | 规划中 | 目标是从“账户感知分析助手”升级为“可复盘、可托管、可验证、可迭代的 A 股交易研究系统”。 |

## 背景

当前系统已经具备：

- 普通单股分析流水线
- Agent ReAct 工具调用模式
- 多 Agent 编排模式
- Web 持仓管理、账户、交易、现金流水和快照
- Agent 工具 `get_portfolio_snapshot`
- 确定性技术信号与 LLM 决策仪表盘
- planning_execute 实验链路、确定性 Planner、Agent Trace、SSE 和本地调试产物
- 对抗式 Debate、阶段化 `watchlist_scan` 选股流水线和 `risk_gate` 风控审计
- Sequoia 风格量化候选池、候选来源审计和 Graphiti 知识图谱最小集成

早期计划中的三个目标已经进入开发调试链路：

1. Agent 执行已支持 planning -> execute 格式，并通过 `AGENT_ANALYSIS_MODE=planning_execute` 控制。
2. Agent 可从 `PortfolioService` 构造 `AgentUserContext`，结合账户、持仓、成本、仓位、现金和风险偏好输出个性化建议。
3. 报告类型已覆盖 `position_review`、`entry_analysis`、`watchlist_scan`、`risk_review` 和 `event_impact`，并在选股场景走阶段化流水线。

## 长期目标：从问答助手到 A 股交易研究系统

`docs/A股架构未来设计.md` 的价值不在于照搬一套新框架，而在于把 A 股决策拆成了清晰的链路：先做数据与环境感知，再做结构化信号，再做质量校准和硬风控，最后才进入方案托管、模拟盘和自进化。这个顺序比“先保存方案再模拟执行”更适合当前项目，因为没有结构化信号和风控闸门时，保存下来的方案仍然只是文本报告，后续很难验证和进化。

后续建设应围绕一条可追溯闭环：

```text
数据采集与事件感知
  -> 市场 Regime / 情绪 / 黑天鹅识别
  -> 四大类信号分类与 L1/L2/L3 聚合
  -> 对抗式辩论与置信度门控
  -> A 股 T+1 / 涨跌停 / 仓位 L4 风控闸门
  -> planning_execute 生成可执行方案
  -> 方案落库、知识图谱记忆与模拟盘托管
  -> MFE/MAE 质量校准与 Trust Score
  -> 策略库沉淀与自进化提案
  -> 量化交易系统与真实交易接口
```

对 A 股新手来说，这条链路的价值是把“今天感觉可以买/不能买”拆成可验证步骤：为什么选这只、当前市场是否允许开仓、信号来自哪些维度、置信度是否足够、T+1 风险能否承受、买多少、错了怎么办、后来有没有按计划执行、这个策略历史上靠不靠谱。

### 从新设计吸收的原则

| 原设计优点 | 应用于本项目的方式 | 不直接照搬的部分 |
| --- | --- | --- |
| Stage 1-7 分层 | 用作本项目长期路线图的阶段顺序，避免过早进入模拟交易 | 不重建一套脱离当前 `agent-trace` 的新系统 |
| 脚本预执行 + LLM 纯分析 | 数据拉取、指标计算、风控闸门尽量确定性；LLM 做解释、辩论和结构化裁决 | 不要求引入独立 Hermes 运行时，先复用现有 API、scheduler、agent-trace 和工具体系 |
| A 股 T+1 全链路约束 | 把 T+1、涨跌停、隔夜跳空写成硬风控和方案约束 | 不做分钟级高频策略作为优先目标 |
| 四大类信号分类 | 基本面、技术面、情绪面、事件面成为前端展示、推送过滤和 L1/L2/L3 聚合基础 | 不把所有旧工具一次性重写为信号源 |
| 三层信号流水线 | L1 单源证据、L2 类别汇总、L3 交易决策，替代纯 Markdown 结论 | 不让 LLM 自由合成不可审计结论 |
| MFE/MAE 与 Trust Score | 作为模拟盘和回测后的信号质量校准指标 | 不在没有足够样本前用它自动改生产规则 |
| L4 风控闸门 | 单股仓位、总仓位、ST/退市、涨停禁追、跌停不可卖、T+1 锁定必须确定性执行 | 不允许模型绕过硬风控 |

### 调整后的优先级

| 优先级 | 阶段 | 目标 | 为什么排在这里 |
| --- | --- | --- | --- |
| P0 | A 股硬约束与结构化输出协议 | 定义 T+1、涨跌停、仓位、止损、信号分类和 L1/L2/L3 输出 schema | 后续方案保存、模拟盘、回测都依赖统一结构 |
| P1 | 工具缺口与市场环境感知 | 补市场情绪、地缘风险、跨资产、宏观、ETF/期权/期货和黑天鹅检测 | 先让 Agent 看见系统性风险，再谈主动开仓 |
| P2 | Regime 状态机与信号分类流水线 | 形成 `regime_detection`、四大类信号 Taxonomy、L1/L2/L3 聚合和置信度门控 | 解决“工具很多但结论不可控”的问题 |
| P3 | L4 风控与仓位算法 | 确定性阻断超仓、追涨停、无止损、T+1 当日卖出、数据缺失自动下单 | 任何托管和模拟下单前必须完成 |
| P4 | 方案保存与连续对话记忆 | 保存可执行 `TradePlan`，把 session、用户偏好、事实、历史观点分层记忆 | 这时保存的是可执行方案，不只是报告文本 |
| P5 | 模拟盘托管与 MFE/MAE 评估 | 按方案虚拟下单、跟踪、退出，并生成计划反馈和质量指标 | 为策略库和自进化提供真实反馈样本 |
| P6 | 回测系统与策略库 | 对信号、方案、策略和 Regime 做历史验证，沉淀版本化策略 | 避免策略只靠 prompt 命名和主观经验 |
| P7 | 自进化提案闭环 | 基于回测、模拟盘、Trace 和用户反馈生成候选改进，人审后上线 | 只有有样本和评估指标后，自进化才不失控 |
| P8 | 量化交易与真实下单 | 信号生成、风控网关、模拟撮合、审计日志成熟后再接真实交易 | 真实交易必须晚于模拟盘和风控闭环 |

## 未来阶段规划

### 阶段 A：A 股硬约束与结构化输出协议

这是新的第一优先级。当前项目已经能跑 `planning_execute` 和 `agent-trace`，但未来要把报告转为可执行方案，必须先定义结构化输出协议。协议应覆盖：

| 输出通道 | 作用 | 消费方 |
| --- | --- | --- |
| `l1_signal` | 单个工具/数据源给出的方向、置信度、证据和数据质量 | L2 聚合、Trace、Graphiti |
| `l2_summary` | 基本面、技术面、情绪面、事件面四大类汇总 | L3 决策、前端 Tab、推送过滤 |
| `l3_decision` | 跨类别融合后的交易动作和置信门控 | TradePlan、Debate Judge |
| `risk_gate` | L4 风控闸门结果：通过、阻断、降级、需人工确认 | 模拟盘、真实交易前置网关 |
| `trade_plan` | 可执行入场、加仓、减仓、止损、止盈和失效规则 | 方案保存、模拟盘托管 |
| `reasoning_trace` | Planner、工具调用、辩论、裁决和风控解释 | `/agent-trace`、审计、复盘 |

A 股硬约束必须作为确定性规则，不交给 LLM 自由判断：

- T+1：今日买入的持仓当日不可卖出，不能生成当天卖出指令。
- 涨停禁追：涨停股不生成追买方案，除非只是观察或次日条件单。
- 跌停不卖：跌停股不生成“立即卖出成交”的假设，只能生成次日或打开跌停后的处理计划。
- ST、退市整理、上市首日/科创板前 5 日：默认禁入或强制人工确认。
- 每笔交易必须有止损或失效条件。
- 单股仓位、总仓位、账户现金和融资约束必须先过风控。
- 数据缺失或关键工具失败时，交易动作必须降级为 `wait` / `monitor` / `manual_review`。

第一版已经按独立 `risk_gate` 落地，由后端确定性执行，LLM 只能解释不能覆盖。

当前阶段 A 已落地为工程底座：

- `src/schemas/agent_signal.py` 定义 `l1_signal`、`l2_summary`、`l3_decision`、`risk_gate`、`trade_plan` 和 `reasoning_trace` 的 Pydantic 协议。
- `src/agent/risk_gate.py` 提供独立 `RiskGateEvaluator`，先覆盖 T+1、涨停禁追、跌停不可假定立即卖出、ST/退市/上市特殊期人工确认、止损/失效条件、关键数据质量、置信门槛、单股/总仓位和现金约束。
- 协议层允许承载不完整或错误方案，风控层负责阻断、降级或转人工确认，保证后续 Trace、模拟盘和回测都能看到可审计原因。
- `agent-trace` 已接入最小风控闭环：运行结束后会从 Debate Judge 或选股裁决生成 `TradePlan`，执行 `risk_gate`，并落盘 `risk_gate.json`；`/trace/run` 与 `/trace/stream` 的完成载荷会返回同一份风控结果。
- Agent Trace 前端已新增 `Risk Gate` 面板，展示风控状态、允许动作、TradePlan、行情状态、规则检查、阻断原因和警告。
- 这一阶段仍不接真实/模拟下单路径，也不让 LLM 覆盖风控结论；下一步再把方案保存和模拟盘托管接到该结果。

### 阶段 B：工具缺口与市场环境感知

工具补全应优先服务三个问题：

1. 现在市场适不适合主动开仓？
2. 有没有战争、制裁、政策、汇率、能源、流动性等系统性冲击？
3. 这些事件会影响哪些板块、哪些持仓、哪些候选股？

建议按 A 股 Stage 1 数据采集思路补齐：

| 能力 | 建议工具 | 说明 |
| --- | --- | --- |
| 市场情绪 | `get_market_sentiment_snapshot` | 输出 `risk_on` / `neutral` / `risk_off` / `panic`、情绪分、恐慌分和动作约束 |
| 地缘风险 | `scan_geopolitical_risk_news` | 专门扫描战争、制裁、冲突、航运中断、能源供给冲击 |
| 跨资产风险 | `get_cross_asset_risk_signals` | 观察黄金、原油、美元、离岸人民币、美债、VIX、A50 等 |
| 资金四象限 | `get_market_capital_flow` / `get_northbound_capital_flow` / `get_margin_trading_summary` / ETF 份额工具 | 外资、杠杆资金、机构/游资、被动资金 |
| 事件检测 | `detect_market_events` | 政策、公告、财报、重大合同、并购重组、增减持、板块异动 |
| 黑天鹅检测 | `detect_black_swan_risk` | 个股闪崩、美股暴跌、人民币急跌、流动性紧张、国债逆回购异常 |
| 宏观指标 | `get_macro_china_snapshot` | M2、社融、PMI、CPI/PPI、LPR、汇率和利率环境 |
| 事件到板块映射 | `map_event_to_sectors` | 判断事件利好/利空哪些板块和产业链 |
| 组合压力测试 | `stress_test_portfolio_by_event` | 把外部事件映射到用户持仓风险 |

第一版可以复用现有搜索服务和规则打分，不必一开始追求完整数据供应商。重点是让工具输出结构化字段：事件类型、严重度、影响市场、影响板块、可信度、证据来源、动作约束。

### 阶段 C：Regime 状态机与信号分类流水线

`regime_detection` 应作为内部决策约束，不做花哨大盘报告。建议从波动率分档开始：计算近 N 日 ATR/价格比例，用历史分位数映射到 `low_volatility` / `normal` / `high_volatility` / `extreme_volatility`，并加入阻尼机制，避免一天异常波动导致档位跳变过大。

建议输出：

```json
{
  "market": "cn",
  "regime": "trending_up | range_bound | trending_down | high_volatility | risk_off | event_driven | panic | unknown",
  "volatility_bucket": "low | normal | high | extreme",
  "risk_level": "low | medium | high | critical",
  "position_bias": "increase_allowed | neutral | reduce_only | no_new_entry",
  "atr_pct": 0.0,
  "evidence": [],
  "constraints": []
}
```

同时引入四大类信号 Taxonomy：

| 大类 | 包含数据源 | 前端/推送用途 |
| --- | --- | --- |
| 基本面 | 财报、公告、北向资金、融资融券、龙虎榜、大宗交易、宏观指标 | 基本面 Tab，适合中期判断 |
| 技术面 | 价格结构、均线、量能、波动率、动量、缠论、SMC、Wyckoff | 技术面 Tab，适合入场和失效条件 |
| 情绪面 | 雪球/东财/微博情绪、百度指数、iVX、ETF 份额、资金拥挤 | 情绪面 Tab，判断过热、恐慌和拥挤交易 |
| 事件面 | 政策、黑天鹅、美股联动、汇率、流动性异常、公司重大事件 | 事件面 Tab，触发风险复查或紧急分析 |

三层信号流水线：

```text
L1 Detail   单源信号：方向 + 置信度 + 证据 + 数据质量
L2 Summary  按四大类聚合：类别方向 + 类别置信度 + 冲突点
L3 Decision 跨类别融合：交易动作 + 综合置信度 + 风控前置条件
```

确定性融合规则建议：

- 覆盖类别少于 3 类时，综合置信度封顶 0.50。
- 方向冲突明显时，综合置信度封顶 0.60，并触发 Debate。
- 四类一致看多时，综合置信度才允许高于 0.80。
- `trade_action` 只有在综合置信度 >= 0.75 且通过 `risk_gate` 时才允许输出。
- A 股专属：3 类及以上看空时输出 `wait` / `reduce` / `clear`，不要强行找多头机会。

### 阶段 D：结构分析与策略规则引擎

技术结构分析应分层落地，避免一开始实现过重：

| 层级 | 内容 | 优先级 |
| --- | --- | --- |
| D1 | 已有均线、趋势、量能、乖离率、形态分析 | 已有，继续收敛为 L1 技术信号 |
| D2 | Wyckoff/VSA：吸筹、拉升、派发、下跌相位 | 高，A 股散户占比高，量价博弈价值大 |
| D3 | SMC：BOS/CHoCH、Order Block、FVG/缺口 | 中，先实现缺口和结构突破，OB 需过滤一字板 |
| D4 | 缠论：K 线合并、分型、笔、线段、中枢、背驰 | 中后期，复杂度高但适配 A 股，可作为策略库增强 |

A 股策略库第一批只保留做多和风控策略：

- 趋势突破做多：突破关键阻力 + 放量确认 + Regime 支持。
- 趋势回调做多：上升趋势回踩支撑 + 企稳信号 + 不追高。
- 区间下沿低吸：震荡下沿 + 风险收益比合格。
- Wyckoff Spring 做多：吸筹末期假跌破后收回。
- 事件冲击避险：风险事件导致 `risk_off` / `panic` 时限制开仓或降低仓位。

删除或暂缓：

- 做空策略：A 股普通账户不支持，不能作为默认交易动作。
- Liquidation Hunt：A 股现货没有加密合约清算语义。
- 分钟级高频止损策略：T+1 下无法当日卖出，第一阶段操作意义有限。

### 阶段 E：对抗式辩论、共识评级与多模型校准

当前项目已经有 Debate Agent，应继续保留，但裁判输出要更结构化：

```json
{
  "confidence_bull": 0,
  "confidence_bear": 0,
  "conflict_dimensions": ["valuation", "capital_flow", "t_plus_1_risk"],
  "judge_action": "open | add | hold | reduce | sell | wait | manual_review",
  "confidence_after_debate": 0,
  "risk_gate_required": true
}
```

辩论角色在 A 股语境里应明确：

- 买入方：论证当前为什么值得买，必须引用技术、资金、事件或基本面证据。
- 质疑方：不是做空者，而是专门找“为什么不该在这个位置买”的理由，如估值过高、获利盘压力、T+1 锁定、涨停接盘、政策风险、数据缺口。
- 裁判：只在证据足够且风控通过时给交易动作；否则输出等待、观察或人工复核。

多模型投票可以后置，但共识评级可以先定义：

| 评级 | 含义 | 动作 |
| --- | --- | --- |
| `strong_buy` | 多维度高度一致看多且风控通过 | 按账户允许仓位执行 |
| `buy` | 多数维度看多但仍有约束 | 正常或降低仓位 |
| `watch` | 方向不明或证据不足 | 不操作，继续观察 |
| `reduce` | 持仓维度多数转弱 | 降低持仓比例 |
| `sell_or_stop` | 多维度看空或触发止损 | 卖出或止损，但需考虑跌停/T+1 可执行性 |

### 阶段 F：L4 风控闸门与仓位算法

风控和仓位必须在方案保存与模拟盘托管之前完成。建议把 L4 风控闸门作为后端确定性模块：

| 规则 | 初版建议 |
| --- | --- |
| 单股仓位上限 | 默认不超过总资产 20%，用户画像可下调 |
| 总仓位上限 | 默认不超过总资产 80%，保留 20% 现金 |
| 每笔风险预算 | 总资产的 1%-2% |
| 默认止损 | 每笔交易必须有止损或失效条件，默认约 5% |
| 隔夜跳空风险 | 每股风险取 `max(买入价-止损价, 买入价*隔夜跳空风险系数)` |
| ST/退市/异常品种 | 默认禁入或人工确认 |
| 涨停 | 禁止追买，改为次日观察条件 |
| 跌停 | 不假设可卖出成交，只输出风险处置计划 |
| T+1 | 买入当日持仓标记为不可卖，不生成卖出指令 |
| 数据缺口 | 关键行情/资金/情绪/风控工具失败时，禁止自动执行 |

仓位算法初版：

```text
每笔最大风险金额 = 总资产 * 单笔风险比例
每股风险 = max(abs(买入价 - 止损价), 买入价 * 隔夜跳空风险系数)
可买股数 = min(每笔最大风险金额 / 每股风险, 可用现金 / 买入价)
```

所有进入模拟盘或真实交易接口的计划都必须先经过 `risk_gate`。

### 阶段 G：方案保存、连续对话与模拟盘托管

在结构化信号、L4 风控和仓位算法具备后，再保存机器可读方案。建议新增 `AgentPlan` / `TradePlan`：

```json
{
  "plan_id": "plan_xxx",
  "source_trace_id": "trace_xxx",
  "symbol": "688469",
  "intent": "entry_analysis",
  "status": "draft | active | triggered | executed | monitoring | closed | reviewed | invalidated | expired",
  "action": "open | add | reduce | hold | sell | wait | manual_review",
  "entry_rules": [],
  "exit_rules": [],
  "risk_rules": [],
  "risk_gate": {},
  "position_sizing": {
    "first_position_pct": 10,
    "max_position_pct": 20,
    "shares": 0
  },
  "valid_until": "YYYY-MM-DD",
  "evidence_refs": [],
  "human_approval_required": true
}
```

连续对话和记忆分层：

| 记忆类型 | 保存内容 | 用途 | 过期策略 |
| --- | --- | --- | --- |
| Session 记忆 | 当前对话目标、已分析股票、用户补充条件、未完成问题 | 同一轮对话不重复问、不重复查 | 会话结束后归档 |
| 用户偏好 | 风险偏好、交易周期、禁买行业、最大仓位、默认止损 | 让后续报告更贴近用户 | 用户可编辑，长期有效 |
| 方案记忆 | 每次入场/持仓/选股方案、触发条件、失效条件 | 后续跟踪“上次计划是否仍成立” | 到期、失效或被新方案覆盖 |
| 事实记忆 | 股票、板块、事件、结论之间的关系 | Graphiti 检索历史分析与事件演化 | 随时间更新 summary |

模拟盘状态机：

```text
draft
  -> active
  -> triggered
  -> executed
  -> monitoring
  -> closed
  -> reviewed
```

每个模拟盘托管计划至少产出：

| 记录 | 内容 | 用途 |
| --- | --- | --- |
| `plan_snapshot` | Agent 原始方案、L1/L2/L3 信号、辩论裁决、风控结果和有效期 | 固定当时判断，防止事后篡改 |
| `paper_order_log` | 虚拟委托、成交、撤单、失败原因、滑点和手续费假设 | 验证方案是否能执行 |
| `plan_feedback` | 收益率、最大回撤、MFE/MAE、触发质量、止损纪律、提前失效原因 | 注入经验库和自进化系统 |

### 阶段 H：MFE/MAE、回测系统与 Trust Score

回测和质量校准应分两层：

1. 信号回测：某个 L1/L2/L3 信号出现后，未来 N 天收益、最大回撤、胜率如何。
2. 方案回测：按 Agent 给出的入场区间、止损、止盈、仓位规则模拟执行。

A 股评估窗口优先使用交易日：

| 窗口 | 用途 |
| --- | --- |
| 1 个交易日 | 短线信号验证 |
| 3 个交易日 | 中线触发质量验证 |
| 5 个交易日 | 波段初步验证 |
| 20 个交易日 | 策略和 Regime 适配验证 |

MFE/MAE：

```text
MFE = (窗口内最高价 - 信号发出价) / 信号发出价 * 100%
MAE = (信号发出价 - 窗口内最低价) / 信号发出价 * 100%
```

Trust Score 初版可采用：

```text
Trust = 0.35 * 命中率(1D)
      + 0.25 * min(平均MFE / 5%, 1)
      + 0.20 * max(0, 1 - 最大回撤 * 2)
      + 0.10 * min(盈亏比 / 3, 1)
      + 0.10 * 样本量因子
```

期望效用：

```text
EV = 命中率 * 平均MFE - (1 - 命中率) * 平均MAE
```

只有 EV > 0 且样本量足够的信号源，才允许提升策略权重。A 股回测还要处理前复权/后复权、涨跌停、停牌、除权除息、滑点、手续费、印花税和未来函数。

### 阶段 I：策略库与自进化提案

策略库不是 prompt 片段合集，而是可版本化、可回测、可复盘的交易方法集合。每个策略至少包含：

| 字段 | 含义 |
| --- | --- |
| `strategy_id` | 稳定 ID |
| `name` | 策略名称 |
| `style` | 趋势、回踩、低吸、事件驱动、价值、防守等 |
| `suitable_regimes` | 适用市场状态 |
| `entry_conditions` | 入场条件 |
| `exit_conditions` | 退出条件 |
| `risk_controls` | 止损、仓位、禁用条件 |
| `required_signals` | 需要哪些 L1/L2 类别信号 |
| `required_tools` | 必须使用的工具 |
| `backtest_metrics` | 回测指标 |
| `trust_score` | 历史信任分 |
| `version` | 策略版本 |
| `status` | experimental / active / deprecated |

自进化不是让模型自己随便改代码，而是建立“经验采集 -> 提案生成 -> 离线验证 -> 模拟盘回放 -> 人审 -> 上线 -> 监控 -> 回滚”的闭环。

经验样本建议结构：

```json
{
  "experience_id": "exp_xxx",
  "plan_id": "plan_xxx",
  "source_trace_id": "trace_xxx",
  "strategy_id": "strategy_xxx",
  "market_regime": "risk_off",
  "sentiment_state": "panic",
  "decision": "wait | open | add | reduce | sell",
  "mfe_pct": 2.8,
  "mae_pct": 5.1,
  "paper_trade_result": {
    "triggered": true,
    "return_pct": -3.2,
    "max_drawdown_pct": -5.1,
    "exit_reason": "stop_loss"
  },
  "diagnosis": {
    "what_worked": [],
    "what_failed": [],
    "missed_evidence": [],
    "tool_failures": []
  },
  "candidate_improvements": []
}
```

自进化输出只能是候选变更：

| 候选变更 | 示例 | 验证要求 |
| --- | --- | --- |
| Prompt 调整 | risk_off 下禁止把“趋势好”直接转成买入 | 用历史 Trace 回放检查输出是否更稳 |
| Planner 调整 | 入场和选股默认加入情绪工具 | 对比工具成本、延迟和决策质量 |
| 策略参数调整 | 首仓从 20% 降到 10%，止损从 8% 收紧到 5% | 回测和模拟盘分 Regime 验证 |
| Judge 权重调整 | 高波动环境提高反方风险权重 | 检查是否减少追高和过度交易 |
| 工具优先级调整 | 资金流失败时强制标记数据缺口，不允许模型编造 | Trace 回放验证失败展示是否准确 |

### 阶段 J：事件驱动调度、推送过滤与量化交易

当前项目不需要引入独立 Hermes 框架，但可以吸收它的运行时思想：

- 数据拉取和指标计算用确定性脚本/服务，LLM 只做分析和结构化输出。
- 定时任务和事件触发共用同一任务队列或调度入口。
- 慢任务并行执行，避免技术分析阻塞事件风控。
- 外部事件可注入一次性分析任务，不必等下一次定时任务。
- 所有最终信号都能追溯到触发事件、工具证据、L1/L2/L3 信号和风控结果。

建议任务分层：

| 层 | 代号 | 职责 | 频率 |
| --- | --- | --- | --- |
| 数据层 | B 类 job | 拉数据、清洗、语义过滤、入库 | 分钟到小时级 |
| 决策层 | C 类 job | 多维分析、辩论、共识、信号输出 | 小时级或事件触发 |
| 进化层 | X 类 job | 复盘、Trust Score、策略提案 | 日级或周级 |

推送和前端展示应按四大类信号过滤：用户可以订阅基本面、技术面、情绪面、事件面或具体数据源，后端在推送前过滤，而不是把所有信号推给前端再筛。

真实交易接口必须晚于以下条件：

- 模拟盘稳定运行一段时间。
- 回测覆盖主要策略和典型市场状态。
- 每个策略有明确禁用条件。
- 风控网关可阻止超仓、追高、数据缺失、恐慌状态下开仓。
- 每笔真实交易都支持人工确认或至少支持一键熔断。

对当前项目来说，新的合理顺序是：

1. 先定义结构化信号协议和 A 股硬风控。
2. 再补情绪、事件、跨资产和黑天鹅工具。
3. 再做 Regime 状态机、四大类信号分类和 L1/L2/L3 聚合。
4. 再做可执行 TradePlan 保存。
5. 再做模拟盘托管和 MFE/MAE 质量评估。
6. 再做回测、策略库和 Trust Score。
7. 再做自进化提案闭环。
8. 最后才考虑量化交易和真实下单。

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

当前项目已在 `src/agent/planner.py` 落地 `CAPABILITY_TOOL_MAP` 和 `get_tools_for_capability()`。Planner 先选择能力域，再按当前 `ToolRegistry` 展开为实际可调用工具；缺失工具会进入 `missing_tools`，不会被假定为可用。

当前映射：

| Capability | Tools |
| --- | --- |
| `technical_analysis` | `analyze_trend`, `calculate_ma`, `get_volume_analysis`, `analyze_pattern` |
| `realtime_quote` | `get_realtime_quote` |
| `portfolio_context` | `get_portfolio_snapshot` |
| `news_event` | `search_comprehensive_intel`, `search_stock_news` |
| `capital_flow` | `get_capital_flow`, `get_market_capital_flow`, `get_northbound_capital_flow`, `get_margin_trading_summary` |
| `fundamental_analysis` | `get_stock_info` |
| `chip_distribution` | `get_chip_distribution` |
| `regime_detection` | `get_market_indices`, `get_sector_rankings`, `get_volume_analysis` |
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

## 已落地阶段记录

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
| `RiskGateEvaluator` | 由后端确定性执行 A 股硬风控，消费 Debate Judge 或选股裁决生成的 `TradePlan`，输出通过、阻断、降级或人工复核结果 |

当前流程：

```text
AgentUserContext
  -> Planner
  -> Shared Evidence Bundle
  -> PrimaryThesisAgent
  -> AdversarialThesisAgent
  -> Debate rounds
  -> DebateJudgeAgent
  -> TradePlan draft
  -> RiskGateEvaluator
  -> Final account-aware action plan
```

当前实现：

- `src/agent/debate.py` 提供 `PrimaryThesisAgent`、`AdversarialThesisAgent` 和 `DebateJudgeAgent` 的轻量运行时编排。
- `src/agent/executor.py` 只在 `planning_execute`、存在 `AgentUserContext` 且已有成功工具证据时触发 Debate；`normal` 模式不受影响。
- `src/agent/tools/market_tools.py` 提供 `discover_watchlist_candidates`，用于 watchlist_scan 在无用户候选股票代码时先生成候选池；`src/agent/stock_selection.py` 在 planning_execute 的 watchlist_scan 下优先执行阶段化选股流水线，并通过 `SelectionRunContext` 管理候选发现、初筛、深度分析、组合配置、反方审查和 Judge 裁决；`src/agent/runner.py` 仍保留审计兜底，阻止空候选 watchlist_scan 直接输出最终选股结论。
- Debate 三方共用同一份 `Shared Evidence Bundle`，包含用户问题、主报告、`AgentUserContext`、Planner 和工具 Evidence Ledger。
- `api/v1/endpoints/agent.py` 的 Trace 响应和 SSE `done` 事件会返回 `debate` 字段，并在调试目录写入 `debate.json`。
- Trace 结束时会构造 `TradePlan` 并调用 `RiskGateEvaluator`，将结果写入 `risk_gate.json`，同时在 `/trace/run` 响应和 `/trace/stream` 完成事件中返回。
- `apps/dsa-web/src/pages/AgentTracePage.tsx` 新增 `Debate Judge` 模块，展示主观点、反方观点、Judge 裁决、分维度证据、采纳/驳回论点和风控条件；Judge 输出会显式区分账户风险、技术面、资金面、消息面、基本面和数据质量。
- Agent Trace 页面新增 `Risk Gate` 面板和分层 Trace 视图，可查看候选池来源、工具证据、辩论裁决、风控规则检查和复盘入口。
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
- [x] 阶段化选股链路能落盘 `stock_selection.json`、`selection_context.json`、`final_report.json` 和各阶段 JSON 产物。
- [x] Trace 运行能从 Debate Judge 或选股裁决生成 `TradePlan`，执行确定性 `risk_gate`，并落盘 `risk_gate.json`。
- [x] README 未继续膨胀，细节保留在专题文档中。
- [x] `entry_analysis` 专项输出规范已补齐到与 `position_review` 同等级的可执行程度，并使用可见 Planning -> Execute 格式。
- [x] 第五阶段按开发调试模式收口；正式用户画像配置入口暂缓产品化，不作为当前阶段阻塞项。
- [x] Debate Agent 已按“强制反向立场辩论 + Judge 最终裁决”接入 planning_execute，覆盖持仓模式和选股/入场模式。
- [x] `discover_watchlist_candidates` 已支持 Sequoia 量化候选、强势板块、用户种子和 fallback 多路召回，并通过统一评分合并候选。
- [x] Graphiti/Neo4j 已具备可选最小集成路径，可用于分析结果入图和 Agent 检索，但仍不是主链路硬依赖。
