# A 股 Regime 状态机原理

本文说明 `detect_market_regime` 的设计目标、算法口径、数据输入、状态持久化和在 Agent 选股链路中的作用。它不是面向用户展示的大盘报告，而是 Agent 内部的市场环境约束，用来判断当前是否适合主动开仓、追趋势、提高仓位或只做观察。

当前实现已经能给出市场环境标签，但实际使用上仍偏粗：它能告诉后续链路“现在是什么状态”，却不能告诉后续链路“这个状态历史上未来 30 天通常怎么走、跌破现价概率多大、是否常先回踩再上涨、减仓后有没有合理买回点”。下一版应把 Regime 拆成两层：状态识别层继续保持确定性，新增 forward probability 层提供后验概率和路径参考。

## 1. 设计目标

市场环境不是只有上涨和下跌。相同的个股信号，在不同市场状态下应该对应不同动作：

- 趋势上行：允许回踩确认后的顺势策略。
- 震荡区间：优先箱体上下沿，不在中位追价。
- 高波动：降低首仓、收紧止损、缩短信号有效期。
- risk_off / panic：主动开仓降级为等待，优先处理账户风险。

因此 Regime 状态机输出的是结构化约束，而不是“看多/看空”一句话。

## 2. 当前实现入口

核心代码：

- `src/agent/regime.py`：确定性算法引擎。
- `src/agent/tools/market_tools.py`：Agent 工具 `detect_market_regime`。
- `src/storage.py`：SQLite 表 `market_regime_state`，保存上次状态、pending 状态和完整 payload。
- `src/agent/stock_selection.py`：`watchlist_scan` 选股流水线消费市场状态并做确定性降档。

工具输出核心字段：

```json
{
  "regime": "trending_up | trending_down | range_bound | high_volatility | risk_off | panic | unknown",
  "volatility_bucket": "very_low | low | normal | elevated | high_vol | extreme | unknown",
  "sentiment_state": "extreme_greed | greed | neutral | fear | extreme_fear | unknown",
  "wyckoff_phase": "accumulation | distribution | markup | markdown | range | unknown",
  "risk_level": "medium | medium_high | high | unknown",
  "risk_multiplier": 1.0,
  "strategy_hints": [],
  "evidence": [],
  "conflicts": [],
  "data_quality": "sufficient | limited | insufficient",
  "confirmation": {}
}
```

## 3. 波动率分档

波动率不使用固定阈值，而是使用经验 CDF：

1. 用市场代理指数的 OHLC 计算 True Range。
2. 计算 N 日 ATR，默认窗口为 14。
3. 计算 `ATR% = ATR / close`。
4. 把当前 ATR% 放入最近历史样本中排序，得到经验分位数。
5. 根据分位数映射到波动档位。

当前分档：

| 经验分位 | 档位 | 含义 |
| --- | --- | --- |
| `> 95%` | `extreme` | 极端波动 |
| `> 85%` | `high_vol` | 高波动 |
| `> 70%` | `elevated` | 波动抬升 |
| `>= 58%` | `normal` | 常态波动 |
| `>= 35%` | `low` | 低波动 |
| `< 35%` | `very_low` | 极低波动 |

每个档位绑定 `risk_multiplier` 和策略提示。例如 `extreme` 会提示禁止激进趋势追踪，优先风控和等待确认。

## 4. 阻尼机制

单根异常 K 线可能把经验分位瞬间推到很高。为了避免状态剧烈翻转，状态机会读取 SQLite 中上一次有效波动档位，并限制每次最多移动一级。

示例：

```text
上次档位：low
本次原始档位：extreme
生效档位：normal
```

这样可以避免从低波环境直接跳到极端波动，导致 Agent 过度反应。

## 5. A 股情绪合成

原始设想里的 Funding Rate、Long/Short Ratio、Fear & Greed、OI 更适合加密或衍生品市场。A 股第一版使用替代分量：

| 原始维度 | A 股替代项 | 当前来源 |
| --- | --- | --- |
| Funding Rate | 两融余额变化 | `get_margin_trading_summary` |
| Long/Short Ratio | 指数宽度 | `get_market_indices` |
| Fear & Greed | 预留 | 后续可接自建 A 股情绪指数 |
| OI / Flow | 北向资金、市场主力净流入 | `get_northbound_capital_flow`、`get_market_capital_flow` |

每个分量会压缩到 `[-1, 1]`，再加权合成情绪分数：

```text
margin_balance_change: 0.22
market_breadth:        0.24
fear_greed_index:      0.20
northbound_flow_z:     0.18
market_flow_z:         0.16
```

映射结果：

| 分数 | 情绪状态 |
| --- | --- |
| `>= 0.65` | `extreme_greed` |
| `>= 0.25` | `greed` |
| `<= -0.65` | `extreme_fear` |
| `<= -0.25` | `fear` |
| 其他 | `neutral` |

缺失分量不会被编造，只会降低 `data_quality`。

## 6. Wyckoff 相位识别

Wyckoff 相位使用最近约 100 根 K 线，综合以下因素：

- 当前价格在区间内的相对位置。
- 前段成交量和后段成交量的变化。
- VSA：近期振幅相对均值、近期成交量相对均值。
- Effort vs Result：成交量努力与价格结果是否匹配。
- Spring：假跌破后重新站回区间。
- Upthrust：假突破后回落。

输出相位：

- `accumulation`：吸筹。
- `distribution`：派发。
- `markup`：上升推进。
- `markdown`：下跌推进。
- `range`：震荡。
- `unknown`：样本不足或无法判断。

Wyckoff 不直接决定买卖，但会影响最终 `regime` 和风险提示。例如价格仍在短均线上方，但出现 `distribution`，会写入冲突项。

## 7. Regime 分类

分类不是单一指标决定，而是结合：

- 波动档位。
- 趋势位置：MA20、MA60、20 日收益、60 日收益。
- 情绪状态。
- Wyckoff 相位。

核心规则示例：

- `extreme` 波动且近 20 日明显下跌 -> `panic`。
- 高波动叠加恐惧情绪 -> `risk_off`。
- 高波动但未到 risk_off -> `high_volatility`。
- Wyckoff 为 `markup` 或均线/收益支持 -> `trending_up`。
- Wyckoff 为 `markdown` 或均线/收益走弱 -> `trending_down`。
- 其他 -> `range_bound`。

## 8. 状态确认

Regime 切换需要连续确认，默认 `confirmation_bars=3`。

如果上次状态是 `range_bound`，本次原始状态变成 `trending_up`：

```json
{
  "state": "pending",
  "raw_regime": "trending_up",
  "previous_regime": "range_bound",
  "pending_regime": "trending_up",
  "pending_count": 1,
  "required": 3
}
```

只有连续满足 3 次后才切换为新 regime。这样可以减少震荡行情中的频繁翻转。

## 9. SQLite 持久化

状态存入本地 SQLite 表 `market_regime_state`。每个市场保留一条最新状态：

- `market`
- `as_of`
- `regime`
- `raw_regime`
- `volatility_bucket`
- `raw_volatility_bucket`
- `pending_regime`
- `pending_count`
- `payload`
- `updated_at`

这让阻尼和确认机制能跨 Agent 运行生效，而不是只在一次工具调用内有效。

## 10. 在选股中的作用

`watchlist_scan` 选股流水线会在候选发现后立即调用 `detect_market_regime`，并把结果传入：

- 候选初筛。
- 单股深度分析。
- 组合配置。
- 反方审查。
- Judge 裁决。

同时还有确定性风控兜底：

- `risk_off` / `panic` / `extreme`：即使模型输出 `open`，也会降级为 `wait`，首仓归零。
- `high_vol` / `medium_high`：如果仍允许 `open`，首仓会被压低，并写入自动降级规则。

也就是说，Regime 不只是 prompt 里的背景信息，而是会真实影响选股和仓位输出。

## 11. 当前限制

第一版重点是建立状态机和 Agent 接入，不追求一次性补齐所有外部数据。

当前限制：

- 市场代理默认仍走现有日线加载链路，后续应优先使用 Tushare `index_daily` 明确拉沪深300指数。
- Fear & Greed 为预留位，当前没有自建 A 股情绪指数。
- 股指期货 OI、基差、ETF 期权 PCR/OI 尚未接入。
- 情绪合成分量不足时会返回 `data_quality=limited`，不会编造缺失数据。
- 输出偏标签和策略提示，缺少 `(regime -> future return)` 的历史概率校准。
- 当前 `risk_off / panic / extreme` 会直接进入仓位降档规则，但缺少按历史样本验证的适用边界。
- 缺少单股级 regime 概率和买回点参考，点位计算层只能凭当前结构和模型判断回踩价。
- 缺少路径形状信息：同样 30 日收益为正，可能是先跌后涨，也可能是直接上涨无回踩；这对等待、分批入场和 TRIM 完全不同。
- 缺少 `n / effective_n / low_confidence`，模型和 Judge 无法判断 regime 统计是否可靠。

## 12. Regime forward probability

当前已在状态机之上新增第一版概率层，用于回答“当前 regime 在历史上未来窗口通常怎么走”。这层只提供后验证据，不直接输出 `BUY / SELL`；最终动作仍由 Meta-Agent、点位计算层、Judge 和 Risk Gate 决定。

实现入口：

- `src/agent/regime_probability.py`：纯函数概率层，复用 `src/agent/regime.py` 的分类逻辑。
- `src/agent/tools/market_tools.py`：Agent 工具 `get_regime_forward_probability`。
- `src/agent/stock_selection.py`：`watchlist_scan` 在 `detect_market_regime` 后调用概率工具，并把摘要写入 `market_regime.forward_probability`；点位 prompt、fallback 和报告会消费 `regime_probability` / `reentry_reference`，但只把它作为后验证据和买回参考。

### 12.1 设计目标

Regime 识别层回答：

```text
现在是什么市场状态？
```

Regime 概率层回答：

```text
历史上处于这个状态时，未来 7/30/60/90 天通常怎么走？
跌破现价概率多大？
窗口内通常先回踩还是直接上涨？
如果现在减仓，什么价位接回更有统计依据？
这个统计样本够不够可信？
```

这层只提供后验证据，不直接输出 `BUY / SELL`。最终动作仍由 Meta-Agent、点位计算层、Judge 和 Risk Gate 决定。

### 12.2 建议输出字段

工具 `get_regime_forward_probability` 独立于 `detect_market_regime`，避免市场状态识别工具变得过重。第一版默认使用市场代理指数，输出 7 / 30 / 60 / 90 个交易日 forward probability。

```json
{
  "status": "ok",
  "market": "cn",
  "symbol": "000300.SH",
  "regime": "range_bound",
  "as_of": "2026-06-11",
  "windows": {
    "30d": {
      "n": 420,
      "effective_n": 14,
      "median_return_pct": 1.2,
      "mean_return_pct": 0.8,
      "p_up": 0.56,
      "p_below_current": 0.44,
      "p_down_gt_5pct": 0.18,
      "p10_return_pct": -6.4,
      "p20_return_pct": -3.2,
      "p90_return_pct": 8.5,
      "low_confidence": false
    }
  },
  "path_profile": {
    "window": "90d",
    "pct_dip_then_up": 0.31,
    "pct_up_no_dip": 0.22,
    "pct_pop_then_down": 0.18,
    "pct_down_no_pop": 0.29,
    "dip_median_pct": -4.1,
    "days_to_trough_median": 12,
    "pop_median_pct": 5.6,
    "days_to_peak_median": 18,
    "regime_persist_median_days": 16,
    "window_median_days": 62
  },
  "reentry_reference": {
    "current_price": 10.0,
    "downside_quantile": 0.2,
    "downside_pct": -3.2,
    "reentry_price": 9.68,
    "p_below_current": 0.44,
    "low_confidence": false
  }
}
```

### 12.3 算法口径

1. 用 production `src/agent/regime.py` 的分类逻辑对历史每一天重新打 regime 标签，禁止在概率层复制另一套规则。
2. 对每个历史日期，用 `date + Nd` 之后第一个可交易日收盘价计算 forward return。
3. 同时统计窗口内最低价、最高价、到达最低/最高的交易日数。
4. 计算 regime 在窗口内实际持续天数，避免把 regime 切换后的收益错误归因给原 regime。
5. 重叠窗口要折算有效样本数：`effective_n` 不能等于原始 `n`。
6. 显著回踩和显著冲高用当日 ATR 自校准，例如 `min_return <= -1 * atr_pct` 才算给过明显低吸点。
7. 尾部 lookahead 不足、停牌、涨跌停不可成交、除权复权口径异常的样本必须标记或剔除。

### 12.4 当前接入路径

第一阶段已接入市场代理指数：

- 默认 `index_code=000300`。
- 支持 `000001 / 000016 / 000300 / 000852 / 000905 / 399006`。
- 输出当前市场 regime 对应的 forward probability。
- `market_regime.forward_probability` 中保留概率摘要、`low_confidence`、路径画像和买回参考。
- `pricing_agent` prompt 明确禁止把概率层当成买卖信号；fallback 只会在 `Mean_Reversion_Pullback` 中把 `reentry_price` 写成“参考回踩/买回价”，并保留“仍需量价确认”。
- 最终报告的 Meta/点位计算链路会展示 `Regime 概率证据`；当样本或有效样本不足时，会明确标记“仅作弱证据，不能单独支持开仓”。

下一阶段做单股级概率：

- 只对持仓、用户指定单股或 deep dive 候选计算。
- 输出 `symbol_regime_probability` 和 `reentry_reference`。
- 供点位计算层生成入场区间、等待回踩线、TRIM 后买回点和失效条件。

第三阶段做后验校准：

- 从 Agent Trace 复盘每次 Judge 决策后的 7d / 30d 表现。
- 比较不同 regime 下 open / wait / trim 的结果。
- 调整降档规则时必须有历史证据，不再只凭标签。

### 12.5 下游使用规则

- `MarketRegime Expert` 只能引用概率摘要，不得把概率直接翻译成买卖建议。
- `Meta-Agent` 可以把概率写成硬约束或必算场景，例如“跌破现价概率高，必须计算回踩入场场景”。
- `pricing_agent` 必须使用 `reentry_reference` 解释 TRIM 后如何接回；如果没有低于现价的买回点，TRIM 应降级为 HOLD / wait 或明确只做风险减仓。
- `Judge` 对 `low_confidence=true` 的概率只作弱证据。
- `Risk Gate` 不直接消费概率层，避免统计概率绕过交易硬约束。

## 13. 后续增强顺序

建议优先级：

1. 用 `TUSHARE_TOKEN` 接入 `index_daily`，稳定指数历史数据。
2. 扩展 `get_regime_forward_probability` 的本地指数缓存和复权/不可成交样本标记。
3. 新增单股级 `symbol_regime_probability` 和 `reentry_reference`，只对 deep dive 标的和持仓标的计算。
4. 将概率摘要接入 Meta-Agent、点位计算层和 Judge，不直接接入 Risk Gate。
5. 接入 `fut_daily` / `fut_holding`，加入股指期货持仓和基差信号。
6. 接入 `opt_basic` / `opt_daily`，加入 ETF/股指期权成交、持仓和 PCR。
7. 建立 A 股 Fear & Greed 指数，融合涨跌家数、成交额、涨停跌停、北向、两融和波动率。
8. 对不同 Regime 下的选股命中率、回撤和机会成本做回测校准，调整 risk multiplier 和仓位降档规则。
