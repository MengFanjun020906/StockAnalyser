# 单股信号生命周期与持仓监控设计

> 状态：已实现（Phase 1-4，影子评估模式，2026-08-11）  
> 适用范围：单股询问、账户持仓监控  
> 不适用范围：选股候选池自动建信号

## 1. 背景

当前 Agent 可以根据技术面、消息面、资金面和基本面生成买点与交易建议，但一次性报告还不能解决两个实际问题：

1. AI 给出的理想买点经常在有效期内无法触达，建议看起来安全但不能执行。
2. 同一只股票第一天和第二天的行情与证据不同，重新分析后买点和结论可能变化。如果覆盖旧结论，将无法判断原信号何时失效，也无法进行真实回测。

本设计将“一次分析结论”升级为可持续跟踪的交易信号，但严格限制创建范围，避免选股候选批量进入长期状态系统。

## 2. 已冻结的产品边界

| 场景 | 领域对象 | 是否维护生命周期 | 维护方式 |
| --- | --- | --- | --- |
| 选股 | `CandidateObservation` | 否 | 仅保留在本次选股 Trace 中 |
| 单股询问 | `EntrySignal` | 是 | 查询前补算，分析后创建或修订 |
| 用户持仓 | `PositionWatch` | 是 | 每个交易日定时检查，异常时深度分析 |

核心约束：

- 选股结果不是正式交易信号，不写入活跃信号表。
- 用户发起单股分析后，只要最终证据形成机器可读的完整价格计划，就允许创建 `EntrySignal`；普通 QA 不要求当前结论为买入。
- 已持仓股票默认进入 `PositionWatch`，不重复创建普通入场信号。
- 用户明确询问“是否加仓”时，可以创建 `signal_type=add_position` 的独立 `EntrySignal`。
- 信号可以不每天生成，但已有信号必须能够在下次访问时补算真实状态。
- 上线时不从旧 Trace 回填信号；只有上线后的合格单股询问和首次持仓定时扫描会创建记录。

### 2.1 第一版实现入口

| 能力 | 实现 |
| --- | --- |
| 状态快照与事件流水 | `entry_signals`、`signal_events`、`position_watches`、`position_watch_events` |
| 调度状态 | `signal_scheduler_states`，按 CN/HK/US 保存心跳、最近交易日和下次运行时间 |
| 通知投递 | `signal_notification_outbox`，状态变化通知的事务内落库、幂等投递和失败重试 |
| 单股信号创建 | 普通 Chat 与 Trace 成功返回后统一调用 `SignalLifecycleService` |
| 持仓监控 | 仅注册到独立的 `python main.py --schedule` 进程 |
| 查询 API | `GET /api/v1/signal-preview` 及 entry/position 详情接口 |
| Web 页面 | `/signal-preview`；旧 `/agent-verdict-reviews` 地址重定向到新页面 |

`POSITION_WATCH_ENABLED=true` 控制持仓扫描是否注册。A 股、港股在各自市场时区 18:00 检查，美股在纽约时区 16:30 检查，因此夏令时自动生效。后台每 5 分钟只检查是否到达运行窗口，同一 `market + trading_date` 只成功执行一次。

## 3. 三类对象

### 3.1 CandidateObservation

`CandidateObservation` 是一次选股运行中的候选快照，不具备长期有效性。

```json
{
  "run_id": "selection-001",
  "stock_code": "600519",
  "as_of": "2026-08-10T15:00:00+08:00",
  "candidate_score": 82,
  "recommendation": "继续观察",
  "source_trace_id": "trace-selection-001"
}
```

它只回答“为什么进入本次候选池”，不能表达用户已经准备交易，也不能参与后续持仓告警。

### 3.2 EntrySignal

`EntrySignal` 表示一次可以被验证的单股入场计划。它必须在创建时冻结有效期、触发条件和失效条件。

建议仅在同时满足以下条件时创建：

- 当前任务最终只解析出一只股票，不是选股或多标的比较。
- 最终输出包含至少一个可验证的入场/观察区间，以及止损价或失效条件。
- 价格来自本轮结构化证据和 `TradePlan`，不从 Markdown 报告反向解析。
- 没有区间的普通 QA、纯资料查询和选股不创建记录。

建议字段：

```json
{
  "signal_id": "600519-trend_pullback-20260810-001",
  "signal_type": "entry",
  "account_id": null,
  "stock_code": "600519",
  "market": "cn",
  "setup_type": "agent_entry",
  "direction": "long",
  "status": "active",
  "revision": 1,
  "created_as_of": "2026-08-10T15:00:00+08:00",
  "last_checked_as_of": "2026-08-10T15:00:00+08:00",
  "valid_trading_days": 5,
  "valid_until": "2026-08-17",
  "entry_zone": [1450.0, 1470.0],
  "trigger_rule": {
    "type": "price_entered_zone",
    "zones": [
      {"label": "ideal", "low": 1450.0, "high": 1470.0},
      {"label": "secondary", "low": 1490.0, "high": 1500.0}
    ]
  },
  "invalidation_rules": [
    "daily_close_below_1420",
    "trend_structure_bearish",
    "material_negative_event"
  ],
  "evidence_quality": "ok",
  "source_trace_id": "trace-single-001",
  "version": 1,
  "metadata": {
    "current_action": "wait",
    "trigger_action": "open",
    "plan_mode": "conditional_entry",
    "execution_eligible": true,
    "policy_version": "signal-lifecycle.v2",
    "structure_version": "chan-structure.v2",
    "evidence_fingerprint": "sha256:..."
  }
}
```

`EntrySignal` 是账户无关的入场计划，`account_id` 固定为空；它只在单股询问链路创建，不代表真实成交，也不会自动关联某个持仓。持仓数量和成本由独立的 `PositionWatch` 维护。没有新增证据时，价格区间、止损和有效期不能静默漂移；若新证据改变交易形态，则旧信号进入 `superseded`，新信号使用新的 `signal_id`。

`current_action` 表示分析当下的动作，`trigger_action` 表示价格进入计划区后交给 Risk Gate 复核的动作。典型的严格结论是 `current_action=wait`、`trigger_action=open`：当前不追价，但保留一个可以用未来 K 线验证的条件计划。`current_action=reject` 表示当前明确不入场，但报告仍给出了值得验证的观察区间；它以 `plan_mode=observation_only` 保存且 `execution_eligible=false`。Risk Gate 阻断的计划使用 `plan_mode=risk_blocked`，同样只用于观察和审计。

`entry_low` / `entry_high` 保留第一个主区间以兼容现有查询；全部不连续区间写入 `trigger_rule.zones`。例如 `19.6–20.5` 与 `21.2–22.0` 必须保存为两个区间，不能合并为 `19.6–22.0`。

### 3.3 PositionWatch

`PositionWatch` 表示用户账户中一段连续持仓周期的监控状态，不应每天创建一条新记录。

唯一键建议使用：

```text
account_id + stock_code + position_cycle_id
```

建议字段：

```json
{
  "watch_id": "account-1-600519-cycle-3",
  "account_id": 1,
  "stock_code": "600519",
  "position_cycle_id": 3,
  "status": "active",
  "quantity": 200,
  "average_cost": 1460.0,
  "opened_at": "2026-08-01",
  "last_checked_as_of": "2026-08-10T15:30:00+08:00",
  "risk_level": "low",
  "current_action": "hold",
  "version": 7
}
```

持仓数量归零后关闭当前 `PositionWatch`。未来再次买入同一股票时创建新的 `position_cycle_id`，不能复用上一段持仓状态。

## 4. EntrySignal 状态机

```text
proposed
   |
   v
active
   +--> price_triggered --> filled --> 创建或关联 PositionWatch
   +--> expired
   +--> invalidated
   +--> superseded
   +--> cancelled
   +--> needs_review --> active / invalidated
   +--> ambiguous --> 人工确认或分钟线补证
```

状态语义：

| 状态 | 是否终态 | 含义 |
| --- | --- | --- |
| `proposed` | 否 | 仅观察或被 Risk Gate 阻断的计划，记录区间但不具备执行资格 |
| `active` | 否 | 信号在有效期内，等待触发或失效 |
| `price_triggered` | 否 | 市场价格进入条件区间，但不代表真实订单成交 |
| `filled` | 是 | 已通过券商订单或账户持仓变化确认成交 |
| `expired` | 是 | 有效期结束且没有触发 |
| `invalidated` | 是 | 命中创建时冻结的失效条件 |
| `superseded` | 是 | 新交易形态或新信号替代当前信号 |
| `cancelled` | 是 | 用户主动取消跟踪 |
| `needs_review` | 否 | 数据过期、缺失或冲突，暂时不能可靠判断 |
| `ambiguous` | 否 | 日线内同时触发买点和失效条件，无法判断发生顺序 |

系统未接入真实订单时，只能确认 `price_triggered`，不得把“最低价进入买点区间”写成 `filled`。

## 5. 什么时候过期或作废

### 5.1 正常过期

- 普通单股入场信号默认有效 5 个交易日。
- 中线回调类信号可以配置为 10 个交易日。
- 使用标的所属市场的交易日历计算，不按自然日计算。
- 股票停牌且没有可交易 K 线时，不消耗有效交易日。
- 即使没有定时任务，读取信号时也必须根据 `valid_until` 计算 `effective_status=expired`。

### 5.2 硬失效

以下条件命中后进入 `invalidated`：

- 价格满足创建时冻结的失效条件，例如“日线收盘跌破 1420”。
- 原交易结构被确定性证据破坏，例如多头结构变为空头结构。
- 出现有明确来源的重大监管、财务或公司治理风险。
- Risk Gate 发现继续执行将违反账户、交易制度或仓位约束。

“盘中触及”和“日线收盘跌破”是不同语义，必须在创建信号时确定，后续不能临时替换。

### 5.3 不应直接作废的情况

- 某个数据源超时或缺失：进入 `needs_review`。
- 新闻或资金数据过期：降低质量并等待刷新。
- 不同来源数值冲突：保留冲突并暂停升级结论。
- 公司除权除息导致价格机械变化：先按复权因子调整信号价格或进入人工复核，不能误判为触发或止损。

## 6. 单股询问时如何维护

单股分析入口需要在正式分析前后各执行一次生命周期操作。

```text
用户询问单股
    |
    v
查找该股票的 active / needs_review 信号
    |
    v
从 last_checked_as_of 补算到当前 as_of
    |
    v
运行本次四维分析与 Risk Gate
    |
    v
维持 / 修订 / 作废旧信号，或创建新信号
    |
    v
生成报告与 signal_lifecycle Trace artifact
```

### 6.1 分析结果到信号预览的闭环

自然语言报告只负责向用户解释，不再从报告文本反向解析价格。买入/加仓询问以及普通单股 QA 都会进入机器可读计划候选流程，但只有形成完整区间时才产生 `TradePlan`：

```text
用户发起单股分析
    -> 查询前补算旧信号
    -> Agent 完成证据收集
    -> 自然语言回答 + submit_trade_plan
    -> 校验 TradePlan 的股票、动作、入场区间和风险条件
    -> 同一份 TradePlan 进入 Risk Gate
    -> SignalLifecycleService 幂等创建或修订 EntrySignal
    -> /api/v1/signal-preview 读取并画到 K 线上
```

- 普通/流式 Chat 使用 `submit_trade_plan` 工具提交计划，工具只记录提案，不下单，也不改变原有自然语言回答。
- Trace 优先使用同一份机器可读计划；多 Agent 链路只在最终 Decision Agent 阶段开放 `submit_trade_plan`，由已经汇总的四维意见生成旁路计划。
- `TradePlan.action=open` 必须对应普通入场询问，`TradePlan.action=add` 必须对应明确加仓询问；它们表示条件满足后的触发动作。
- 当前结论可以是 `current_action=wait`。只要同时存在可验证的价格区间和风险条件，仍创建条件计划，并在页面标记为“等待触发”。
- 当前结论可以是 `current_action=reject`。有完整区间时创建“仅观察”记录；没有区间、减仓、卖出、纯资料查询和选股不会创建记录。
- Risk Gate 阻断的完整计划创建“风险门阻断”记录，并冻结 `execution_eligible=false`，不能被下游当成买入动作。
- 理想区、次优区等不连续区间分别写入 `entry_zones` / `trigger_rule.zones`，触达判断逐区间执行。
- 普通 Chat 没有预取评分时，只读本地 K 线构建 Strategy4/缠论快照；数据不足时信号进入 `needs_review`，不会升级成强结论。
- 列表和详情都按查询时点计算 `effective_status`，过期判断只读展示，不在 GET 请求中改写数据库。
- 信号预览按 `market + stock_code` 合并同一分区中的历史记录，仓库按更新时间倒序后只展示最新一条；股票名称优先使用已存名称，缺失时从本地股票索引补齐，代码作为次级标识保留。
- 图表从 `signal_created` 和 `signal_revised` 事件恢复每一版价格区间；同一版本的理想区和次优区分别绘制，旧版本画到下一次修订日，当前版本画到有效期或最新可见 K 线。

更新规则：

- 证据没有实质变化：保持原买点、止损和有效期，只更新触达概率与检查时间。
- 新证据改变强弱判断：增加 `revision` 并记录 `change_reason`。
- 原信号失效：写入终态事件，不能覆盖或删除原记录。
- 回调交易变成突破交易：创建新的 `signal_id`，通过 `supersedes_signal_id` 关联旧信号。
- 已有持仓：优先读取 `PositionWatch`；只有明确的加仓意图才创建 `signal_type=add_position` 的 `EntrySignal`。

禁止因为价格上涨就不断上移原始买点。没有新增证据却修改价格条件，应计入“目标漂移”Badcase。

## 7. 中断后的历史补算

系统可能几天没有运行。下次检查时需要按时间顺序重放 `last_checked_as_of` 之后的行情：

1. 读取缺失交易日的日线或分钟线。
2. 按交易日顺序检查有效期、价格触发和失效条件。
3. 找到第一个终止事件后停止继续推演。
4. 记录事件真实发生日期，而不是本次检查日期。
5. 如果同一根日 K 同时触及买点和止损，优先读取分钟线判断先后顺序。
6. A 股冲突日会先尝试补取当日 5 分钟线；港股、美股只使用本地已有分钟线。仍没有分钟线时进入 `ambiguous`，不得选择对回测更有利的顺序。

补算过程必须使用当时可获得的数据和固定 `as_of`，禁止使用后来公告或未来 K 线解释过去的决策。

## 8. 持仓每日监控

### 8.1 创建与关闭

- 账户同步发现数量大于 0 的新持仓时，创建或恢复 `PositionWatch`。
- 数量、成本或账户发生变化时增加版本并记录事件。
- 数量归零时将当前持仓周期标记为 `closed`。
- 同一股票跨账户分别维护，不能合并成本和仓位。
- A 股持仓检查必须保留 T+1 可卖数量和当日买入数量。

### 8.2 两层执行策略

为控制模型成本，每日任务分为两层：

```text
L0：所有持仓的确定性轻量扫描
价格 / 止损距离 / 趋势变化 / 量价异常 / 重大事件 / 数据新鲜度
                         |
                         v
L1：只有异常或临近触发条件的持仓运行完整四维 Agent
技术 / 消息 / 资金 / 基本面 / Risk Gate
```

L0 可在每个市场收盘且数据源完成更新后运行。没有异常时只追加 `position_checked` 事件，不生成完整报告，也不调用 LLM。

触发 L1 的建议条件：

- 接近或跌破止损、支撑和风险阈值。
- 趋势状态发生变化。
- 资金流出现显著反转或异常。
- 出现重大公告、监管或财务事件。
- 持仓数量、成本或账户约束发生变化。
- 数据连续缺失，需要重新取证。
- 用户主动发起持仓复查。

### 8.3 通知去重

以下情况才向用户发送通知：

- 动作从 `hold` 变为 `reduce`、`exit` 或 `add`。
- 风险等级发生实质变化。
- 止损、退出或重大事件条件被触发。
- 持仓数量或成本发生变化。
- 数据连续缺失并进入 `needs_review`。

相同状态的每日检查只写审计事件，不重复推送“继续持有”。状态变化先和 `PositionWatch` 事件在同一事务写入 `signal_notification_outbox`，后台投递器再领取 `pending/failed` 记录发送；幂等键使用 `watch_id + state_version + action`，失败按指数退避重试，避免任务重跑重复推送。

## 9. 持久化与并发

### 9.1 存储模型

生命周期数据采用“当前状态快照 + 追加式事件流水”，而不是把完整对象覆盖写入一张 JSON 表：

```text
entry_signals            当前 EntrySignal 快照
signal_events            EntrySignal 追加式事件
position_watches         当前 PositionWatch 快照
position_watch_events    PositionWatch 追加式事件
signal_scheduler_states  各市场定时任务的心跳和最近运行状态
signal_notification_outbox 状态变化通知的待投递队列和重试记录
```

快照表用于回答“现在是什么状态”，事件表用于回答“何时、因为什么变成这个状态”。一次状态转换必须在同一数据库事务中同时写入事件和更新快照。

`CandidateObservation` 不写入上述六张表，继续保存在选股 Trace 中。只有用户进入单股询问或明确加入跟踪后，才允许创建 `EntrySignal`。

### 9.2 entry_signals

`entry_signals` 保存单股入场信号和加仓信号的当前状态。建议字段如下：

| 字段 | 类型建议 | 约束与用途 |
| --- | --- | --- |
| `id` | Integer | 数据库内部主键 |
| `signal_id` | String(128) | 稳定业务 ID，唯一且创建后不变 |
| `owner_id` | String(64)，可空 | 信号所属用户；本地单用户可空，多用户部署必须填写 |
| `account_id` | Integer，可空 | 第一版保持为空；EntrySignal 不绑定账户，持仓由独立 `PositionWatch` 维护 |
| `stock_code` | String(16) | 规范化股票代码 |
| `market` | String(8) | `cn` / `hk` / `us` |
| `signal_type` | String(24) | `entry` / `add_position` |
| `setup_type` | String(32) | `trend_pullback`、`breakout`、`chan_first_buy` 等 |
| `direction` | String(8) | 第一版固定为 `long`，字段保留扩展能力 |
| `status` | String(24) | 当前生命周期状态 |
| `revision` | Integer | 分析结论的业务修订次数 |
| `entry_low` / `entry_high` | Numeric | 第一个主区间，保留用于兼容查询和排序 |
| `stop_price` | Numeric，可空 | 冻结的止损价 |
| `trigger_rule_json` | Text | 触发规则及全部不连续 `zones` 的结构化 JSON |
| `invalidation_rules_json` | Text | 失效规则列表的结构化 JSON |
| `created_as_of` | DateTime | 创建信号时允许使用的数据截止点 |
| `last_checked_as_of` | DateTime | 生命周期已经补算到的数据截止点 |
| `valid_trading_days` | Integer | 有效交易日数量 |
| `valid_until` | Date | 按所属市场交易日历计算出的到期日 |
| `evidence_quality` | String(24) | `ok` / `partial` / `missing` / `conflicted` |
| `source_trace_id` | String(128) | 创建该信号的 Trace |
| `supersedes_signal_id` | String(128)，可空 | 新信号替代的旧信号 |
| `linked_watch_id` | String(128)，可空 | 预留字段；未接入券商成交前不自动填写 |
| `version` | Integer | 并发更新版本号，从 1 开始 |
| `created_at` / `updated_at` | DateTime | 数据库记录时间 |

建议索引：

- `UNIQUE(signal_id)`。
- `(owner_id, stock_code, market, status)`：查询某用户某股票的活跃信号。
- `(status, valid_until)`：扫描应过期的信号。
- `(account_id, stock_code, status)`：查询账户相关的加仓信号。
- `(source_trace_id)`：从 Trace 反查生命周期对象。

不要强制同一用户、同一股票只能存在一条记录，因为不同 `setup_type` 可以先后产生信号。是否允许同时存在多条活跃信号，由 Repository 在事务内根据产品规则判断。

### 9.3 signal_events

`signal_events` 是不可覆盖的审计流水。每次创建、检查、修订和终止都追加一条事件：

| 字段 | 类型建议 | 约束与用途 |
| --- | --- | --- |
| `id` | Integer | 数据库内部主键 |
| `event_id` | String(128) | 事件业务 ID，唯一 |
| `signal_id` | String(128) | 关联 `entry_signals.signal_id` |
| `event_type` | String(32) | `signal_created`、`signal_checked` 等 |
| `from_status` / `to_status` | String(24)，可空 | 状态转换前后值；纯检查事件可以相同 |
| `occurred_at` | DateTime | 市场事件真实发生时间 |
| `as_of` | DateTime | 判定本事件时使用的数据截止点 |
| `source` | String(32) | `single_stock_query`、`lazy_reconcile`、`broker_sync` 等 |
| `source_trace_id` | String(128)，可空 | 产生本事件的 Trace |
| `dedupe_key` | String(192) | 幂等键，唯一 |
| `payload_json` | Text | 触发价、变更原因、证据摘要和缺失项等可变详情 |
| `created_at` | DateTime | 事件写入数据库的时间 |

建议索引为 `(signal_id, occurred_at, id)`，确保可以稳定按时间回放。同一个 `dedupe_key` 只能写入一次，例如：

```text
sig-600519-001:entry_price_triggered:2026-08-12
```

示例事件：

```json
{
  "event_id": "evt-001",
  "signal_id": "sig-600519-20260810-001",
  "event_type": "entry_price_triggered",
  "from_status": "active",
  "to_status": "price_triggered",
  "occurred_at": "2026-08-12T10:15:00+08:00",
  "as_of": "2026-08-12T10:15:00+08:00",
  "source": "lazy_reconcile",
  "source_trace_id": "trace-single-002",
  "dedupe_key": "sig-600519-20260810-001:entry_price_triggered:2026-08-12",
  "payload": {
    "trigger_price": 1458.0,
    "entry_zone": [1450.0, 1470.0]
  }
}
```

### 9.4 position_watches

`position_watches` 保存一段连续持仓周期的当前监控状态：

| 字段 | 类型建议 | 约束与用途 |
| --- | --- | --- |
| `id` | Integer | 数据库内部主键 |
| `watch_id` | String(128) | 稳定业务 ID，唯一 |
| `account_id` | Integer | 关联 `portfolio_accounts.id`，不可空 |
| `stock_code` / `market` | String | 持仓标识 |
| `position_cycle_id` | Integer | 同一账户再次持有该股票时递增 |
| `status` | String(24) | `active` / `closed` / `needs_review` |
| `quantity` | Numeric | 最近一次观察到的持仓数量 |
| `average_cost` | Numeric | 最近一次观察到的平均成本 |
| `opened_at` | DateTime | 本次持仓周期开始时间 |
| `last_checked_as_of` | DateTime | 已完成监控的数据截止点 |
| `source_position_updated_at` | DateTime，可空 | 对应持仓快照的更新时间 |
| `risk_level` | String(16) | 当前风险等级 |
| `current_action` | String(16) | `hold` / `add` / `reduce` / `exit` |
| `version` | Integer | 并发更新版本号 |
| `created_at` / `updated_at` | DateTime | 数据库记录时间 |

唯一约束使用 `(account_id, stock_code, market, position_cycle_id)`；常用索引使用 `(account_id, status)` 和 `(stock_code, market, status)`。

现有 `portfolio_trades` 是成交事实来源，`portfolio_positions` 是交易重放后的最新持仓快照。`PositionWatch` 只记录“系统最近看到了什么以及作出了什么监控判断”，不能成为持仓数量和成本的事实来源。

因此 `position_watches` 只外键关联稳定的 `account_id`，通过 `account_id + stock_code + market` 读取当前 `portfolio_positions`，不直接外键关联可能被重放替换的持仓快照行。数量和成本保留在监控快照中仅用于审计当时的判断。

### 9.5 position_watch_events

`position_watch_events` 与 `signal_events` 结构相似，至少包含：

```text
event_id
watch_id
event_type
occurred_at
as_of
source
source_trace_id
dedupe_key
payload_json
created_at
```

`payload_json` 可以记录检查前后的数量、成本、风险等级、动作、L0 命中规则、是否升级 L1、通知结果和数据缺失项。建议索引为 `(watch_id, occurred_at, id)`，并对 `dedupe_key` 建唯一约束。

事件类型至少包括：

```text
signal_created
signal_checked
signal_revised
entry_price_triggered
signal_expired
signal_invalidated
signal_superseded
position_opened
position_changed
position_checked
position_risk_changed
position_closed
```

### 9.6 列与 JSON 的边界

以下需要过滤、排序、关联或建索引的字段必须单独建列：

- 用户、账户、股票代码和市场。
- `status`、`revision`、`version`。
- 入场区间、止损价、有效期和 `as_of`。
- 事件类型、发生时间、Trace ID 和幂等键。

只有形状可能继续演进、且不参与核心查询的内容存 JSON，例如规则表达式、证据摘要、变更原因、缺失字段和事件诊断。Repository 负责统一 JSON 编解码，业务层不直接操作数据库中的 JSON 字符串。

第一版为兼容现有持仓模型沿用 `Float`；API 展示统一格式化，状态判断只使用明确的价格区间和日线收盘条件。若后续接入真实订单和资金结算，再迁移到 `Numeric/Decimal`。

### 9.7 原子写入、幂等与并发

一次状态转换采用以下事务：

```text
BEGIN IMMEDIATE
    1. 查询 dedupe_key；已经存在则直接返回原结果
    2. 读取当前快照，并校验调用方期望的 version
    3. 使用纯状态机校验 from_status -> to_status 是否合法
    4. 插入追加式事件
    5. 更新当前快照和 last_checked_as_of；仅交易计划变化时增加 revision
    6. version = version + 1
COMMIT
```

约束：

- 当前快照可以更新，事件历史只能追加，不能覆盖。
- 每个事件保存 `occurred_at`、`as_of`、`source`、`source_trace_id` 和 `dedupe_key`。
- 用户查询和定时任务可能同时更新同一对象，写入时使用 `version` 做乐观锁或 CAS。
- 相同 `dedupe_key` 的事件必须幂等，避免任务重试产生重复状态转换和通知。
- 事件插入和快照更新必须共用一个事务，不能分别提交。
- `version` 不一致时重新读取并重算，不允许最后写入者静默覆盖前一个结果。
- SQLite 写入延续现有 `BEGIN IMMEDIATE` 和有限重试机制；未来切换其他数据库时保留版本校验语义。

`session_id` 只标识一次会话，不能作为跨会话生命周期的所有者。进入多用户模式前必须由认证层稳定提供 `owner_id`，并在所有信号查询中强制加入所有者过滤。

终态记录不物理删除。清理策略只能归档历史事件，不能删除仍被 Trace、回测或持仓周期引用的数据。

### 9.8 读取与延迟过期

读取活跃信号时同时查询 `active`、`needs_review` 和 `ambiguous`，再根据当前市场交易日历计算 `effective_status`。即使后台任务没有运行，超过 `valid_until` 的记录也必须立即按 `expired` 对外展示。

延迟发现过期时，在同一事务中追加 `signal_expired` 事件并更新快照。事件的 `occurred_at` 使用真实到期时间，`created_at` 使用本次补算写库时间，两者不能混用。

### 9.9 在当前仓库中的落点

第一版沿用现有 SQLAlchemy 和 Repository 模式：

- 六张表的 ORM Model 放入 `src/storage.py`，并接入现有 `_run_schema_migrations()`。
- 新建 `src/repositories/signal_lifecycle_repo.py`，集中负责事务、幂等、版本检查、事件追加和快照查询。
- 生命周期 Schema 放入 `src/schemas/`；可复用现有 Agent 信号枚举，但不能把 ORM 对象直接暴露给 Agent。
- SQLite 写入复用 `DatabaseManager._run_write_transaction()` 的锁冲突重试策略。
- 单股分析、定时任务和 API 都只能调用 Repository，不能各自拼接 SQL 或复制状态转换逻辑。

需要覆盖的存储测试至少包括：建库迁移、创建信号、重复幂等写入、非法状态转换、版本冲突、延迟过期、同日触发与失效冲突、持仓周期关闭后重新创建，以及事件与快照的事务原子性。

## 10. 与 Trace 的关系

单股 Trace 和生命周期事件审计至少记录：

- 分析前的信号状态。
- 本次补算的历史区间与数据质量。
- 命中的状态转换规则。
- 本次 revision 和变化原因。
- 分析后的信号状态。
- 关联的旧信号、新信号或 `PositionWatch`。

选股 Trace 继续保存 `CandidateObservation` 和候选证据，但不生成活跃信号记录。

## 11. 评估指标

不能只提高买点触达率，否则直接按现价买入一定更容易成交，但风险和收益可能恶化。

### 11.1 EntrySignal

- 有效期内触达率。
- 平均触达等待时间。
- 触达后的 5/10/20 日收益。
- 未触达后的机会成本。
- `expired`、`invalidated`、`superseded` 比例。
- 每笔信号的 revision 次数。
- 没有新增证据却修改买点的目标漂移率。
- 补算产生 `ambiguous` 的比例。
- 包含未成交零收益后的执行调整收益。

第一版通过 `GET /api/v1/signal-preview/evaluation` 做只读影子回放。触发信号使用入场区间上沿作为保守模拟成交价，计算触发后的 5/10/20 个交易日收益；未触发信号不伪造买入收益，只计算有效期结束后的机会成本和避免损失。`execution_eligible=false` 的仅观察/风险门阻断记录保留在总样本和图表中，但不进入执行收益、触达率及参数评审分母。基准为 CN `000300`、HK `HSI`、US `SPX`，基准缺失时返回 `missing`，不填 0。按 `setup_type` 至少 30 个可评估样本且覆盖 60 天才标记为 `reviewable`，评估结果不会自动改变策略参数。

### 11.2 PositionWatch

- 每日持仓覆盖率。
- L0 升级 L1 的比例与模型成本。
- 风险告警准确率和提前量。
- 重复通知率。
- 数据缺失和 `needs_review` 比例。
- 减仓/退出建议后的最大回撤变化。

## 12. 默认策略

第一版建议冻结以下默认值：

| 项目 | 默认值 |
| --- | --- |
| 普通 EntrySignal 有效期 | 5 个交易日 |
| 中线回调信号有效期 | 10 个交易日 |
| 单股信号维护 | 用户查询前按需补算 |
| 选股候选 | 不创建生命周期信号 |
| 持仓 L0 检查 | 每个交易日盘后一次 |
| 持仓 L1 分析 | 异常、状态变化或用户主动复查时 |
| 停牌日 | 不消耗有效交易日 |
| 数据缺失 | `needs_review`，不直接作废 |
| 无券商成交证据 | 最高只能到 `price_triggered` |
| 相同持仓状态 | 记录事件，不重复通知 |
| A/H 扫描时间 | 当地 18:00 |
| 美股扫描时间 | 纽约 16:30，自动适配夏令时 |
| 退出预警 L3 | 建议减仓至 50%，不自动下单 |
| 硬止损 | 建议退出，不自动下单 |
| 退出预警 L1/L2、评分下降或数据异常 | 保持持有并升级 L1 四维 Agent + Risk Gate |
| 上线历史 | 不回填旧 Trace 或旧报告 |

## 13. 实施阶段

### Phase 1：状态与存储（已完成）

- 定义 `EntrySignal`、`SignalEvent`、`PositionWatch` 和事件 Schema。
- 创建六张生命周期表并接入现有数据库迁移。
- 实现 Repository、追加式事件、幂等键、版本检查和状态转换纯函数。
- 保证事件写入和快照更新在同一事务中完成。
- 实现交易日有效期和历史 K 线补算。

### Phase 2：单股按需生命周期（已完成）

- 单股分析前加载并补算旧信号。
- 单股分析后创建、修订、作废或替代信号；证据指纹不变时拒绝目标漂移，首次有效期不会被修订延长。
- 普通/流式 Chat 与 Trace 共用机器可读 `TradePlan`，并通过公共 API 到信号预览的跨层回归验证。
- 将当前动作与触发动作分离；等待、当前拒绝和 Risk Gate 阻断结论只要有完整价格计划也能进入信号预览，并冻结执行资格。
- 接入 Risk Gate、生命周期事件和只读查询 API。
- 不修改选股链路的候选持久化语义。

### Phase 3：持仓监控（已完成）

- 通过账户快照创建、更新和关闭 `PositionWatch`。
- 增加盘后 L0 定时任务。
- 异常时触发 L1 四维分析。
- 增加事务内 outbox、幂等投递、失败重试和状态变化摘要。

### Phase 4：执行评估（已完成，影子模式）

- 建立触达、过期、失效、机会成本、触发后收益和基准超额收益回放。
- 提供 30 秒内存短缓存，页面只读展示整体和按 `setup_type` 汇总。
- 达到样本门槛后标记 `reviewable`，当前仍不自动校准有效期或买点，必须人工发布新的策略版本。

## 14. 验收标准

- 选股运行不会创建 `EntrySignal` 或 `PositionWatch`。
- 单股查询能够读取并补算同一股票的历史活跃信号。
- 旧信号不会被新报告覆盖，所有变化都有 revision 和事件记录。
- 没有新增证据时，系统不能静默移动买点、止损或失效条件。
- 数据缺失不会被当作信号失效，也不会升级为强结论。
- 日线事件顺序不明确时必须补分钟线或标记 `ambiguous`。
- 持仓每日扫描不会重复创建信号，也不会重复推送相同状态。
- 清仓后关闭当前持仓周期，再次买入会创建新的 `position_cycle_id`。
- Trace 可以解释信号何时创建、如何变化、为什么终止。
- 任意状态更新都能从事件流水重放，并与当前快照一致。
- 相同 `dedupe_key` 重试不会产生重复事件、版本递增或通知。
- 并发写入不会静默覆盖其他请求已经提交的状态。
- `/signal-preview` 只读取图表、指标线、缠论笔/中枢和生命周期事件；页面加载不会触发状态迁移。
- 活动中的计划明确区分“等待触发”“仅观察”“风险门阻断”，并按 `revision` 和区间标签分别展示，不把观察计划渲染为立即买入。
- 图表对结构证据使用中文标签：一买、二买、三买、一卖、二卖、三卖；`center_breakout` 和 `center_breakdown` 仍作为独立结构事件展示。
