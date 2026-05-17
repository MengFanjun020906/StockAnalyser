# Agent 候选池多专家架构方案

> 目标：不推翻已有 AlphaSift、Sequoia、板块、消息和资金工具，而是把它们从“串行候选源”升级为“专家内部召回器”，让选股候选阶段真正具备技术面、资金面、消息面、情绪面、基本面等多维度来源。

## 1. 当前状态

当前 `watchlist_scan` 已经具备阶段化选股和 `expert_graph` 展示能力，但候选发现阶段仍然主要是一个串行工具链：

```text
AlphaSift
-> Sequoia
-> event_impact
-> news_momentum
-> sector
-> fallback
```

这意味着：

- `candidate_source=auto` 是多来源串行召回，不是多专家并行选股。
- `sector` 只是“板块成分股候选来源”，不是多专家模式。
- `expert_graph` 当前更像是“基于已有证据的专家化解释/裁决图谱”，不是每个专家独立召回、独立取证、独立输出候选池。
- `fallback_seed_pool` 只是兜底观察池，不应被解释为策略筛选结果。

因此，当前系统可以描述为：

```text
单 Agent / 阶段化选股主链路
+ 多来源串行候选召回
+ expert_graph 专家解释层
```

目标状态应升级为：

```text
多专家候选召回
-> 统一候选合并
-> 分维度取证
-> 反方审查
-> Judge 裁决
-> 风控闸门
```

## 2. 不推倒重做的原则

已有实现仍然有价值，不应重新写一套硬编码选股系统。

核心原则：

- 保留 AlphaSift 的 YAML 策略和多因子筛选能力。
- 保留 Sequoia 的技术形态、突破、RPS、动量策略。
- 保留 `sector` 的板块/概念扩散能力，但必须补齐诊断和稳定数据源。
- 保留 `event_impact` 的“事件 -> 主题 -> 后续验证 -> 个股”链路。
- 保留 `news_momentum` 的公司级新闻/公告硬事件识别。
- 保留资金、筹码、龙虎榜、融资融券、北向、板块资金等工具作为资金专家的数据源。
- 不让固定种子池进入策略候选，只能作为“兜底观察池”。

已有能力应该从：

```text
discover_watchlist_candidates 里的串行分支
```

迁移为：

```text
各专家内部可复用的 CandidateProvider / ToolAdapter
```

## 3. 目标专家划分

### 3.1 策略多因子专家

职责：用可配置策略和因子分数发现基础候选。

复用资产：

- `AlphaSiftCandidateProvider`
- AlphaSift YAML 策略目录
- 本地 OHLCV SQLite 缓存

输出重点：

- 命中的 YAML 策略。
- 因子得分。
- 硬筛通过原因。
- 策略适用场景。
- 数据质量和有效期。

### 3.2 技术形态专家

职责：发现形态、趋势、突破、RPS、放量等技术候选。

复用资产：

- `SequoiaCandidateProvider`
- `analyze_trend`
- `calculate_ma`
- `get_volume_analysis`
- `analyze_pattern`
- 后续接入的 Chan / SMC / Regime 技术结构工具

输出重点：

- 趋势强度。
- 突破或回踩结构。
- 是否追高。
- 技术失效条件。
- 与市场环境是否匹配。

### 3.3 板块主题专家

职责：从强势板块、热点概念和板块资金扩散到个股。

复用资产：

- `get_sector_rankings`
- `sector` 板块成分召回
- StockAPI 热点板块、板块成分、板块资金历史
- Tushare 行业/概念相关数据

输出重点：

- 热点板块名称。
- 板块强度和资金方向。
- 成分股来源。
- 板块接口诊断。
- 是否只是主题观察，还是已经形成个股候选。

### 3.4 资金面专家

职责：用资金流、筹码、龙虎榜、融资融券、北向等数据判断承接力度。

复用资产：

- `get_capital_flow`
- `get_chip_distribution`
- StockAPI 资金流向、龙虎游资、人气、涨停池
- Tushare 融资融券、龙虎榜、增减持、回购、解禁

输出重点：

- 主力资金是否持续流入。
- 资金和价格是否同向。
- 筹码是否集中。
- 龙虎榜/游资是否支持短线情绪。
- 是否出现资金反证。

### 3.5 消息事件专家

职责：从公司级新闻、公告、订单、业绩、减持、监管等硬事件生成或否决候选。

复用资产：

- `news_momentum`
- `score_stock_news_sentiment`
- `search_stock_news`
- `search_comprehensive_intel`
- 后续公告、监管、减持、订单专用接口

输出重点：

- 公司级事件类型。
- 利好/利空/中性方向。
- 消息发布时间和有效窗口。
- 是否可直接影响个股。
- 是否只作为风险反证。

### 3.6 情绪/宏观事件专家

职责：处理不能直接推导个股的宏观、地缘、政策、产业事件。

复用资产：

- `event_impact`
- Tavily / Bocha / SearXNG 等新闻搜索
- 事件验证窗口
- 后续 Graphiti 知识图谱

输出重点：

- 事件本身。
- 可能影响的变量。
- 相关主题。
- 后续真实验证事实。
- 未验证时只能进入观察，不得直接推出个股。

### 3.7 基本面专家

职责：用质量、成长、估值、财务安全边际生成或过滤候选。

复用资产：

- Tushare 股票基础数据
- 三大财报
- 估值指标
- ST、解禁、质押、回购、增减持等参考数据

输出重点：

- 盈利质量。
- 成长稳定性。
- 估值位置。
- 财务风险。
- 是否适合作为中线候选。

## 4. 两阶段专家职责

同一个专家应支持两种模式：`discover` 和 `evaluate`。

```text
discover：全市场或大样本扫描，负责发现候选
evaluate：针对已入池股票做深度验证，负责输出证据和反证
```

二者不能混为一个职责：

| 阶段 | 输入 | 输出 | 目标 |
| --- | --- | --- | --- |
| `discover` | 全市场数据、策略库、板块、新闻、资金异动 | `ExpertCandidatePacket` | 找到“值得继续验证”的股票或主题 |
| `evaluate` | 候选股票 + 对应维度完整证据 | `EvidenceCard` / `ExpertEvidencePacket` | 判断该候选在本维度是否支持入场 |

角色转换示例：

- 技术专家：
  - `discover`：扫描突破、RPS、放量、形态候选。
  - `evaluate`：验证某只候选是否仍处于可入场结构，是否追高，止损位是否明确。
- 资金专家：
  - `discover`：扫描资金异动、龙虎榜、人气、板块资金流入。
  - `evaluate`：验证某只候选资金是否持续承接，是否存在资金背离。
- 消息专家：
  - `discover`：扫描公司级订单、业绩、公告、监管、减持事件。
  - `evaluate`：判断该股票消息是否真实、是否新鲜、是否构成支持或反证。
- 情绪/宏观专家：
  - `discover`：发现宏观、地缘、政策、产业事件和主题观察。
  - `evaluate`：只有出现后续事实验证时，才对相关候选输出支持；未验证时只输出观察或风险提示。

第一阶段专家之间默认信息隔离：各专家独立召回，不读取其他专家的候选或结论。交叉验证统一放到第二阶段和合并/Judge 层处理。这样可以避免“技术专家因为提前知道资金流出而不报候选”，导致候选召回阶段丢失可审计证据。

## 5. 候选协议

每个专家不直接返回自然语言大段解释，而是返回统一的 `ExpertCandidatePacket`。

建议结构：

```json
{
  "expert": "technical",
  "status": "ok",
  "data_quality": {
    "freshness": "eod",
    "as_of": "2026-05-15",
    "source_chain": ["sequoia", "local_sqlite"],
    "warnings": []
  },
  "themes": [],
  "candidates": [
    {
      "code": "301183",
      "name": "东田微",
      "market": "cn",
      "score": 82,
      "confidence": 0.76,
      "stance": "support",
      "reason": "RPS 强势突破 + 放量突破",
      "evidence_refs": ["sequoia:rps_breakout", "trend:breakout"],
      "reason_dimensions": [
        {
          "dimension": "technical",
          "label": "技术面",
          "detail": "RPS 强势突破，成交量同步放大"
        }
      ],
      "counter_evidence": [],
      "valid_until": "2026-05-18",
      "refresh_policy": "next_trading_day"
    }
  ]
}
```

字段语义：

- `score`：专家内部 0-100 分，用于同专家内排序。
- `confidence`：0-1 置信度，只表示该专家对自己证据质量和方向的把握。
- `stance`：`support` / `watch` / `oppose` / `invalid`。
- `themes`：宏观、情绪、政策、产业事件的主题观察列表；当事件尚不能直接推出个股时，允许 `candidates=[]` 且 `themes` 非空。
- `reason_dimensions`：前端展示用，不允许塞原始长数据。
- `counter_evidence`：显式反证。
- `valid_until`：候选有效期，不同专家可以不同。
- `refresh_policy`：刷新策略，例如盘中、收盘后、下个交易日、周度。

候选进入深度分析后，各专家输出从 `ExpertCandidatePacket` 切换为 `EvidenceCard` 和 `ExpertEvidencePacket`。字段语义应与 [agent-evidence-card-protocol.md](agent-evidence-card-protocol.md) 保持一致，尤其是 `confidence`、`stance`、`data_quality`、`counter_evidence` 和有效期语义。

## 6. 合并与裁决规则

候选合并层负责把多个专家输出统一成候选池。

### 6.1 去重

同一股票按代码合并，名称以股票基础信息索引为准，禁止专家间传递代码/名称不一致的候选。

### 6.2 共振加权

同一股票被多个专家命中，应提高优先级，但不能简单相加。

建议规则：

```text
base_score = max(expert_scores)
consensus_bonus = min(15, sum(expert.confidence * 5 for expert in supporting_experts))
counter_penalty = 反证扣分
quality_penalty = 数据质量扣分
final_score = clamp(base_score + consensus_bonus - counter_penalty - quality_penalty, 0, 100)
```

### 6.3 容量控制

合并后的候选池必须有明确上限，否则 7 个专家各出 8 只会膨胀成 30 只以上，拖慢深度取证并稀释上下文。

建议默认：

```text
max_candidates_to_deep_dive = 8
min_per_expert = 1
max_per_expert = 4
max_theme_watch_items = 5
```

截断规则：

1. 先按身份校验后的股票代码去重。
2. 每个成功专家至少保留 1 只最高分候选，前提是 `stance != invalid`。
3. 单一专家最多贡献 4 只，避免某一类策略垄断候选池。
4. 剩余名额按 `final_score`、`confidence`、数据新鲜度排序补齐。
5. `themes` 不占用股票候选名额，单独进入主题观察区。
6. `fallback_seed_pool` 只在所有专家无候选时启用，不参与容量保底。

### 6.4 专家权重

专家权重应支持配置，并允许按市场环境动态调整。

默认权重示例：

```text
technical = 0.25
strategy_factor = 0.20
capital_flow = 0.20
sector_theme = 0.15
news_event = 0.10
fundamental = 0.10
```

Regime 调整示例：

- 强趋势/高风险偏好：提高技术、策略、板块主题权重。
- 震荡/低波动：提高基本面、资金面权重。
- 极端波动：降低趋势追踪和追涨类候选权重，提高风控和反证权重。
- 消息驱动行情：提高消息事件和情绪/宏观专家权重，但未验证事件只能进入观察。

### 6.5 反证优先

如果出现以下情况，应进入观察或降权：

- 技术面强，但资金连续流出。
- 板块热，但个股没有成交确认。
- 消息利好，但监管、减持、业绩风险明显。
- 情绪事件尚未被后续事实验证。
- 数据源过期或失败。

交叉验证原则：

- 第一阶段不做跨专家否决，只保留各自候选和诊断。
- 第二阶段对入围候选并行取证，各专家输出支持、观察、反对或无效。
- 合并/Judge 层负责处理技术支持但资金反对、消息支持但监管反对等冲突。

### 6.6 fallback 隔离

`fallback_seed_pool` 永远不能与策略候选混合展示。

规则：

- 只在所有专家候选为空时启用。
- 前端展示为“兜底观察池”。
- 不参与共振加分。
- 不得作为“推荐”或“策略命中”。

## 7. 并行编排目标

当前 `auto` 是串行多路召回。目标是改成并行多专家召回：

```text
parallel:
  StrategyFactorExpert
  TechnicalCandidateExpert
  SectorThemeExpert
  CapitalFlowExpert
  NewsEventExpert
  SentimentThemeExpert
  FundamentalExpert

join with timeout
-> normalize packets
-> merge candidates
-> identity audit
-> evidence cards
-> judge input
```

每个专家必须有独立超时和降级策略：

- 单专家失败不拖垮整个候选池。
- 失败专家输出 `status=failed` 或 `status=timeout`。
- 合并层保留失败诊断。
- Judge 必须知道哪些维度缺失。

## 8. 深度分析阶段

候选池进入深度分析后，应继续使用同一批专家，但职责从“发现候选”切换为“验证候选”。

### 8.1 输入

深度分析阶段的输入是经过容量控制后的候选池：

```text
selected_candidates
+ candidate_origin_packets
+ market_regime
+ account_context
+ user_constraints
```

每个专家只接收与自己维度相关的候选和工具结果摘要，但可以看到候选的基础身份信息、候选来源和市场环境。

### 8.2 并行验证

深度分析阶段也应并行：

```text
parallel for each candidate:
  TechnicalExpert.evaluate
  CapitalFlowExpert.evaluate
  NewsEventExpert.evaluate
  SectorThemeExpert.evaluate
  FundamentalExpert.evaluate
  PortfolioRiskExpert.evaluate

join
-> ExpertEvidencePacket
-> JudgeInputPacket
```

每个专家必须输出结构化证据，而不是自然语言长文。

### 8.3 输出协议

深度分析阶段输出对齐 `agent-evidence-card-protocol.md`：

- 单工具/单信号输出 `EvidenceCard`。
- 单专家聚合输出 `ExpertEvidencePacket`。
- Judge 收到的是 `JudgeInputPacket`。

示例：

```text
TechnicalExpert.evaluate(301183)
-> EvidenceCard: trend_breakout
-> EvidenceCard: ma_structure
-> EvidenceCard: overbought_risk
-> ExpertEvidencePacket: technical stance=support confidence=0.72
```

### 8.4 失败和缺失

如果某个维度工具全部失败，专家仍应输出一个 `ExpertEvidencePacket`：

```text
stance = invalid
confidence = 0
data_quality.status = failed
top_risks = ["资金面工具全部超时，不能确认承接"]
```

Judge 不应把缺失维度当成中性，而应降低总体置信度。

## 9. 前端可信展示

前端不能只显示 `expert_graph` 标签。应该同时展示：

- `AGENT_ARCH`
- `AGENT_ORCHESTRATION_MODE`
- 是否收到 `selection_expert_graph_done`
- `expert_count`
- 各专家 `status`
- 各专家候选数
- 是否使用了 `fallback_seed_pool`
- 哪些专家失败或超时

推荐展示方式：

```text
多专家候选状态
模式：AGENT_ARCH=multi / AGENT_ORCHESTRATION_MODE=expert_graph
专家图谱：已生成 selection_expert_graph_done
候选来源：
  技术专家 8 只
  策略专家 5 只
  资金专家 3 只
  消息专家 2 只
  情绪专家 观察 4 个主题
  基本面专家 4 只
降级：
  sector 成分接口 timeout
  已启用本地策略兜底
```

这样用户能判断：

- 是否真的启动了多专家。
- 哪些专家真实产出了候选。
- 哪些只是观察或降级。
- 哪些候选来自 fallback。

## 10. 迁移步骤

### P0：澄清状态

- 前端显示 `AGENT_ARCH` 和 `AGENT_ORCHESTRATION_MODE`。
- Trace 显示是否收到 `selection_expert_graph_done`。
- `fallback_seed_pool` 继续独立展示为兜底观察池。
- `sector` 失败诊断进入 Trace。

### P1：抽象候选协议

- 新增 `ExpertCandidatePacket` 和 `ExpertCandidate` schema。
- 为 AlphaSift / Sequoia / sector / news_momentum / event_impact 写 adapter。
- 保留现有 `discover_watchlist_candidates`，但内部改为调用 adapter。

### P2：多专家候选编排器

- 新增 `CandidateExpertOrchestrator`。
- 并行运行各专家。
- 每个专家独立 timeout。
- 合并层输出统一候选池。
- 所有专家诊断落盘到 Trace。

### P3：深度取证专家化

- 同一批专家支持 `evaluate` 模式。
- 候选池进入并行深度分析。
- EvidenceCard 继续负责压缩工具结果。
- Judge 输入明确包含专家来源、共振、反证和缺失维度。

### P4：回测和效果评估

- 保存每次专家候选输出。
- 做 T+1 / T+3 / T+5 回评。
- 统计每个专家的命中率、误报率、收益风险比。
- 根据评估结果动态调整专家权重。

## 11. 结论

AlphaSift、Sequoia、sector、event_impact、news_momentum 和资金工具都有保留价值。

问题不是这些项目没用，而是当前层级不对：

```text
现在：一个大工具里串行跑多个候选源
目标：多个专家各自用已有候选源独立产出候选
```

因此最优路线不是重写，而是把已有能力包进专家架构：

```text
已有 Provider / Tool
-> Expert Adapter
-> ExpertCandidatePacket
-> CandidateExpertOrchestrator
-> Merge / Judge / Risk Gate
```

这样既能复用过去做的创新特性，也能逐步走向真正的多专家选股系统。
