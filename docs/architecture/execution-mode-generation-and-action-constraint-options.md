# execution_mode 生成链路与「必须给出动作」的可落点

> 范围：**只查现状、只列选项**。本轮不改代码、不改提示词、不做 git 操作，也不做方案取舍。
> 目的：查清 `execution_mode` 从哪来、为什么模型大量输出 `wait`、`wait` 与 `conditional_open` 的边界在哪，以及如果要加「决策层必须交出一个动作」的约束，可以落在哪些层。
> 数据来源：本地 `data/agent_traces/`（449 个 trace，有效 206 个，127 条计划）；代码与提示词为仓库当前版本。
> 生成日期：2026-11-19
> 关联文档：[selection-pipeline-open-blockers-diagnosis.md](selection-pipeline-open-blockers-diagnosis.md)、[selection-threshold-quantile-calibration.md](selection-threshold-quantile-calibration.md)、[llm-decision-conservatism-external-solutions.md](llm-decision-conservatism-external-solutions.md)

标注约定：**数据事实**可复现；**分析判断**依赖口径，不应当作定论。

---

## 0. 结论速览

**1）`execution_mode` 的唯一直接来源是 `portfolio_allocation` 阶段的 LLM 输出。**

【数据事实】链路是：`candidate_discovery` → `candidate_screening` → `single_stock_deep_dive`（给 `action_bias`）→ `meta_orchestrator` → `pricing_agent`（给 `if_then_order_matrix[].scenarios[].execution_mode`）→ `portfolio_allocation`（给 `positions_plan[].execution_mode`）→ 渲染层的 `_execution_mode()`。
【数据事实】`portfolio_allocation` 的 `positions_plan` schema 把 `execution_mode` 定义为 `immediate_open | conditional_open | strong_watch | plain_wait | reject`（[stock_selection_prompts.py#L708](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L708)），与全局枚举一致（[L30-L39](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L30-L39)）。

**2）prompt 已经要求「每只股票必须给动作」，但没有要求「必须给非 wait 动作」。**

【数据事实】组合规则第 6 条原文：「每只股票必须给动作、首仓比例或不买原因、入场条件、信号有效期、加仓条件、止损条件、复查触发。」（[L666](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L666)）
【数据事实】但第 9 条在 `risk_off / panic` 下强制 `portfolio_action` 为 `wait` 或 `reject`（[L669](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L669)），第 13 条明确允许「所有标的都是 plain_wait/reject」（[L673](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L673)）。
【分析判断】所以「必须给动作」在 prompt 层已经成立，但没有阻止模型把 85% 的计划都写成 `wait`；这不是缺约束，而是约束没有区分「有条件等待」和「无条件观望」。

**3）代码存在默认值，缺字段不会报错，会静默变成 `wait` / `plain_wait`。**

【数据事实】`positions_plan` 的 `action` 缺省为 `"wait"`（[L6219](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6219)），`action_strength` 缺省为 `"weak"`（[L6220](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6220)）；`execution_mode` 缺失时不是报错，而是进入 `_execution_mode()` 的规则链，最终大概率落到 `plain_wait`。
【数据事实】整段 LLM 调用失败时，`_fallback_portfolio_allocation` 用 `selected_scenario.execution_mode`，没有则 `conditional_open if action == "open" else "plain_wait"`（[L4799](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L4799)）。

**4）`wait` 与 `conditional_open` 的边界不在「有没有点位」，而在「强度 + 标签」。**

【数据事实】127 条计划中：`action=wait` 108 条（96 条 `conditional_open` + 12 条 `plain_wait`）、`action=monitor` 15 条（9 条 `conditional_open` + 4 条 `plain_wait` + 2 条 `strong_watch`）、`action=reject` 4 条（3 条 `reject` + 1 条 `conditional_open`）。
【数据事实】96 条 `conditional_open` 全部有止损和复查触发，88 条有数字入场价；12 条 `plain_wait` 中 11 条有止损、12 条有复查触发、8 条有数字入场价。
【数据事实】108 条 `wait` 的强度分布：`weak` 90、`medium` 15、`strong` 1、`none` 2。规则链里的条件入场要求强度至少 `medium`，因此 **90/108 的 wait 即使删掉显式 `execution_mode` 也进不了条件入场**。

**5）`wait` 主要是「现在不买 / 等确认」，不是「明确看空」。**

【数据事实】对 108 条 `wait` 的理由/风险做关键词粗分（**启发式，非人工标注**）：不确定/数据缺口 48、偏看空 26、混合 19、其他 15；明确回避措辞（「回避」「不宜参与」「不推荐执行」等）只有 2 条。
【数据事实】131 条深度结果中 109 条 `action_bias=wait`，这 109 条**全部**带 `ideal_entry_zone` 和 `stop_loss`。
【分析判断】多数 `wait` 并不是「这只票不行」，而是「现在这个价不行 / 证据不够 / 大盘不支持」。这正是「必须交出动作」约束可以发挥作用的地方：把「不买」拆成「有条件等待」和「明确放弃」。

**6）四个可落点的改动面和风险差异很大，推荐顺序见 §5，但本轮不做决定。**

【分析判断】
- 只改 prompt：最便宜，但规则 6 已经存在且 85% 仍输出 wait，说明 prompt 单独不够；要求模型「给触发价」还会诱导模型编价格。
- 加校验层：最贴近现有 `trade_plan_guard` 的「只校验不写回」模式，不改点位生成、不改下游语义，是风险最小的第一落点。
- 拆数据结构：语义最清晰，但会波及枚举、渲染、回测、复盘、Web 类型，改动面最大。
- 改下游消费：能让「带触发的 wait」变成可执行计划，但必须先解决触发价来源，否则会把 AI 编的价格当成买点。

**7）本链路的点位实际由 LLM 产出，确定性 pricing 引擎没有接入。**

【数据事实】`pricing_agent` 是一个 LLM 阶段，失败时走 `_fallback_pricing_agent`；`src/agent/pricing/` 下的确定性点位引擎（`generate_trade_plan` 等）**没有被 `stock_selection.py` 引用**。
【分析判断】这与「数学模型给点位、AI 做建议」的表述有落差。任何「wait 带触发价」的方案，必须显式规定触发价只能从 `if_then_order_matrix[selected_scenario].entry_zone`、深度结果 `entry_quality.ideal_entry_zone / stop_loss` 或已有日线锚点复制，不能由模型新生成；否则会进一步扩大 AI 造点位的范围。

---

## 1. `execution_mode` 从哪里来

### 1.1 完整链路

```
candidate_discovery
  └─ 候选 + signal_score（入池召回分）
candidate_screening
  └─ shortlist[].score（entry_quality_score）
single_stock_deep_dive
  └─ summary.action_bias / action_strength
     full.entry_quality.ideal_entry_zone / stop_loss / failure_condition
meta_orchestrator
  └─ asset_regimes / main_constraints / meta_constraint_packages
pricing_agent（LLM 阶段，失败时 _fallback_pricing_agent）
  └─ if_then_order_matrix[].scenarios[]：scenario_name / action / execution_mode / entry_zone / stop_loss / condition
     if_then_order_matrix[].selected_scenario
portfolio_allocation（LLM 阶段，失败时 _fallback_portfolio_allocation）
  └─ positions_plan[].action / action_strength / execution_mode / entry_condition / stop_loss_condition
_recommendation_items()
  └─ 复制 plan.execution_mode；缺失则调用 _execution_mode()
_execution_mode()
  └─ 最终 decision mode（explicit 优先，其次 reject → actionable → conditional_entry → strong_watch → plain_wait）
下游
  └─ 报告渲染 / execution_recommendations / 排序 / 回测 / 复盘
```

【数据事实】`portfolio_allocation` 的 schema 是 `execution_mode` 的直接落点（[L708](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L708)）；`pricing_agent` 只提供每个场景的 `execution_mode` 和 `selected_scenario`，不决定最终计划模式。

### 1.2 枚举与语义

【数据事实】全局枚举原文（[L30-L39](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L30-L39)）：

```
action_bias = open | wait | reject | monitor
action_strength = strong | medium | weak | none
execution_mode = immediate_open | conditional_open | strong_watch | plain_wait | reject
```

| 值 | 语义 | 代码中的实际用途 |
| --- | --- | --- |
| `immediate_open` | 立即开仓 | `execution_recommendations` 收录；排序最前；回测按 `immediate` 映射 |
| `conditional_open` | 条件触发后开仓 | `execution_recommendations` 收录；排序第 1 档；回测按 `conditional` 映射 |
| `strong_watch` | 强观察，不直接买 | 不进入 `execution_recommendations`；排序第 12 档；回测不可执行 |
| `plain_wait` | 普通等待 | 不进入 `execution_recommendations`；排序最后；回测 `entry_rule=unknown` |
| `reject` | 放弃 | 不进入可执行列表；回测排除 |

【数据事实】`action_bias` 的 prompt 枚举只有 `open | wait | reject | monitor`，但本地 131 条深度结果里有 1 条输出 `conditional_open`，属于 schema 外的值。这说明「模型输出枚举外值」在现有链路里不会被校验拦截。

### 1.3 点位从哪来（与项目体制的关系）

【数据事实】`pricing_agent` 是 LLM 阶段：`build_pricing_agent_prompt()` 输出 JSON；整段失败时 `_fallback_pricing_agent()` 用 `_pricing_action_for_scenario()` 做确定性映射。
【数据事实】`_pricing_action_for_scenario()` 在 `risk_off / panic / trending_down / avoid` 下固定返回 `("monitor", "plain_wait")`；在 Breakout_Continuation / Mean_Reversion_Pullback 下返回 `("wait", "conditional_open")`；其余返回 `("monitor", "plain_wait")`（[L4432-L4440](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L4432-L4440)，调用点在 `_fallback_pricing_agent` [L4727](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L4727)）。
【数据事实】`src/agent/pricing/` 下存在确定性点位引擎（锚点、ATR、tick 对齐等），但 `stock_selection.py` 没有导入它。
【分析判断】因此当前「点位」主要来自 LLM 对工具证据的归纳，而不是确定性数学模型。这是后续任何「触发价」方案必须面对的前提约束。

---

## 2. prompt 的「必须给动作」约束现状

### 2.1 已有约束原文

【数据事实】组合规则第 6 条（[L666](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L666)）：

> 每只股票必须给动作、首仓比例或不买原因、入场条件、信号有效期、加仓条件、止损条件、复查触发。

【数据事实】组合规则第 9 条（[L669](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L669)）：

> 如果 market_regime 为 risk_off / panic，portfolio_action 必须为 wait 或 reject；如果 volatility_bucket 为 extreme 且 regime 不是 trending_up / bull_volatile，不得主动开新仓。

【数据事实】组合规则第 12 条（[L672](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L672)）：

> positions_plan 的 rank 必须按“可执行性”排序：open 且证据完整优先；wait 只有在 execution_mode=conditional_open、动作强度至少 medium、具备明确入场条件和止损/失效条件、且没有强反向证据时才可排在前列；reject/monitor/watch 不得包装成首选。

【数据事实】组合规则第 13 条（[L673](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L673)）：

> 如果所有标的都是 plain_wait/reject，summary.core_reason 必须明确写“本轮没有可直接入手标的”，recommended_position_count 必须为 0；如果存在 conditional_open 或 strong_watch，summary.core_reason 必须写“本轮没有无条件买入标的，但存在可按次日条件触发的强候选”。

【数据事实】组合规则第 17 条（[L677](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L677)）：

> positions_plan.entry_condition / stop_loss_condition 应优先来自 if_then_order_matrix 中选中的场景；如果点位计算层只给 monitor/plain_wait，不得强行升级为 open。

【数据事实】深度分析规则第 12 条（[L232](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L232)）：

> 即使 action_bias=wait，也要尽量给出“不能直接买的原因、次日可触发条件、禁止追高、失效条件”；无法给止损或失效条件时必须保持 weak/none，不得包装成可执行买点。

【数据事实】Meta/点位字段说明（[L48](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L48)）：

> if_then_order_matrix[].selected_scenario：当前更可执行的场景；若其 action 不是 open/conditional_open，不得强行升级。

### 2.2 结论：有「必须给动作」，没有「必须给可执行动作」

【分析判断】
1. 规则 6 已经堵住了「漏给动作」；模型不能交白卷，只能交 `wait`。
2. 规则 9 和 13 又合法化了「全部 wait」，所以「必须给动作」在弱市里退化为「必须写 wait」。
3. 真正缺的不是「动作字段」，而是 `wait` 的**可机检语义**：是有触发条件的等待，还是明确放弃。

### 2.3 缺字段时会发生什么

【数据事实】
- `action` 缺失 → `"wait"`（[L6219](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6219)）。
- `action_strength` 缺失 → `"weak"`（[L6220](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6220)）。
- `execution_mode` 缺失 → `_execution_mode()` 规则链；由于 `weak` 强度，最终大概率 `plain_wait`（见 §3）。
- 整段 LLM 失败 → `_fallback_portfolio_allocation` 用场景模式，没有则 `plain_wait`（[L4799](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L4799)）。
- 阶段 JSON 解析只校验能否 parse，不校验必填字段；没有 schema validation 会拦住缺失字段。

【数据事实】本地 127/127 计划都有 `execution_mode`，所以「缺字段 → 兜底」路径在这批样本里没有被触发。但不能据此认为模型不会漏字段，只能说明这批运行没漏。

---

## 3. 短路逻辑：完整判断链

### 3.1 计划字段如何进入推荐项

【数据事实】`_recommendation_items()`（定义在 [L6135](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6135)，计划复制段在 [L6214-L6238](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6214-L6238)）：

```python
target.update({
    "action": plan.get("action") or target.get("action") or "wait",
    "action_strength": plan.get("action_strength") or target.get("action_strength") or "weak",
    "execution_mode": plan.get("execution_mode") or target.get("execution_mode"),
    ...
})
items = list(by_code.values())
for item in items:
    item["execution_mode"] = _execution_mode(item)
```

即：计划里的 `execution_mode` 被原样带入；然后每个 item 再跑一次 `_execution_mode()`。由于 item 此时已带显式值，`_execution_mode()` 第一层就会返回它。

### 3.2 `_execution_mode()` 判断链

【数据事实】函数位于 [L6388-L6401](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6388-L6401)，等价伪代码如下：

```
def _execution_mode(item):
    1. explicit = item.execution_mode（小写化）
       若 explicit ∈ {immediate_open, conditional_open, strong_watch, plain_wait, reject}
       → 直接返回 explicit                      # 第一层，127/127 命中
    2. 若 action ∈ {reject, avoid}
       → 返回 "reject"
    3. 若 _is_actionable_recommendation(item)
       → action ∈ {open, buy} 返回 "immediate_open"
       → action == wait 返回 "conditional_open"
    4. 若 _is_conditional_entry_item(item)
       → 返回 "conditional_open"
    5. 若 _is_strong_watch_item(item)
       → 返回 "strong_watch"
    6. 返回 "plain_wait"
```

各分支的精确条件：

| 分支 | 条件 | 返回 |
| --- | --- | --- |
| 1 | 显式 `execution_mode` 在允许枚举内 | 原值 |
| 2 | `action in {reject, avoid}` | `reject` |
| 3 | `action in {open, buy}` 且有 plan 或 deep dive | `immediate_open` |
| 3 | `action == wait` 且有 plan + deep dive + 强度 ≥ medium + 有入场 + 有止损 + 风险里没有强势空头/资金流出/数据过期/基本面缺失 | `conditional_open` |
| 4 | `action == wait` + 强度 ≥ medium + 有入场 + 有退出 + 无硬性利空 + (`entry_quality_score >= 88` 或 (`>= 85` 且有共识)) | `conditional_open` |
| 5 | `action not in {reject, avoid}` + 无硬性利空 + (`recall_score >= 85` 或有共识) | `strong_watch` |
| 6 | 其余 | `plain_wait` |

【数据事实】`_is_actionable_recommendation()` 在 [L6284-L6303](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6284-L6303)；`_is_conditional_entry_item()` 在 [L6358-L6374](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6358-L6374)；`_is_strong_watch_item()` 在 [L6377-L6385](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6377-L6385)。

### 3.3 关键事实：规则链几乎不参与

【数据事实】127/127 计划都命中第 1 层，阈值和规则链没有参与最终模式判定。
【数据事实】108 条 `wait` 中 90 条强度是 `weak`，即使删除显式 `execution_mode`，第 3/4 层也会因为强度不足而失败，最终落到 `plain_wait` 或 `strong_watch`。
【分析判断】「模型输出 wait」不是代码拒绝，也不是阈值拒绝，而是模型自己写了 `wait/conditional_open/plain_wait`，代码照单全收。

### 3.4 下游怎么用

| 消费点 | 行为 | 位置 |
| --- | --- | --- |
| 可执行推荐列表 | 只收 `immediate_open` / `conditional_open` | [L861-L865](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L861-L865) |
| 观察池 | 没有 deep dive 且不是可执行模式的 item | [L870-L873](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L870-L873) |
| 排序 | `immediate_open` 最前，`conditional_open` 第 1 档，`strong_watch` 第 12 档，可观察项 20 + 动作档，其余第 99 档 | [L6271-L6281](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6271-L6281) |
| 报告文案 | `plain_wait` 显示为「等待」，`conditional_open` 显示为「条件触发后试仓」 | [L7700-L7703](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L7700-L7703) |
| 回测可执行性 | `execution_mode` 不含 `conditional` 且不在 `ACTIONABLE_ENTRY_ACTIONS` 时排除 | [agent_entry_execution_backtest_service.py#L1429-L1440](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/services/agent_entry_execution_backtest_service.py#L1429-L1440) |
| 回测 entry_rule | 含 break→breakout、含 immediate→immediate、含 conditional→conditional、含 pullback→pullback，其余→unknown | [L1287-L1297](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/services/agent_entry_execution_backtest_service.py#L1287-L1297) |
| 复盘动作 | 依次取 `plan.action` → `plan.execution_mode` → `judge.final_action`，默认 `wait` | [agent_verdict_review_service.py#L966-L970](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/services/agent_verdict_review_service.py#L966-L970) |

【分析判断】`plain_wait` 与 `conditional_open` 在下游是两个世界：一个进观察池、不参与回测；一个进可执行列表、参与回测和排序。这是「wait 标签」影响最大的地方。

---

## 4. wait 与 conditional_open 的边界

### 4.1 数量与字段完整性

【数据事实】127 条计划：

| action + 模式 | 条数 | 有止损 | 有复查触发 | 有数字入场价 |
| --- | --- | --- | --- | --- |
| `wait` + `conditional_open` | 96 | 96/96 | 96/96 | 88/96 |
| `wait` + `plain_wait` | 12 | 11/12 | 12/12 | 8/12 |
| `monitor` + 任意模式 | 15（9 cond / 4 plain / 2 strong） | — | — | — |
| `reject` + 任意模式 | 4（3 reject / 1 conditional） | — | — | — |

【数据事实】`wait` 的强度分布：`weak` 90、`medium` 15、`strong` 1、`none` 2。

【分析判断】边界不在「有没有点位」：
- 96 条 `conditional_open` 全部有点位；
- 12 条 `plain_wait` 里也有 11 条有止损、8 条有数字入场价；
- 真正的差别是**模型给的标签和强度**，不是数据完备性。

### 4.2 语义粗分（启发式）

【数据事实】对 108 条 `wait` 的 `reason + risk_flags` 做关键词分类：

| 类别 | 条数 | 典型措辞 |
| --- | --- | --- |
| 不确定 / 数据缺口 | 48 | 「数据缺失」「无法精确定价」「risk_off」「证据不足」 |
| 偏看空 | 26 | 「技术空头」「资金净流出」「趋势走弱」「破位」 |
| 混合 | 19 | 同时出现两类关键词 |
| 其他 | 15 | 无明确关键词或模板化 |

【数据事实】明确回避措辞（「回避」「不宜参与」「不推荐执行」等）只有 2 条。
【数据事实】18 条 `wait` 的 reason 是完全模板化的兜底文案（「价格回落至均值回归锚点附近且未破前低…」），不是模型针对个股写的判断。

【分析判断】多数 `wait` 是「现在不买 / 等确认 / 数据不够」，不是「这只票明确不行」。但关键词分类是启发式，不能替代人工标注；「偏看空 26 条」这个数字只能当量级参考。

### 4.3 深度层已经有点位

【数据事实】131 条深度结果中 109 条 `action_bias=wait`；这 109 条**全部**有 `ideal_entry_zone` 和 `stop_loss`。
【分析判断】「wait」在深度层并不等于「没有买点」，而是「不建议现在执行，但结构上存在可观察的入场区」。这为「把 wait 变成带触发的条件计划」提供了现成数据，但也意味着不能把「有入场区」误读成「应该买」。

### 4.4 边界结论

【分析判断】当前的 `wait` 是一个语义过载的标签，至少混合了四种情况：
1. 有条件等待（有触发价、有失效价，只是模型选了 `plain_wait`）；
2. 证据不足（数据缺口，应该标记为「需要补数据」而不是「看空」）；
3. 大盘/账户约束（risk_off、现金缺失，属于组合层限制，不是个股结论）；
4. 明确放弃（真正看空或不符合账户）。

第 1、3 类不应被当成「决策层没给动作」；第 2、4 类才是「没有可执行结论」。如果不拆开，任何「必须给动作」的约束都会被迫在这四类之间做错误归并。

---

## 5. 「必须给出动作」的四个可落点

### 5.1 方案 A：改 prompt 措辞

**做法**：在 `build_portfolio_allocation_prompt` 和 `build_deep_dive_prompt` 里加约束：
- `wait` 必须同时给出 `trigger_condition`（可机检的价格或事件）和 `invalidation_condition`；
- 无法给出触发/失效条件的，不得用 `wait`，必须用 `reject` 或 `no_trade`（若新增枚举）；
- 所有触发价必须从 `if_then_order_matrix[selected_scenario].entry_zone` 或 `entry_quality.ideal_entry_zone` 复制，不得自行生成。

**改动面**：
- `src/agent/stock_selection_prompts.py`：组合规则第 6/12/13 条、深度规则第 12 条、schema 注释。
- 不改代码逻辑；不改枚举（除非同时做方案 B）。

**影响环节**：所有依赖 `execution_mode` 的下游（因为模型输出会变），但代码行为不变。

**风险**：
- 【数据事实】规则 6 已经要求「必须给动作」，结果仍是 108/127 wait，说明 prompt-only 约束的边际效果有限。
- 要求模型给价格，会诱导模型编造点位，直接触碰「AI 不得产生点位」的红线。
- 无法验证模型是否遵守；没有校验层就只是文案变化。
- 会改变 prompt 版本，进而改变所有阶段的分数分布（参见阈值标定文档的漂移问题）。

**外部依据**：已有调研文档整理了「Decision Paralysis」「Know When to Abstain」「TradingAgents 的 HOLD 动作」等做法；共识是「允许弃权，但弃权必须有明确条件和理由」，而不是「强制买入」。见 [llm-decision-conservatism-external-solutions.md](llm-decision-conservatism-external-solutions.md) 的来源清单（本轮未新增外部检索）。

**体制检查**：如果 prompt 只要求「复制已有场景的触发价」，不要求模型生成价格，则不触碰红线；如果要求模型「给出一个合理触发价」，则触碰。

---

### 5.2 方案 B：改数据结构，拆分 wait

**做法**：把 `plain_wait` 拆成两个值，或新增字段：
- 方案 B1（改枚举）：`plain_wait` → `conditional_wait`（有触发+失效） + `no_trade`（明确放弃，必须给理由类别）。
- 方案 B2（加字段，不改枚举）：保留 `plain_wait`，新增 `wait_reason`（`data_gap | regime_constraint | no_setup | bearish`）和 `trigger_source`（`pricing_scenario | entry_quality | none`）。

**改动面**：
- `src/agent/stock_selection_prompts.py`：`COMMON_ENUMS`（[L34](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L34)）、`positions_plan` schema（[L708](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L708)）、规则 12/13。
- `src/agent/stock_selection.py`：`_execution_mode()` 的显式枚举集合（[L6389](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6389)）、`_recommendation_sort_rank()`（[L6271](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6271)）、渲染标签（[L7700-L7703](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L7700-L7703)）、`_signal_validity_text`。
- `src/services/agent_entry_execution_backtest_service.py`：`_is_actionable_entry_item()`、`_infer_entry_rule()`。
- `src/services/agent_verdict_review_service.py`：`_resolve_symbol_action()` 与统计口径。
- `apps/dsa-web/` / `apps/dsa-desktop/`：如果前端按枚举渲染 badge/筛选，需要同步类型与文案。
- `tests/`：所有断言枚举的用例。

**影响环节**：报告、回测、复盘、Web/Desktop 展示，以及历史 trace 的可比性。

**风险**：
- 改动面最大，容易漏掉某个消费方；旧 trace 没有新字段，需要兼容默认值。
- 枚举扩张会让「模式」再次过载；如果 `no_trade` 和 `reject` 语义不清，会重复现在的问题。
- 需要一次数据迁移/兼容策略（旧 `plain_wait` 映射到哪个新值）。

**外部依据**：外部实践普遍区分「HOLD / 观望」与「NO ACTION / 不参与」，但具体命名没有统一标准；本项目的 `wait / monitor / reject` 已经接近这个三分，只是 `wait` 内部没有区分「有条件」和「无条件」。

**体制检查**：不改点位来源，只改标签与字段，安全；但必须定义 `trigger_source`，否则新字段仍可能被模型编造。

---

### 5.3 方案 C：加校验层（只校验不写回）

**做法**：新增 `src/agent/execution_plan_guard.py`（或扩展现有 `trade_plan_guard.py`，但它当前不读 `execution_mode`），在最终报告落盘前做一致性校验，输出 stage 级 `execution_mode_validation`：
- `wait + plain_wait` 且没有可机检触发价或失效价 → `needs_review`；
- `wait + strength >= medium + 有入场 + 有止损` 但 `execution_mode=plain_wait` → `needs_review`（标签与证据不一致）；
- `entry_condition` 有数字价格，但 `trigger_source` 无法对应到 `if_then_order_matrix` 或 `entry_quality` → `needs_review`（疑似模型自造点位）；
- 全部 `wait` 且 `core_reason` 已写明「本轮没有可直接入手标的」→ `ok`（这是合法的无交易结论）；
- 全部 `wait` 且没有无交易理由 → `needs_review`。

**改动面**：
- 新增一个校验模块（纯函数，输入 plan + pricing matrix + deep dive，输出 findings）。
- `src/agent/stock_selection.py`：在报告组装处调用，把结果写入 stage 级字段；不修改 plan、不改 `execution_mode`、不改点位。
- `tests/`：校验规则的单测。
- 报告渲染：可选展示 `needs_review` 标记。

**影响环节**：只增加诊断字段和标记；下游执行/回测逻辑不变。

**风险**：
- 误报：很多 `entry_condition` 是定性描述（「回踩 MA20 企稳」），无法机检；需要定义「可机检」的边界，否则会大面积 `needs_review`。
- 校验层只能暴露问题，不能提升动作质量；如果没人消费 `needs_review`，等于没做。
- 需要明确 `trigger_source` 的判定规则，否则「疑似自造点位」会变成新的争议点。

**外部依据**：仓库已有 `trade_plan_guard` 的「只校验、不写回、`advisory_only`」先例；外部 guardrail/validator 模式也支持「生成与校验分离」。这是与现有架构最一致的做法。

**体制检查**：不改点位生成、不改 AI 职责，最安全。

---

### 5.4 方案 D：改下游消费，把带触发的 wait 当成可执行输出

**做法**：
- 在 `execution_recommendations` 里，把「`wait` + 有触发价 + 有失效价 + 强度 ≥ medium + 触发价来源合法」的 item 纳入「条件计划」；
- 回测把这类 item 映射为 `conditional`；
- 报告里明确标注「条件触发，不是当前买入」。

**改动面**：
- `src/agent/stock_selection.py`：`execution_recommendations` 过滤（[L861-L865](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L861-L865)）、`_recommendation_sort_rank()`、`_signal_validity_text()`、报告小节。
- `src/services/agent_entry_execution_backtest_service.py`：`_is_actionable_entry_item()`、`_infer_entry_rule()`。
- `src/services/agent_verdict_review_service.py`：动作统计。
- 测试与前端筛选。

**影响环节**：报告可执行列表、回测样本量、复盘口径。

**风险**：
- 把「观察」变成「可执行」，会改变用户对报告的解读；必须有清晰的「条件触发」标识。
- 如果触发价来自模型而不是已有场景，等于让 AI 造点位，直接违反体制。
- 回测样本量会变大，历史对比口径断裂。
- 可能与 `reject` 语义冲突：部分 `wait` 实际是「明确放弃」，不应被升级。

**外部依据**：If-Then 条件单、条件触发型交易计划是成熟做法；TradingAgents 等工作也把 HOLD 与 BUY/SELL 并列，但不会把 HOLD 直接当市价单执行。

**体制检查**：只有在触发价来源被强制约束到 `if_then_order_matrix` / `entry_quality` / 日线锚点时，才不触碰红线；否则不应做。

---

### 5.5 四个方案对比

| 维度 | A 改 prompt | B 改数据结构 | C 加校验层 | D 改下游消费 |
| --- | --- | --- | --- | --- |
| 改动文件数 | 1 | 6+（含前端） | 2-3 | 3-4 |
| 是否改枚举 | 否（除非 B） | 是 / 或加字段 | 否 | 否 |
| 是否改下游语义 | 间接 | 是 | 否 | 是 |
| 是否可能诱导模型造点位 | 高（若要求给价） | 中（若加 trigger_source 则低） | 低 | 中 |
| 是否可回滚 | 容易 | 难（历史兼容） | 容易 | 中 |
| 能否立刻发现问题 | 不能 | 不能 | 能 | 能（但会改变口径） |
| 与现有 guard 先例一致 | 否 | 否 | 是 | 否 |

【分析判断】如果只选一个起点，**方案 C 的风险最低、最快能拿到证据**；方案 A 可以作为 C 的配套（只写「必须复制来源字段」，不要求模型生成价格）；方案 B 适合在 C 运行一段时间、确认问题分布后再做；方案 D 必须等触发价来源被 C 校验住之后再考虑。

【分析判断】不建议只做方案 A：规则 6 已经证明「写了必须给动作」不等于「会给可执行动作」，没有校验层就无法知道模型是否遵守。

---

## 6. 样本量与结论强度

【数据事实】
- 127 条计划、42 次运行、2026-05-30 ~ 2026-11-16，单一市场环境。
- 108 条 `wait`；96 条 `conditional_open`、12 条 `plain_wait`。
- 关键词语义分类是启发式，没有人工标注；18 条是模板化兜底文案。
- 127/127 都有 `execution_mode`，所以缺字段兜底路径未被样本覆盖。
- 深度层 109 条 wait 全部有点位，说明「wait 无点位」不成立。

【分析判断】
- 「wait 占 85%」「90/108 强度 weak」「96/96 conditional_open 有止损」是强事实（可直接复算）。
- 「wait 主要是等待不是看空」是中等强度判断（依赖关键词分类）。
- 「加校验层能改善决策质量」是**未经验证的假设**：本轮没有做 A/B，也没有验证 `needs_review` 与后续收益的关系。

---

## 7. 复现方式与未验证项

### 7.1 复现口径

1. 遍历 `data/agent_traces/*/request.json`，保留 `context.candidate_discovery_mode == "thesis_desk_committee"`。
2. 计划字段：`portfolio_allocation.json -> full.positions_plan[]` 的 `action`、`execution_mode`、`action_strength`、`entry_condition`、`stop_loss_condition`、`review_trigger`、`risk_flags`、`reason`。
3. 深度字段：`single_stock_deep_dive.json -> full.results[].summary.action_bias`、`full.entry_quality.ideal_entry_zone / stop_loss`。
4. 点位场景：`pricing_agent.json -> full.if_then_order_matrix[].scenarios[] / selected_scenario`。
5. 语义粗分：对 `reason + risk_flags` 做关键词匹配（不确定/数据缺口、偏看空、混合、其他），不是模型分类。
6. 去重：无（计划按原始 127 条统计；深度按 131 条统计）。

### 7.2 未验证项

- 未验证模型在新 prompt 下的实际输出分布（本轮不改 prompt）。
- 未验证 `entry_condition` 中的数字是否真的来自 `if_then_order_matrix`，还是模型自行生成（只做了字段存在性统计）。
- 未验证「needs_review 标记」与后续收益/回撤的关系。
- 未做 `wait` 与 `conditional_open` 的前向收益对照；阈值标定文档已给出部分相关证据，但不能直接替代。
- 未检查 Web/Desktop 是否按 `execution_mode` 做筛选；如果做方案 B/D，需要单独排查前端类型与展示。
- 未验证 `monitor` 与 `plain_wait` 的语义差异是否被下游正确使用。

### 7.3 本轮边界

- 未修改 `src/agent/stock_selection.py`、`src/agent/stock_selection_prompts.py` 或任何配置。
- 未修改枚举、schema、校验层、下游消费。
- 未执行 `git add / commit / tag / push`。
