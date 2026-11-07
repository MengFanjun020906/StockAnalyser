# 确定性价格锚点与交易计划方案

> 状态：V1 已实现并接入正式单股链路；历史参数校准和真实行情验收仍是发布门槛
> 第一版：A 股普通股票，日线主锚点，周线只作趋势过滤
> 接管方式：代码默认启用，readiness 失败或数学条件不足时显式无计划，绝不回退到 LLM 补价
> 调研日期：2026-08-12

当前实现位于 `src/agent/pricing/`，已完成快照、锚点、聚类、逐场景计划、评审权限、账户决策、生命周期、持久化、结构化回测和报告渲染。普通/流式单股分析、Trace、Risk Gate 与 Signal Preview 均读取同一 `TradePlanResult`。这里的“已实现”表示代码和确定性回归已经落地，不代表 `price-plan-params.v1` 已通过足够历史样本证明具有投资有效性。

## 1. 目标与原则

当前单股 Agent 可以提取均线、筹码和价格结构，但精确入场区间、止损、目标位和有效期仍可能由 LLM 临场生成，导致相同快照下价格漂移、正文与机器计划不一致。

本方案建立唯一价格真源：

- 数学层确定价格锚点、候选区间、止损、目标、有效期和技术确认规则。
- LLM 只评审基本面、资金面和消息面证据，可以保持、降级或否决，不能生成、修改或升级价格。
- 数学层不能形成合格计划时返回 `NO_PLAN`，禁止回退到 LLM 或 Markdown 补价。
- 报告、Risk Gate、Signal Preview、信号生命周期和回测只消费同一个结构化计划或其兼容投影。
- 数学生成状态、LLM 评审权限、账户决策和 Risk Gate 状态分别保存，禁止用一个枚举混合多个层次。

## 2. 第一版范围

V1 只覆盖：

- A 股普通股票的做多计划；ETF、可转债、港股、美股和做空暂不接管。
- 日线生成入场锚点和计划，周线只输出趋势过滤结果，不生成独立交易区间。
- 单股询问及其后续信号生命周期；选股候选不创建和维护计划生命周期。
- 现有筹码分布可以作为成交结构锚点，但不能用日线 OHLCV 估算结果冒充真实 Volume Profile。

V1 的 `as_of` 必须解析为交易所交易日和时区。盘中请求只截断到前一已收盘交易日；收盘后请求在行情提供方确认日线完成后才可使用当日 bar。无法证明当日 bar 已完成时保守回退到前一交易日，并把实际 `as_of_session` 写入响应，禁止把未收盘日线混入计划。

短线、波段和中长线统一多周期选区属于 V2。V1 不混用日线入场、其他周期止损或目标。

## 3. 职责边界

```text
可复现市场快照
  -> 多类确定性锚点生成器
  -> 锚点资格审查
  -> 来源族去相关和 ATR 聚类
  -> 账户无关的 PricePlanArtifact（逐区间场景）
  -> LLM 股票级/区间级证据评审
  -> AccountPlanDecision（风险偏好/仓位/Risk Gate）
  -> TradePlanResult 只读组装
  -> PlanLifecycleState / 事件流水
  -> 报告 / Signal Preview / 生命周期 / 回测
```

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| 数学层 | 锚点、逐区间场景、止损、目标、风险收益比、可达性分档、有效期 | 基本面和事件语义判断、账户仓位和执行状态 |
| LLM | 基本面、资金、消息的支持/反对论点及证据引用 | 生成或修改价格、修改数学状态、自由给仓位 |
| 权限合并器 | 按固定矩阵生成评审权限 | 自由改写计划或账户决策 |
| `position_sizing.v1` | 生成仓位上限并接受确定性下调 | 生成价格 |
| `MarketRules` | 报价单位、涨跌停、停牌、T+1 和可成交性 | 解释投资逻辑 |
| Risk Gate | 数据质量、账户和风险硬约束 | 生成价格或恢复被否决计划 |

## 4. 输入快照与价格口径

快照构建器负责 IO、`as_of` 截断、缺失标记和价格口径统一；锚点生成、资格审查、聚类和计划计算均为纯函数。

价格计划快照至少包含，并且**不包含用户、账户、持仓和风险偏好**：

- 截止 `as_of` 已收盘的日线和周线 OHLCV、成交额、数据源和实际根数；
- 前复权计算序列、逐日复权因子和同期未复权现金价格；
- `as_of` 前最近可用的筹码均价、70%/90% 成本区、来源和数据时间；
- 分型、笔、中枢等结构证据及其确认日期；
- 股票板块、ST 状态、上市状态和交易日历版本；
- Strategy4 / Chan 结构证据，不复制自由文本结论。

市场状态、用户风险偏好、账户资金和持仓属于后续 `AccountPlanDecision` 的输入，不能改变 `PricePlanArtifact` 中任何价格。相同市场快照必须先产生相同的账户无关价格计划，然后不同用户才能在同一组场景上得到不同的选择和仓位结果。

日线只使用当时已经收盘的 bar，周线只使用 `as_of` 前已经结束的完整周 bar。V1 固定使用最近 `daily_evaluation_bars=520` 根日线和 `weekly_evaluation_bars=60` 根完整周线；EMA/MACD 等递归指标在评估窗口前额外使用 `daily_warmup_bars=200`、`weekly_warmup_bars=40` 根。快照必须记录实际使用根数，缺少预热根数时返回对应 `INSUFFICIENT_*_HISTORY`，不能用缩短窗口静默替代。历史回测中的筹码数据必须来自 `observed_at <= as_of` 的持久化快照；没有历史快照时移除该来源族并标记缺失，禁止拿今天的筹码分布解释过去。

指标和历史结构在统一的前复权计算序列上计算，所有对外价格必须转换为 `as_of` 当日现金可成交口径。原始现金 OHLCV 的标准归一化公式为：

```text
normalized_price[t] = raw_cash_price[t]
                      * adjustment_factor[t]
                      / adjustment_factor[as_of]
```

该序列在 `as_of` 与原始现金价格相等，指标和结构锚点直接落在同一现金基准上。若提供方直接给出前复权序列，必须先用逐日因子抽样验证等价后使用，不能再二次换算；禁止用首尾价格比反推因子。原本就是现金口径的筹码锚点不重复缩放。每个锚点记录 `price_basis` 和因子版本；以下任一条件失败时整份计划返回 `PRICE_SCALE_UNVERIFIED`，不能只禁用单个锚点。Strategy4 分数只作为下游证据和 Risk Gate 输入，不得参与本节的现金价格计算。

1. OHLCV、复权因子和标的身份来自同一提供方，或存在版本化的跨源映射；
2. 因子日期不晚于 `as_of`，并覆盖快照中的全部 K 线；
3. 换算后 OHLC 关系、正价格和最新现金收盘价误差检查通过；
4. 数据源、复权口径、换算实现和 `as_of` 均进入指纹。

因子语义必须由 `adjustment_factor_semantics` 显式声明。若提供方约定 `adjusted_price[t] = raw_cash_price[t] * F[t]`，则 `cash_price[t] = adjusted_price[t] * F[as_of] / F[t]`；若约定相反，则使用该提供方的版本化逆变换。没有语义声明时不能猜方向，直接返回 `PRICE_SCALE_UNVERIFIED`。

### 4.1 历史筹码快照

现有筹码接口不能默认视为支持历史回放。V1 按以下规则处理：

- 在线生成只能使用 `trade_date <= as_of` 的筹码数据，并把原始响应、标准化结果、来源和抓取时间持久化到 `chip_distribution_snapshots`；
- 历史回放只能读取当时已持久化的快照，或读取提供方明确支持指定历史交易日的响应；
- 禁止拿当前筹码覆盖历史 `as_of`，也禁止用日线成交量伪造筹码分布；
- 缺少历史筹码时，仅将对应锚点记为 `MISSING_HISTORICAL_CHIP_SNAPSHOT`，成本成交族中的 AVWAP 仍可独立参与；
- 剩余来源族不足两个时，该场景只能是观察区或 `NO_PLAN`；
- 离线评估必须单列“含真实筹码快照”和“缺筹码降级”覆盖率，不能混为一组统计。

## 5. 锚点协议与状态

```json
{
  "anchor_id": "600667-1d-ma30-20260811",
  "symbol": "600667",
  "market": "cn",
  "timeframe": "1d",
  "type": "ma30",
  "source_family": "trend",
  "price": 20.70,
  "price_basis": "cash",
  "role": "support",
  "direction": "falling",
  "anchor_status": "observation",
  "as_of": "2026-08-11",
  "confirmation_date": "2026-08-11",
  "source_ref": "calculate_ma",
  "source_priority": 100,
  "distance_atr": -1.42,
  "rule_version": "price-anchor.v1",
  "calculation_fingerprint": "sha256:..."
}
```

### 5.1 生成状态与运行状态

`AnchorStatus`（生成时不可变）：

| 状态 | 含义 | 是否参与聚类计票 |
| --- | --- | --- |
| `eligible` | 角色、方向、历史反应、新鲜度和来源均合格 | 是 |
| `observation` | 数值有效但资格不完整，例如下降中的支撑均线 | 否，只展示 |
| `rejected` | 口径错误、已破坏、过期或不可追溯 | 否 |

不可变 `PricePlanArtifact` 只保存 `GenerationStatus`：

| 状态 | 生成条件 |
| --- | --- |
| `generated` | 至少一个场景满足数学硬条件 |
| `observation` | 存在可展示区间，但没有场景满足正式交易硬条件 |
| `no_plan` | 无法形成可解释区间或关键口径失败 |

价格触达、确认、失效和过期是生成后的可变事实，只能进入事件账本和 `PlanLifecycleState`，不能回写 `PricePlanArtifact`。场景运行状态统一为：

| 状态 | 含义 |
| --- | --- |
| `waiting_touch` | 等待首次触达 |
| `touched` | 已进入区间但尚未满足确认规则 |
| `confirmed` | 数学确认规则成立 |
| `invalidated` | 确认前失效条件成立 |
| `expired` | 有效交易日耗尽 |
| `ambiguous` | 同一根日线无法判断触达和失效先后，只表示数据歧义 |
| `closed` | 人工关闭、版本停用或已被新计划替代 |

计划聚合状态由场景状态实时派生，不单独存一套可修改的计划状态字段。优先级固定为 `confirmed > touched > waiting_touch > ambiguous > invalidated > expired > closed`。证据变化需要重算价格时创建新计划修订，不回写旧计划或旧事件。

### 5.2 V1 锚点

第一版采用：

1. 趋势锚点：MA、EMA、布林中轨。
2. 成本成交锚点：数据源提供的筹码均价和 70%/90% 成本区。
3. 价格结构锚点：确认摆动点、分型、前高前低和缠论中枢边界。
4. 波动边界：ATR、布林带和历史波动分位，主要用于归一化、区间宽度和失效距离。

Anchored VWAP 起点只能来自确认摆动点、有效放量突破、中枢突破/跌破或除权除息后的首个交易日。回测从事件确认日开始使用，禁止把后来确认的拐点放回更早日期。

Fibonacci、Kronos、神经网络价格预测、纯相似 K 线、未确认 ZigZag 和离线全窗口变点不作为 V1 价格来源。

## 6. 来源族去相关和候选区间

来源族冻结如下：

| 来源族 | 指标/结构 | 族内规则 |
| --- | --- | --- |
| `trend` | MA、EMA、布林中轨 | 同一局部区间只保留一个代表 |
| `cost_volume` | AVWAP、筹码均价、70%/90% 成本区 | 同一局部区间只保留一个代表 |
| `price_structure` | 摆动点、分型、前高前低、中枢 | 同一结构事件只保留一个代表 |
| `volatility_boundary` | 布林上下轨、ATR envelope、波动分位边界 | 同一局部区间只保留一个代表；裸 ATR 不投票 |

其中只有 `trend / cost_volume / price_structure` 计入“至少两个来源族”的门槛；`volatility_boundary` 只负责 padding、最大宽度和失效缓冲，不能与共享同一均线中心的趋势锚点重复投票。“来源族”只是工程上的反重复计票规则，不代表这些指标在统计意义上相互独立，因此不能据此生成概率或置信度。

### 6.1 锚点资格审查

不再定义含糊的“资格分”。锚点先过硬门槛，再用固定排序键选族内代表。

`eligible` 必须同时满足：

1. 现金价格有限、为正且价格口径已验证；
2. `confirmation_date <= as_of`，来源数据和抓取版本可追溯；
3. `age_bars <= max_age_bars[anchor_type]`；
4. 支撑锚点在最近 `break_confirm_bars` 根已收盘 K 线中，没有连续收盘低于 `anchor_price - break_buffer_atr * ATR14`；阻力锚点对称处理；
5. 趋势锚点满足 `slope_over_window >= min_slope_atr_per_bar[anchor_type]`，其中 `slope_over_window = (value[t] - value[t-window]) / (ATR14[t] * window)`；结构锚点已经由 `price_structure` 层确认；
6. 历史反应次数达到 `min_reactions[anchor_type]`。允许首次使用的事件型 AVWAP 可把该参数设为 `0`，但必须有已确认事件日期。

一次“历史反应”定义为：价格进入 `anchor_price +/- reaction_touch_atr * ATR14` 后，在随后 `reaction_horizon_bars` 个已收盘交易日内，向预期方向移动至少 `reaction_move_atr * ATR_at_touch`；支撑的预期方向是向上，阻力的预期方向是向下。反应窗口不能越过 `as_of`，相邻反应起点至少间隔 `reaction_min_gap_bars`，同一段行情不能重复计数。`age_bars` 是 `as_of` 与 `confirmation_date` 之间的交易日数量，不按自然日计算。

数值有效但不满足方向或最小反应要求时是 `observation`；未来数据、口径不明、过期、非法数值或已被结构破坏时是 `rejected`。所有阈值按锚点类型写入 `price-plan-params.v1`。

同一来源族的代表按以下元组做字典序选择，方向固定，禁止运行时临时加权：

```text
reaction_count_capped DESC
 median_reaction_move_atr DESC
 age_bars ASC
 source_priority ASC
 anchor_id ASC
```

`source_priority` 是版本化的提供方优先级表，不是模型分数。这样既可去重，也避免 MA30、EMA30 和布林中轨重复计票。

### 6.2 聚类、边界和基础排序

1. 只对相同周期、相同角色的 `eligible` 锚点聚类；V1 交易区只使用日线支撑锚点，阻力锚点留给目标位。
2. 锚点按现金价格、`anchor_id` 升序排列。新锚点只有在与前一个锚点距离不超过 `cluster_gap_atr * ATR14`，且加入后整个簇宽度不超过 `max_cluster_width_atr * ATR14` 时才进入当前簇，否则开启新簇。该完整连接宽度约束用于阻止同族锚点链式桥接两个远离价位。
3. 每个簇按 6.1 的排序键为每个来源族保留一个代表，再重新计算簇；不足两个可计票来源族时只能生成观察区。
4. 设代表价格集合为 `P`，区间边界固定为：

```text
padding = max(tick_size, zone_padding_atr * ATR14)
zone.low  = floor_to_tick(min(P) - padding)
zone.high = ceil_to_tick(max(P) + padding)
```

5. 加 padding 后宽度超过 `max_zone_width_atr * ATR14` 的簇降为观察区，不允许裁掉某个锚点后伪造共振。
6. padding 后发生重叠的两个簇，只在合并后仍满足最大宽度时合并并重新去相关；否则只保留基础排序更高者为交易场景，另一个为观察场景。

场景基础排序键固定为：独立来源族数量降序、是否含 `price_structure` 降序、代表锚点反应中位数降序、距当前价的 ATR 距离升序、区间宽度升序、最老锚点年龄升序、`scenario_id` 升序。最终最多保留三个互不重复的 `ZoneScenario`，展示角色为 `primary / secondary / observation`。两个不连续区间绝不能用整体 `min/max` 合并。

## 7. V1 可达性与风险偏好

V1 不输出未经校准的“触达概率”，改为确定性 `ReachabilityBand`：

```text
if last_cash_price > zone.high:
    price_relation = above
    distance_atr = (last_cash_price - zone.high) / ATR14
elif zone.low <= last_cash_price <= zone.high:
    price_relation = inside
    distance_atr = 0
elif last_cash_price < zone.low AND (stop_loss is null OR last_cash_price > stop_loss):
    price_relation = below
    distance_atr = null
else:
    price_relation = invalidated
    distance_atr = null

near:   distance_atr <= 1.0
medium: 1.0 < distance_atr <= 2.5
far:    distance_atr > 2.5
unknown: price_relation in {below, invalidated}，或 ATR/现金价格不可用
```

`below` 表示价格已经穿过计划区间但尚未到失效位，不能被 `max(0, ...)` 伪装为 `near`；它只能等待重新生成计划或按生命周期规则失效。`1.0/2.5` 是 `price-plan-params.v1` 参数，变更必须升级参数版本。

风险偏好只在 `AccountPlanDecision` 中选择场景和确认/仓位强度，不改变任何区间边界。每种偏好使用固定排序键：

| 偏好 | 场景选择顺序 |
| --- | --- |
| 进取 | `inside > near > medium > far > below`，再按基础排序、净风险收益比降序 |
| 均衡 | 基础排序，再按 `inside > near > medium > far > below`、净风险收益比降序 |
| 稳健 | 含价格结构优先、来源族数量降序、净风险收益比降序、基础排序；额外要求基础确认连续两个收盘日有效 |

只有 `role != observation`、`price_relation in {above, inside}`、止损/目标完整且净风险收益比过线的场景才能被选择。相同排序键时以 `scenario_id` 升序裁决，确保完全确定。

未来只有在按市场状态、计划周期和距离分桶的历史样本达到最低样本量，并通过概率校准检验后，才能新增 `touch_probability`。样本不足时仍返回分档，不能返回伪精确概率。

## 8. 确定性交易计划

### 8.1 每个区间都是完整场景

数学层为每个交易区分别生成一个完整 `ZoneScenario`：

- 区间上下沿、角色、排序、来源族和证据引用；
- 独立的基础技术确认规则；
- 独立的结构止损和失效位；
- 独立的第一、第二目标位及退出比例；
- 以保守成交价计算的风险收益比；
- `ReachabilityBand`、有效交易日和失效条件；
- 数据质量、参数/算法版本和指纹。

禁止在计划顶层放一套共享止损/目标去解释多个区间。第一和第二场景即使来自同一计划，也必须能被单独回测、失效和展示。

### 8.2 止损、目标和风险收益比

对每个做多场景：

1. `entry_reference = zone.high`，作为最保守计划入场参考价。
2. 从 `as_of` 前已确认的分型低点、摆动低点和中枢下沿中，寻找不高于 `zone.low` 的最近结构支撑；如果场景内本身含价格结构锚点，则同时把 `zone.low` 作为保护边界候选。
3. `protective_level = max(符合条件的结构支撑)`；没有结构支撑时场景只能观察并记录 `NO_VALID_STOP`。
4. `stop_loss = floor_to_tick(min(zone.low, protective_level) - stop_buffer_atr * ATR14)`。必须满足 `stop_loss < zone.low`，且 `(entry_reference - stop_loss) / ATR14 <= max_stop_distance_atr`，否则只能观察。
5. 从同一冻结快照的 `eligible` 阻力锚点中选目标。`target_1` 是**高于 `entry_reference` 的最低阻力**；阻力先按来源族去重，同价时用 6.1 排序键裁决。选出后必须满足 `target_1 - entry_reference >= min_target_gap_atr * ATR14`，否则记录 `TARGET_TOO_CLOSE` 并降为观察，禁止跳过这个最近阻力去挑更远的目标。`target_2` 是高于 `target_1` 且至少相隔 `second_target_gap_atr * ATR14` 的最低阻力。
6. 没有 `target_1` 时记录 `NO_VALID_TARGET` 并降为观察。`target_2` 可以为空；为空时 `target_1_exit_ratio=1.0`，否则使用 `execution-policy.v1` 冻结的退出比例。
7. 做多计划采用保守取整：入场上沿向上、止损向下、目标向下取最小报价单位。

账户无关计划使用 `reference-cost-policy.v1` 的固定参考双边成本和滑点计算 `reference_net_rr_1`：

```text
reference_entry_net = entry_reference * (
  1 + (reference_buy_cost_bps + reference_buy_slippage_bps) / 10000
)
reference_stop_net = stop_loss * (
  1 - (reference_sell_cost_bps + reference_sell_slippage_bps) / 10000
)
reference_target_1_net = target_1 * (
  1 - (reference_sell_cost_bps + reference_sell_slippage_bps) / 10000
)

reference_net_risk = reference_entry_net - reference_stop_net
reference_net_reward_1 = reference_target_1_net - reference_entry_net
reference_net_rr_1 = reference_net_reward_1 / reference_net_risk
```

这套参考成本只用于跨账户一致的场景资格审查，不声称等于用户真实成本。`target_1` 缺失、`reference_net_risk <= 0` 或 `reference_net_rr_1 < min_reference_net_rr_1` 时场景只能观察。`AccountPlanDecision` 必须按第 12 节用真实委托数量和账户费率重算 `account_net_rr_1`；真实结果不达标时只能阻断或降级，不能修改场景价格。所有金额计算使用 `Decimal`，中间过程不提前四舍五入。

### 8.3 机器确认规则

触达不等于买点。设日线 `t` 已收盘：

```text
touch_t = low[t] <= zone.high AND high[t] >= zone.low
recent_touch_t = exists k in [t - confirm_after_touch_bars + 1, t]
                 where touch_k = true
hard_break_t = close[t] <= stop_loss
volume_break_t = close[t] < zone.low - breakdown_buffer_atr * ATR14[t]
                 AND volume[t] >= breakdown_volume_ratio * volume_ma5_prev[t]
```

`volume_ma5_prev[t]` 只使用 `t-1` 及更早数据。触达事件在 `confirm_after_touch_bars` 个交易日内有效，使 `t-1` 的触底可以在 `t` 的底分型确认后生效；窗口过后必须再次触达。`hard_break_t` 或 `volume_break_t` 成立时场景失效；同一根日线既触达又触发失效且无法从更细粒度行情确定先后时记为 `ambiguous`，不能乐观确认。

止跌形态只认以下三个机器谓词：

```text
body = abs(close - open)
bar_range = max(high - low, tick_size)
lower_shadow = min(open, close) - low
upper_shadow = high - max(open, close)

hammer = close > open
         AND lower_shadow >= hammer_lower_body_ratio * max(body, tick_size)
         AND upper_shadow <= hammer_upper_body_ratio * max(body, tick_size)
         AND (close - low) / bar_range >= hammer_close_location_min

bullish_engulfing = close[t-1] < open[t-1]
                    AND close[t] > open[t]
                    AND open[t] <= close[t-1]
                    AND close[t] >= open[t-1]

confirmed_bottom_fractal = low[t-1] < low[t-2]
                           AND low[t-1] < low[t]
```

底分型的结构日期是 `t-1`，确认日期必须记为 `t`；若复用 `price_structure` 的包含关系合并 K 线，则必须复用其版本化事件输出，评分器不得另写一套分型。

量能规则固定为：锤子线或底分型使用 `volume[t] <= contraction_volume_ratio * volume_ma5_prev[t]`；看涨吞没使用 `volume[t] >= expansion_volume_ratio * volume_ma5_prev[t]`。基础技术确认在“`recent_touch_t` 成立、未失效、三个形态之一成立、对应量能规则成立、止损和净风险收益比合格”时成立。

```text
进入候选区间
  -> 未破坏区间/失效位
  -> 已收盘日 K 的机器止跌形态成立
  -> 对应量能规则通过
  -> 止损距离和净风险收益比通过
  -> PlanLifecycleState = confirmed
```

暴跌进入区间只能变成 `touched`、`ambiguous` 或 `invalidated`，不能自动升级为买入。确认发生在日线收盘后，最早只能从下一交易日进入可执行评估。

### 8.4 周线过滤和有效期

周线只使用已完成周 K，不生成价格。设最后一根完整周线为 `w`：

```text
weekly_above = close[w] >= EMA13[w]
weekly_rising = EMA13[w] > EMA13[w - weekly_slope_window]

supportive = weekly_above AND weekly_rising
neutral = weekly_above XOR weekly_rising
opposing = NOT weekly_above AND NOT weekly_rising
```

历史不足 `weekly_min_bars` 时是 `unavailable`。`supportive` 使用基础确认；`neutral` 要求确认后下一交易日收盘仍高于 `zone.low` 且未失效；`opposing/unavailable` 将交易场景降为观察。周线过滤状态和参数进入 `PricePlanArtifact` 与指纹，但不修改任何价格。

`valid_from` 是 `actual_as_of_session` 后第一个可交易日；`confirmation_deadline` 是再向后 `confirmation_valid_trading_days - 1` 个可交易日。停牌日不计数。截止日收盘仍未确认则 `expired`；若截止日确认，委托仍可在随后 `order_valid_trading_days` 个可交易日内等待成交。成交后的回测最长持有 `max_holding_trading_days`，到期按收盘价和成本退出。三个窗口均来自 `price-plan-params.v1`，不能使用自由文本日期。

## 9. LLM 评审与确定性合并

LLM 进行两级评审：

- 股票级：基本面硬伤、重大事件、资金性质及可核验 `hard_veto`。
- 区间级：资金成本或事件逻辑是否支持该区间、适用周期和非价格风险。

数学计划必须在 Debate Judge 之前生成，并以只含 `scenario_id`、区间、来源族、数学原因码和角色的紧凑结构进入共享证据包。Judge 对技术、资金、基本面和消息面分别输出 `plan_verdict / evidence_refs / reason_codes / scenario_ids`；`scenario_ids=[]` 表示股票级意见，非空时只能引用数学层已存在的区间。报告中的价格来源于 `PricePlanArtifact`，四维意见只形成 `DimensionPlanReview` 和逐区间 `ScenarioReviewImpact`，两者不包含新价格。

固定输入枚举为 `support / mixed / insufficient / opposing / hard_veto`。合并器输出独立的 `ReviewPermission`：

| LLM 结论 | ReviewPermission | 唯一处理 |
| --- | --- | --- |
| `support` | `allowed` | 保留基础确认规则，仓位乘数 `1.0` |
| `mixed` | `conditional` | 要求基础确认连续两个已收盘交易日保持有效，仓位乘数 `0.5` |
| `insufficient` | `observe_only` | 只展示和补数，仓位乘数 `0.0` |
| `opposing` | `observe_only` | 降为观察，仓位乘数 `0.0` |
| `hard_veto` | `vetoed` | 禁止执行，仓位乘数 `0.0` |

股票级和区间级权限取更严格者。`hard_veto` 必须引用可核验一手证据；LLM 评审不修改锚点、场景价格、生成状态或生命周期状态。

每个场景必须同时具有技术、资金、基本面和消息面的有效结构化结论，缺少任一维度即按 `insufficient -> observe_only`。同一维度可以同时存在股票级和区间级意见；合并时只对当前 `scenario_id` 取更严格者，禁止用一个区间的反对意见降低其他区间。`support` 遇到 `partial` 证据确定性降为 `mixed`；没有真实工具证据的模型结论不进入权限合并。技术面仅检查执行条件，不对已经参与数学价格计算的指标重复加权。

评审调用本身另存 `ReviewStatus = pending / completed / cached / failed`。只有 `completed/cached` 的合法结构化输出可以进入权限合并；`pending/failed` 一律按 `insufficient -> observe_only` 处理，同时保留错误原因，不能因为模型故障放宽权限。

`review-policy.v1` 冻结 `mixed` 为连续两个已收盘交易日有效、仓位乘数 `0.5`；`support` 乘数为 `1.0`，其余为 `0.0`。未来修改必须升级 policy 版本并重新离线验收，运行时不能二选一。

每次评审生成不可变 `PlanReviewArtifact`。每次用户/账户评估生成新的不可变 `AccountPlanDecision`，它引用价格计划、场景、评审、风险偏好、`position_sizing.v1` 结果和 Risk Gate 结果；账户变化不得回写价格计划。

账户决策在构造 Risk Gate 输入前，分别对“允许动作集合”和“仓位上限”只截断一次：

```text
math_actions = {observe}
if scenario is confirmed:
    math_actions += {open, add}

review_actions =
  allowed     -> {observe, open, add}
  conditional -> {observe, open, add} only after extra confirmation
  observe_only -> {observe}
  vetoed       -> {}

pre_gate_allowed_actions = intersection(
  math_actions,
  review_actions,
  intent_allowed_actions,
)

pre_gate_position_cap = min(
  position_sizing_v1_cap * risk_profile_multiplier * review_multiplier,
  account_cap,
)
RiskGateInput.requested_action = clamp_to_allowed_set(
  original_requested_action,
  pre_gate_allowed_actions,
)
RiskGateInput.position_cap = pre_gate_position_cap
```

`clamp_to_allowed_set` 不使用隐含枚举大小关系：原动作允许则原样保留；不允许但 `observe` 可用则固定降为 `observe`；集合为空则返回 `blocked`。其中 `review_multiplier` 和 `risk_profile_multiplier` 已冻结在 `[0, 1]`；`account_cap` 缺失时使用 `1.0`，不是 `0`，但账户净盈亏比缺失仍按参考成本标记而不能伪装为真实成本。Risk Gate 不重复截断价格或仓位，只追加只读检查 `COMPOSITE_VERDICT_CEILING_APPLIED`，记录原始动作、截断后动作、原始仓位上限、截断后上限、触发来源和 `clamped=true/false`。这样报告、账户决策和 Risk Gate 只存在一个实际 clamp 点，审计可以证明是否发生过降级。

最终执行状态由纯函数计算：

```text
PricePlanArtifact.GenerationStatus
+ selected ZoneScenario
+ current PlanLifecycleState
+ ReviewPermission
+ position_sizing.v1 result
+ RiskGateResult
+ readiness/kill-switch state
=> ExecutionStatus
```

`ExecutionStatus` 及优先级冻结如下，按表从上到下首次命中：

| 状态 | 唯一条件 |
| --- | --- |
| `system_unready` | 开关已开，但数据库、参数、交易制度或自检未就绪 |
| `disabled` | 全局开关关闭或锚点族被紧急停用后无可用场景 |
| `no_plan` | `GenerationStatus=no_plan` |
| `closed` | 选中场景状态为 `invalidated/expired/closed` |
| `vetoed` | `ReviewPermission=vetoed` |
| `observe_only` | `GenerationStatus=observation`、场景角色为观察，或权限为 `observe_only` |
| `waiting_confirmation` | 场景状态未 `confirmed`，或 `conditional`/稳健型的附加确认尚未完成 |
| `risk_blocked` | 数学及评审允许，但仓位上限为空/为零，或 Risk Gate 不允许 `open/add` |
| `actionable` | 数学及附加确认完成、权限允许、仓位上限大于零，且 Risk Gate 允许 `open/add` |

Risk Gate 的 `downgraded` 只有在 `allowed_action` 仍是当前请求对应的 `open/add` 且仓位上限大于零时才可进入 `actionable`；否则进入 `risk_blocked`。执行状态只是条件计划状态，不代表已经下单或成交。

## 10. 仓位唯一真源

价格引擎不计算最终仓位。现有 `position_sizing.v1` 继续作为基础仓位上限唯一真源，由 Strategy4 档位和市场状态决定基础上限。`AccountPlanDecision` 最多选择一个可执行场景，其余场景继续用于展示和生命周期观察，避免同一计划重复占用仓位。

风险偏好、LLM 评审和账户 Risk Gate 只能在该上限上确定性下调：

```text
final_position_cap = min(
  position_sizing_v1_cap * risk_profile_multiplier * review_multiplier,
  account_cap,
  risk_gate_cap
)
```

上式中的前半部分正是 `pre_gate_position_cap`；`risk_gate_cap` 只代表 Risk Gate 的硬约束检查结果。实际送入 Risk Gate 的金额不得再次套用 `review_multiplier` 或 `risk_profile_multiplier`，最终固定为 `final_position_cap = min(pre_gate_position_cap, risk_gate_cap)`。

风险偏好乘数必须在仓位模块升级版本时单独冻结，不能写进价格引擎，也不能由 LLM 返回。评审、风险偏好和 Risk Gate 输入缺失时按最严格权限处理，绝不放大仓位；只有“系统不绑定账户”这一设计导致的 `account_cap` 缺失按无额外账户上限 `1.0` 处理，并在决策中记录 `account_context_absent=true`。

## 11. 可复现、指纹与 LLM 固化

“相同输入得到相同结果”只约束：

- 数学层：相同快照、参数和算法版本得到相同锚点、区间和计划。
- 合并器：相同数学计划、已固化 LLM 评审和 Risk Gate 输入得到相同权限结果。

LLM 本身不声明可复现。首次评审后保存不可变评审 artifact，历史回放读取 artifact，不重新调用模型。

### 11.1 规范序列化

所有指纹输入使用规范 JSON：UTF-8、键名排序、数组保持协议规定顺序、时间统一 ISO 8601、价格和因子使用固定精度十进制字符串、缺失值显式为 `null`、禁止 `NaN/Infinity`，最后计算 SHA-256。

### 11.2 指纹分层

| 指纹 | 必须覆盖的输入 |
| --- | --- |
| `data_fingerprint` | symbol/market/timeframe、排序后的交易日与 OHLCV/成交额、复权因子、现金价格、行情源、各自 `as_of`、筹码原始字段/来源/时间、结构输入、完整 `MarketRuleSnapshot`（tick、涨跌停、停牌、T+1、规则版本）、交易日历版本 |
| `calculation_fingerprint` | `data_fingerprint`、算法版本、参数版本、价格口径、预热长度和实际历史根数 |
| `plan_fingerprint` | 规范化数学计划载荷和 `calculation_fingerprint`；包含用于取整和可成交性判断的制度快照；排除自身、数据库 ID、写入时间、LLM 评审、仓位、Risk Gate 和执行状态 |
| `evidence_bundle_fingerprint` | 排序后的基本面、资金、消息证据引用、内容摘要、来源和各自 `as_of` |
| `review_fingerprint` | LLM 评审规范化输入、原始结构化输出、验证结果和模型调用元数据 |

LLM 评审缓存键至少包含：

```text
plan_fingerprint
+ evidence_bundle_fingerprint
+ prompt_version
+ provider/model/version
+ generation_parameters
```

模型没有稳定服务端版本号时记录 provider 返回的模型标识和调用时间，不能伪造版本。证据或计划变化时必须创建新评审，禁止命中旧缓存。

### 11.3 身份、修订、幂等与并发

```text
plan_key = symbol + market + timeframe + as_of_session
           + algorithm_version + params_version

plan_id = "pp_" + sha256(plan_key + plan_fingerprint)[0:32]
scenario_id = "zs_" + sha256(plan_id + canonical_scenario_payload)[0:24]
review_id = "pr_" + sha256(plan_id + review_fingerprint)[0:32]
decision_id = "pd_" + sha256(plan_id + scenario_id + decision_scope
                              + decision_input_fingerprint)[0:32]
```

- `(plan_key, plan_fingerprint)` 唯一；同一请求重试返回已有 artifact，不新增修订。
- 同一 `plan_key` 因数据更正产生不同 `plan_fingerprint` 时，在数据库事务中分配递增 `revision`，并设置 `supersedes_plan_id`；旧 artifact 不修改。
- `decision_scope` 是会话/用户/账户的稳定内部 ID；不得把姓名、账号或其他敏感原文放进 ID。
- 生命周期事件唯一键为 `(plan_id, scenario_id, event_type, effective_trade_date, source_event_fingerprint)`，重复任务只能幂等命中。
- `PlanLifecycleState` 是事件流的物化结果，带 `state_version`。更新使用行锁或 compare-and-swap；版本冲突时重放事件，不允许最后写入者静默覆盖。
- 同一 `as_of_session` 的较新修订落库后，旧修订状态转为 `closed` 的事实也必须以独立事件记录，不能修改旧 artifact。

## 12. 交易制度与价格取整

交易制度抽成共享 `MarketRules`，由价格计划、Risk Gate、生命周期和回测共同调用，不能只在链路末端检查。

每个交易日生成不可变 `MarketRuleSnapshot`，至少包含 `venue/board/st_status/listing_status/trade_date/previous_close/tick_size/buy_lot_size/price_limit_rule/limit_up/limit_down/trade_calendar_version/rule_source/rule_version`。规则优先读取交易所或已验证行情提供方的当日字段；自行计算时必须保存采用的日期版本。不能用当前规则回算制度不同的历史日期。

V1 A 股普通股票规则：

- 默认报价单位是 `0.01`，但计算始终读取 `tick_size`；区间下沿和止损向下、区间上沿向上、做多目标向下取整，不能统一使用 `ROUND_HALF_UP`。
- 买入数量按 `buy_lot_size` 向下取整；V1 默认普通股票一手 100 股。卖出零股按实际持仓和当日制度处理，价格引擎不自行补整手。
- 涨跌停价按交易日期、板块、ST/退市状态和当日规则确定，不用一个固定百分比覆盖所有股票。
- 停牌 bar 不触发区间、不计有效交易日，也不能成交。
- 买入遇到一字涨停只记录价格条件，不记成交；卖出/止损遇到一字跌停同理。一字跌停不代表买方绝对不可成交，一字涨停也不代表卖方不可成交，必须按订单方向判断。
- A 股买入后的退出遵循 T+1。信号确认发生在收盘后，最早委托日是下一可交易日。

账户成本按显式版本化的账户费率输入计算：

```text
buy_notional = buy_fill_price * quantity
sell_notional = sell_fill_price * quantity

buy_fee = max(buy_notional * buy_commission_rate, min_commission)
          + transfer_fee(buy_notional, venue, trade_date)
          + other_buy_levies(trade_date)

sell_fee = max(sell_notional * sell_commission_rate, min_commission)
           + transfer_fee(sell_notional, venue, trade_date)
           + stamp_duty(sell_notional, trade_date)
           + other_sell_levies(trade_date)
```

V1 的 `AccountContext` 接收 `buy_commission_bps`、`sell_commission_bps`、`minimum_commission`、`transfer_fee_bps`、`stamp_duty_bps` 和 `cost_policy_version`。只有字段齐全时才输出按整手数量计算的 `account_net_rr_1` 与 `planned_quantity`；显式提供账户但费率不全时返回 `ACCOUNT_COST_POLICY_UNAVAILABLE` 并阻断执行。系统未绑定账户时保留参考计划，标记 `account_context_absent=true` 和 `reference-cost-policy.v1`，不声称是真实账户成本。按生效日期自动选择交易费率制度表属于后续增强，不能在完成前写成已实现能力。回测滑点按 `execution-policy.v1` 的方向性 bps 应用：买入上调、卖出下调，并受当日涨跌停价约束。

区间“触达”和“可成交”分别记录。缺少板块/ST/前收盘或无法确定历史交易规则时，正式回测标记为 `unevaluable`，实时计划返回 `NO_PLAN`。

## 13. 严格对象、生命周期与迁移

### 13.1 三类不可变对象和一个读模型

“计划对象”不能同时承载数学真相、LLM 结论和会随行情变化的生命周期。V1 明确拆成：

1. `PricePlanArtifact`：账户无关、数学层产物。只要输入指纹相同，内容完全不变。
2. `PlanReviewArtifact`：LLM 对股票级和场景级证据的结构化评审。只引用场景，不含任何新价格。
3. `AccountPlanDecision`：某个用户/账户/风险偏好在某一时刻对场景的选择、确认强度、仓位上限和 Risk Gate 结果。账户变化只产生新决策。
4. `TradePlanResult`：API、报告和 Trace 的组装读模型，引用上面三个对象和当前 `PlanLifecycleState`，不是新的价格来源。

所有对象使用 Pydantic `ConfigDict(extra="forbid")`。错误只能通过枚举状态和 `reason_codes` 表达，不能让消费者从自由文本补字段。

### 13.2 `PricePlanArtifact` 与 `ZoneScenario`

```text
PricePlanArtifact {
  schema_version, plan_id, revision, supersedes_plan_id?
  symbol, market="cn", side="long", timeframe="1d"
  requested_as_of, actual_as_of_session
  generation_status: generated | observation | no_plan
  scenarios: ZoneScenario[]                 # 最多 3 个，互不合并
  observation_anchors: AnchorEvidence[]
  market_rules_snapshot_ref
  data_quality, reason_codes
  algorithm_version, params_version
  data_fingerprint, calculation_fingerprint, plan_fingerprint
  created_at, trace_ref
}

ZoneScenario {
  scenario_id, role: primary | secondary | observation
  base_rank, source_families[], anchor_refs[]
  entry_low, entry_high, entry_reference
  stop_loss, target_1?, target_2?
  target_1_exit_ratio
  reference_net_rr_1, reference_net_risk, reference_net_reward_1
  reachability_band, price_relation
  confirmation_rules[], invalidation_rules[]
  confirmation_seed_bars[]                 # 最多 5 根，供首个前向日量能/形态判断
  valid_from, confirmation_deadline
  confirmation_valid_trading_days, order_valid_trading_days
  scenario_reason_codes[], scenario_fingerprint
}
```

每个 `ZoneScenario` 独立保存止损、目标、风险收益比、有效期和失效规则。`generation_status=no_plan` 时 `scenarios=[]`，且必须有至少一个数学原因码；`observation` 场景可以缺止损或目标，但必须明确原因。`generated` 的正式交易场景必须有止损、`target_1`、正的 `reference_net_rr_1` 和完整机器规则。

### 13.3 `PlanReviewArtifact`、`AccountPlanDecision` 和生命周期

```text
PlanReviewArtifact {
  review_id, plan_id, scenario_id?
  evidence_bundle_fingerprint, prompt_version, model_ref
  conclusion: support | mixed | insufficient | opposing | hard_veto
  evidence_refs[], validated_output, review_status
  dimension_reviews[]: dimension, scope, verdict, evidence_quality,
                       execution_effect, evidence_refs[], scenario_ids[]
  scenario_impacts[]: scenario_id, math_effect, applicable_dimensions[],
                      final_effect, reason_codes[]
  review_fingerprint, created_at
}

AccountPlanDecision {
  decision_id, plan_id, decision_scope, risk_profile
  selected_scenario_id?
  review_permission, confirmation_policy
  position_sizing_ref, target_position_pct?
  account_net_rr_1?, planned_quantity?
  account_cost_policy_ref?, account_context_absent
  risk_gate_ref, execution_status
  input_fingerprint, created_at
}

PlanLifecycleState {
  plan_id, scenario_id, state_version
  state: waiting_touch | touched | confirmed | invalidated |
         expired | ambiguous | closed
  first_touched_at?, confirmed_at?, invalidated_at?, expired_at?
  last_evaluated_as_of, last_event_id
}
```

生命周期事件只追加不修改：`plan_created`、`touched`、`technical_confirmed`、`invalidated`、`expired`、`superseded`、`manually_closed`。状态迁移由事件日期和第 8.3 节谓词计算，使用事件唯一键和 `state_version` 幂等更新。`TradePlanResult` 只在请求时组装；同一计划可以给不同账户返回不同 `selected_scenario_id`，但所有账户看到的价格场景相同。

### 13.4 原因码

`reason_codes` 使用固定枚举，不能塞入自由文本：

| 原因码 | 适用场景 |
| --- | --- |
| `INSUFFICIENT_DAILY_HISTORY` / `INSUFFICIENT_WEEKLY_HISTORY` | 历史或预热不足 |
| `MISSING_CASH_PRICE` / `ADJUSTMENT_FACTOR_MISSING` | 无法映射现金价格 |
| `ADJUSTMENT_CONFLICT` / `PRICE_SCALE_ANOMALY` | 复权或价格尺度冲突 |
| `PRICE_SCALE_UNVERIFIED` | 复权因子语义、覆盖范围或现金价校验未通过 |
| `DATA_SOURCE_CONFLICT` / `AS_OF_INCONSISTENT` | 多源或时间口径冲突 |
| `MISSING_HISTORICAL_CHIP_SNAPSHOT` | 对应 `as_of` 没有可复现筹码快照，仅禁用该锚点 |
| `MARKET_RULES_UNAVAILABLE` | 无法确定报价、涨跌停或交易日制度 |
| `UNSUPPORTED_INSTRUMENT` | V1 不支持该标的类型或无法证明是 A 股普通股票 |
| `ANCHOR_EXPIRED` / `ANCHOR_BROKEN` / `STRUCTURE_UNCONFIRMED` | 单个锚点过期、被破坏或尚未完成右侧确认 |
| `NO_ELIGIBLE_ANCHORS` | 没有通过资格审查的锚点 |
| `INSUFFICIENT_SOURCE_FAMILIES` | 不足两个独立来源族 |
| `NO_VALID_STOP` / `NO_VALID_TARGET` | 无法形成结构止损或目标 |
| `TARGET_TOO_CLOSE` | 最近结构阻力距离不足，不允许跳过阻力选远目标 |
| `RISK_REWARD_BELOW_MINIMUM` | 所有区间均低于最低风险收益比 |
| `ZONE_WIDTH_EXCEEDED` | padding 后区间超过最大宽度 |
| `AS_OF_ROLLED_BACK` | 请求发生在未收盘时，已回退到上一已收盘交易日 |
| `REVIEW_PENDING` / `REVIEW_FAILED` | 数学计划存在，但评审未完成或失败，只能观察 |
| `LLM_PRICE_AUTHORITY_VIOLATION` | LLM 输出结构化或正文价格字段，已清洗并按评审失败处理 |
| `POSITION_SIZING_UNAVAILABLE` / `RISK_GATE_BLOCKED` | 计划不能执行，但不改变数学价格 |
| `ACCOUNT_COST_POLICY_UNAVAILABLE` / `ACCOUNT_NET_RR_BELOW_MINIMUM` | 显式账户的费用口径不完整，或账户真实净 RR 不达标 |
| `ACCOUNT_CONTEXT_ABSENT_REFERENCE_COST` | 系统未绑定账户，仅使用明确标识的参考成本；不是错误或真实账户结果 |
| `SYSTEM_NOT_READY` / `SYSTEM_DISABLED` | readiness gate 失败或全局开关关闭 |
| `ANCHOR_FAMILY_DISABLED` | 来源族隔离开关生效 |

`NO_PLAN` 只使用导致数学计划无法形成的原因；评审、仓位、Risk Gate 和系统开关原因保留在同一结果中，但通过 `execution_status` 表达，不能反向改写 `generation_status`。详细诊断可另存结构化 `diagnostics`，不参与状态判断。

原因码还需声明作用域：`plan` 级错误影响 `generation_status`；`scenario` 级错误只降级对应场景；`review/account/system` 级错误只影响 `execution_status`。`MISSING_HISTORICAL_CHIP_SNAPSHOT`、`TARGET_TOO_CLOSE`、`ZONE_WIDTH_EXCEEDED` 默认是 scenario 级；只有所有场景均因此不可用时，聚合器才补充 `NO_ELIGIBLE_ANCHORS` 或 `INSUFFICIENT_SOURCE_FAMILIES` 形成 `no_plan`。

### 13.5 旧 `TradePlan` 兼容投影

现有 `src/schemas/agent_signal.py::TradePlan` 暂时保留，只由 `TradePlanResult` 单向投影：

- `entry_zones` 映射最多三个独立区间，`entry_zone_low/high` 仅映射主区。
- `take_profit_price` 仅兼容映射主场景第一目标；新消费者必须读取各场景的 `targets`。
- `execution_window` 仅渲染主场景 `confirmation_deadline`、`confirmation_valid_trading_days` 和 `order_valid_trading_days`，不能反向解析为正式数据。
- `target_position_pct` 来自账户决策和仓位模块，不来自价格引擎。
- `metadata` 只承载兼容引用，不作为新字段逃生口。

正式链路不能从旧 `TradePlan`、Debate 或 Markdown 反向创建 `TradePlanResult`。旧 `extra="allow"` 只服务兼容期，新对象严格拒绝未知字段。

### 13.6 持久化与 `EntrySignal`

新增不可变 `trade_plan_artifacts` 表，至少保存：`plan_id`、`revision`、`supersedes_plan_id`、symbol/market/as_of、生成状态、有效期、数据/计算/计划指纹、完整价格计划 JSON、Trace 引用和创建时间。另建 `plan_review_artifacts`、`account_plan_decisions`、`plan_lifecycle_events`、`plan_lifecycle_states`、`market_rule_snapshots` 和 `chip_distribution_snapshots`；评审、账户决策和运行状态不能塞回价格计划 JSON。

`entry_signals` 新增可空 `plan_id`、`scenario_id` 外键；现有 `entry_low/high/stop_price/trigger_rule_json` 保留为所选场景的生命周期查询物化字段。`EntrySignal` 继续遵守现有模块的账户无关约束，`account_id` 为空；同一 `owner_id + plan_id` 最多创建一条当前信号，`scenario_id` 指向当次单股询问选中的场景，避免多个区间重复占仓语义。账户实际仓位和费率只存在于 `AccountPlanDecision`，不得写回 `EntrySignal`。Signal Preview 仍从 `PricePlanArtifact.scenarios[]` 绘制全部主、次、观察区。场景状态统一存放在 `plan_lifecycle_states`，不把第二个区间的止损写入第一个信号。

数据库迁移遵循：

- 新表和新列采用向后兼容迁移，不改写历史计划。
- 旧记录 `plan_id = null`，标记 `legacy_unstructured` 并只读展示。
- Markdown 解析器只允许离线读取旧 Trace，结果标记 `legacy_unverified`，不能创建新正式信号或参与正式统计。
- 新计划每次价格变化创建新 `plan_id/revision`，旧 artifact 永不覆盖。

## 14. 回测分类和既有服务关系

现有 `agent_entry_execution_backtest_service.py` 的 `STRATEGY_NAMES` 是执行策略/基准维度，继续保留。新方案增加 `deterministic_price_plan` 作为正式策略；`strict_ai_entry` 只用于旧 Trace 对照，其他 baseline 继续比较。

每个策略结果使用两轴，且**按场景保存**，不能把多个区间压成一条价格线：

```text
evaluation_status = evaluated | unevaluable | skipped
outcome = WIN | LOSS | EXPIRED | INVALIDATED | NOT_FILLED | UNRESOLVED | null
```

- `scenario_evaluations[]` 对每个 `scenario_id` 单独计算触达、确认、成交、退出和 MAE/MFE；
- `policy_evaluation` 再按风险偏好的固定场景排序选择至多一个执行场景。它记录 `selected_scenario_id`、选择原因和是否有其他场景先触达，不能覆盖逐场景结果；
- 没有用户账户时使用 `backtest-policy.v1` 的中性选择顺序，仅用于数学策略评估，不把 LLM 账户决策偷换成数学结果；
- LLM 评审只允许读取 `as_of` 当时已经固化的 `PlanReviewArtifact`。缺少评审时仍可跑 math-only 轴，但 full-decision 轴标记 `unevaluable`，不能用今天的评审补历史。

- `WIN/LOSS`：已成交并结束；超时退出按扣除成本后的净收益正负分类，零收益保守记 `LOSS`。
- `EXPIRED`：有效期内未形成可成交确认。
- `NOT_FILLED`：已确认且仍在订单窗口内，但因停牌、涨停、价格未触及限价或数量/制度约束始终没有成交。
- `INVALIDATED`：入场前先触发结构失效。
- `UNRESOLVED`：入场前同一交易日同时满足触达/确认和失效，但没有更细粒度数据判断先后。它不是生命周期状态 `ambiguous` 的别名，而是回测结果分类；生命周期只记录 `ambiguous` 事件。
- `outcome=null`：数据不足或策略不适用，由 `evaluation_status` 解释。

入场后同一根 K 线同时覆盖止损和目标时，按“止损优先”成交，记录 `ambiguous_bar=true`；若因此净收益为负则 `LOSS`，无需使用 `UNRESOLVED`。有分钟线时按分钟数据重放，覆盖日线保守规则。

V1 成交模型固定为：

1. 确认在 `t` 日收盘产生，委托从下一可交易日开始；停牌、无交易或一字涨停不成交。
2. 买入限价为 `zone.high`。下一根 K 线开盘价不高于限价时按开盘价成交；否则只有当 `low <= zone.high <= high` 才按限价成交；两种情况都叠加方向性滑点并按 tick 取整。
3. 若开盘已经低于 `stop_loss`，视为入场前失效，不在失效价下方强行成交。
4. `target_1` 命中后按 `target_1_exit_ratio` 减仓；剩余仓位等待 `target_2`、止损或有效期结束。没有第二目标时第一目标卖出全部仓位。
5. 同一日同时命中多个退出条件按 `stop_loss > target_1 > target_2` 的保守优先级；T+1 以前不允许卖出刚买入的数量。

每个 scenario 和 policy 结果都保存成交数量、买卖费用、净收益、MAE、MFE、等待交易日和 `unevaluable_reason`。旧 Markdown 正则提取器只能生成 `legacy_unverified` 对照，不得进入新策略统计。

回测事件链：

```text
计划生成 -> 有效期内触达 -> 技术确认 -> 下一根可成交价格入场
         -> T+1 后先止损/先目标/超时退出
```

触达率、可成交率、确认率和确认后胜率分开统计，同时记录期望收益、MAE、MFE、最大回撤、等待时间和过期率。参数采用 walk-forward/独立样本多目标验收，不能只优化胜率或触达率。

## 15. 开源调研结论

正式运行尽量只依赖仓库已有的 Pandas、NumPy、K 线、筹码分布、缠论结构和信号生命周期；外部库主要用于公式对照、离线寻参和差分验证。

| 项目 | 可借鉴能力 | 结论 | 主要原因 |
| --- | --- | --- | --- |
| [TA-Lib](https://ta-lib.org/) | MA、EMA、ATR、布林带等标准指标 | 测试对照，不作 V1 必需运行依赖 | BSD；成熟，但仓库已有同类公式，直接替换会扩大部署和口径漂移风险 |
| [Pandas TA Classic](https://github.com/xgboosted/pandas-ta-classic) | Pandas 风格指标集合 | 研究环境备选对照 | MIT；不能替代事件锚定 AVWAP 和计划语义 |
| [SciPy `find_peaks`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.find_peaks.html) | 按 prominence、distance、width 识别峰谷 | 摆动点差分验证 | 噪声会改变峰值位置，正式锚点必须增加右侧确认窗口 |
| [trendln](https://github.com/GregoryMorse/trendln) | 局部极值和趋势线 | V2 消融实验 | 参数较多，实时因果性和稳定性需单独验证 |
| [Market Profile](https://github.com/bfolkens/py-market-profile) | 从 OHLCV 估算 Volume Profile | 只作研究参考 | 日线 OHLCV 无法还原真实日内价量分布 |
| [scikit-learn DBSCAN](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.DBSCAN.html) | 密度聚类 | 聚类基准，不增加正式依赖 | 单股锚点少且是一维，自研 ATR 连通聚类更确定、可解释 |
| [Optuna](https://github.com/optuna/optuna) | 多目标、约束式参数优化 | 推荐离线校准 | MIT；只搜索候选参数，不接触实时决策和测试集选参 |
| [ruptures](https://github.com/deepcharles/ruptures) | 离线变点检测 | 不进 V1 | 全窗口运行容易把未来信息带回历史锚点 |
| [VectorBT](https://github.com/polakowo/vectorbt) | 大规模向量化扫描 | 可选粗筛工具 | Apache 2.0 + Commons Clause，且不能直接表达完整生命周期 |
| [Backtesting.py](https://github.com/kernc/backtesting.py) | 独立事件回测 | 可选差分回测器 | AGPL-3.0；正式结果仍以项目事件回测器为准 |
| [skfolio](https://github.com/skfolio/skfolio) | Walk Forward、Combinatorial Purged CV | 借鉴验证方法 | 更偏组合优化，本项目需按锚点事件执行 as-of 回放 |

开源许可证不是当前非商用项目的排除条件，但研究依赖仍须锁定版本并记录许可证。TA-Lib 对照测试使用 `pytest.importorskip`，不进入默认 CI 阻断路径；可提交固定对照样本覆盖核心公式。

## 16. 实现结构

```text
src/agent/pricing/
  schemas.py        # Snapshot、Anchor、ZoneScenario、三个 artifact 和读模型
  params.py         # 市场/周期参数与版本注册
  fingerprint.py    # 规范 JSON 与分层 SHA-256
  snapshot.py       # IO、as_of 截断、复权/现金口径、缺失标记
  anchors.py        # 纯函数锚点生成、方向、反应、破坏和新鲜度审查
  clustering.py     # 来源族去相关和 ATR 连通聚类
  plan.py           # 止损、目标、风险收益比、有效期和 artifact 组装
  confirmation.py   # 生命周期与回测共享的触达、形态、量能和失效谓词
  market_rules.py   # tick、涨跌停、停牌、T+1 和可成交性
  decision.py       # LLM 权限矩阵、账户决策与最终状态纯函数
  account_cost.py   # 整手委托、账户费用与真实净风险收益比
  lifecycle.py      # 事件账本和 PlanLifecycleState 物化
  repository.py     # 不可变 artifact 与事件账本持久化
  backtest.py       # 结构化计划到现有回测服务的适配
  readiness.py      # Schema、参数、制度元数据和指纹自检
  report.py         # 只从 TradePlanResult 渲染价格计划
  runtime.py        # 单股链路唯一公开入口
```

唯一公开入口始终返回 `TradePlanResult`；失败通过 `generation_status=no_plan`、`execution_status` 和非空 `reason_codes` 表达，不使用另一套松散返回类型。任何消费方不得重新从报告、Debate、Prompt 或 Trace 文本提取价格。

## 17. 正式接管、开关与回滚

已新增 `AGENT_DETERMINISTIC_PRICE_PLAN_ENABLED`，默认 `true`；未配置也直接使用确定性计划，但入口必须先通过 `PricePlanReadinessGate`。自检验证数据库表、参数注册表、精确日期的已验证 MarketRules 身份元数据和指纹稳定性；真正的前收盘、涨跌停价、停牌、价格尺度与历史根数继续由快照和纯判决器硬校验。任一项失败时返回 `system_unready` + `SYSTEM_NOT_READY`，不生成任何价格，也不回退到 LLM。

设为 `false` 时：

- 停止生成和修订所有新价格计划；
- 报告明确显示“确定性价格计划已停用”，不输出入场、止损和目标位；
- 不创建新 `EntrySignal`，旧计划和 Signal Preview 保持只读；
- 绝不恢复 LLM 或 Markdown 补价。

启用开关不能绕过 readiness gate；迁移未完成、参数版本缺失或自检失败时，服务仍可提供不含交易价格的分析报告，但必须显式显示“确定性价格计划未就绪”及原因。正式发布验收必须覆盖“默认配置启动”和“旧数据库启动”两种场景。

新增 `AGENT_PRICE_PLAN_DISABLED_ANCHOR_FAMILIES` 作为局部隔离开关，默认空。发现某个来源族错价时可暂时移除该族并升级运行诊断；剩余来源族仍须满足两族门槛，否则返回 `NO_PLAN`。配置落地时同步更新 `.env.example`。

接管状态：

| 项目 | 状态 |
| --- | --- |
| 数据库迁移、严格 Schema、参数注册表、MarketRules 与 readiness gate | 已实现 |
| 快照、指纹、锚点、聚类、逐场景计划、评审与账户决策 | 已实现 |
| 生命周期、Signal Preview、报告和 Risk Gate 读取同一 artifact | 已实现 |
| `deterministic_price_plan` 结构化回测、T+1、停牌和涨跌停边界 | 已实现确定性回归；分钟级冲突回放尚未接入 |
| 新单股链路切断 LLM/Markdown 补价，旧 Trace 保留历史只读 | 已实现 |
| 历史 Trace as-of 回放、训练窗口校准、独立样本与 walk-forward 验收 | 待执行，是发布质量门槛 |

全局回滚的产品代价是暂时完全失去新入场区间、止损和目标位，而不是恢复旧能力。正式监控至少包含 `NO_PLAN` 比例和原因、来源族隔离、候选区覆盖率、触达/成交/确认/过期率、止损率、错价告警及报告/计划一致率。

## 18. 旧路径改造结果与兼容边界

| 旧问题 | 当前处理 |
| --- | --- |
| Analyzer Prompt 要求 LLM 给出买点、止损和目标 | 已改成 `N/A（由确定性价格引擎后处理）`；若 LLM 仍输出价格，评审记 `LLM_PRICE_AUTHORITY_VIOLATION` 并清洗结构化价格字段 |
| `submit_trade_plan` 允许 LLM 提交价格计划 | 已从默认注册表移除；兼容导入壳固定返回失败，`ALL_SIGNAL_TOOLS` 为空，Executor/Orchestrator 不消费历史工具结果 |
| 理想区与次优区整体取 `min/max` | `_extract_trace_entry_zone()` 已移除；新链路逐个保存 `scenario_id` 和边界 |
| 从 Debate 读取 LLM 止损与仓位 | 价格 fallback 已移除；账户仓位只从 `position_sizing.v1` 与确定性下调链路产生 |
| 回测从 Markdown 正则提价 | 新 `deterministic_price_plan` 策略读取持久化 artifact；旧正则仅服务历史 `strict_ai_entry` 对照，不能生成新计划或进入新策略统计 |
| 旧 `TradePlan` 单目标、自由文本有效期、`EntrySignal` 无计划引用 | 新严格对象和数据库表已落地；旧对象只接受从 `TradePlanResult` 的单向兼容投影，`EntrySignal` 已增加 `plan_id/scenario_id` |

新 Trace 必须带 `source=deterministic_price_plan`、`plan_id` 和 `trade_plan_result`。报告、Trace、Risk Gate、Signal Preview 或回测若缺少同一 artifact 引用，应按链路故障处理，不能从正文重建价格。

## 19. V1 参数与离线发布门槛

代码使用不可变 `price-plan-params.v1`，当前运行值如下。它们是可复现基线，不是已经证明最优的投资参数。

| 参数组 | V1 值 |
| --- | --- |
| 历史窗口 | 日线 `520+200` 预热；周线 `60+40` 预热；ATR `14` |
| 聚类/区间 | gap `0.35 ATR`；簇宽 `0.70 ATR`；padding `0.10 ATR`；区间最大 `1.00 ATR` |
| 破坏/反应 | 连续破坏 `2` 日，buffer `0.20 ATR`；触达 `0.20 ATR`；10 日内反应 `0.80 ATR`；相邻反应间隔 `8` 日 |
| 新鲜度 | 筹码 `20`、事件 `120`、结构 `120` 个交易日 |
| 止损/目标/RR | 止损 buffer `0.25 ATR`；最大止损距离 `2.50 ATR`；目标间距 `0.50/1.00 ATR`；最低参考净 RR `1.20` |
| 参考成本 | 买/卖成本各 `8 bps`，买/卖滑点各 `5 bps` |
| 可达性 | near `<=1.0 ATR`；medium `<=2.5 ATR`；否则 far |
| 时效 | 确认 `5` 个有效交易日；确认后委托 `2` 个有效交易日；触达确认窗口 `2` 日 |
| 形态量能 | 量均线 `5` 日；锤子下影/实体 `2.0`、上影/实体 `1.0`、收盘位置 `0.60`；缩量 `0.80`、放量 `1.20`、破位放量 `1.50` |

参数校准只允许在训练窗口进行；固定留出验证窗口和滚动 walk-forward 窗口，任何后续参数包必须升级版本，并记录样本范围、目标函数、最小样本量、入选理由和独立样本结果。小样本时不自动调参，保持上一版参数并返回覆盖率告警。正式对外宣称策略有效前，必须完成第 20 节验收；未完成时只能把本模块描述为“确定性、可回放的价格计划引擎”，不能描述为已验证盈利模型。

## 20. 对抗性验收与静默失败检测

以下检查必须进入自动化测试和运行诊断。任何一项失败都要写入结构化 `diagnostics`，并按表返回状态；不能只在日志中打印后继续生成价格。

| 场景 | 必须检测 | 预期结果 |
| --- | --- | --- |
| 盘中请求或未收盘日线 | `actual_as_of_session` 与请求时间/交易日历校验 | 截断到上一已收盘日，记录 `AS_OF_ROLLED_BACK` |
| 复权因子缺失、方向未知或 OHLC 关系异常 | 逐日因子覆盖、语义、现金价误差和正值校验 | `generation_status=no_plan` + `PRICE_SCALE_UNVERIFIED` |
| 历史筹码只有当前快照 | `observed_at <= as_of` 且指定日期可取 | 禁用筹码锚点并记录 `MISSING_HISTORICAL_CHIP_SNAPSHOT`，不使用未来数据 |
| 日/周线根数或 EMA/MACD 预热不足 | 实际根数与参数包最小值比较 | `no_plan` + `INSUFFICIENT_*_HISTORY` |
| 两个来源族共振但区间宽度超限 | 重新计算 padding 后的完整宽度 | 只保留观察场景并记录 `ZONE_WIDTH_EXCEEDED`，不裁锚点凑交易区 |
| 当前价低于区间下沿但高于止损 | `price_relation=below`，禁止 `max(0, distance)` | `ReachabilityBand=unknown`，不得显示 `near` |
| 最近阻力过近 | 先选最近阻力，再检查 `min_target_gap_atr` | `TARGET_TOO_CLOSE`，禁止跳过阻力选远目标 |
| LLM 返回价格、自由文本或 JSON 解析失败 | Schema 严格校验和价格字段 diff | 丢弃价格字段，评审 `failed/observe_only`，数学价格不变 |
| 账户费率、手数或真实净盈亏比缺失 | `AccountCostPolicy` 和 lot rounding 校验 | 保留参考计划，`risk_blocked` 或标记账户净值不可评估，不改价格 |
| Risk Gate 再次乘 review/risk multiplier | 检查 `RiskGateInput.position_cap` 与 clamp 检查 | 失败；只允许一个 `COMPOSITE_VERDICT_CEILING_APPLIED` 检查 |
| 同一请求重复执行或并发盘后任务 | plan/review/decision/event 唯一键与 `state_version` | 返回已有 artifact 或重放冲突，不重复创建信号 |
| 两个不连续入场区间 | `scenario_id`、逐场景边界和逐场景 stop/targets | Signal Preview、回测和旧投影都不能整体 `min/max` |
| 同一日线同时触达和失效 | 日线 OHLC 歧义 + 可用分钟数据检查 | 入场前 `ambiguous/UNRESOLVED`；入场后止损优先并记录 `ambiguous_bar` |
| 新旧链路同时产出价格 | Trace 检查 `price_source` 和 artifact 引用 | 新链路只允许 `math_artifact`，LLM/Markdown 提取命中即失败 |

每次请求和回放至少输出：`plan_id`、`scenario_id`、`actual_as_of_session`、五类指纹、`price_source`、来源族覆盖、缺失原因、生命周期事件版本、clamp 检查和最终 `execution_status`。监控按这些字段聚合，才能区分“没有信号”和“系统没有算出来”。

正式启用前必须满足：

- 无未来数据泄漏，且数学层同快照完全可复现；
- 复权计算价能稳定映射到现金可成交价；
- 异常数据稳定返回带原因的 `NO_PLAN`，没有静默 fallback；
- 报告、严格计划、Risk Gate、Signal Preview、生命周期和回测引用同一 `plan_id`；
- 不连续区间始终独立保存和逐区间判断；
- 独立样本的多目标指标达到校准时冻结的验收门槛；
- 关闭全局开关和隔离任一来源族的行为均经过测试。
