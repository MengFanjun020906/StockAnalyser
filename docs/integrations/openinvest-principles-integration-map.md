# openInvest 原理实际接入点索引

本文回答一个具体问题：本项目哪些地方已经实际吸收了 `openInvest` 的投资委员会原理，而不是只做了前端展示或概念文档。

结论先说：接入点主要在后端 Agent 选股链路、工具层、Trace 审计和离线复盘里。它不是把 `openInvest` 整仓搬进来，而是把可复用的机制拆成小模块接到本项目已有链路中。

## 1. 总览

| openInvest 原理 | 本项目实际落点 | 运行入口 | 可查证产物 |
| --- | --- | --- | --- |
| Regime 状态识别 | `src/agent/regime.py`、`src/agent/tools/market_tools.py::detect_market_regime` | `src/agent/stock_selection.py::run_stock_selection_pipeline` 在候选发现后调用 | `tool_calls.json`、`selection_context.json`、`final_report.json.market_regime` |
| Regime forward probability / 买回参考 | `src/agent/regime_probability.py`、`src/agent/tools/market_tools.py::get_regime_forward_probability` | `stock_selection.py::_attach_regime_forward_probability()` 紧跟 `detect_market_regime` 执行 | `market_regime.forward_probability`、`pricing_agent.json`、`final_report.json` |
| 新闻源适配 | `src/agent/tools/search_tools.py::search_openinvest_news` | Agent 工具注册后由 planner / search 阶段调用 | `tool_calls.json` 中的 `search_openinvest_news` 调用和 `source_chain` |
| 多席位候选发现 | `src/agent/candidate_experts_v2/committee.py::run_thesis_desk_committee` | `AGENT_CANDIDATE_DISCOVERY_MODE=thesis_desk_committee`，当前也是默认主链路 | `candidate_discovery.json`、`seed_pool.json`、`expert_packets.json` |
| 反方审查 / 辩论 | `src/agent/stock_selection.py` 的 `adversarial_review` 阶段；通用 debate 在 `src/agent/debate.py` | 选股链路进入 Judge 前执行反方审查；`planning_execute` 证据链路会写 `debate.json` | `adversarial_review.json`、`debate.json`、`final_report.json.adversarial_review` |
| Judge 确定性后处理 | `src/agent/judge_sanity.py::apply_judge_sanity` | `judge_decision` LLM 输出后立即执行 | `judge_decision.json.full.sanity_checks`、`judge_sanity.json` |
| LLM telemetry | `src/agent/llm_telemetry.py`，由 `src/agent/llm_adapter.py` 和阶段 scope 写入 | Trace 运行时通过 `llm_telemetry_scope(trace_id, artifact_dir)` 激活 | `llm_usage.jsonl`、`llm_telemetry.json` |
| Dreaming / verdict review | `src/services/agent_verdict_review_service.py`、`scripts/build_agent_verdict_reviews.py`、`scripts/build_agent_verdict_insights.py` | 离线扫描本地 Trace 和本地 `StockDaily`，不重跑 Agent | `data/agent_reviews/verdict_review.jsonl`、`data/agent_reviews/insights/agent_verdict_insights.md` |

## 2. 运行链路

当前 `watchlist_scan` 的主链路不是前端驱动，而是在 `src/agent/stock_selection.py` 里串起来：

```text
candidate_discovery
  -> detect_market_regime
  -> get_regime_forward_probability
  -> balanced_candidate_evidence
  -> candidate_screening
  -> single_stock_deep_dive
  -> meta_orchestrator
  -> pricing_agent
  -> portfolio_allocation
  -> adversarial_review
  -> judge_decision
  -> apply_judge_sanity
  -> final_report
```

也就是说，Regime、概率层、席位委员会、反方审查和 Judge sanity 都在生成最终报告之前参与了真实决策。前端只是读取这些产物和状态。

## 3. Regime 识别与概率层

### 3.1 状态识别层

实际入口：

- `src/agent/regime.py`
- `src/agent/tools/market_tools.py::detect_market_regime`
- `src/storage.py` 的 `market_regime_state` 持久化
- `src/agent/stock_selection.py::_run_market_regime_tool()`

`stock_selection` 会在候选发现完成后调用：

```python
ctx.market_regime = _run_market_regime_tool(ctx=ctx, tool_registry=tool_registry)
_attach_regime_forward_probability(ctx=ctx, tool_registry=tool_registry)
```

这个状态不只是 prompt 背景。`_apply_market_regime_constraints()` 会在 `risk_off / panic / extreme` 或高波动状态下确定性压低仓位，必要时把 `open` 降成 `wait`。

### 3.2 Forward probability / reentry

实际入口：

- `src/agent/regime_probability.py`
- `src/agent/tools/market_tools.py::get_regime_forward_probability`
- `src/agent/stock_selection.py::_attach_regime_forward_probability()`
- `src/agent/stock_selection.py::_compact_regime_forward_probability()`

这层是从 openInvest 借鉴后重写到本仓库的能力：不是只给 `trending_up / risk_off` 这类标签，而是输出 7 / 30 / 60 / 90 日历史 forward return 分布、`effective_n`、`low_confidence`、路径画像和 `reentry_reference`。

边界也要说清：概率层只作为后验证据和买回参考，不能直接绕过 Judge 或 Risk Gate 触发买卖。工具自身也写了 guardrail：`low_confidence=true` 时不能作为主要决策依据。

## 4. 新闻获取

实际入口：

- `src/agent/tools/search_tools.py::_handle_search_openinvest_news()`
- `src/agent/tools/search_tools.py::search_openinvest_news_tool`

这是最直接接入 `openInvest` 代码的一处。工具会查找仓库旁边的 `openInvest` 目录，把它加入 import path，然后使用：

```python
from services.news_sources import fetch_all
from services.news_sources.rss_feed import load_default_feeds
```

默认优先 `yfinance` ticker-linked news；RSS 和 DDGS 是可选项。返回结果里有 `source_chain`、`errors`、`message_score`、`event_tags`、`risk_flags`，所以可以在 Trace 里看到到底是 yfinance、RSS、DDGS 哪个源有结果，哪个源缺依赖或为空。

如果 `openInvest` 目录不存在，工具会返回：

```json
{
  "status": "unavailable",
  "provider": "openInvest.news_sources",
  "source_chain": [{"result": "missing_openInvest_dir"}]
}
```

这意味着它不是静默 fallback。看 `tool_calls.json` 就能判断这条链路是否真实跑过。

## 5. 多席位委员会

实际入口：

- `src/agent/candidate_experts_v2/committee.py::run_thesis_desk_committee()`
- `src/agent/candidate_experts_v2/aggregator.py::aggregate_desk_picks()`、`allocate_slots()`
- `src/agent/candidate_experts_v2/experts/*_desk.py`
- `src/agent/stock_selection.py::_run_candidate_discovery_tool()`

当前配置默认是：

```text
AGENT_CANDIDATE_DISCOVERY_MODE=thesis_desk_committee
```

`run_thesis_desk_committee()` 的核心流程是：

```text
build_recall_pool
  -> [EarlyTurn | Momentum | QualityRepair | ThemeCatalyst] desks parallel
  -> aggregate_desk_picks
  -> allocate_slots
  -> discover-compatible payload
```

这里吸收的是 openInvest “多角色委员会”的思想，但实现已经改成本项目的选股席位：早期拐点、动量、质量修复、题材催化。它不是前端图谱；候选池输出会真实进入后续 `candidate_screening`、`single_stock_deep_dive`、`portfolio_allocation`。

可查证字段：

- `candidate_discovery.json.candidate_source = "thesis_desk_committee"`
- `candidate_discovery.json.thesis_desk_committee`
- `candidate_discovery.json.negative_conclusion_reasons`
- `seed_pool.json`
- `expert_packets.json`

## 6. 反方审查与辩论

本项目有两条相关链路，需要区分：

1. `watchlist_scan` 选股链路里的 `adversarial_review` 阶段。
2. 通用 `planning_execute` 工具证据之后的 `src/agent/debate.py` 辩论链路。

选股链路里，`adversarial_review` 在 `portfolio_allocation` 之后、`judge_decision` 之前执行。它会读取候选发现、证据包、筛选、深挖、Meta、点位、组合配置、Regime 和 evidence ledger，然后输出反方风险点、证据缺口和建议裁决。

通用 debate 链路则由 `src/agent/debate.py` 提供 Primary Thesis、Adversarial Thesis、Debate Judge，并在 Agent Trace 里落 `debate.json`。这更接近 openInvest 的“主观点、强制反方、Judge”协议。

边界：当前 `watchlist_scan` 的 `adversarial_review` 是阶段化反方审查，不等于每个席位完全信息隔离的多轮委员会。已有评估文档把“更严格的信息隔离和多轮交叉质询”列为后续增强方向。

## 7. Judge sanity

实际入口：

- `src/agent/judge_sanity.py::apply_judge_sanity()`
- `src/agent/stock_selection.py` 中 `judge_decision` 后处理

这是从 openInvest `parse_cio_memo` 类机制借鉴来的确定性后处理。它解决的问题是：不要只相信 LLM Judge 自己遵守规则。

当前规则包括：

- 上游 worker 不可用且 Judge 仍给主动交易动作时，降级为 `wait`。
- Judge 给主动开仓，但组合配置没有任何可执行开仓仓位时，降级为 `wait`。
- 市场处于 `risk_off / panic / extreme` 时，主动开仓裁决降级。
- 单票仓位超过配置上限时 clamp，并把调整后的 allocation 写回。

可查证字段：

- `judge_decision.json.full.sanity_checks`
- `judge_decision.json.full.required_plan_changes`
- `judge_sanity.json`
- `summary.json.judge_sanity`

## 8. LLM telemetry

实际入口：

- `src/agent/llm_telemetry.py`
- `src/agent/llm_adapter.py`
- `src/agent/stock_selection.py` 各阶段 `llm_telemetry_scope(stage=..., agent_role=...)`
- `api/v1/endpoints/agent.py::TraceArtifactWriter`

Trace 运行时会设置：

```python
llm_telemetry_scope(trace_id=session_id, artifact_dir=str(artifact_writer.path))
```

每次 LLM 调用会写入：

- `trace_id`
- `agent_role`
- `symbol`
- `stage`
- `provider`
- `model`
- token
- latency
- estimated cost
- tool_calls
- ok / error

产物：

- `llm_usage.jsonl`
- `llm_telemetry.json`

这对应 openInvest 的可观测性原则：每个角色、阶段、成本和失败原因都要能在审计链里还原。

## 9. Dreaming / verdict review

实际入口：

- `src/services/agent_verdict_review_service.py`
- `scripts/build_agent_verdict_reviews.py`
- `scripts/build_agent_verdict_insights.py`
- `api/v1/endpoints/agent_verdict_reviews.py`

这条链路只读本地 Trace 和本地 `StockDaily`，把过去的 Judge 决策和未来 7 / 30 日表现对齐，生成后验复盘样本。

产物：

- `data/agent_reviews/verdict_review.jsonl`
- `data/agent_reviews/insights/agent_verdict_insights.md`

边界：当前复盘洞察只供人工复盘，不自动注入线上选股 prompt，也不会重跑 Agent 或外部行情。这是有意保守，避免离线复盘结果无审计地影响线上决策。

## 10. 怎么确认某次 Trace 真的跑了

给一个 Trace 目录，例如：

```text
data/agent_traces/<timestamp>-<session_id>/
```

按下面顺序查：

1. `tool_calls.json`：搜索 `detect_market_regime`、`get_regime_forward_probability`、`search_openinvest_news`。
2. `candidate_discovery.json`：看 `candidate_source` 是否为 `thesis_desk_committee`，以及是否有 `thesis_desk_committee`、`negative_conclusion_reasons`。
3. `selection_context.json` 或 `final_report.json`：看 `market_regime.forward_probability` 是否存在。
4. `pricing_agent.json`：看是否输出 `regime_probability` / `reentry_reference`。
5. `adversarial_review.json`：看反方审查是否进入 Judge 前证据链。
6. `judge_decision.json` 和 `judge_sanity.json`：看 sanity 是否产生审计规则。
7. `llm_usage.jsonl` 和 `llm_telemetry.json`：看各阶段 LLM 调用、token、耗时和失败。
8. `debate.json`：如果是 `planning_execute` 或通用工具证据链，查看主观点、反方和 Judge 裁决。

## 11. 还没有完全接入的部分

这些不要误判成已经完成：

- 没有把 `openInvest` 的整套 Core / Memory / Jobs / Web API 搬进来。
- 没有直接挂载 `openInvest` 的前端 dist；之前评估过，`openInvest` 的 GUI dist 来自独立发布包，本项目不建议直接接。
- `watchlist_scan` 的反方审查已经真实接入，但还不是严格多轮、信息隔离的委员会协议。
- Regime probability 已接入市场代理指数层，单股级 `symbol_regime_probability` 和更细的买回点约束仍是后续增强。
- Verdict review 已能离线生成后验样本和 insight，但当前不会自动反哺线上决策。
- PnL / benchmark 展示仍主要是可借鉴方向，不是本次列出的核心已接入决策链。

## 12. 相关文档

- [openInvest 可接入组件评估](openinvest-integration-assessment.md)
- [A 股 Regime 状态机原理](../modules/regime-state-machine.md)
- [专家委员会开发指南](../modules/expert-committee-dev-guide.md)
- [完整指南：Agent Trace 产物](../full-guide.md)
