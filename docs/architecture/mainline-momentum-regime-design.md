# 主线动量 Regime 设计方案

状态：设计草案

背景来源：`docs/architecture/关于regime的讨论与设计.md`

## 1. 问题背景

当前讨论的核心问题是：传统技术指标在主线龙头上经常给出过度保守结论。

典型现象：

- AI 产业链、算力金属、光模块、光芯片、封装封测、CPO、PCB、玻璃基板、树脂、光纤、存储等方向持续成为资金主线。
- 其中大量强势股长期偏离布林带上轨、RSI 超买、乖离率偏高。
- 现有 AI 如果直接把这些信号解释为“追高风险”，容易持续输出“不建仓/等待”，错过主升浪。
- 但如果简单改成“主线股超买一律豁免”，又会在退潮期、高潮末端和后排跟风股上放大回撤。

因此，目标不是把 AI 调得更激进，而是让 AI 先识别：

1. 当前主题是否真的是市场主线。
2. 个股在主题内是核心龙头、中军、后排，还是高潮补涨。
3. 当前价格结构是主升、分歧、高潮，还是退潮。
4. 同一个“超买”信号在不同环境下应该如何解释。

## 2. 与现有系统关系

仓库内已经具备以下基础能力：

- `src/agent/regime.py`：市场级 `detect_market_regime`，输出大盘环境、波动分档、情绪状态、Wyckoff 相位和风险等级。
- `src/agent/regime_probability.py`：Regime forward probability，用历史后验统计描述当前 regime 后续路径。
- `src/agent/stock_selection.py`：选股链路已经消费 `market_regime`，并在组合配置阶段做确定性降档。
- `src/agent/judge_sanity.py`：Risk Gate 会在 `risk_off` / `panic` / `extreme` 环境下降级主动开仓。
- `src/agent/stock_selection_prompts.py`：候选筛选已经要求“突破、涨停、资金接力型机会不得只因乖离率高直接淘汰”，并要求点位层计算 `Breakout_Continuation`、`Fakeout_Exhaustion`、`Mean_Reversion_Pullback` 三类场景。

现有系统的问题不在于缺少“大盘 regime”，而在于缺少两层更贴近实战的结构化判断：

- 主题层：Theme Regime。
- 个股生态位层：Stock Role / Asset Setup。

本方案是在现有 Market Regime 之上增加主题动量层，不替换现有风控。

## 3. 设计原则

1. Risk Gate 不可被主题热度绕过。
   `risk_off`、`panic`、`extreme volatility` 仍然是硬约束。主线很强只能影响候选分型、仓位和条件单，不能覆盖账户级风控。

2. 不直接屏蔽布林带、RSI、乖离率。
   指标本身保留，但解释权重随 regime 改变：
   - 均值回归机会：超买是拒绝或等待理由。
   - 主线动量机会：超买是强势特征，但必须绑定承接确认和失效条件。
   - 高潮退潮机会：超买重新变成衰竭风险。

3. 不硬编码“AI 算力永远是主线”。
   主题白名单可以作为初始 universe，但是否进入主线状态必须由板块排名、资金、涨停、宽度和持续性每日计算。

4. 龙头和后排必须分开处理。
   核心龙头的偏离上轨可能是主升浪，后排跟风的偏离上轨通常是流动性和接盘风险。

5. 输出条件单，不输出无条件追高。
   主线动量策略的核心是 If-Then 条件矩阵，而不是“现在买”。

## 4. 目标与非目标

### 4.1 目标

- 增加 `theme_regime`，识别主题处于主升、分歧、高潮、退潮还是未知。
- 增加 `stock_role`，识别个股在主题中的生态位。
- 改造候选筛选和 Meta-Agent，让主线核心股不再只因超买被淘汰。
- 改造点位计算，让主线动量票必须给出承接确认、失效条件和回踩买回方案。
- 保留现有 Risk Gate 的硬降级能力。
- 为后续复盘和回测留下结构化字段。

### 4.2 非目标

- 不把主线动量做成账户默认策略。
- 不让 LLM 仅凭文字判断主题热度。
- 不让任何主题热度覆盖账户仓位上限、止损、流动性约束和 Risk Gate。
- 不一次性引入复杂机器学习模型，第一版以确定性规则和可审计证据为主。

## 5. 总体架构

```text
Market Regime
  detect_market_regime
  get_regime_forward_probability
        |
        v
Theme Momentum Snapshot
  hot sectors
  sector rankings
  sector leaders
  limit-up pool
  local daily breadth
        |
        v
Candidate Evidence
  theme_regime
  theme_membership
  stock_role
  momentum_setup
        |
        v
Candidate Screening
  不因超买直接淘汰主线核心动量票
  对后排/退潮票提高风险惩罚
        |
        v
Meta-Agent
  asset_regime
  hard_constraints_for_pricing_agent
  max_chase_premium
        |
        v
Pricing Agent
  Breakout_Continuation
  Fakeout_Exhaustion
  Mean_Reversion_Pullback
        |
        v
Judge + Risk Gate
  保留 risk_off / panic / extreme 硬降级
```

## 6. 新增核心概念

### 6.1 Theme Regime

主题层 regime 用于回答：

> 这个主题现在是不是资金主线？如果是，处于主升、分歧、高潮还是退潮？

建议枚举：

```text
mainline_markup        主线主升
mainline_divergence    主线分歧
climax_extension       高潮延伸
rotation_weakening     轮动弱化
theme_risk_off         主题退潮
range_rotation         轮动震荡
unknown                数据不足
```

解释：

- `mainline_markup`：主题持续领涨，核心股趋势良好，资金和宽度支持。
- `mainline_divergence`：主题仍是主线，但内部开始分化，适合只看核心，不碰后排。
- `climax_extension`：涨停数量和涨幅强，但炸板、长上影、放量滞涨增多，需要降低追高权限。
- `rotation_weakening`：主题排名下滑，资金流入减弱，强势股开始分化。
- `theme_risk_off`：核心龙头破位、跌停或资金大幅流出，主题仓降级。
- `range_rotation`：主题有表现但持续性不足，按轮动处理。

### 6.2 Stock Role

个股生态位用于回答：

> 这只股票是主线核心，还是后排跟风？

建议枚举：

```text
core_leader            核心龙头
core_midcap            核心中军
high_beta_leader       高弹性核心
follower               后排跟风
late_chaser            高潮补涨
exhaustion_candidate   衰竭候选
unrelated              非主题相关
unknown                数据不足
```

判定参考：

- 板块内成交额排名。
- 板块内涨幅排名。
- 是否最先涨停或涨停封单强。
- 连板高度或阶段新高。
- 近 20 / 60 日相对板块强度。
- 是否被多个热点板块共同覆盖。
- 是否出现放量滞涨、炸板、长上影、跌破 5/10 日线。

### 6.3 Momentum Setup

动量 setup 用于回答：

> 当前强势结构应该如何交易或等待？

建议枚举：

```text
breakout_confirmation      右侧突破确认
first_pullback             首次回踩生命线
high_tight_flag            高位横盘蓄势
limit_up_continuation      涨停接力
divergence_to_consensus    分歧转一致
fakeout_exhaustion         假突破/衰竭
mean_reversion_only        仅适合均值回归
unknown                    数据不足
```

## 7. 数据来源

第一版优先复用现有工具，不新增重型外部依赖。

### 7.1 主题热度

可用工具：

- `get_stockapi_hot_sectors`
- `get_sector_rankings`
- `get_stockapi_hot_sector_leaders`
- `get_stockapi_limit_up_pool`
- `get_stockapi_sector_constituents`
- `get_stockapi_sector_flow_history`

主题热度字段：

```json
{
  "theme": "ai_compute_chain",
  "theme_name": "AI 算力产业链",
  "as_of": "YYYY-MM-DD",
  "regime": "mainline_markup",
  "confidence": 0.0,
  "data_quality": "sufficient | limited | insufficient",
  "scores": {
    "rank_persistence": 0.0,
    "capital_flow": 0.0,
    "limit_up_breadth": 0.0,
    "leader_strength": 0.0,
    "internal_breadth": 0.0,
    "exhaustion_risk": 0.0
  },
  "evidence": [],
  "conflicts": []
}
```

### 7.2 个股生态位

可用证据：

- 个股所属板块。
- 所属板块是否命中主题 universe。
- 个股在主题成分中的成交额排名、涨幅排名、资金流排名。
- 是否命中 hot-sector leaders。
- 是否在涨停池中，首封时间、炸板次数、连板高度。
- 本地日线价量结构：MA5/10/20、阶段新高、量能、换手、振幅。

个股输出字段：

```json
{
  "code": "300000",
  "theme_membership": [
    {
      "theme": "ai_compute_chain",
      "matched_boards": ["CPO", "光模块"],
      "membership_confidence": 0.0
    }
  ],
  "stock_role": "core_leader",
  "momentum_setup": "breakout_confirmation",
  "overbought_interpretation": "strength_requires_confirmation",
  "chase_permission": "conditional_only",
  "evidence": [],
  "risk_flags": []
}
```

## 8. 主题评分口径

### 8.1 Rank Persistence

衡量主题是否连续占据市场前排。

建议输入：

- 近 1 / 3 / 5 / 10 个交易日板块排名。
- 主题相关板块在涨幅榜、资金榜中的出现次数。
- 排名是否从单日爆发变成连续占优。

建议规则：

- 多个相关板块连续 3 日进入前 20，分数提高。
- 仅单日冲高但无持续性，最高只能支持 `range_rotation` 或 `mainline_divergence`。

### 8.2 Capital Flow

衡量资金是否持续流入。

建议输入：

- `get_stockapi_hot_sectors` 的净流入、强度、流入天数。
- `get_stockapi_sector_flow_history` 的近几日资金流。

建议规则：

- 连续净流入且强度上升，支持 `mainline_markup`。
- 涨幅强但资金流出，写入冲突项，倾向 `climax_extension` 或 `rotation_weakening`。

### 8.3 Limit-Up Breadth

衡量情绪强度和接力活跃度。

建议输入：

- 涨停池中主题相关标的数量。
- 连板高度。
- 炸板次数。
- 首封时间。

建议规则：

- 涨停数量扩散、炸板率低，支持主线强度。
- 涨停数量暴增但炸板率高、后排补涨多，支持 `climax_extension`。

### 8.4 Leader Strength

衡量核心龙头是否仍然健康。

建议输入：

- 热门板块龙头列表。
- 核心标的是否创新高。
- 是否跌破 5/10 日线。
- 是否放量滞涨或长上影。

建议规则：

- 核心龙头创新高且成交额健康，支持 `mainline_markup`。
- 核心龙头跌停、破位或连续放量滞涨，主题直接降级。

### 8.5 Internal Breadth

衡量主题内部是健康扩散还是只剩少数抱团。

建议输入：

- 主题成分站上 MA5/10/20 的比例。
- 主题成分近 5 日上涨比例。
- 成交额是否集中在少数标的。

建议规则：

- 宽度提升，主线更健康。
- 宽度下降但龙头仍强，判定为 `mainline_divergence`。
- 龙头和宽度同时走弱，判定为 `theme_risk_off`。

### 8.6 Exhaustion Risk

衡量高潮和衰竭风险。

建议输入：

- 放量长上影数量。
- 炸板率。
- 跌破 5/10 日线的核心标的数量。
- 涨停次日负反馈数量。
- 高频轮动但无持续承接。

建议规则：

- 衰竭风险高时，不允许简单豁免超买。
- `climax_extension` 下只能保留小仓位条件单，禁止无条件追高。

## 9. Regime 判定示例

### 9.1 主线主升

条件示例：

- 主题相关板块连续进入市场前排。
- 主题资金连续净流入。
- 核心龙头创新高或沿 MA5/MA10 上行。
- 涨停池中主题标的活跃，炸板率可控。
- 内部宽度未明显恶化。

输出：

```json
{
  "theme_regime": "mainline_markup",
  "interpretation": "主线主升期，核心动量票允许用突破/回踩确认参与。",
  "constraints": [
    "仅核心龙头/中军享受超买解释降权",
    "后排跟风仍按追高风险处理",
    "必须给出承接确认和失效条件"
  ]
}
```

### 9.2 主线分歧

条件示例：

- 主题仍在市场前排，但内部宽度下降。
- 龙头仍强，后排开始掉队。
- 资金流入减弱或分化。

输出：

```json
{
  "theme_regime": "mainline_divergence",
  "interpretation": "主线未结束但分化加剧，只允许核心标的条件参与。",
  "constraints": [
    "follower / late_chaser 不得因主题强度升级",
    "core_leader 只能给 conditional_open",
    "Fakeout_Exhaustion 必须明确"
  ]
}
```

### 9.3 高潮延伸

条件示例：

- 多个后排补涨。
- 涨停数量暴增。
- 炸板率、长上影、放量滞涨上升。

输出：

```json
{
  "theme_regime": "climax_extension",
  "interpretation": "主题处于情绪高潮，动量仍可能延续，但追高风险显著提高。",
  "constraints": [
    "禁止 immediate_open",
    "只能等待分歧转一致或首次缩量回踩",
    "max_chase_premium 必须收紧",
    "单票仓位必须下降"
  ]
}
```

### 9.4 主题退潮

条件示例：

- 核心龙头跌破 MA10 或出现跌停。
- 主题资金明显流出。
- 主题板块排名掉出前列。
- 涨停次日负反馈扩散。

输出：

```json
{
  "theme_regime": "theme_risk_off",
  "interpretation": "主题退潮，超买恢复为强风险信号。",
  "constraints": [
    "主动开仓降级为 wait",
    "持仓优先计算减仓和止损",
    "后排标的直接 reject 或 monitor"
  ]
}
```

## 10. 下游使用规则

### 10.1 Candidate Screening

新增规则：

- 如果 `market_regime` 是 `risk_off` / `panic` / `extreme`，维持现有硬降级。
- 如果 `theme_regime=mainline_markup` 且 `stock_role in {core_leader, core_midcap, high_beta_leader}`：
  - 不得只因布林带上轨、RSI 超买、乖离率高直接淘汰。
  - 必须把风险转为 `risk_flags` 和后续点位约束。
- 如果 `stock_role in {follower, late_chaser}`：
  - 超买、炸板、放量滞涨应提高淘汰概率。
  - 不得仅因主题强度进入 deep dive。
- 如果 `theme_regime in {climax_extension, theme_risk_off}`：
  - 后排候选最高只能是 monitor。
  - 核心候选也必须标注更小首仓和更严格失效条件。

### 10.2 Meta-Agent

Meta-Agent 需要把主题证据转为点位层硬约束：

- `asset_regime`
- `theme_regime`
- `stock_role`
- `overbought_interpretation`
- `max_chase_premium`
- `invalidation_level`
- `mean_reversion_anchor`
- `risk_constraints`

示例：

```json
{
  "asset_regime": "Right_Side_Momentum_High_Exhaustion_Risk",
  "theme_regime": "mainline_divergence",
  "stock_role": "core_leader",
  "hard_constraints_for_pricing_agent": {
    "max_chase_premium": {
      "value": "1.5%",
      "source": "theme_regime",
      "reason": "主线分歧期只允许承接确认后的有限追价。"
    },
    "risk_constraints": [
      "若主题核心龙头跌破 10 日线，Breakout_Continuation 降级为 watch。",
      "若放量长上影或炸板回封失败，触发 Fakeout_Exhaustion。"
    ]
  }
}
```

### 10.3 Pricing Agent

点位层必须分别计算：

- `Breakout_Continuation`
  - 条件：突破关键高点、主题同步走强、核心股承接确认。
  - 动作：只允许 `conditional_open` 或 `strong_watch`，除非未来显式允许更激进模式。

- `Fakeout_Exhaustion`
  - 条件：冲高回落、放量滞涨、炸板、跌破分时均线或 MA5/MA10。
  - 动作：回避、减仓、等待二次确认。

- `Mean_Reversion_Pullback`
  - 条件：首次缩量回踩 MA5/MA10 或前高平台，主题未退潮。
  - 动作：低吸条件单或买回参考。

### 10.4 Judge

Judge 需要新增审查问题：

- 候选是否真的属于当前主题，而不是名称相似。
- 个股是否为核心，还是后排补涨。
- 超买被豁免的理由是否来自主题和个股生态位证据。
- 是否存在 `Fakeout_Exhaustion` 方案。
- 是否绕过了 Risk Gate。

### 10.5 Risk Gate

Risk Gate 不直接消费 `theme_regime` 来放宽限制。

可接受的方式：

- `theme_regime` 可以降低或提高计划仓位建议。
- `theme_regime` 可以影响 `max_chase_premium`。
- `theme_regime` 可以让核心股从 reject 变成 conditional monitor。

不可接受的方式：

- `theme_regime` 不能把 `risk_off` 下的主动开仓重新升回 open。
- `theme_regime` 不能覆盖账户最大仓位。
- `theme_regime` 不能删除止损或失效条件。

## 11. 配置建议

第一版主题 universe 建议配置化，不写死在代码中。

示例：

```yaml
themes:
  ai_compute_chain:
    display_name: AI 算力产业链
    aliases:
      - AI 算力
      - 光模块
      - CPO
      - 光芯片
      - PCB
      - 先进封装
      - 封装封测
      - 玻璃基板
      - 树脂
      - 光纤
      - 存储
      - 算力金属
    seed_symbols: []
    max_theme_exposure_pct: 20
    max_single_stock_pct: 5
```

注意：

- `aliases` 只用于召回和归类，不代表主题一定处于主线。
- `seed_symbols` 可为空，优先由板块和工具结果动态识别。
- 仓位上限只是主题策略建议，最终仍由账户配置和 Risk Gate 决定。

## 12. 实施路径

### Phase 1：文档与字段设计

- 增加本设计文档。
- 明确 `theme_regime`、`stock_role`、`momentum_setup` 的 schema。
- 明确 prompt 和 Risk Gate 的边界。

验收：

- 文档能解释为什么不是简单屏蔽超买。
- 文档能指导后续代码改造。

### Phase 2：Theme Momentum Snapshot

新增内部函数或工具，聚合现有数据源：

- 热门板块。
- 板块排行。
- 热门板块龙头。
- 涨停池。
- 主题成分本地日线宽度。

输出 `theme_momentum_snapshot`。

验收：

- 数据源失败时返回 `limited / insufficient`，不编造主题状态。
- 同一主题可输出热度、持续性、风险和冲突项。

### Phase 3：Candidate Evidence 接入

在候选证据层增加：

- `theme_membership`
- `theme_regime`
- `stock_role`
- `momentum_setup`
- `overbought_interpretation`

验收：

- 主线核心票不会只因超买被淘汰。
- 后排补涨不会只因主题强而升级。
- 所有判断都有 evidence / risk_flags。

### Phase 4：Prompt 与 Fallback 改造

改造：

- Candidate Screening prompt。
- Meta-Orchestrator prompt。
- Pricing Agent prompt。
- Adversarial Review / Judge 检查项。

验收：

- `mainline_markup + core_leader` 支持 Breakout / Pullback 条件单。
- `climax_extension + late_chaser` 被限制为 monitor/reject。
- `theme_risk_off` 下主动开仓降级。

### Phase 5：复盘与校准

从 Agent Trace 统计：

- 因“超买/乖离率”被 wait/reject 的股票，后续 1/3/5/10 日收益和最大回撤。
- 按 `theme_regime`、`stock_role` 分组。
- 比较 open / wait / reject 的机会成本和回撤。

验收：

- 能证明哪些情况下超买应该降权。
- 能证明哪些情况下超买仍然应该强风控。
- 用数据调整 `max_chase_premium`、仓位和 deep dive 门槛。

## 13. 测试建议

### 13.1 单元测试

覆盖：

- 主题热度不足时返回 `unknown`。
- 热门板块持续、资金流入、龙头健康时返回 `mainline_markup`。
- 炸板率和长上影升高时返回 `climax_extension`。
- 核心龙头破位时返回 `theme_risk_off`。
- 同一只股票在不同 `theme_regime` 下 `overbought_interpretation` 不同。

### 13.2 Prompt 回归测试

构造样例：

1. 主线主升 + 核心龙头 + RSI 超买。
   期望：不直接 reject，输出条件单约束。

2. 主线分歧 + 后排补涨 + 偏离上轨。
   期望：monitor 或 reject。

3. 主题退潮 + 核心股破位。
   期望：主动开仓降级。

4. 大盘 `risk_off` + 主题仍强。
   期望：Risk Gate 仍降级。

### 13.3 Trace 复盘

增加 Trace 字段检查：

- 是否记录 `theme_regime`。
- 是否记录 `stock_role`。
- 是否记录超买解释。
- 是否记录点位层三场景。
- 是否记录 Risk Gate 是否触发。

## 14. 主要风险

### 14.1 主题归因错误

风险：股票名称或板块标签相似，但实际并不是 AI 算力主线。

缓解：

- 主题 membership 必须来自所属板块、资金榜、龙头榜、涨停原因等多源证据。
- 单一来源最高只能给 limited confidence。

### 14.2 高潮期过度追价

风险：系统把高潮延伸误判为主升。

缓解：

- 引入 `exhaustion_risk`。
- `climax_extension` 下禁止 immediate open。
- 强制计算 Fakeout_Exhaustion。

### 14.3 后排跟风被误升

风险：主题强导致后排票被错误升级。

缓解：

- `stock_role` 作为超买豁免前置条件。
- 只有核心龙头/中军享受超买解释降权。

### 14.4 风控被软化

风险：主题动量规则污染账户级风控。

缓解：

- Risk Gate 不消费 `theme_regime` 来放宽限制。
- 所有主题动量仓受单票和主题总暴露上限约束。

## 15. 最小可行版本

MVP 只做四件事：

1. 用现有工具生成 `theme_momentum_snapshot`。
2. 给候选股增加 `theme_regime` 和 `stock_role`。
3. 修改候选筛选规则：
   - `mainline_markup + core_leader/core_midcap` 不因超买直接淘汰。
   - `climax_extension/theme_risk_off + follower/late_chaser` 严格降级。
4. 在点位层强制输出：
   - 突破确认。
   - 假突破/衰竭。
   - 回踩买回。

第一版不要追求自动买卖结论，先让系统从“看到超买就否决”升级为“识别 setup 后给条件单”。

## 16. 结论

这套改造的关键不是让 AI 更敢追高，而是让 AI 有能力区分：

- 主线核心的强势超买。
- 后排补涨的危险超买。
- 高潮末端的衰竭超买。
- 退潮期的接盘超买。

最终原则：

> Market Regime 决定账户风险边界，Theme Regime 决定主题参与权限，Stock Role 决定个股是否享受动量解释，Pricing Agent 决定是否存在可执行条件单。

## 17. 当前落地状态

已完成第一版代码闭环：

- 新增 `src/agent/theme_momentum.py`，用纯函数生成 `theme_momentum_snapshot`，并对 seed 或单股 symbol 输出同口径 `theme_profile`。
- `src/agent/candidate_experts_v2/committee.py` 在 Seed Pool 收尾阶段聚合已有热点板块、涨停池、人气榜和热点龙头结果，输出 `theme_momentum`，并给每个 seed 标注：
  - `theme_regime`
  - `stock_role`
  - `momentum_setup`
  - `overbought_interpretation`
  - `chase_permission`
- `seed_pool_summary.preview` 和候选字典会暴露 `theme_profile`，便于 Trace 复盘。
- `AgentExecutor` 的单股 `entry_analysis` / `position_review` 会在 ReAct 前预取 StockAPI 热点板块、涨停池、热点龙头和人气榜，围绕当前 symbol 生成 `single_stock_theme_profile` 并注入 user message；如果只命中热榜但不匹配主题别名，仍会标记为 `unrelated`，不会升级为主线核心。
- 候选筛选、Meta-Agent、点位计算 prompt 和单股 Planner prompt 已加入约束：只有 `mainline_markup/mainline_divergence + core_leader/core_midcap/high_beta_leader` 才能把超买降级为“强势但需承接确认”；高潮、退潮、后排、无关主题和衰竭候选不能因主题热度升级。
- StockAPI 工具入口已修正 `.env` 加载，确保本地续费 token 可被工具进程读取；`fake_useragent` 缺失时 Eastmoney patch 使用稳定 UA fallback，不再阻断 StockAPI 工具导入。

当前仍保留的边界：

- `theme_momentum_snapshot` 第一版已接入 Seed Pool 和单股 ReAct 预取，但还没有单独做 API/前端展示页。
- StockAPI `hotBkJlrDr`、`hotBkJlrLongTou`、`youziRank` 如果账号无对应套餐权限，会以 `failed/limited` 进入证据，不会编造主线状态。
- Risk Gate 仍不消费 `theme_regime` 来放宽账户级硬约束。
