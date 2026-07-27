# Seed Pool 质量监控模块计划

## 1. 目标

建设一个长期可用的 Seed Pool 质量监控闭环，用来回答：

- 某个交易日进入 seed pool 的股票，下一交易日表现如何？
- 不同 seed source（AlphaSift、Sequoia、news_theme_daily、hot_rank、capital_flow_anomaly 等）的次日有效性如何？
- 每只 seed 后续被四席位支持、观察、反对或拒绝的理由是什么？
- 四席位是否过滤掉了后续表现好的 seed，或者放行了后续表现差的 seed？

第一版默认评估口径不只看绝对涨跌幅，必须同时覆盖：

- `下一交易日收盘涨跌幅`：衡量 seed 的绝对表现。
- `Alpha / 超额收益`：衡量 seed 是否跑赢基准，第一版基准固定为上证指数。
- `MFE / MAE`：衡量 T+1 盘中理论最大有利空间和最大不利风险。
- `liquidity_status`：识别一字涨停等无法正常买入的样本，并在胜率/均值统计中剔除不可买入样本。

## 2. 核心口径

| 字段 | 含义 |
| --- | --- |
| `seed_date` | seed pool 归属交易日，例如 2026-06-05 |
| `generated_at` | seed pool 实际生成时间，可能是 seed_date 收盘后或下一自然日 |
| `evaluation_date` | 评估用交易日，默认是 seed_date 的下一交易日 |
| `seed_close` | seed_date 收盘价 |
| `evaluation_close` | evaluation_date 收盘价 |
| `next_close_return_pct` | `(evaluation_close / seed_close - 1) * 100` |
| `benchmark_code` | 第一版固定为上证指数：Tushare 代码 `000001.SH`，Baostock 代码 `sh.000001` |
| `benchmark_return_pct` | 基准指数 evaluation_date 相对 seed_date 的收盘涨跌幅 |
| `alpha_return_pct` | `next_close_return_pct - benchmark_return_pct` |
| `mfe_pct` | 最大有利偏移，`(evaluation_high / seed_close - 1) * 100` |
| `mae_pct` | 最大不利偏移，`(evaluation_low / seed_close - 1) * 100` |
| `liquidity_status` | `NORMAL` / `LIMIT_UP_UNABLE_BUY` / `LIMIT_DOWN_RISK` / `UNKNOWN` |
| `trace_id` | 生成该 seed pool 的 Agent Trace 标识 |
| `run_id` | 选股流水线运行标识 |

原则：

- 种子池质量评估不等于买入建议。
- seed source 内部诊断分只作为来源内信号强度，不做跨来源统一排名。
- 四席位结论用于解释 seed 是否被继续采用，不替代次日表现回评。
- 即使后续 Meta、点位计算、Judge 或报告阶段失败，也应该保留 seed pool 快照和席位处理结果。
- 所有胜率、平均绝对收益、平均 Alpha、平均 MFE/MAE 统计，必须在代码层剔除 `liquidity_status=LIMIT_UP_UNABLE_BUY` 的样本，防止把“买不到的一字涨停”计入可交易表现。
- `LIMIT_UP_UNABLE_BUY` 第一版判定：T+1 日 `evaluation_open == evaluation_high == evaluation_close` 且 `next_close_return_pct >= 9.8`。浮点比较必须使用价格最小精度或容差，不能直接依赖二进制浮点相等。

## 3. 链路总览

```text
candidate_discovery
  -> build seed pool
  -> build SeedFactPacket
  -> run four thesis desks
  -> persist seed pool snapshot
  -> persist per-seed desk outcomes
  -> later evaluation job fills next-trading-day OHLC/return
  -> API serves date/source/desk quality views
  -> Web page displays seed quality dashboard + K line + trace links
```

推荐写入点：`candidate_discovery` 完成后立即写入 DB。

原因：

- 此时 seed pool、SeedFactPacket 和四席位 packets 已经形成。
- 后续深挖、Meta、点位、组合配置、Judge 的失败不应影响 seed pool 质量记录。
- 对排障最有价值的拒绝/支持理由已经在 `thesis_desk_packets` 或 per-seed packets 中可追溯。

## 4. 数据模型设计

### 4.1 `selection_seed_pool_snapshots`

记录一次 seed pool 生成事件。

| 字段 | 说明 |
| --- | --- |
| `id` | 主键 |
| `run_id` | 选股流水线运行 ID |
| `trace_id` | Agent Trace ID |
| `seed_date` | seed pool 归属交易日 |
| `generated_at` | 生成时间 |
| `market` | 市场，第一版默认 `cn` |
| `candidate_discovery_mode` | 例如 `thesis_desk_committee` |
| `seed_count` | seed 数量 |
| `status` | `ok` / `partial` / `failed` |
| `error` | 候选发现错误摘要 |
| `created_at` | DB 写入时间 |

### 4.2 `selection_seed_pool_items`

记录 seed pool 中的每只股票。

| 字段 | 说明 |
| --- | --- |
| `id` | 主键 |
| `snapshot_id` | 关联 `selection_seed_pool_snapshots.id` |
| `code` | 股票代码 |
| `name` | 股票名称 |
| `market` | 市场 |
| `source` | seed source |
| `source_diagnostics` | 来源内诊断字段，JSON |
| `trigger_signals` | 入池信号，JSON |
| `catalyst_tags` | 催化剂标签，JSON，例如 `["100亿算力大单", "AI服务器"]` |
| `catalyst_tier` | 催化剂级别，`1` 强 / `2` 中 / `3` 弱 / `0` 无、无效或辟谣 |
| `entry_reason` | 入池理由摘要 |
| `freshness` | source 数据日期或新鲜度 |
| `seed_order` | seed pool 内原始顺序 |
| `entered_deep_dive` | 是否进入深挖 |
| `entered_final_report` | 是否进入最终报告 |

### 4.3 `selection_seed_pool_desk_outcomes`

记录每只 seed 在四席位中的处理结果。

| 字段 | 说明 |
| --- | --- |
| `id` | 主键 |
| `item_id` | 关联 `selection_seed_pool_items.id` |
| `desk` | `early_turn_desk` / `momentum_desk` / `quality_repair_desk` / `theme_catalyst_desk` |
| `status` | 席位 packet 状态 |
| `stance` | `support` / `watch` / `neutral` / `oppose` / `invalid` / `missing` |
| `decision` | `accepted` / `rejected` / `not_evaluated` / `failed` |
| `reason` | 支持或拒绝理由 |
| `risks` | 风险条目，JSON |
| `evidence` | 席位证据摘要，JSON |
| `metrics` | 席位可选结构化指标，JSON；第一版不要求四席位输出交易点位 |
| `errors` | LLM、工具或超时错误，JSON |
| `elapsed_ms` | 单 seed 或席位处理耗时 |

职责边界：四席位负责判断 seed 是否值得继续观察或进入候选，稳定输出 `stance/decision/reason/risks/evidence/errors`；交易点位属于后续 Meta / pricing_agent，不前置要求四席位产出。

### 4.4 `selection_seed_pool_evaluations`

记录 seed 的后验表现。

| 字段 | 说明 |
| --- | --- |
| `id` | 主键 |
| `item_id` | 关联 `selection_seed_pool_items.id` |
| `evaluation_date` | 默认下一交易日 |
| `seed_close` | seed_date 收盘价 |
| `evaluation_open` | evaluation_date 开盘价 |
| `evaluation_high` | evaluation_date 最高价 |
| `evaluation_low` | evaluation_date 最低价 |
| `evaluation_close` | evaluation_date 收盘价 |
| `next_close_return_pct` | 下一交易日收盘涨跌幅 |
| `benchmark_code` | 基准指数代码，第一版固定上证指数 |
| `benchmark_return_pct` | 基准指数次日涨跌幅 |
| `alpha_return_pct` | 超额收益，`next_close_return_pct - benchmark_return_pct` |
| `mfe_pct` | T+1 最大有利偏移，基于最高价 |
| `mae_pct` | T+1 最大不利偏移，基于最低价 |
| `liquidity_status` | `NORMAL` / `LIMIT_UP_UNABLE_BUY` / `LIMIT_DOWN_RISK` / `UNKNOWN` |
| `data_status` | `ok` / `missing_price` / `not_trading` / `failed` |
| `error` | 价格数据错误摘要 |
| `updated_at` | 评估更新时间 |

## 5. 后端服务设计

### 5.1 快照写入服务

新增服务职责：

- 从 `candidate_discovery` payload 中提取 seed pool summary、preview、source diagnostics。
- 从 `thesis_desk_packets` / `per_seed_packets` 中提取每只 seed 的四席位 stance、reason、risks、errors。
- 建立 `trace_id/run_id/seed_date/generated_at` 关联。
- 对同一 `run_id + trace_id` 做幂等写入，避免重复记录。

### 5.2 评估填充服务

新增定时或按需任务：

- 找出还没有 `selection_seed_pool_evaluations` 的 seed item。
- 根据 `seed_date` 找下一交易日。
- 下一交易日以本地基准指数 OHLC 中 seed_date 之后的第一根交易日为准，不能只按自然工作日推断，节假日休市要自动跳过。
- 通过统一 `MarketDataProvider` 读取 seed_date 和 evaluation_date 的个股日线 OHLC。
- 同步读取基准指数上证指数的 seed_date 和 evaluation_date 日线 OHLC。
- 计算 `next_close_return_pct`、`benchmark_return_pct`、`alpha_return_pct`、`mfe_pct`、`mae_pct`。
- 判定 `liquidity_status`，其中一字涨停不可买入样本标记为 `LIMIT_UP_UNABLE_BUY`。
- 缺失行情时写入 `data_status`，不伪造收益。

数据源约束：

- 底层封装统一 `MarketDataProvider` 接口，服务层只依赖 `get_daily_bars(symbol, start_date, end_date, market)`。
- 第一优先级：Baostock，免费且限制较少；A 股个股使用 Baostock 股票代码格式，基准上证指数使用 `sh.000001`。
- 第二优先级：Tushare Pro；个股使用 `ts_code`，基准上证指数使用 `000001.SH`。
- provider 返回字段统一为 `trade_date/open/high/low/close/volume/amount/source`，缺失字段必须显式标记，不允许静默填 0。

计算逻辑：

```text
next_close_return_pct = (evaluation_close / seed_close - 1) * 100
benchmark_return_pct  = (benchmark_eval_close / benchmark_seed_close - 1) * 100
alpha_return_pct      = next_close_return_pct - benchmark_return_pct
mfe_pct               = (evaluation_high / seed_close - 1) * 100
mae_pct               = (evaluation_low / seed_close - 1) * 100
LIMIT_UP_UNABLE_BUY   = evaluation_open == evaluation_high == evaluation_close
                        and next_close_return_pct >= 9.8
```

第一版可以提供手动 API 或脚本触发，后续再接入每日任务。

### 5.3 API 草案

```text
GET /api/v1/seed-pool-quality/dates
GET /api/v1/seed-pool-quality?seed_date=2026-06-05
GET /api/v1/seed-pool-quality/snapshots/{snapshot_id}
GET /api/v1/seed-pool-quality/items/{item_id}
GET /api/v1/seed-pool-quality/items/{item_id}/chart-data
POST /api/v1/seed-pool-quality/evaluate?seed_date=2026-06-05
```

`GET /api/v1/seed-pool-quality?seed_date=...` 返回：

- 页面汇总指标。
- 来源分组统计。
- 席位分组统计。
- seed item 列表。
- 每只 seed 的次日表现、来源、入池理由、四席位摘要和 trace 链接。

`GET /api/v1/seed-pool-quality/items/{item_id}/chart-data` 返回：

- `bars`：`seed_date` 前 20 个交易日到后 5 个交易日的日 K 数据。
- `evaluation`：该 seed 的 `next_close_return_pct`、`benchmark_return_pct`、`alpha_return_pct`、`mfe_pct`、`mae_pct`、`liquidity_status`。
- `catalyst`：`catalyst_tags`、`catalyst_tier`、`trigger_signals`。
- `desk_outcomes`：四席位 `stance/reason/risks/metrics` 摘要。
- `price_lines`：可选结构化参考线，仅当上游显式提供标准化数值指标时返回；第一版不从四席位自然语言 reason 正则抽取点位。例如：

```json
[
  {"desk": "momentum_desk", "key": "bos_level", "price": 12.34, "label": "BOS 支撑", "color": "green"},
  {"desk": "early_turn_desk", "key": "invalidation_level", "price": 11.88, "label": "证伪/止损", "color": "red"},
  {"desk": "quality_repair_desk", "key": "ma20_anchor", "price": 12.05, "label": "MA20 锚点", "color": "yellow"}
]
```

后端职责边界：`chart-data` API 只透传明确结构化的参考线，前端不在浏览器侧正则解析席位自然语言，也不把四席位理由当作交易点位来源。

## 6. 前端页面设计

入口：

- 在 Agent Trace 或 seed pool 区域增加按钮：`查看种子池质量`。
- 跳转到 `/seed-pool-quality`。

页面结构：

1. 日期选择区
   - `seed_date` 选择器。
   - 展示 `generated_at`、`evaluation_date`。

2. 顶部质量概览
   - seed 总数。
   - 可交易样本数；`LIMIT_UP_UNABLE_BUY` 剔除样本数。
   - 次日上涨数 / 下跌数。
   - 次日平均涨跌幅 / 中位涨跌幅。
   - 平均 Alpha / 中位 Alpha。
   - 平均 MFE / 平均 MAE。
   - 缺失行情数量。

3. 来源质量表
   - source。
   - seed 数量。
   - 次日上涨比例。
   - 平均次日收盘涨跌幅。
   - 平均 Alpha。
   - 平均 MFE / MAE。
   - `LIMIT_UP_UNABLE_BUY` 样本数。
   - 被任一席位 support/watch 的比例。

4. 席位质量表
   - desk。
   - support/watch 数量。
   - oppose/invalid 数量。
   - support/watch 样本次日平均表现和 Alpha。
   - oppose/invalid 样本次日平均表现和 Alpha。
   - 被拒绝但次日上涨的样本数量。

5. 归因分析面板
   - 页面中部增加 Tab：`Source` / `Desk` / `Catalyst Tier`。
   - 每个 Tab 展示分组柱状图，对比平均 Alpha、胜率、平均 MFE、平均 MAE。
   - 所有柱状图默认使用剔除 `LIMIT_UP_UNABLE_BUY` 后的可交易样本；Tooltip 中展示被剔除样本数。

6. Seed 明细表
   - 股票代码/名称。
   - source。
   - 入池理由。
   - catalyst tags / catalyst tier。
   - seed 日收盘价。
   - 下一交易日收盘价。
   - 下一交易日收盘涨跌幅。
   - Alpha、MFE、MAE、liquidity_status。
   - 四席位结论摘要。
   - 是否进入深挖。
   - 是否进入最终报告。
   - trace 链接。

7. 行展开详情 / 右侧复盘面板
   - 引入 ECharts 或 TradingView Lightweight Charts 渲染 K 线，不再实现只够展示的基础 K 线。
   - 标记 seed_date、evaluation_date、seed_close。
   - 如 `chart-data.price_lines` 存在，渲染为可选参考线；不存在时不展示价格线。
   - 智能 Tooltip：悬浮 T+1 K 线时分块展示 OHLC、Alpha、MFE、MAE、liquidity_status、catalyst tags，以及四席位 `stance/reason` 折叠摘要。
   - 以固定四席位矩阵展示低位启动席、动量席、质量修复席、主题催化席的 `stance/decision/reason/risks/errors`；缺失席位也显示为 `missing`，避免用户误以为该席位支持或反对。
   - 展示 source diagnostics / trigger signals。

## 7. 日 K 展示

第一版展示范围：

- `seed_date` 前 20 个交易日。
- `seed_date` 后 5 个交易日。

图上标记：

- 入池日。
- 下一交易日评估日。
- 入池收盘价。
- 可选参考线 `price_lines`：仅当上游显式给出标准化数值指标时展示。

图表组件要求：

- 第一版引入 ECharts 或 TradingView Lightweight Charts。
- K 线数据由 `chart-data` API 提供，不在前端自行拼行情。
- 可选参考线由 `chart-data.price_lines` 提供，前端按 `key/color/label` 渲染水平线；没有参考线时 K 线仍然可用。
- Tooltip 必须联动 `evaluation/catalyst/desk_outcomes`，不能只展示 OHLC。

## 8. 指标解释

| 指标 | 用途 |
| --- | --- |
| 次日收盘涨跌幅 | 绝对表现指标，不单独作为最终质量判断 |
| 基准收益 | 上证指数次日收益，第一版固定 `000001.SH` / `sh.000001` |
| Alpha / 超额收益 | `次日收盘涨跌幅 - 基准收益`，用于判断是否跑赢市场 |
| MFE | T+1 最高价相对 seed_close 的最大有利偏移 |
| MAE | T+1 最低价相对 seed_close 的最大不利偏移 |
| liquidity_status | 流动性状态；`LIMIT_UP_UNABLE_BUY` 不参与平均胜率和平均收益 |
| 来源胜率 | 判断 source 是否长期有效 |
| 席位支持后表现 | 判断席位是否提高 seed precision |
| 被拒绝但上涨 | 找出席位误杀样本 |
| 被支持但下跌 | 找出席位误判样本 |
| 缺失行情数量 | 衡量数据质量，不参与收益均值 |

后续可扩展：

- 3 日 / 5 日 / 10 日收益。
- 行业指数 Alpha。
- 按市场状态分组回评。

## 9. 实施 Todo

### P0：文档和契约

- [x] 明确第一版评估口径为次日绝对收益 + 上证指数 Alpha + MFE/MAE + 流动性状态过滤。
- [x] 明确快照写入点为 `candidate_discovery` 完成后。
- [x] 明确需要追溯四席位支持/拒绝理由。
- [x] 明确 `LIMIT_UP_UNABLE_BUY` 不参与平均胜率和平均收益。
- [x] 明确四席位不承担交易点位输出，第一版前端按四席位矩阵展示 `stance/decision/reason/risks/errors`。
- [x] 确认页面路由和入口位置：Web 侧边栏新增“质量”，路由为 `/seed-pool-quality`。
- [x] 确认日线价格数据优先来源：Baostock 优先，Tushare Pro 备选。

### P1：DB 和 Repository

- [x] 新增 seed pool 快照、item、desk outcome、evaluation 表结构。
- [x] 增加初始化逻辑。
- [x] 在初始化逻辑中补充 `catalyst_tags`、`catalyst_tier`、`metrics`、`benchmark_return_pct`、`alpha_return_pct`、`mfe_pct`、`mae_pct`、`liquidity_status` 等新字段。
- [x] 新增 Repository 层，支持幂等写入和按日期查询。
- [x] 覆盖 Repository 单元测试。

### P2：快照写入

- [x] 在 `candidate_discovery` 完成后写入 snapshot。
- [x] 从 seed pool payload 提取 seed item。
- [x] 从 `thesis_desk_packets` / `per_seed_packets` 提取四席位 outcome。
- [x] 保留 trace/run 关联。
- [x] 对候选发现失败但 seed pool 已生成的情况仍写入快照。
- [x] 增加快照写入测试。

### P3：表现评估

- [x] 实现下一交易日解析。
- [x] 封装本地日线优先的评估数据读取，在线数据源用于补齐。
- [x] 实现 seed_date 和 evaluation_date 个股日线 OHLC 查询。
- [x] 实现上证指数基准 OHLC 查询。
- [x] 计算 `next_close_return_pct`、`benchmark_return_pct`、`alpha_return_pct`、`mfe_pct`、`mae_pct`。
- [x] 实现 `LIMIT_UP_UNABLE_BUY` 判定与统计剔除逻辑。
- [x] 缺失行情时写入结构化失败状态。
- [x] 增加评估服务测试，覆盖 Alpha、MFE/MAE、一字板过滤、缺失行情。

### P4：API

- [x] 新增日期列表 API。
- [x] 新增按 seed_date 查询质量总览 API。
- [x] 新增单 snapshot 详情 API。
- [x] 新增单 seed item 详情 API。
- [x] 新增 K 线及可选参考线 API：`GET /api/v1/seed-pool-quality/items/{item_id}/chart-data`。
- [x] 新增按需 evaluation API。
- [x] 增加 API 测试。

### P5：前端

- [x] 在侧边栏增加质量监控入口。
- [x] 新增 `/seed-pool-quality` 页面。
- [x] 实现日期选择和概览指标。
- [x] 实现来源/席位/Catalyst 归因视图。
- [x] 实现归因分析 Tab：按 Source、Desk、Catalyst Tier 分组展示 Alpha 和胜率。
- [x] 实现 seed 明细表。
- [x] 实现右侧详情面板，展示入池理由和四席位 stance/reason/metrics。
- [x] 引入 ECharts，实现 K 线渲染、seed close / T+1 标记和智能 Tooltip 联动。
- [x] 增加 lint 和 build 验证。

### P6：运营和排障

- [x] 增加手动回填脚本，用于从历史 trace 导入 seed pool 快照：`scripts/backfill_seed_pool_quality_from_traces.py`。
- [x] 增加评估任务日志和失败重试：`scripts/evaluate_seed_pool_quality.py` 输出结构化 ok/skipped/error，`daily_run.sh` 未成功不写完成标记，下次续跑重试。
- [x] 在 Trace 页面提供跳转到质量监控页的反向链接：`AgentTracePage` 会按 `seed_date` 跳转 `/seed-pool-quality`。
- [x] 后续接入每日自动评估任务：`scripts/daily_run.sh` 第四步通过 `SEED_POOL_QUALITY_DAILY_EVALUATION_ENABLED=true` opt-in 执行。

## 10. 第一版验收标准

- 能选择某个 `seed_date` 查看 seed pool。
- 能看到每只 seed 的下一交易日收盘涨跌幅。
- 能看到每只 seed 的上证指数基准收益、Alpha、MFE、MAE 和 liquidity_status。
- 能按 source 看到平均表现、平均 Alpha 和上涨比例，且统计默认剔除 `LIMIT_UP_UNABLE_BUY`。
- 能展开每只 seed，看到四席位支持/拒绝理由。
- 能在 K 线图上看到 seed close / T+1 标记，并在 T+1 Tooltip 中看到量化指标、Catalyst 和席位复盘摘要。
- 能在前端以固定四席位矩阵看到每个席位的支持/观察/反对/缺失状态与理由。
- 能按 Source、Desk、Catalyst Tier 做归因分析。
- 能跳回生成该 seed 的 trace。
- 即使后续选股流水线失败，只要 seed pool 和席位包存在，就能落库并回评。

## 11. 非目标

第一版不做：

- 自动修改 seed source 权重。
- 自动调整四席位 prompt。
- 多周期收益排名。
- 实盘交易归因。
- 把 seed pool 质量指标直接显示为买入评分。

第一版必须包含：

- 基准对比 Alpha。
- 盘中极值 MFE / MAE。
- 流动性状态过滤，尤其是一字涨停不可买入样本剔除。
- K 线图上的 seed close / T+1 标记、T+1 智能 Tooltip 和四席位理由矩阵。

非目标中列出的自动调权、Prompt 调优、多周期收益排名和实盘交易归因，可以在积累足够样本后再进入下一阶段。
