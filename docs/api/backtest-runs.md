# 组合回测 API 规格

Base path：`/api/v1/backtest`。认证沿用现有管理员会话/认证开关，不新增用户体系。

## 1. 创建实验

> Web 创建入口已下线 P0，默认 P3；`mode=recorded_plan_replay` 仍保留在 API 以兼容旧客户端/历史复跑。

`POST /api/v1/backtest/runs` → `202`

请求：

```json
{
  "config": {
    "mode": "recorded_plan_replay",
    "source_pipeline": "single_stock",
    "start_date": "2026-01-05",
    "end_date": "2026-01-09",
    "initial_cash": 100000,
    "granularity": "1d",
    "price_basis": "daily_declared_unadjusted",
    "missing_data_policy": "strict",
    "risk_profile": "balanced",
    "max_single_position_pct": 100,
    "max_total_exposure_pct": 100,
    "fee_policy_ref": "cn-a-share-2023.v1",
    "slippage_bps": 5,
    "participation_rate": 0.01,
    "corporate_action_policy": "assume_none",
    "plan_ids": [],
    "trace_ids": [],
    "selection_links": [],
    "benchmark": null
  },
  "allow_incomplete": false
}
```

- `config` 使用 `src/backtest/schemas.py::ExperimentConfig`；未传字段使用默认值。
- `risk_profile` 会解析成冻结的 `risk_policy`（保守 10/40、平衡 20/80、激进 40/100），并写入配置/指纹。
- `source_pipeline=selection_confirmed` 时必须提供 `selection_links`，否则纳入计划为空并返回 `no_included_plans`。
- `allow_incomplete=false` 时先做数据预检；缺失 5 分钟/日线覆盖返回 `precheck_blocked`。
- 相同 request fingerprint 已有 queued/validating/running 时返回现有 run，`deduplicated=true`。
- 成功后返回 `RunSummaryResponse`，`status=queued`。

错误：

| HTTP | error | 说明 |
| --- | --- | --- |
| 422 | `no_included_plans` | 结构化计划/决策/父链筛选后为空，`detail.inputs` 保留审计 |
| 422 | `precheck_blocked` | 覆盖不足，`detail.blocking_reasons` 给出原因 |
| 422 | `invalid_date_range` / `date_range_too_large` / `too_many_plans` | 参数边界 |
| 400 | `not_cancellable` 等 | 状态冲突 |
| 500 | `internal_error` | 未处理异常 |

### P3 历史 Agent 研究请求

```json
{
  "config": {
    "mode": "historical_agent_research",
    "source_pipeline": "research",
    "start_date": "2026-01-05",
    "end_date": "2026-06-30",
    "initial_cash": 100000,
    "granularity": "1d",
    "price_basis": "daily_declared_unadjusted",
    "risk_profile": "balanced",
    "corporate_action_policy": "assume_none",
    "fee_policy_ref": "cn-a-share-2023.v1",
    "plan_ids": [],
    "trace_ids": [],
    "selection_links": [],
    "research": {
      "universe_mode": "auto_local_coverage",
      "universe": [],
      "auto_universe_limit": 100,
      "confirmation_mode": "auto_accept",
      "entry_mode": "adaptive",
      "order_valid_trading_days": 15,
      "decision_frequency": "monthly",
      "selection_top_n": 5,
      "max_confirmations_per_decision": 5,
      "max_llm_calls": 500,
      "min_history_bars": 40,
      "model_ref": null,
      "prompt_version": "research-prompt.v1",
      "plan_version": "research-plan.v1"
    }
  },
  "allow_incomplete": false
}
```

P3 不使用 `plan_ids` / `trace_ids` / `selection_links`；这些字段必须为空。计划在回测过程中生成，预检不会返回 `no_included_plans`。P3 run 进入 `research` 队列。通过 `server:app` / `python main.py --serve` 启动时，API 进程内会自动运行 standard + research 队列；也可额外用 `python -m src.backtest.worker --kind research` 扩容。`uvicorn api.app:app` 不启用自动调度。

`entry_mode` 三种：`adaptive`（默认，突破+回踩）、`pullback`、`breakout`。`order_valid_trading_days` 默认 15，控制限价单有效期。

`confirmation_mode` 两种：

- `auto_accept`（默认）：选股后直接生成计划，不再逐只调用确认 LLM；结果标 `auto_accept_selection`，用于观察纯 AI 选股。
- `llm`：逐只单股确认，可能 reject；调用预算会相应增加。

`universe_mode` 两种：

- `explicit`：必须提供 `universe`（≤50），用于受控实验。
- `auto_local_coverage`：`universe` 留空，从全市场 Sequoia 日线库扫描有足够 as-of 历史的 A 股，按成交额/bar 数预筛 `auto_universe_limit` 个候选；返回 `local_coverage_universe` 标签，仍不是严格 PIT。

Sequoia 当前覆盖约 5,599 个标的、2025-10-14 到 2026-11-06。OHLC 原为 `legacy_hfq_bridge`，P3 用成交额/成交量回推到现金价格尺度并标 `research_price_basis_rebased`；不是交易所原始未复权价格。

P3 预检额外字段：

```json
{
  "diagnostics": {
    "research": {
      "universe": ["300308", "300502", "300274"],
      "universe_size": 3,
      "universe_mode": "auto_local_coverage",
      "universe_source": "sequoia_local_coverage",
      "data_source": "sequoia:vendor/Sequoia-X/data/sequoia_v2.db",
      "decision_dates": ["2026-01-05", "2026-02-02", "2026-03-02"],
      "decision_count": 3,
      "estimated_llm_calls": 18,
      "max_llm_calls": 500,
      "model_ref": "deepseek/...",
      "data_coverage": {"symbol_count": 5599, "min_date": "2025-10-14", "max_date": "2026-11-06"},
      "tool_status": {"fundamental": "gap", "news": "gap", "realtime_search": "blocked"},
      "labels": ["survivor_bias", "model_knowledge_leakage_risk", "llm_nondeterminism", "tool_gap"]
    }
  }
}
```

P3 结果状态固定为 `partial`，并带研究标签；只能作为研究结论，不能冒充生产策略收益或可托管收益。

### 基准曲线

`GET /api/v1/backtest/runs/{run_id}/benchmarks` 返回展示用多指数曲线：

- 上证指数 `000001.SH`
- 创业板指 `399006.SZ`
- 科创50 `000688.SH`
- 沪深300 `000300.SH`

所有曲线按 run 的初始权益归一化，只用于图表对比，不参与撮合、仓位、现金或盈亏计算。首次请求优先用 Tushare `index_daily`，本地 Sequoia 有数据时兜底，并缓存到 run 目录 `benchmarks.json`。

响应同时返回：

- `strategy_return_pct`：策略区间收益；
- `shanghai_return_pct`：上证指数区间收益；
- `excess_vs_shanghai_pct`：`策略 - 上证`，正数表示跑赢上证；
- `benchmark_returns`：各指数区间收益。

Web 结果页顶部“上证超额”卡片直接展示 `excess_vs_shanghai_pct`，副标题显示上证收益。

## 2. 列表 / 详情

`GET /api/v1/backtest/runs?status=&mode=&source_pipeline=&page=1&page_size=20`

返回 `{total, page, page_size, items: RunSummaryResponse[]}`。

`GET /api/v1/backtest/runs/{run_id}` → `RunDetailResponse`：

- 基础字段：`run_id`、`mode`、`source_pipeline`、`risk_profile`、`status`、`attempt`、`progress`、`phase`、`data_quality`、`quality_flags`、`metrics`、`counts`、`error_code`、`error_message`
- `manifest`：快照 manifest（sessions/symbols/coverage/content_hash 等）
- `coverage`：按标的的覆盖汇总
- `inputs`：纳入/排除计划、原因、父链
- `diagnostics`：执行/数据诊断
- `result_available`：账本产物是否可读

状态机：`queued → validating → running → succeeded / partial / failed / cancelled / interrupted`。`partial` 只能研究展示，不伪装完整成功。

## 3. 明细表

均支持 `page` / `page_size`（≤500）：

- `GET /runs/{id}/equity`
- `GET /runs/{id}/orders`
- `GET /runs/{id}/fills`
- `GET /runs/{id}/trades`
- `GET /runs/{id}/positions`
- `GET /runs/{id}/inputs?included=true|false`
- `GET /runs/{id}/diagnostics`
- `GET /runs/{id}/metrics`

返回 `{total,page,page_size,items}` 或 `{items}`；金额/价格为数值，精度以导出 CSV/JSON 为准。

## 4. 取消 / 重跑

- `POST /runs/{id}/cancel`：queued 直接取消；running 在安全事件边界生效，保留诊断，不发布成功指标。终态返回 `not_cancellable`。
- `POST /runs/{id}/rerun`：创建新 attempt，force 入队；数据未变且前次 succeeded 时按完整 fingerprint 复用结果，返回 `reused_from_run_id`。

## 5. 导出

`GET /runs/{id}/export?kind=config|manifest|metrics|inputs|diagnostics|ledger|orders|fills|equity|trades`

- config/manifest/metrics/inputs/diagnostics → JSON
- ledger/orders/fills/equity/trades → CSV（流式）
- 导出不含密钥；只读取配置的 run root，不接受任意路径。

## 6. 数据预检 / 分钟线同步

`POST /api/v1/backtest/prechecks`

```json
{"config": { ...ExperimentConfig... }}
```

→ `202 DataJobResponse`（`job_id`、`job_type=precheck`、`status=queued`）

`GET /api/v1/backtest/prechecks/{job_id}`：轮询 `status`、`result`。`result` 为 `PrecheckResult`：

```json
{
  "status": "ok|warning|blocked",
  "included_plans": 1,
  "excluded_plans": 0,
  "symbols": ["600519"],
  "sessions": 5,
  "expected_bars_per_session": 48,
  "coverage": [],
  "gaps": [],
  "inputs": [],
  "blocking_reasons": [],
  "warnings": ["market_rules_unverified"],
  "diagnostics": {"exclusion_reasons": {"decision_status:observe_only": 3}},
  "quality": "partial"
}
```

`POST /api/v1/backtest/minute-bars/sync`

```json
{
  "symbols": ["600519"],
  "start_date": "2026-01-01",
  "end_date": "2026-01-31",
  "frequency": "5",
  "adjustflag": "3",
  "config": null
}
```

→ `202 DataJobResponse`；`GET /api/v1/backtest/data-jobs/{job_id}` 轮询。网络下载是独立 job，纯回测不会静默下载。

## 7. 辅助资源

- `GET /api/v1/backtest/candidates?start_date=&end_date=&symbol=&limit=`：结构化价格计划候选（含 decision 状态、trace_refs），用于新建表单。
- `GET /api/v1/backtest/risk-policies`：保守/平衡/激进预设。
- `GET /api/v1/backtest/fee-policies`：版本化费用策略与规则窗口。

## 8. 幂等与一致性

- `request_fingerprint` = 规范化配置 + engine version + git revision/dirty + 计划 hash + calendar/fee/risk policy 版本。
- `experiment_fingerprint` = request fingerprint + 快照 content hash + 规则/公司行为版本 + 纳入输入 hash。
- 相同 `experiment_fingerprint` 的 succeeded run 可复用；新执行生成新 attempt，不覆盖旧结果。
- 订单/成交/账本事件/持仓批次/权益点按时间戳事务提交；核心不变量在每步校验。
