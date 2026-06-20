# Agent Trace 测试避坑清单

本文档记录 Agent、Trace、Planner、工具、候选专家席位和数据源 fallback 相关改动的测试避坑项。目标是减少“单元测试通过，但用户从 Trace 页面或真实入口一跑就失败”的情况。

这不是通用测试指南。它只处理本仓库反复出现过的 Agent 链路假阳性问题。

## 核心原则

1. **先确认用户实际入口，再写测试。** 同一个需求从单股 `entry_analysis`、持仓 `position_review`、选股 `watchlist_scan`、Trace API、Web SSE、内部函数直接调用进入时，链路完全不同。
2. **单测通过不等于用户路径通过。** mock 工具、mock LLM、直接调用工具函数只能证明局部逻辑，不能证明 Planner 能选到工具、专家能看到工具、Runner 能压缩上下文、Trace 能落盘。
3. **工具成功不等于 Agent 能用。** 新工具至少要同时检查 registry、Planner capability、Runner ETL profile、专家白名单、YAML manifest、Prompt 可见性和 Trace artifact。
4. **直接工具测试不等于预算内可用。** 真实 Trace 里有批量工具、LLM 等待、外层 runner timeout、内部数据源 timeout、缓存、fallback 竞争；单独运行一个函数不会暴露这些问题。
5. **验证结论必须落到 artifact。** 不能只看 pytest 绿了或命令退出 0，要看 `planner.json`、`tool_calls.json`、`events.ndjson`、`selection_context.json`、`final_report.json` 里是否出现预期工具、状态和证据。
6. **没有实跑就不要说“修好了”。** 如果只跑了局部测试，交付时必须写清楚“未跑真实 Trace / 未跑 Web SSE / 未跑真实数据源”的缺口。

## 改动前必须先做的事

每次开始修 Agent/Trace 问题前，先写下这 5 个答案：

| 问题 | 要求 |
| --- | --- |
| 用户实际想要什么 | 用一句话复述，不要只复述技术名词。例如“用户期望四席位实际能调用资金流、板块、公告和单股 Regime 概率工具”。 |
| 用户从哪里触发 | Web Trace、API `/agent/trace/run`、聊天、命令行、内部脚本、历史 Trace replay。 |
| 实际进入哪个 intent | `entry_analysis`、`position_review`、`watchlist_scan`、`qa`，以 `context.json` / `planner.json` 为准。 |
| 失败证据在哪里 | 具体 Trace 目录、失败文件、失败工具、失败阶段、错误字段。 |
| 修复后要在哪个 artifact 看到证据 | 例如 `tool_calls.json` 里工具成功、`selection_context.json` 里 `expert_state` 非空、`candidate_discovery.json` 里席位诊断消失。 |

如果这 5 项答不出来，先不要改代码。

## 历史假阳性模式

| 假阳性 | 之前怎么发生 | 必须怎么测 |
| --- | --- | --- |
| 工具函数单独测试成功，但 Agent 没调用 | 工具已注册，但 Planner capability 没映射，或单股 plan 没选该 capability。 | 查 `planner.json` 的 capability / execution plan，再查 `tool_calls.json` 是否真的出现该工具。 |
| 工具已注册，但专家席位看不到 | 四席位用 `allowed_tools` 和 YAML manifest 硬过滤，registry 里有不代表席位可见。 | 同时验证专家 Python 白名单、`tools_manifest/*.yaml`、manifest 渲染 prompt、真实 registry 校验。 |
| mock LLM 通过，真实 LLM 输出失败 | mock 输出固定 JSON，绕过了 `response_format`、最终轮 JSON、token 上限和 tool loop。 | 至少跑一次 `scripts/probe_thesis_desks_from_trace.py --real-llm --with-tool-schemas`；涉及真实工具时加 `--with-real-tools`。 |
| 直接调用 staged pipeline 成功，Trace API 失败 | Trace API 会先做 intent 识别、账户上下文注入、候选模式覆盖、artifact 写入。 | 用 `/api/v1/agent/trace/run` 或 Web Trace 实跑，不能只调用内部函数。 |
| 单工具无缓存成功，真实 Trace 超时 | 真实链路里多个 Tushare / StockAPI / Eastmoney 源共享预算，外层 runner 可能提前截断。 | 用接近真实的 `AGENT_TOOL_CALL_TIMEOUT_SECONDS` 和 `STOCK_NO_CACHE=1` 跑 Trace 或 runner 级测试。 |
| 缓存掩盖问题 | 本地缓存命中时没有触发真实数据源失败、空表、节假日、慢请求。 | 至少补一组 no-cache 或清缓存测试；交付时说明是否用了缓存。 |
| `partial + errors` 被误判 | 有有效数据的 partial payload 既可能是可用降级，也可能是失败；只看 `errors` 会误报。 | 同时断言“无有效数据 -> failure”和“有关键数据 -> success with diagnostics”。 |
| 阶段失败路径没测 | 正常路径有 `expert_state`，但候选发现 fail-fast 路径没有写入。 | 对每个新增收口字段，都测正常路径、partial 路径、fail-fast 路径。 |
| Trace 页面看不到但后端单测过 | 后端写了字段，SSE done payload、history restore、前端展示可能没接。 | 涉及 UI 时必须跑 Web 测试或手动打开 Trace 页检查，不要只测 API。 |
| 文档或测试说明和真实命令漂移 | 复制了旧字段、旧脚本参数或不存在的 flag。 | 写入文档前用 `rg` / `--help` / 源码确认命令真实存在。 |

## 工具链路的七层可见性

涉及任何新工具、工具重命名、工具补证或专家席位可见性调整时，必须逐层检查：

1. **工具注册层**
   - `src/agent/factory.py` 或对应 registry 入口能注册工具。
   - `get_tool_registry().list_tools()` 能看到工具名。
2. **Planner 层**
   - `src/agent/planner.py` 的 capability map 包含工具。
   - 对应 intent 的 plan 会选择该 capability。
3. **Runner 层**
   - `src/agent/runner.py::resolve_tool_etl_profile()` 不返回 `generic`。
   - 压缩 profile 保留模型需要判断的业务字段，不把关键字段压没。
4. **单股 ReAct 层**
   - `tool_registry.to_openai_tools()` 里有该工具声明。
   - 单股 `tool_calls.json` 里实际出现工具调用，或 planner 明确说明为什么不需要。
5. **专家席位 Python 白名单**
   - `src/agent/candidate_experts_v2/experts/*_desk.py` 的 `*_TOOLS` 包含该工具。
6. **专家席位 YAML manifest**
   - `src/agent/candidate_experts_v2/tools_manifest/*_desk.yaml` 包含工具用途、适用时机、典型参数。
   - `tests/test_tools_manifest.py` 覆盖真实 registry 校验，而不是只测空 registry。
7. **Prompt 可见性**
   - manifest block 渲染进系统 prompt。
   - 真实或 probe LLM 调用时 `tool_decls` 数量符合预期。

任何一层缺失，都可能出现“我自己测工具成功，但 Agent 实跑不用/看不到”的问题。

## 最低验证矩阵

### 1. 单股入口变更

适用范围：

- `entry_analysis`
- `position_review`
- 单股 ReAct 工具调用
- 单股 Planner capability
- 单股预取上下文，例如 `symbol_regime_probability`

最低验证：

```bash
.venv/bin/python -m pytest tests/test_agent_planner.py tests/test_agent_executor.py
```

还必须做一次真实入口验证，二选一：

```bash
# 需要本地 API 服务已启动
curl -s http://127.0.0.1:8000/api/v1/agent/trace/run \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "帮我分析 600519 是否适合入场",
    "stock_code": "600519",
    "analysis_mode": "planning_execute",
    "report_intent": "entry_analysis",
    "inject_portfolio_context": false
  }'
```

或通过 Web Trace 页面实际跑同一请求。

必须检查：

- `context.json`：`intent` 是预期值，不是被账户上下文污染成别的 intent。
- `planner.json`：包含预期 capability 和工具。
- `tool_calls.json`：预期工具实际出现，且 `success/status/error` 语义正确。
- `final_report.json` 或最终输出：没有把缺失证据编造成确定结论。

### 2. 选股 / 四席位专家变更

适用范围：

- `watchlist_scan`
- seed pool / seed gate
- 四席位专家白名单或 manifest
- candidate discovery
- `expert_state`
- 组合配置、Judge、风险门

最低验证：

```bash
.venv/bin/python -m pytest \
  tests/test_agent_stock_selection.py \
  tests/test_candidate_experts_v2.py \
  tests/test_candidate_committee.py \
  tests/test_tools_manifest.py
```

必须至少跑一次 Trace 复盘 probe：

```bash
.venv/bin/python scripts/probe_thesis_desks_from_trace.py \
  --trace-dir data/agent_traces/20260617-220820-trace-43fc72b59e5946568308549f77bfa894 \
  --limit 8
```

如果改动涉及真实 LLM 工具调用、response_format、tool loop、timeout 或专家工具可见性，继续跑：

```bash
.venv/bin/python scripts/probe_thesis_desks_from_trace.py \
  --trace-dir data/agent_traces/20260617-220820-trace-43fc72b59e5946568308549f77bfa894 \
  --real-llm \
  --with-tool-schemas \
  --with-real-tools \
  --llm-timeout-s 30 \
  --per-seed-timeout-s 60 \
  --limit 8
```

必须检查：

- `seed_pool.json`：种子池不是空的，source diagnostics 没有异常清空。
- `seed_gate.json`：门卫没有错误丢弃所有候选。
- `candidate_discovery.json`：席位状态、errors、negative reasons 合理。
- `llm_usage.jsonl`：真实 LLM 调用有记录；超时/失败能定位到席位和 seed。
- `selection_context.json`：`orchestration_mode`、`candidate_discovery_mode`、`expert_state` 符合预期。
- `final_report.json`：即使失败也有 `partial_failure` 和可解释 next_step。

### 3. 数据源 / fallback / timeout 变更

适用范围：

- `data_provider/`
- `src/agent/tools/data_tools.py`
- `src/agent/tools/market_tools.py`
- 资金流、板块榜单、Regime、新闻、公告、StockAPI、Tushare fallback

最低验证：

```bash
.venv/bin/python -m pytest \
  tests/test_data_tools_get_capital_flow.py \
  tests/test_fundamental_adapter.py \
  tests/test_fundamental_context.py \
  tests/test_market_tools_timeouts.py \
  tests/test_agent_regime.py \
  tests/test_agent_regime_probability.py
```

必须额外覆盖至少一种真实预算场景：

- `STOCK_NO_CACHE=1`
- 低 timeout
- 主源失败后 fallback 仍有预算
- 节假日或非交易时段
- 数据源返回空表但不是异常

重点检查：

- 顶层 `status` 是否代表真实可用性。
- `errors` / `warnings` 是否保留诊断但不误导 Agent。
- 有有效数据的 `partial` 是否仍进入模型上下文。
- 没有有效数据的 `partial` 是否标为失败。
- `source_chain` / `flow_sources` / `selected_flow_source` 是否能解释采用了哪个源。

### 4. Trace artifact / Web Trace 变更

适用范围：

- `api/v1/endpoints/agent.py`
- `TraceArtifactWriter`
- SSE stream
- `apps/dsa-web/src/pages/AgentTracePage.tsx`
- history restore

最低验证：

```bash
.venv/bin/python -m pytest tests/test_agent_models_api.py
```

涉及前端展示时还要跑：

```bash
cd apps/dsa-web
npm run lint
npm run build
```

必须检查：

- SSE `done` payload 和历史 `GET /sessions/{session_id}` 返回字段一致。
- `events.ndjson` 没有不可 JSON 序列化值。
- `tool_calls.json`、`stock_selection.json`、`final_report.json` 不出现裸 `NaN` / `Infinity`。
- 前端空状态、partial、failed、timeout 不互相误判。

## 真实 Trace 审计步骤

拿到一个失败 Trace，例如：

```text
data/agent_traces/20260617-220820-trace-43fc72b59e5946568308549f77bfa894
```

按这个顺序看：

1. `request.json`
   - 用户原始 message、stock_code、account_id、report_intent、candidate_discovery_mode。
2. `context.json`
   - 最终注入的 `AgentUserContext`。
   - 是否被持仓上下文覆盖了用户真正想要的 intent。
3. `planner.json`
   - Planner 选择了哪些 capability。
   - 是否包含用户关心的工具能力。
4. `events.ndjson`
   - 是否有 `tool_start` 没有对应 `tool_done`。
   - 是否在某个阶段之后直接 error。
5. `tool_calls.json`
   - 工具是否实际调用。
   - `success=false` 的工具是否有有效数据。
   - 是否出现 `tool_not_found`、`tool_not_whitelisted`、`timeout`。
6. `stock_selection.json` / `selection_context.json`
   - 每个 stage 的 status。
   - `next_step` 是否解释了提前停止原因。
7. `candidate_discovery.json`
   - seed pool、gate、席位输出、rejected/negative reasons。
8. `llm_usage.jsonl`
   - 真实 LLM 是否返回、耗时、模型、错误。
9. `final_report.json` / `final.md`
   - 最终报告是否忠实反映工具失败和证据缺口。

如果 artifact 里没有证据，不能用“本地单测通过”替代。

## 常见问题的专项验收

### `get_capital_flow`

历史问题：

- 直接工具测试成功，但真实 Agent 批量工具并发时预算耗尽。
- 三套资金流来源并发或顺序不当，导致主源成功仍被慢 fallback 拖死。
- 有数据的 partial 被当失败，或无数据的 partial 被当成功。

验收要点：

- 覆盖 DC、THS、legacy moneyflow 和 StockAPI fallback。
- 覆盖主源成功后快速返回。
- 覆盖 fallback 失败时 `errors/warnings/source_chain` 可解释。
- Trace `tool_calls.json` 里有 `main_net_inflow`、`selected_flow_source` 或等价有效字段。

### `get_sector_rankings`

历史问题：

- 快速源失败后，后续 fallback 被挤压到 0 秒预算。
- 单测只 mock 一个成功源，没有覆盖预算分配。

验收要点：

- 覆盖 Tushare THS hot industry、Eastmoney、StockAPI fallback。
- 覆盖低 timeout 时仍返回结构化 timeout，而不是抛异常或空对象。
- Trace 里能看到 `source_chain` 和榜单口径。

### `get_regime_forward_probability`

历史问题：

- 合成历史或缓存路径单测通过，真实 Trace 拉指数历史时慢或数据不足。
- 前向概率计算成功，但 Runner 压缩上下文时把关键字段压没。

验收要点：

- 覆盖缓存优先路径。
- 覆盖样本不足 / low confidence。
- 覆盖 `forward_probability`、`path_profile`、`reentry_reference`、`sample_quality_summary` 在模型上下文中可见。
- 选股链路里区分市场级 `get_regime_forward_probability` 和单股 `get_symbol_regime_probability`。

### 四席位工具可见性

历史问题：

- 工具在 registry 中存在，但四席位 `allowed_tools` 和 YAML manifest 没有同步。
- 测试用 `tool_registry={}` 或 fake tool_decls，绕过真实白名单。

验收要点：

- `tests/test_tools_manifest.py` 必须校验真实 registry。
- probe 输出里每个 desk 的 `tool_decls` 数量要符合预期。
- 新工具必须写清楚 `when_to_use`、`typical_args` 和口径边界。
- 真实 Trace 中不能出现 `tool_not_whitelisted`。

## Definition of Done

Agent/Trace 相关修复只有同时满足下面条件，才算真正完成：

- 明确复述了用户实际入口和预期。
- 有失败 Trace 或等价真实入口作为复现基准。
- 单元测试覆盖局部逻辑。
- 至少一个真实入口或 trace replay 覆盖端到端链路。
- artifact 中能看到修复证据。
- 交付说明写清楚已验证项和未验证项。
- 如果没有跑真实 LLM、真实数据源、Web Trace 或 SSE，必须明确说没有跑。

## 交付时必须写清楚

每次 Agent/Trace 修复交付时，至少写：

- 改了什么。
- 为什么这个改动能覆盖用户实际失败路径。
- 跑了哪些单元测试。
- 跑了哪个真实 Trace / API / Web 入口。
- 检查了哪些 artifact 文件。
- 哪些链路没有跑，风险是什么。
- 回滚方式。

不要只写“测试通过”。
