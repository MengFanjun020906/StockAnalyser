# Agent 工具能力缺口分析

> 本文基于当前 `ToolRegistry` 中已注册的 Agent 工具整理，目标是回答两个问题：现有工具在哪些维度已经够用，哪些维度会影响选股/个股分析质量，以及下一阶段应该优先补哪些工具。重点关注用户提出的“情绪面工具缺口”，例如战争新闻、地缘冲突、制裁、突发公共安全事件引发的市场恐慌。

## 1. 当前工具版图

当前 Agent 工具大致覆盖 7 类能力，共 20 个工具：

| 类别 | 已有工具 | 主要价值 |
| --- | --- | --- |
| 行情数据 | `get_realtime_quote`、`get_daily_history` | 获取最新价格、涨跌幅、成交量、估值字段、历史 K 线和行情时效口径 |
| 技术分析 | `analyze_trend`、`calculate_ma`、`get_volume_analysis`、`analyze_pattern` | 趋势、均线、乖离率、MACD、RSI、量价、K 线形态、突破/箱体等 |
| 筹码与资金 | `get_chip_distribution`、`get_capital_flow` | A 股筹码成本、获利盘、主力资金净流入、5/10 日资金持续性 |
| 基本面与估值 | `get_stock_info` | 估值、成长、盈利、机构、龙虎榜、板块归属等紧凑上下文 |
| 市场与选股 | `get_market_indices`、`get_sector_rankings`、`discover_watchlist_candidates` | 指数环境、板块强弱、选股候选池生成 |
| 消息检索 | `search_stock_news`、`search_comprehensive_intel` | 个股新闻、公告、风险排查、行业与盈利相关搜索 |
| 组合/历史/图谱 | `get_portfolio_snapshot`、`get_skill_backtest_summary`、`get_strategy_backtest_summary`、`get_stock_backtest_summary`、`search_knowledge_graph` | 账户持仓、组合风险、历史信号表现、Graphiti 知识图谱检索 |

整体看，当前系统已经具备“单股分析”的基础闭环：行情 -> 技术 -> 资金/筹码 -> 基本面 -> 消息 -> 账户 -> 最终判断。选股链路也有了候选发现和分阶段筛选框架。

真正的缺口不是“没有工具”，而是“缺少能影响整体市场风险偏好的外部环境工具”。现在工具更偏个股、板块和历史价格，对宏观风险、地缘冲突、全球风险偏好、突发事件扩散的感知不足。

## 2. 按分析维度看缺口

### 2.1 市场风险偏好：缺口高

已有能力：

- `get_market_indices` 可以看到指数涨跌。
- `get_sector_rankings` 可以看到板块分化。
- 技术工具可以判断个股和指数层面的趋势。

不足：

- 指数涨跌是结果，不是原因。它不能告诉 Agent “为什么今天市场突然恐慌”。
- 缺少全市场风险偏好判断，例如 risk-on / risk-off、恐慌扩散、避险资产上行、股债汇商品联动。
- 缺少外部冲击检测，例如战争升级、地缘冲突、制裁、恐怖袭击、航运中断、能源供给冲击。

影响：

- 在战争新闻或突发风险事件出现时，Agent 可能仍然只按个股技术面给出“趋势不错，可以等回踩”的结论，忽视系统性风险。
- 选股时可能继续从强势板块里找标的，但没有判断“强势是否只是避险切换”或“热点是否来自恐慌交易”。

建议新增工具：

- `get_market_sentiment_snapshot`
- `get_global_risk_events`
- `get_cross_asset_risk_signals`

### 2.2 情绪面与舆情：缺口高

已有能力：

- `search_stock_news` 能搜单只股票相关新闻。
- `search_comprehensive_intel` 能做个股多维搜索。
- Pipeline 中存在美股社交情绪服务，但不是当前 Agent ToolRegistry 的通用工具，且更偏美股社交平台。

不足：

- 没有“市场整体情绪工具”。目前情绪主要靠个股新闻片段让 LLM 自己判断。
- 没有热度、恐慌等级、新闻密度、负面词比例、事件扩散范围等结构化字段。
- 没有把战争、制裁、台海、中东、俄乌、红海、能源、黄金、原油、汇率等风险关键词纳入固定扫描。
- 对 A 股、港股、美股的情绪传导路径没有区分。

影响：

- 用户问“今天还能不能买”“市场是不是有风险”，系统可能只能回答个股层面，而不能判断市场情绪是否已经进入避险模式。
- 对事件驱动行情的判断会偏慢，尤其是非公司层面的系统性事件。

建议新增工具：

- `get_market_sentiment_snapshot(region="cn|hk|us|global")`
- `scan_geopolitical_risk_news(region="global", lookback_hours=24)`
- `score_news_sentiment(query, market, lookback_hours)`

### 2.3 宏观与政策：缺口中高

已有能力：

- `search_comprehensive_intel` 的 market analysis / industry 维度可以间接搜到政策或宏观新闻。
- `get_market_indices` 能观察宏观影响后的市场结果。

不足：

- 没有经济日历、央行议息、CPI/PPI、PMI、非农、汇率、利率、国债收益率等结构化输入。
- 没有政策事件的时间、重要性、预期值、实际值、偏离程度。
- 对 A 股特别重要的政策口径、监管表态、产业政策没有独立入口。

影响：

- 美联储、央行、财政政策、监管政策变化时，Agent 只能靠搜索结果总结，稳定性不够。
- 对成长股、资源股、出口链、券商地产等政策敏感板块，判断会缺少上游因子。

建议新增工具：

- `get_macro_calendar`
- `get_policy_event_digest`
- `get_rate_fx_commodity_snapshot`

### 2.4 板块与产业链：缺口中

已有能力：

- `get_sector_rankings` 能看到涨跌榜。
- `get_stock_info` 返回所属板块。
- `discover_watchlist_candidates` 能从强势板块成分股里找候选。

不足：

- 板块目前主要是涨跌排名，缺少“为什么涨”的催化归因。
- 缺少产业链上下游关系，例如原油上涨影响航运/化工/航空，战争影响军工/黄金/油气/航运。
- 缺少板块拥挤度、持续性、分歧度、龙头扩散度。

影响：

- 选股可能找到强势板块成分股，但解释不清楚板块上涨是长期景气、政策催化、避险交易，还是短期情绪脉冲。
- 反方审查难以识别“板块过热”和“主题一日游”。

建议新增工具：

- `explain_sector_move`
- `map_event_to_sectors`
- `get_sector_heat_breadth`

### 2.5 个股消息与公告：缺口中

已有能力：

- `search_stock_news` 搜最新个股新闻。
- `search_comprehensive_intel` 覆盖公告、风险排查、盈利展望和行业趋势。

不足：

- 搜索结果是新闻条目，不是结构化事件。Agent 需要自己判断事件类型、日期、影响方向、可信度。
- 缺少对公告原文、交易所问询、减持计划、限售解禁、诉讼处罚的强结构化解析。
- 缺少“事件是否已经被价格反映”的字段。

影响：

- 单股分析可以看到新闻，但不稳定；同样新闻可能被模型解读成不同风险等级。
- 对减持、业绩预告、监管处罚这类硬风险，最好结构化，而不是靠自然语言搜索摘要。

建议新增工具：

- `get_company_event_timeline`
- `get_regulatory_risk_events`
- `get_unlock_and_reduction_schedule`

### 2.6 资金面：缺口中

已有能力：

- `get_capital_flow` 已覆盖个股主力净流入、5/10 日流入和板块资金流。

不足：

- 当前主要针对 A 股个股，不覆盖 ETF、指数、港股、美股。
- 缺少北向资金、融资融券余额、ETF 申赎、期权/期货持仓等风险偏好指标。
- 工具失败时对选股质量影响很大，需要更强 fallback。

影响：

- 短线交易判断依赖资金工具，一旦超时就会缺少主力承接判断。
- 对系统性风险，单股主力资金不足以判断全市场撤退。

建议新增工具：

- `get_market_capital_flow`
- `get_margin_financing_snapshot`
- `get_etf_flow_snapshot`

### 2.7 账户与组合：缺口中

已有能力：

- `get_portfolio_snapshot` 能返回账户摘要、持仓和风险块。

不足：

- 账户风险目前更偏仓位、回撤、止损，缺少“事件冲击下的组合压力测试”。
- 缺少行业/主题/因子暴露，例如持仓是否集中在半导体、军工、出口链、人民币贬值受损资产。
- 缺少“如果系统性恐慌发生，哪些持仓应优先处理”的排序。

影响：

- 遇到战争、汇率、政策、流动性冲击时，系统难以把外部事件映射到用户具体持仓。

建议新增工具：

- `stress_test_portfolio_by_event`
- `get_portfolio_factor_exposure`
- `rank_positions_by_event_sensitivity`

### 2.8 知识图谱：缺口中

已有能力：

- `search_knowledge_graph` 可以查历史分析结论和事件关系。
- `agent-trace` 已经接入 Graphiti 入图链路。

不足：

- 图谱目前更像“分析记忆”，不是“实时事件数据库”。
- 如果外部事件没有先被工具扫描并入库，图谱无法凭空知道战争或政策变化。
- 图谱 ontology 还偏股票、板块、市场事件和分析结论，缺少地缘冲突、宏观政策、商品、汇率、避险资产等实体类型。

影响：

- 图谱能增强复盘和历史关联，但不能替代实时风险事件扫描。

建议新增实体：

- `GeopoliticalEvent`
- `MacroPolicyEvent`
- `Commodity`
- `Currency`
- `SafeHavenAsset`
- `RiskSentimentRegime`

## 3. 最应该补的情绪面工具

### 3.1 工具一：`get_market_sentiment_snapshot`

定位：给 Agent 一个“当前市场情绪总览”，用于所有入场、选股、风险复查任务的前置风控。

建议参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `region` | string | `cn` / `hk` / `us` / `global` |
| `lookback_hours` | integer | 默认 24，最大 168 |
| `include_news` | boolean | 是否返回代表性新闻 |

建议返回：

```json
{
  "status": "ok",
  "region": "global",
  "lookback_hours": 24,
  "risk_appetite": "risk_on | neutral | risk_off | panic",
  "sentiment_score": 0,
  "panic_score": 0,
  "drivers": [
    {
      "type": "geopolitical | macro | policy | liquidity | earnings | commodity | fx",
      "title": "",
      "impact": "positive | negative | mixed | unknown",
      "severity": "low | medium | high | critical",
      "affected_markets": ["cn", "hk", "us"],
      "affected_sectors": [],
      "evidence": []
    }
  ],
  "cross_asset": {
    "gold": {"direction": "up|down|flat|unknown"},
    "oil": {"direction": "up|down|flat|unknown"},
    "usd_cnh": {"direction": "up|down|flat|unknown"},
    "us_10y": {"direction": "up|down|flat|unknown"},
    "vix": {"direction": "up|down|flat|unknown"}
  },
  "action_constraints": [
    "risk_off 或 panic 时，entry_analysis 默认降低动作强度",
    "战争/制裁类事件未澄清前，周期和出口链需额外审查"
  ],
  "missing_evidence": []
}
```

这个工具不直接给股票结论，只给市场情绪状态和动作约束。它应该进入 `regime_detection` 和 `risk_assessment`，并在 `entry_analysis`、`watchlist_scan` 中作为默认前置工具。

### 3.2 工具二：`scan_geopolitical_risk_news`

定位：专门扫描战争、冲突、制裁、恐怖袭击、航运中断、能源供给冲击等事件。

建议关键词域：

- 战争/军事：战争、冲突、袭击、导弹、空袭、停火、军事演习、边境冲突。
- 地缘政治：制裁、禁运、外交危机、断交、领海、台海、中东、俄乌、红海。
- 供应链：航运中断、港口关闭、油气管道、芯片出口管制、关键矿产限制。
- 避险资产：黄金、美元、日元、美债、原油、天然气。

建议返回：

```json
{
  "status": "ok",
  "event_count": 3,
  "highest_severity": "high",
  "events": [
    {
      "title": "",
      "event_type": "war | sanction | shipping_disruption | energy_shock | diplomatic_crisis",
      "region": "",
      "published_at": "",
      "severity": "low | medium | high | critical",
      "market_impact": {
        "risk_appetite": "negative",
        "benefit_sectors": ["黄金", "军工", "油气"],
        "pressure_sectors": ["航空", "出口链", "高估值成长"]
      },
      "confidence": "low | medium | high",
      "sources": []
    }
  ],
  "summary": "",
  "missing_evidence": []
}
```

### 3.3 工具三：`get_cross_asset_risk_signals`

定位：避免只看股票市场。恐慌通常先体现在黄金、原油、美元、离岸人民币、美债收益率、VIX 或股指期货中。

建议覆盖：

- 黄金：避险需求。
- 原油/天然气：战争、供给冲击、通胀压力。
- 美元指数 / USD-CNH：外资风险偏好和人民币资产压力。
- 美债收益率：利率预期和成长股估值压力。
- VIX / 股指期货：美股风险偏好。
- A50 / 恒生科技期货：A/H 股开盘前风险提示。

建议返回：

```json
{
  "status": "ok",
  "risk_signal": "calm | watch | risk_off | panic",
  "signals": [
    {
      "asset": "gold",
      "move": "up",
      "change_pct": null,
      "interpretation": "避险资产走强"
    }
  ],
  "market_readthrough": {
    "cn": "negative|neutral|positive|unknown",
    "hk": "negative|neutral|positive|unknown",
    "us": "negative|neutral|positive|unknown"
  }
}
```

## 4. 对现有 Prompt / Planner 的影响

当前 `planning_prompts.py` 已经写了 `sentiment_analysis` 和 `regime_detection` 这些能力域，但实际 ToolRegistry 里没有对应的专门工具。现在的映射更偏：

- `news_event` -> `search_comprehensive_intel` / `search_stock_news`
- `regime_detection` -> `get_market_indices` / `get_sector_rankings` / 技术工具

问题在于：Prompt 层已经知道“需要情绪”，执行层却没有“情绪工具”。这会导致模型只能用新闻搜索临时补位，质量不稳定。

建议把能力域拆清楚：

| 能力域 | 当前映射 | 建议映射 |
| --- | --- | --- |
| `news_event` | 个股新闻和综合搜索 | 保持不变，聚焦个股/行业/公告 |
| `sentiment_analysis` | 目前无独立工具 | `get_market_sentiment_snapshot`、`score_news_sentiment` |
| `geopolitical_risk` | 目前无独立工具 | `scan_geopolitical_risk_news` |
| `cross_asset_risk` | 目前无独立工具 | `get_cross_asset_risk_signals` |
| `regime_detection` | 指数、板块、技术 | 增加市场情绪、跨资产、宏观事件 |

## 5. 优先级路线图

### P0：先补市场情绪快照

新增 `get_market_sentiment_snapshot`。

原因：

- 对 `entry_analysis`、`watchlist_scan`、`risk_review` 都有通用价值。
- 能直接解决“战争新闻导致市场恐慌但工具看不到”的问题。
- 初版可以复用现有 `SearchService`，不需要立即引入新数据源。

最小实现：

- 用固定查询词搜索最近 24 小时全市场风险新闻。
- 对标题和摘要做规则打分：战争、制裁、袭击、暴跌、恐慌、避险、黄金、原油、美元、汇率等。
- 返回 `risk_appetite`、`panic_score`、`drivers`、`action_constraints`。
- 工具失败时返回 `status=failed`，不得让前端显示 OK。

### P1：补地缘风险专用扫描

新增 `scan_geopolitical_risk_news`。

原因：

- 战争、制裁、航运、能源属于高冲击低频事件，不能混在普通新闻搜索里。
- 可以直接影响候选池：军工/黄金/油气受益，航空/旅游/出口链/高估值成长承压。

### P1：补跨资产风险信号

新增 `get_cross_asset_risk_signals`。

原因：

- 新闻可能滞后或噪声大，跨资产价格能提供市场真实投票。
- 对港股、美股和 A 股开盘前判断尤其有价值。

### P2：补事件到板块映射

新增 `map_event_to_sectors`。

原因：

- 选股不只是知道有事件，还要知道哪些板块可能受益/受损。
- 能提升候选发现质量，避免只按涨幅榜追热点。

### P2：补组合事件压力测试

新增 `stress_test_portfolio_by_event`。

原因：

- 用户真正需要的是“这件事对我的持仓有什么影响”。
- 这能把情绪面从泛泛风险提示落到具体持仓动作。

## 6. 推荐的工具接入顺序

建议按这个顺序接入：

1. `src/agent/tools/sentiment_tools.py`
   - `get_market_sentiment_snapshot`
   - `scan_geopolitical_risk_news`
   - `get_cross_asset_risk_signals`

2. `src/agent/factory.py`
   - 注册 `ALL_SENTIMENT_TOOLS`

3. `src/agent/planner.py`
   - 给 `sentiment_analysis`、`geopolitical_risk`、`cross_asset_risk` 增加工具映射
   - `entry_analysis` 和 `watchlist_scan` 在没有明确市场平稳证据时默认加入 `sentiment_analysis`

4. `src/agent/planning_prompts.py`
   - 把战争、制裁、航运中断、能源冲击写进 `risk_assessment` 和 `regime_detection`
   - 要求 `panic` / `risk_off` 时降低开仓动作强度

5. `src/agent/stock_selection_prompts.py`
   - 候选发现阶段加入市场情绪限制
   - 组合配置阶段加入系统性风险下的仓位上限
   - 反方审查阶段必须检查外部冲击

6. Graphiti ontology
   - 增加 `GeopoliticalEvent`、`MacroPolicyEvent`、`RiskSentimentRegime`
   - 将情绪快照和事件扫描结果作为 episode 入库

## 7. 验收标准

新增情绪面工具后，至少要满足以下行为：

1. 用户问“今天能不能买”“帮我选几只”时，Agent 会先检查市场情绪或在计划中说明为什么不需要。
2. 如果最近存在高严重度战争/制裁/冲突新闻，最终输出必须出现系统性风险提示。
3. `risk_off` 或 `panic` 状态下，未持仓开仓建议必须降级为 `wait` / `monitor`，除非有非常强的反向证据。
4. 选股报告必须区分“受益于避险情绪”和“基本面/技术面真正强势”。
5. 工具返回 `error` / `failed` / `tool_failed` 时，前端 Trace 不得显示 OK。
6. 情绪工具的输出要能进入 Graphiti，后续同类事件可以被 `search_knowledge_graph` 检索到。

## 8. 数据源可行性分析

每个建议新增的工具都需要数据支撑。下面逐一分析现有数据源能覆盖多少、缺口在哪、怎么补。

### 8.1 现有数据源能力盘点

| 数据源 | 优先级 | 覆盖范围 | 跨资产能力 |
| --- | --- | --- | --- |
| EfinanceFetcher | 0 | A 股行情、指数、板块 | 无 |
| TushareFetcher | 0 | A 股行情、指数 | 无 |
| AkshareFetcher | 1 | A 股行情、板块成分、宏观数据 | **AkShare 有商品期货、外汇、宏观经济数据接口** |
| PytdxFetcher | 2 | A 股行情 | 无 |
| BaostockFetcher | 3 | A 股历史 | 无 |
| YfinanceFetcher | 4 | 美股/港股/全球指数 | **可获取 GC=F(黄金)、CL=F(原油)、^VIX、DX-Y.NYB(美元指数)、^TNX(10Y美债)** |
| LongbridgeFetcher | 5 | 美股/港股 | 无 |
| TickFlowFetcher | 99 | A 股指数增强 | 无 |

### 8.2 工具 → 数据源映射

| 工具 | 所需数据 | 可用数据源 | 缺口 |
| --- | --- | --- | --- |
| `get_market_sentiment_snapshot` | 新闻标题+摘要、指数涨跌 | SearchService（新闻）+ get_market_indices（指数） | 需要新增规则打分逻辑，不需要新数据源 |
| `scan_geopolitical_risk_news` | 地缘/战争/制裁相关新闻 | SearchService（固定关键词搜索） | 不需要新数据源，需要固定查询词库 |
| `get_cross_asset_risk_signals` | 黄金、原油、美元、VIX、美债收益率 | **YfinanceFetcher**（^VIX 已在 US 指数列表中）；AkShare 有商品期货接口 | 需要封装 yfinance 对 GC=F/CL=F/DX-Y.NYB/^TNX 的调用；AkShare 可作为 A 股商品期货备选 |
| `get_macro_calendar` | 经济日历、央行议息、CPI/PMI | AkShare 有 `macro_*` 系列接口 | 需要封装 AkShare 宏观接口 |
| `get_rate_fx_commodity_snapshot` | 利率、汇率、商品价格 | YfinanceFetcher + AkShare | 需要新增 symbol 列表和统一返回格式 |
| `map_event_to_sectors` | 事件-板块映射 | 无现成数据源，需要 LLM 推理或维护映射表 | 需要新建映射表或用 LLM 实时推理 |
| `stress_test_portfolio_by_event` | 持仓 + 事件敏感度 | PortfolioService + 板块归属 | 需要新建"事件-板块-个股"敏感度矩阵 |
| `get_market_capital_flow` | 北向资金、融资融券 | AkShare 有 `stock_hsgt_*`（北向）和 `stock_margin_*`（融资融券）接口 | 需要封装 AkShare 接口 |

### 8.3 关键结论

**P0 工具（市场情绪快照 + 地缘风险扫描）不需要新数据源**——复用现有 SearchService 即可实现最小版本。

**P1 工具（跨资产风险信号）需要小幅扩展 YfinanceFetcher**——增加对商品/外汇/债券 symbol 的支持。YfinanceFetcher 已经存在且能处理任意 Yahoo Finance symbol，只需要新增一个 `get_cross_asset_quotes(symbols: list)` 方法。

**P2 工具（宏观日历、资金面）需要封装 AkShare 的宏观和资金接口**——AkShare 已经是项目依赖，接口存在但未被 Agent 工具层使用。

### 8.4 YfinanceFetcher 跨资产扩展方案

YfinanceFetcher 当前只用于美股/港股个股和指数。但 yfinance 本身支持任意 Yahoo Finance symbol：

```python
# 已验证可用的跨资产 symbol
CROSS_ASSET_SYMBOLS = {
    "gold": "GC=F",           # COMEX 黄金期货
    "oil": "CL=F",            # WTI 原油期货
    "natural_gas": "NG=F",    # 天然气期货
    "usd_index": "DX-Y.NYB", # 美元指数
    "usd_cnh": "CNY=X",      # 美元/离岸人民币
    "us_10y": "^TNX",         # 10 年期美债收益率
    "vix": "^VIX",            # VIX 恐慌指数
    "a50_futures": "XIN9.FGI", # 富时 A50 期货（可能延迟）
    "hsi_futures": "HSI=F",   # 恒生指数期货
}
```

实现成本低：在 YfinanceFetcher 中新增一个方法，批量拉取这些 symbol 的最新价格和涨跌幅即可。

### 8.5 AkShare 宏观/资金接口盘点

AkShare 已有但未被 Agent 工具使用的接口：

| 接口 | 用途 | 对应工具 |
| --- | --- | --- |
| `stock_hsgt_north_net_flow_in_em` | 北向资金净流入 | `get_market_capital_flow` |
| `stock_margin_sse` / `stock_margin_szse` | 融资融券余额 | `get_margin_financing_snapshot` |
| `macro_china_cpi` / `macro_china_pmi` | 中国宏观数据 | `get_macro_calendar` |
| `macro_usa_cpi` / `macro_usa_nfp` | 美国宏观数据 | `get_macro_calendar` |
| `futures_main_sina` | 商品期货主力合约 | `get_cross_asset_risk_signals` 备选 |

## 9. 工具调用时机与策略

### 9.1 调用模式：条件触发 vs 始终调用

情绪面工具不应该每次分析都调用——单股分析时如果市场平稳，调用情绪工具只会浪费 token 和延迟。

| 场景 | 调用策略 | 原因 |
| --- | --- | --- |
| 选股（`watchlist_scan`） | **始终调用** `get_market_sentiment_snapshot` | 选股必须先判断市场环境是否适合开仓 |
| 入场分析（`entry_analysis`） | **始终调用** `get_market_sentiment_snapshot` | 入场决策必须考虑系统性风险 |
| 持仓复查（`position_review`） | **条件触发**：指数跌幅 > 1.5% 或前次情绪为 risk_off/panic | 持仓复查更关注个股，除非市场异常 |
| 单股分析（`normal`） | **条件触发**：指数跌幅 > 2% 或用户明确问"市场风险" | 日常单股分析不需要每次都查情绪 |
| 风险复查（`risk_review`） | **始终调用** 全部 3 个情绪工具 | 风险复查的核心就是外部冲击 |

### 9.2 Planner 触发逻辑

在 `src/agent/planner.py` 中，Planner 决定本次分析需要哪些能力域。建议增加以下触发规则：

```python
# 伪代码：Planner 决策逻辑
def should_include_sentiment(intent, market_context):
    # 始终触发
    if intent in ("watchlist_scan", "entry_analysis", "risk_review"):
        return True
    # 条件触发
    if market_context and market_context.get("index_change_pct", 0) < -1.5:
        return True
    if market_context and market_context.get("last_sentiment") in ("risk_off", "panic"):
        return True
    # 用户意图触发
    if user_mentions_risk_keywords(user_message):
        return True
    return False
```

### 9.3 批量分析时的调用优化

批量分析 20 只股票时，情绪快照只需要调用一次：

- `get_market_sentiment_snapshot` → 在 Pipeline.run() 开始时调用一次，结果注入所有个股分析的 context
- `scan_geopolitical_risk_news` → 同上，一次调用，结果共享
- `get_cross_asset_risk_signals` → 同上，一次调用

个股级别的工具（`search_stock_news`、`get_capital_flow` 等）仍然每只股票单独调用。

### 9.4 Token 成本估算

| 工具 | 预估输入 token | 预估输出 token | 调用频率 |
| --- | --- | --- | --- |
| `get_market_sentiment_snapshot` | 0（规则打分，不调 LLM） | 返回 JSON ~500 token | 每轮分析 1 次 |
| `scan_geopolitical_risk_news` | SearchService 调用（不额外消耗 LLM token） | 返回 JSON ~800 token | 每轮分析 1 次 |
| `get_cross_asset_risk_signals` | 0（直接拉行情数据） | 返回 JSON ~400 token | 每轮分析 1 次 |

**结论**：3 个 P0/P1 工具本身不调用 LLM，只消耗搜索 API 和行情 API。对 Agent 的 token 成本影响仅在于返回结果被注入 context（约 1700 token/轮），可接受。

## 10. 时效性与缓存设计

### 10.1 缓存策略

| 工具 | 缓存 TTL | 原因 |
| --- | --- | --- |
| `get_market_sentiment_snapshot` | **30 分钟** | 情绪变化不会秒级波动，30 分钟内复用合理 |
| `scan_geopolitical_risk_news` | **60 分钟** | 地缘事件不会分钟级变化，但不能太长以免错过升级 |
| `get_cross_asset_risk_signals` | **15 分钟**（交易时段）/ **60 分钟**（非交易时段） | 交易时段价格变化快，非交易时段可以放宽 |
| `get_macro_calendar` | **6 小时** | 经济日历一天更新一次 |
| `get_market_capital_flow` | **30 分钟** | 北向资金盘中有波动但不需要实时 |

### 10.2 缓存实现位置

建议在 `GraphService` 或新建 `src/services/sentiment_cache.py` 中实现：

```python
# 缓存 key 设计
cache_key = f"{tool_name}:{region}:{lookback_hours}"
# 例如：get_market_sentiment_snapshot:global:24

# 缓存存储
# 方案 A：内存 dict + TTL（简单，进程重启丢失）
# 方案 B：SQLite 表（持久化，可跨进程）
# 建议先用方案 A，后续按需升级
```

### 10.3 批量分析时的缓存行为

Pipeline.run() 分析 20 只股票时：
1. 第一只股票触发 `get_market_sentiment_snapshot`，结果写入缓存
2. 后续 19 只股票命中缓存，零延迟
3. 如果分析耗时超过 30 分钟，缓存过期，下一只股票会重新拉取

### 10.4 缓存失效条件

除了 TTL 过期，以下情况应主动失效缓存：

- 指数出现 > 3% 的急跌（可能有突发事件）
- 用户手动触发"刷新市场状态"
- 新的地缘事件被 Graphiti 入图（说明有新信息）

## 11. 流动性风险维度（补充缺口）

### 11.1 问题

文档覆盖了地缘、宏观、情绪、资金面，但没有单独提流动性风险。流动性不足时：
- 再好的技术形态也可能是陷阱（无法出货）
- 选股候选如果流动性不足，实际无法建仓
- 节假日前后、极端行情时流动性会骤降

### 11.2 现有能力

- `get_realtime_quote` 返回成交额、换手率
- `get_volume_analysis` 返回量能状态
- 选股 Prompt 1 已有"换手率不低于 3%，量比大于 1"的硬编码阈值

### 11.3 缺口

- 没有全市场流动性状态（今天全市场成交额是多少？是否萎缩到冰点？）
- 没有流动性季节性判断（节前效应、长假前缩量）
- 港股个股流动性极端分化，但没有专门的流动性筛选
- 没有"流动性冲击预警"（某只股票突然放巨量可能是出货信号）

### 11.4 建议新增

**工具**：`get_market_liquidity_status`

```json
{
  "status": "ok",
  "region": "cn",
  "total_amount_today": 8500,
  "total_amount_unit": "亿元",
  "vs_5d_avg": -0.15,
  "vs_20d_avg": -0.25,
  "liquidity_level": "normal | shrinking | frozen | surging",
  "seasonal_note": "节前第3个交易日，历史平均缩量15%",
  "action_constraint": "流动性萎缩时，小盘股候选需额外审查成交额"
}
```

**数据源**：`get_market_indices` 已返回全市场成交额（A 股的 market_stats 有 total_amount）。只需要加历史对比逻辑。

**优先级**：P2，在 P0/P1 情绪工具之后。

## 12. 工具失败降级链设计

### 12.1 设计原则

- 工具失败不能让 Agent 静默忽略风险
- 降级后必须在输出中标注"该维度证据缺失"
- 多个工具同时失败时，应主动降低动作强度

### 12.2 逐工具降级链

| 工具 | 失败时降级方案 | 降级后的动作约束 |
| --- | --- | --- |
| `get_market_sentiment_snapshot` | 用 `get_market_indices` 的指数涨跌幅做简单判断：跌 > 2% 视为 risk_off | 标注"情绪判断基于指数涨跌，未经新闻验证" |
| `scan_geopolitical_risk_news` | 用 `search_comprehensive_intel` 搜索"战争 制裁 冲突 袭击"关键词 | 标注"地缘风险扫描降级为通用搜索，覆盖面有限" |
| `get_cross_asset_risk_signals` | 用 `get_market_indices(region="us")` 获取 VIX（已在 US 指数列表中） | 标注"跨资产信号仅有 VIX，缺少商品/外汇/债券验证" |
| `get_market_capital_flow` | 用 `get_capital_flow` 的板块资金流做近似 | 标注"全市场资金面缺失，仅有板块级数据" |
| `get_macro_calendar` | 无降级，标注"宏观日历不可用" | 不影响技术面判断，但基本面结论需降低置信度 |

### 12.3 多工具同时失败的处理

```python
# 伪代码：多工具失败时的动作约束
failed_tools = [t for t in sentiment_tools if t.status == "failed"]

if len(failed_tools) >= 2:
    # 多个情绪工具失败，无法判断市场环境
    action_constraints.append(
        "多个市场环境工具不可用，无法确认系统性风险状态。"
        "建议保守处理：entry_analysis 降级为 WAIT/MONITOR，"
        "watchlist_scan 降低候选数量和首仓比例。"
    )
    max_action_strength = "weak"  # 强制降低动作强度
```

### 12.4 降级信息的传递

降级信息必须出现在最终输出中，不能被 Agent 吞掉：

- 在 `evidence_ledger` 中标注 `tool_failed` 或 `degraded`
- 在选股报告的"需要补充的信息"部分列出
- 在反方审查（Prompt 5）中作为"证据缺口"被引用

## 13. 选股 Prompt 接口契约

### 13.1 新增输入变量

选股 prompt 链路需要新增以下共享输入变量：

```text
{{market_sentiment}}          市场情绪快照（get_market_sentiment_snapshot 返回值）
{{geopolitical_risks}}        地缘风险事件列表（scan_geopolitical_risk_news 返回值）
{{cross_asset_signals}}       跨资产风险信号（get_cross_asset_risk_signals 返回值）
{{market_liquidity}}          市场流动性状态（get_market_liquidity_status 返回值）
{{sentiment_tool_status}}     情绪工具调用状态：all_ok / partial / all_failed
```

### 13.2 Prompt 1（候选发现）的情绪约束

在 Prompt 1 的"执行规则"中新增：

```text
8. 市场情绪约束：
   - 如果 {{market_sentiment}}.risk_appetite == "panic"：
     直接输出 INSUFFICIENT_CANDIDATES，附原因"市场处于恐慌状态，不适合主动选股"。
   - 如果 {{market_sentiment}}.risk_appetite == "risk_off"：
     candidate_strategy 强制限制为 value_quality 或 low_risk_income，
     不得使用 hot_sector 或 growth_turnaround。
   - 如果 {{geopolitical_risks}}.highest_severity == "critical"：
     候选池必须排除受冲击板块（从 events[].market_impact.pressure_sectors 获取）。
   - 如果 {{sentiment_tool_status}} == "all_failed"：
     候选数量上限减半，首仓比例建议不超过 3%。
```

### 13.3 Prompt 2（候选初筛）的情绪评分

在 Prompt 2 的"评分维度"中新增：

```text
- 市场环境适配：
  - 候选股票是否属于当前 risk_appetite 下的合理方向。
  - 如果 risk_off，防御性板块（公用事业、医药、黄金）加分，进攻性板块（科技、新能源）减分。
  - 如果有地缘事件，受益板块（军工、黄金、油气）加分，受损板块（航空、旅游、出口链）减分。
```

### 13.4 Prompt 4（组合配置）的仓位约束

在 Prompt 4 的"配置规则"中新增：

```text
8. 市场情绪仓位约束：
   - risk_appetite == "panic"：不建仓，输出"本轮不建仓"。
   - risk_appetite == "risk_off"：总首仓比例不超过可用现金的 30%，单票不超过 5%。
   - risk_appetite == "neutral"：正常配置规则。
   - risk_appetite == "risk_on"：正常配置规则，可适当放宽。
   - 如果 cross_asset_signals.risk_signal == "panic"：覆盖上述规则，强制不建仓。
```

### 13.5 Prompt 5（反方审查）的情绪检查

在 Prompt 5 的"反方必须检查"中新增：

```text
9. 市场情绪是否被低估：
   - 原方案是否在 risk_off/panic 环境下仍然建议 OPEN。
   - 是否存在地缘事件未被原方案考虑。
   - 跨资产信号是否与原方案的乐观结论矛盾（例如 VIX 飙升但原方案仍建议买入）。
   - 情绪工具是否有失败/降级，原方案是否忽略了证据缺口。
10. 流动性是否被忽视：
   - 候选股票的成交额是否足以支撑建仓（日成交额 > 预计建仓金额的 10 倍）。
   - 全市场流动性是否处于萎缩状态。
```

### 13.6 接口契约总结

```
选股链路数据流：

[情绪工具层]
  get_market_sentiment_snapshot → {{market_sentiment}}
  scan_geopolitical_risk_news   → {{geopolitical_risks}}
  get_cross_asset_risk_signals  → {{cross_asset_signals}}
  get_market_liquidity_status   → {{market_liquidity}}
      ↓
[Prompt 1: 候选发现]
  输入：上述 4 个变量 + 原有变量
  约束：panic 不选股，risk_off 限制策略，排除受冲击板块
      ↓
[Prompt 2: 候选初筛]
  输入：候选池 + 情绪变量
  评分：新增"市场环境适配"维度
      ↓
[Prompt 3: 单股深度分析]
  输入：单股数据 + 情绪变量（作为背景参考）
  约束：risk_off 时 action_strength 不得为 strong
      ↓
[Prompt 4: 组合配置]
  输入：深度分析结果 + 情绪变量
  约束：按 risk_appetite 限制总仓位和单票比例
      ↓
[Prompt 5: 反方审查]
  输入：全部前序结果 + 情绪变量
  检查：情绪是否被低估、证据缺口是否被忽略
```

## 14. 结论

当前工具已经能支撑单股分析和基础选股，但缺少一个关键层：全市场情绪与外部冲击层。

这个缺口会在以下场景明显影响质量：

- 战争、制裁、地缘冲突升级。
- 原油、黄金、美元、离岸人民币出现异常波动。
- 市场从结构性行情突然切到系统性避险。
- 用户要求选股，但市场环境已经不适合主动开仓。
- 用户持仓暴露在出口链、能源、军工、黄金、半导体管制等事件敏感方向。

下一步最值得做的是先实现 `get_market_sentiment_snapshot`，再补 `scan_geopolitical_risk_news` 和 `get_cross_asset_risk_signals`。这三类工具会把 Agent 从“个股分析器”提升为“能判断市场是否适合交易的账户级助手”。
