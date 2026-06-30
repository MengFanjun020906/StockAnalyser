# Agent Evidence Card Protocol

本文定义多专家选股链路中的统一证据传递协议。目标是解决“工具越多，上下文越稀释”的问题：工具可以返回完整数据，但专家之间只能传递高密度、可审计、带方向性的证据卡。

## 1. 核心原则

### 1.1 不传工具结果，传证据卡

工具原始返回通常包含大量低价值字段、空值、历史明细和调试信息。多专家链路中，工具结果必须先被压缩成 `EvidenceCard`，再进入专家判断。

原始结果的用途：

- 落盘到 trace artifact，用于复盘、前端展开、调试和回测。
- 保留 `full_ref`，让前端和开发者能追到完整证据。
- 不直接进入专家 prompt，除非该工具本身已经返回了严格受控的 evidence card。

专家之间传递的内容：

- 数据是否有效。
- 数据是否新鲜。
- 发生了什么变化。
- 是否异常。
- 对买入/等待/拒绝的方向性影响。
- 该证据的置信度、反证和失效条件。

### 1.2 只传变化、异常和决策影响

静态字段只有在影响判断时才保留。比如行业、总市值、PE、PB、均线值、资金流明细都不应原样堆给模型，而应转成：

- 当前值是否触发阈值。
- 与 3/5/10/20 日相比是否改善或恶化。
- 与价格、成交量、板块、消息是否同向。
- 是否构成支持、反对、中性或等待确认。

### 1.3 数据质量和信号同级

`freshness`、`source`、`status`、`warnings` 不是附属字段，而是判断权重的一部分。过期数据不能和实时数据同权重；空数据、降级数据、失败数据不能被模型当成有效证据。

### 1.4 反证必须显式化

每张证据卡必须能表达反证。系统不只记录“发现了什么”，还要记录“这件事是否削弱当前交易假设”。最终 Judge 应优先阅读反证，而不是只看支持理由。

## 2. 数据层级

### 2.1 Raw Tool Result

完整工具结果，仅用于落盘和调试。

建议位置：

- `tool_raw/<tool_name>/<stock_code>.json`
- 或当前 trace 下的阶段 `full_ref`

不得直接进入多专家 prompt。

### 2.2 EvidenceCard

单工具或单维度压缩后的最小判断单元。

```json
{
  "card_id": "capital_flow:603418:2026-04-15",
  "run_id": "trace-session-id",
  "stock": {
    "code": "603418",
    "name": "友升股份",
    "market": "cn"
  },
  "dimension": "capital_flow",
  "producer": {
    "tool": "get_capital_flow",
    "expert": "capital_expert",
    "version": "evidence-card-v1"
  },
  "data_quality": {
    "status": "ok",
    "as_of": "2026-04-15",
    "freshness": "stale",
    "source": "tushare:moneyflow",
    "source_chain": [
      {"provider": "stockapi:codeFlow", "result": "empty"},
      {"provider": "tushare:moneyflow", "result": "ok"}
    ],
    "warnings": ["primary_source_empty", "fallback_used"],
    "missing_fields": []
  },
  "signals": [
    {
      "name": "main_net_inflow",
      "value": -12831600,
      "unit": "CNY",
      "direction": "negative",
      "strength": "medium",
      "change": {
        "vs_5d": "weakening",
        "vs_10d": "weakening"
      },
      "interpretation": "主力资金近期偏流出，对立即入场构成反证"
    }
  ],
  "impact": {
    "stance": "oppose",
    "action_bias": "wait",
    "confidence": 0.62,
    "score_delta": -8,
    "reason": "资金流与入场方向不一致，且没有出现明显回流确认"
  },
  "counter_evidence": [
    {
      "refuted_claim": "技术面突破可立即买入",
      "refutation": "资金流未同步转强",
      "severity": "medium"
    }
  ],
  "expiry": {
    "valid_until": "2026-04-16",
    "refresh_trigger": "next_trading_day_close_or_intraday_flow_update"
  },
  "raw_ref": "tool_raw/get_capital_flow/603418.json"
}
```

### 2.3 ExpertEvidencePacket

一个专家输出给 Judge 或下游专家的聚合包。它不是工具结果列表，而是专家对一组 EvidenceCard 的压缩判断。

```json
{
  "packet_id": "capital_expert:603418:trace-session-id",
  "expert": "capital_expert",
  "dimension": "capital_flow",
  "stock": {
    "code": "603418",
    "name": "友升股份",
    "market": "cn"
  },
  "stance": "oppose",
  "action_bias": "wait",
  "confidence": 0.64,
  "summary": "资金维度偏弱，主力净流入为负，暂不支持追高入场。",
  "top_supports": [],
  "top_risks": [
    "主力资金未确认突破",
    "最近资金流证据来自 fallback，需关注数据新鲜度"
  ],
  "key_cards": [
    "capital_flow:603418:2026-04-15"
  ],
  "missing_evidence": [
    "实时盘中大单资金"
  ],
  "recommended_next_tools": [
    {
      "tool": "get_capital_flow",
      "reason": "下一交易日收盘后刷新资金确认"
    }
  ],
  "raw_refs": [
    "tool_raw/get_capital_flow/603418.json"
  ]
}
```

### 2.4 JudgeInputPacket

最终 Judge 只消费各专家的 `ExpertEvidencePacket`、关键风控约束和账户约束，不再消费完整工具 JSON。

```json
{
  "stock": {"code": "603418", "name": "友升股份", "market": "cn"},
  "expert_packets": {
    "technical": {"stance": "support", "confidence": 0.71, "summary": "..."},
    "capital_flow": {"stance": "oppose", "confidence": 0.64, "summary": "..."},
    "news_event": {"stance": "neutral", "confidence": 0.45, "summary": "..."},
    "risk": {"stance": "oppose", "confidence": 0.80, "summary": "..."}
  },
  "hard_constraints": {
    "t_plus_1": true,
    "limit_up_down": "not_limit",
    "max_single_position_pct": 20
  },
  "decision_matrix": [
    {"dimension": "technical", "stance": "support", "base_weight": 0.30, "effective_weight": 0.27, "score_delta": 12},
    {"dimension": "capital_flow", "stance": "oppose", "base_weight": 0.25, "effective_weight": 0.18, "score_delta": -8}
  ],
  "top_counter_evidence": [
    "资金流未确认技术突破",
    "行业资金流日期偏旧"
  ]
}
```

## 3. 字段规范

### 3.1 dimension

允许值应稳定，便于前端聚合和回测统计：

- `quote`
- `technical`
- `price_structure`
- `capital_flow`
- `chip`
- `fundamental`
- `news_event`
- `sentiment`
- `sector`
- `macro`
- `regime`
- `risk`
- `account_fit`

### 3.2 data_quality.status

| 值 | 含义 | 是否可作为有效证据 |
| --- | --- | --- |
| `ok` | 数据成功且字段可用 | 是 |
| `partial` | 有部分有效字段 | 视字段而定 |
| `empty` | 接口成功但无数据 | 否，可作为缺口 |
| `stale` | 数据过期但仍有参考价值 | 降权 |
| `failed` | 工具失败 | 否 |
| `timeout` | 工具超时 | 否 |
| `not_supported` | 当前标的不支持 | 否 |

### 3.3 freshness

| 值 | 含义 |
| --- | --- |
| `realtime` | 实时或准实时 |
| `intraday` | 当日盘中 |
| `eod_current` | 最新交易日收盘后 |
| `recent` | 最近 1-3 个交易日 |
| `stale` | 超过策略可接受窗口 |
| `unknown` | 无法判断 |

### 3.4 stance

| 值 | 含义 |
| --- | --- |
| `support` | 支持当前交易假设 |
| `oppose` | 反对当前交易假设 |
| `neutral` | 无明显方向 |
| `wait_confirm` | 有信号但需要下一步确认 |
| `invalid` | 数据无效，不参与判断 |

### 3.5 action_bias

| 值 | 含义 |
| --- | --- |
| `open` | 可考虑开仓 |
| `add` | 可考虑加仓 |
| `hold` | 持有观察 |
| `reduce` | 降低仓位 |
| `exit` | 退出 |
| `wait` | 等待确认 |
| `reject` | 不纳入候选 |

### 3.6 confidence

`confidence` 表示该证据卡或专家包对自身结论的可信度，范围固定为 `0.0-1.0`。

| 区间 | 含义 |
| --- | --- |
| `0.0-0.3` | 低可信，只能作为弱提示或缺口 |
| `0.3-0.6` | 中低可信，需要其他维度确认 |
| `0.6-0.8` | 中高可信，可参与主要决策 |
| `0.8-1.0` | 高可信，通常来自新鲜、稳定、直接相关的数据 |

`confidence` 不等于看涨概率，也不等于最终胜率。它只描述“这条证据支持其自身解释的可靠程度”。

### 3.7 score_delta

系统内部采用 100 分制决策基准：

- 初始中性分为 `50`。
- 所有维度的 `score_delta` 加权后汇总到最终决策分。
- 最终分数必须裁剪到 `0-100`。

单个 EvidenceCard 的 `score_delta` 建议范围为 `-15` 到 `+15`；极端硬风险或确定性利好可放宽到 `-25` 到 `+25`，但必须写明原因。超过该范围应进入硬约束或 veto，而不是继续扩大 delta。

Judge 汇总公式建议：

```text
final_score = clamp(
  50 + sum(score_delta_i * effective_weight_i / max(base_weight_i, 0.01)),
  0,
  100
)
```

其中：

- `base_weight` 来自策略配置、维度默认权重或用户选择的交易风格。
- `effective_weight` 是根据 `freshness`、`confidence`、`data_quality.status` 和市场 regime 调整后的实际权重。
- 不允许无上限累加；所有输出给前端和 Judge 的分数必须在 `0-100` 内。

### 3.8 counter_evidence

`counter_evidence` 用于记录反证，不是普通证据列表。

字段规范：

| 字段 | 含义 |
| --- | --- |
| `refuted_claim` | 被质疑或被削弱的交易假设 |
| `refutation` | 反驳该假设的证据 |
| `severity` | `low / medium / high / veto` |

兼容旧字段时，`claim` 应被解释为 `refuted_claim`，`evidence` 应被解释为 `refutation`。

### 3.9 有效窗口

`expiry.valid_until` 不是由每张卡片随意决定，而应由策略配置和维度默认值共同决定。

默认有效窗口：

| dimension | 默认有效窗口 | 刷新触发 |
| --- | --- | --- |
| `quote` | 盘中 1-5 分钟 | 价格或成交量更新 |
| `technical` | 日线策略 1 个交易日；盘中策略 15-60 分钟 | 新 K 线或关键价位突破 |
| `price_structure` | 1-3 个交易日 | BOS/CHoCH/中枢变化 |
| `capital_flow` | 收盘后 1 个交易日；盘中资金 5-15 分钟 | 资金流更新 |
| `chip` | 1-5 个交易日 | 收盘后筹码数据刷新 |
| `fundamental` | 7-30 天 | 财报、公告、监管事件更新 |
| `news_event` | 1-3 天 | 新事实节点出现 |
| `sentiment` | 盘中 30-120 分钟；收盘后 1 个交易日 | 热度、涨停池、人气榜变化 |
| `sector` | 盘中 15-60 分钟；收盘后 1 个交易日 | 板块排行或资金流更新 |
| `macro` | 1-30 天 | 宏观数据、政策或会议节点更新 |
| `regime` | 日线 1 个交易日；极端波动下盘中刷新 | 波动率/广度/资金状态变化 |
| `risk` | 直到风险解除或新公告覆盖 | 风险公告、交易状态变化 |

策略 YAML 可以覆盖默认窗口，例如超短线策略应缩短 `quote / capital_flow / sentiment / sector` 的有效期，波段策略可以放宽 `technical / chip / fundamental` 的有效期。

EvidenceCard 只记录最终计算出的 `valid_until` 和 `refresh_trigger`；计算逻辑应放在策略配置或 evidence adapter 中。

## 4. 压缩规则

### 4.1 工具到 EvidenceCard

每个工具适配器必须完成以下步骤：

1. 丢弃空字段、重复字段、调试字段和大段原始列表。
2. 提取最新有效时间 `as_of`。
3. 计算 `freshness`。
4. 提取最多 3-5 个有效信号。
5. 对每个信号标注 `direction`、`strength`、`change` 和 `interpretation`。
6. 按 `abs(score_delta)` 从大到小排序 `signals`，影响最大的信号排在最前。
7. 给出 `impact.stance`、`action_bias`、`confidence` 和 `score_delta`。
8. 根据策略配置和维度默认窗口计算 `expiry`。
9. 显式列出反证和缺失证据。
10. 保留 `raw_ref`，不内联完整 raw。

### 4.2 EvidenceCard 到 ExpertEvidencePacket

每个专家最多输出：

- `summary`：不超过 240 字。
- `top_supports`：最多 3 条。
- `top_risks`：最多 3 条。
- `key_cards`：最多 5 张。
- `missing_evidence`：最多 5 条。
- `recommended_next_tools`：最多 3 个。

如果证据卡之间互相矛盾，专家必须写入 `top_risks` 或 `counter_evidence`，不能只选支持自己结论的证据。

### 4.3 ExpertEvidencePacket 到 JudgeInputPacket

Judge 输入只保留：

- 各专家 `stance / action_bias / confidence / summary`。
- 决策矩阵。
- 硬风控约束。
- 最强支持证据和最强反证。
- 数据缺口。

Judge 不接收完整 raw，也不接收长新闻原文、K 线列表、财报表格和资金流明细。

### 4.4 权重来源和动态调整

`decision_matrix.weight` 不由模型临场拍脑袋生成。权重来源优先级如下：

1. 用户指定策略或账户约束。
2. 策略 YAML 中的维度权重。
3. 系统默认权重。

建议默认权重：

| dimension | 短线 | 波段 | 中长线 |
| --- | ---: | ---: | ---: |
| `technical` | 0.25 | 0.25 | 0.15 |
| `price_structure` | 0.20 | 0.20 | 0.10 |
| `capital_flow` | 0.20 | 0.15 | 0.10 |
| `news_event` | 0.10 | 0.10 | 0.10 |
| `sentiment` | 0.10 | 0.05 | 0.03 |
| `sector` | 0.10 | 0.10 | 0.07 |
| `fundamental` | 0.03 | 0.10 | 0.25 |
| `risk` | veto | veto | veto |
| `account_fit` | constraint | constraint | constraint |

动态调整规则：

- `data_quality.status in [failed, timeout, empty, not_supported]`：`effective_weight=0`，只作为缺口或反证。
- `freshness=stale`：`effective_weight *= 0.3-0.6`，具体系数由策略窗口决定。
- `confidence < 0.4`：`effective_weight *= 0.5`。
- `confidence >= 0.8` 且 `freshness in [realtime, intraday, eod_current]`：可小幅上调，但不超过 `base_weight * 1.2`。
- `risk` 维度出现 `severity=veto`：不进入普通加权，直接触发硬阻断或强制 `wait/reject`。

### 4.5 专家冲突处理

多个专家互相矛盾时，Judge 必须先判断冲突类型，再决定是否加权汇总或 veto。

| 冲突类型 | 示例 | Judge 行为 |
| --- | --- | --- |
| 可加权冲突 | 技术面 support，资金面 oppose | 进入 `decision_matrix` 加权汇总，同时写入 `top_counter_evidence` |
| 时间窗口冲突 | 技术面日线 support，盘中情绪急剧恶化 | 优先更短窗口风险，通常降级为 `wait_confirm` |
| 硬风险冲突 | 技术面 support，但监管/退市/ST/跌停风险触发 | `risk` veto，直接 `reject` 或 `wait` |
| 数据质量冲突 | 一个专家基于 stale 数据 support，另一个基于新鲜数据 oppose | 降低 stale 证据权重，保留新鲜证据 |
| 账户约束冲突 | 股票信号 support，但账户仓位/回撤线不允许 | `account_fit` constraint，限制动作或仓位 |

Judge 输出必须解释冲突如何处理，不能只给最终动作。

### 4.6 维度缺失降级

当某一维度工具全部失败时，仍应生成降级包，而不是静默缺失。

降级 `ExpertEvidencePacket` 示例：

```json
{
  "expert": "capital_expert",
  "dimension": "capital_flow",
  "stance": "invalid",
  "action_bias": "wait",
  "confidence": 0.0,
  "summary": "资金面工具全部失败，本轮不能把资金面作为支持证据。",
  "top_supports": [],
  "top_risks": ["资金面证据缺失，禁止用技术突破单独确认买入"],
  "missing_evidence": ["main_net_inflow", "inflow_5d", "intraday_large_order_flow"],
  "recommended_next_tools": [
    {"tool": "get_capital_flow", "reason": "下一轮刷新资金证据"}
  ],
  "raw_refs": []
}
```

Judge 看到缺失维度时：

- 不得把缺失维度当作中性利好。
- 对高依赖该维度的策略降级动作强度。
- 如果关键维度缺失且其他维度证据不足，输出 `wait_confirm`。

## 5. 各维度摘要要求

### 5.1 技术面

只保留：

- 趋势状态：多头、空头、震荡、突破、回踩。
- 关键位置：支撑、压力、止损、追高线。
- 结构变化：BOS、CHoCH、中枢突破、背驰、未完成笔。
- 与量能是否匹配。

不保留完整 K 线数组。

### 5.2 资金面

只保留：

- 最新主力净流入。
- 3/5/10 日变化。
- 是否与价格同向。
- 是否发生异常流入/流出。
- 数据来源和更新时间。

不保留逐日完整资金流表。

### 5.3 消息/事件

只保留：

- 事件链条中的当前节点。
- 事件是否已从宏观/行业传导到公司。
- 相关公司是直接受益、间接受益、潜在受损还是仅主题相关。
- 证据来自公告、新闻、政策、交易异动还是推断。

不允许从热点事件直接硬跳到个股结论；必须记录中间传导节点。

### 5.4 情绪面

只保留：

- 情绪方向：拥挤、修复、分歧、恐慌、冷却。
- 情绪来源：新闻热度、板块热度、人气榜、涨停池、社媒。
- 情绪是否已有价格兑现。
- 是否存在反向交易风险。

### 5.5 基本面

只保留：

- 是否存在硬风险：ST、退市、监管、质押、解禁、减持、业绩预警。
- 财务变化是否异常。
- 估值是否构成主要矛盾。
- 是否与当前交易周期匹配。

不保留三大报表原始行。

## 6. 上下文预算

建议预算：

| 层级 | 单股票预算 | 用途 |
| --- | ---: | --- |
| Raw Tool Result | 不进模型 | 落盘、前端、复盘 |
| EvidenceCard | 600-1200 字 | 单工具/单维度有效证据 |
| ExpertEvidencePacket | 800-1500 字 | 专家输出 |
| JudgeInputPacket | 3000-6000 字 | 最终裁决 |

全局约束：

- 候选池阶段最多传 top 10 候选的摘要。
- 深度分析阶段默认 top 4。
- 每只股票最多保留 6 个维度的 EvidenceCard。
- Judge 最多读取每只股票 5 条支持证据和 5 条反证。

## 7. 与现有 trace 的关系

现有 `SelectionRunContext` 已有 `summary / full / full_ref`，本协议在其上收敛语义：

- `summary`：应逐步迁移为 `ExpertEvidencePacket` 或阶段摘要。
- `full`：可包含 EvidenceCard 列表，但不要再被下游 prompt 全量引用。
- `full_ref`：指向完整阶段文件或 raw 工具结果。
- `evidence_ledger.summary.entries[*].preview`：只作为调试预览，不作为专家判断主输入。

前端展示建议：

- 默认展示 ExpertEvidencePacket。
- 点击展开 EvidenceCard。
- 再点击 raw_ref 查看原始工具结果。

## 8. 实施顺序

### P0：协议与类型

- 新增 `EvidenceCard`、`ExpertEvidencePacket`、`JudgeInputPacket` schema。
- 给每个卡片生成稳定 `card_id`。
- 在 trace 中落盘 `evidence_cards.json` 和 `expert_packets.json`。

### P1：工具适配

采用中间层 `evidence_adapter`，不要求每个工具直接返回 EvidenceCard。

职责划分：

- 工具层继续负责真实数据获取、source_chain、错误诊断和 raw 结构化返回。
- `evidence_adapter` 负责 raw -> EvidenceCard 的压缩、排序、计分、新鲜度和有效窗口计算。
- 专家只消费 EvidenceCard 或 ExpertEvidencePacket。
- trace 同时保存 raw 和 EvidenceCard，便于前端逐层展开。

建议模块边界：

```text
src/agent/evidence/
  schemas.py          # EvidenceCard / ExpertEvidencePacket / JudgeInputPacket
  adapter.py          # dispatch raw tool result to dimension-specific adapter
  freshness.py        # validity window and freshness calculation
  scoring.py          # score_delta and confidence calibration
  dimensions/
    technical.py
    capital_flow.py
    chip.py
    news_event.py
    regime.py
```

优先改造高频工具：

- `get_realtime_quote`
- `analyze_trend`
- `analyze_price_structure`
- `get_capital_flow`
- `get_chip_distribution`
- `get_stock_info`
- `search_comprehensive_intel`
- `detect_market_regime`

### P2：多专家链路改造

- 专家输入从 raw evidence 改为 EvidenceCard。
- 专家输出统一为 ExpertEvidencePacket。
- Judge 输入统一为 JudgeInputPacket。
- 保留 raw/full_ref 给前端和调试。

### P3：质量门禁

- 如果 `freshness=stale`，自动降低证据权重。
- 如果 `status in [failed, timeout, empty]`，只能作为缺口或反证，不能作为支持证据。
- 如果股票代码和名称不一致，EvidenceCard 直接标记 `invalid`。
- 如果同一维度证据互相冲突，必须进入 `counter_evidence`。
- 如果专家之间出现硬风险冲突，Judge 必须优先执行 veto/constraint，而不是普通加权。
- 如果关键维度缺失，Judge 必须降低动作强度或输出 `wait_confirm`。

## 9. 验收标准

- 多专家 prompt 中不再出现完整工具 JSON。
- 每个专家输出都能追溯到 `key_cards` 和 `raw_refs`。
- 前端能展示“专家结论 -> 证据卡 -> 原始数据”的三层结构。
- Judge 结论能解释哪些证据支持、哪些证据反对、哪些证据缺失。
- 工具数量增加时，单次专家输入长度不会线性增长。
