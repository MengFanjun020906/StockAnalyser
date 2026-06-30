# openInvest 原理实际接入点索引

本文回答一个具体问题：本项目哪些地方已经实际吸收了 `openInvest` 的投资委员会原理，而不是只做了前端展示或概念文档。

结论先说：接入点主要在后端 Agent 选股链路、工具层、Trace 审计和离线复盘里。它不是把 `openInvest` 整仓搬进来，而是把可复用的机制拆成小模块接到本项目已有链路中。

本次同步基于本地 `openInvest` 当前 `origin/main` 快照 `9886db4`。这版最值得学习的不是某个单点工具，而是几条策略性原则：角色信息隔离必须落在数据投喂契约上，新闻要先变成结构化事件再触发委员会，卖出/等待必须带路径概率和买回参考，运行时风险偏好要可治理地调整，系统自己要持续公开胜率、持有率、PnL 和 benchmark 差距。

## 1. 总览

| openInvest 原理 | 本项目实际落点 | 运行入口 | 可查证产物 |
| --- | --- | --- | --- |
| 真委员会 / 信息隔离 | 已映射为四席位候选发现、反方审查和通用 debate；但还没有完全做到 Quant/Risk/CIO 级别的输入隔离 | `run_thesis_desk_committee()`、`adversarial_review`、`src/agent/debate.py` | `candidate_discovery.json`、`expert_packets.json`、`adversarial_review.json`、`debate.json` |
| Regime 状态识别 | `src/agent/regime.py`、`src/agent/tools/market_tools.py::detect_market_regime` | `src/agent/stock_selection.py::run_stock_selection_pipeline` 在候选发现后调用 | `tool_calls.json`、`selection_context.json`、`final_report.json.market_regime` |
| Regime forward probability / 买回参考 | `src/agent/regime_probability.py`、`src/agent/tools/market_tools.py::get_regime_forward_probability` | `stock_selection.py::_attach_regime_forward_probability()` 紧跟 `detect_market_regime` 执行 | `market_regime.forward_probability`、`pricing_agent.json`、`final_report.json` |
| 事件驱动新闻 | 已接 `search_openinvest_news`；主题催化席已做新闻摘要卡，但尚未持久化为 openInvest 式 event store | Agent 工具注册后由 planner / search 阶段调用；四席位主题催化席只对 AI/科技产业链做新闻补证 | `tool_calls.json` 中的 `search_openinvest_news` 调用和 `source_chain`、`expert_packets.json` |
| 多席位候选发现 | `src/agent/candidate_experts_v2/committee.py::run_thesis_desk_committee` | `AGENT_CANDIDATE_DISCOVERY_MODE=thesis_desk_committee`，当前也是默认主链路 | `candidate_discovery.json`、`seed_pool.json`、`expert_packets.json` |
| 反方审查 / 辩论 | `src/agent/stock_selection.py` 的 `adversarial_review` 阶段；通用 debate 在 `src/agent/debate.py` | 选股链路进入 Judge 前执行反方审查；`planning_execute` 证据链路会写 `debate.json` | `adversarial_review.json`、`debate.json`、`final_report.json.adversarial_review` |
| Judge 确定性后处理 | `src/agent/judge_sanity.py::apply_judge_sanity` | `judge_decision` LLM 输出后立即执行 | `judge_decision.json.full.sanity_checks`、`judge_sanity.json` |
| LLM telemetry | `src/agent/llm_telemetry.py`，由 `src/agent/llm_adapter.py` 和阶段 scope 写入 | Trace 运行时通过 `llm_telemetry_scope(trace_id, artifact_dir)` 激活 | `llm_usage.jsonl`、`llm_telemetry.json` |
| Dreaming / verdict review | `src/services/agent_verdict_review_service.py`、`scripts/build_agent_verdict_reviews.py`、`scripts/build_agent_verdict_insights.py` | 离线扫描本地 Trace 和本地 `StockDaily`，不重跑 Agent | `data/agent_reviews/verdict_review.jsonl`、`data/agent_reviews/insights/agent_verdict_insights.md` |
| 运行时风险治理 | 本项目仍主要是 env / 配置项分散控制；尚未形成 openInvest 式 Web/API/CLI 白名单覆盖层 | 后续可对齐到 Agent 设置页、API 和 `.env.example` | 目前不是完整已接入项 |
| PnL / benchmark 自审计 | 本项目已有入场执行回测和 verdict review；尚未形成 openInvest 式长期 PnL 曲线、benchmark 组和隐私化公开指标 | Web“入场”页、后验复盘页；后续可补 portfolio-level benchmark | `entry_execution_backtest` 相关产物、`verdict_review.jsonl` |
| Backtest no-lookahead 治理 | 本项目离线复盘避免重跑 Agent 和外部拉取；尚未有 LLM 训练截止日锁 | 后续适合接到 prompt/策略回测入口 | 当前不是完整已接入项 |

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

## 4. 新闻获取与事件层

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

但 openInvest 当前版本更值得学习的点，不只是“能搜新闻”，而是 `jobs/event_watch.py` 的事件层设计：

- 先从持仓、目标资产和宏观标签生成 watched symbols / macro tags。
- 多源新闻进入后先 normalize 成结构化 event。
- 按 URL 和 event store 去重。
- 只有命中 watched symbols / macro tags 且 severity 超过阈值时，才触发 committee 和邮件通知。

这对本项目新闻面链路的启发是：AI/科技产业链股票需要消息面，但模型不应该直接吞原文列表。更好的输入形态应该是短事件卡：

```text
事件类型 -> 影响品类 -> 受益/受损环节 -> 涉及股票 -> 证据来源 -> 时效 -> 置信度 -> 需要下游验证的问题
```

当前主题催化席已经把新闻补证收窄到 AI/科技产业链，并要求摘要化输出产品品类出口、国产替代政策、业务映射和资金验证；下一步可以把这些摘要卡持久化为 `event_store`，让后续 Trace 不必重复搜索同一批原文。

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

openInvest 当前 `core/committee/debate.py` 的关键强点是：信息隔离不是 prompt 口号，而是由 orchestrator 控制输入。

- Quant Analyst 看 asset、regime、market data、valuation、sentiment，但不看 portfolio。
- Risk Officer 看 asset、portfolio、wealth context、prior insights，但不看技术面细节。
- Round 2 才把 Quant / Risk 互相挑战后的调整意见交给 CIO。
- CIO 最终看到结构化 brief，再由 `parse_cio_memo` 做确定性解析和 sanity check。

本项目四席位目前更像“不同选股视角并行”，还不是严格的资产配置委员会。下一步如果要继续学 openInvest，应把“每个席位可见哪些字段”固化为代码级输入契约，而不是只在 prompt 里要求角色扮演。

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

openInvest 这里有两个细节值得继续同步：

- Convergence rule：连续轮次 `SIGNAL` 稳定且 `STRENGTH` 差异低于阈值时，可以停止辩论，不需要机械跑满轮次。
- Failure sentinel：worker 不可用时不是让 CIO 自由发挥，而是在 `parse_cio_memo` 里强制降级到保守动作。

本项目已经有 partial degraded 和 `judge_sanity`，但 debate 还可以补“稳定即停止”和“关键席位不可用时的强制裁决上限”。

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

openInvest 新版还有几个更细的裁决约束，当前本项目只部分吸收：

- BUY 过强时可以降为 ACCUMULATE，避免 LLM 把置信度写成交易冲动。
- TRIM / SELL 必须有低于当前价的 reentry price，否则降级为 HOLD。
- concentration lens 默认关闭，只有显式开启时才允许“组合过度集中”成为自动减仓理由。
- 防御性 DCA 是带授权和分批约束的动作，不是无限买入许可。

这些规则对 A 股选股的等价含义是：卖出或等待不能只写“风险高”，必须说明回补路径；仓位风险不能隐含假设用户全资产都在本系统；任何防御性加仓都应该有分批、金额和触发条件。

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

## 10. openInvest 当前版本最值得学习的策略性优点

这次更新后，openInvest 的优势可以概括成九条，按本项目可借鉴价值排序如下。

| 优点 | openInvest 做法 | 对本项目的学习价值 |
| --- | --- | --- |
| 信息隔离落在代码契约 | `core/committee` 由 session / debate 控制每个 agent 的输入字段，Quant 和 Risk 天然看不到对方不该看的上下文 | 四席位和最终 Judge 需要从“prompt 角色分工”升级为“可见字段分工” |
| 事件层高于新闻源 | `event_watch` 将新闻规范化、去重、按 watched symbols / macro tags 和 severity 触发委员会 | 新闻面应该先总结成事件卡，尤其是产品品类出口、国产替代政策、产业链环节映射 |
| 路径概率约束卖出与买回 | `regime_probability` / path profile 输出 p_below_current、downside、reentry reference | 本项目已有市场与单股概率工具，下一步应让 TRIM / wait 明确绑定买回参考 |
| Judge 后处理有硬规则 | `parse_cio_memo` 解析 CIO memo 后执行 confidence cap、allocation clamp、worker failure 降级、TRIM reentry 校验 | LLM 裁决必须被确定性规则二次约束，尤其是开仓、减仓和不可用工具场景 |
| 运行时治理可审计 | Web/API/CLI/env 的白名单配置覆盖写入 `config_overrides.json`，例如风险偏好、concentration lens、DCA 开关 | 本项目现在配置较分散，后续应把 Agent 风险偏好和关键门禁纳入统一 override 层 |
| 回测默认防 lookahead | backtest 默认拒绝 `decision_date > 2024-06-30`，除非显式 `--allow-lookahead` | 用 LLM 评估历史策略时，需要显式防训练截止日后的信息泄漏 |
| PnL 与 benchmark 自揭短 | README 公开系统胜率、HOLD 比例，并用多组 benchmark 比较；PnL SVG 隐私化，只展示百分比 | 本项目有入场回测，但还缺长期组合级 benchmark 和“系统自我披露” |
| Reward 看组合质量而不只看命中率 | `backtest_reward.py` 同时考虑年化收益、回撤、相对余额宝 alpha、Sharpe bonus | 后验复盘不应只看 hit/miss，还要把回撤、机会成本和资金效率纳入奖励 |
| Markdown-as-database 可审计 | 组合、委员会报告、记忆和复盘用 frontmatter + body，配合文件锁、原子写和 Git 审计 | 本项目 Trace 已经较强，长期记忆和策略复盘可以补更适合人工审阅的记录层 |

一句话总结：openInvest 把“投资委员会”做成了治理系统，而不是一个多角色 prompt。本项目已经吸收了工具、Trace、概率和 sanity 的一部分，但还需要继续学习它的输入隔离、事件存储、运行时治理和自审计。

## 11. 建议的同步优先级

| 优先级 | 建议同步项 | 原因 | 可能落点 |
| --- | --- | --- | --- |
| P0 | 四席位 / Judge 输入隔离契约 | 这是 openInvest 最核心的质量来源，能减少模型互相污染和“看完答案再解释” | `src/agent/candidate_experts_v2/`、`src/agent/stock_selection.py` |
| P0 | 新闻事件卡持久化 | 消息面驱动不能让模型吞原文；事件卡能节省时间并支持复用 | `src/agent/tools/search_tools.py`、`data/agent_events/`、主题催化席 |
| P0 | TRIM / wait 的 reentry 强校验 | 避免只会喊风险但不给买回路径，和 openInvest 的 path profile 思路一致 | `src/agent/judge_sanity.py`、`pricing_agent.json` |
| P1 | 统一运行时治理覆盖层 | 风险偏好、concentration lens、新闻席位开关、DCA/分批规则不应散落在 env 和 prompt 中 | API 设置页、`.env.example`、Agent 配置 schema |
| P1 | lookahead guard | 历史 Trace、策略回测、LLM prompt 评估都需要显式防未来信息泄漏 | 回测脚本、verdict review、entry execution backtest |
| P1 | portfolio-level PnL benchmark | 现在更多是单 Trace / 入场层评估，缺长期组合级自揭短 | Web“入场”页、后验复盘页、新 benchmark service |
| P2 | strategy reward | 把胜率、收益、回撤、机会成本合成可比较分数，用于评估 prompt / 策略迭代 | `src/services/agent_verdict_review_service.py`、离线 insight |
| P2 | Markdown/atomic audit memory | 给长期策略洞察和人工复盘更稳定的可读存储格式 | `data/agent_reviews/insights/`、未来 memory 层 |

## 12. 怎么确认某次 Trace 真的跑了

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

## 13. 还没有完全接入的部分

这些不要误判成已经完成：

- 没有把 `openInvest` 的整套 Core / Memory / Jobs / Web API 搬进来。
- 没有直接挂载 `openInvest` 的前端 dist；之前评估过，`openInvest` 的 GUI dist 来自独立发布包，本项目不建议直接接。
- `watchlist_scan` 的反方审查已经真实接入，但还不是严格多轮、信息隔离的委员会协议。
- Regime probability 已接入市场代理指数层和部分单股工具，但更细的买回点强约束、币种/成本口径 overlay、持仓成本路径画像仍是后续增强。
- Verdict review 已能离线生成后验样本和 insight，但当前不会自动反哺线上决策。
- PnL / benchmark 展示仍主要停留在入场回测和后验复盘，尚未达到 openInvest “组合长期表现 + 多 benchmark + 隐私化公开百分比”的自审计层。
- 没有实现 openInvest 的 `event_store`、severity threshold、watched macro tags 触发委员会机制。
- 没有实现 Web/API/CLI/env 统一白名单配置覆盖和 `config_overrides.json` 这类治理层。
- 没有实现 LLM 训练截止日维度的 backtest lock；现有离线复盘只是避免重跑 Agent 和外部拉取。
- 没有把 concentration lens default-off、gold defense DCA 等策略治理原则完整迁移；A 股场景需要先翻译成仓位假设、分批规则和回补路径规则。

## 14. 相关文档

- [openInvest 可接入组件评估](openinvest-integration-assessment.md)
- [A 股 Regime 状态机原理](../modules/regime-state-machine.md)
- [专家委员会开发指南](../modules/expert-committee-dev-guide.md)
- [完整指南：Agent Trace 产物](../full-guide.md)
