# Agent 情绪热度候选闭环开发、测试与验收规格

本文面向执行 AI，收口现有情绪热度候选的默认自动发现和用户可见入口。这里的“闭环”是可选来源接入、展示和后续验证，不是新增情绪数据源或自动交易能力。

## 当前现状

- `get_sentiment_heat_candidates` 和 `sentiment_heat` 显式来源已实现，`tests/test_agent_sentiment_tools.py` 已覆盖工具与显式来源。
- `discover_watchlist_candidates(candidate_source="auto")` 当前多路召回由候选专家编排及其他自动来源构成；单独的 `sentiment_heat` 分支是显式模式，尚未并入 auto 主干。执行时要再次检查 `CandidateExpertOrchestrator` 是否已有情绪专家，避免重复接入。
- 前端 Candidate Pool 已有情绪/热点维度显示能力，但独立入口和 Trace 空态文案未完成。现有 Agent Trace 候选来源控件当前可能已改成实验性固定选项，需服从当前用户工作区，不回滚其改动。

## 目标和范围

1. `auto` 模式在不破坏现有主干/候选容量的前提下，调用现有情绪热度发现能力；每一步输出独立状态、候选数、耗时/质量和降级原因。
2. 与 AlphaSift、Sequoia、事件、消息、板块等来源按现有统一去重/合并机制合并，保存 `sentiment_heat:*` 来源证据、热度指标和理由维度。情绪热度仅为候选证据，不能直接升级成推荐/开仓结论。
3. Agent Trace 和候选池页面提供显式选择/筛选入口，展示候选源名称和关键热度依据；自动模式结果能识别该来源。没有情绪数据时准确显示未启用/无数据/工具失败，不再使用“后续情绪工具接入”这类过时文案。

非目标：新增 provider、扩大多市场、改 Regime 或 Risk Gate 评分、把人气等同基本面、增加用户风险画像/账户管理、令情绪候选绕过逐股深度取证。

## 实施步骤

1. 追踪 auto 路径实际编排入口，检查 `CandidateExpertOrchestrator` 的 expert registry、`market_tools.py` 中函数分支和 sentiment tool handler。确认情绪来源当前未在任何自动专家中召回后，再在最贴近现有架构的层新增接线，避免双重调用。
2. 确定调用超时、异常隔离和容量分配。情绪源失败不得让 auto 候选发现失败；情绪源不能挤掉所有策略主干名额。必须保留每源上限和最终候选总容量。
3. 统一候选字段：市场、代码、来源、`recall_sources`、`reason_dimensions`、评分、热度排名/变化、数据 as-of、数据质量。热度缺失时不填 0 冒充中性数据。
4. 为 Agent Trace 增加显式来源控制（沿用当前 UI 的来源选择机制），auto/default 仍可自动召回；候选决策卡片显示来源/理由。更新空状态文案，只陈述现有能力和本次结果。
5. 候选池页面增加情绪来源筛选/维度筛选；适配后端 source family 映射和生命周期状态。用户选择只控制发现来源，不得被解释成用户已认可或委托交易。
6. 更新 Prompt/tool descriptions：解释这是关注度/交易情绪观察信号，要与价格、资金、事件、基本面交叉验证；工具失败进入 evidence ledger，并触发保守降级。

## 测试验证

后端：auto 成功时确实调用一次现有情绪发现；显式 sentiment_heat 仍可用；多源重复候选正确合并并保留所有来源；候选容量和主策略席位不被突破；空情绪结果不伪造候选；情绪 provider 异常时其他来源继续；数据 stale/缺排名时质量状态可见；市场不支持行为不扩大。

前端：Trace 控件选择/自动模式；卡片显示来源和数据新鲜度；Candidate Pool 筛选/空状态/未知来源 fallback；不存在过期“尚未接入”文案。加入一条 API mock 的 Trace auto 结果渲染测试和 candidate pool source filter 测试。

运行 `rtk pytest tests/test_agent_sentiment_tools.py tests/test_candidate_pool_store.py tests/test_candidate_pool_api.py <新增候选发现测试>`、py_compile、Web `rtk npm run lint`/`rtk npm run build`、最终 `rtk ./scripts/ci_gate.sh`。若要验证在线情绪 provider，作为额外网络检查单独记录；确定性单测不得依赖实时外网。

## 验收条件

- auto 默认发现包含情绪候选且有单独 discovery step；来源失败不阻断其他来源。
- 现有显式 `sentiment_heat` 模式兼容，去重后仍可审计原来源。
- Trace 与候选池页面可显式看见/筛选来源及理由；无数据、失败和 stale 有区别。
- 情绪证据不会单独产生买入结论或绕过 Judge/Risk Gate。
- 删除或更新旧 Trace 空态过时文案；没有引入新数据依赖或多市场支持。

## 交付与回滚

用户可见来源能力更新 CHANGELOG、Agent 候选池专题文档和 API spec（仅当接口变化）。若 auto 接线质量异常，可按单个 discovery step feature flag/配置关闭，显式来源仍可保留；回滚不能删除现有情绪工具或误报它不存在。交付说明列出来源权重/容量策略、数据时效和失败降级验证。
