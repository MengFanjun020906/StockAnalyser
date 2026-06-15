# Agent 情绪面工具实施调研与闭环方案

> 目标：把“情绪面 / 消息面”从模糊的搜索结果，升级成可以进入 L1 候选池、Regime、风险闸门和最终报告的结构化工具链。本文记录调研结论、实施方案和当前落地状态。

## 1. 结论

当前候选池已接入 AlphaSift / Sequoia / 板块涨跌榜 / `event_impact` / `news_momentum`。其中 `event_impact` 负责“事件 -> 影响变量 -> 主题观察 -> 后续事实验证 -> 个股候选”，`news_momentum` 负责“公司级新闻/公告硬事件 -> 个股消息面候选”。尚未实现的是独立市场情绪热榜候选，例如人气榜、关键词热度、社交热度和涨跌停情绪宽度。

要把功能做好，不能只加一个 `search_news` 包装。应该拆成两条链路：

| 链路 | 目标 | 入池维度 | 典型理由 |
| --- | --- | --- | --- |
| 情绪热度链路 | 找市场关注度和交易情绪正在升温的股票/板块 | `情绪/热点` | 人气排名上升、概念热度扩散、涨停生态强化、板块热度集中 |
| 消息事件链路 | 找新闻、公告、政策、风险事件驱动的股票/板块 | `消息面` | 公司公告、行业催化、政策事件、监管风险、地缘冲击映射 |

建议优先实现 4 个 Agent 工具：

1. `get_market_sentiment_snapshot`
2. `get_sentiment_heat_candidates`
3. `score_stock_news_sentiment`（已实现）
4. `scan_global_risk_events`

再把 `discover_watchlist_candidates(auto)` 改成多路召回：

`AlphaSift 技术/资金因子 -> Sequoia 形态动量 -> 事件影响观察/验证 -> 消息面动量 -> 情绪热度候选 -> 强势板块候选 -> fallback`

### 1.1 已落地：事件影响链 v1

当前已先落地 `discover_watchlist_candidates(candidate_source="event_impact")`，并在 `auto` 多路召回中启用。它不是“新闻里出现公司名就入池”，而是严格按下面的链路运行：

`1 日热点事件搜索 -> 事件类型识别 -> 影响变量 -> 主题观察 -> 7 日后续事实验证 -> 主题成分候选`

关键约束：

- 当天突发的宏观、地缘、政策、科技新闻只生成 `breaking/developing` 事件和主题观察，不直接生成个股候选。
- 只有 7 日窗口内出现真实后续事实，例如板块异动、资金流入、订单、油价、运价、保险费、出口、供应链变化等，事件才升级为 `confirmed`。
- 只有 `confirmed` 事件对应的主题，才会通过行业/概念成分股进入候选池。
- 搜索入口使用 `SearchService.search_general_news()`，不带股票代码，避免链路变成“先有股票再找新闻”。
- 对“暂无资金流入”“未见板块异动”“仍待验证”等否定表达做过滤，避免把未发生事实误判成验证证据。
- 事件节点、影响变量、主题观察会在 Graphiti 启用时 best-effort 入图；Graphiti 不可用时不影响候选发现。

`news_sentiment` 目前作为兼容别名指向 `event_impact`。个股消息评分已由 `score_stock_news_sentiment` 与 `news_momentum` 补齐；独立情绪热榜仍按 Phase 1 继续补齐。

## 2. 当前项目可复用基础

### 2.1 已有能力

| 模块 | 可复用点 | 说明 |
| --- | --- | --- |
| `src/search_service.py` | Bocha、Tavily、Brave、SerpAPI、MiniMax、SearXNG、多 key、缓存、时效过滤 | 可以直接作为新闻事件搜索底座 |
| `src/agent/tools/search_tools.py` | `search_stock_news`、`search_comprehensive_intel`、新闻落库 | 可复用结果结构和 `news_intel` 持久化 |
| `src/agent/tools/market_tools.py` | `get_sector_rankings`、`discover_watchlist_candidates` | 可接入情绪候选并输出 `reason_dimensions` |
| `src/agent/regime.py` | `SentimentComponents` 预留 | 市场情绪快照可反哺 Regime |
| `src/storage.py` | `news_intel`、分析历史、SQLite | 可新增情绪快照和事件表 |
| `apps/dsa-web/src/pages/AgentTracePage.tsx` | 候选卡片已支持 `情绪/热点`、`消息/输入` | 后端只要输出结构化字段，前端能显示 |

### 2.2 当前缺口

- `search_stock_news` 是单股搜索，不负责全市场扫描。
- 板块候选只有“强势板块成分股”，没有“为什么强势”的事件归因。
- 情绪热度没有结构化字段，如热度排名、排名变化、新闻密度、负面比例、涨停扩散、风险词命中。
- 消息面没有进入 L1 候选池，只在候选之后的深挖阶段补证据。
- Regime 的 Fear & Greed 位仍是预留，不是真正的 A 股新闻/热度情绪指数。

## 3. 数据源调研

### 3.1 推荐优先级

| 优先级 | 数据源 | 用途 | Token | 稳定性判断 |
| --- | --- | --- | --- | --- |
| P0 | AkShare / 东方财富人气榜、个股新闻、概念/行业板块、涨跌停池 | A 股情绪热度、个股新闻、主题扩散 | 不需要 | 免费但接口可能变动，要有 fallback |
| P0 | 现有 SearchService：Bocha / Tavily / SerpAPI / Brave / SearXNG | 新闻事件搜索、行业催化、风险事件 | 使用现有 key | 已接入项目，最适合先复用 |
| P0 | Tushare `anns_d` / `exchange_ann` | 公告、交易所公告、硬事件 | `TUSHARE_TOKEN`，部分接口需权限 | 结构化强，适合公告和监管事件 |
| P1 | GDELT DOC 2.0 / GDELT Cloud | 全球风险、战争、制裁、宏观事件、海外新闻情绪 | DOC 2.0 可无 key；Cloud v2 需 key | 全球事件覆盖强，但中文 A 股个股弱 |
| P2 | 百度指数、雪球、股吧、同花顺热榜 | 社交/搜索关注度 | 多数无稳定开放 API | 不建议首版依赖，容易脆弱 |
| P2 | 商业舆情 API | 舆情情绪和媒体监控 | 需采购 | 可后续增强，不作为首版前提 |

### 3.2 具体 API / 函数建议

#### AkShare / 东方财富热度与新闻

推荐函数：

- `ak.stock_hot_rank_latest_em()`：东方财富个股人气榜最新排名。
- `ak.stock_hot_rank_detail_realtime_em(symbol)`：人气排名实时变化。
- `ak.stock_hot_rank_detail_em(symbol)`：历史趋势和粉丝特征。
- `ak.stock_hot_keyword_em(symbol)`：人气关键词。
- `ak.stock_hot_up_em()`：人气飙升榜。
- `ak.stock_news_em(stock="300059")`：指定个股最近新闻，字段包含代码、标题、内容、发布时间、URL。
- `ak.stock_board_concept_name_em()` / `ak.stock_board_industry_name_em()`：概念/行业板块列表和涨跌幅。
- `ak.stock_board_concept_cons_em(symbol)` / `ak.stock_board_industry_cons_em(symbol)`：板块成分股。
- `ak.stock_zt_pool_em(date)` / `ak.stock_dt_pool_em(date)`：涨停池 / 跌停池，用于市场情绪温度。

用途划分：

| 数据 | 进入哪个工具 | 结构化字段 |
| --- | --- | --- |
| 人气榜排名 | `get_sentiment_heat_candidates` | `hot_rank`、`rank_change`、`heat_score` |
| 人气关键词 | `score_stock_news_sentiment` / `get_sentiment_heat_candidates` | `keywords`、`theme_tags` |
| 个股新闻 | `score_stock_news_sentiment` | `news_count`、`event_tags`、`positive/negative` |
| 概念/行业涨幅 | `get_market_sentiment_snapshot` / `explain_sector_sentiment` | `sector_heat`、`breadth` |
| 涨跌停池 | `get_market_sentiment_snapshot` | `limit_up_count`、`limit_down_count`、`risk_appetite` |

#### SearchService：Bocha / Tavily / SerpAPI / Brave

项目已经有统一搜索服务，首版不要重复造搜索 provider。

推荐查询模板：

| 场景 | Query 模板 |
| --- | --- |
| 个股消息面 | `{stock_name} {stock_code} 公告 业绩 订单 合作 减持 监管 最近` |
| 行业催化 | `{sector_name} A股 催化 政策 订单 景气度 涨价 最近` |
| 市场风险 | `A股 市场 风险 避险 恐慌 地缘 制裁 汇率 原油 黄金 最近` |
| 全球风险 | `(war OR sanction OR conflict OR oil OR shipping OR Taiwan OR Middle East) market risk stocks` |

Tavily 推荐参数：

- `topic="news"`：新闻场景。
- `days=1/3/7` 或 `time_range="day|week"`：保证时效。
- `max_results<=10`：避免 Token 膨胀。

SerpAPI 推荐参数：

- 百度中文搜索：`https://serpapi.com/search?engine=baidu&q=...&ct=2`
- 用时间过滤参数限制近期结果，避免旧新闻污染。

#### Tushare 公告

推荐接口：

- `pro.anns_d(ts_code, ann_date/start_date/end_date)`：上市公司全量公告，含标题和原文 URL。
- `pro.exchange_ann(start_date, end_date)`：交易所公告，适合监管、交易规则、处罚、问询等事件。

用途：

- 不用它做“情绪热度”，只做硬事件确认。
- 对 `减持`、`增持`、`业绩预告`、`问询函`、`处罚`、`重大合同`、`回购`、`解禁` 等事件给确定性标签。

#### GDELT

推荐两种接法：

1. 首版使用公开 DOC 2.0 API：
   - `https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=artlist&format=json&timespan=24h`
   - `mode=timelinetone` 可看平均 tone 时间线。
   - 查询支持 `theme:TERROR`、`tone<-5`、`toneabs>10`、布尔 OR、NEAR、REPEAT 等。
2. 后续如需要更干净事件结构，再接 GDELT Cloud v2：
   - 需要 `GDELT_CLOUD_API_KEY`
   - 适合冲突事件、故事聚类、实体发现。

用途：

- 不用于 A 股单股候选主召回。
- 用于 `scan_global_risk_events`，判断战争、制裁、能源、航运、汇率、地缘风险是否进入 `risk_off`。
- 输出给 Regime 和 Risk Gate，而不是直接推荐股票。

## 4. 工具设计

### 4.1 `get_market_sentiment_snapshot`

定位：市场情绪总览，作为选股和入场前置检查。

参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `market` | string | `cn` | 首版只支持 A 股 |
| `lookback_hours` | integer | `24` | 新闻和热度窗口 |
| `include_news` | boolean | `true` | 是否返回代表性新闻 |
| `include_global_risk` | boolean | `true` | 是否扫描全球风险 |

返回结构：

```json
{
  "status": "ok",
  "market": "cn",
  "as_of": "2026-05-11T15:30:00+08:00",
  "sentiment_score": 62,
  "sentiment_state": "greed",
  "risk_appetite": "risk_on",
  "data_quality": "full",
  "components": {
    "hot_rank": {"score": 70, "count": 100, "source": "akshare.stock_hot_rank_latest_em"},
    "sector_heat": {"score": 66, "top_sectors": ["半导体", "机器人"]},
    "limit_pool": {"score": 58, "limit_up_count": 56, "limit_down_count": 4},
    "news_tone": {"score": 52, "negative_ratio": 0.18},
    "global_risk": {"score": 45, "risk_level": "medium"}
  },
  "top_positive_events": [],
  "top_negative_events": [],
  "warnings": []
}
```

评分方式：

`sentiment_score = 0.25 * hot_rank + 0.25 * sector_heat + 0.20 * limit_pool + 0.20 * news_tone + 0.10 * global_risk_adjusted`

状态映射：

| 分数 | 状态 | 交易含义 |
| --- | --- | --- |
| `>= 80` | `extreme_greed` | 热度过高，候选可以保留但仓位降档，严禁追高 |
| `65-79` | `greed` | 风险偏好强，允许强势策略，但要检查拥挤 |
| `45-64` | `neutral` | 正常 |
| `30-44` | `fear` | 降低新开仓优先级 |
| `< 30` | `extreme_fear` | 只允许防守或等待，除非明确逆向策略 |

### 4.2 `get_sentiment_heat_candidates`

定位：生成 L1 情绪/热点候选，进入 `discover_watchlist_candidates(auto)`。

参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `market` | string | `cn` | 首版 A 股 |
| `limit` | integer | `20` | 候选数 |
| `lookback_hours` | integer | `24` | 热度变化窗口 |
| `min_heat_score` | number | `60` | 最低热度分 |
| `include_news` | boolean | `true` | 是否补新闻摘要 |

返回候选字段：

```json
{
  "code": "300572",
  "name": "安车检测",
  "source": "sentiment:hot_rank",
  "recall_sources": ["akshare:hot_rank", "akshare:hot_keyword"],
  "signal_score": 74.2,
  "strategy_tags": ["hot_rank_up", "theme_heat"],
  "reason_dimensions": [
    {
      "dimension": "sentiment",
      "label": "情绪/热点",
      "detail": "东方财富人气排名进入前 100，且关键词集中在机器人/智能制造"
    },
    {
      "dimension": "message",
      "label": "消息面",
      "detail": "近 24 小时出现 3 条相关行业催化新闻，未发现重大负面公告"
    }
  ],
  "sentiment_payload": {
    "hot_rank": 32,
    "rank_change": 18,
    "keywords": ["机器人", "设备更新"],
    "news_count": 3,
    "negative_event_count": 0,
    "confidence": 0.72
  }
}
```

候选分数：

`heat_score = rank_score * 0.35 + rank_delta_score * 0.20 + keyword_score * 0.15 + news_density_score * 0.15 + sector_sync_score * 0.15`

过滤规则：

- 剔除 ST / 退市风险 / 停牌。
- 剔除只有热度但无成交额承接的股票。
- 若 `negative_event_count > 0`，不直接剔除，但标记 `risk_flags`，交给 L2 筛选和反方审查。
- 如果热度来自跌停、监管处罚、暴雷新闻，只能进入风险观察，不进入买入候选。

### 4.3 `score_stock_news_sentiment`

定位：给单只股票结构化消息面评分，可用于 L1 候选增强和 L3 深挖。

参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `stock_code` | string | 必填 | 股票代码 |
| `stock_name` | string | 可选 | 股票名称，可自动补齐 |
| `lookback_hours` | integer | `72` | 新闻窗口 |
| `include_announcements` | boolean | `true` | 是否查 Tushare 公告 |

事件分类：

| 类别 | 例子 | 默认方向 |
| --- | --- | --- |
| `earnings_positive` | 业绩预增、扭亏、利润大增 | 正面 |
| `contract_order` | 大订单、中标、战略合作 | 正面 |
| `policy_tailwind` | 政策支持、补贴、行业规划 | 正面 |
| `buyback_increase` | 回购、增持 | 正面 |
| `reduction_unlock` | 减持、解禁 | 负面 |
| `regulatory_risk` | 问询、处罚、立案、监管函 | 负面 |
| `litigation_default` | 诉讼、违约、债务风险 | 负面 |
| `rumor_high_heat` | 传闻、网传、未证实消息 | 不确定 |

返回结构：

```json
{
  "status": "ok",
  "code": "600519",
  "name": "贵州茅台",
  "message_score": 56,
  "message_state": "neutral",
  "news_count": 8,
  "positive_count": 2,
  "negative_count": 1,
  "uncertain_count": 1,
  "events": [
    {
      "event_type": "earnings_positive",
      "direction": "positive",
      "severity": "medium",
      "title": "...",
      "source": "Tushare.anns_d",
      "published_at": "2026-05-10",
      "url": "...",
      "confidence": 0.82
    }
  ],
  "summary": "消息面中性偏正，但存在一条减持相关风险，需要 L2 复核。"
}
```

评分方式：

- 先做确定性中文关键词分类，避免纯靠 LLM。
- 标题和摘要合并匹配事件词典，当前不做 LLM 打分。
- Tushare 公告与搜索新闻先统一为新闻项，再走同一套事件分类和去重逻辑。
- 多来源重复新闻只计一次，避免同一事件刷屏导致误判。
- 负面事件会生成 `risk_flags`，不会作为 `news_momentum` 买入候选来源。

### 4.4 `scan_global_risk_events`

定位：扫描系统性风险，不直接选股，主要影响 Regime / Risk Gate。

参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `scope` | string | `global` | `global/cn/us/hk` |
| `lookback_hours` | integer | `24` | 时间窗口 |
| `risk_topics` | array | 默认内置 | 战争、制裁、能源、航运、汇率、公共安全 |

数据源：

- GDELT DOC 2.0：全球事件和 tone。
- SearchService：中文和英文新闻补充。
- 可选：大宗商品、汇率、黄金等跨资产工具，后续接入。

输出：

```json
{
  "status": "ok",
  "risk_level": "medium",
  "risk_score": 44,
  "risk_topics": ["Middle East", "oil", "shipping"],
  "events": [],
  "affected_sectors": ["油气", "航运", "黄金", "军工"],
  "risk_gate_hint": "reduce_new_entries",
  "data_quality": "limited"
}
```

## 5. 存储与缓存设计

新增 SQLite 表建议：

### 5.1 `sentiment_snapshots`

用于保存市场情绪快照。

字段：

- `id`
- `market`
- `as_of`
- `sentiment_score`
- `sentiment_state`
- `risk_appetite`
- `data_quality`
- `components_json`
- `top_events_json`
- `source_chain_json`
- `created_at`

唯一约束：

- `(market, as_of_bucket)`，其中 `as_of_bucket` 可按 10 分钟或交易日收盘分桶。

### 5.2 `sentiment_events`

用于保存新闻/公告/全球风险事件。

字段：

- `id`
- `event_id`
- `scope`
- `market`
- `code`
- `name`
- `sector`
- `event_type`
- `direction`
- `severity`
- `confidence`
- `title`
- `summary`
- `source`
- `url`
- `published_at`
- `raw_json`
- `created_at`

去重逻辑：

- 优先用 URL。
- URL 缺失时用 `title + source + published_at` hash。
- 同一事件被多个来源报道时合并 `sources`，不重复加分。

### 5.3 缓存 TTL

| 数据 | TTL | 原因 |
| --- | --- | --- |
| 人气榜 / 热榜 | 5-10 分钟 | 盘中变化快 |
| 板块热度 | 5-10 分钟 | 盘中变化快 |
| 涨跌停池 | 5 分钟 | 盘中情绪温度 |
| 个股新闻搜索 | 30 分钟 | 防止搜索配额浪费 |
| Tushare 公告 | 30-60 分钟 | 公告时效强但无需秒级 |
| GDELT 风险扫描 | 30 分钟 | 全球新闻不必高频 |

## 6. 候选池接入方案

### 6.1 `discover_watchlist_candidates(auto)` 改造

当前：

`AlphaSift -> Sequoia -> event_impact -> sector -> fallback`

建议：

`AlphaSift -> Sequoia -> event_impact -> news_momentum -> sentiment_heat -> sector -> fallback`

合并规则：

- 同一股票如果同时命中 AlphaSift 和情绪热度，`recall_sources` 合并。
- `signal_score` 加共振分，但要上限限制，避免热度刷屏压过硬证据。
- `reason_dimensions` 必须保留所有维度，不允许后来的来源覆盖前面的技术/资金理由。
- 来源多样性保留：Top N 至少保留 AlphaSift、Sequoia、sentiment/news 各一路头部候选，除非该来源为空。

### 6.2 候选理由展示样例

```json
{
  "code": "300572",
  "name": "安车检测",
  "source": "multi_recall",
  "recall_sources": [
    "alphasift:volume_breakout",
    "sentiment:hot_rank",
    "news:industry_catalyst"
  ],
  "reason_dimensions": [
    {
      "dimension": "strategy",
      "label": "策略",
      "detail": "AlphaSift YAML 多因子策略入池：放量突破"
    },
    {
      "dimension": "technical",
      "label": "技术面",
      "detail": "20 日突破幅度=4.2；量比=2.1"
    },
    {
      "dimension": "capital",
      "label": "资金面",
      "detail": "成交额=2.30亿；换手率=6.4%"
    },
    {
      "dimension": "sentiment",
      "label": "情绪/热点",
      "detail": "东方财富人气排名上升 18 位，所属机器人概念同步升温"
    },
    {
      "dimension": "message",
      "label": "消息面",
      "detail": "近 24 小时出现设备更新政策催化新闻，未发现重大负面公告"
    }
  ]
}
```

## 7. Regime / 风控接入

### 7.1 Regime

`get_market_sentiment_snapshot` 应写入 `detect_market_regime` 的情绪分量：

- `sentiment_score >= 65`：情绪偏贪婪。
- `sentiment_score <= 35`：情绪偏恐惧。
- `global_risk.risk_level >= high`：即使 A 股热度高，也要降低 `risk_appetite`。

### 7.2 Risk Gate

风控规则：

- `extreme_greed`：不阻断候选，但新开仓仓位降档，提示拥挤。
- `fear`：只允许低仓位试探或等待。
- `extreme_fear` / `global_risk=high`：默认阻断新开仓，除非用户明确要求逆向策略。
- 个股 `regulatory_risk` / `reduction_unlock`：进入反方审查硬风险。

## 8. 实施顺序

### Phase 0：事件影响链 v1（已实现）

文件：

- `src/search_service.py`
- `src/agent/tools/market_tools.py`
- `src/services/graphiti/ontology.py`
- `src/services/graphiti/graph_service.py`
- `tests/test_market_tools_watchlist.py`

已实现：

- 新增 `SearchService.search_general_news()`，用于无股票锚点的热点新闻搜索。
- 新增 `event_impact` 候选来源和 `news_sentiment` 兼容别名。
- 内置事件规则把新闻映射到 `trade_policy`、`geopolitical_energy`、`technology_policy`、`green_industry` 等事件类型。
- 事件先输出影响变量和观察主题；7 日窗口内有后续事实验证后才生成主题成分候选。
- Graphiti ontology 新增 `ImpactVariable`、`ThemeWatch`，`MarketEvent` 增加 `maturity`。
- Graphiti 写入为 best-effort，不作为候选发现硬依赖。

边界：

- 当前事件类型识别是确定性规则，不是 LLM schema 解析。
- 当前验证事实来自搜索摘要关键词匹配，已处理常见否定表达，但还不是完整语义蕴含判断。
- 当前候选从已验证主题成分股召回，不直接从事件文本抽公司名。

### Phase 1：确定性情绪热度候选

文件建议：

- `src/agent/sentiment/models.py`
- `src/agent/sentiment/lexicon.py`
- `src/agent/sentiment/akshare_adapter.py`
- `src/agent/tools/sentiment_tools.py`
- `tests/test_agent_sentiment_tools.py`

实现：

- 接 AkShare 人气榜、关键词、板块、涨跌停池。
- 实现 `get_market_sentiment_snapshot`。
- 实现 `get_sentiment_heat_candidates`。
- 接入 `discover_watchlist_candidates(auto)`。
- 前端不需要大改，复用 `reason_dimensions`。

验收：

- 无搜索 key 也能跑出情绪热度候选。
- 候选池里能出现 `情绪/热点`。
- AkShare 失败时返回 `data_quality=limited`，不能报错拖垮选股。

### Phase 2：消息面事件评分（已实现）

实现：

- 复用 `SearchService.search_stock_news()` 搜索个股新闻。
- 个股消息补证默认只尝试前两个可用搜索 provider；这是辅助维度的快速证据，不允许拖慢整轮 Trace。
- 接 Tushare `anns_d` 公告接口；未配置 `TUSHARE_TOKEN` 时自动跳过，不影响搜索链路。
- 实现 `score_stock_news_sentiment`，输出 `message_score`、`message_state`、正/负/不确定事件数、事件标签、风险提示和来源诊断。
- 实现 `src/agent/sentiment/news_events.py` 消息事件分类词典和 URL/标题去重。
- 将 `news_momentum` 候选接入 `discover_watchlist_candidates(auto)`；广域新闻先搜索 A 股公司公告/订单/监管/业绩等主题，再从搜索结果文本中匹配公司实体，避免只做“先有股票再找新闻”。

验收：

- 候选池里能出现 `消息面`，前端显示为“消息面候选”。
- 减持、处罚、问询等负面事件不会被当作利好热度，只进入风险提示。
- 同一新闻多源转载只计一次。

### Phase 3：全球风险扫描

实现：

- 接 GDELT DOC 2.0。
- 实现 `scan_global_risk_events`。
- 接入 Regime 和 Risk Gate。

验收：

- 战争、制裁、油价、航运冲击等事件能输出 `risk_level` 和 `affected_sectors`。
- 高风险事件会影响新开仓建议，而不是只作为报告文字。

### Phase 4：闭环复盘

实现：

- 情绪候选落库。
- T+1 / T+5 回评情绪候选收益、回撤、命中率。
- 在后续候选分数里按历史表现校准权重。

验收：

- 能回答“最近情绪候选表现怎么样”。
- 能发现某类热度来源是否容易一日游。

## 9. 测试矩阵

| 测试 | 内容 |
| --- | --- |
| 单元测试 | 词典分类、分数映射、重复新闻去重、source 合并 |
| Mock provider 测试 | AkShare / Tushare / SearchService / GDELT 失败、空数据、异常格式 |
| 候选池测试 | `discover_watchlist_candidates(auto)` 混合技术、资金、情绪、消息候选 |
| Regime 测试 | 情绪分数进入 `SentimentComponents`，极端风险降档 |
| 前端测试 | L1 候选池显示 `情绪/热点` 和 `消息面` 理由 |
| 离线测试 | 无网络、无 key 时仍能降级为技术/资金候选 |
| 在线 smoke | 配置 key 后验证真实接口字段，不写死响应结构 |

## 10. 配置项建议

新增 `.env.example`：

```bash
# Agent 情绪面工具
AGENT_SENTIMENT_ENABLED=true
AGENT_SENTIMENT_LOOKBACK_HOURS=24
AGENT_SENTIMENT_CACHE_TTL_SECONDS=600
AGENT_SENTIMENT_MAX_CANDIDATES=20
AGENT_SENTIMENT_INCLUDE_GLOBAL_RISK=true

# 可选：GDELT Cloud v2；不配置则使用公开 DOC 2.0 或跳过 Cloud
GDELT_CLOUD_API_KEY=
```

复用已有配置：

- `TUSHARE_TOKEN`
- `TAVILY_API_KEYS`
- `SERPAPI_API_KEYS`
- `BRAVE_API_KEYS`
- `BOCHA_API_KEYS`
- `SEARXNG_BASE_URLS`

## 11. 不建议的实现方式

- 不建议只让 LLM 读新闻列表后自由打分。结果不可复现，也无法回测。
- 不建议首版依赖雪球/股吧爬虫。容易被反爬和页面变更影响。
- 不建议把“涨幅高”直接等价为“情绪好”。涨幅是结果，情绪工具要解释热度来源。
- 不建议把负面新闻也作为普通热度候选。负面高热度要进入风险候选或反方审查。
- 不建议每次选股都全网搜索全市场。应先用热榜/板块缩小候选，再对候选做新闻评分。

## 12. 最终验收标准

这个功能算“做好”必须同时满足：

1. 候选池至少能出现 `情绪/热点` 和 `消息面` 两类结构化理由。
2. 没有搜索 key 时，情绪热度链路仍能靠 AkShare 降级运行。
3. 有 `TUSHARE_TOKEN` 时，公告类硬事件优先于普通新闻摘要。
4. 全球风险不会直接推荐股票，但会影响 Regime / Risk Gate。
5. 每条候选都能解释来源、分数、事件、置信度和数据质量。
6. 所有外部接口失败都 fail-open，不拖垮技术/资金候选池。
7. 情绪候选可落库、可回评、可调整权重。

## 13. 参考资料

- AkShare 项目与接口库说明：<https://github.com/akfamily/akshare>
- AkShare 人气榜相关函数列表：<https://github.com/akfamily/akshare/blob/main/docs/tutorial.md>
- AkShare `stock_news_em` 个股新闻接口说明：<https://akshare-hh.readthedocs.io/en/latest/data/ws/ws.html>
- Tavily Search API：<https://docs.tavily.com/documentation/api-reference/endpoint/search>
- SerpAPI Baidu Search API：<https://serpapi.com/baidu-search-api>
- Tushare 上市公司公告 `anns_d`：<https://www.tushare.pro/document/2?doc_id=176>
- Tushare 交易所公告 `exchange_ann`：<https://tushare.pro/document/41?doc_id=74>
- GDELT DOC 2.0 API tone / theme / article list：<https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/amp/>
- GDELT Query Interface：<https://gdelt.github.io/>
