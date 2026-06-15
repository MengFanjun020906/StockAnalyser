# Stage3 价格结构分析引擎原理

本文说明 `analyze_price_structure` 的设计目标、Chan 缠论流水线、SMC 结构识别和在 Agent 选股链路中的位置。

## 1. 设计目标

Stage3 的输入是已经取得的 OHLCV K 线，输出是结构化价格证据。它不直接判断“买入/卖出”，也不硬编码“三买三卖”。原因是同一个结构在不同 `market_regime`、资金面、账户风险和消息背景下含义不同。

因此本层只回答：

- 当前价格结构有哪些笔、中枢、力度变化。
- 当前摆动序列是否出现 BOS / CHoCH。
- 是否存在 Order Block / FVG 等可复核区域。
- 是否有未完成笔，提示实时推演仍在形成中。

## 2. 当前代码入口

- `src/agent/structure/price_structure.py`：确定性结构引擎。
- `src/agent/tools/analysis_tools.py`：Agent 工具 `analyze_price_structure`。
- `src/agent/planner.py`：`technical_analysis` capability 默认包含该工具。
- `src/agent/stock_selection.py`：选股深度分析阶段会读取该工具结果。

## 3. Chan 缠论流水线

当前实现按以下顺序处理：

1. 原始 K 线标准化为 `PriceBar`。
2. 处理包含关系，生成合并 K 线。
3. 识别顶/底分型。
4. 强制分型严格交替；同类连续分型只保留更极端的一个。
5. 按最小跨度约束生成笔，默认相邻分型间隔不少于 4 根 K 线。
6. 用连续三笔的区间重叠识别中枢：
   - `ZG = min(三笔高点)`
   - `ZD = max(三笔低点)`
   - 若 `ZG >= ZD`，认为存在重叠区间。
7. 计算笔力度：
   - `price_move`
   - `amplitude_pct`
   - `amplitude_ratio_vs_prev`
   - `macd_area_ratio_vs_prev`
8. 基于最后一笔端点和最新 K 线检测未完成笔。

`macd_area_ratio_vs_prev` 使用笔起止区间内的 MACD 柱绝对面积积分做力度对比；`amplitude_ratio_vs_prev` 则用价格振幅做对比。两者一起给 LLM 判断“价创新高/新低但力度是否收缩”提供证据。

## 4. SMC 结构识别

SMC 部分基于最近摆动序列：

- 摆动点：用 `swing_window` 作为左右半径，识别局部高点/低点。
- 标签：
  - 高点：`HH` / `LH`
  - 低点：`HL` / `LL`
- BOS：
  - 偏上行或震荡结构中，收盘价突破最近 swing high，标记 bullish BOS。
  - 偏下行或震荡结构中，收盘价跌破最近 swing low，标记 bearish BOS。
- CHoCH：
  - 上行结构跌破最近 swing low，标记 bearish CHoCH。
  - 下行结构突破最近 swing high，标记 bullish CHoCH。
- Order Block：
  - 冲动 K 线实体占全幅大于 60%。
  - 成交量高于近均量 1.3 倍。
  - 取冲动 K 线前一根反色 K 线区间为 OB。
- FVG：
  - 三根 K 线结构中，第三根低点高于第一根高点，标记 bullish FVG。
  - 第三根高点低于第一根低点，标记 bearish FVG。

## 5. 工具输出

核心输出结构：

```json
{
  "status": "ok",
  "data_quality": "sufficient | limited | insufficient",
  "bar_count": 120,
  "chan": {
    "merged_bar_count": 100,
    "fractal_count": 12,
    "pen_count": 5,
    "center_count": 1,
    "latest_fractals": [],
    "latest_pens": [],
    "latest_centers": [],
    "unfinished_pen": {},
    "structure_summary": {}
  },
  "smc": {
    "swing_count": 10,
    "latest_swings": [],
    "bos": {},
    "choch": {},
    "order_blocks": [],
    "fair_value_gaps": [],
    "structure_summary": {}
  }
}
```

## 6. 在选股中的作用

`watchlist_scan` 的单股深度分析会调用：

- `get_realtime_quote`
- `analyze_trend`
- `analyze_price_structure`
- `get_capital_flow`
- `get_stock_info`
- `get_chip_distribution`
- `search_comprehensive_intel`

结构结果会进入 `dimension_summary.price_structure`，给后续组合配置、反方审查和 Judge 提供“价格结构”证据。

## 7. 当前限制

- 目前只使用日线级别，尚未做多周期共振。
- MACD 柱面积比已按日线笔区间积分实现，但尚未按多周期或线段级别校准。
- 缠论线段级别尚未独立建模，当前到“笔/中枢/未完成笔”。
- SMC 为简化版，适合做结构提示，不适合作为独立交易系统。

## 8. 后续增强

建议顺序：

1. 给 `analyze_price_structure` 增加周期参数，支持日线/周线/分钟线。
2. 增加线段识别和中枢延伸/扩张/新生分类。
3. 将结构输出纳入 L1/L2/L3 信号协议，给风险闸门和回测系统统一消费。
4. 回测不同 Regime 下 Chan/SMC 结构信号的命中率和回撤。
