# 估值行业/历史分位观察值（FAC-03，as-built）

> 本文记录实际实现与字段口径。条目定义见 `2026-11-17/showcase-package-review/outputs/TODO.md` 的 FAC-03。

## 1. 定位与边界

- `FactSheet.valuation` 是一个 **observation 块**：`usage="observation"`、`display_only=True`。
- **不参与**排序、入选、席位打分、阈值判定与否决门，也不改变 `action`。挂载发生在 `allocate_slots` 之后，只写法不读。
- desk LLM 事实包（`experts/desk_base._fact_sheet_to_dict`）、seed 事实包（`seed_facts._fact_sheet_to_dict`）和模型精简事实（`seed_facts._compact_fact_sheet`）**均不包含** `valuation`，由守卫测试钉住。
- 默认关闭：`AGENT_VALUATION_FACT_ENABLED=false`。关闭时不读缓存、不写字段，payload 里 `valuation_summary={"enabled": false}`。

## 2. 代码位置

| 职责 | 文件 |
| --- | --- |
| 读取缓存、计算分位、序列化 | `src/agent/candidate_experts_v2/valuation_fact.py` |
| 数据模型 | `src/agent/candidate_experts_v2/schemas.py::ValuationFact` |
| 挂载点 | `src/agent/candidate_experts_v2/committee.py::run_thesis_desk_committee` Step 3.6 |
| 参数透传与 payload 白名单 | `src/agent/stock_selection.py` |
| 配置 | `src/config.py`、`src/core/config_registry.py`、`.env.example` |
| 测试 | `tests/test_valuation_fact.py` |

## 3. 数据源

只读本地缓存，**不联网**：

- `data/factor_eval_cache/basic_YYYYMMDD.csv`：逐交易日全市场截面，取 `ts_code`、`pe_ttm`；按日期排序后取 `as_of_date` 之前最近 120 个交易日。
- `data/factor_eval_cache/industry_snapshot_sw2021.csv`：申万一级当前归属（`l1_code` / `l1_name` / `ts_code` / `in_date` / `out_date`），只用 `out_date` 为空的行，同码取 `in_date` 最新一条。

缺文件或列缺失不补数、不下载，直接按缺失处理。

## 4. 字段契约（共 5 个指标 + 1 个标记）

| 字段 | 含义 |
| --- | --- |
| `pe_ttm` | 最近交易日 PE(TTM)；当日全市场截面缺失时回退到个股序列最后一个非空值，并记 `pe_ttm_from_last_observation` |
| `industry_pe_median` | 同申万一级、`pe_ttm > 0` 的同业 PE 中位数（含自身） |
| `industry_pe_percentile` | 个股 PE 在同行正 PE 样本中的分位，值域 0–1 |
| `history_pe_percentile` | 个股 PE 在自身 120 日窗口正 PE 观测中的分位，值域 0–1 |
| `pe_change_20d` | 相对 20 个观测前的 PE 变化，单位 %；任一端 ≤0 或缺失则为 `None` |
| `pe_negative` | 亏损标记，`pe_ttm <= 0` 时为 `True` |

辅助元数据：`industry_source="sw2021_l1"`、`industry_code`、`industry_name`、`as_of_date`（请求日）、`data_as_of_date`（实际取到的最后交易日）、`industry_peer_count`、`history_observations`、`status`、`missing_reason`、`quality_notes`。

## 5. 计算与缺失分档

- 分位：`样本中 <= 当前值 的占比`，保留 4 位小数；只用正 PE 样本。
- 行业分位：同业样本（含自身）≥ 8 才计算，否则 `missing_reason=industry_peers_below_minimum`。
- 历史分位：正 PE 观测 ≥ 20 才计算（窗口 120 个交易日），否则 `missing_reason=history_observations_below_minimum`。
- 亏损股（`pe_ttm <= 0`）：只置 `pe_negative=True` 并记 `pe_negative_excluded_from_percentiles`，两个分位均为 `None`，**不一票否决**。
- `status`：两个分位都有 → `ok`；否则 → `partial`；完全取不到 PE → `missing`。
- `missing_reason` 优先级：`pe_ttm_missing` > `industry_unavailable` > `industry_peers_below_minimum` > `history_observations_below_minimum`。

## 6. 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_VALUATION_FACT_ENABLED` | `false` | 观察块总开关 |
| `AGENT_VALUATION_CACHE_DIR` | 空（用 `data/factor_eval_cache`） | 缓存目录 |

## 7. 明确不做

- **动态 PE（`pe_dynamic`）**：本地缓存只有 `pe_ttm`，动态 PE 需在线拉取，与本模块"只读本地、不联网"的约定冲突。不保留 `None` 占位，也不产生告警。
- **估值综合评分（`valuation_score`）**：行业分位、历史分位与 20 日变化之间没有经过评估的确定性权重口径，合成总分等于新增未经验证的决策输入。本模块只输出原始分位与变化，不打分。

## 8. 测试

`tests/test_valuation_fact.py` 覆盖：分位与 20 日变化、亏损股排除、同业样本不足、历史观测不足、120 天窗口截断、20 日前 PE≤0、缓存缺失降级、展示序列化，以及 desk/seed 隔离。
