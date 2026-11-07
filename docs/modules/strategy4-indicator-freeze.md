# 策略 4 指标口径冻结（strategy4.v1）

## 1. 版本定位与免责说明

本文冻结 `individual-stock-composite-scoring-system.md` 第 2 节所依赖的策略 4 指标口径。

**重要前提**：本仓库不存在策略 4 的原始公式源（通达信/同花顺自编公式、原始策略文档均不可得）。全仓库检索 `策略4`、`strategy_4`、`指导线`、`MA57`、`买线`、`卖线`、`all_ok` 只命中综合评分文档与 `src/agent/scoring/` 自身，没有可复用的同源实现。

因此本文**不是对原始策略 4 的还原**，而是一套重新定义的、自洽且可回放的口径。所有下游评分、回测、仓位结果都绑定 `rule_version = strategy4.v1`；若日后取得原始公式，必须新开 `strategy4.v2` 并重跑影子回放，不得原地覆盖 v1 语义。

| 项 | 值 |
| --- | --- |
| 口径版本 | `strategy4.v1` |
| 冻结日期 | 2026-08-05 |
| 口径来源 | 本仓库自定义（无原始策略源） |
| 关联评分版本 | `stock-composite-shadow-v1` |

## 2. 通用计算约定

以下约定对本文所有公式生效，实现时不得逐项重新解释。

| 约定项 | 冻结值 |
| --- | --- |
| 数据来源 | 复用 `CompositeSnapshotBuilder` 已加载的本地缓存日线，禁止评分阶段联网兜底 |
| 复权口径 | 沿用快照 `metadata.price_adjustment_basis`；`unknown` 时照常计算，但快照标记 `data_quality = limited` |
| as-of 边界 | 所有序列先按 `as_of` 截断，再计算指标；不得使用 `as_of` 之后的任何 bar |
| 取值位置 | `[t]` 表示截断后最后一根 bar，`[t-n]` 表示往前第 n 根 |
| 浮点比较 | 指标值保留 6 位小数（`round(x, 6)`），布尔判定直接用比较运算符，不引入额外容差 |
| 边界取值 | 阈值一律**闭区间**：`>=` / `<=`，与评分文档 §2.2 保持一致 |
| 缺失语义 | 数据不足时该特征取 `None` 并写入 `missing_features`，禁止用 `0` 或 `False` 代替缺失 |
| EMA 预热 | 沿用 `EMA_WARMUP_BARS = 200`，预热不足时 EMA 系列特征记为缺失 |

## 3. 核心线定义

### 3.1 指导线（guide_line）

```text
guide_line[t] = EMA(close, 13)[t]
```

选 EMA(13) 的理由：13 与 57 是同一参数族（13 ≈ 57/4.4，接近常见的 13/34/55 斐波那契族），且"指导"在中文技术分析语境中通常指跟随价格更紧的短期趋势线。用 EMA 而非 SMA，是为了让金叉判定与 MACD 族口径一致。

最小 bar 数：`13 + 200`（EMA 预热）。不足时 `guide_line = None`。

### 3.2 MA57

```text
ma57[t] = SMA(close, 57)[t]
ma57_rising[t] = ma57[t] > ma57[t - 3]
```

用简单移动平均，不用 EMA——MA57 在本体系里承担"中期区域基准"角色，SMA 对单日异动更钝，更适合当基准线。

`ma57_rising` 取 3 日窗口而非 1 日，是为了避免 MA57 在横盘时因单日抖动反复翻转导致 `region_pass` 闪断。最小 bar 数：`57 + 3 = 60`。

### 3.3 买线（buy_line）与卖线（sell_line）

```text
dif[t]  = EMA(close, 12)[t] - EMA(close, 26)[t]
dea[t]  = EMA(dif, 9)[t]

buy_line[t]  = dif[t]
sell_line[t] = dea[t]
```

即标准 MACD(12, 26, 9) 的快线与慢线。选它的理由：缠论侧的背驰/钝化判定已经在用 MACD 柱（见 `price_structure.py`），买卖线复用同一套 EMA 基座可以保证"金叉"和"背驰"不会因为两套指标基座而互相矛盾。

最小 bar 数：`26 + 9 + 200`（预热）。

> 命名保留"买线/卖线"是为了对齐评分文档表述；实现中字段名用 `buy_line` / `sell_line`，并在 docstring 注明等价于 DIF / DEA。

## 4. 多周期共振（日多 / 日强 / 周多 / 周强）

### 4.1 四项判定

```text
日多 daily_bull   = close_d[t]      >  guide_line_d[t]
日强 daily_strong = guide_line_d[t] >  ma57_d[t]
周多 weekly_bull  = close_w[t]      >  guide_line_w[t]
周强 weekly_strong= guide_line_w[t] >  ma57_w[t]

resonance_count = sum([日多, 日强, 周多, 周强])   # 0 ~ 4
```

语义分工：`多` 判断价格相对短期趋势线的位置（多头排列），`强` 判断短期趋势线相对中期基准的位置（趋势强度）。日线与周线用**完全相同的公式**，只换输入序列，这样共振才是同一口径在两个周期上的一致性检验，而不是四个互不相干的条件。

### 4.2 周线序列口径

周线复用 `_resample_weekly(daily)` 的现有实现（按自然周聚合，`open` 取首、`high` 取最大、`low` 取最小、`close` 取末、`volume` 求和）。

- 周线指标在**重采样后的序列**上计算，不是日线指标的周末采样。
- 未收盘周（`weekly_last_bar_complete = false`，即 `as_of` 非周五）的最后一根周 bar **参与计算**，但 `weekly_bull` / `weekly_strong` 标记 `provisional = true`，且快照 `s4.status` 降为 `partial`。
- 判定 `weekly_last_bar_complete` 沿用现有 `last_date.weekday() == 4`；节假日导致的周中收官不额外特判（已知偏差，记在 §8）。

### 4.3 数据量硬约束（阻塞项）

周线 MA57 需要 **57 根周 bar + 3 根上升判定窗口 = 60 根周 bar**，约合 **300 个交易日**；叠加 EMA 预热后需要更多。

当前 `DAILY_LOOKBACK_BARS = 260` **不足以支撑周强**，会让 `weekly_strong` 永久为 `None`、`resonance_count` 永久缺失，进而 `s4_score` 永远算不出来；因此 v1 实现固定使用 520 根。

冻结决定：

```text
DAILY_LOOKBACK_BARS: 260 -> 520
```

520 个交易日约合 104 周，可覆盖 `57 + 3` 周线窗口。经评审确认，200 根 EMA 预热按日线统一口径换算为 40 根周线预热；代码必须同时记录两套实际/要求数量，不能把 104 周伪装成 200 周预热。这是启用 `resonance_count` 的**前置改动**，必须与本口径同批落地。

| 特征 | 最小日线 bar 数 |
| --- | ---: |
| `daily_bull` / `daily_strong` | 200 |
| `weekly_bull` | 约 250 |
| `weekly_strong` | 约 500 |
| `golden_cross_today` | 200 |

不足时对应特征取 `None`，并按 §2 写入 `missing_features`；不足 200 根时 `s4.status = not_implemented`，周线未收盘或周线特征不完整时为 `partial`。

## 5. 今日金叉与 all_ok

### 5.1 今日金叉

```text
golden_cross_today =
      buy_line[t]   >  sell_line[t]
  AND buy_line[t-1] <= sell_line[t-1]
```

严格要求"今日发生穿越"，只有 `buy_line > sell_line`（已在金叉状态中）不得分。这样第 6 项才是时点确认分，不与"区域/共振"重复计分。

### 5.2 all_ok

```text
all_ok =
      region_pass
  AND (resonance_count >= 3)
  AND (volume_ratio_5d  >= 1.2)
  AND (volatility_pct   <= 6)
  AND (gain_10d_pct     <= 9)
```

即**除"今日金叉"以外的其余 5 项全部通过**。定义成"其余 5 项"而不是"全部 6 项"，是因为评分文档 §2.2 里金叉分的通过标准写的是「买线上穿卖线，且 `all_ok`」——若 `all_ok` 包含金叉自身会构成循环定义。

`all_ok` 只在 5 项**全部非缺失**时才有值；任一项缺失则 `all_ok = None`。

### 5.3 区域（region_pass）

```text
region_pass = (guide_line[t] > ma57[t]) AND ma57_rising[t]
```

与评分文档 §2.2「`指导线 > MA57` 且 `MA57` 上升」一致。注意 `region_pass` 与 `daily_strong` 的前半段同式——这是有意的：区域分看的是"强势区 + 中期基准向上"，共振看的是多周期一致性，二者共享一个不等式但语义不同，不视为重复计分。

## 6. s4_score 计算

```text
s4_score =
    region_pass                    * 2
  + (resonance_count >= 3)         * 2
  + (volume_ratio_5d >= 1.2)       * 2
  + (volatility_pct  <= 6)         * 2
  + (gain_10d_pct    <= 9)         * 1
  + (golden_cross_today && all_ok) * 1
```

- 布尔为真取 1，否则取 0；结果 clamp 到 `[0, 10]`。
- 任一输入特征为 `None` 时 `s4_score = None`（沿用 `_score_s4` 现有的 `missing -> None` 行为），不得按 0 分计。
- `volume_ratio_5d`、`volatility_pct`、`gain_10d_pct` 已由 `composite_snapshot.py` 实现，本次不改口径。

## 7. 回测胜率口径（backtest_win_rate.v1）

### 7.1 不能复用现有 win_rate_pct

`src/core/backtest_engine.py:267` 的 `win_rate_pct` 统计的是**历史报告 `operation_advice` 的方向命中率**，样本单位是"一份报告建议"。评分文档 §5 需要的是**策略 4 信号触发后的盈亏胜率**，样本单位是"一次信号"。两者样本域、持有期、结算方式都不同，直接复用会得到无意义的降级判定。

因此需要新建信号级回测，冻结参数如下。

### 7.2 冻结参数

| 参数 | 冻结值 | 理由 |
| --- | --- | --- |
| 信号定义 | `golden_cross_today && all_ok`（即 s4 第 6 项得分的条件） | 这是策略 4 唯一的时点入场信号 |
| 回测窗口 | 截至 `as_of` 的近 3 年，滚动 | A 股风格切换快，5 年会混入异质 regime |
| 持有期 | 信号次日开盘买入，持有 10 个交易日后收盘卖出 | 与 `gain_10d_pct` 同尺度，便于交叉校验 |
| 最小样本数 | 20 笔 | 低于此返回 `insufficient_sample` |
| 手续费 | 买入 0.025%，卖出 0.025% + 印花税 0.05% | 按现行 A 股实际费率 |
| 滑点 | 单边 0.1% | 保守估计，宁可低估胜率 |
| 胜负判定 | 扣除费用与滑点后净收益 `> 0` 记胜；`= 0` 记负 | 平盘不计胜，避免虚高 |
| 复权口径 | 后复权 | 持有期跨除权时结算才正确 |

```text
win_rate_pct = 胜出笔数 / 总笔数 * 100
```

### 7.3 样本不足的处理（关键）

评分文档 §5 写的降级条件是「回测胜率 `< 40%`」。样本不足既不满足 `< 40%` 也不满足 `>= 40%`，必须走**第三条分支**：

```text
if sample_count < 20:
    backtest_win_rate_status = "insufficient_sample"
    win_rate_pct = None
    -> 不触发降级，但在 adjustments 中记录 reason = "backtest_sample_insufficient"
elif win_rate_pct < 40:
    -> 降级为 买入-历史胜率低
else:
    -> 不降级
```

漏掉这一分支会让新股、次新股和冷门票被无声降级。降级与否必须显式写入 `adjustments`，便于审计。

### 7.4 A 股特有排除规则

以下 bar 不产生信号样本（会导致买不进或结算失真）：

- 信号次日开盘一字涨停（`open == high == low` 且涨幅达停板）
- 信号次日停牌（`volume == 0`）
- 上市不满 60 个交易日的标的整体不参与回测

## 8. 仓位映射与大盘系数（position_sizing.v1）

### 8.1 S/A/B 档位

依赖 §4 的 `resonance_count`，因此本节必须在 §4 落地后才能启用。

| 档位 | 条件 | 基础仓位 |
| --- | --- | ---: |
| S | `resonance_count == 4` 且 `volume_ratio_5d >= 1.5` | `0.20` |
| A | `resonance_count == 4` 且 `volume_ratio_5d >= 1.2` | `0.15` |
| B | 其他 `all_ok` 为真的情况 | `0.10` |
| 无 | `all_ok` 为假或缺失 | `None` |

按 S -> A -> B 顺序**首次命中即定档**。内部统一用小数表示（`0.20` 而非 `20`），仅在格式化输出时转百分比。

### 8.2 大盘系数

绑定 `src/agent/regime.py` 的 `detect_market_regime` 输出，做确定性查表，**不允许由 LLM 给系数**：

| `MarketRegime` | 附加条件 | 大盘系数 |
| --- | --- | ---: |
| `trending_up` | `VolatilityBucket` ∈ {`very_low`, `low`, `normal`} | `1.00` |
| `trending_up` | 其他波动档 | `0.80` |
| `range_bound` | — | `0.60` |
| `event_driven` | — | `0.50` |
| `high_volatility` | — | `0.40` |
| `trending_down` | — | `0.35` |
| `risk_off` | — | `0.30` |
| `panic` | — | `0.25` |
| `unknown` | — | `0.25` |

`unknown` 取下限而非中值：缺数据时必须最保守。系数范围 `0.25 ~ 1.00`，与评分文档 §7.2 的约束一致。

### 8.3 最终仓位

```text
target_position_cap_ratio = min(base_position * market_coefficient, SINGLE_NAME_CAP)

target_position_cap_pct = target_position_cap_ratio * 100
SINGLE_NAME_CAP = 0.20
```

- clamp **只执行一次**，由调用方在构造 `RiskGateInput` 前完成，并把截断前仓位与 `clamp_applied` 一并传给 Risk Gate 的只读审计 check，避免双份截断逻辑。
- `base_position` 或 `market_coefficient` 任一缺失时两个仓位字段都为 `None`。
- 内部计算统一使用比例（例如 `0.20`）；`RiskGateInput` 仍使用百分比（例如 `20.0`），边界转换只做一次。

### 8.4 持仓减仓目标

评分文档 §5 只给了 `required_holding_action = reduce` 的方向，未给幅度。冻结为：

```text
level = 3 且 holding = true
  -> required_holding_action = "reduce"
  -> reduce_to_ratio = 0.50    # 减至当前仓位的 50%
```

影子模式下该字段仍不直接执行交易。

## 9. 已知偏差与不做的事

1. 周线完成度用 `weekday() == 4` 判定，节假日导致的周中收官会被误判为未完成周，此时周线特征被标为 `provisional`，属于保守方向的偏差，v1 接受。
2. 指导线、买线、卖线的参数（13 / 12,26,9）是本文自定义选择，未经原始策略验证；影子回放若出现分层不单调，优先回到本节调参而不是改评分权重。
3. 不引入可配置化开关：本口径全部硬编码在 `strategy4.v1` 常量中。让阈值可配会使影子回放结论无法归因。
4. 不改动 `chan_score` 侧任何口径。
5. 逃顶预警剩余 5 类信号（顶分型、放量滞涨、买卖线收敛、超买、独立二次确认）不在本文范围；其中"买卖线收敛"依赖本文 §3.3 的买卖线定义，可在本口径落地后单独冻结。

## 10. 落地顺序与验收

1. 本文档评审定稿。
2. `DAILY_LOOKBACK_BARS` 260 -> 520，周线 EMA 预热使用 40 根换算值，确认取数与缓存开销可接受。
3. 造 fixture：3-5 只标的（建议覆盖主板/创业板/次新）各 520 根日线 CSV 存入 `tests/fixtures/`，人工算出各特征期望值写进测试。
4. 实现 §3~§6，从 `S4_UNIMPLEMENTED_FEATURES` 中逐个移除已实现特征；`s4.status` 可升为 `ok`。
5. 实现 §8（依赖第 4 步的 `resonance_count`）。
6. 实现 §7（仅影响降级修正，不阻塞 `score` 脱离 `null`）。
7. 影子回放验收，全部满足后才允许把 `shadow_mode` 切为执行约束：
   - **分层单调性**：`18~20` 分组的 10 日净收益中位数显著高于 `6~9` 分组，且各档位单调不倒挂。
   - **覆盖率**：`verdict_status = evaluable` 的标的占比 `>= 80%`；低于此说明数据量或缺失策略有问题。
   - **稳定性**：相邻交易日 `signal_level` 跳变率 `<= 15%`。

分层不单调时回到 §9 第 2 条调参，不得直接放开 `shadow_mode`。

## 11. 使用边界

本口径用于辅助个股筛选与仓位管理，不构成投资建议。股市有风险，投资需谨慎。
