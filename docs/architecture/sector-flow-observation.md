# 板块资金流观察值（FAC-01b / FAC-01c，as-built）

> 设计定稿：`2026-11-17/showcase-package-review/outputs/sector-flow-design.md`（方案 A）。
> 本文记录**实际实现**与设计不一致之处，以及运维/评估口径。

## 1. 定位与边界

- `FactSheet.sector_flow` 是一个 **observation 块**：`usage="observation"`、`display_only=True`。
- **不参与**排序、入选、席位打分、聚合权重、否决门。挂载发生在 `allocate_slots` 之后，只写法不读。
- 默认关闭：`AGENT_SECTOR_FLOW_OBSERVATION_ENABLED=false`。关闭时委员会不构建快照、不发额外请求、payload 里 `sector_flow_summary={"enabled": false}`。
- desk LLM 事实包（`seed_facts._compact_fact_sheet`）与 desk 序列化（`experts/desk_base._fact_sheet_to_dict`）**均不包含** `sector_flow`，由守卫测试钉住。

## 2. 代码位置

| 职责 | 文件 |
| --- | --- |
| 快照构建、映射、分档、序列化、对拍 | `src/agent/candidate_experts_v2/sector_flow.py` |
| 参数（窗口/权重/过热/阈值/并发/上限） | `src/agent/candidate_experts_v2/resources/sector_flow.yaml` |
| 行业规范化别名（人工维护） | `src/agent/candidate_experts_v2/resources/sector_aliases.yaml` |
| 数据模型 | `src/agent/candidate_experts_v2/schemas.py::SectorFlowFact` |
| 挂载点 | `src/agent/candidate_experts_v2/committee.py::run_thesis_desk_committee` Step 3.5 |
| 配置透传 | `src/agent/stock_selection.py` |
| 展示 | `apps/dsa-web/src/pages/AgentTracePage.tsx`（候选决策表） |
| 测试 | `tests/test_sector_flow.py` |

未拆分 `sector_flow_context.py` / `sector_flow_snapshot_service.py`：快照读写只有一处调用方，拆成 service 需要新增跨层依赖，收益不足；保持单模块 + 受控 IO，便于单测与回滚。

## 3. 数据源（与设计文档的偏差）

设计假设 StockAPI `hotBkJlrDr` + `bkFlowHistory` 可用。**2026-11-18 实测**：

- `get_stockapi_hot_sectors(limit=100)` 只返回 8 个板块，代码是 `801xxx`（行业分类代码），不是东财 `BKxxxx`；
- `get_stockapi_sector_flow_history("801027")` 返回 `404`（当前账号无该接口权限）。

因此改为 **akshare 东财板块**（与 `DataFetcherManager.get_belong_boards` 同源）：

| 用途 | 接口 |
| --- | --- |
| 行业板块全集（`bk_code` + 名称 + 成分家数） | `ak.stock_board_industry_name_em()` |
| 全市场 5 日资金/涨幅截面（一次请求） | `ak.stock_sector_fund_flow_rank(indicator="5日", sector_type="行业资金流")` |
| 单板块逐日资金流（受控并发） | `ak.stock_sector_fund_flow_hist(symbol=板块名)` |
| 成分股反查（默认关闭） | `ak.stock_board_industry_cons_em(symbol=板块名)` |

**20 股对拍结论**：20/20 个股的行业板块 `bk_code` 在 akshare 行业板块全集中精确命中，且 code 完全相等；每只股票命中 3 个层级行业板块，按“成分股家数最少（更具体）→ `bk_code` 升序”选唯一主板块。

## 4. 映射顺序

1. `bk_code` 精确匹配（主路径，`mapping_method="code"`，`confidence="high"`）；
2. 别名表**归一化后完全相等**（`NFKC` + 去空白 + 大写；禁止子串/正则/包含），`mapping_method="alias"`；
3. 成分股反查（`constituent_fallback=true` 才启用，`mapping_method="constituent"`，`confidence="medium"`）；
4. 都不命中 → `mapping_method="unmapped"`，`missing_reason` 取 `no_bk_code` / `unmapped_industry` / `provider_failed`。

别名命中多个 canonical → 记 `alias_ambiguous`，**不计算**，不猜。

## 5. 缺失分档

| 条件 | `status` | 处理 |
| --- | --- | --- |
| `sample_days < 2` | `missing` | `score/state=None`，`missing_reason=insufficient_history` |
| `2 <= sample_days < window_days` | `partial` | `score_partial=True`，权重按可用分项重归一，日比例分母用 `sample_days` |
| `sample_days >= window_days` | `ok` | 正常计算 |
| 板块涨幅缺失 | 不影响 `status` | `overheat_status="missing"`、`overheat_penalty=None`（按 0 处理，不扣分） |
| 中间某天缺失 | `partial` | 该天跳过，`missing_dates` 记入 `quality_notes` |
| 请求日无快照 | — | 回退最近 3 个交易日；超过则 `missing_reason=date_mismatch` |

`0` 是合法值（净流入 0 / 正流入 0 天 / 过热惩罚 0），一律不当缺失。

## 6. 计算口径

- 分位：`(less + 0.5 * equal) / n`。全量模式下 `n` 是**全部行业板块**（实测 496 个）中 `sample_days >= 2` 的板块数，即 `percentile_sample_size`；只有 `universe_limit > 0` 截断时才退化为“已抓取板块内”。
- 复合分：六项权重见 YAML；缺失分项不参与并重归一权重。
- 分数：流入分支 `0.25 + 0.75 * composite - penalty`；流出分支 `max(0, 0.25 * composite - 0.10 - penalty)`，`clamp(0,1)`。
- 过热：`sector_return_window` 超过 8% 后按 30 斜率扣分，上限 0.18。
- `state`：`persistent_inflow / net_inflow / neutral / outflow / unknown`，阈值在 YAML。

## 7. 快照落库

- 路径：`data/factor_eval_cache/sector_flow_snapshot_{YYYY-MM-DD}.json`（可用 `AGENT_SECTOR_FLOW_CACHE_DIR` 覆盖）。
- 结构：`as_of_date`、`window_dates`、`window_days`、`unit`、`source_chain`、`board_catalog`、`boards`、`errors`，以及口径字段 `percentile_universe`、`universe_coverage`、`percentile_sample_size`、`universe_size`、`catalog_size`、`failed_boards`、`insufficient_history_boards`、`last_error_retry_at`。
- 写入方式：临时文件 + `os.replace` 原子替换；进程内锁避免同一进程重复构建。
- **全量默认**：`universe_limit: 0` 表示抓取全部行业板块（实测 496 个），映射到的板块排在最前，其余按 `bk_code` 升序，构建顺序确定可复现。
- **分位口径**：
  - `percentile_universe="complete"`：`universe_size >= catalog_size` 且 `failed_boards` 为空（全量发起、无抓取失败）；
  - `percentile_universe="partial"`：截断（`universe_limit > 0`）或存在抓取失败的板块，`build_sector_flow_fact` 会把 `percentile_universe=partial(x/y)` 写进 `quality_notes`；
  - `universe_coverage = universe_size / catalog_size` 与 `percentile_sample_size`（真正进入分位样本的板块数）分开记录，便于区分“覆盖率”和“样本量”。
- **当日缓存**：同一交易日重复构建直接命中缓存（实测 2.7ms，无额外请求）；若缓存快照是在 `universe_limit > 0` 时生成的截断快照（`universe_size < catalog_size`），全量模式会重建一次，不会沿用旧截断结果。
- **失败板块单独重试**：`retry_failed=True` 时只重取 `errors` 中的板块，成功后重算截面分位并覆盖原快照；其余板块不重复取数。重试受 `snapshot.error_retry_cooldown_minutes`（默认 10 分钟）冷却约束，冷却内继续命中缓存。
- **全量实测（2026-11-18，akshare 东财）**：496/496 板块 `status=ok`，`failed_boards=[]`，`percentile_universe=complete`，`percentile_sample_size=496`；总请求 498 次（目录 1 + 5 日截面 1 + 逐板块 496），并发 8 下耗时 **20.3s**，折算约 **24.6 req/s**。
- **并发取值依据**（30 板块样本，0 错误）：c=4 → p50 139ms / p95 220ms，折算约 21s；c=8 → p50 229ms / p95 351ms，折算约 16s；c=12 → p50 190ms / p95 245ms，折算约 9s（瞬时约 52 req/s）。选 **8 并发 + 重试 1 次 + 0.5s 线性退避**，为瞬时失败留出重试余量，同时避免把并发压到东财接口的限流阈值。
- **建议每日运行时间窗**：A 股收盘（15:00）后、东财板块资金流完成当日更新后再构建，建议 **15:30 之后**首次触发；构建约 20–30s，单日只跑一次，当日快照 `complete` 后重复触发不再采集。

## 8. 展示位置

`/agent/trace` 的候选决策表（`CandidateDecisionTable`），复用现有列：

- **badges**：`板块:持续流入 / 净流入 / 中性 / 流出 / 未知`；`missing` 显示 `板块资金流:数据不足`；`partial` 追加 `(部分)`。
- **metricHighlights**：`板块5日净流入`、`正流入 n/5 天`、`加速度`、`板块5日涨幅`、`过热惩罚`、`板块数据 <as_of_date>`。
- **riskFlags**：缺失原因（无板块代码/未映射/历史不足/数据源失败/日期不匹配）与 `partial` 重归一说明。
- **evidence**：固定追加一条“板块资金流（仅观察，不构成建议）”。

不新增页面、不改表格交互。

## 9. 开关与回滚

```bash
AGENT_SECTOR_FLOW_OBSERVATION_ENABLED=false   # 默认关闭
AGENT_SECTOR_FLOW_CACHE_DIR=                  # 留空 = data/factor_eval_cache
```

回滚：保持开关 `false` 即恢复原行为；已生成的快照文件不影响主流程，可直接删除。

## 10. 影子期与有效性判定

- 影子期：**60 个交易日**，只积累快照与候选记录，不消费、不影响线上输出。
- 起算日 = 每日影子采集首次 `outcome=complete` 的日期（记录在 `sector_flow_shadow_runs/{date}.jsonl`）。若从 **2026-11-18** 开始，按周一至周五计，第 60 个交易日为 **2027-02-09**；法定节假日会顺延，以运行记录里实际累计的 60 个 `complete` 交易日为准。
- 进入有效性判定需同时满足：
  1. 覆盖率 ≥ 80%（`ok + partial / total`）；
  2. 60 日平均 Rank IC > 0.03 且 t > 2；
  3. 前 20% 组 5 日超额 > 0，且 ≥ 60% 月份成立；
  4. 控制板块 5 日涨幅后偏 IC > 0.02；
  5. 过热惩罚组表现不劣于无惩罚组。
- 未满足任一条 → 判定 `not_evaluable` 或 `fail`，继续只展示。

## 11. 每日影子采集（FAC-01d）

- **入口**：`main.py --schedule`（或 `SCHEDULE_ENABLED=true`）已有的 `background_tasks` 机制，由 `src/scheduler.py` 轮询线程触发；模块 `src/services/sector_flow_shadow_scheduler.py`，任务名 `sector_flow_shadow`。不新增独立进程/定时器。
- **开关**：与观察值共用 `AGENT_SECTOR_FLOW_OBSERVATION_ENABLED`（默认关闭）。关闭时不注册任何后台任务。
- **时间**：工作日 **15:30–18:00（Asia/Shanghai）**，每 600s 检查一次；窗口外直接 `skipped`，不取数。窗口放宽到 18:00 是为了在上游偶发连接重置时多出约 13 次独立重试，避免 15:30–16:00 内三次全失败就当天零样本；当日快照 `complete` 后不再重复采集。
- **只做落盘**：仅调用 `build_or_load_snapshot` 构建/补全快照；不发通知、不生成报告、不参与选股与排序。
- **幂等**：同交易日重复触发命中当日快照缓存（`generated_at` 不变，记录 `cache_hit=true`）；旧截断快照在全量模式下会重建一次。
- **失败隔离与增量**：单板块失败只记入 `failed_boards`，`retry_failed=True` 在冷却（`snapshot.error_retry_cooldown_minutes`，默认 10 分钟）后只重试失败板块；任何异常都被捕获并写入运行记录，后台线程不向上抛，不影响每日主分析。
- **可观察痕迹**：
  - 日志前缀 `[SectorFlowShadow]`：完成/部分完成/未就绪/失败，含 `duration`、`ok`、`failed`、`percentile`、`sample`；
  - 运行记录 `data/factor_eval_cache/sector_flow_shadow_runs/{YYYY-MM-DD}.jsonl`，每次触发一行：`started_at`、`finished_at`、`duration_s`、`cache_hit`、`outcome`（`complete`/`partial`/`not_ready`/`error`/`skipped`）、`boards_ok/partial/missing`、`failed_count`、`failed_boards`、`percentile_universe`、`percentile_sample_size`、`universe_coverage`、`snapshot_as_of`；异常时附 `error`。
- **与 CI 的分工**：`.github/workflows/daily_analysis.yml` 在北京时间 18:00 跑一次性分析，runner 不持久化 `data/`，也不注册该后台任务。影子采集固定在本机 `--schedule` 入口执行，避免本地与 CI 同时请求东财；CI 侧保持 `AGENT_SECTOR_FLOW_OBSERVATION_ENABLED` 关闭。若某个环境只在 CI 运行，观察值仍会在分析时按需构建一次，不依赖影子任务。
- **确认每天真的在跑**：`ls data/factor_eval_cache/sector_flow_shadow_runs/` 应每天新增一个 `{date}.jsonl`；`tail -n 1` 看当天最后一条的 `outcome/cache_hit/duration_s`；日志里检索 `[SectorFlowShadow]`。

## 12. 已知限制

- `main_inflow_pct_avg` 用逐日净占比均值；`sector_return_window` 取自 5 日截面接口的“5日涨跌幅”，与逐日资金流窗口可能相差 1 个交易日（盘中/停牌），已在 `quality_notes` 标注来源。
- 全量模式分位覆盖全部行业板块；若 `universe_limit > 0` 截断，分位宇宙随之收缩，需要以 `quality_notes` 里的 `percentile_universe=partial` 为准。
- akshare 单板块逐日接口在并发 8 下 p50 约 229ms、p95 约 351ms；全量 496 板块约 20s，单板块失败重试 1 次并退避 0.5s。瞬时请求约 25–31 req/s，盘中重复触发会命中当日缓存。
- 新上市板块可能出现 `insufficient_history`（`sample_days < 2`），会单独列入 `insufficient_history_boards`，不记为抓取失败，也不影响其他板块的分位计算。
