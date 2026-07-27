# 消息事件 Graphiti 知识图谱实施方案

> 目标：把消息面从“当轮搜索结果”升级为“可入库、可去重、可追踪、可验证、可回查”的事件知识图谱。最终用于候选发现、消息面验证、反方审查和前端事件追踪。

## 1. 当前现状

项目已经具备 Graphiti / Neo4j 最小链路：

- `src/services/graphiti/ontology.py` 已定义 `Stock`、`Sector`、`MarketEvent`、`ImpactVariable`、`ThemeWatch`、`AnalysisConclusion`。
- Agent Trace 结束后会通过 `_ingest_trace_to_graphiti()` 写入 Graphiti。
- `discover_watchlist_candidates` 已有 `news_momentum` 和 `event_impact` 两条消息/事件候选来源。

但当前消息面仍有明显断点：

- 新闻搜索结果主要服务当轮候选发现，缺少独立持久化。
- 多篇新闻是否属于同一事件，主要依赖当轮文本匹配，不能跨天去重。
- 宏观/政策/地缘事件只能形成“主题观察”，缺少后续事实验证闭环。
- 事件和股票之间的影响关系没有稳定图谱路径。
- 前端能看到当轮消息，但看不到事件的历史演化链。

## 2. 设计原则

1. **新闻不是结论，事件才是主对象**
   原始新闻只是 evidence source。系统入图时要抽取为事件、主题、影响变量和受影响实体。

2. **当天最新事件只影响市场/主题，不强行推出个股**
   对宏观、政策、地缘、产业链事件，如果没有后续事实验证，不直接生成个股候选，只进入 `ThemeWatch`。

3. **事件到个股必须走验证链**
   推荐链路应是：
   `新闻 -> 事件 -> 影响变量 -> 主题/板块 -> 后续验证事实 -> 股票候选`

4. **Graphiti 做时序事实，SQLite 做任务状态**
   Graphiti/Neo4j 负责实体关系和时序事实；本地 SQLite 负责抓取任务、原文缓存、去重 hash、处理状态和失败重试。

5. **图谱写入不阻塞主分析**
   入图失败不能拖垮选股/报告主链路。失败写入本地重试队列。

## 3. 数据来源

### 3.1 第一阶段数据源

优先复用现有搜索与消息工具：

- `search_general_news`
- `search_stock_news`
- `search_comprehensive_intel`
- `news_momentum`
- `event_impact`

建议查询窗口：

- 公司级硬事件：最近 3 天。
- 宏观/政策/产业事件：最近 7 天。
- 事件验证搜索：事件发生后 1-14 天滚动查询。

### 3.2 后续可接入数据源

- Tushare 公告、业绩预告、龙虎榜、增减持、回购、质押、解禁。
- StockAPI 异动、涨停池、龙虎游资、资金流向。
- GDELT / 国际新闻源，用于地缘、关税、能源、航运、科技监管等事件。

## 4. 本体设计

现有本体可复用，但建议扩展字段和边类型。

### 4.1 节点

| 节点 | 说明 | 关键字段 |
| --- | --- | --- |
| `NewsArticle` | 原始新闻/公告条目 | title, url, source, published_at, content_hash |
| `MarketEvent` | 归一化事件 | title, event_type, maturity, event_key, first_seen_at, last_seen_at |
| `ImpactVariable` | 影响变量 | variable_key, direction, confidence |
| `ThemeWatch` | 主题观察 | theme_name, status, validation_window_days |
| `Sector` | 行业/概念板块 | sector_name, level |
| `Stock` | 股票 | code, stock_name, market |
| `ValidationFact` | 后续验证事实 | title, observed_at, status, source |
| `AnalysisConclusion` | 系统分析结论 | signal, confidence, sentiment_score |

### 4.2 边

| 边 | 起点 -> 终点 | 说明 |
| --- | --- | --- |
| `MENTIONS` | NewsArticle -> Stock/Sector/Institution | 新闻直接提及实体 |
| `REPORTS` | NewsArticle -> MarketEvent | 新闻报道某事件 |
| `AFFECTS_VARIABLE` | MarketEvent -> ImpactVariable | 事件影响哪些变量 |
| `WATCHES_THEME` | MarketEvent -> ThemeWatch | 事件触发主题观察 |
| `MAPS_TO_SECTOR` | ThemeWatch -> Sector | 主题映射到板块 |
| `HAS_CONSTITUENT` | Sector -> Stock | 板块成分股关系 |
| `VALIDATED_BY` | MarketEvent/ThemeWatch -> ValidationFact | 后续事实验证 |
| `GENERATES_CANDIDATE` | ValidationFact -> Stock | 验证事实可推出个股候选 |
| `SUPPORTS` / `REFUTES` | MarketEvent/ValidationFact -> AnalysisConclusion | 支持或反驳分析结论 |

## 5. 本地 SQLite 状态表

Graphiti 不适合承担抓取任务队列和幂等控制，建议新增 SQLite 表。

### 5.1 `news_event_ingest_job`

记录每次消息抓取任务。

字段：

- `job_id`
- `query`
- `source`
- `window_days`
- `status`
- `started_at`
- `finished_at`
- `error`

### 5.2 `news_article_cache`

缓存原始新闻，防止重复入图。

字段：

- `article_id`
- `content_hash`
- `title`
- `url`
- `source`
- `published_at`
- `snippet`
- `raw_json`
- `graphiti_episode_id`
- `ingest_status`

### 5.3 `event_watch_state`

记录事件追踪状态。

字段：

- `event_key`
- `event_title`
- `event_type`
- `maturity`
- `first_seen_at`
- `last_seen_at`
- `validation_window_days`
- `next_check_at`
- `status`
- `candidate_generated_count`

## 6. 入库流程

### 6.1 新闻采集

入口建议：

- 定时任务：每天盘前、盘中、盘后各跑一次。
- Agent Trace 中的 `news_momentum/event_impact` 搜索结果也写入队列。
- 用户问事件影响时，事件搜索结果同步进入队列。

流程：

1. 搜索新闻。
2. 计算 `content_hash = sha256(title + url + published_at)`。
3. 已存在则跳过原文入库，但更新 `last_seen_at`。
4. 新文章写入 `news_article_cache`。
5. 提交 Graphiti episode。

### 6.2 事件抽取

每篇新闻入图 episode 使用结构化文本：

```json
{
  "article": {
    "title": "...",
    "source": "...",
    "published_at": "...",
    "url": "..."
  },
  "extraction_task": {
    "extract": ["MarketEvent", "ImpactVariable", "ThemeWatch", "Stock", "Sector"],
    "rules": [
      "宏观事件不能直接生成个股候选",
      "只有公司级公告/订单/业绩/监管/减持等硬事件可直接关联股票",
      "事件成熟度必须是 breaking/developing/confirmed"
    ]
  }
}
```

### 6.3 事件归一化

Graphiti 会做实体去重，但工程层仍应生成稳定 `event_key`：

```text
event_key = normalize(event_type + core_subject + impact_variable + date_bucket)
```

例子：

- `tariff_us_china_semiconductor_2026w20`
- `oil_shipping_hormuz_passage_2026w20`
- `company_order_688041_domestic_substitution_2026w20`

用途：

- 防止同一事件每天被当成新事件。
- 支持 `event_watch_state` 持续追踪。
- 支持前端按事件聚合展示。

## 7. 事件追踪与验证

### 7.1 事件成熟度

| maturity | 含义 | 是否可生成个股 |
| --- | --- | --- |
| `breaking` | 刚发生，只有初始新闻 | 默认不生成 |
| `developing` | 有多篇报道或市场反应 | 只生成主题观察 |
| `confirmed` | 有后续事实验证 | 可生成候选 |

### 7.2 验证任务

对 `breaking/developing` 事件，系统设置 `next_check_at`，在 1/3/7/14 天窗口内搜索后续事实：

- 政策是否落地。
- 价格变量是否变化，如油价、汇率、关税预期。
- 板块是否持续走强。
- 是否出现订单、公告、业绩、资金流等公司级验证。

只有出现验证事实时，才允许：

- `ThemeWatch.status = confirmed`
- 写入 `ValidationFact`
- 对相关股票生成 `news_momentum/event_impact` 候选

## 8. Agent 工具设计

### 8.1 `ingest_news_events`

用途：把新闻搜索结果写入 SQLite + Graphiti。

参数：

- `query`
- `window_days`
- `market`
- `source`
- `max_results`

返回：

- 入库文章数
- 新事件数
- 更新事件数
- 待验证事件数
- 失败原因

### 8.2 `search_event_graph`

用途：查询事件图谱。

参数：

- `query`
- `market`
- `entity_type`
- `maturity`
- `limit`

典型查询：

- “最近 7 天影响半导体的 confirmed 事件”
- “特朗普访华可能影响哪些主题，哪些已经被后续事实验证”
- “某股票最近是否有关联负面事件”

### 8.3 `get_event_watchlist`

用途：返回仍处观察期的事件。

返回：

- event_key
- title
- maturity
- watch_themes
- next_check_at
- validation_window_days
- validation_matches

### 8.4 `promote_event_candidates`

用途：从 confirmed 事件生成候选股。

规则：

- 公司级硬事件可直接生成股票候选。
- 宏观/主题事件必须经过 `ThemeWatch -> Sector -> Stock`，并且需要资金/技术至少一个维度确认。
- 未验证事件只能输出 `themes`，不能输出 `candidates`。

## 9. 与现有选股链路衔接

### 9.1 L1 候选发现

`discover_watchlist_candidates(auto)` 中的消息链路应改为：

1. 调 `search_event_graph` 查询最近 confirmed 事件。
2. 调 `get_event_watchlist` 展示观察主题。
3. 调 `promote_event_candidates` 生成消息候选。
4. 如果图谱为空，再走现有搜索 fallback。
5. 搜索 fallback 的结果写回图谱。

### 9.2 L2 深度分析

对每只候选股：

- 查询 `Stock -> MarketEvent -> ValidationFact`。
- 生成消息面 EvidenceCard：
  - 支持证据：confirmed 正面事件。
  - 反证：监管、减持、处罚、业绩下修。
  - 缺口：只有 breaking 事件，无验证事实。

### 9.3 反方审查

反方不再只靠 LLM 推理，应优先查询：

- 同事件是否已经过热或退潮。
- 同板块是否出现负面验证事实。
- 该股是否有近期被忽略的负面事件。
- 历史类似事件是否短期冲高后回落。

## 10. 前端展示

建议新增或扩展两个位置。

### 10.1 Agent Trace L1

在候选池区域展示：

- 消息事件候选数量。
- 主题观察数量。
- 事件成熟度：突发 / 发展中 / 已验证。
- 候选生成路径：

```text
事件：国产替代订单增加
-> 影响变量：国产替代预期上升
-> 主题：半导体设备
-> 验证事实：公司公告订单
-> 候选：688041 海光信息
```

### 10.2 独立事件图谱页

建议路由：`/event-graph`

核心视图：

- 活跃事件列表。
- 事件详情时间线。
- 关联主题/板块/股票。
- 验证事实。
- 生成过哪些候选。
- 当前状态：观察 / 已验证 / 已失效。

## 11. 实施阶段

### P0：文档与协议

- 明确事件本体、状态表、工具契约。
- 补充 `.env.example` 中 Graphiti/Neo4j 配置说明。

### P1：本地持久化

- 新增 SQLite 表：
  - `news_event_ingest_job`
  - `news_article_cache`
  - `event_watch_state`
- 增加 content hash 去重。
- 把 `news_momentum/event_impact` 搜索结果写入本地缓存。

### P2：Graphiti 入图

- 扩展 ontology：`NewsArticle`、`ValidationFact`。
- 新增 `ingest_news_events` 服务。
- 每条新闻写成 Graphiti episode。
- 失败写重试队列，不阻塞主链路。

### P3：事件追踪任务

- 实现 `get_event_watchlist`。
- 实现 `refresh_event_validation` 定时任务。
- 对观察期事件滚动搜索后续事实。

### P4：候选发现接入

- `discover_watchlist_candidates` 优先查询图谱 confirmed 事件。
- 未验证事件只输出 `themes`。
- confirmed 事件通过 `promote_event_candidates` 输出候选。

### P5：前端闭环

- Agent Trace 展示事件链路。
- 新增 `/event-graph` 页面。
- 支持按事件、主题、股票过滤。

## 12. 验收标准

- 同一事件多篇新闻不会重复生成多个事件节点。
- breaking 事件不会直接生成股票候选。
- confirmed 事件能展示清楚：
  `新闻 -> 事件 -> 影响变量 -> 主题/板块 -> 验证事实 -> 股票候选`
- 候选池报告能写明“消息面候选来自哪个已验证事件”。
- 图谱写入失败不影响选股报告生成。
- 前端可以看到观察中事件和已验证事件的区别。

## 13. Todo

> 状态勘误（2026-07-17）：本旧方案已被 `docs/plans/graphiti-integration-plan.md` 中的 NewsSignalCard / 显式边 / Graphiti outbox 方案替代。下面条目不再作为活跃待办；对应能力以新闻信号卡片、`search_knowledge_graph`、Web“消息”页和 Seed Pool 证据链为准。

- [x] P1 新增 SQLite 事件缓存和追踪状态表：由新闻信号卡片关系型真源表和 Graphiti outbox 替代。
- [x] P1 将 `news_momentum/event_impact` 搜索结果写入事件缓存：由 NewsSignalCard 入库、事件抽取和边重建链路替代。
- [x] P2 扩展 Graphiti ontology：`NewsArticle`、`ValidationFact`：由新闻信号卡片 episode、显式边和 Graphiti 投影替代。
- [x] P2 实现 `ingest_news_events` 服务与重试机制：由 `scripts/maintain_news_signals.py`、Graphiti outbox、重试/死信机制替代。
- [x] P3 实现事件成熟度更新和验证任务：由卡片 gate、反馈、outcome 刷新和边质量指标替代。
- [x] P4 新增 `search_event_graph`、`get_event_watchlist`、`promote_event_candidates` 工具：由 `search_knowledge_graph`、新闻信号卡片查询、Seed Pool 证据增强替代。
- [x] P4 改造 `discover_watchlist_candidates`：图谱优先，搜索 fallback：现实现为候选 seed 先召回，Graphiti/新闻信号作为证据增强，不作为硬依赖。
- [x] P5 Agent Trace 展示事件链路和成熟度：由 Trace artifact、新闻信号卡片详情和 Seed Pool 质量页跳转替代。
- [x] P5 新增 `/event-graph` 事件图谱页面：由 Web“消息”详情局部图和 Graphiti/Neo4j 投影替代。
- [x] 测试：同事件去重、未验证事件不出个股、confirmed 事件生成候选：由新闻信号服务、边重建和候选池消息/图谱证据测试覆盖。

## 14. 补充：事件抽取执行方案

### 14.1 谁来做抽取

每篇新闻入图前需要完成实体抽取（事件、影响变量、主题、关联股票）。可选方案：

| 方案 | 说明 | 优势 | 劣势 |
| --- | --- | --- | --- |
| A：Graphiti 内置 extraction | 依赖 Graphiti 配置的 LLM，写入 episode 时自动抽取 | 最简单，不需要额外代码 | 中文 A 股语境可能不够精准，不可控 |
| B：入图前 LLM 预抽取 | 用轻量模型（如 deepseek-chat）预抽取结构化字段，再写入 Graphiti | 可控性强，可定制 prompt | 每篇新闻一次 LLM 调用，成本和延迟 |
| C：规则预筛 + LLM 精抽 | 先用关键词/正则过滤低相关新闻，只对高相关新闻做 LLM 抽取 | 成本最低 | 可能漏掉隐含关联 |

建议选择：**方案 C 为主，方案 B 为补充**。

- 第一层：关键词/标题匹配过滤明显无关新闻（如娱乐、体育）。
- 第二层：对通过过滤的新闻，用轻量 LLM 做结构化抽取。
- 第三层：Graphiti 入图时仍可做补充 extraction，但不依赖它作为唯一抽取层。

成本预估：
- 假设每天 200 篇新闻通过第一层过滤。
- 每篇 LLM 抽取约 500-800 token input + 200-400 token output。
- 使用 deepseek-chat 约 $0.001/篇，每天约 $0.2。

### 14.2 抽取 Prompt 模板

```text
你是 A 股市场事件抽取器。从以下新闻中提取结构化信息。

规则：
- 宏观/政策/地缘事件不能直接关联个股，只能关联主题或板块。
- 只有公司级公告/订单/业绩/监管/减持等硬事件可直接关联股票。
- 事件成熟度：breaking（仅初始报道）/ developing（多篇报道或市场反应）/ confirmed（有后续事实验证）。
- 如果无法确定事件类型，标记为 unknown。

输出 JSON：
{
  "events": [...],
  "impact_variables": [...],
  "themes": [...],
  "stocks_mentioned": [...],
  "sectors_mentioned": [...],
  "polarity": "positive/negative/neutral"
}
```

## 15. 补充：event_key 归一化可靠性

### 15.1 生成规则

`event_key` 由 LLM 抽取时同步生成，遵循以下规范：

```text
event_key = lowercase(event_type) + "_" + lowercase(core_subject) + "_" + date_bucket
```

- `event_type`：枚举值（tariff, policy, earnings, order, regulation, geopolitical, industry_chain, monetary, fiscal, disaster）
- `core_subject`：2-4 个英文关键词，用下划线连接
- `date_bucket`：`YYYY-wWW`（ISO 周），持续事件使用首次出现的周

### 15.2 合并与别名

同一事件可能被不同新闻抽取出不同 key。处理方式：

- LLM 抽取时，prompt 要求"如果这是已知事件的后续报道，使用相同 event_key"。
- 入图前查询 `event_watch_state`，如果 title 相似度 > 0.85 且 event_type 相同，合并到已有 key。
- 支持 `event_aliases` 字段，允许一个事件有多个 key 指向同一节点。

### 15.3 模糊去重

新闻层面的去重不能只靠 `content_hash`（同一篇被不同站转载时 url 不同）：

- 第一层：`content_hash` 精确去重。
- 第二层：title 相似度 > 0.9 且 `published_at` 同天，视为同一篇。
- 相似度计算建议用 Jaccard（分词后）或编辑距离，不需要 embedding。

## 16. 补充：负面事件处理

### 16.1 事件极性

每个 `MarketEvent` 增加 `polarity` 字段：

| polarity | 含义 | 处理方式 |
| --- | --- | --- |
| `positive` | 利好事件 | 走正向候选链路 |
| `negative` | 利空事件 | 直接进入反证库，不走候选发现 |
| `neutral` | 中性/不确定 | 进入主题观察 |

### 16.2 负面事件链路

负面事件不走 `ThemeWatch -> 验证 -> 候选` 的正向链路，而是：

```text
负面新闻 -> MarketEvent(polarity=negative)
-> 直接关联 Stock（如果是公司级）
-> 写入反证库
-> L2 深度分析时作为 counter_evidence
-> 如果股票已在候选池中，触发降权或移除
```

典型负面事件：
- 监管处罚、立案调查
- 财务造假、审计保留意见
- 大股东减持、高管离职
- 业绩暴雷、下修预告
- 质押爆仓、债务违约

### 16.3 负面事件和候选池的交互

- 候选池中的股票如果命中负面事件，自动标记 `risk_flag`。
- Judge 必须看到负面事件标记，不能只看正面证据。
- 前端展示时，负面事件用红色标记，和正面候选理由区分。

## 17. 补充：事件过期和清理

### 17.1 事件生命周期

| 状态 | 含义 | 自动转换规则 |
| --- | --- | --- |
| `breaking` | 刚发生 | 超过 3 天无后续 → `fading` |
| `developing` | 发展中 | 超过 `validation_window_days` 无验证 → `expired` |
| `confirmed` | 已验证 | 验证后 30 天无新事实 → `archived` |
| `fading` | 退潮中 | 7 天后 → `expired` |
| `expired` | 已过期 | 不再参与候选发现，保留图谱供回查 |
| `archived` | 归档 | 长期保留，不参与任何实时链路 |

### 17.2 清理策略

- `expired` 事件：保留图谱节点和边，但不参与 `search_event_graph` 的默认查询（需显式指定 `include_expired=true`）。
- `archived` 事件：90 天后可从 Neo4j 迁移到冷存储（导出 JSON），释放图谱空间。
- `news_article_cache`：原文缓存 30 天后可清理 `raw_json`，只保留 metadata。

### 17.3 定时清理任务

建议每天盘后运行一次状态更新：

```text
1. breaking 超过 3 天无后续报道 → fading
2. developing 超过 validation_window_days → expired
3. fading 超过 7 天 → expired
4. confirmed 超过 30 天无新事实 → archived
5. news_article_cache 超过 30 天 → 清理 raw_json
```

## 18. 补充：价格反应回写

### 18.1 目的

判断"消息是否已被价格消化"，避免推荐已经涨完的股票。

### 18.2 数据结构

在 `MarketEvent` 或 `ValidationFact` 上增加 `price_reaction` 字段：

```json
{
  "price_reaction": {
    "tracked_stocks": [
      {
        "code": "688041",
        "name": "海光信息",
        "reaction_start": "2026-05-12",
        "reaction_pct": 12.5,
        "reaction_days": 3,
        "max_drawdown_after": -3.2,
        "exhausted": false
      }
    ],
    "sector_reaction": {
      "sector": "半导体设备",
      "reaction_pct": 8.3,
      "reaction_days": 2
    }
  }
}
```

### 18.3 判断规则

| 条件 | 判断 | 对候选的影响 |
| --- | --- | --- |
| `reaction_pct > 15%` 且 `reaction_days > 3` | 大概率已消化 | 降权或标记 `exhausted=true` |
| `reaction_pct < 5%` 且事件为 confirmed | 可能尚未反应 | 正常进入候选 |
| `reaction_pct > 10%` 但 `max_drawdown_after > 5%` | 冲高回落 | 标记风险，不建议追高 |

### 18.4 数据来源

价格反应数据从现有行情工具获取：
- 事件 `first_seen_at` 后 1/3/5/10 天的涨跌幅。
- 建议在验证任务（Section 7.2）中同步计算价格反应。

## 19. 补充：事件关联度分级

### 19.1 关联类型

在 `MENTIONS`、`GENERATES_CANDIDATE` 等边上增加 `relevance` 字段：

| relevance | 含义 | 示例 | 能否生成候选 |
| --- | --- | --- | --- |
| `direct` | 公司公告、订单、业绩直接相关 | 公司获得 XX 订单 | 可以 |
| `indirect` | 产业链上下游、供应商/客户 | 下游需求增加利好上游 | 需验证后可以 |
| `thematic` | 仅主题相关，无直接业务关联 | 同属"国产替代"概念 | 默认不生成，只观察 |

### 19.2 规则

- 只有 `direct` 关联的公司级事件可直接生成候选。
- `indirect` 需要至少一个 `ValidationFact` 确认传导关系后才能生成候选。
- `thematic` 只进入 `ThemeWatch`，不生成个股候选。

## 20. 补充：调度策略

### 20.1 触发方式

| 触发 | 时间 | 内容 | 调度方式 |
| --- | --- | --- | --- |
| 盘前采集 | 每日 8:30 | 隔夜新闻、公告、政策 | cron / APScheduler |
| 盘中采集 | 每日 11:30, 14:00 | 盘中异动、突发事件 | cron / APScheduler |
| 盘后采集 | 每日 16:00 | 收盘总结、龙虎榜、资金 | cron / APScheduler |
| Trace 触发 | 用户运行 trace 时 | 搜索结果同步入队列 | 同步写入 SQLite，异步入图 |
| 验证任务 | 每日 17:00 | 对观察期事件搜索后续事实 | cron / APScheduler |
| 清理任务 | 每日 18:00 | 状态更新和过期清理 | cron / APScheduler |

### 20.2 失败处理

- 采集失败：记录到 `news_event_ingest_job.error`，下次定时任务重试。
- Graphiti 写入失败：写入本地重试队列（SQLite 表 `graphiti_retry_queue`），最多重试 3 次，间隔 5/15/60 分钟。
- 全部失败：不影响主分析链路，前端展示"事件图谱暂不可用"。
- 告警：连续 3 次采集失败时，写入日志 WARNING 级别（后续可接飞书通知）。

### 20.3 Trace 中的新闻入图时序

```text
用户触发 trace
-> news_momentum / event_impact 搜索新闻
-> 搜索结果立即写入 news_article_cache（同步）
-> 搜索结果进入 Graphiti 入图队列（异步）
-> 主分析链路继续，不等待入图完成
-> 入图完成后更新 event_watch_state
```

## 21. 补充：容量和性能预估

### 21.1 数据量预估

| 指标 | 预估值 | 说明 |
| --- | --- | --- |
| 每日新闻采集量 | 300-500 篇 | 三次采集合计 |
| 通过关键词过滤后 | 100-200 篇 | 约 40% 通过率 |
| 需要 LLM 抽取的 | 100-200 篇 | 过滤后全部抽取 |
| 每日新增事件节点 | 10-30 个 | 大部分新闻归入已有事件 |
| 每日新增边 | 50-150 条 | MENTIONS, REPORTS 等 |
| 图谱总节点（半年后） | ~10,000 | 事件 + 股票 + 板块 + 主题 |
| 图谱总边（半年后） | ~50,000 | 各类关系 |

### 21.2 性能要求

| 操作 | 目标延迟 | 说明 |
| --- | --- | --- |
| 单篇新闻入图 | < 3s | 含 LLM 抽取 + Graphiti 写入 |
| `search_event_graph` 查询 | < 500ms | 常规查询，limit 20 |
| `get_event_watchlist` | < 200ms | 本地 SQLite 查询 |
| `promote_event_candidates` | < 1s | 图谱查询 + 规则过滤 |
| 每日全量采集 | < 10min | 含搜索 + 过滤 + 抽取 + 入图 |

### 21.3 Neo4j 资源

- 建议最小配置：2 核 4GB（开发/个人使用）。
- 索引：`MarketEvent.event_key`、`Stock.code`、`NewsArticle.content_hash`、`MarketEvent.maturity`。
- 定期 `CALL db.clearQueryCaches()` 防止内存膨胀。

## 22. 补充：与 Evidence Card Protocol 对接

消息面 EvidenceCard 示例（对齐 `architecture/agent-evidence-card-protocol.md`）：

```json
{
  "card_id": "news_event:688041:2026-05-15",
  "run_id": "trace-session-id",
  "stock": {"code": "688041", "name": "海光信息", "market": "cn"},
  "dimension": "news_event",
  "producer": {
    "tool": "search_event_graph",
    "expert": "news_event_expert",
    "version": "evidence-card-v1"
  },
  "data_quality": {
    "status": "ok",
    "as_of": "2026-05-15",
    "freshness": "recent",
    "source": "graphiti:event_graph",
    "source_chain": [
      {"provider": "graphiti", "result": "ok"},
      {"provider": "tavily_search", "result": "ok"}
    ],
    "warnings": [],
    "missing_fields": []
  },
  "signals": [
    {
      "name": "confirmed_positive_event",
      "value": "国产替代订单落地",
      "direction": "positive",
      "strength": "high",
      "change": {
        "vs_5d": "strengthening"
      },
      "interpretation": "公司公告获得国产替代订单，事件已从 developing 升级为 confirmed"
    }
  ],
  "impact": {
    "stance": "support",
    "action_bias": "open",
    "confidence": 0.78,
    "score_delta": 12,
    "reason": "公司级硬事件已验证，且价格尚未充分反应（事件后涨幅 4.2%）"
  },
  "counter_evidence": [
    {
      "claim": "国产替代利好可立即买入",
      "evidence": "同板块已有多只股票涨幅超 15%，板块可能接近过热",
      "severity": "low"
    }
  ],
  "expiry": {
    "valid_until": "2026-05-18",
    "refresh_trigger": "next_trading_day_or_new_event"
  },
  "raw_ref": "graphiti:event/company_order_688041_domestic_substitution_2026w20"
}
```

## 23. 补充：边的时间属性

图谱边应携带时间戳，支持时序查询：

| 边 | 时间字段 | 说明 |
| --- | --- | --- |
| `REPORTS` | `reported_at` | 新闻发布时间 |
| `MENTIONS` | `mentioned_at` | 提及时间 |
| `AFFECTS_VARIABLE` | `affected_at` | 事件影响开始时间 |
| `VALIDATED_BY` | `validated_at` | 验证事实观察时间 |
| `GENERATES_CANDIDATE` | `generated_at` | 候选生成时间 |
| `SUPPORTS` / `REFUTES` | `assessed_at` | 评估时间 |

Graphiti 本身支持时序 episode，但显式标注时间字段有助于：
- 按时间范围过滤边（"最近 7 天的验证事实"）。
- 前端展示事件时间线。
- 回测时按时间点还原图谱状态。
