# Agent 选股阶段当前实现说明

最后核对：2026-05-23

本文档描述当前工作区里 `watchlist_scan` 选股链路的实际实现，用于排查 Agent Trace、候选池、最终报告和前端展示问题。它不是目标方案文档，也不等同于 Prompt 草案。

## 一句话结论

当前选股不是单个 LLM prompt 直接给推荐，而是一条后端阶段化流水线：

```text
识别 watchlist_scan
  -> L1 多专家候选召回
  -> 市场环境检测
  -> 均衡候选取证
  -> 候选初筛
  -> 单股深挖
  -> 组合配置
  -> 反方审查
  -> Judge 裁决
  -> 最终 JSON + Markdown 渲染
  -> Trace artifact + 前端展示
```

候选分只表示 L1 召回强度，不等于买入推荐分。最终展示里的“立即入场 / 条件入场 / 强观察 / 等待 / 排除”是在报告渲染层把候选池、单股深挖和组合配置结果合并后派生出来的执行语义。

## 入口与触发条件

主要入口在 `src/agent/stock_selection.py`：

- `SELECTION_INTENT = "watchlist_scan"`
- `should_run_stock_selection(agent_user_context)`
- `run_stock_selection_pipeline(...)`

选股流水线只在以下条件同时满足时运行：

```text
agent_user_context.report.analysis_mode == "planning_execute"
agent_user_context.report.intent == "watchlist_scan"
```

Agent Trace 页面常见入口在 `api/v1/endpoints/agent.py`：

- `POST /api/v1/agent/trace/run`
- `POST /api/v1/agent/trace/stream`
- `GET /api/v1/agent/trace/sessions/{session_id}`

Trace 请求会先通过 `_build_trace_context()` 构造 `AgentUserContext`。如果用户明确要求“选股、候选、下周可关注、可入手股票”等 watchlist 场景，才会进入 `watchlist_scan`。否则会落到 `qa`、`entry_analysis` 或 `position_review`，不跑这条选股流水线。

运行层有两条调用路径：

- `src/agent/executor.py`：执行器里检测到 `watchlist_scan` 后调用 `run_stock_selection_pipeline()`。
- `src/agent/orchestrator.py`：多 Agent 编排路径里也会按相同条件接入阶段化选股。

## 运行时对象

| 对象 | 位置 | 作用 |
| --- | --- | --- |
| `AgentUserContext` | `src/schemas/agent_context.py` | 用户请求、账户、持仓、风险偏好、报告意图的统一上下文。 |
| `SelectionRunContext` | `src/agent/stock_selection.py` | 单次选股运行的内部状态，保存市场、账户摘要、各阶段结果、工具调用、证据账本、专家图谱状态和 token 使用。 |
| `SelectionStage` | `src/agent/stock_selection.py` | 阶段结果容器，固定包含 `status`、`summary`、`full_ref`、`full`。 |
| `StockSelectionResult` | `src/agent/stock_selection.py` | 选股流水线对外结果，包含 `final_report_json`、`final_markdown`、`selection_context`、`tool_calls_log`。 |
| `ToolRegistry` | `src/agent/tools/registry.py` | 统一执行底层数据工具。 |
| `LLMToolAdapter` | `src/agent/llm_adapter.py` | 调用 LLM 生成各阶段 JSON。 |
| `TraceArtifactWriter` | `api/v1/endpoints/agent.py` | 把 Trace 请求、事件、工具调用、选股结果和最终报告落盘。 |

`SelectionRunContext.set_stage()` 会把每个阶段统一规整为：

```json
{
  "status": "ok/partial/insufficient_data/failed",
  "summary": {},
  "full_ref": "stage_name.json",
  "full": {}
}
```

下游 prompt 默认注入 `summary`，完整排障和前端展开依赖 `full` 或落盘 artifact。

## 阶段总览

| 顺序 | 阶段 | 主要函数 | 关键输入 | 关键输出 | Trace 事件 |
| --- | --- | --- | --- | --- | --- |
| 0 | 启动 | `run_stock_selection_pipeline()` | 用户问题、账户上下文、工具注册表 | `SelectionRunContext` | `selection_start` |
| 1 | 候选发现 | `_run_candidate_discovery_tool()` + `build_candidate_discovery_prompt()` | 用户种子、市场、候选策略、`discover_watchlist_candidates` 结果 | `candidate_discovery` | `selection_candidate_discovery_done` |
| 2 | 市场环境 | `_run_market_regime_tool()` | 市场类型 | `market_regime` | `selection_market_regime_done` |
| 3 | 均衡取证 | `_build_balanced_candidate_evidence_stage()` | L1 候选池 | `balanced_candidate_evidence`、`candidate_evidence.json`、`candidate_evidence.md` | `selection_balanced_candidate_evidence_done` |
| 4 | 候选初筛 | `_collect_base_evidence()` + `build_candidate_screening_prompt()` | 候选池摘要、均衡证据、指数、板块、行情 | `candidate_screening` | `selection_candidate_screening_done` |
| 5 | 单股深挖 | `_collect_deep_dive_evidence()` + `build_deep_dive_prompt()` | 深挖标的、EvidenceCard、市场环境、初筛摘要 | `single_stock_deep_dive` | `selection_deep_dive_done` |
| 6 | 组合配置 | `build_portfolio_allocation_prompt()` | 深挖结果、账户约束、可用现金、持仓 | `portfolio_allocation` | `selection_allocation_done` |
| 7 | 反方审查 | `build_adversarial_review_prompt()` | 候选、证据、配置方案 | `adversarial_review` | `selection_adversarial_done` |
| 8 | Judge 裁决 | `build_judge_decision_prompt()` | 配置方案、反方审查、市场环境 | `judge_decision` | `selection_judge_done` |
| 9 | 专家图谱 | `_maybe_run_expert_graph()` | 候选池、深挖、配置、Judge | `expert_state` | `selection_expert_graph_done` |
| 10 | 最终渲染 | `_build_final_report_json()` + `render_stock_selection_markdown()` | 全部阶段结果 | `final_report_json`、`final_markdown` | `selection_done` |

任何 LLM 阶段 JSON 解析失败时，`_call_stage_json()` 会使用对应 fallback，并在 `full.tool_failures` 中记录 `llm_json_parse_failed` 和截断后的原始输出。单个工具失败通常不会中断整轮选股，而是以 `tool_failed` 进入证据缺口和 Trace。

## L1 候选发现

底层工具是 `discover_watchlist_candidates`，定义在 `src/agent/tools/market_tools.py`。

用户如果显式传入 `seed_symbols`，候选发现会直接返回用户种子；否则当前自动候选发现主要支持 A 股 `cn` 市场。非 A 股自动发现会返回 `not_supported`。

`candidate_source = "auto"` 时，会调用 `CandidateExpertOrchestrator`，位置在 `src/agent/candidate_experts/orchestrator.py`。当前候选专家包括：

| 专家 | 维度 | 说明 |
| --- | --- | --- |
| `strategy_factor_expert` | 策略/因子 | 使用 AlphaSift YAML、本地策略数据等生成策略候选。 |
| `technical_candidate_expert` | 技术 | 使用 Sequoia 等技术策略生成候选。 |
| `sector_theme_expert` | 板块主题 | 根据热点板块和板块成分生成候选。 |
| `news_event_expert` | 消息事件 | 根据公告、新闻、事件影响生成候选。 |
| `sentiment_theme_expert` | 情绪热点 | 根据热榜、涨停、情绪主题生成候选。 |
| `fundamental_expert` | 基本面 | 使用本地基本面快照和 TuShare 基本面工具生成候选。 |
| `capital_flow_expert` | 资金 | 使用 TuShare/StockAPI 资金、龙虎榜、热钱等工具生成候选。 |

多专家结果会合并为：

```json
{
  "status": "ok/partial",
  "candidate_source": "expert_graph_discovery/fallback",
  "candidates": [],
  "candidate_count": 0,
  "expert_packets": [],
  "themes": [],
  "quality": {},
  "hard_exclusion": {},
  "capacity": {},
  "discovery_steps": []
}
```

这里的 `candidates` 是 L1 召回池，只代表“值得继续分析”。它不是最终推荐，也不代表可买入。

## 均衡候选取证

均衡取证由 `_build_balanced_candidate_evidence_stage()` 负责。它从 L1 候选池里按四类来源各取最多 2 只：

```text
strategy      策略候选
news          消息候选
capital       资金候选
fundamental   基本面候选
```

同一股票如果已经被前一个维度选中，后续维度会跳过并继续补位。四类不足时，再按候选分从剩余候选里补齐。该阶段最多覆盖 8 只候选。

每只候选会复用 `_collect_deep_dive_evidence()` 的底层工具取证，多个候选通过 `ThreadPoolExecutor` 并行执行，默认最多 4 个 worker。

候选级取证工具包括：

```text
get_realtime_quote
analyze_trend
analyze_price_structure
get_capital_flow
get_stock_info
get_chip_distribution
search_comprehensive_intel
```

这些工具的原始结果会被压缩成统一候选证据包：

- `technical`
- `price_structure`
- `capital_flow`
- `news_event`
- `fundamental`
- `chip`
- `risk`

输出位置：

- 阶段内：`balanced_candidate_evidence.full`
- Trace JSON：`candidate_evidence.json`
- Trace Markdown：`candidate_evidence.md`

`candidate_evidence.json` 是后续阶段和排障的结构化真源；`candidate_evidence.md` 主要给 Trace 页面和人工阅读使用。

## 候选初筛与基础证据

进入初筛前会调用 `_collect_base_evidence()`，额外收集：

```text
detect_market_regime
get_market_indices
get_sector_rankings
get_realtime_quote    针对候选池前若干只
```

随后 `build_candidate_screening_prompt()` 会把以下内容交给 LLM：

- 用户问题
- 账户摘要与风险偏好
- L1 候选池摘要
- 均衡候选取证摘要
- 市场环境
- 证据账本摘要
- 基础行情、指数和板块证据

初筛阶段的职责是把候选分层，不直接决定最终买入。最终报告仍以后续单股深挖、组合配置和 Judge 为准。

## 单股深挖

深挖数量由 `AGENT_SELECTION_DEEP_DIVE_LIMIT` 控制，配置读取顺序是：

```text
Config.agent_selection_deep_dive_limit
  -> 环境变量 AGENT_SELECTION_DEEP_DIVE_LIMIT
  -> DEFAULT_DEEP_DIVE_LIMIT
```

函数 `_selection_deep_dive_limit()` 会把值限制在 1 到 20 之间。当前 `src/config.py` 默认值是 4，代码常量 `DEFAULT_DEEP_DIVE_LIMIT` 是 8，实际运行通常以配置默认 4 为准。

当前深挖标的是：

```python
deep_targets = candidates[:deep_dive_limit]
```

也就是说，它使用候选发现阶段的候选顺序，而不是严格使用初筛 shortlist。这个行为会影响“哪些股票进入逐股深度分析”。

单股深挖会优先复用均衡取证阶段已经拿到的 raw evidence；如果没有命中，再现场调用 `_collect_deep_dive_evidence()`。随后会生成 EvidenceCard：

```text
raw tool result
  -> build_evidence_cards_for_stock()
  -> compact prompt evidence
  -> build_deep_dive_prompt()
```

深挖输出里重点字段包括：

- `summary.action_bias`
- `summary.action_strength`
- `full.entry_quality`
- `full.dimension_summary`
- `full.key_evidence`
- `full.risk_flags`
- `full.failure_conditions`
- `full.missing_evidence`
- `full.evidence_cards`

其中 `entry_quality` 是后续报告层派生“条件入场 / 强观察”的关键来源。

## 组合配置、反方审查与 Judge

组合配置阶段由 `build_portfolio_allocation_prompt()` 生成 `positions_plan`。它会综合：

- 单股深挖摘要
- 均衡候选证据摘要
- 市场环境
- 用户持仓
- 可用现金
- 风险偏好与仓位约束

随后 `_apply_market_regime_constraints()` 会根据市场环境约束配置结果。风险状态偏弱时，即使单股有机会，也可能被压成 `wait`、`monitor` 或低仓位条件计划。

反方审查阶段由 `build_adversarial_review_prompt()` 负责，主要找：

- 证据缺口
- 追高风险
- 资金和趋势反证
- 基本面或消息面硬风险
- 组合约束冲突

Judge 阶段由 `build_judge_decision_prompt()` 负责，之后还会经过：

- `_stabilize_judge_decision()`
- `_apply_judge_position_overrides()`

Judge 的结果决定最终报告里的总体动作、裁决摘要和下一步，但逐股展示仍会合并深挖、配置计划和候选池信息。

## 最终报告结构

`_build_final_report_json()` 返回的核心结构是：

```json
{
  "selection_context": {},
  "market_regime": {},
  "orchestration_mode": "legacy/expert_graph",
  "expert_state": null,
  "candidate_discovery": {},
  "balanced_candidate_evidence": {},
  "candidate_screening": {},
  "single_stock_deep_dive": {},
  "portfolio_allocation": {},
  "adversarial_review": {},
  "judge_decision": {},
  "evidence_ledger": {}
}
```

之后 `_enforce_report_stock_identity()` 会做股票代码和名称一致性校验。如果发现模型或工具输出的名称与代码不一致，会按代码覆盖名称，并把修正记录写入 `stock_identity_audit`。

Markdown 报告由 `render_stock_selection_markdown()` 生成。报告层会先调用 `_recommendation_items()`，把三类信息合并到同一只股票上：

- 单股深挖结果
- L1 候选池信息
- 组合配置 `positions_plan`

然后通过 `_execution_mode()` 派生展示用执行模式。

## 执行动作派生规则

当前报告层支持以下执行模式：

| 执行模式 | 含义 | 主要来源 |
| --- | --- | --- |
| `immediate_open` | 可以形成直接开仓计划 | `action=open/buy` 且有组合计划或深挖结果。 |
| `conditional_open` | 当前顶层动作仍可能是 `wait`，但具备次日条件入场脚本 | `wait`、动作强度达到阈值、有入场条件、有退出条件、候选分或多源共振达标、无硬反向风险。 |
| `strong_watch` | 强观察，不直接入场 | 候选分较高或多源共振，但条件不够完整或仍缺关键确认。 |
| `plain_wait` | 普通等待 | 不满足条件入场或强观察。 |
| `reject` | 排除或回避 | `action=reject/avoid` 或硬风险明显。 |

相关默认阈值在 `src/agent/stock_selection.py`：

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `AGENT_CONDITIONAL_ENTRY_SCORE_MIN` | `88` | `wait` 升级为条件入场所需候选分。 |
| `AGENT_STRONG_WATCH_SCORE_MIN` | `85` | 强观察所需候选分。 |
| `AGENT_CONDITIONAL_ENTRY_MIN_STRENGTH` | `medium` | 条件入场最低动作强度。 |
| `AGENT_NO_CHASE_PCT_DEFAULT` | `6` | 没有显式禁止追高线时的默认追高阈值。 |

硬反向风险由 `HARD_BEARISH_RISK_MARKERS` 判断，包含 ST、退市、停牌、趋势空头、跌破关键支撑、资金净流出、重大减持、业绩预警、数据严重过期等文本标记。

所以用户看到的“高候选分 + wait”不一定矛盾：

- 候选分高：说明 L1 召回强。
- `wait`：说明组合配置或 Judge 不允许直接开仓。
- `conditional_open`：说明报告层认为它虽然不是直接开仓，但可给出次日触发条件。
- `plain_wait`：说明缺少入场条件、退出条件、证据质量或存在硬风险。

## Trace artifact 落盘

`TraceArtifactWriter` 会把运行结果落到本地 Trace 目录。目录名来自 session id，一般形如：

```text
trace-<uuid>
```

默认根目录由 `_trace_artifact_root()` 根据本地数据库路径推导，接口返回里也会带 `artifact_dir`。

常见文件：

| 文件 | 内容 |
| --- | --- |
| `request.json` | Trace 请求参数。 |
| `context.json` | Trace 上下文、账户上下文、上下文摘要。 |
| `planner.json` | Planner 识别出的任务、意图和工具计划。 |
| `todo.md` | 面向开发排障的执行清单。 |
| `events.ndjson` | SSE/进度事件逐行落盘。 |
| `tool_calls.json` | 所有工具调用及结果预览。 |
| `evidence_ledger.json` | 工具证据账本。 |
| `stock_selection.json` | 完整选股结果。 |
| `selection_context.json` | 选股运行上下文。 |
| `final_report.json` | 最终结构化报告。 |
| `candidate_evidence.json` | 均衡候选取证结构化结果。 |
| `candidate_evidence.md` | 均衡候选取证 Markdown 展示。 |
| `<stage>.json` | 各阶段独立结果，例如 `candidate_discovery.json`。 |
| `summary.json` | 本次 Trace 总结。 |
| `final.md` | 最终 Markdown 报告。 |

前端 `apps/dsa-web/src/pages/AgentTracePage.tsx` 会从两个方向拿数据：

- SSE 实时流：`POST /api/v1/agent/trace/stream`，逐条 `JSON.parse(data: ...)`。
- 历史恢复：`GET /api/v1/agent/trace/sessions/{session_id}`，读取后端落盘 artifact。

因此只要 SSE payload、API 响应或 artifact 中出现非标准 JSON，前端都可能在解析阶段报错。

## `NaN` 非法 JSON 问题

报错示例：

```text
Unexpected token 'N', ...""pe_ttm": NaN, "pb":"... is not valid JSON
```

这是标准 JSON 兼容性问题。JSON 标准不允许 `NaN`、`Infinity` 和 `-Infinity`。Python 的 `json.dumps()` 默认 `allow_nan=True`，会把 `float("nan")` 直接写成裸值 `NaN`；浏览器的 `JSON.parse()` 是严格 JSON 解析器，遇到裸 `NaN` 就会失败。

当前代码里最可疑路径如下：

```text
底层数据源 / pandas / numpy
  -> get_stock_info 返回 pe_ttm = NaN
  -> _candidate_fundamental_schema()
  -> metrics 只过滤 None，不过滤 NaN
  -> balanced_candidate_evidence / final_report_json 携带 "pe_ttm": NaN
  -> _write_trace_json() 或 SSE json.dumps()
  -> 前端 JSON.parse()
  -> Unexpected token 'N'
```

具体代码点：

- `src/agent/stock_selection.py`
  - `_candidate_fundamental_schema()` 会收集 `roe`、`revenue_growth`、`profit_growth`、`pe_ttm`、`pb`、`debt_ratio`。
  - 当前判断是 `if info.get(key) is not None`，但 `NaN is not None`，所以 `NaN` 会被保留。
- `api/v1/endpoints/agent.py`
  - `_write_trace_json()` 使用 `json.dumps(payload, ensure_ascii=False, indent=2, default=str)`，没有关闭 `allow_nan`，也没有先做非有限数清洗。
  - `_append_trace_event()` 同样直接 `json.dumps(event, ensure_ascii=False, default=str)`。
  - SSE 输出里也使用 `json.dumps(event, ensure_ascii=False)`。

需要注意：`default=str` 不能解决 `NaN`。`default` 只处理 JSON 编码器不知道怎么序列化的对象；`float("nan")` 是 Python 原生 float，编码器会按默认规则写成裸 `NaN`。

## 如何定位这类问题

如果 Trace 页面打开失败，先定位对应 session 的 artifact 目录，再查非标准数字：

```bash
rg -n '\bNaN\b|\bInfinity\b|-Infinity' data/agent_traces
```

重点检查：

```text
candidate_evidence.json
final_report.json
stock_selection.json
selection_context.json
tool_calls.json
events.ndjson
```

如果命中类似：

```json
"pe_ttm": NaN
```

基本可以确认是底层工具返回了非有限数字，后端 JSON 边界没有清洗，导致前端严格解析失败。

## 当前边界与限制

- L1 候选发现只代表召回，不代表推荐；高候选分不能直接解释为可以买。
- 自动候选发现主要支持 A 股，非 A 股可能返回 `not_supported`。
- 单股深挖当前按候选发现顺序截取前 `AGENT_SELECTION_DEEP_DIVE_LIMIT` 只，不严格按初筛 shortlist 重新排序。
- 工具失败不会默认中断整轮选股，失败会进入 `tool_calls`、`evidence_ledger` 和 `missing_evidence`。
- LLM 阶段输出 JSON 失败会走 fallback，因此最终报告可能出现较保守、泛化的等待结论。
- 当前没有全链路非有限数字清洗；任何工具 raw result、阶段 full、Trace event、final report 里都可能携带 `NaN`。
- Python 后端 `json.dumps()` 默认能写出 `NaN`，但浏览器端和标准 JSON 客户端不能解析。

## 后续修复建议

这份文档只记录当前实现。后续如果要修复 `NaN` 问题，建议按边界从内到外闭环：

1. 在数据工具返回前，把 `NaN`、`Infinity`、`-Infinity` 统一转为 `None`。
2. 在 `_candidate_fundamental_schema()` 等结构化证据构造点过滤非有限数字。
3. 在 Trace 写文件和 SSE 输出前增加统一 JSON sanitizer。
4. 在 `_write_trace_json()`、`_append_trace_event()` 和 SSE `json.dumps()` 处使用 `allow_nan=False`，让问题在后端更早暴露。
5. 增加回归测试，断言 `stock_selection.json`、`final_report.json`、`candidate_evidence.json` 和 SSE done payload 中不出现裸 `NaN`。

