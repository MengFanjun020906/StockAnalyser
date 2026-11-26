# 影子账户（Paper Trading，A/B 双账本）

## 1. 定位

影子账户是选股链路的常驻托管跟踪器：它不参与选股、排序、阈值、action 或否决，只把链路已经产出的结论拿虚拟资金模拟执行，长期观察"链路如果真拿钱跑，会是什么结果"。

- **不自己跑 LLM**：选股与单股分析由用户手工运行，影子账户只消费沉淀下来的产物。
- **完全隔离**：两本各自独立的虚拟账户分别落在独立 SQLite（默认 `data/paper_trading/a.db`、`b.db`），不读也不写真实 `portfolio_*` 表，只读行情与产物。
- **默认关闭**：`AGENT_PAPER_TRADING_ENABLED=false`；关闭时不建库、不注册后台任务、不产生任何交易。

## 2. 两本账的口径

两本账各自 100,000 元，一次性给足、之后不补钱，允许满仓；共用同一套撮合、费用、行情与 T+1，规则只有入场门控不同。

| 账本 | 入场门控 | 仓位 |
| --- | --- | --- |
| **A 本（严格链路）** | 只有 AI 最终动作是 `open` 才买 | 优先用 AI 计划的 `initial_position_pct`；缺失时用引擎比例，再缺失用配置兜底 |
| **B 本（点位优先）** | 引擎入场区当日被价格触及、且非涨停一字板就买；AI 说什么都只记录不执行 | 用引擎仓位比例；缺失时用配置兜底 |

补充口径：

- 手数 100 股整数倍。
- 佣金/过户费/印花税复用 `src/backtest/fees.py` 的 `cn-a-share-2023.v1`：佣金 2.5bp（最低 5 元）、过户费 0.1bp、卖出印花税 5bp。
- T+1：当日买入当日不可卖；前一日及更早持仓当日可卖。
- 现金不足时按可用现金与预算取整手，不融资、不补钱。

## 3. 输入：只认两类产物

### 3.1 选股运行产物（喂 A 本 + B 本）

- 位置：`<主库目录>/agent_traces/<run_id>/stock_selection.json`（Web/API 运行选股后自动沉淀；`AGENT_PAPER_TRADING_TRACE_DIR` 可覆盖根目录）。
- AI 动作：`final_report_json.portfolio_allocation.full.positions_plan[]` 的 `code` / `action` / `initial_position_pct`。
- 引擎点位：优先取 `trade_plan_artifacts` 中该标的已持久化的正式计划；没有持久化产物时，用同一确定性引擎按决策日现算出点位（`persist=False`、不联网、不调用 LLM）。
- 路由：**同时进 A 本与 B 本**。A 本看 `action` 是否为 `open`，B 本只看点位是否被触及。

### 3.2 单股分析产物（只喂 A 本）

- 位置：主库 `analysis_history` 表（`main.py --stocks ...` 或 Web/API 单股分析写入）。
- AI 动作：`raw_result.decision_type` 为 `buy`，或 `operation_advice` 为「买入/加仓/建仓/增持」时视为 `open`；其余不买。
- 引擎点位：同样只取 `trade_plan_artifacts` 或确定性引擎现算。
- 路由：**只进 A 本**。B 本是"点位优先、忽略 AI"，单股分析的结论本身是 AI 驱动的复检，不参与点位优先账本。

### 3.3 关联方式

- `paper_ingest(source, run_id)` 是产物台账：`source` ∈ `selection_run` / `single_analysis`，`run_id` 为选股 trace 的 run id 或单股分析的 `query_id`。
- `paper_plans.run_id/source` 把每个计划回溯到具体产物；`paper_trades`、`paper_decisions` 同样带 `run_id/source`，因此每笔交易都能追到"是哪次运行、哪类产物"。
- 计划在决策日的**下一个交易日**起可执行，有效窗口取引擎计划自身的截止日；过期计划自动置为 `expired`。

## 4. 成交口径

成交只用引擎点位，不让模型报价：

- 入场（默认 `touch`）：日线最低价触及入场区上沿 `entry_high` 即视为限价单成交，成交价 `min(entry_high, open)`；若开盘已跳空低于入场区下沿，则按开盘价成交。
  - 每笔买入额外记录 `close_in_zone`：当日收盘价是否也落在 `[entry_low, entry_high]` 内，事后可据此单独统计 `close` 口径的对照数字。
- 入场（可选 `close`）：仅当收盘价落在入场区内才按收盘价成交，更保守、成交更少。
- 出场：同一根日线同时触及止损与目标时按**止损优先**（保守）；先按 `target_1_exit_ratio` 在目标 1 部分止盈，剩余仓位等目标 2 或止损。止损跳空低开按开盘价成交。
- 涨跌停：一字涨停不买，一字跌停不卖；阈值按 10% / 20%（创业板、科创板）/ 30%（北交所）。
- 停牌或无成交（无当日 bar）：不成交，持仓按最近收盘价继续持有。
- 计划消费：完全清仓后计划标记为 `consumed`，不会在次日被同一份旧计划重新买入。

### 4.1 日线近似的偏差方向

当前只使用本地日线 `stock_daily`，无法还原盘中路径：

- 成交**概率**略偏乐观：忽略排队、滑点与流动性，把"触及价格"等同于"全额成交"。
- 成交**价格**偏保守：入场取区间上沿而非下沿，不会系统性抬高收益。
- 同日止损与目标的先后顺序无法从日线判定，已固定为**止损优先**，属于保守处理。
- 若需要更保守，使用 `AGENT_PAPER_TRADING_ENTRY_FILL_MODE=close`，代价是成交笔数明显减少；也可用 `close_in_zone` 标记做离线对照。

### 4.2 分钟级数据现状（2026-11 实测）

- Tushare 私有网关（`ts.gyzcloud.top`）当前积分档不包含 `stk_mins` / `rt_k` / `rt_min`，均返回 403「不在积分档范围内，请单独开通该接口权限」；日线 `daily` 正常，说明 token 与网关本身可用。
- Baostock 5/15/30/60 分钟可用，约 2020 年起，不支持 1 分钟；本地 `stock_minute_bars` 仅 96 只、122 个交易日（2026-05-15 → 2026-11-11），且不是日常任务写入。
- Sina（akshare `stock_zh_a_minute`）可用：5 分钟单次约 1970 根、覆盖约 41 个交易日；1 分钟约 8 个交易日；含当日盘中数据，延迟数分钟。
- 结论：分钟线只适合对"当日触及入场区的少数标的"做二次确认，无法支撑历史回补。本期仍以日线近似为准，不扩全量采集。

## 5. 每日流程与幂等

影子账户不做"每天定时重跑选股链路"，而是按两条独立节奏运行：

1. **产物入账**（随时）：扫描最近 `AGENT_PAPER_TRADING_INGEST_LOOKBACK_DAYS` 天内、尚未入账的产物，解析为计划写入 `paper_ingest` 与 `paper_plans`。
2. **收盘记净值**（工作日 `AGENT_PAPER_TRADING_RUN_AFTER` 之后）：先卖后买，再按收盘价 mark to market，写入当日唯一一条净值，并刷新全部决策的 T+5 / T+20。

幂等约束：

- 同一次运行（同一 `source + run_id`）重复扫描不会重复入账，主键去重。
- 同一天多份产物：按 `(决策日, 标的)` 覆盖，后来的计划替换先前的计划，不累加。
- 同日重跑会话是**覆盖**：先删除当天的交易、持仓快照、净值与当日决策，再按上一交易日快照重放。
- **当天没有新产物**：只把存量持仓按收盘价记账、写一条净值，不产生成交、不报错、不补造数据。
- 已结算的历史日期不允许回写（`refused_historical_rewrite`）；一天最多一条净值，由 `paper_nav.as_of` 主键保证。
- 产物若拿不到正式引擎计划（引擎状态为 `observation`），该标的当天不参与撮合，不猜点位。

## 6. 产出

默认写入 `<影子库目录>/report/`：

- `report.json`：两本账的账户快照、净值曲线、每笔决策、基准对比、A/B 对照与 `close` 口径统计。
- `nav_curve_A.csv` / `nav_curve_B.csv`：日期、现金、市值、权益、基准收盘价、基准净值、持仓数。
- `decisions_A.csv` / `decisions_B.csv`：每笔买入的入场价/数量/费用、`close_in_zone`、T+5 与 T+20 收益、退出日期/价格/已实现收益、状态、来源 `source` 与 `run_id`。

净值口径：`equity = cash + Σ(quantity × 当日收盘价)`；基准净值 = `初始资金 × 当日 000985.CSI 收盘价 / 首个净值日 000985.CSI 收盘价`。基准走 `index_daily` 缓存（`data/factor_eval_cache`），不从 `stock_daily` 按裸码取指数；缺失基准价的日期写 `null`，不阻断净值记录。

## 7. 手工喂一次产物并看到两本净值

以本机 2026-11-24 为例：

1. 先正常跑一次选股或单股分析，确认产物已落库：
   - 选股：`data/agent_traces/<run_id>/stock_selection.json`
   - 单股：主库 `analysis_history` 有当日新行
2. 手动结算（开启开关 + 指定结算日）：

```bash
AGENT_PAPER_TRADING_ENABLED=true .venv/bin/python scripts/run_paper_trading.py --date 2026-11-24
```

3. 输出会直接打印两本账的 `equity / cash / trades / positions` 与 A/B 对照；详细结果看：
   - `data/paper_trading/report/report.json`
   - `data/paper_trading/report/nav_curve_A.csv`、`decisions_A.csv`
   - `data/paper_trading/report/nav_curve_B.csv`、`decisions_B.csv`
4. 只看报告不跑会话：`... --report-only`；只想入账不定净值：`... --ingest-only`；强制重新解析已入账产物：`... --reingest`。

常用参数：

- `--book A` / `--book A,B`：只跑指定账本。
- `--db-dir /tmp/pt`：把影子库放到临时目录，便于试验。
- `--trace-dir /path/to/agent_traces`：指定选股产物目录。
- `--force`：无视 `AGENT_PAPER_TRADING_ENABLED` 开关临时跑一次。
- `--no-ingest`：只用已入账产物结算，不扫描新产物。

## 8. 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_PAPER_TRADING_ENABLED` | `false` | 总开关 |
| `AGENT_PAPER_TRADING_DB_DIR` | 空 | 影子库目录；空 = 主库目录下 `paper_trading/`（内含 `a.db`、`b.db`） |
| `AGENT_PAPER_TRADING_TRACE_DIR` | 空 | 选股产物目录；空 = 主库目录下 `agent_traces/` |
| `AGENT_PAPER_TRADING_OUTPUT_DIR` | 空 | 报告目录；空 = 影子库目录下 `report/` |
| `AGENT_PAPER_TRADING_INITIAL_CASH` | `100000` | 每本一次性虚拟资金 |
| `AGENT_PAPER_TRADING_MAX_POSITION_PCT` | `20` | 兜底单只仓位（%），仅当 AI 与引擎都没给比例时使用 |
| `AGENT_PAPER_TRADING_MAX_POSITIONS` | `5` | 每本同时持仓上限 |
| `AGENT_PAPER_TRADING_MAX_INGEST_PER_RUN` | `20` | 单次最多解析多少条新产物 |
| `AGENT_PAPER_TRADING_INGEST_LOOKBACK_DAYS` | `7` | 只扫描最近 N 天产物 |
| `AGENT_PAPER_TRADING_BENCHMARK` | `000985.CSI` | 基准指数（走 `index_daily` 缓存） |
| `AGENT_PAPER_TRADING_ENTRY_FILL_MODE` | `touch` | `touch` / `close` |
| `AGENT_PAPER_TRADING_RUN_AFTER` | `18:00` | 收盘记净值的最早时间 |

## 9. 已知边界与风险

- 只覆盖本地 `stock_daily` 中的 A 股代码；港股、美股、北交所不参与。
- 高价股受预算约束：一手金额超过单标的预算时无法建仓（例如 10 万账户、20% 兜底仓位下，600519 一手约 14 万），这不是异常，而是预算的结果。
- 不建模滑点、排队、成交量限制与部分成交；真实冲击成本会高于此处。
- 引擎给出 `observation`（数据不足）的标的不建仓，B 本因此可能长期空仓；这是引擎口径而非缺陷。
- 日线无法判定盘中先后顺序，已固定止损优先；这会让部分"先到目标后回落止损"的样本被记为亏损。
- 基准 000985.CSI 若在某日缺失（例如缓存尚未覆盖该交易日），只影响当日 `benchmark_nav`，不影响账户净值。
- 影子库与报告目录属于本地运行产物，默认不提交；旧版单账本 `data/paper_trading.db` 不再读取，可自行删除或保留。

## 10. 回滚

- 关闭 `AGENT_PAPER_TRADING_ENABLED` 或删除 `paper_trading_shadow` 后台注册即可停止运行。
- 删除 `data/paper_trading/`（`a.db`、`b.db`、`report/`）可清空影子账户；真实 `portfolio_*` 不受影响。
