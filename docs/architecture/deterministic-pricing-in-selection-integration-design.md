# 方案二：把确定性点位引擎接进选股链路（方案设计）

> 范围：**只做设计**。本轮不改代码、不改 prompt、不做 git 操作。
> 目标：`watchlist_scan` 选股链路改为「`src/agent/pricing/` 确定性引擎是唯一价格权威，LLM 只做判断和证据」。
> 关联文档：[execution-mode-generation-and-action-constraint-options.md](execution-mode-generation-and-action-constraint-options.md)、[selection-pipeline-open-blockers-diagnosis.md](selection-pipeline-open-blockers-diagnosis.md)、[selection-threshold-quantile-calibration.md](selection-threshold-quantile-calibration.md)

标注约定：

- **【事实】**：已在本仓库代码/本地数据中核对，附文件与行号。
- **【方案建议】**：依赖设计取舍，不是现状。
- **【需用户决策】**：必须由你拍板，正文只列选项和后果，**不填默认答案**。汇总见 §9。

---

## 0. 摘要

**先纠正一处上一轮简报中的事实错误。**

【事实】上一轮说"引擎只支持 A 股主板普通股"不准确。`is_supported_cn_common_stock` 接受的前缀是 `600/601/603/605/688/689/000/001/002/003/300/301/4/8/92`（[market_rules.py#L18-L26](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/market_rules.py#L18-L26)），也就是说**主板、创业板、科创板、北交所代码都能通过符号过滤**；真正的门槛在"元数据是否已验证"和"是否停牌/退市"，不在板块。

**方案骨架（【方案建议】）。**

1. 在 `pricing_agent` 阶段改为调用引擎，引擎结果落到新增的 `positions_plan[].price_plan_ref` 与一整套数值字段；`pricing_agent` 的 LLM 调用被删除或降级为"只解释、不出数"。
2. 渲染层只从 `price_plan` 的数值字段生成 `entry_condition` / `stop_loss_condition` 文本，`pricing_agent` 与深度层里的 `ideal_entry_zone` / `stop_loss` 全部忽略。
3. 引擎失败 → 该票**绝不回退到 LLM 价格**；按失败原因分流为"剔除 / 观察 / 待复核"。
4. 分四阶段上线：影子 → 单票替换 → 全 A 股替换 → 回测复盘切换。

**必须先拍板的三件事（详见 §9）：**

- 港股/美股权威性怎么处理（D1）；
- 引擎失败票在报告里的处置口径（D2）；
- 场景语义是替换还是并存（D3）。

---

## 1. 结构映射

### 1.1 字段级对照表

【事实】引擎 `ZoneScenario` 的数值字段见 [schemas.py#L164-L226](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/schemas.py#L164-L226)；选股 `positions_plan[]` 的现有字段见 [stock_selection_prompts.py#L684-L735](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L684-L735)。

| 引擎字段 | 选股现有字段 | 映射类型 | 说明 |
| --- | --- | --- | --- |
| `scenario_id` | — | **新增** | 用于回测/复盘/去重，必须落库 |
| `role`（PRIMARY/SECONDARY/OBSERVATION） | `execution_mode`（部分） | 语义转换 | 只有 PRIMARY 可映射 `conditional_open`；SECONDARY 视强度；OBSERVATION 只能 `strong_watch`/`plain_wait` |
| `entry_low` / `entry_high` | `entry_condition`（字符串） | **新增数值 + 文本派生** | 建议同时保留数值字段，文本由数值生成 |
| `entry_reference`（恒等于 `entry_high`） | — | 新增 | [plan.py#L304](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/plan.py#L304) |
| `stop_loss` | `stop_loss_condition`（字符串） | **新增数值 + 文本派生** | 可能为 `None`（`NO_VALID_STOP`） |
| `target_1` / `target_2` | — | **新增** | 现有选股结构没有目标价字段 |
| `target_1_exit_ratio` | — | **新增** | 有 `target_2` 时 50%，否则 100% |
| `reference_net_rr_1` | — | **新增** | 净盈亏比（含成本） |
| `reachability_band` / `price_relation` | — | **新增** | 用于"现在离入场区多远"的展示与排序 |
| `confirmation_rules` / `invalidation_rules`（RuleSpec） | `entry_condition` / `stop_loss_condition` 文本 | **新增结构化字段 + 文本摘要** | 文本摘要必须保留关键词，见 §1.3 |
| `valid_from` / `confirmation_deadline` / `order_valid_trading_days` | `signal_validity`（字符串） | 语义转换 | 字符串需按固定模板生成 |
| `scenario_reason_codes` | — | **新增** | 下游分流失败原因的唯一依据 |
| `confirmation_seed_bars` / `anchor_refs` / `scenario_fingerprint` | — | 新增（可只存 artifact） | |
| 引擎 `generation_status` / `data_quality` / `reason_codes` | — | **新增（计划级）** | 建议挂 `price_plan` 对象而不是逐票复制 |
| — | `action` / `action_strength` | **保留（LLM）** | 判断归 LLM，价格归引擎，需用户确认（D4） |
| — | `execution_mode` | **冲突** | 见 §1.2；建议由引擎可执行性否决 LLM 的乐观值 |
| — | `scenario_name`（Breakout_Continuation 等） | **丢失** | 引擎没有叙事场景名，只有角色与锚点来源 |
| — | `condition` / `risk_reward_comment` / `constraints_used` | **丢失或降级** | 可保留为 LLM 叙述，但不得含数字 |
| — | `reentry_reference` | **丢失** | 引擎无该语义 |

**丢失项（【事实】）**：三个具名叙事场景、自然语言 `condition`、`risk_reward_comment`、`constraints_used`、`reentry_reference`。它们都是 LLM 产物，与"AI 不再出点位"的目标冲突，**不能原样保留数字**。

**需要新增的字段（【方案建议】）**：至少 12 个（`plan_id`、`scenario_id`、`role`、`entry_low`、`entry_high`、`stop_loss`、`target_1`、`target_2`、`target_1_exit_ratio`、`reference_net_rr_1`、`reason_codes`、`confirmation_rules`/`invalidation_rules`）。理由：现有结构只有两段自由文本，无法表达目标、盈亏比、机读规则。

### 1.2 场景语义对齐

【事实】引擎场景数量是 **1–3 个**，按锚点簇排序，角色为 `PRIMARY / SECONDARY / OBSERVATION`，没有任何 "Breakout/MeanReversion" 这类叙事名（[plan.py#L335-L345](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/plan.py#L335-L345)、[schemas.py#L164-L180](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/schemas.py#L164-L180)）。
【事实】选股 `pricing_agent` 被要求每只股票固定输出 3 个具名场景（[stock_selection_prompts.py#L624-L640](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L624-L640)）。

三种对齐方式：

| 方式 | 做法 | 后果 |
| --- | --- | --- |
| A. 1:1 换皮 | 保留三个叙事名当标签，价格全部由引擎填；引擎不足 3 个时空着 | 最简单，但标签与锚点簇可能语义不符（叙事的 Breakout 可能对应不到 PRIMARY 簇） |
| B. 叙事归叙事、定价归定价 | 叙事场景仍由 LLM 写，但每个场景必须引用 `scenario_id` 且不得给价 | 保留可读性，但需要 LLM 输出 `scenario_id` 的映射能力，容易错配 |
| C. 弃叙事名 | 统一为"主场景/次场景/观察场景"，触发条件由 `confirmation_rules` 生成 | 语义最干净、可机检；报告可读性下降，需要改渲染模板 |

【方案建议】C 为主，A 作为过渡。理由：引擎的 PRIMARY/SECONDARY 本身带 `source_families` 与 `anchor_refs`，可以自动生成"趋势回踩型/成本支撑型/结构支撑型"这类客观标签，不需要 LLM 编叙事。
【需用户决策】见 D3。

**数量不足/多余**：【事实】引擎最多 3 个、可能只有 1 个或 0 个正式场景（0 个时 `generation_status=observation`，[plan.py#L110-L125](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/plan.py#L110-L125)）。【方案建议】不足不补，多余不可能（已有 `max_length=3`）；下游渲染需要能处理 1、2、3 个场景。

### 1.3 Decimal → 字符串的转换点与精度

【事实】引擎全程 `Decimal`，tick 对齐在 `floor_to_tick` / `ceil_to_tick` 完成（[clustering.py#L79-L90](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/clustering.py#L79-L90)）；`entry_reference` 被强制等于 `entry_high`；`stop_loss` 与目标都由 `Decimal` 计算后再对齐。
【事实】下游 `positions_plan` 的 `entry_condition` / `stop_loss_condition` 是**自由文本**，`trade_plan_guard` 用 `float()` 解析文本里的数字做校验（[trade_plan_guard.py#L30-L95](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/trade_plan_guard.py#L30-L95)）。

【方案建议】转换放在"生成 `positions_plan` 的那一层"（当前 `_recommendation_items` 尚未涉及），规则：

1. **先存数值，再派生文本**：新增数值字段保持 `Decimal`（JSON 序列化为字符串），文本只是展示层。
2. 文本统一用 `format(value, ".2f")`（A 股 tick=0.01），禁止 `float() -> str`。
3. 所有区间文本使用固定模板，例如 `"待价格回到 {low}–{high} 区间并满足缩量确认"`，确保渲染层关键词命中。
4. 校验层优先读数值字段，文本仅用于展示；`trade_plan_guard` 需增加"数值优先"分支。

【风险】现有渲染器靠文本关键词判断入场条件来源（[stock_selection.py#L6748-L6830](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6748-L6830)），模板一旦不含"区间/缩量/站稳"等词，会落到默认文案或丢失触发说明。这是 §8 的风险 2。

---

## 2. 市场覆盖

### 2.1 引擎真实支持边界

【事实】引擎不是"只支持主板"：

- 符号过滤接受主板、创业板（300/301）、科创板（688/689）、北交所（4/8/92）（[market_rules.py#L18-L26](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/market_rules.py#L18-L26)）。
- 板块与涨跌停：创业板/科创板 20%，北交所 30%，其余 10%，ST 覆盖为 5%（[market_rules.py#L46-L61](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/market_rules.py#L46-L61)）。
- tick 固定 0.01、每手 100 股、T+1（[market_rules.py#L89-L95](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/market_rules.py#L89-L95)）。
- 只做多、只有日线（`side`/`timeframe` 是唯一字面量，[schemas.py#L246-L248](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/schemas.py#L246-L248)）。
- **港美股**：`normalize_cn_symbol` 只保留数字后 6 位，`hk00700` / `AAPL` 都会变成空串或不足 6 位 → 直接 `UNSUPPORTED_INSTRUMENT`，`execution_status=no_plan`（[runtime.py#L119-L124](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/runtime.py#L119-L124)）。

### 2.2 港股/美股的处理选项

【事实】选股管道 `_resolve_market` 支持 `cn/hk/us`（[stock_selection.py#L5877 附近](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L5877)）；引擎对这两类市场必然返回 `UNSUPPORTED_INSTRUMENT`。

| 选项 | 后果 |
| --- | --- |
| A. 港美股继续走 LLM 给价 | 与"唯一价格权威"直接冲突，但保住功能完整性；必须在报告里显式标注"该市场点位来自 AI" |
| B. 港美股点位标为不可用，只保留判断/观察 | 口径统一、不误导；港美股报告无法直接执行，产品能力下降 |
| C. 港美股暂不下发可执行结论 | 最保守；选股结果只剩 A 股可用，可能引发用户质疑"为什么美股没结果" |
| D. 新建港美股规则适配（tick/手数/涨跌停/交易日历） | 工作量最大，但能真正做到全市场唯一权威 |

【方案建议】短期 B，中期 D。依据：引擎的规则层已经把 tick/手数/涨跌停参数化，扩展到港股不是重写，而是补一套 `MarketRuleSnapshot`；但需要新的交易日历与最小变动价位数据，属于独立工程。
【需用户决策】D1。

### 2.3 可交易性判定链（逐条核对，不猜）

【事实】引擎的实际判定顺序：

1. 开关关闭 → `SYSTEM_DISABLED`，`execution_status=system_disabled`（[runtime.py#L111-L116](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/runtime.py#L111-L116)）。
2. 符号不在允许前缀 → `UNSUPPORTED_INSTRUMENT`，`execution_status=no_plan`。
3. 就绪闸门不过 → `SYSTEM_NOT_READY`，`execution_status=system_unready`。
4. 快照构建异常 → 同样 `SYSTEM_NOT_READY`（[runtime.py#L178-L184](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/runtime.py#L178-L184)）。
5. 计划级致命原因（任一命中即 `no_plan`）：`UNSUPPORTED_INSTRUMENT`、`INSUFFICIENT_DAILY_HISTORY`、`INSUFFICIENT_WEEKLY_HISTORY`、`MISSING_CASH_PRICE`、`PRICE_SCALE_UNVERIFIED`、`MARKET_RULES_UNAVAILABLE`、`AS_OF_INCONSISTENT`（[plan.py#L348-L379](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/plan.py#L348-L379)）。
6. `MARKET_RULES_UNAVAILABLE` 的触发条件包括：tick ≤ 0、无前收/涨跌停价、`instrument_type != "common_stock"`、未验证上市状态、非 `listed`、停牌。**ST 本身不拒绝**（只要求 `st_status_verified`），只是涨跌停按 5% 计。

【事实】元数据验证只对"当前有效交易日 + 本地股票索引内"生效（[market_rules.py#L121-L149](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/market_rules.py#L121-L149)）。

【事实】本地 `static/stocks.index.json` 覆盖 31,571 条，其中 CN 5,191 条：主板 754+225+611+113，科创板 603+1，创业板 939+453，**北交所 0 条**、B 股 0 条。
【事实】因此在本仓库环境下：创业板/科创板若在索引内可正常验证；**北交所标的无法自动验证元数据**，只能依赖已持久化的 `market_rule_snapshots`（本地仅有 14 条、日期集中在 2026-09-18）。
【风险】`is_supported_cn_common_stock` 把 `4xxxxx` 也当北交所（30% 涨跌停）；`4` 开头还包含部分新三板代码，存在误分类可能。这是代码事实，是否要预过滤见 D9。

---

## 3. 数据依赖

### 3.1 逐项核对

【事实】引擎依赖的 8 张表（就绪闸门要求，[readiness.py#L1-L60](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/readiness.py#L1-L60)）：

`trade_plan_artifacts`、`plan_review_artifacts`、`account_plan_decisions`、`plan_lifecycle_states`、`plan_lifecycle_events`、`market_rule_snapshots`、`chip_distribution_snapshots`、`trade_plan_trace_links`。

【事实】本地 `data/stock_analysis.db` 上述 8 张表全部存在。

| 依赖项 | 引擎要求 | 选股现状 | 缺口 |
| --- | --- | --- | --- |
| 日线根数 | 520 评估 + 200 预热 = **720 根**（[params.py#L59-L60](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/params.py#L59-L60)） | 选股目前只取近 40 根算 ATR20（[stock_selection.py#L5245 附近](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L5245)） | **有**：需新增 720 根加载 |
| 周线根数 | 60+40 = **100 根**（由日线聚合，不额外请求） | 无 | 可由日线生成，无外部缺口 |
| 本地日线缓存 | — | `stock_daily` 仅 **50 个代码**；≥720 根的只有 **1 个**；Sequoia 库 5,601 个代码但只有 2025-10-23~2026-11-17（约 260 根） | **大**：绝大多数标的必须走网络 |
| 网络兜底 | `load_history_df` 默认允许网络；请求 `days*2` 日历日（[base.py#L353-L361](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/data_provider/base.py#L353-L361)） | 选股工具已具备网络兜底 | 可满足 720 根（前提 provider 返回全区间），但每票一次请求 |
| 筹码分布 | 可选，缺失只进 `missing_fields`（[snapshot.py#L80-L88](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/snapshot.py#L80-L88)） | 选股未调用筹码工具 | 不阻断；但会少一族锚点（成本量） |
| 市场规则 | 当前交易日 + 索引内，或已持久化快照 | 选股无持久化规则快照 | **有**：非索引标的/历史日期会 `SYSTEM_UNREADY` |
| 价格刻度 | `_verify_price_scale` 必须 verified（[snapshot.py#L288-L300](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/snapshot.py#L288-L300)） | 选股用缓存数据，复权口径未必稳定 | **中**：复权基准冲突会 `PRICE_SCALE_UNVERIFIED` |
| 就绪闸门 | 每次调用都跑（表/参数/规则/指纹） | 无 | 需在选股前预跑一次并复用结果 |

### 3.2 不同标的会返回什么

【事实】

| 标的类型 | 引擎返回 |
| --- | --- |
| 上市不足约 2 年（<520 根） | `no_plan` + `INSUFFICIENT_DAILY_HISTORY` |
| 数据源缺当天/前收 | `no_plan` + `MISSING_CASH_PRICE` 或 `MARKET_RULES_UNAVAILABLE` |
| 停牌/退市整理 | `no_plan` + `MARKET_RULES_UNAVAILABLE` |
| 复权口径冲突 | `no_plan` + `PRICE_SCALE_UNVERIFIED` |
| 元数据无法验证（如北交所、历史日期） | `SYSTEM_NOT_READY`（`system_unready`） |
| HK/US | `UNSUPPORTED_INSTRUMENT`，`execution_status=no_plan` |
| 有数据但止损不可用 | 场景 `role=OBSERVATION` + `NO_VALID_STOP`，计划仍可 `observation` |
| 有数据但目标太近 | 场景 `OBSERVATION` + `TARGET_TOO_CLOSE` |
| 净盈亏比不足 | 场景 `OBSERVATION` + `RISK_REWARD_BELOW_MINIMUM` |
| 有正式场景 | `generation_status=generated`，最多 3 个，至少 1 个 PRIMARY |

【事实】是否 `no_plan` 只看 §2.3 第 5 步那 7 个计划级原因；`NO_VALID_STOP`、`TARGET_TOO_CLOSE`、`RISK_REWARD_BELOW_MINIMUM` **不会**让整只票失败，只会让该场景降级为观察（[plan.py#L196-L246](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/plan.py#L196-L246)）。

### 3.3 一次选股的开销

【事实】纯计算：本地基准（720 根合成日线，单次 `evaluate_price_plan`）**平均 31.26 ms/只**（100 次均值，本机实测）。
【事实】`generate_trade_plan(..., persist=True)` 的 IO 结构：

- 就绪闸门：1 次表元数据查询 + 1 次规则快照查询；
- 快照：日线 1 次范围查询（缓存不新鲜时 +1 次 Sequoia +1 次网络）、周线由日线聚合、筹码 1 次（可能走工具/网络）、规则 1 次；
- 落库：`save_market_rules`、`save_chip_snapshot`、`save_artifact`（含场景/事件）、`save_review`、`save_account_decision`、`save_trace_link`，约 6 个独立事务。

【方案建议】

1. **就绪闸门每次运行只跑一次**，结果复用；不要逐票跑。
2. **快照批量预取**：`generate_trade_plan` 接受现成 `snapshot` 参数，可用现有 `ThreadPoolExecutor` 证据阶段的并行度（配置 `agent_seed_fact_max_workers`，默认 12）先并发拉 720 根，再串行/小并发跑纯计算。
3. **`persist` 分级**：影子阶段只落 artifact，不写 review/decision；正式阶段再全量写。
4. 30 只候选：计算约 1 秒；DB 约 250–300 次往返；网络 0–30 次请求（取决于缓存命中）。**网络是主要时延来源**，需设上限与超时，失败即 `SYSTEM_NOT_READY`，不要重试放大。

【不确定】生产环境 `stock_daily` 的实际覆盖率未知；本地样本只有 50 个代码，不能代表线上。

---

## 4. 失败与降级

### 4.1 状态与原因码的处理建议

【事实】引擎的状态枚举：`system_disabled` / `system_unready` / `no_plan`（含 `UNSUPPORTED_INSTRUMENT`）/ `actionable` / `waiting_confirmation` / `observe_only` / `vetoed` / `risk_blocked` / `closed`（[decision.py#L664-L701](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/decision.py#L664-L701)、[runtime.py#L111-L146](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/runtime.py#L111-L146)）。

| 结果 | 【方案建议】选股处置 | 依据 |
| --- | --- | --- |
| `system_unready` | 整轮暂停出价，报告显式提示"点位引擎未就绪"；该票降为观察，**不入选可执行列表** | 系统级问题不是个股问题，不能逐票伪装成"不适合" |
| `UNSUPPORTED_INSTRUMENT` | 按 D1 的港美股/非普通股策略处理 | 事实：非普通股在 `MARKET_RULES_UNAVAILABLE`/符号层就失败 |
| `no_plan` + `INSUFFICIENT_DAILY_HISTORY` / `MISSING_CASH_PRICE` / `PRICE_SCALE_UNVERIFIED` | 观察池 / `needs_review`，不入选 | 数据问题，不代表标的不好 |
| `no_plan` + `MARKET_RULES_UNAVAILABLE`（停牌/退市/非普通股） | 剔除 | 不可交易 |
| 场景全为 `OBSERVATION`（`NO_VALID_STOP` / `TARGET_TOO_CLOSE` / `RISK_REWARD_BELOW_MINIMUM`） | 强观察（`strong_watch`），可出现在观察池，不可给 `conditional_open` | 有价但不可执行 |
| `actionable` + PRIMARY | 可给 `conditional_open`，首仓按引擎的 `target_position_pct`（若接 decision） | |
| `waiting_confirmation` | `conditional_open`，文本写确认条件 | |
| `vetoed` / `closed` / `risk_blocked` | `reject` 或 `plain_wait` | |

【需用户决策】D2：是"引擎失败就剔除"还是"统一降为观察"，需要产品口径。

### 4.2 "绝不回退 LLM 给价"的实现保证

【事实】现有链路里 LLM 给价的入口不止一个：

1. `pricing_agent.if_then_order_matrix`（`entry_zone`/`stop_loss` 字符串）；
2. 深度结果 `entry_quality.ideal_entry_zone` / `stop_loss`（[stock_selection_prompts.py#L246-L266](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection_prompts.py#L246-L266)）；
3. `_fallback_pricing_agent` 会复制 Meta 约束里的价位字符串（[stock_selection.py#L4712 附近](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L4712)）；
4. 单标的 trace 收尾时另有一套确定性计划（见 §5.3），与 LLM 价格并存。

【方案建议】四层保证：

1. **数据层**：`positions_plan[]` 的数值字段只允许来自 `price_plan`；写回函数做"来源断言"——没有 `plan_id` 的数值直接丢弃并记 `missing_evidence`。
2. **渲染层**：`_entry_condition_source_from_plan` 不再解析 LLM 文本，只读数值字段；深度层 `ideal_entry_zone`/`stop_loss` 在选股链路中强制置空（或干脆从 prompt 移除，但本轮不动 prompt）。
3. **兜底层**：`_fallback_pricing_agent` 改为**只返回场景骨架、不含数字**；它不得成为引擎失败时的替代价格源。
4. **验收层**：新增不变量测试——"报告 JSON 中任何数字价位都必须能追溯到 `plan_id`"。

【方案建议】测试清单：

- 单测：引擎返回 `no_plan` 时，`positions_plan` 不含任何数字 `entry/stop`；
- 单测：LLM 输出里带价格的字段被 `sanitize` 剥离（现有 `sanitize_llm_price_fields` 可复用，[decision.py#L815 附近](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/decision.py#L815)）；
- 契约测试：`final_report_json` 全量扫描，凡出现形如 `\d+\.\d{2}` 的 entry/stop，必须带 `plan_id`；
- 集成测试：mock 引擎全部失败，断言报告仍能生成且没有价格；
- 回归测试：固定 10 只票离线快照，比较新旧输出差异。

---

## 5. 对下游的影响面

### 5.1 报告渲染

【事实】渲染器从 `positions_plan` 派生的推荐项读取 `entry_condition` / `stop_loss_condition` / `signal_validity`；并用文本关键词推断"条件是否完整"（[stock_selection.py#L6748-L6830](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L6748-L6830)）。
【方案建议】**必须同步改**：新增"数值优先"分支；模板文案固定，避免关键词失配。
【兼容】旧字段保留，仅新增数值字段，旧报告仍可渲染。

### 5.2 `trade_plan_guard`

【事实】它对 `pricing_full` / `allocation_full` 做价格合理性校验，用 ATR20 与文本解析（[stock_selection.py#L5356-L5369](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L5356-L5369)、[trade_plan_guard.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/trade_plan_guard.py)）。
【方案建议】**建议同步改**：让 guard 优先校验数值字段；否则引擎输出被转成文本后再解析，可能产生"区间被解析成单值"的误报。
【兼容】可以只加分支，不改现有签名。

### 5.3 单标的 trace 收尾的既有确定性计划

【事实】API 在 trace 收尾时，若 `positions_plan` 恰好只有一个标的，会**额外**跑一次引擎并合并确定性块（[agent.py#L1996-L2140](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/api/v1/endpoints/agent.py#L1996-L2140)、[agent.py#L2624 附近](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/api/v1/endpoints/agent.py#L2624)）。
【方案建议】选股链路接入后，应复用这次生成结果，避免同一次运行生成两份计划；并把"单标的才生成"的限制去掉。
【不确定】合并后的确定性块在选股报告里的展示位置与优先级，代码中没有看到显式约定。

### 5.4 entry 回测服务

【事实】`agent_entry_execution_backtest_service` 有两条路径：确定性 artifact 路径（使用 `evaluate_price_plan_backtest`）与"选股 trace"路径——后者从 `final_report.portfolio_allocation.positions_plan` / `pricing_agent.if_then_order_matrix` 正则抽价，策略名如 `strict_ai_entry`（[agent_entry_execution_backtest_service.py#L1287-L1297](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/services/agent_entry_execution_backtest_service.py#L1287-L1297)、[#L1429-L1440](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/services/agent_entry_execution_backtest_service.py#L1429-L1440)）。
【方案建议】**必须同步改**：选股路径改读数值字段；否则新报告会被旧正则解析，回测口径与展示口径分裂。

### 5.5 API / Web / Desktop

【事实】API 对选股结果是 dict 透传，没有严格 schema；Web 主要渲染 Markdown，`positions_plan` 没有强类型；Desktop 是 Web 的壳。
【方案建议】API 追加字段即可，属向后兼容；Web 若要在表格里展示目标价/盈亏比，需要小改；Desktop 无独立改动。
【兼容】旧字段不删，前端不需要立刻改。

---

## 6. 历史 trace 兼容

【事实】历史 trace 的 `final_report_json` 里只有 LLM 文本价，没有 `plan_id`；`status` 与 markdown 已经固化。
【方案建议】

1. **不回填**历史 trace（引擎重算会改变历史结论，破坏可追溯性）。
2. 新旧区分靠 `price_plan.algorithm_version` / `params_version` / `plan_id` 是否存在；报告头部标注"点位来源：确定性引擎 v1 / 历史 AI 文本"。
3. 回测/复盘按 `plan_id` 分组，历史样本单独统计，不与新样本混算。
4. 查询接口同时返回 `price_source` 字段（`deterministic_v1` / `legacy_llm`）。

【需用户决策】D6：是否允许对历史 trace 做"影子重算"（只存不展示，用于对比），以及是否要回填。

---

## 7. 落地节奏

【方案建议】

**阶段 0：影子模式（不改任何用户可见输出）**

- 先按 §11 完成日线补齐（E1 待拍）；补齐到位后，选股跑完对候选池**额外**跑引擎，`persist=False`，只落一份独立对账文件 `pricing_shadow.json`（覆盖率、失败原因分布、与 LLM 价位差异）。详细设计见 §12。
- 验证：引擎覆盖率、各 reason 占比、引擎 vs LLM 入场区偏离分布；不改变报告、不写业务表。
- 回滚：关闭 `AGENT_PRICING_SHADOW_ENABLED` 即可，零副作用。

**阶段 1：单市场单票替换（A 股，只对"引擎成功"的票展示引擎价）**

- 引擎成功的票：数值来自引擎；失败的票：不显示任何价格（不是回退 LLM）。
- 验证：不变量测试 + 10 只票人工对账 + 报告渲染快照测试。
- 回滚：保留旧字段与旧渲染分支，开关回退。

**阶段 2：A 股全量唯一权威（D1：港美股维持现状，不纳入本轮）**

- 删除 A 股 `pricing_agent` 的 LLM 价格输入；`_fallback_pricing_agent` 去数字；港美股链路保持不动。
- 落实 D4 的引擎否决权：引擎 `OBSERVATION`/`no_plan` 时，LLM 的 `action`/`execution_mode` 不得高于观察/等待。
- 验证：全链路回归、回测口径一致性、报告抽样。
- 回滚：按 market 维度开关，港美股本来就不受影响。

**阶段 3：回测/复盘切换到 `plan_id` 口径**

- 回测服务改读数值字段；历史样本单列。
- 验证：同一批 trace 双跑，差异可解释。

每阶段都有独立开关与回滚点；不要一次全量切换。

---

## 8. 风险最高的三处

1. **数据覆盖与就绪闸门导致大面积"无价"。**
   【事实】本地 50 个代码中只有 1 个满足 720 根；本地索引无北交所；规则快照只有 14 条。直接切换会让"唯一权威"变成"大多数票没有价格"。
   【依据】§3.1 实测数据。缓解：阶段 0 必须先量化覆盖率，再决定是否允许网络兜底与批量预取。

2. **渲染与执行语义依赖文本关键词。**
   【事实】`_entry_condition_source_from_plan` / `_necessary_conditions_text` 靠关键词判断条件完整性；引擎规则摘要若不命中关键词，会退回默认文案，用户看不到触发条件。
   【依据】§5.1。缓解：模板固定 + 数值优先 + 渲染快照测试。

3. **双价格来源长期并存，导致回测/复盘口径分裂。**
   【事实】回测服务仍在用正则从 LLM 文本抽价；单标的 trace 收尾还会额外生成一份确定性计划。
   【依据】§5.3、§5.4。缓解：先统一数据出口（只允许 `plan_id` 价格进入报告），再切回测。

【补充风险（低于前三）】`execution_mode` 的冲突已由 **D4（引擎否决权）** 定调：引擎 `OBSERVATION`/`no_plan` 时 LLM 不得给出高于观察/等待的动作。剩余风险是实现时是否真的把否决层加在所有出口（报告、API、回测），需要测试兜底。

---

## 9. 决策记录

### 9.1 已拍板（2026-11-19）

| 编号 | 决策点 | 结论 | 对设计的影响 |
| --- | --- | --- | --- |
| **D1** | 港股/美股的点位权威 | **本次改造范围只含 A 股。港美股保持现状不动，不纳入本轮。** | §2.2 的四个选项全部不适用于本轮；港美股继续走现有 LLM 链路，但**不得**与 A 股共用"引擎唯一权威"的表述；报告/API 需能区分市场来源。 |
| **D2** | 日线数据不足 | **用 Tushare 拉取补齐。** | §3.1 的 `INSUFFICIENT_DAILY_HISTORY` 缺口通过数据补齐解决，不是通过降级或跳过解决；补齐链路的量级、配额、复权口径需在阶段 0 实测（见 §11）。 |
| **D4** | `action` / `execution_mode` 归谁 | **引擎具备否决权。LLM 不能越过引擎给出可执行动作；引擎场景全为 `OBSERVATION` 或整票无计划时，LLM 的最高动作不得高于观察/等待。** | `pricing_agent`/`portfolio_allocation` 输出的 `action_bias`、`execution_mode` 必须经过引擎结果裁剪；引擎是价格与可执行性的上位约束，LLM 只在引擎允许的范围内做判断与证据。实现上表现为"引擎否决层"，不是"双写后人工仲裁"。 |

### 9.2 仍待用户决策

| 编号 | 决策点 | 选项 | 影响 |
| --- | --- | --- | --- |
| **D2'** | 引擎失败的票怎么处置（D2 已定为补数据，但补完仍失败的票未定） | A 剔除；B 降为观察；C 标 `needs_review` 等人看 | 影响报告数量与用户信任；建议按 reason 分流，但整体口径要定 |
| **D3** | 场景语义 | A 1:1 换皮保留叙事名；B 叙事与定价分离；C 弃叙事名、统一主/次/观察 | A 最快但易错配；C 最干净但改渲染 |
| **D5** | 是否接 `enrich_trade_plan_result`（review + 账户决策 + 仓位） | A 只取 `price_plan`；B 接 review；C 接 review + account decision | 接入越深，DB 写入与账户上下文依赖越多 |
| **D6** | 历史 trace | A 不回填、只标注来源；B 影子重算不展示；C 回填 | 回填会改写历史结论 |
| **D7** | 单标的 trace 的既有确定性计划 | A 复用；B 保留双份；C 删除旧路径 | 影响一致性与重复写库 |
| **D8** | `system_unready` 的处理粒度 | A 整轮暂停出价；B 逐票降级；C 只有失败票降级并提示 | A 最一致但可能整轮无结果；B 掩盖系统问题 |
| **D9** | 北交所/ST/次新是否预过滤 | A 在候选发现阶段就排除；B 交给引擎返回 no_plan；C 特殊规则 | 影响候选池与失败率 |
| **D10** | 是否允许人工改引擎价位 | A 完全禁止；B 只允许 `needs_review` 标记；C 允许覆盖但留痕 | 与"数学模型给点位"的体制直接相关 |

---

## 10. 事实清单与未验证项

### 10.1 本轮核对到的事实

- 引擎与选股链路之间**没有任何 import**；`stock_selection.py` 不引用 `src/agent/pricing/`。
- 引擎支持主板/创业板/科创板/北交所代码；港美股在符号层直接失败。
- 引擎要求 720 根日线 + 100 根周线；本地 `stock_daily` 只有 50 个代码、1 个达到 720 根；Sequoia 库约 260 根。
- 本地索引 5,191 个 CN 代码，无北交所；规则快照 14 条、集中在 2026-09-18。
- 纯计算约 31 ms/只；`persist=True` 约 8–10 次 DB 往返 + 最多 2 次网络请求。
- 计划级失败只有 7 个原因；`NO_VALID_STOP` 等只降级场景，不整票失败。
- 引擎失败不会回退 LLM 价格——**但前提是选股链路改造时主动删除回退路径**；现状里 `_fallback_pricing_agent` 仍会复制上游文本价。

### 10.2 未验证/不确定

- 生产环境日线缓存覆盖率（本地样本不代表线上）。
- 选股报告中确定性块的展示位置与优先级。
- 北交所/新三板候选是否真的会进入选股池。
- `pricing_agent` 的 3 个叙事场景与引擎锚点簇的可对齐率（需阶段 0 数据）。
- 港美股规则层改造成本（未评估外部数据源）。

---

## 11. 阶段 0 准备：量级核算与真实 API 实测（2026-11-19）

本节全部为【事实】，除明确标注【方案建议】的段落。

### 11.1 补齐范围与量级

【事实】Tushare `stock_basic`（`list_status=L`）返回 **5,580** 只在市 A 股：

| 板块 | 数量 |
| --- | ---: |
| 主板（沪 + 深） | 3,194 |
| 创业板 | 1,411 |
| 科创板 | 619 |
| 北交所 | 356 |

【事实】按交易日拉取时，2026-11-18 全市场返回 **5,573** 行（停牌股不返回，官方文档明确"停牌期间不提供数据"）。

【事实】720 根日线的窗口约等于最近 2.9 年（本地 002475 的 720 根覆盖 2023-11-17 ~ 2026-11-09，可交叉验证）。

| 口径 | 数值 |
| --- | ---: |
| 全量行数（按标的） | 5,580 × 720 = **4,017,600 行** |
| 全量行数（按交易日实际） | 5,573 × 720 = **4,012,560 行** |
| 实测单行占用（表 + 索引，临时库 18,640 行） | **230 B/行** |
| 预计存储增量 | **约 0.9 GB**（4.0M × 230 B ≈ 0.92 GB） |
| 当前库体积（`data/stock_analysis.db`） | 约 136–143 MB |

【事实】`stock_daily` 现有 50 个代码、11,054 行；表 + 索引共 2.9 MB，与 230 B/行一致。

### 11.2 Tushare 配额、限频、权限（实测，不靠假设）

【事实】仓库实际调用的不是官方 `api.tushare.pro`，而是 `.env` 中的 `TUSHARE_HTTP_URL=https://ts.gyzcloud.top/api`（第三方兼容网关），代码默认值则是 `http://118.89.66.41:8010/`（[tushare_client.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/data_provider/tushare_client.py#L24-L24)）。因此官方配额只能作参考，实际以后端网关为准。

【事实】官方文档（`daily`，https://tushare.pro/document/2?doc_id=27）口径：

- **未复权行情**，交易日 15:00–16:00 之间入库；停牌期间不提供数据。
- 最低 **120 积分**可免费试用，**每分钟 50 次**。
- 基础 **5000 积分**每分钟可调 **500 次**，**每次 6000 条**。
- 官方建议：全市场历史"循环日期"拉取，不要循环 `ts_code`。

【事实】本网关实测（2026-11-19 16:00–16:05 CST）：

| 项目 | 实测结果 |
| --- | --- |
| `stock_basic` 全量 5,580 行 | 成功，0.45 s |
| `daily` 单票 5 年 | 1,210 行，0.17–0.69 s |
| `daily` 单票 30 年 | **恰好 6,000 行**（2001-11-05 ~ 2026-11-18），确认 6,000 行截断 |
| `daily` 按交易日全市场 | 5,573 行，0.27–0.42 s |
| 连续 30 次 `daily` | 30/30 成功，0 报错，总 12.0 s |
| 延迟分布 | avg 0.400 s，p50 0.401 s，p95 0.470 s，max 0.504 s |
| 折算可持续频率 | 至少 **150 次/分钟**（本次仅压测 30 次，更高上限未验证） |
| `adj_factor` | 可用（返回 `ts_code, trade_date, adj_factor`） |
| `cyq_perf`（筹码） | 可用（返回 cost_5/15/50/85/95pct、weight_avg、winner_rate） |
| `pro_bar(adj=qfq/hfq)` | **不可用**，网关报"请指定正确的接口名" |

【事实】16:03 时点，`trade_cal` 显示 2026-11-19 是交易日，但 `daily` 最新只到 2026-11-18 —— 该网关存在约 1 个交易日的入库滞后。当天引擎若以 `as_of=今天` 运行，会因 `require_target_date=True` 拒绝本地缓存并走网络兜底（实测单票 1.65 s）。

【结论】权限足够，配额不构成阻塞；真正需要注意的是网关的第三方属性、6,000 行上限与 1 个交易日滞后。

### 11.3 全量补齐 vs 按需补齐

| 维度 | 方案 A：全量补齐（按交易日循环） | 方案 B：按需补齐（只补候选池） |
| --- | --- | --- |
| 调用量 | **720 次**（每日 1 次，每次约 5,573 行） | 每轮 1–24 次（近期 trace：发现 1–24、短名单 1–8、深研 1–4） |
| 纯网络耗时 | 约 4–6 分钟（实测 0.27–0.42 s/次） | 每轮 <10 秒 |
| 含写库总耗时 | 约 10–20 分钟（4.0M 行） | 秒级（每票 720 行） |
| 存储增量 | 约 0.9 GB | 每票约 165 KB（720 × 230 B）；按 30 票/日累积一年约 1.2 GB |
| 覆盖率达成 | 一次性 100%（5,580 只） | 仅覆盖"被选到过"的票；全市场覆盖无确定时间 |
| 首轮可用性 | 批次跑完即可用 | 候选首次出现时才有数据，存在首见延迟 |
| 失败影响 | 单日批次失败只缺 1 天，可补 | 单票失败只影响该票 |
| 主要风险 | 0.9 GB 存储、4M 行写入、一次性运维窗口 | 冷启动时选股链路被补齐延迟拖慢 |

【方案建议】**混合方案**：先用"按交易日循环"做一次性 720 日回补（720 次调用），之后每天增量 1 次；对回补窗口之后的新上市/新入池标的，再按 `ts_code` 单票补。理由：

1. 按日期拉取比按标的拉取少约 **7.75 倍**调用（720 vs 5,580），且符合官方建议。
2. 单日全市场 5,573 行 < 6,000 行上限，不需要分页。
3. 全量回补后，影子模式的候选票都已是缓存命中，不再引入首见延迟。
4. 增量维护成本是每天 1 次调用，可忽略。

不建议"纯按需"作为唯一方案：它无法给影子模式提供稳定的全市场覆盖率基线，也无法回答"引擎在真实候选池上的失败率"。

### 11.4 小样本真实拉取与引擎验证

【事实】从 Tushare 实拉 5 年日线，写入临时库副本（`/tmp/stage0_test.db`，不改仓库数据），再跑引擎：

| 代码 | 板块/类型 | 拉取行数 | 拉取耗时 | 引擎快照 | 引擎结果（as_of=2026-11-18） |
| --- | --- | ---: | ---: | --- | --- |
| 600519 | 沪主板-大市值 | 1,210 | 0.17 s | daily 720 / weekly 100 | `generated`（运行时路径，已有 11-18 规则快照） |
| 601398 | 沪主板-银行 | 1,210 | 0.38 s | daily 720 / weekly 100 | 纯评估 `MARKET_RULES_UNAVAILABLE` |
| 000001 | 深主板-银行 | 1,210 | 0.45 s | daily 720 / weekly 100 | 纯评估 `MARKET_RULES_UNAVAILABLE` |
| 002594 | 深主板-中盘 | 1,210 | 0.33 s | daily 720 / weekly 100 | 纯评估 `MARKET_RULES_UNAVAILABLE` |
| 300750 | 创业板-大市值 | 1,210 | 0.47 s | daily 720 / weekly 100 | 纯评估 `MARKET_RULES_UNAVAILABLE` |
| 301029 | 创业板-301 段 | 1,210 | 0.28 s | daily 720 / weekly 100 | 纯评估 `MARKET_RULES_UNAVAILABLE` |
| 688981 | 科创板-大市值 | 1,204 | 0.43 s | daily 720 / weekly 100 | 纯评估 `MARKET_RULES_UNAVAILABLE` |
| 920309 | 北交所-次新 | **2** | 0.30 s | daily 0 / weekly 0 | `INSUFFICIENT_DAILY_HISTORY`、`INSUFFICIENT_WEEKLY_HISTORY`、`MARKET_RULES_UNAVAILABLE`、`MISSING_CASH_PRICE`、`PRICE_SCALE_UNVERIFIED` |

【事实】字段足够：`daily` 返回 `ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount`。仓库现有 `_normalize_data` 已做单位换算（`vol × 100`、`amount × 1000`），写入 `stock_daily` 后 `price_basis=amount_volume_already_cash_scale`、`price_scale_verified=True`。

【事实】720 根写入后，**不再出现 `INSUFFICIENT_DAILY_HISTORY` / `INSUFFICIENT_WEEKLY_HISTORY`**；周线由日线聚合出 100 根，满足 60+40。

【事实】但出现了一个独立阻塞：`MARKET_RULES_UNAVAILABLE`。原因是 [plan.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/plan.py#L364-L376) 要求 `instrument_type_verified`、`st_status_verified`、`listing_status_verified` 全为真；而 [market_rules.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/market_rules.py#L121-L149) 的 `current_cn_common_stock_metadata` 只在"as_of == 当前有效交易日且命中本地股票索引"时返回已验证身份。历史日期（如 11-18 回放）拿不到身份，除非该票在该日已有持久化规则快照。实测只有 600519 在 11-18 有快照，因此它能 `generated`，其余 6 只全部 `MARKET_RULES_UNAVAILABLE`。

【事实】实时 `as_of=今天` 的路径是通的：实测 600519 以 11-19 为 as_of、本地数据只到 11-18 时，引擎走 Tushare 网络兜底，1.65 s 拿到 720 根，结果 `generated`，原因码 `AS_OF_ROLLED_BACK` + `NO_VALID_STOP`。

【事实】北交所当前无法出计划：本地 `static/stocks.index.json` 的 5,191 个 CN 代码里没有北交所，`current_cn_common_stock_metadata` 返回 `None`，就绪闸门 `market_rules` 检查失败，`generate_trade_plan` 直接返回 `system_unready`（实测 920309）。这不是 `INSUFFICIENT_DAILY_HISTORY`，而是身份元数据缺失。

【事实】单票开销（临时库、`persist=False`）：

| 阶段 | 耗时 | SQL 次数 |
| --- | ---: | ---: |
| 就绪闸门失败（`system_unready`） | 0.001 s | 2 |
| 快照构建（缓存命中，冷/热） | 0.44 s / 0.08 s | 1 |
| 纯评估 `evaluate_price_plan` | 0.004–0.043 s | 0 |
| 实时网络兜底（本地缺当日） | 1.65 s | 1 |
| `generate_trade_plan` 全链（成功票） | 0.38 s | 5 |

【结论】几十只候选的总开销主要取决于"数据是否已在本地"：缓存命中约 **0.1 s/只**（30 只 <5 s）；需要网络兜底时约 **1.6 s/只**（30 只约 50 s，且会放大第三方网关压力）。这就是"先全量回补再跑影子"的直接依据。

### 11.5 复权口径

【事实】引擎要求**现金流价格（cash / 未复权）**，不是前复权或后复权：

- `AnchorEvidence.price_basis` 是字面量 `Literal["cash"]`（[schemas.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/schemas.py#L151-L166)）。
- 快照字段名直接是 `last_cash_price`，缺失会触发 `MISSING_CASH_PRICE`（[plan.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/plan.py#L360-L361)）。
- 历史加载器明确把后复权的 Sequoia 数据"复原"为 cash，并注明"StockAnalyser charting and execution logic expects cash/未复权 prices"（[sequoia_price_rebaser.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/services/sequoia_price_rebaser.py#L79-L85)）。
- `_verify_price_scale` 用成交额/成交量 VWAP 校验价格量级，输出 `amount_volume_already_cash_scale` 等 basis（[snapshot.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/snapshot.py#L288-L300)）。

【事实】Tushare `daily` 正好是**未复权**行情（官方文档原文），与引擎口径一致。因此补齐时应写 `daily` 原始未复权价，**不要把 qfq/hfq 写入 `stock_daily`**。

【事实】本网关不支持 `pro_bar`，但 `adj_factor` 可用；如果未来需要"结构分析用前复权、执行价用 cash"，可以在读取时用 `adj_factor` 现算，而不改存储口径。

【方案建议】复权口径维持 **cash/未复权**，与引擎契约一致；不引入第二套复权价入库。

【需用户决策】这会带来一个已知代价：除权除息日前后，未复权序列有跳空，ATR 与结构位会被一次性缺口影响。如果要求"结构分析必须连续"，就需要改引擎（先在前复权序列上算位，再用 `adj_factor` 映射回 cash），属于引擎改造，不在本轮范围。**请确认：接受 cash 口径的除权跳空，还是要求引擎支持双口径？**

【事实】另一个相关缺口：`stock_daily` 表没有 `pre_close` 列（`PRAGMA table_info(stock_daily)` 实测），而引擎计算涨跌停用的是 `bars[-2].close` 作为昨收（[snapshot.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/pricing/snapshot.py#L104-L116)）。Tushare `daily` 返回的 `pre_close` 是**除权后的昨收**，与 `bars[-2].close` 在除权日不同。若严格按涨跌停判定，应在补齐时保留 `pre_close`（需要加列或旁路表）。

### 11.6 本轮新增的风险与缺口

1. 【事实】网关是第三方（`ts.gyzcloud.top`），配额与稳定性不受本项目控制；官方 120/5000 积分配额表不能直接套用。
2. 【事实】网关 `daily` 在 16:03 仍缺当日数据，存在约 1 个交易日滞后；与引擎 `require_target_date=True` 叠加会导致"本地有 720 根但仍走网络"。
3. 【事实】历史日期回放会大面积 `MARKET_RULES_UNAVAILABLE`，除非逐票持久化规则快照或改用实时 as_of。
4. 【事实】本地索引无北交所，北交所票在就绪闸门即失败（`system_unready`），与数据是否补齐无关。
5. 【事实】`cyq_perf` 可用，但字段与引擎 `ChipSnapshot` 不是一一对应（`cost_15pct/cost_85pct` ≈ 70% 区间、`cost_5pct/cost_95pct` ≈ 90% 区间、`weight_avg` ≈ `avg_cost`），属于近似映射，需要单独确认口径。

### 11.7 阶段 0 相关需用户决策

| 编号 | 决策点 | 选项 | 建议 |
| --- | --- | --- | --- |
| **E1** | 补齐策略 | ~~A 全量按交易日 + 每日增量；B 纯按需；C 先按需观察一个月再全量~~ | **已拍板（2026-11-19）：按交易日一次性回补 720 日，之后每日增量一次。** |
| **E2** | 复权口径 | A cash/未复权（与引擎一致）；B 结构用前复权 + 执行用 cash（需改引擎） | 建议 A，除非明确要求结构连续性 |
| **E3** | 是否保留 `pre_close` | A 给 `stock_daily` 加列；B 另建旁路表；C 不保留，接受除权日涨跌停误差 | 需用户拍；影响涨跌停判定精度 |
| **E4** | 影子模式 as_of | A 用"最后一个有数据的交易日"；B 用今天并允许 1 日滞后走网络 | 建议 A（避免 30 只 × 1.6 s 的网关压力） |
| **E5** | 影子模式候选范围 | A 候选发现全量；B 短名单；C 深研结果；D 持仓计划 | 需用户拍；建议 B+C（既可对账又不放大开销） |
| **E6** | 北交所 | A 候选阶段排除；B 补索引元数据后再支持；C 接受 `system_unready` | 需用户拍；当前不补索引则只能 A/C |

### 11.8 回补执行约定（2026-11-19 实际执行）

【事实】执行前的现状：

- `stock_daily` 有 `code, date, open, high, low, close, volume, amount, pct_chg, ma5, ma10, ma20, volume_ratio, data_source, created_at, updated_at`，**没有 `adj_factor` 列，也没有独立的复权因子表**。
- `stock_daily` 只有**非唯一**索引 `ix_code_date`，没有 `(code, date)` 唯一约束，因此无法使用数据库级 upsert。
- 回补前数据库约 143 MB，磁盘可用约 23 GB；已用 `sqlite3 .backup` 备份到 `data/backups/stock_analysis_pre_stage0_20261119.db`。

【事实】本轮回补采取的约定：

| 问题 | 做法 |
| --- | --- |
| 存储口径 | 只写 Tushare `daily` 的**未复权**原始行情；不写 qfq/hfq |
| 复权因子 | 不改 `stock_analysis.db` 表结构；单独写入 `data/adj_factors.db` 的 `stock_adj_factors(code, date, adj_factor, data_source, created_at)`，主键 `(code, date)` |
| 幂等 | 整日已存在 ≥ 4,000 行时**整日跳过**；否则在 `BEGIN IMMEDIATE` 内先一次性取该日已有 `code` 集合，过滤后 `executemany` 批量插入（不用数据库级 upsert，因为无 `(code, date)` 唯一约束）。重复执行不会产生重复行，实测重复 `(code, date)` 为 0 |
| 失败处理 | 单日失败写入 `data/backfill/stage0_backfill_log.jsonl` 并继续；连续 3 日失败、限频、字段缺失、单日 > 6,000 行或价格精度异常则立即中止 |
| 进度 | 每交易日一行 JSONL：日期、拉取行数、写入行数、跳过行数、耗时、复权因子行数、失败原因 |
| 窗口 | 最近 720 个交易日，截止网关已发布的最后一日（本次为 2026-11-18，窗口 2023-11-28 ~ 2026-11-18） |

### 11.9 实际回补结果（2026-11-19 完成）

【事实】执行结果（数据来自 `data/backfill/stage0_summary.json` 与回补后复核）：

| 指标 | 结果 |
| --- | --- |
| 交易日 | **720 天全部 `ok`，0 失败**；另有 266 天整日 `skipped_day`（含先前批次与 5 日试跑），重跑无重复写入 |
| 写入行数 | `stock_daily` 总计 **3,899,014 行**，其中 `data_source='tushare_stage0_backfill'` **3,887,960 行**，其余 11,054 行为回补前已有的其他数据源记录 |
| 标的覆盖 | **5,690 个代码**（窗口内） |
| 日期范围 | `2023-11-28` ~ `2026-11-18`；另有 7 行 `2023-11-17`~`2023-11-27` 的旧数据落在窗口外 |
| 重复 `(code, date)` | **0** |
| 价格精度 | 每日 `max_price_dev = 0.0`（原始报价即 2 位小数），无一日触发精度异常 |
| 单日耗时 | 0.27–4.30 s（首日含索引预热 4.3 s，其余多在 0.6–2.0 s） |

【事实】720 根达标情况（引擎能否出计划的关键指标）：

| 分档 | 数量 |
| --- | --- |
| `>= 720` 根 | **4,370** |
| `520–719` 根 | 946 |
| `< 520` 根 | 374 |
| 本地索引内 CN 代码（5,191）中 `>= 720` | **4,155** |

未达 720 根的主因是**停牌**与**窗口内新上市**，不是拉取失败：

- 窗口起点（2023-11-28）已存在的 5,310 只中，4,370 只 `>= 720`；缺口 `p50=0 / p90=5 / p95=10 / p99=452 / max=700`。
- 窗口起点之后新上市的 380 只全部 `< 720`（结构性不足，靠扩窗无法解决）。

【事实】扩窗收益（向前多补 N 个交易日，仅统计窗口起点已存在的 5,310 只）：

| 额外交易日 | `>= 720` 只数 |
| --- | --- |
| +5 | 4,873 |
| +10 | 5,146 |
| +20 | 5,184 |
| +30 | 5,190 |

【需用户决策】E1 原定 720 日已执行完毕；是否扩窗到 +10/+20/+30 属于新增决策，不擅自执行。建议至少 +10（覆盖 96.9% 的老票），但扩窗会再增加约 5.3 万行/10 日。

【事实】存储增量与完整性：

| 项目 | 数值 |
| --- | --- |
| `stock_analysis.db` 回补前（备份文件） | 143,380,480 B（约 137 MiB） |
| `stock_analysis.db` 回补后 | 1,114,472,448 B（约 1.04 GiB），净增约 **971 MB** |
| `adj_factors.db` | 405,557,248 B（约 387 MiB），**3,920,102 行 / 5,708 个代码 / 2023-11-28 ~ 2026-11-18** |
| 合计增量 | 约 **1.33 GB** |
| WAL | 回补期间曾涨至 21,237.6 MB；`wal_checkpoint(TRUNCATE)` 返回 `(0,0,0)` 后降到 0.3 MB，磁盘可用从 14 GiB 回到 35 GiB |
| 完整性 | 主库 `integrity_check = ok`，`adj_factors.db` 同样 `ok` |

【事实】两处需要如实记录的数据细节：

1. 回补脚本只按 `trade_cal` 的开市日写入；数据库窗口内另有 4 个非交易日（`2026-04-06`、`2026-05-01`、`2026-05-04`、`2026-05-05`）存在极少量旧数据，来自其他数据源，本次**未写入、未删除**。
2. `adj_factor` 只存不消费：当前选股链路与引擎都不会读取它，不改变任何现有行为；E2 若最终选择前复权口径，可直接用它计算。

【事实】回补脚本位于 `/tmp/stage0_backfill.py`、验收脚本位于 `/tmp/stage0_acceptance.py`，均为临时脚本，**未加入仓库**；回补未改动任何业务代码、未切换任何默认开关、未做 git 操作。

### 11.10 扩窗 20 个交易日（2026-11-19 执行）

【事实】按用户决策在 720 日基础上向前再补 20 个交易日（2023-10-31 ~ 2023-11-27），并顺带补入当日（2026-11-19）增量；窗口变为 **2023-10-31 ~ 2026-11-19，共 741 个交易日**。

| 指标 | 720 日窗口 | 扩窗后 |
| --- | --- | --- |
| `stock_daily` 总行数 | 3,899,014 | **4,010,655** |
| `>= 720` 根 | 4,370 | **5,188**（+818） |
| `520–719` 根 | 946 | **130** |
| `< 520` 根 | 374 | **372** |
| 代码数 | 5,690 | 5,690（无新增标的，扩窗只补历史行） |

【事实】剩余 502 只不足 720 根的归类（依据 Tushare `stock_basic` 的 `list_date` / `delist_date` 与交易日历）：

| 原因 | 数量 | 说明 |
| --- | --- | --- |
| 新上市 | 374 | 上市日晚于窗口起点，结构性不足，扩窗无法解决 |
| 停牌 | 21 | 上市早于窗口起点、仍在交易，但窗口内缺若干交易日 |
| 其他（退市/长期停止交易） | 107 | 窗口内已退市或长期停牌，最后一根日线早于窗口终点 |

【事实】存储：`stock_analysis.db` 1,062.8 MB → **1,085.5 MB**，`adj_factors.db` 387 MB → **398 MB**；磁盘可用约 33 GiB。WAL 当前 495 MB，`wal_checkpoint(TRUNCATE)` 返回 busy——正在运行的 `main.py --schedule`（PID 66205）持有读快照；未强行中断该进程。WAL 未超过 1 GB，无磁盘风险；停止调度进程后可再执行一次 checkpoint 回收。

---

## 12. 影子模式（已落地，只读不生效）

12.1–12.5 为设计稿；实际实现与差异见 12.6。

### 12.1 落点

在 [stock_selection.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L838-L845) 的 `recommendations = _recommendation_items(...)` **之后**、组装 `StockSelectionResult` 之前，增加一个独立函数：

```text
run_pricing_shadow_probe(context, codes, as_of) -> dict
```

- 输入：候选代码集合（E5 决定取发现/短名单/深研/持仓的并集）、`as_of = 最后一个有数据的交易日`。
- 逐票调用 `generate_trade_plan(symbol, as_of=as_of, persist=False)`，**不接** `enrich_trade_plan_result`，不写 `trade_plan_artifacts`。
- 输出：`pricing_shadow` 字典（见 12.2），只附加到 `StockSelectionResult` 的旁路字段/独立 trace 文件。
- 严格顺序：先完成 `recommendations` 与所有既有字段，再跑影子；影子函数**不得**接收或修改 `recommendations`、`positions`、`ctx.stages`。

### 12.2 结果存储与对照

【方案建议】不改 `final_report_json` 的既有键，也不进 Markdown。新增独立 trace 文件 `pricing_shadow.json`（与 `pricing_agent.json` 同级），结构：

```json
{
  "enabled": true,
  "as_of": "2026-11-18",
  "generated_at": "...",
  "coverage": {"total": 8, "generated": 3, "observation": 2, "no_plan": 3},
  "reason_histogram": {"NO_VALID_STOP": 2, "MARKET_RULES_UNAVAILABLE": 3},
  "symbols": [
    {
      "code": "600519",
      "engine": {"generation_status": "generated", "plan_id": "...", "scenarios": [...]},
      "llm": {"entry_zone": "...", "stop_loss": "...", "execution_mode": "conditional_open"},
      "diff": {"entry_zone_overlap": false, "stop_distance_pct_engine": 2.1, "stop_distance_pct_llm": 3.4}
    }
  ]
}
```

对照只做**离线口径比较**：入场区间是否重叠、止损距离差、目标位差、引擎 `generation_status` 与 LLM `execution_mode` 是否矛盾。不把任何结论写回报告。

### 12.3 开关

- 新增 `AGENT_PRICING_SHADOW_ENABLED`，**默认 False**。
- `AGENT_PRICING_SHADOW_MAX_SYMBOLS`（默认 30）防止候选异常放大开销。
- 复用现有 `AGENT_DETERMINISTIC_PRICE_PLAN_ENABLED` 仅用于引擎本体；影子开关独立，关闭时零调用、零 IO。
- 不改变任何现有默认值。

### 12.4 "只读不生效"的测试保证

1. **前后快照测试**：同一输入跑两遍（开关 off / on），断言 `final_report_json`、`final_markdown`、`appendix_markdown` 以及 `_recommendation_items` 的输出**逐字节相同**。
2. **字段级断言**：对每个推荐项断言 `action`、`action_strength`、`execution_mode`、`entry_condition`、`stop_loss_condition` 与开关 off 时完全一致。
3. **引擎失败注入**：monkeypatch `generate_trade_plan` 返回 `system_unready` / `no_plan`，断言整轮选股仍成功、报告不变、仅 `pricing_shadow.json` 记录失败。
4. **异常隔离**：让影子函数抛异常，断言主流程不受影响（捕获、记录、继续）。
5. **写入面断言**：用临时库 + SQL 事件计数，断言影子模式下的 SQL 只读（无 `INSERT/UPDATE/DELETE`）。
6. **黄金快照**：选一条历史 trace 重放，除 `pricing_shadow.json` 外所有产物哈希一致。

### 12.5 运行时机

【方案建议】影子任务放在选股主流程结束之后异步执行，或独立脚本按日批量执行；`as_of` 取"最后一个有数据的交易日"（E4=A），避免每票触发网络兜底。若坚持 `as_of=今天`，则需先确认网关当日数据已入库，否则 30 只候选会带来约 50 s 的额外串行网络开销。

### 12.6 实际实现（2026-11-19）

【事实】代码落点：

| 文件 | 内容 |
| --- | --- |
| `src/agent/pricing/shadow.py` | `run_pricing_shadow_probe()` / `build_qfq_snapshot()` / `classify_market()`，只读探针本体 |
| [stock_selection.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/agent/stock_selection.py#L256-L300) | `_pricing_shadow_codes()` 与 `_run_pricing_shadow_if_enabled()`；调用点在 `_apply_trade_plan_validation(ctx)` 之后、`result.success = True` 之前 |
| [config.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/src/config.py) | `agent_pricing_shadow_enabled` / `_path` / `_max_codes` |
| [tests/test_pricing_shadow.py](file:///Users/mengfanjun/Desktop/mfj/StockAnalyser/tests/test_pricing_shadow.py) | 6 条守卫测试 |

【事实】与设计稿的差异：

1. **候选范围**：设计稿写"发现/短名单/深研/持仓的并集"，实际取 **`candidate_screening.shortlist` ∪ `single_stock_deep_dive` 目标 ∪ `portfolio_allocation.positions_plan`**，去重后按 `AGENT_PRICING_SHADOW_MAX_CODES`（默认 60）截断。理由：发现阶段的原始池可达数百只，逐只建快照会把影子成本放大 10 倍以上；短名单+深研+持仓正好覆盖"会被 LLM 定价或会进报告"的全部标的，且规模在几十只。
2. **`as_of`**：实际取 `resolve_completed_cn_as_of(date.today())`（今天收盘后即今天）。原因：`MARKET_RULES_UNAVAILABLE` 的元数据校验只对"当前有效交易日"生效，用历史 `as_of` 会让所有票直接 `no_plan`，影子数据失去意义。
3. **不触网**：快照由 `PricePlanSnapshotBuilder(fallback_to_network=False, chip_loader=None)` 构建，并直接传给 `generate_trade_plan(persist=False)`，因此不会触发 Sequoia/Tushare 兜底，也不会写 `trade_plan_artifacts`。
4. **文件名**：默认 `data/pricing_shadow.json`（单文件覆盖），可用 `AGENT_PRICING_SHADOW_PATH` 改路径。
5. **前复权口径**：`qfq(t) = cash(t) × adj_factor(t) / adj_factor(as_of)`，`volume_qfq(t) = volume_cash(t) × adj_factor(as_of) / adj_factor(t)`，数据来自 `data/adj_factors.db`；未引入第三方复权库。市场规则仍沿用 cash 版本，仅用于分析对照。
6. **状态口径**：`generated / observation / no_plan / system_unready / unsupported / error`；`unsupported` 覆盖港股、美股和不在引擎符号白名单内的代码，`system_unready` 覆盖北交所等元数据无法验证的标的。

【事实】落盘结构（截取）：

```json
{
  "schema_version": "pricing-shadow.v1",
  "as_of": "2026-11-19",
  "summary": {"by_status": {"observation": 4, "generated": 1, "system_unready": 1, "unsupported": 2},
              "by_reason_code": {"NO_VALID_STOP": 3, "WEEKLY_TREND_OPPOSING": 4, "...": 0}},
  "items": [{
    "code": "600715", "market": "cn", "status": "generated",
    "reason_codes": ["INSUFFICIENT_SOURCE_FAMILIES", "NO_VALID_STOP"],
    "cash": {"scenarios": [{"entry_low": "1.94", "entry_high": "2.04", "stop_loss": "1.58",
                            "target_1": "2.71", "reference_net_rr_1": "1.428...", "role": "primary"}]},
    "qfq": {"scenarios": [...]},
    "qfq_status": {"status": "ok", "adj_factor_as_of": "4.4932", "max_abs_price_deviation_pct": 13.47},
    "comparison": {"atr_14": {"cash": "0.3243", "qfq": "0.3243", "delta_pct": 0.0},
                   "scenarios": [{"entry_low": {"cash": "1.94", "qfq": "1.94"}, "stop_loss": {"cash": "1.58", "qfq": "1.58"}}]},
    "elapsed_ms": 340.28
  }]
}
```

【事实】实测（真实 trace `20261116-173331` 的 5 只候选 + 北交所/港股/美股各 1 只）：

| 代码 | 状态 | 日线根数 | 耗时 |
| --- | --- | --- | --- |
| 000062 | observation | 720 | 758 ms（含冷启动） |
| 002035 | observation | 720 | 367 ms |
| 300820 | observation | 720 | 367 ms |
| 600715 | generated | 720 | 340 ms |
| 601872 | observation | 720 | 364 ms |
| 830799（北交所） | system_unready | 0 | 4 ms |
| hk00700 / AAPL | unsupported | — | 0 ms |

- 纯计算 `evaluate_price_plan` 实测约 **30 ms/只**；端到端 340–760 ms/只，差额来自 720 根日线的 DB 读取与结构分析（约 8–10 次 DB 往返）。
- 8 只合计 3.03 s；按 60 只上限估算约 **25–35 s**，且仅在开关开启时发生。
- 前复权与未复权的价格最大偏差 2.3%–13.5%，说明双口径换算生效；本次 5 只的 ATR 差为 0，是因为最近 14 根 K 线的复权因子未变化（结论正确，不是未生效）。

【事实】守卫测试 6/6 通过：关闭时零构造成本、港美股如实记录、单只失败不中断、前复权换算公式、开关开启前后 `SelectionRunContext` 与 `_build_final_report_json` 完全一致、探针异常被吞掉不影响主流程。测试命令：`.venv/bin/python -m pytest tests/test_pricing_shadow.py -q`。
