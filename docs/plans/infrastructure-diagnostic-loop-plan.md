# 基础设施诊断与硬化计划

> 日期：2026-07-16
> 范围：tools / context / memory / pipeline / planning-execute / loop-engineering
> 模式：autonomous goal loop；遇到不可验证、静默降级、数据质量下降或权限阻断时停止升级。

## 目标

把 MVP 阶段快速堆起来的 Agent 基础设施改造成可审计、可复现、可回滚的底层系统。重点不是重写交易策略，而是确认每一次工具调用、上下文注入、记忆使用、pipeline 传递、planner/replan 和 loop 状态都有明确契约、证据和失败语义。

## 非目标

- 不重写选股、买卖点、新闻因子或仓位策略。
- 不默认打开记忆、Graphiti、外部搜索或任何会改变分析结论的能力。
- 不合并到 `main`，不发布正式 release。
- 不做数据库迁移，除非诊断证明没有迁移无法记录关键质量信号。
- 不用隐藏 fallback 让流程“看起来成功”。

## 诊断矩阵

| 基础设施面 | 需要确认的问题 | 立即可自动化的检查 | 需要后续决策的问题 |
| --- | --- | --- | --- |
| Tools | 工具是否重复注册、schema 是否完整、ETL 压缩 profile 是否覆盖、失败是否可见 | 工具清单、重复名、参数 schema、`resolve_tool_etl_profile()`、planner 映射工具存在性 | 是否把部分弱质量 fallback 改成 fail-fast |
| Context | 用户账户、持仓、目标股票、报告 intent 是否稳定注入，并让 plan/todo 随任务变化 | 代表性 `AgentUserContext` 生成 planner，比较 capability/tool plan 是否不同 | 是否扩展 context schema 支持更完整的交易账户约束 |
| Memory | 记忆是否门控、样本阈值是否透明、失败是否可观测 | 记录当前门控和中性降级语义；避免把未启用记忆当成证据 | 是否默认开启记忆；记忆读取失败是否阻断分析 |
| Pipeline | 主流程阶段、可选服务、外部依赖、通知和图谱同步是否隔离失败 | 后续增加 stage ledger：每阶段 status、evidence freshness、fallback quality | 是否把 pipeline 的部分 best-effort 步骤升级为硬门槛 |
| Planning/Execute | `planner.json`、`todo.md`、`evidence_ledger.json` 是否讲清工具预期输入输出、replan 和下一步 | todo/replan/evidence ledger contract 离线审计 | 是否把 replan 从静态策略升级为运行时显式 artifact |
| Loop | `LOOP.md`、`STATE.md`、budget、constraints 是否和真实开发模式一致 | 检查 stale state、预算语义、autonomous goal loop 合同 | 是否将 loop 审计纳入 CI |

## 执行阶段

### Stage 0：计划与真源对齐

- 新增本计划文档，固定诊断范围、非目标和验收标准。
- 修正 `STATE.md` 中上一轮 Graphiti 残留状态。
- 区分 Daily Triage 和 Autonomous Goal Loop 的预算语义，避免用 L1 日常巡检预算误约束目标开发 loop。
- 澄清高风险路径在 autonomous loop 下的处理方式：允许目标范围内修改，但必须在 PR/final 中显式披露、验证和回滚。

### Stage 1：离线基础设施审计

- 新增 `scripts/audit_agent_infra.py`。
- 审计工具注册表、重复工具名、工具参数 schema、ETL profile 覆盖。
- 审计 planner capability 元数据是否锁步，映射工具是否真实存在。
- 用 entry / position / watchlist 三类 context 验证 planner/todo 不应一模一样。
- 审计 `todo.md` 必须包含 expected result、downstream use、fallback、next step、replan 和执行复核。
- 审计 `evidence_ledger.json` 必须区分 success/failed/timeout/cached 的局限和影响。

### Stage 2：记忆与上下文可观测性

- 先记录当前 `AgentMemory` 的门控、中性降级和样本阈值。
- 若审计证明记忆失败被当成有效证据，再改为显式 degraded status。
- 是否默认开启记忆需要单独确认，因为它会改变分析结果。

### Stage 3：Pipeline 质量账本

- 梳理主流程、Agent Trace、Graphiti、新闻信号、回测、报告和通知的阶段边界。
- 为关键阶段补齐 status / freshness / fallback_quality / limitations。
- 隐性成功、低质量 fallback、未知时效的数据必须在 artifact 或报告中暴露。

### Stage 4：Loop 运行固化

- 每个目标 loop 开始时先跑基础设施审计。
- 每次提交前更新 `STATE.md` 和 PR 描述。
- 每 200k 估算 token 或每次提交前做一次 checkpoint，记录当前证据、风险、下一步和是否继续。
- 若发现无法自动修复的问题，转成 issue 或下一阶段计划，不混入当前修复。

## 验收标准

- 有一份可审计的基础设施诊断计划。
- 有一个离线命令可以检查 tools/context/planner/todo/evidence/loop 的核心契约。
- 当前 stale loop state 被清理，budget/constraints 不再和 autonomous goal loop 冲突。
- 新增测试覆盖审计脚本和 stale state 检测。
- Autonomous loop 超过 200k 估算 token 时必须更新 `STATE.md` / PR checkpoint。
- 本轮不改变交易策略和记忆默认行为。
- 验证结果、未完成项、风险和回滚方式写入 PR/final。

## Grill-Me 触发条件

下面问题会改变系统行为或风险边界，必须停下来问：

- 是否默认开启 `AgentMemory` 或 Graphiti 记忆检索。
- 是否把某个现有 best-effort/fallback 能力改成 fail-fast。
- 是否新增数据库迁移或修改已有表结构。
- 是否改变 API response schema、前端用户可见工作流或报告结论格式。
- 是否合并到 `main`、发布 release 或关闭 PR/issue。
