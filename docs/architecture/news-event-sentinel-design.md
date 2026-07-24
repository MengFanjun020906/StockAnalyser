# 新闻事件哨兵底层设计

本文定义 StockAnalyser 接入 openInvest `event_watch` 哨兵模式时应保留的底层形状。目标不是复制 openInvest 的代码，而是在本项目已有新闻信号、Graphiti outbox、Agent Trace 和通知体系之上，形成一个可审计、可限流、可回放的事件感知模块。

## 目标

- 在盘中或长时段运行时，持续读取持仓、自选和候选池相关的新闻事件。
- 在 LLM 或 Agent 介入前完成源级去重、时间约束、质量门和关注标的过滤，控制噪声与成本。
- 将原始新闻先沉淀为 `RawNewsEpisode`、`NewsExtractedEvent`、`NewsSignalCard`，再决定是否触发通知或 Agent Trace。
- 将飞书机器人消息卡片做成最外层适配器，底层只输出结构化通知信封，不绑定飞书 JSON。
- 保留 openInvest 的哨兵优势：主动感知、命中触发、任务审计、失败隔离、冷却防刷屏。

## 非目标

- 不做自动交易，不根据单条新闻直接生成买卖指令。
- 不替换现有 `NewsSignalService`、`NewsSignalRepository` 或 Graphiti outbox。
- 不把 openInvest 的 `EventStore` 和 job runner 原样搬入本仓库。
- 第一阶段不实现完整飞书交互卡片 UI，只定义底层输出契约。
- 新闻事件哨兵和价格异动哨兵分开设计；前者看事件，后者看分钟价格和 ATR。

## 从 openInvest 学到的优点

1. **事件层高于新闻源**：`event_watch` 不是把新闻列表直接推给用户，而是先归一化为事件，再按严重度、方向和影响标的触发。
2. **关注宇宙驱动采集**：查询由 holdings 和 target assets 生成，同时保留 CPI、FOMC、非农、PPI 等宏观常驻查询。
3. **LLM 前去重**：已见 URL 在归一化前跳过，避免重复消耗 token。
4. **多源失败隔离**：DDGS、SearXNG、RSS、yfinance、中文快讯并发抓取，单源失败不影响其他源返回。
5. **低成本预过滤**：RSS / wire 泛新闻先按 symbol alias 与宏观关键词过滤，再进入事件抽取。
6. **触发门明确**：只有新事件、严重度达标、方向非 neutral、affected symbols 命中 watched set 时才触发。
7. **调度和运行审计分离**：cron 只是触发器，任务执行结果写 run log，便于复盘命中率与失败原因。
8. **通知和重跑是外层动作**：邮件与委员会重跑不影响事件入库主链路，失败只记录 warning。
9. **价格哨兵独立**：价格异常检测有自己的窗口、阈值和冷却，不与新闻事件判断混在一起。

## StockAnalyser 的模块形状

新增深模块建议命名为 `NewsEventSentinel`，位置可放在 `src/services/news_event_sentinel.py`。它对调度器暴露一个窄接口：

```python
class NewsEventSentinel:
    def run_once(self, *, now: datetime | None = None, dry_run: bool = False) -> NewsEventSentinelResult:
        ...
```

这个接口内部隐藏以下实现细节：

- 关注宇宙构建。
- 多源新闻抓取与 source-level 去重。
- 原始新闻写入和结构化事件抽取。
- 事件卡片评分、质量门和 company mapping gate。
- 触发策略、冷却策略和运行审计。
- 通知信封生成和外层触发派发。

调度器、CLI、API 或后续飞书机器人都只依赖 `run_once()` 的结果，不直接调用采集源或判断规则。

## 内部模块与接口

| 模块 | 建议接口 | 职责 |
| --- | --- | --- |
| `WatchedUniverseProvider` | `load() -> WatchedUniverse` | 汇总 Portfolio 持仓、`STOCK_LIST` 自选、Seed Pool 关注候选和宏观常驻主题。 |
| `NewsSourceAdapter` | `fetch(universe, since, limit) -> list[RawNewsCandidate]` | 统一 CLS、雪球、宏观财经源、openInvest 搜索源和未来 RSS 源；单源失败只写 diagnostics。 |
| `RawEpisodeIngestor` | `ingest(candidates) -> IngestResult` | 复用 `NewsSignalService` / `NewsSignalRepository`，按 `episode_id` 与 `dedup_key` 幂等写入。 |
| `EventNormalizer` | `normalize(raw_episodes) -> EventProjection` | 复用当前 `_extract_news_events` 和新闻信号卡片生成逻辑，LLM 失败时保留规则兜底。 |
| `SentinelDecisionPolicy` | `decide(cards, universe) -> list[SentinelTrigger]` | 按关注命中、严重度、方向、质量、时效、冷却决定是否触发。 |
| `SentinelLedger` | `record_run()` / `record_trigger()` | 持久化 run、trigger、cooldown 和通知状态，支持回放与排障。 |
| `SentinelNotifier` | `send(envelope) -> NotificationResult` | 外层通知接口；飞书、邮件、企业微信、日志都只是实现。 |
| `TraceTriggerAdapter` | `enqueue(trigger) -> TraceResult` | 可选触发 Agent Trace，和通知解耦。 |

这里的关键是接口浅、实现深：调度器只知道“跑一次哨兵”，飞书只知道“渲染一个通知信封”，Agent 只知道“收到一个事件触发任务”。新闻源、去重、评分和冷却都留在哨兵底层模块内部；飞书通知卡片只是最外层汇报适配器，不参与采集、入库或 LLM 抽取。

## 数据流

```text
Scheduler / CLI / API
  -> NewsEventSentinel.run_once()
    -> WatchedUniverseProvider.load()
    -> NewsSourceAdapter.fetch()
    -> RawEpisodeIngestor.ingest()
    -> EventNormalizer.normalize()
    -> SentinelDecisionPolicy.decide()
    -> SentinelLedger.record_run() / record_trigger()
    -> SentinelNotifier.send(envelope)       # optional, best-effort
    -> TraceTriggerAdapter.enqueue(trigger) # optional, budgeted
```

每一步都返回结构化 diagnostics。底层哨兵可以主动复用新闻信号链路完成拉取、入库、去重、LLM 抽取和卡片生成；飞书信息卡片只消费触发后的通知信封，负责监控汇报，不负责改变数据真源。下游通知或 Trace 失败不能回滚已成功的事件入库。

## 关注宇宙

`WatchedUniverse` 至少包含：

```python
@dataclass(frozen=True)
class WatchedUniverse:
    holdings: list[WatchedSymbol]
    watchlist: list[WatchedSymbol]
    candidate_symbols: list[WatchedSymbol]
    macro_queries: list[str]
    source_queries: list[str]
    symbol_aliases: dict[str, list[str]]
    loaded_at: datetime
```

构建顺序：

1. Portfolio 当前持仓，优先级最高。
2. `STOCK_LIST` / Web 自选组合，作为关注列表。
3. 最近 Seed Pool / Agent 候选池，可作为低优先级观察对象。
4. 宏观常驻查询：国内货币政策、逆回购、汇率、监管政策、美联储、CPI、PPI、非农、关税等。
5. 主题常驻查询只在命中持仓或候选主题时启用，避免全市场关键词刷屏。

股票代码要在模块入口统一规范化，支持 A 股、港股、美股的 display code、provider code、公司简称和别名。后续触发判断只使用 canonical symbol，避免大小写或市场后缀不一致导致“通知发了但 Trace 没触发”。

## 采集与去重

底层新闻哨兵可以主动复用现有能力：

- `NewsSignalService.ingest_cls_incremental()` 继续负责 CLS 增量快讯。
- 雪球热榜、宏观财经源和 `SearchService` 结果通过 `NewsSourceAdapter` 转成统一候选。
- openInvest 的新闻搜索思路可以作为 adapter 实现参考，但不能绕开本项目的关系型新闻信号真源。

去重分三层：

1. **URL / source id 去重**：和 openInvest 一样，在 LLM 前过滤已见 URL 或 source id。
2. **episode 去重**：继续使用 `RawNewsEpisode.episode_id` 和 `dedup_key` 幂等写入。
3. **event / card 去重**：复用 `NewsExtractedEvent` 的身份约束、`NewsSignalCard.card_id` 和 same-event 聚类。

LLM 抽取只处理新 episode 或发生强更新的 episode。强更新包括正文补全、来源数量增加、同事件出现更高可信来源、同事件影响标的变为持仓。

## 触发策略

`SentinelDecisionPolicy` 不按“有新闻就推送”工作，而按事件触发：

| 门 | 规则 |
| --- | --- |
| 新鲜度门 | `published_at` 或 `ingested_at` 在扫描窗口内；未知时间降权，不作为强触发。 |
| 关注命中门 | 公司级事件必须命中持仓、自选或候选池；宏观事件必须命中 A 股或美股宏观风险白名单；正向产业线索可不要求公司命中。 |
| 质量门 | `status=active`，低质量 raw episode、泛称“这家公司”、无明确主体映射默认不触发。 |
| 方向门 | `news_tone` / event direction 不能是 neutral；利空、监管、减持、业绩预告、重大合同等保留更高优先级。 |
| 严重度门 | `signal_score`、`evidence_grade`、`mapping_confidence` 和事件类型共同决定 severity。 |
| 冷却门 | 同一 `(symbol, event_type, direction, canonical_event_key)` 在冷却窗口内只触发一次。 |
| 升级门 | 冷却期内若新增高可信来源、severity 升级或持仓风险暴露显著增加，可突破冷却。 |

当前宏观白名单聚焦两个市场：A 股宏观使用 `MACRO:A_SHARE`，识别 A 股、沪深指数、人民币、人民银行、公开市场、逆回购、MLF、LPR、降准、社融、M2、国内流动性等线索；美股宏观使用 `MACRO:US`，识别美国、美联储、FOMC、非农、失业率、PCE、美元、美债、纳指、标普、道指等线索。欧洲央行等非目标市场宏观不会默认触发。正向产业线索使用 `SIGNAL:POSITIVE`，要求 `status=active`、`news_tone=positive`、`signal_score>=50`，即使暂未映射到具体持仓/自选，也会推送给飞书做机会观察。

建议 severity 映射：

| Severity | 建议条件 | 默认动作 |
| --- | --- | --- |
| `low` | 行业主题或弱相关候选，证据有限 | 只入库，不通知。 |
| `mid` | 命中自选/候选，证据可信，方向明确 | 生成通知信封，可发送飞书摘要。 |
| `high` | 命中持仓，或监管/业绩/减持/停复牌/重大事故等高风险事件 | 发送通知，并可排队 Agent Trace。 |
| `critical` | 影响持仓且可能改变风险假设，或宏观风险明显冲击组合 | 通知 + 高优先级 Agent Trace，仍不自动交易。 |

## 运行审计与冷却

建议新增最小持久化表，而不是把冷却状态塞进卡片 diagnostics：

```text
news_event_sentinel_runs
- run_id
- started_at
- finished_at
- status
- watched_symbol_count
- source_query_count
- fetched_count
- unseen_count
- raw_episode_count
- card_count
- trigger_count
- errors_json
- diagnostics_json

news_event_sentinel_triggers
- trigger_id
- run_id
- card_id
- event_id
- canonical_symbol
- event_type
- direction
- severity
- cooldown_key
- triggered_at
- notification_status
- trace_status
- notification_payload_json
- diagnostics_json
```

`cooldown_key` 由 canonical symbol、事件类型、方向和事件摘要稳定哈希生成。公司级事件使用股票 canonical symbol；宏观事件使用 `MACRO:A_SHARE` 或 `MACRO:US`；正向产业线索使用 `SIGNAL:POSITIVE`。冷却判断读取最近 trigger 记录即可，不需要额外状态表。这样能回答三个问题：这轮哨兵拉了什么、为什么触发、为什么没有重复触发。

## 通知信封

底层只产出 `SentinelNotificationEnvelope`：

```python
@dataclass(frozen=True)
class SentinelNotificationEnvelope:
    title: str
    severity: str
    direction: str
    symbols: list[str]
    summary: str
    why_triggered: list[str]
    source_count: int
    first_seen_at: datetime | None
    card_id: str
    event_id: str | None
    trace_id: str | None
    links: list[dict[str, str]]
    transmission_paths: list[dict[str, Any]]
    diagnostics: dict[str, Any]
```

当前 webhook 版 `FeishuSentinelNotifier` 已接入 `SentinelNotifier`，使用 `NEWS_EVENT_SENTINEL_FEISHU_ENABLED=true` 开启，并复用 `FEISHU_WEBHOOK_URL`、`FEISHU_WEBHOOK_SECRET`、`FEISHU_WEBHOOK_KEYWORD`。飞书机器人消息卡片只读取这个信封并渲染为 interactive card。卡片建议包含：

- 标题：`[high][利空] 600519 茅台：...`
- 摘要：一句话解释事件。
- 触发原因：持仓命中、自选命中、severity、来源数、证据等级。
- 关联传导路径：来自 `NewsSignalCard.transmission_paths`，展示事件如何影响行业、公司或持仓风险。
- 影响标的：canonical code + name。
- 来源：最多 3 条标题与链接。
- 动作：查看消息卡片、启动/查看 Agent Trace、稍后复核。

这样未来要换邮件、企业微信、Slack 或 Stream Bot，只需要新增 `SentinelNotifier` 实现，不需要改哨兵判断。

## Agent Trace 触发

Agent Trace 属于外层动作，不属于事件判定本身。建议分阶段：

1. `notify_only`：只入库和发通知，验证噪声率。
2. `trace_for_holdings`：只有命中持仓且 severity >= high 时，排队持仓复盘 Trace。
3. `trace_for_watchlist`：自选或候选命中 severity >= high 时，排队事件影响 Trace。
4. `manual_confirm`：飞书卡片提供按钮，由用户点击后触发 Trace。

需要预算保护：每轮最大 Trace 数、每天最大 Trace 数、同 symbol 冷却、同事件冷却、失败退避。Trace 失败不影响事件入库和通知审计。

## 调度设计

当前 `src/services/news_signal_scheduler.py` 已经支持 opt-in 后台任务，第一阶段沿用 interval task，比引入 APScheduler cron 成本低：

```text
NEWS_EVENT_SENTINEL_ENABLED=false
NEWS_EVENT_SENTINEL_INTERVAL_MINUTES=30
NEWS_EVENT_SENTINEL_ACTIVE_WINDOWS=08:00-23:59
NEWS_EVENT_SENTINEL_MAX_ITEMS_PER_SOURCE=50
NEWS_EVENT_SENTINEL_MIN_SEVERITY=mid
NEWS_EVENT_SENTINEL_COOLDOWN_MINUTES=120
NEWS_EVENT_SENTINEL_TRIGGER_MODE=notify_only
NEWS_EVENT_SENTINEL_TRACE_MAX_PER_RUN=2
NEWS_EVENT_SENTINEL_TRACE_MAX_PER_DAY=8
NEWS_EVENT_SENTINEL_FEISHU_ENABLED=false
GRAPHITI_OUTBOX_WORKER_ENABLED=true
GRAPHITI_OUTBOX_INTERVAL_SECONDS=600
# FEISHU_WEBHOOK_URL=...
# FEISHU_WEBHOOK_SECRET=...
# FEISHU_WEBHOOK_KEYWORD=StockAnalyser
```

后续如果需要 openInvest 同款 cron，可再增加 `NEWS_EVENT_SENTINEL_CRON`，但 cron parser 不应成为第一阶段的核心依赖。运行窗口应在 `NewsEventSentinel` 内判断，确保手动 `run_once(dry_run=True)` 仍可跳过窗口或显式 override。市场触发还要经过 `NEWS_EVENT_SENTINEL_CARD_MAX_AGE_MINUTES` 新鲜度闸门，防止同一张日内旧卡在冷却过期后被重新当作最新消息发送。触发通知会按 `NEWS_EVENT_SENTINEL_TRACE_MAX_PER_RUN` 对 Graphiti 做 best-effort 查询，并将 trace 状态写入飞书卡片和 trigger ledger；Graphiti outbox worker 与哨兵 worker 同进程注册，用于按周期把新闻卡与关系边投影到 Neo4j。Graphiti 的 LLM、embedding、reranker 都走项目适配层；没有 OpenAI-compatible embedding key 时降级为本地 hash embedding，确保消息入库与关系投影不会因为外部 embedding 凭证缺失而停摆；使用 Ollama Qwen3 时会自动透传 `think=false`，避免结构化抽取阶段长时间只输出 thinking。

后台任务注册原则：

- 默认关闭。
- interval clamp：5 到 120 分钟。
- `run_immediately` 可以配置，生产默认不必立即跑，开发/验收可立即跑。
- 单次运行内部要有 run ledger，避免进程重启后无法解释漏报或重复触发。
- 若上一轮未结束，调度器或服务内部应跳过本轮并记录 `skipped_running`。

## 与现有新闻信号链路的关系

- `RawNewsEpisode` 仍是原始新闻真源。
- `NewsExtractedEvent` 仍是结构化事件事实层。
- `NewsSignalCard` 仍是页面、Agent evidence 和 Graphiti 投影的主卡片层。
- Graphiti outbox 仍异步消费 active 卡片，不能阻塞哨兵主流程。
- 哨兵新增的是“持续拉取/读取 + 触发决策 + 审计冷却”，不是另一套飞书消息卡片系统。飞书卡片层只负责把触发结果监控汇报出去。

## 当前实现状态

第一版已落地底层可运行闭环：`src/services/news_event_sentinel.py` 暴露 `NewsEventSentinel.run_once()`，`src/repositories/news_event_sentinel_repo.py` 持久化 run/trigger 审计，`src/services/news_signal_scheduler.py` 可在 `NEWS_EVENT_SENTINEL_ENABLED=true` 时注册 `news_event_sentinel` 后台任务。当前默认关注宇宙来自 Portfolio active positions 与 `STOCK_LIST`，默认新闻入口复用 CLS 增量入库和当日 `NewsSignalCard`；webhook 版 `FeishuSentinelNotifier` 已接入但默认关闭，开启后只消费通知信封并发送 interactive card；方向性产业线索支持 `SIGNAL:POSITIVE` 与 `SIGNAL:NEGATIVE`，分别服务入场观察和避险预警；开启 `NEWS_EVENT_SENTINEL_HEARTBEAT_ENABLED` 后，若本轮无市场触发，会按独立 heartbeat cooldown 发送存活卡片并写入 trigger ledger；Agent Trace 仍保持外层适配器位置，尚未直接接入；哨兵通知信封已显式携带 `transmission_paths`，供飞书卡片渲染关联传导路径。

## 测试策略

优先围绕 `NewsEventSentinel.run_once()` 写离线单测，使用 fake adapters：

- 空关注宇宙：不抓新闻、不触发。
- 单源失败：返回 partial diagnostics，其他源结果仍入库。
- 重复 URL / episode：不进入 LLM 抽取，不触发重复通知。
- 未命中关注标的：只入库，不通知。
- 命中自选且 severity=mid：生成通知信封，不触发 Trace。
- 命中持仓且 severity=high：生成通知信封并排队 Trace。
- 低质量或泛称公司映射：压制触发。
- 冷却窗口内同事件：不重复通知。
- severity 升级或新增可信来源：允许突破冷却。
- 飞书适配器：只测 envelope 到 card payload 的渲染，不测事件判断。

## 分阶段落地

| 阶段 | 交付 | 验收 |
| --- | --- | --- |
| P0 | 本设计文档和配置草案 | 设计边界明确，飞书与核心解耦。 |
| P1 | `NewsEventSentinel.run_once()` + fake adapter 测试 + run/trigger ledger | 可离线跑通 notify-only 决策。 |
| P2 | 接入现有 CLS/雪球/宏观源和 `NewsSignalService` 入库 | 盘中增量能产生卡片和触发记录。 |
| P3 | 飞书 `SentinelNotifier` 交互卡片 | 卡片展示事件、原因、来源、Trace 状态。 |
| P4 | Agent Trace 触发适配器 | 仅在预算和冷却允许时触发复盘 Trace。 |
| P5 | 独立价格异动哨兵 | 以分钟价格、ATR 和持仓暴露为输入，不复用新闻 severity。 |

## 风险与护栏

- **消息噪声**：默认 `notify_only`，先用 run ledger 评估命中率，再开 Trace。
- **LLM 成本**：LLM 前做 URL/source id/episode 去重，低质量源和未命中关注宇宙不抽取。
- **符号误映射**：公司级触发必须要求正文或标题出现明确代码/公司名，沿用当前 company mapping gate。
- **时间漂移**：未知发布时间只作为弱证据；过期新闻不能触发盘中告警。
- **通道失败**：飞书发送失败只更新 trigger 的 notification status，不回滚事件入库。
- **Agent 放大成本**：Trace 触发必须有 run/day budget 和 cooldown。
- **调度误区**：interval 和 active window 明确按 Asia/Shanghai 解释；未来 cron 要在配置校验中校验。

## 最小实现建议

第一版只做三个类和两张审计表：

1. `NewsEventSentinel`：编排 `run_once()`。
2. `SentinelDecisionPolicy`：纯函数式触发判断，便于单测。
3. `SentinelNotificationEnvelopeBuilder`：把触发事件转成通知信封。
4. `news_event_sentinel_runs`：记录每轮扫描结果。
5. `news_event_sentinel_triggers`：记录触发、冷却和通知/Trace 状态。

webhook 版飞书机器人已作为 `FeishuSentinelNotifier` 落地：它只消费 envelope，不参与 watched set、severity、cooldown 或 Agent Trace 判断。后续增强重点是按钮交互、人工确认和 Trace 触发，而不是改变底层哨兵判断。
