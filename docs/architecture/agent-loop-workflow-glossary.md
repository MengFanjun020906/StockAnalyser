# Agent Loop / Workflow Glossary

本文统一 StockAnalyser 中 AI 基础设施的概念、边界和命名。目标是让 planning、execute、replan、memory、trace、workflow 和 loop 不再混用，后续新增子 Agent 或工具时按同一套语言描述。

## 1. 核心概念

| Concept | 中文名 | 在本项目中的含义 | 主要 artifact |
| --- | --- | --- | --- |
| Agent | 智能体 | 具备角色边界、工具白名单、输入上下文和输出合同的执行单元。它可以调用工具，也可以只做综合裁决。 | `src/agent/agents/`, `src/agent/stock_selection.py` |
| Main Agent | 主 Agent | 负责编排用户目标、上下文、规划、执行、复盘和最终交付的顶层入口。 | `AgentExecutor`, `TraceArtifactWriter` |
| Workflow / 工作流 | 工作流 | 一组确定性阶段顺序，强调“阶段怎么走”。例如选股链路从 seed pool 到 Judge。 | `selection_context.json`, `events.ndjson` |
| Pipeline / 流水线 | 流水线 | 数据和 artifact 在阶段之间的传递关系，强调“输入输出怎么接”。 | `candidate_discovery.json`, `meta_orchestrator.json`, `final_report.json` |
| Loop / 循环 | 循环 | 带反馈的迭代：plan -> execute -> evaluate -> replan -> stop。强调“什么时候继续、什么时候停止”。 | `LOOP.md`, `STATE.md`, `todo.md` |
| Planning | 规划 | 把用户问题拆成能力域、工具计划、预期结果、下游用途、失败降级和停止条件。 | `planner.json`, `todo.md` |
| Execute | 执行 | 按计划调用工具或运行阶段，把结果写入证据账本和 trace。 | `tool_calls.json`, `evidence_ledger.json` |
| Replan | 重规划 | 工具失败、强反证、关键字段缺失或停止条件满足时的有界调整。只补会改变动作的缺口。 | `todo.md`, `events.ndjson` |
| Context | 上下文 | 本轮模型可见的压缩输入，包括用户问题、账户、计划、候选摘要和必要证据。 | `context.json`, prompt payload |
| Memory | 记忆 | 跨运行保留的知识或反馈，不等于当前事实。当前主要由 Graphiti/本地复盘文件承载。 | Graphiti, `data/agent_reviews/` |
| Trace | 轨迹 | 单次运行的完整落盘记录，用于调试、恢复、复盘和证据追溯。 | `data/agent_traces/<trace-id>/` |
| Evidence Ledger | 证据账本 | 工具调用结果的结构化摘要，记录 evidence、limitation 和 impact。 | `evidence_ledger.json` |
| Planning Ledger / 计划账本 | 计划账本 | `todo.md` 的正式语义：保存计划、工具交接合同、执行状态、缺口和复用规则。 | `todo.md` |
| Guardrail / Gate | 护栏/闸门 | 用确定性规则约束交易动作，例如 T+1、仓位、止损、数据质量。 | `risk_gate.json`, tests |
| Skill | 技能 | 可插拔的策略、流程或开发规范。它提供行为规则，但不能覆盖仓库硬规则。 | `.claude/skills/`, `.agents/skills/` |

## 2. Loop 与 Workflow 的区别

`workflow` 是阶段拓扑，回答“先做什么、后做什么”。例如：

```text
seed pool -> four desks -> screening -> deep dive -> Meta -> pricing -> allocation -> adversarial -> Judge
```

`loop` 是反馈控制，回答“是否继续、是否重试、是否降级、是否停止”。例如：

```text
plan -> execute tools -> inspect failures and conflicts -> replan only missing critical evidence -> stop
```

因此，一个 workflow 可以包含多个 loop。选股 workflow 中，候选发现、单股深挖、Meta/点位计算和 Judge 都可以触发局部 loop，但不能无限扩展工具域。

## 3. Planning Ledger / 计划账本

`todo.md` 不应只是展示列表。它是可复用的 Planning Ledger / 计划账本，用于节省 planning token 和恢复上下文。

必须写入：
- 任务识别：`intent`、主标的、持仓状态、主维度、预期输出。
- 工具交接合同：`expected_result`、`downstream_use`、`fallback_on_failure`、`next_step`。
- Replan 策略：触发条件、允许动作、禁止动作、artifact 更新要求。
- 执行状态：工具成功、失败、超时、降级、结果预览、未调用计划工具。
- 复用契约：`reuse_source_trace`、`reuse_payload`、`reuse_rule`、`invalidates_on`。

允许复用：
- 任务结构、能力域、工具计划、上一轮未完成项、失败/降级摘要和执行状态计数。
- 不会改变最终动作的省略维度。
- 工具之间的上下游交接说明。

必须重验：
- 实时行情、资金流、新闻、市场状态、账户现金/持仓、涨跌停和任何时效数据。
- 上一轮 `failed`、`timeout`、`fallback`、`stale`、`partial` 或 `unavailable` 的结果。
- 用户目标、标的范围、工具注册、planner 合同变化后的关键步骤。

失效条件：
- 用户从单股入场改为全市场选股，或从选股改为持仓风险复查。
- 主标的或目标股票集合变化。
- 工具 schema、ETL profile、planner capability map 或风险闸门合同变化。
- 旧账本只包含 fallback 成功，且缺口会影响当前动作。
- 旧账本中的核心行情、资金、新闻或账户事实已经过期。

## 4. Agent 命名

命名采用“英文专业名 / 中文专业名 / 当前代码或阶段名”的三列。代码类名和 JSON 字段暂不强制迁移，避免破坏 API 与历史 Trace。

| Role | Bilingual Name | 当前实现 |
| --- | --- | --- |
| Main Orchestrator | Serenity Investment Orchestrator / 静研投资编排官 | `AgentExecutor`, `run_agent_trace` |
| Planner | Planning Steward / 计划管控官 | `src.agent.planner` |
| Evidence Collector | Evidence Acquisition Agent / 证据采集官 | ToolRegistry + runner |
| Technical Analyst | Market Structure Analyst / 市场结构分析师 | `TechnicalAgent` |
| Intelligence Analyst | Intelligence Analyst / 消息情报分析师 | `IntelAgent` |
| Risk Analyst | Risk Control Analyst / 风险控制分析师 | `RiskAgent`, `risk_gate` |
| Portfolio Analyst | Portfolio Allocator / 组合配置师 | `PortfolioAgent`, `portfolio_allocation` |
| Decision Synthesizer | Decision Synthesizer / 决策综合官 | `DecisionAgent` |
| Structural Reversal Desk | Structural Reversal Desk / 结构反转席 | `EarlyTurnDeskExpert` |
| Momentum Desk | Momentum Continuation Desk / 动量延续席 | `MomentumDeskExpert` |
| Quality Desk | Quality Repair Desk / 质量修复席 | `QualityRepairDeskExpert` |
| Catalyst Desk | Theme Catalyst Desk / 主题催化席 | `ThemeCatalystDeskExpert` |
| Meta Layer | Constraint Architect / 约束架构师 | `meta_orchestrator` |
| Pricing Layer | Execution Pricing Analyst / 执行点位分析师 | `pricing_agent` |
| Allocation Layer | Account-Aware Allocator / 账户化配置官 | `portfolio_allocation` |
| Adversarial Reviewer | Red-Team Reviewer / 反方审查官 | `adversarial_review` |
| Judge | Investment Arbiter / 投资裁决官 | `judge_decision` |
| Memory Curator | Research Memory Curator / 研究记忆管理员 | Graphiti + review artifacts |
| Outcome Reviewer | Outcome Review Analyst / 后验复盘分析师 | `agent_verdict_review`, `entry_execution_backtest` |
| Report Writer | Research Report Composer / 研究报告编写官 | `final.md`, `final_report.json` |

## 5. 子 Agent 分工边界

候选席位只负责“是否值得深挖”，不负责最终买入建议。

Meta 层只负责把机会翻译成硬约束和必算场景，不负责排序和点位。

点位计算层只负责 If-Then 条件单，不负责推翻 Meta 的硬约束。

组合配置只负责账户约束下的执行排序，不负责证明股票本身最好。

反方审查必须主动寻找证据缺口、追高风险、资金缺失和违反硬约束的地方。

Judge 只在上游 artifact 完整后裁决；如果关键证据缺失，正确动作是 `wait`、`monitor` 或 `reject`，不是补写乐观结论。

## 6. 报告层命名

最终用户报告建议使用以下稳定词汇：

| 报告词汇 | 含义 |
| --- | --- |
| 机会首选 | 股票机会质量最高，不等于当前可买 |
| 执行首选 | 账户、市场和风控约束下最接近可执行 |
| 条件入场 | 只有触发条件、止损、失效条件和有效期同时成立才可执行 |
| 可观察标的 | 已深挖但未排到执行首选 |
| 观察池 | 未深挖或证据不足，只用于后续跟踪 |
| 证据缺口 | 当前链路没有覆盖、会影响结论强度的缺失项 |
| 降级成功 | 工具或阶段用 fallback 完成，但数据质量低于正常成功，必须显式披露 |

## 7. 维护要求

- 新增 Agent、desk、stage 或长期记忆能力时，先补本文命名表和概念边界。
- 修改 `todo.md` 结构时，同步更新 `scripts/audit_agent_infra.py` 的 marker。
- README 只放精简 operating model；细节放本文。
- 代码命名迁移应单独做兼容 PR，不和文档命名一次性混改。
