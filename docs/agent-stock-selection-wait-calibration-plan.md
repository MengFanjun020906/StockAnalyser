# Agent 选股 wait 过度保守校准方案

本文档用于审阅“选股报告大量输出 `wait`，但次日强势候选继续大涨”这一问题的改造方案。目标不是把 `wait` 粗暴改成 `open`，而是让系统在风险边界内输出更有看盘价值的条件型动作。

## 背景

当前选股报告里经常出现类似结果：

```text
入场结论：wait
动作强度：weak
理想入场区间：当前不形成可执行买点，仅保留观察条件
首仓比例：0%
止损位：未触发建仓，不设置交易止损
目标位：-
```

这种输出在严格风控意义上并不一定错，因为 `wait` 代表当前不形成确定交易计划。但从日常看盘决策角度看，它有三个明显问题：

- 强候选和普通观察股看起来没有区别。
- 晚间复盘场景下，报告没有给出“明天什么条件可以上”的执行脚本。
- `候选分 100` 与 `wait weak` 并列展示时，会让用户感觉系统自相矛盾。

## 当前保守来源

当前偏保守主要来自三层规则。

### 1. 组合配置 Prompt 规则

位置：`src/agent/stock_selection_prompts.py`

关键约束：

- 首仓必须保守，除非行情、技术、基本面、消息和资金证据均明确支持。
- 休市、资金面缺失、消息面缺失或行情时效不明时，只能给条件型计划。
- 账户摘要为空或可用现金缺失时，`portfolio_action` 不得为 `open`。
- `wait` 只有动作强度至少 `medium`、有入场条件和止损条件、且没有强反向证据时才可排前。
- 如果所有候选都是 `wait/reject`，或 `wait` 标的是 `weak/none`，必须写“本轮没有可直接入手标的”。

这些规则保护了系统不乱给开仓，但也容易把强势延续机会压成弱等待。

### 2. Judge 裁决 Prompt 规则

位置：`src/agent/stock_selection_prompts.py`

关键约束：

- 证据缺口较多时最终动作优先用 `wait` 或 `monitor`。
- 价格高于 `no_chase_line` 且没有回踩确认时必须降级为 `wait`。
- 账户约束缺失时最终动作不得为 `open`。

这些规则本身合理，但缺少“条件型入场”这个中间表达，导致所有不满足直接开仓的强候选都落入普通 `wait`。

### 3. 报告渲染规则

位置：`src/agent/stock_selection.py`

当前 `_is_actionable_recommendation()` 只把两类内容视为可执行：

- `open/buy` 且有计划或深挖结果。
- `wait` 且同时满足：有计划、有深挖、动作强度不是 `weak/none`、有入场条件、有止损条件、没有强反向风险。

但 `_render_recommendation_block()` 对非 actionable 的 `wait` 会统一渲染成：

- `⏳ 等待`
- 首仓 `0%`
- 不给目标位
- 不给止损交易计划
- 只写泛化复查触发

这就是用户看到“好像什么信息都没有”的直接原因。

## 设计目标

1. 保留风险边界，不把证据不足的股票直接升级为 `open`。
2. 让强候选输出明确的次日看盘脚本。
3. 区分“弱等待”和“条件入场”。
4. 让 `候选分`、`动作强度`、`买入可执行性` 三个概念分开显示。
5. 晚间复盘时，报告重点回答“明天什么条件触发、什么情况不追、什么情况作废”。

## 非目标

- 不承诺收益。
- 不把所有高分候选都升级为买入。
- 不绕过账户约束、仓位约束和硬风险约束。
- 不在缺少止损或失效条件时输出确定 `open`。

## 核心方案

### 1. 保持底层动作枚举稳定

短期不建议直接新增顶层 `action=conditional_open`，因为当前链路、测试和前端都围绕：

```text
open | wait | reject | monitor
```

建议先使用派生语义：

```json
{
  "action": "wait",
  "action_strength": "medium",
  "execution_mode": "conditional_open",
  "entry_condition": "明日竞价强承接且开盘后不破分时均线，可小仓试探",
  "stop_loss_condition": "跌破前一交易日低点或关键均线即退出观察",
  "no_chase_line": "高开超过 5% 且无回踩不追"
}
```

这样不破坏现有 schema，同时能在报告层把它展示成“条件入场”。

#### Schema 落点

短期分两步处理，避免一次性改动过大。

第一步只做报告层派生，不要求 LLM 输出 `execution_mode`：

- `single_stock_deep_dive.full.entry_quality` 已存在，可继续作为入场质量与风控条件来源。
- `portfolio_allocation.full.positions_plan[]` 已存在，可继续作为最终报告的逐股计划来源。
- 报告层在 `_collect_recommendation_items()` 合并 deep dive 与 positions plan 后，根据 `action/action_strength/entry_condition/stop_loss_condition/failure_conditions/risk_flags/candidate_score` 派生 `execution_mode`。
- 派生字段只在内存中的 recommendation item 使用，不写入外部 API 契约。

第二步再让 LLM 稳定输出 `execution_mode`：

- 推荐加在 `portfolio_allocation.full.positions_plan[]`，因为它表示“账户约束下的最终执行形态”，比单股深挖更接近最终报告语义。
- `single_stock_deep_dive.full.entry_quality` 只补充触发条件字段，例如 `auction_trigger/pullback_trigger/breakout_trigger/failure_condition`。
- 如果后续需要落 schema，建议新增轻量枚举：`execution_mode = immediate_open | conditional_open | strong_watch | plain_wait | reject`，但不替代现有 `action`。

#### 前端兼容性

第一步只改 Markdown 报告渲染，Web/Desktop 不需要立即同步识别新字段。

第二步如果 API 返回 `execution_mode`，前端建议同步增强：

- Agent Trace 候选决策榜可把 `conditional_open` 显示为“条件入场”。
- Candidate Pool 页可把 `strong_watch` 显示为“强观察”。
- Desktop 若只渲染 Markdown，无需额外适配；若解析结构化 JSON 展示建议动作，则需要同步映射。

兼容原则：

- 老前端看不到 `execution_mode` 时仍按 `action` 展示。
- 新前端优先显示 `execution_mode`，但不改变底层 `action`。

### 2. 将 wait 拆成三类

| 内部状态 | 判断条件 | 展示动作 | 含义 |
| --- | --- | --- | --- |
| `conditional_open` | `wait + medium/strong + 有入场条件 + 有止损/失效条件 + 无硬反证` | `⚡ 条件入场` | 不是现在无脑买，而是明天满足条件可试探 |
| `strong_watch` | 高候选分或多专家共振，但缺少止损/资金/消息关键证据 | `👀 强观察` | 值得盯盘，但暂不形成交易脚本 |
| `plain_wait` | `wait weak/none` 或缺口较多 | `⏳ 等待确认` | 当前没有足够可执行价值 |

#### 阈值定义

第一版建议使用可配置阈值，而不是写死在 prompt 里。

建议默认值：

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `AGENT_CONDITIONAL_ENTRY_SCORE_MIN` | `88` | 候选分达到该值且证据条件满足时，允许从普通 `wait` 派生为 `conditional_open` |
| `AGENT_STRONG_WATCH_SCORE_MIN` | `85` | 候选分达到该值但缺关键交易条件时，允许展示为 `strong_watch` |
| `AGENT_CONDITIONAL_ENTRY_MIN_STRENGTH` | `medium` | 条件入场最低动作强度 |
| `AGENT_NO_CHASE_PCT_DEFAULT` | `6` | 没有明确工具价格线时，生成禁止追高条件的默认参考上限 |

`candidate_score` 仍然只表示“入池召回分”。它不能单独决定 `conditional_open`，必须同时满足交易条件。

`conditional_open` 第一版硬条件：

- `action == wait`
- `action_strength in {medium, strong}`
- `candidate_score >= AGENT_CONDITIONAL_ENTRY_SCORE_MIN`，或多专家/多来源共振且分数不低于 `AGENT_STRONG_WATCH_SCORE_MIN`
- 有 `entry_condition` 或 `ideal_entry_zone`
- 有 `stop_loss_condition`、`stop_loss` 或 `failure_conditions`
- 不存在硬反证

`strong_watch` 第一版条件：

- `candidate_score >= AGENT_STRONG_WATCH_SCORE_MIN`，或存在多专家/多来源共振
- 不满足 `conditional_open` 的完整条件
- 不存在硬排除或明确重大风险

#### 硬反证定义

不要交给 LLM 自由判断。第一版用规则层枚举匹配，后续再把 EvidenceCard 的 `severity=veto` 接入。

建议硬反证关键词/结构化来源：

- 硬排除：ST、*ST、退市风险、停牌、不可成交、名称代码不一致。
- 趋势风险：强势空头、跌破关键支撑、跌破止损位、破位放量下跌。
- 资金风险：主力净流出、资金持续净流出、龙虎榜明显卖出主导。
- 消息风险：监管处罚、业绩预警、重大减持、债务/流动性风险、重大诉讼。
- 数据风险：行情严重过期、关键价格缺失、股票身份无法确认。
- 市场风险：`market_regime in {risk_off, panic}` 或 `volatility_bucket == extreme`。

命中硬反证时：

- 不得派生 `conditional_open`。
- 只有仍值得跟踪时才允许 `strong_watch`。
- 硬排除类直接保持 `reject/avoid`。

#### 禁止追高线

`5%/6%` 不应作为永久固定值。第一版规则：

- 如果 deep dive 或工具已经给出 `no_chase_line`，优先使用工具/模型给出的价格或条件。
- 如果没有 `no_chase_line`，报告层只生成文字条件，不生成精确价格，例如“高开超过默认阈值且无回踩不追”。
- 默认阈值用 `AGENT_NO_CHASE_PCT_DEFAULT=6`，仅作为兜底文案参考。
- 后续可按波动率动态调整：低波动 3%-4%，普通 5%-6%，高波动或题材强势 7%-10%，但必须在文档和测试中单独定义。

### 3. 报告标题改造

当前：

```text
⏳ 等待 1：688266 泽璟制药-U（100分）
```

建议：

```text
⚡ 条件入场 1：688266 泽璟制药-U（候选分 100）
```

或：

```text
👀 强观察 1：688266 泽璟制药-U（候选分 100）
```

关键是把 `100分` 明确改成 `候选分 100`，避免被理解成“买入确定性 100 分”。

### 4. 报告表格改造

对 `conditional_open` 不再输出空交易表，而输出看盘执行表。

建议字段：

| 项目 | 决策 |
| --- | --- |
| 看盘动作 | 条件入场 |
| 动作强度 | medium / strong |
| 行情口径 | 收盘后 / 盘中 / 最近交易日 |
| 明日触发条件 | 竞价强承接、放量突破、回踩不破、板块同步等 |
| 可试探仓位 | 例如 5%-10%，账户缺失时写“需按账户约束确认” |
| 禁止追高 | 高开过多、缩量冲高、板块分化、跌破分时均线等 |
| 失效条件 | 跌破关键支撑、资金转弱、利空确认、板块退潮等 |
| 加仓条件 | 首仓盈利后、量价继续确认、回踩不破等 |
| 复查触发 | 次日竞价、开盘 15 分钟、盘后复查 |

对 `plain_wait` 才使用“当前不形成可执行买点”。

#### entry_quality 字段缺失处理

`entry_quality` 不要求一次性全输出，但要分级处理，不能默认补空字符串后继续当成可执行计划。

建议降级规则：

| 缺失情况 | 处理方式 |
| --- | --- |
| 只有 `auction_trigger` 或 `pullback_trigger` 缺失，但有完整入场条件和止损 | 仍可保留 `conditional_open`，用其他触发条件补位 |
| `entry_condition`、`stop_loss`、`failure_condition` 三者中任意一个缺失 | 降级为 `strong_watch`，不标记为 `conditional_open` |
| `no_chase_line` 缺失，但有其它明确条件 | 允许保留观察脚本，禁止给出具体追买价格 |
| 连续多个关键字段缺失，且 action_strength 为 weak/none | 保持 `plain_wait` |

默认值策略：

- 不建议默认填“看起来完整”的交易字段。
- 可以默认填“只保留观察条件”或“未形成可执行买点”这类保守描述。
- 只要缺的是交易硬条件，就优先降级，不优先补空。

### 5. Prompt 规则改造

#### 组合配置阶段

把现有规则：

```text
账户摘要为空或可用现金缺失时，portfolio_action 不得为 open。
```

保留，但补充：

```text
账户摘要为空或可用现金缺失时，不得输出确定 open 仓位；但如果候选具备强势延续条件、明确触发条件和失效条件，可以输出 wait + execution_mode=conditional_open，并将仓位写为“需按账户约束确认”或保守试探区间。
```

把现有规则：

```text
如果所有深度分析标的都是 wait/reject，或 wait 标的 action_strength 为 weak/none，summary.core_reason 必须明确写“本轮没有可直接入手标的”。
```

调整为：

```text
如果所有标的都是 plain_wait/reject，summary.core_reason 写“本轮没有可直接入手标的”。如果存在 conditional_open 或 strong_watch，summary.core_reason 必须写“本轮没有无条件买入标的，但存在可按次日条件触发的强候选”。
```

#### 单股深挖阶段

补充字段或要求：

```json
{
  "entry_quality": {
    "ideal_entry_zone": "",
    "secondary_entry_zone": "",
    "auction_trigger": "",
    "breakout_trigger": "",
    "pullback_trigger": "",
    "no_chase_line": "",
    "stop_loss": "",
    "failure_condition": ""
  }
}
```

即使最终 `action_bias=wait`，也要求尽量输出：

- 为什么不能直接买。
- 哪些条件满足后可以试探。
- 哪些条件出现必须放弃。

### 6. 排序规则改造

建议新增辅助函数：

```python
def _is_conditional_entry_item(item):
    return (
        action == "wait"
        and action_strength in {"medium", "strong"}
        and has entry_condition
        and has stop_loss_condition or failure_conditions
        and no hard bearish risk
    )
```

排序优先级：

```text
1. open
2. conditional_open
3. strong_watch
4. plain_wait
5. monitor
6. reject
```

这样强候选不会被普通观察股淹没。

## 示例输出

### 条件入场样例

```markdown
### ⚡ 条件入场 1：688266 泽璟制药-U（候选分 100）

| 项目 | 决策 |
| --- | --- |
| 看盘动作 | 条件入场，不是无条件追买 |
| 动作强度 | medium |
| 行情口径 | 收盘后数据，等待次日竞价确认 |
| 明日触发条件 | 竞价高开但不超过 4%，开盘 15 分钟放量且不破分时均线，可小仓试探 |
| 可试探仓位 | 5%-10%，需受账户单票上限约束 |
| 禁止追高 | 高开超过 6% 且无回踩，或缩量冲高，不追 |
| 失效条件 | 跌破前一交易日低点 / 板块退潮 / 资金明显转弱 |
| 加仓条件 | 首仓后放量站稳压力位，且板块同步走强 |
| 复查触发 | 次日竞价、开盘 15 分钟、收盘后各复查一次 |

入选理由：
- 候选分高，说明多源召回或多维证据强。
- 当前不直接 open，是因为缺少次日承接和可复盘买点确认。
```

### 强观察样例

```markdown
### 👀 强观察 1：688266 泽璟制药-U（候选分 100）

| 项目 | 决策 |
| --- | --- |
| 看盘动作 | 强观察 |
| 动作强度 | medium |
| 不能直接入场原因 | 缺少资金确认或止损条件不足 |
| 明日重点观察 | 竞价承接、板块强度、回踩支撑、成交量 |
| 触发升级条件 | 补足资金承接并形成明确止损后，可升级为条件入场 |
| 作废条件 | 高开低走、板块分化、资金净流出扩大 |
```

## 实施步骤

### 第一步：报告层先改

文件：

- `src/agent/stock_selection.py`

改动：

- 新增 `_is_conditional_entry_item()`。
- 新增 `_is_strong_watch_item()`。
- 修改 `_recommendation_sort_rank()`。
- 修改 `_render_recommendation_block()`。
- 将标题中的 `（100分）` 改为 `（候选分 100）`。
- 对 `conditional_open` 输出看盘执行表。

优点：

- 风险最小。
- 不改 LLM schema。
- 能立刻改善报告可读性。

### 第二步：Prompt 增强

文件：

- `src/agent/stock_selection_prompts.py`

改动：

- 在单股深挖 prompt 中要求输出 `auction_trigger`、`pullback_trigger`、`breakout_trigger`、`failure_condition`。
- 在组合配置 prompt 中明确允许 `wait + conditional_open` 语义。
- 在 Judge prompt 中要求区分“无条件买入失败”和“条件入场成立”。

### 第三步：测试覆盖

文件：

- `tests/test_agent_stock_selection.py`
- 可能涉及 `tests/test_planning_prompts.py`

新增测试：

- `wait + medium + entry_condition + stop_loss` 渲染为 `⚡ 条件入场`。
- `wait + weak` 仍渲染为 `⏳ 等待确认`。
- 高候选分标题显示 `候选分 100`。
- 缺少止损但高分时渲染为 `👀 强观察`，不渲染为直接买入。
- `reject` 和硬风险仍不可被升级。

#### 回测与验证

建议分两层验证。

1. **离线回放**
   - 用历史 trace 或历史候选池数据回放，标记哪些股票会被分到 `conditional_open`、`strong_watch`、`plain_wait`。
   - 对比次日表现，不直接评估“能不能赚”，而是评估“条件入场组是否比普通等待组更容易出现正向延续”。
   - 重点观察次日收益分布、最大回撤、开盘后 15 分钟强弱、是否更容易捕捉延续而非追高。

2. **Prompt 稳定性测试**
   - 新增 golden tests，固定输入样本后检查 LLM 输出是否稳定包含 `entry_condition`、`stop_loss`、`failure_condition`、`no_chase_line` 等关键字段。
   - 对 `conditional_open`、`strong_watch`、`plain_wait` 分别建样本，确保不会因为一次字段缺失就全退化成 `wait weak`。
   - 对 `risk_off / panic / extreme` 样本，确保不会误输出 `conditional_open`。

如果要做更严格验证，可以再加一轮历史样本 A/B：

- A 版：当前保守规则。
- B 版：引入条件入场拆分。
- 比较 B 版是否显著增加“次日继续走强但未被发现”的覆盖率，同时不明显放大错误开仓率。

## 风险与护栏

- 不能因为候选分高就给 `open`。
- 不能在没有止损/失效条件时输出条件入场。
- 不能把“高开大涨”写成追买理由，必须有禁止追高线。
- 不能绕过账户单票上限和总仓位上限。
- 如果市场状态为 `risk_off/panic/extreme`，仍必须降级为普通等待或观察。

## 推荐决策

建议先做第一步“报告层改造”，把强 `wait` 显示成 `条件入场/强观察`，同时把 `100分` 改为 `候选分 100`。

确认报告呈现符合预期后，再改 Prompt，让上游更稳定地产出次日触发条件、禁止追高线和失效条件。

## 实施状态

已落地第一轮闭环实现：

- 报告层已按规则派生 `execution_mode`，把 `wait` 拆成 `conditional_open`、`strong_watch`、`plain_wait`。
- Markdown 标题已改为 `候选分 X`，避免把入池召回分误读为买入确定性。
- `conditional_open` 已输出次日触发、试探仓位、禁止追高、失效条件、加仓条件和复查触发。
- `strong_watch` 已输出不能直接入场原因、重点观察和升级条件。
- Prompt 已补充 `entry_quality` 的竞价/突破/回踩/失效字段，并允许组合配置输出 `wait + execution_mode=conditional_open`。
- 新增环境变量说明：`AGENT_CONDITIONAL_ENTRY_SCORE_MIN`、`AGENT_STRONG_WATCH_SCORE_MIN`、`AGENT_CONDITIONAL_ENTRY_MIN_STRENGTH`、`AGENT_NO_CHASE_PCT_DEFAULT`。
- 单元测试已覆盖条件入场、强观察、弱等待不升级和候选分标题。
