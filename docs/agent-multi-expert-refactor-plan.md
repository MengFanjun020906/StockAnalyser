# Agent 多专家重构方案

> 目标：在不推翻现有 `planning_execute / watchlist_scan / Debate / risk_gate` 链路的前提下，把选股和分析重构成“共享状态 + 多专家 + Judge 收口”的显式编排框架。

## 1. 设计目标

当前系统已经有分阶段能力，但它们仍然主要表现为“一个 Agent 顺着工具链跑到底”。这会带来两个问题：

1. 角色边界不清，技术面、资金面、消息面、情绪面经常混在同一个 LLM 调用里。
2. 候选池、初筛、深挖、组合配置、反方审查之间的证据流不够显式，Trace 能看见结果，但看不见每个专家到底负责什么。

本次重构只做第一阶段：

- 只重构 `watchlist_scan`
- 只动内部编排层，不切换外部 graph 框架
- 所有专家统一使用当前主模型
- 保留现有工具注册、现有 Trace、现有 `risk_gate`

## 2. 参考原则

借鉴 TradingAgents-CN 的核心思想，但不直接绑定其代码结构：

- 共享状态，而不是各说各话
- 每个专家只负责一个维度
- 专家之间只传结构化结果，不传自然语言长篇散文
- Judge 负责冲突裁决，不负责补证据
- 风控仍然由确定性 `risk_gate` 最终把关

## 3. 目标架构

```text
Orchestrator
  -> Evidence Collector
  -> Market Regime Expert
  -> Candidate Discovery Expert
  -> Technical Expert
  -> Capital / Chip Expert
  -> News / Sentiment Expert
  -> Fundamental Expert
  -> Portfolio / Risk Expert
  -> Bull / Bear Debate
  -> Judge / Allocator
  -> Risk Gate
```

## 4. 多专家职责

### 4.1 Market Regime Expert

输入：
- `detect_market_regime`
- `get_market_indices`
- `get_sector_rankings`

输出：
- 市场状态
- 波动率档位
- 情绪状态
- 策略约束
- 不适合追高或开仓的提示

### 4.2 Candidate Discovery Expert

输入：
- `discover_watchlist_candidates`
- 候选池的 `reason_dimensions`

输出：
- 候选池排序建议
- 候选来源审计
- 哪些候选主要来自技术面、资金面、消息面、情绪面

### 4.3 Technical Expert

输入：
- `get_realtime_quote`
- `analyze_trend`
- `calculate_ma`
- `get_volume_analysis`
- `analyze_pattern`
- `analyze_price_structure`

输出：
- 趋势判断
- 关键均线结构
- 追高风险
- 入场区间
- 止损条件

### 4.4 Capital / Chip Expert

输入：
- `get_capital_flow`
- `get_market_capital_flow`
- `get_northbound_capital_flow`
- `get_margin_trading_summary`
- `get_chip_distribution`

输出：
- 资金是否承接
- 筹码是否集中
- 主力是否持续流入
- 是否适合追涨

### 4.5 News / Sentiment Expert

输入：
- `search_stock_news`
- `search_comprehensive_intel`
- 后续新增的情绪面工具

输出：
- 消息面利好/利空
- 情绪热度
- 板块催化
- 风险事件

### 4.6 Fundamental Expert

输入：
- `get_stock_info`

输出：
- 估值、盈利、成长、行业质量
- 是否存在基本面与价格脱节

### 4.7 Portfolio / Risk Expert

输入：
- `get_portfolio_snapshot`
- 投资者偏好
- 持仓成本

输出：
- 仓位是否允许
- 单票仓位上限
- 是否要减仓/等待

### 4.8 Bull / Bear Debate

输入：
- 上述所有专家观点

输出：
- 正方方案
- 反方方案
- 冲突点
- 需要补证据的地方

### 4.9 Judge / Allocator

输入：
- 正反方方案
- 共享证据
- 市场状态
- 账户约束

输出：
- accept / accept_with_changes / reject / wait_for_more_data
- 最终动作
- 风控条件

## 5. 统一状态协议

建议新增：

- `src/agent/multi_expert/state.py`
- `src/agent/multi_expert/orchestrator.py`
- `src/agent/multi_expert/experts/*.py`

核心结构：

```python
class AgentState(BaseModel):
    task: str
    intent: str
    market: str
    account_summary: dict
    investor_profile: dict
    market_regime: dict
    candidate_pool: list
    evidence_bundle: dict
    expert_opinions: dict
    debate: dict
    judge: dict
    risk_gate: dict
```

```python
class ExpertOpinion(BaseModel):
    expert_name: str
    verdict: str
    confidence: float
    summary: str
    supporting_evidence: list[str]
    opposing_evidence: list[str]
    missing_evidence: list[str]
    risk_flags: list[str]
    recommended_action: str | None
```

原则：
- 专家只读共享状态
- 专家输出必须结构化
- 不允许专家自己决定最终仓位

## 6. watchlist_scan 的阶段拆分

现有流程保留，但内部拆成专家节点：

1. 候选发现专家
2. 市场环境专家
3. 技术专家
4. 资金/筹码专家
5. 消息/情绪专家
6. 基本面专家
7. 组合/风险专家
8. 正反方辩论
9. Judge 裁决
10. risk_gate 确认

## 7. 编排策略

### 第一阶段：兼容式编排

- 不改普通单股分析。
- `watchlist_scan` 先走专家编排。
- 专家底层还是调用现有工具。
- 统一主模型，不做模型分层。

### 第二阶段：选股策略优化

候选池不再只看技术面：

- 技术面：突破、趋势、结构
- 资金面：成交额、量比、主力流入、筹码
- 消息面：公告、事件、新闻催化
- 情绪面：热度、概念扩散、市场关注度

候选卡片必须至少给出一个主维度和一个次维度，不允许只剩“技术面不错”这种空话。

## 8. 兼容策略

- `AGENT_ORCHESTRATION_MODE=legacy|expert_graph`
- 默认先保留 `legacy`
- `expert_graph` 只影响 `watchlist_scan`
- `risk_gate` 保持最后硬闸门
- Trace 继续落盘 `stock_selection.json`、`debate.json`、`risk_gate.json`

## 8.1 第一阶段落地状态

第一阶段采用兼容式落地，不替换现有五阶段选股流水线，也不额外增加工具调用和模型调用。`watchlist_scan` 仍按候选发现、市场 Regime、初筛、单股深挖、组合配置、反方审查和 Judge 裁决运行；当 `AGENT_ORCHESTRATION_MODE=expert_graph` 时，系统会把这些阶段已经收集到的证据组织成共享 `AgentState`，并生成结构化专家意见。

当前专家输出写入：

- `selection_context.expert_state`
- `final_report_json.expert_state`
- Trace 本地落盘的 `stock_selection.json`、`selection_context.json`、`final_report.json`

第一版专家包括：

| 专家 | 输入证据 | 输出重点 |
| --- | --- | --- |
| `market_regime_expert` | `detect_market_regime` | 市场状态、波动档位、风险约束、是否应降档 |
| `candidate_discovery_expert` | `discover_watchlist_candidates` 的 `reason_dimensions` | 候选来源、策略/技术/资金/情绪/输入维度、候选池是否为空 |
| `technical_expert` | 深度分析里的趋势和价格结构摘要 | 趋势/结构是否支持、技术证据缺口 |
| `capital_chip_expert` | 资金流和筹码工具结果 | 资金承接、筹码证据、资金工具失败风险 |
| `news_sentiment_expert` | 综合情报和候选热点/消息来源 | 消息、热点、情绪证据是否足够 |
| `fundamental_expert` | `get_stock_info` / 深度分析基本面摘要 | 估值、公司信息和基本面证据缺口 |
| `portfolio_risk_expert` | 组合配置和 Judge 摘要 | 仓位动作、组合约束和风险控制 |

设计上这些专家只做“结构化归因和冲突暴露”，不决定最终仓位；最终动作仍由现有 Judge 和确定性 `risk_gate` 收口。默认 `legacy` 模式完全保持原行为。

## 9. 验收标准

第一阶段完成的标准是：

1. `watchlist_scan` 里每个候选都有明确的专家来源。
2. 候选池不再只剩技术面，至少能显式显示资金面、消息面、情绪面中的一部分。
3. Trace 能看到每个专家的输出。
4. Judge 仍能合并冲突并给出最终动作。
5. `risk_gate` 仍是最终硬闸门。
6. 现有单股分析不被破坏。

## 10. 实施顺序

1. 先加状态协议和编排骨架。
2. 再把 `watchlist_scan` 接进去。
3. 再补 Trace 展示。
4. 再逐步把普通单股分析迁移过去。
