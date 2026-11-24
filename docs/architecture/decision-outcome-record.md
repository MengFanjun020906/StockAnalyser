# 决策结果待结算记录（MEM-01a）

## 目标

在决策发生后立即落一条“待结算”记录，等行情到位后再补 T+5 收益，用于后续记忆/评分线积累样本。该链路是**只写观察数据**，不参与选股、排序、仓位、阈值或 veto。

## 开关

- 环境变量：`AGENT_DECISION_OUTCOME_ENABLED`，默认 `false`。
- 关闭时：`_build_final_report_json` 内的 hook 只读配置后立即返回；`build_decision_outcome_background_tasks` 返回 `[]`；不建表写入、不注册后台任务、不查询行情。
- 开启时：只影响写入与后台结算，报告内容与选股结果不变。

## 记录范围

只记录可执行动作，判定依据是推荐项的 `execution_mode`：

| 记录 | `execution_mode` | 说明 |
| --- | --- | --- |
| 是 | `immediate_open` | 开盘直接入场 |
| 是 | `conditional_open` | 条件触发入场 |
| 否 | `strong_watch` / `plain_wait` | 计入 `skipped_actions`，不落行 |

`watch / avoid` 等纯观察项同样走 `skipped_actions` 计数。horizon 固定 `N = 5`，不提供配置。不回填历史 plan，仅从开启后的新决策开始积累。

## 表结构

表名 `decision_outcome_records`，建在现有 `data/stock_analysis.db`。幂等键 `UNIQUE(run_id, code)`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | Integer PK | 自增 |
| `run_id` | String(128) | 一次 Agent 运行 ID，索引 |
| `code` | String(20) | 标准化后的股票代码，索引 |
| `market` | String(12) | 默认 `cn` |
| `decision_as_of` | Date | 决策日，索引 |
| `action` | String(32) | 推荐动作原文 |
| `execution_mode` | String(32) | 入场模式 |
| `plan_mode` / `plan_id` | String | 计划引用，可空 |
| `entry_zone_text` | Text | 入场区间/条件原文 |
| `entry_reference` | Float | 实际入场参考价（结算时回填） |
| `horizon_days` | Integer | 固定 5 |
| `entry_session` / `exit_session` | Date | 结算会话（回填） |
| `entry_price` / `exit_price` | Float | 成交/出场价（回填） |
| `cost_bps` | Float | 固定 20 |
| `trade_return` | Float | 净收益，`exit/entry - 1 - cost_bps/10000` |
| `status` | String(32) | 见下 |
| `reason_code` | String(64) | 边界原因 |
| `created_at` / `updated_at` / `settled_at` | DateTime | 审计时间 |

写入语义：同一 `(run_id, code)` 重跑时，`pending` 行只刷新 `action / execution_mode / plan_mode / plan_id / entry_zone_text`；终态行永不覆盖。

## 结算口径

与因子回测一致，只读本地 `stock_daily`，不联网：

- 入场：决策日后首个交易日**开盘价**；
- 出场：决策日 + 5 个交易日的**收盘价**；
- 成本：20bp；
- 退市：用最后可成交日收盘价强制出场；
- 一字涨停：不入场，直接判不可成交。

## 状态与边界

| status | 触发条件 | reason_code |
| --- | --- | --- |
| `pending` | 数据未到位，等待下一轮 | — |
| `settled` | 正常入场并取得出场价 | `suspended_at_exit`（停牌后在更晚交易日恢复时用首个可成交收盘） |
| `skipped_missing_entry` | 入场日缺失行情，等待超过 3 个交易日后放弃 | `entry_bar_missing` |
| `skipped_unfillable` | 入场日一字涨停，无法成交 | `entry_limit_locked` |
| `delisted_exit` | 无出场日及更晚行情，用最后可成交收盘 | `delisted_before_exit` |
| `expired_missing_exit` | 超过 10 个交易日仍无可用出场价 | `no_bar_at_exit` |

所有终态都会写 `settled_at`；除正常结算与停牌恢复外 `trade_return` 为空，不留静默 NULL 语义。

## 执行方式

- 后台：`main.py --schedule` 注册每小时一次的结算任务（仅在开关开启时）。
- 手工补跑：`python scripts/settle_decision_outcomes.py [--as-of YYYY-MM-DD]`，输出 JSON 汇总。

## 已知数据限制

结算依赖本地日线覆盖。若 `stock_daily` 最近交易日数据尚未同步或个股长期停牌，记录会停在 `pending`，成熟后按上表转为对应终态；这不是记录逻辑错误。

主库全市场日线现由 `scripts/daily_run.sh` 的 Step 1b（[sync_main_db_daily.py](../../scripts/sync_main_db_daily.py)）按交易日补齐：只拉主库缺失的交易日、`INSERT OR IGNORE` 不覆盖已有行、同步 `adj_factors.db`，单日失败或上游未发布只告警并在下次运行重试。
