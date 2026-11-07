# 个股综合评分体系（策略 4 + 缠论）

本文将图片版“个股综合评分体系（满分 20 分）”整理为可回放、可测试、可审计的规则说明，并记录 StockAnalyser 的实际接入状态。当前已接入 `strategy4.v1` 影子模式：策略 4 和缠论证据均可计算；评分、回测修正和仓位字段仍不改变交易动作，必须经过影子回放验收后才能解除影子约束。

## 1. 评分总览

```text
总分 = s4_score + chan_score

s4_score  ∈ [0, 10]
chan_score ∈ [0, 10]
总分       ∈ [0, 20]
```

- `s4_score`：策略 4 技术面筛选分。
- `chan_score`：缠论形态分析分。
- 评分完成后先映射为信号等级，再应用额外降级/升级逻辑。
- 逃顶预警系统独立于总分运行；预警可以直接降低操作等级或触发持仓处理建议。

### 1.1 当前实现边界

```text
price_structure（证据标签）
  -> CompositeSnapshotBuilder（取数、as_of 截断、缺失/provisional 标记）
  -> evaluate_composite_verdict(snapshot)（纯函数评分）
  -> 选股 deterministic_scoring stage / 单股 composite_verdict.json
  -> Risk Gate composite_verdict_ceiling 只读审计
```

- `src/agent/structure/price_structure.py` 只识别缠论证据，不给交易结论。
- `src/agent/scoring/composite_snapshot.py` 负责固定 520 根日线窗口、200 根日线 EMA 预热记录、40 根周线 EMA 预热记录、`as_of` 截断、周线重采样和缺失状态。
- `src/agent/scoring/strategy4.py` 统一计算指导线、MA57、DIF/DEA、日周共振、金叉和 `all_ok`，回测不得另行实现这些指标。
- `src/agent/scoring/composite_verdict.py` 是无 I/O 纯函数，负责去重、加减分和等级映射。
- 选股链路在单股深挖后生成 `deterministic_scoring` 阶段；单股 trace 在 Agent 推理前生成确定性评分证据并注入上下文，最终生成 `composite_verdict.json`，同步响应、流式响应和历史响应共用该 artifact。
- 单股 trace 的评分构建器要求完整 520 根历史窗口；本地缓存不足时允许联网补齐，且 Trace 页面展示 S4/缠论分项、实际 K 线数量与缺失原因。多候选选股阶段仍只复用本地缓存，避免候选数量放大网络请求。
- 当前 `shadow_mode = true`，Risk Gate 只记录 `composite_verdict_ceiling` check，`clamp_applied = false`。

### 1.2 单股目标一致性与失败可见性

单股 Trace 必须在评分前锁定唯一股票，目标解析顺序如下：

1. 请求中的显式股票代码。
2. 本地生成的股票名称索引；该步骤不联网，避免预评分被远端名称服务阻塞。
3. 用户问题中唯一命中的持仓股票名称。

系统禁止在目标不明确时回退到“第一只持仓”。解析结果写入 `context_summary.target_resolution`；显式名称无法解析、名称歧义或解析异常时，Strategy4 以 `target_unresolved` 失败状态进入 Trace，不能静默跳过。

执行阶段会校验请求上下文、工具调用和综合评分三处股票代码。任意错配都返回 `target_symbol_mismatch`，错误评分不进入 Risk Gate。历史 artifact 不做回填，但 API 读取时会再次校验：已存 Strategy4 或 Risk Gate 属于另一只股票时，将其只读隔离为失败结果，保留 `expected_symbol`、`actual_symbol` 和工具目标用于审计。

`partial`、`provisional` 和 `not_evaluable` 不等于没有证据。只要快照已生成，报告提示词和 Trace 页面仍展示区域、共振、量比、波动率、近 10 日涨幅、金叉、`all_ok`、K 线覆盖和缺失原因。Trace 还会对 Strategy4 每项给出阈值、当前值及通过/未通过原因，并对已触发缠论信号注明“计入买入分”“周线未收盘暂不计分”或“仅作为退出风险提示”，因此 `0` 分也必须能够逐项解释。

未收盘周线且全部必需特征已有值时，评分器同时输出 `provisional_score` 和 `provisional_breakdown`。正式 `score` 仍保持 `null`，Risk Gate 和信号等级继续只读取正式分数；Trace 页面用“临时评分”展示 `provisional_score`，不能把临时周线提升为正式结论。真正缺失特征时不生成临时分数，也不能把缺失写成 0 分。对于修复前已经落盘、但 snapshot 足以计算临时分数的历史 Trace，API 只在读取响应中补充这两个临时字段并标记 `read_time_enrichment.persisted = false`，不改写 artifact，也不重新计算或覆盖正式结论。

## 2. 策略 4 评分（`s4_score`，满分 10 分）

### 2.1 组成

| 维度 | 分值上限 |
| --- | ---: |
| 区域 | 2 |
| 多周期共振 | 2 |
| 量比 | 2 |
| 波动率 | 2 |
| 近 10 日涨幅 | 1 |
| 今日金叉 | 1 |
| **合计** | **10** |

### 2.2 通过标准

| 维度 | 满分 | 通过标准 | 说明/计算口径 |
| --- | ---: | --- | --- |
| 区域 | 2 | 强势区 | `指导线 > MA57` 且 `MA57` 上升 |
| 多周期共振 | 2 | 满足 `>= 3/4` 项 | 日多 + 日强 + 周多 + 周强 |
| 量比 | 2 | `量比 >= 1.2x` | 当日量 / 5 日均量 |
| 波动率 | 2 | `<= 6%` | `ATR14 / MA20 * 100` |
| 近 10 日涨幅 | 1 | `<= 9%` | 防止追高 |
| 今日金叉 | 1 | 当日触发 | 买线上穿卖线，且 `all_ok` |

> 图片中的“今日金叉”通过标准按“买线上穿卖线”整理；“买线”和“卖线”的具体均线/指标定义需在接入前与现有策略 4 实现对齐。

策略 4 分数建议按“满足即得分”的方式计算：

```text
s4_score =
    region_pass       * 2
  + (resonance_count >= 3) * 2
  + (volume_ratio >= 1.2)  * 2
  + (volatility_pct <= 6)  * 2
  + (gain_10d_pct <= 9)    * 1
  + (golden_cross_today && all_ok) * 1
```

其中 `resonance_count` 是四个多周期条件中通过的数量；布尔值为真时取 `1`，否则取 `0`。实现时应将分数限制在 `[0, 10]`。

## 3. 缠论评分（`chan_score`，满分 10 分）

图片给出的计算关系为：

```text
缠论得分 = 分型/笔得分 + MACD 背驰/钝化得分 + 买点类型得分
```

各信号的基础加减分如下：

| 信号类型 | 得分 | 触发条件 |
| --- | ---: | --- |
| 一买（底背驰底分型） | `+5` | 最新向下笔价格创新低，且相对上一同向笔的 MACD 面积比 `<= 0.8` |
| 二买（更高低点） | `+4` | 前一底之上的次低点 |
| 三买（中枢突破） | `+2` | 最新中枢结束后的第一根向上笔，其终点高于 `ZG` |
| 底部钝化 | `+2` | 绿柱连续缩短（空头动能衰竭） |
| 周线买点确认 | `+1` | 已完成周线命中一买、二买或三买 |
| 顶背驰（惩罚） | `-3` | 最新向上笔价格创新高，且相对上一同向笔的 MACD 面积比 `<= 0.8` |
| 顶部钝化（惩罚） | `-2` | 红柱连续 4 根缩短 |

实现约束：

1. 先计算所有命中的加分和惩罚，再将结果限制到 `[0, 10]`，得到 `chan_score`。
2. V1 按信号名称全局去重，同名信号跨周期只计一次；周线确认使用独立的 `weekly_confirmation` 名称。
3. 图片区间在 V1 固定为保守端点：一买 `+5`、三买 `+2`、周线确认 `+1`、顶背驰 `-3`、顶部钝化 `-2`。
4. 未收盘周线信号保留为 `metrics.provisional = true` 的证据，但不参与评分；Chan 组件状态记为 `partial`。
5. 信号保存 `name`、`triggered`、`timeframe`、`metrics` 和证据，确保分数可解释、可回放。
6. 历史数据不足时 `chan_score = null`，不能用 0 分代替缺失。

## 4. 综合分数与信号等级

`total_score = s4_score + chan_score`，最高 20 分。

| 总分区间 | 信号等级 | 信号描述 | 操作建议 |
| ---: | --- | --- | --- |
| `18 ~ 20` | 强确认买入 | 买入，强确认 | 满格建仓 |
| `15 ~ 17` | 有效买入 | 买入，有效（缠论二买/三买确认） | 正常建仓 |
| `10 ~ 14` | 观察买入 | 买入，观察（回测数据不足/无缠论） | 半仓试探 |
| `6 ~ 9` | 过渡买入 | 买入，过渡（有金叉但条件未全过） | 观望 |
| `0 ~ 5` | 观望/逃顶 | 观望，或触发逃顶预警 | 不操作/出场 |

边界应采用闭区间判断，避免分数落在相邻等级之间：`18` 归入强确认买入，`15` 归入有效买入，依此类推。

强确认不是单纯区间映射，V1 使用显式合取门槛：

```text
total_score >= 18 AND first_buy AND s4_score >= 8
```

一买已经包含底背驰条件，不再重复要求“一买 + 底背驰”。达到 18 分但不满足上述合取条件时，降为 `buy_valid`。

## 5. 额外修正逻辑（信号降级/升级）

这些规则独立作用于综合分数映射结果。图片明确标注“降级条件（即使评分高也降级）”。

| 条件 | 原始动作 | 修正动作 |
| --- | --- | --- |
| 缠论顶背驰 + 其他信号 | 买入 | `买入-顶部风险` |
| 回测胜率 `< 40%` | 买入 | `买入-历史胜率低` |
| 逃顶预警为高危（`level = 3`）且已有持仓 | 强制处理 | `逃顶预警-建议今日减仓` |

建议修正优先级从高到低为：

1. `level = 3` 且持仓：优先执行持仓风险动作。
2. 顶背驰或回测胜率不足：保留评分结果，但降低可执行信号等级并增加原因。
3. 其他情况下使用第 4 节的总分等级。

“降级”不应修改原始 `s4_score`、`chan_score` 和 `total_score`，应通过单独的 `adjustments`/`effective_signal` 字段表达，便于审计。

当前 V1 已接入信号级回测结果和低胜率修正记录，但 `adjustments[].applied` 固定为 `false`；持仓标的命中 `level = 3` 时输出 `required_holding_action = reduce` 和 `reduce_to_ratio = 0.50`，同样不直接执行交易。

## 6. 逃顶预警系统（7 类信号，独立于评分）

### 6.1 预警等级

| 等级 | 标签 | 含义 |
| --- | --- | --- |
| `level = 1` | 提示 | 单个轻微信号 |
| `level = 2` | 中危 | 顶分型、顶背驰、顶部钝化、放量滞涨、买卖线收敛、超买等信号中出现一项 |
| `level = 3` | 高危 | 缠论顶背驰与其他信号叠加 |

### 6.2 预警项与触发标准

| 预警项 | 触发标准 |
| --- | --- |
| 顶分型 | 最近 3 根 K 线出现顶分型（高点被两侧包夹） |
| 顶背驰 | 价格创新高，但 MACD 柱面积缩减 `> 20%` |
| 顶部钝化 | 红柱连续 4 根递减（多头动能衰竭） |
| 放量滞涨 | `量比 >= 2x`，但当日涨幅 `< 1%`（主力出货） |
| 买卖线收敛 | 差距较 5 日前缩小 `> 50%`（即将死叉） |
| 超买 | 近 10 日涨幅 `> 15%`；超过 `20%` 时升为警告 |
| 缠论顶背驰确认 | 缠论模块二次确认顶背驰 |

同一预警项应保留触发时间、周期、实际指标值、阈值和证据 K 线范围。`level` 是预警系统的输出，不应混入 `chan_score` 的加减分。

当前 structure 层只会直接生成顶背驰和顶部钝化；顶分型、放量滞涨、买卖线收敛、超买和独立二次确认仍待实现。因此 V1 的逃顶预警是部分能力，不应解释为 7 类信号已经全部上线。

## 7. 仓位计算（评分到实际操作金额）

### 7.1 信号等级与基础仓位

图片给出的基础仓位还需要结合“共振”和“量比”决定：

| 仓位档位 | 条件 | 基础仓位 |
| --- | --- | ---: |
| S 级 | 共振 `4/4` 且量比 `>= 1.5x` | `20%` |
| A 级 | 共振 `4/4` 且量比 `>= 1.2x` | `15%` |
| B 级 | 其他通过情况 | `10%` |

### 7.2 最终仓位

```text
最终仓位 = 基础仓位 * 大盘系数
最终仓位 <= 单票上限
```

参数约束：

| 参数 | 范围/上限 |
| --- | --- |
| 基础仓位 | `10% ~ 20%` |
| 大盘系数 | `0.25 ~ 1.0` |
| 单票上限 | `<= 20%` |

大盘系数参考：

| 市场状态 | 大盘系数 |
| --- | ---: |
| 强势行情（放量上涨） | `0.8 ~ 1.0` |
| 震荡行情（缩量震荡） | `0.5 ~ 0.8` |
| 弱势行情（下跌或缩量阴跌） | `0.25 ~ 0.5` |

仓位计算应使用小数或百分比的统一内部表示，例如 `20%` 存储为 `0.20`，最后再格式化为百分比，避免 `20` 与 `0.20` 混用。

当前 V1 会计算 `target_position_cap_ratio`（内部比例）和 `target_position_cap_pct`（Risk Gate 边界百分比），但影子模式不截断交易计划。非影子模式下由调用方在构造 `RiskGateInput` 前只执行一次 clamp，并把截断前仓位和 `clamp_applied` 传给 Risk Gate 的只读审计 check，避免双份截断逻辑。

## 8. 建议的接入数据契约

### 8.1 当前快照字段

```json
{
  "symbol": "600519",
  "as_of": "2026-08-05",
  "quote_basis": "latest_trading_day",
  "data_quality": "sufficient",
  "holding": false,
  "s4": {
    "status": "ok",
    "region_pass": true,
    "resonance_count": 4,
    "volume_ratio_5d": 1.35,
    "volatility_pct": 4.8,
    "gain_10d_pct": 5.2,
    "golden_cross_today": false,
    "all_ok": true,
    "guide_line": 1680.1,
    "ma57": 1662.3,
    "ma57_rising": true,
    "buy_line": 12.4,
    "sell_line": 11.8,
    "daily_bull": true,
    "daily_strong": true,
    "weekly_bull": true,
    "weekly_strong": true,
    "missing_features": []
  },
  "chan": {
    "status": "ok",
    "signals": [
      {
        "name": "first_buy",
        "timeframe": "1d",
        "triggered": true,
        "metrics": {"macd_area_ratio": 0.72, "threshold": 0.8},
        "evidence": []
      }
    ]
  },
  "metadata": {
    "daily_lookback_bars": 520,
    "ema_warmup_bars": 200,
    "weekly_ema_warmup_bars_required": 40,
    "weekly_last_bar_complete": true
  }
}
```

字段命名可以适配现有 schema，但应保留以下语义：

- 原始指标值与是否通过分开保存，不能只保存布尔结果。
- 缠论信号只表达证据，分值由版本化 policy 统一决定，structure 层不保存交易结论。
- `metadata` 必须保留历史起止日、实际 K 线数、固定 lookback、EMA 预热根数、复权口径和周线是否完成。
- 回测胜率、市场系数和单票上限均进入 V1 快照；回测缺少元数据时保留 `unavailable/insufficient_data` 状态，不能用默认值替代。

### 8.2 输出字段

```json
{
  "verdict_status": "evaluable",
  "score": 14,
  "scale_max": 20,
  "components": {
    "s4": {"status": "ok", "score": 9, "scale_max": 10},
    "chan": {"status": "ok", "score": 5, "scale_max": 10, "breakdown": {"first_buy": 5}}
  },
  "signal_level": "buy_watch",
  "shadow_mode": true,
  "entry_permission": "conditional",
  "target_position_cap_ratio": 0.2,
  "target_position_cap_pct": 20.0,
  "position_tier": "S",
  "market_coefficient": 1.0,
  "required_holding_action": "none",
  "exit_warning": {
    "level": 0,
    "items": []
  },
  "backtest": {"status": "unavailable", "sample_count": 0},
  "adjustments": [],
  "missing_features": [],
  "strategy4_rule_version": "strategy4.v1",
  "rule_version": "stock-composite-shadow-v1"
}
```

当前 `signal_level` 枚举：

| 枚举 | 对应总分区间 |
| --- | ---: |
| `strong_confirmed` | `18 ~ 20` 且满足强确认合取门槛 |
| `buy_valid` | `15 ~ 17` |
| `buy_watch` | `10 ~ 14` |
| `buy_transition` | `6 ~ 9` |
| `observe_or_exit` | `0 ~ 5` |

## 9. 当前接入状态与后续阻塞

已完成：

1. 策略 4 日线/周线核心线、共振、金叉、`all_ok` 和缠论一买、二买、三买、底/顶部钝化、顶背驰证据标签。
2. 日线/周线快照、`as_of` 截断、520 根 lookback、200/40 根 EMA 预热元数据。
3. 纯函数评分、固定保守端点、同名信号去重、强确认合取门槛。
4. 信号级回测胜率契约；缺少上市日期、ST 或板块涨停元数据时显式返回 `insufficient_data`。
5. S/A/B 仓位映射、比例/百分比边界转换和 Risk Gate 单次 clamp 审计。
6. 选股 `deterministic_scoring` stage、报告影子表格、resume artifact。
7. 单股同步/流式/历史 trace 的 `composite_verdict` 字段和 `composite_verdict.json`。
8. 单股中文名称的离线解析、持仓名称唯一匹配和评分前目标同步，目标不明确时不再回退首持仓。
9. 请求、工具、评分和 Risk Gate 的目标一致性校验；新旧 Trace 的错配结果显式失败，partial Strategy4 原始指标在报告契约和 Trace 页面保持可见。

后续阻塞，按优先级排序。口径已在 [`strategy4-indicator-freeze.md`](./strategy4-indicator-freeze.md) 冻结，剩余工作是数据覆盖和影子验收：

1. 补齐逃顶预警剩余 5 类信号，并冻结买卖线收敛口径（依赖现有买卖线定义）。
2. 为信号级回测接入上市日期、ST/板块涨停幅度等元数据来源。
3. 影子回放验证分层单调性、覆盖率和稳定性后，才允许把 `shadow_mode` 切换为执行约束；验收阈值见冻结文档 §10。

## 10. 使用边界

本评分体系结合策略 4 技术面筛选、缠论形态分析和风险控制，用于辅助个股筛选与仓位管理，不构成投资建议。股市有风险，投资需谨慎。
