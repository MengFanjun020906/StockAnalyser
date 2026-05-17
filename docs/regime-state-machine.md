# A 股 Regime 状态机原理

本文说明 `detect_market_regime` 的设计目标、算法口径、数据输入、状态持久化和在 Agent 选股链路中的作用。它不是面向用户展示的大盘报告，而是 Agent 内部的市场环境约束，用来判断当前是否适合主动开仓、追趋势、提高仓位或只做观察。

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

## 12. 后续增强顺序

建议优先级：

1. 用 `TUSHARE_TOKEN` 接入 `index_daily`，稳定指数历史数据。
2. 接入 `fut_daily` / `fut_holding`，加入股指期货持仓和基差信号。
3. 接入 `opt_basic` / `opt_daily`，加入 ETF/股指期权成交、持仓和 PCR。
4. 建立 A 股 Fear & Greed 指数，融合涨跌家数、成交额、涨停跌停、北向、两融和波动率。
5. 对不同 Regime 下的选股命中率和回撤做回测校准，调整 risk multiplier 和仓位降档规则。
