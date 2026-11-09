# Agent 候选池生命周期开发、测试与验收规格

本文面向执行 AI，要求把候选池的 `new/active` 写入扩展为可解释、可重建、可回评的生命周期，使前端已有 `watching/decayed/removed` 不再是死状态。

## 当前实现与权威口径

`src/agent/candidate_pool_store.py` 按 run 保存候选，并用近似重复次数写 `new/active`；表已有 `valid_until`、`recurrence_count`，但缺少跨 run 当前状态投影、到期转移和回评权重。前端 `CandidatePoolPage.tsx` 已呈现五种状态。API 当前主要是 runs/latest/detail。路线参考 `docs/plans/agent-candidate-pool-roadmap.md` 的生命周期、回评、质量章节；本文件将第一版范围压到生命周期确定性和离线回评，不实现自动线上策略改权。

## 状态定义与转移

状态是“某股票在候选池观察中的当前生命周期”，不是单次发现结果，也不是投资建议：

- `new`：第一次进入有效候选池，尚无跨批次重复证据。
- `active`：仍有效且在近期运行中被再次发现/确认。
- `watching`：仍在有效期内，但本轮未出现或证据不足以确认延续；保留为观察项，不等同负面判断。
- `decayed`：超过软有效期或质量分/新鲜度明显衰减，保留历史但降低展示优先级。
- `removed`：触发硬排除、明确失效/到期策略或人工移出；不可被普通新 run 静默复活，重新进入需记录 re-entry 原因并开启新生命周期。

转移必须由服务层的确定性规则和事件记录驱动。不得把缺失数据等同负面证据；不得仅因单次未召回即删除。阈值/有效期必须集中配置并可在 run 中记录规则版本。第一版可使用按来源族的静态有效期，但不能虚构回测最优阈值。

## 持久化模型与计算

保留现有 run/item 历史表并做向后兼容迁移。新增当前生命周期投影或等效表，主键至少为标准化市场 + 股票代码 + 生命周期 ID；记录状态、首次/最近发现时间、最近有效证据时间、有效截止、连续缺席批数、重复次数、原始/当前分、分数衰减原因、最近状态事件、规则版本和来源族。

每次保存 run 在事务内：写 immutable run/items -> 标准化同市场代码 -> 更新 lifetime 投影 -> 写 transition event。相同 run 重放须幂等。分数保留 raw score，不覆盖原始证据；derived score 单独保存并可重算。状态优先级：硬排除/明确到期 > 有效新证据 > 缺席观察/衰减。混合来源去重不能把一个来源的失效推断成所有来源失效。

数据迁移：现有记录推导最保守状态；缺少时间/来源时标记 `legacy_unknown` 并展示兼容，不批量将历史记录判成 `removed`。SQLite schema ensure/migration 重复运行安全，已有 API 字段保持兼容。

## 回评边界

实现离线候选结果回评：按候选产生时点锚定可用行情，计算明确窗口（首期建议 1/5/20 交易日，配置在代码中且响应中展示窗口）、收益、MFE/MAE、数据完整状态，并按来源族/策略版本聚合样本量与结果。必须使用 as-of 行情，排除候选产生前不可获得的数据；停牌、缺价、上市历史不足标记 `insufficient_data`，不能以零收益替代。

回评结果写入独立结果记录/快照，不改历史原始分。为满足回评权重回写闭环，按来源族/策略版本保存可审计的 `review_weight`（样本量、窗口、先验、计算公式、更新时间、版本均需记录），并将它回写到离线质量/策略评估记录中。第一版生产发现仍使用原始静态权重，`review_weight` 不得自动参与线上排序；启用线上动态权重属于后续需单独评审的变更。

## 实施顺序

1. 固化 symbol/market 标准化与来源族映射；补齐 `sentiment_heat` 等现存来源映射，不吞掉未知来源。
2. 添加 schema migration、lifetime/event store 和状态机纯函数；先写状态转移表驱动单测。
3. 将 `save_run` 更新为事务幂等地记录每只标的的 lifetime；处理重复 run、重复代码、跨市场同代码和候选再进入。
4. 增加显式 lifecycle 查询/API 及按状态筛选，保留现有历史 run API；补状态事件和回评只读端点。
5. 前端展示状态原因、有效期、近期发现/缺席、来源及回评数据质量；筛选器必须与后端枚举一致，并能看历史状态事件。
6. 实现离线回评 job/服务，复用本地日线行情数据，支持重复构建和版本化评估，并将来源/策略的 `review_weight` 回写至独立质量投影；不在页面请求中同步扫描全量样本。

## 测试验证

Store/state tests：首次 `new`、重复进入 `active`、缺席到 `watching`、有效期到 `decayed`、硬排除 `removed`、缺价不衰减、明确证据导致衰减/移除、removed re-entry、新旧 schema migration、同 code 不同 market、同 run 重放、事务失败不产生半写入、事件顺序/版本号。

Review tests：窗口边界、交易日而非自然日、收益/MFE/MAE 计算、前视数据隔离、缺失行情、停牌、重复构建、样本量统计、weight shrinkage/先验及版本更新；断言回评会持久化新质量权重且不会改变 raw score 和生产专家权重。

API/Web tests：枚举筛选、分页、未知状态兼容、状态原因和空数据、历史状态时间线、回评 partial/insufficient 展示。运行定向 `rtk pytest tests/test_candidate_pool_store.py tests/test_candidate_pool_api.py <new lifecycle/review tests>`、py_compile、Web lint/build 和 `rtk ./scripts/ci_gate.sh`。

## 验收条件

- 自然产生 `watching/decayed/removed` 且每个状态附可解释原因/时间/规则版本。
- 同一股票跨 run 的状态连续，按 market 隔离，历史 run 不被改写。
- migration、安全重放和异常中断不丢历史或生成重复生命周期事件。
- 回评有明确 as-of 口径和数据质量状态；来源/策略 review weight 可持久化、可解释、可复算，但不自动改线上权重。
- 页面可筛选并解释所有生命周期状态，不再用静态文案模拟状态。

## 交付与回滚

同步更新候选池专题文档、API spec（若字段变更）和扁平 CHANGELOG。旧表保留，若新生命周期读路径有问题可 feature flag/路由回退到历史 runs 查询；不可在回滚时删历史数据。交付说明报告迁移策略、窗口口径、阈值版本和回评样本限制。
