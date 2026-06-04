# 选股链路说明

本文说明 `watchlist_scan` 选股链路的当前实现语义。更底层的实施细节、类名和历史迁移记录见 [选股链路重构实施方案](选股链路重构-实施方案.md)。

## 1. 设计目标

选股链路解决的是两个容易混淆的问题：

1. 哪些股票本身值得研究。
2. 在当前账户、市场和风控约束下，哪些股票现在接近可执行。

因此最终报告不能只有一个“首选标的”。当前报告拆成：

| 字段 | 含义 | 主要来源 |
| --- | --- | --- |
| `机会首选` | 股票本身的机会质量，不因账户过于谨慎而被抹掉 | 深挖结果 + Meta 约束 + 点位计划 |
| `执行首选` | 当前账户、市场和风控约束下最接近可执行的计划 | `portfolio_allocation.full.positions_plan[].rank` |
| `可观察标的` | 已经进入主线深挖，但执行优先级低于首选 | 深挖主线 + 组合配置 |
| `观察池` | 未进入本轮深挖、证据不足或仅适合后续跟踪 | 候选池与证据包 |

这个拆法避免两类错误：

- 好股票因为账户现金不足、风险偏好谨慎或市场高波动而从报告顶部消失。
- 候选池里的高分股票被误写成可以买入。

## 2. 总体流程

```text
用户选股需求
  -> L1 seed pool
  -> SeedFactPacket
  -> 四席位并行
  -> 聚合与名额分配
  -> candidate_screening
  -> single_stock_deep_dive
  -> meta_orchestrator
  -> pricing_agent
  -> portfolio_allocation
  -> adversarial_review
  -> judge_decision
  -> final.md
```

各阶段只做自己的事。越靠前越偏“发现”，越靠后越偏“执行”。

## 3. L1 seed pool

L1 的职责是高召回，不给买入结论。

当前主要来源：

| 来源 | 作用 |
| --- | --- |
| `AlphaSift` | YAML 多因子策略候选，适合承载可配置硬筛、因子打分和策略标签 |
| `Sequoia` | 本地日线形态候选，覆盖突破、RPS、旗形、涨停洗盘、趋势错杀等技术结构 |
| 资金异动 | 主力资金、资金连续性、龙虎榜、两融等 |
| 热点板块 | 板块强度、主题扩散、板块龙头 |
| 盘前日报主题 | 东方财富财经早餐原文中的行业/主题线索 |
| 用户自选 | 用户显式指定的股票优先进入 |

`AlphaSift` 和 `Sequoia` 是当前 L1 的本地主干候选源；资金、板块和盘前主题用于补充资金行为、市场热度和消息催化维度。

低精度来源不会直接主导主 seed pool。对质量不稳定的消息或基本面样本，更适合作为深层补证、观察池或排障信息。

## 4. SeedFactPacket

进入四席位前，每只 seed 会先构建共享事实包。

典型字段包括：

- 股票代码、名称、行业和宽口径业务线索。
- 趋势、均线、量能、价格结构和支撑压力。
- 资金流、筹码、板块和消息主题线索。
- 事实缺口、工具失败和数据时效。

四席位读取同一份 `SeedFactPacket`，避免每个席位重复拉同一批工具，也避免同一只股票在不同席位看到不同事实。

## 5. 四席位

四席位不是四个数据维度，而是四套打法视角：

| 席位 | 关注问题 | 输出 |
| --- | --- | --- |
| 低位启动席 | 低位结构是否开始转强 | 低位启动候选、证伪点、缺口 |
| 动量席 | 趋势/资金/板块是否支持强势延续 | 动量候选、追高风险、资金验证 |
| 质量修复席 | 基本面或估值修复是否尚未充分反映 | 质量修复候选、估值/业绩风险 |
| 主题催化席 | 盘前主题是否与业务匹配且有资金或板块验证 | 主题催化候选、主题证伪点 |

四席位只决定候选是否进入后续主线，不输出最终买卖建议。

## 6. 聚合与初筛

四席位输出会先进入聚合层：

- 去重。
- 标记多席位共识。
- 标记冲突和反证。
- 按市场 Regime 分配各席位名额。
- 防守市降低动量席位权重。

随后 `candidate_screening` 选择进入 `single_stock_deep_dive` 的主线标的。未进入深挖的候选只能出现在观察池或附录，不能进入核心结论表。

## 7. 单股深挖

`single_stock_deep_dive` 对每只主线候选按 setup playbook 补证：

- 技术结构和关键价位。
- 资金流和筹码结构。
- 主题/消息是否真实匹配。
- 基本面和估值缺口。
- 论点失效条件和价格止损条件。

深挖结果可以输出 `action_bias=wait`，但这不等于机会消失。很多高质量机会在账户或市场约束下只能等待条件触发。

## 8. Meta-Agent

Meta-Agent 不负责最终排序，也不直接给买卖点。

它的输入：

- 四席位报告。
- 单股深挖摘要。
- 市场 Regime。
- 证据缺口和冲突标记。

它的输出：

| 字段 | 说明 |
| --- | --- |
| `asset_regime` | 资产机会类型，例如右侧动量、低位启动、质量修复、主题跟随等 |
| `factual_consensus` | 跨席位和深挖形成的事实共识 |
| `strategic_divergence` | 不同席位之间的主观分歧 |
| `hard_constraints_for_pricing_agent` | 点位计算必须遵守的硬约束 |
| `required_pricing_scenarios` | 点位计算必须覆盖的顺势、衰竭、回归等场景 |

Meta-Agent 的重点是把“股票机会”翻译成“下游可计算的约束包”。

## 9. 点位计算层

`pricing_agent` 消费 Meta 约束包，输出 If-Then 条件单矩阵：

| 字段 | 说明 |
| --- | --- |
| `condition` | 触发条件 |
| `execution_mode` | `immediate_open` / `conditional_open` / `strong_watch` / `plain_wait` / `reject` |
| `entry_zone` | 入场区间 |
| `stop_loss` | 止损位 |
| `failure_condition` | 论点失效条件 |
| `risk_reward_comment` | 风险收益说明 |

点位计算层不重新判断股票好坏，也不能覆盖 Meta 的硬约束。

## 10. 组合配置排序

`portfolio_allocation.full.positions_plan[].rank` 是执行排序真源。

它综合：

- 深挖机会质量。
- Meta 硬约束。
- 点位计算条件单。
- 账户现金、仓位、风险偏好。
- 市场 Regime。
- 是否存在明确入场、止损和失效条件。

排序原则：

- `open` 且证据完整的计划优先。
- `wait + conditional_open` 只有在具备明确触发、止损和失效条件时才可排前。
- `monitor`、`watch`、`reject` 不能包装成执行首选。

注意：执行排序不等于机会排序。账户过于谨慎、现金缺失或市场高波动会影响 `执行首选`，但不应该把高质量机会从 `机会首选` 中抹掉。

## 11. 反方审查与 Judge

`adversarial_review` 负责挑战组合计划：

- 是否违反 Meta 硬约束。
- 是否忽略点位矩阵中的失效条件。
- 是否存在工具失败、证据缺口或市场环境反证。
- 是否把候选分误当推荐分。

`judge_decision` 负责最终裁决：

- `open`
- `wait`
- `monitor`
- `reject`

Judge 不补新证据，只基于已有主方案、反方审查和共享证据裁决。

## 12. 最终报告

最终报告是确定性渲染，不是新的 LLM stage。

核心表头字段：

| 字段 | 渲染规则 |
| --- | --- |
| `最终动作` | Judge 或组合配置的总动作 |
| `裁决` | Judge 的 `primary_plan_verdict` |
| `机会首选` | 主线候选中机会质量最高且没有硬排除的股票 |
| `执行首选` | `positions_plan.rank` 里最接近可执行的股票，条件计划需标注“条件触发” |
| `可观察标的` | 主线中除首选外仍值得跟踪的股票 |
| `核心原因` | 来自组合配置和逐股证据 |
| `最大约束` | 市场、账户、数据缺口或风控约束 |

报告中的 Meta 链路章节只做链路对齐说明，不是另一套推荐排序。标题使用“链路对齐（非推荐排序）”，避免用户误读为独立顺位。

## 13. Trace artifact

每次选股运行会落盘到：

```text
data/agent_traces/<timestamp>-trace-<id>/
```

关键文件：

| 文件 | 作用 |
| --- | --- |
| `seed_pool.json` | L1 seed 来源与合并结果 |
| `seed_facts.json` | 每只 seed 的共享事实包 |
| `candidate_discovery.json` | 四席位候选发现结果 |
| `candidate_evidence.json` / `candidate_evidence.md` | 候选证据包 |
| `single_stock_deep_dive.json` | 逐股深挖结果 |
| `meta_orchestrator.json` | Meta 约束包 |
| `pricing_agent.json` | If-Then 条件单矩阵 |
| `portfolio_allocation.json` | 组合配置和执行排序 |
| `adversarial_review.json` | 反方审查 |
| `judge_decision.json` | Judge 裁决 |
| `final_report.json` | 最终报告结构化输入 |
| `final.md` | 最终 Markdown 报告 |
| `events.ndjson` | 前端 Trace 流事件 |

排障时优先从 `final_report.json`、`portfolio_allocation.json` 和 `events.ndjson` 看报告结构、执行排序和阶段失败。
