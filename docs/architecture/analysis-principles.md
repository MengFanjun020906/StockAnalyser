# 股票分析原理与信号生成机制

本文档解释 daily_stock_analysis 当前分析系统的工作原理，重点说明：

- 系统接收什么输入
- 会调用哪些数据源、工具和模型
- 各类分析维度如何进入上下文
- 确定性技术信号如何打分
- LLM / Agent 如何把数据转成最终决策
- 最终输出结构是什么
- 哪些配置会改变分析行为
- 数据缺失、接口失败、模型失败时如何降级

本文面向想理解项目内部逻辑、二次开发、调参或排查报告结论来源的用户。涉及的主要代码入口包括：

- `main.py`：命令行、定时、Web 服务启动入口
- `src/core/pipeline.py`：单股分析主流程
- `src/stock_analyzer.py`：确定性技术信号分析器
- `src/analyzer.py`：LLM Prompt 构造、响应解析、完整性校验
- `src/search_service.py`：新闻、公告、风险、业绩等情报搜索
- `data_provider/`：行情、日线、筹码、基本面等数据源适配
- `src/agent/`：Agent 模式、工具调用、多 Agent 编排、策略技能
- `templates/` 与 `src/notification.py`：报告渲染和推送

## 1. 总体定位

项目不是单纯的“把股票代码丢给大模型问一句”的系统，而是一个多阶段分析流水线：

```text
用户输入股票列表/任务参数
  -> 加载配置和数据源
  -> 获取历史行情、实时行情、筹码、基本面、资金、板块、新闻舆情
  -> 用确定性规则生成技术趋势信号
  -> 构造结构化 Prompt 或 Agent 上下文
  -> 调用 LLM 生成决策仪表盘 JSON
  -> 校验、补全、保存历史记录
  -> 生成 Markdown/通知内容/API 响应/Web 展示
```

系统内部同时存在两类判断：

1. **确定性计算**：均线、乖离率、量能、MACD、RSI、支撑压力、趋势评分等由代码直接计算，不依赖 LLM 主观判断。
2. **综合解释与决策生成**：LLM 或 Agent 基于确定性结果、实时行情、筹码、基本面、新闻舆情和策略约束，生成面向用户的买卖建议、止损位、目标位、风险提示和行动清单。

因此最终报告里的“买入/持有/卖出”不是单一字段直接决定，而是由确定性技术分、消息面、基本面、资金面、策略技能和风控要求共同影响。

## 2. 输入是什么

### 2.1 最小输入

最小输入来自 `.env` 或运行参数：

```env
STOCK_LIST=600519,300750,002594
GEMINI_API_KEY=...
# 或 DEEPSEEK_API_KEY / AIHUBMIX_KEY / OPENAI_API_KEY / LLM_CHANNELS / LITELLM_CONFIG
```

`STOCK_LIST` 是实际分析范围。股票代码支持 A 股、港股、美股，常见写法包括：

- A 股：`600519`、`000001`、`300750`
- 港股：`hk00700`
- 美股：`AAPL`、`TSLA`

### 2.2 命令行输入

常见运行方式：

```bash
python main.py
python main.py --dry-run
python main.py --stocks 600519,hk00700,AAPL
python main.py --market-review
python main.py --schedule
python main.py --serve
python main.py --serve-only
```

其中：

- `--stocks` 会覆盖环境变量里的股票列表。
- `--market-review` 会运行大盘复盘。
- `--schedule` 会进入定时模式。
- `--serve` / `--serve-only` 会启动 Web/API 服务。

### 2.3 Web / API 输入

Web 前端和 API 通常会提交：

- 股票代码
- 报告类型：`simple` / `full` / `brief`
- 是否启用 Agent
- 当前配置项
- 任务 ID / 查询链路 ID

后端会把这些输入进入 `StockAnalysisPipeline`，并通过任务队列、SSE 进度流和历史记录表维护状态。

### 2.4 配置输入

配置对分析结果影响很大，主要包括：

| 配置类别 | 关键变量 | 影响 |
| --- | --- | --- |
| 股票范围 | `STOCK_LIST` | 决定分析哪些标的 |
| LLM | `GEMINI_API_KEY`、`DEEPSEEK_API_KEY`、`AIHUBMIX_KEY`、`OPENAI_API_KEY`、`LLM_CHANNELS`、`LITELLM_CONFIG` | 决定使用哪个模型生成结论 |
| 新闻搜索 | `ANSPIRE_API_KEYS`、`BOCHA_API_KEYS`、`TAVILY_API_KEYS`、`SERPAPI_API_KEYS`、`BRAVE_API_KEYS`、`SEARXNG_BASE_URLS` | 决定消息面、公告、风险、业绩预期质量 |
| 行情数据 | `TUSHARE_TOKEN`、`LONGBRIDGE_*`、`REALTIME_SOURCE_PRIORITY`、`YFINANCE_PRIORITY` 等 | 决定日线、实时行情和估值字段来源 |
| 技术阈值 | `BIAS_THRESHOLD` | 决定乖离率多高算追高，默认 5% |
| 新闻窗口 | `NEWS_STRATEGY_PROFILE`、`NEWS_MAX_AGE_DAYS` | 决定近期新闻有效窗口 |
| 筹码 | `ENABLE_CHIP_DISTRIBUTION` | 是否启用筹码分布 |
| 基本面 | `ENABLE_FUNDAMENTAL_PIPELINE`、`FUNDAMENTAL_*` | 是否聚合估值、财报、资金、板块等基本面上下文 |
| Agent | `AGENT_MODE`、`AGENT_ARCH`、`AGENT_SKILLS`、`AGENT_ORCHESTRATOR_MODE` | 是否使用工具调用/多 Agent/策略技能 |
| 输出 | `REPORT_TYPE`、`REPORT_LANGUAGE`、`SINGLE_STOCK_NOTIFY` | 决定报告长度、语言和推送方式 |

## 3. 分析链路总览

普通单股分析入口在 `src/core/pipeline.py` 的 `analyze_stock()`。主链路按以下顺序执行：

1. 获取股票名称
2. 获取实时行情
3. 获取筹码分布
4. 判断是否进入 Agent 模式
5. 聚合基本面上下文
6. 计算确定性趋势分析结果
7. 搜索新闻、公告、风险、业绩、行业情报
8. 美股可选补充社交舆情
9. 从数据库读取历史技术上下文
10. 把实时行情、筹码、趋势、基本面合并进上下文
11. 调用 LLM 或 Agent 生成决策仪表盘
12. 校验、补全、保存历史记录

简化流程：

```text
StockAnalysisPipeline.analyze_stock()
  -> DataFetcherManager.get_realtime_quote()
  -> DataFetcherManager.get_chip_distribution()
  -> DataFetcherManager.get_fundamental_context()
  -> StockTrendAnalyzer.analyze()
  -> SearchService.search_comprehensive_intel()
  -> DatabaseManager.get_analysis_context()
  -> GeminiAnalyzer._format_prompt()
  -> LLM completion
  -> GeminiAnalyzer._parse_response()
  -> DatabaseManager.save_analysis_history()
```

如果启用 Agent：

```text
StockAnalysisPipeline.analyze_stock()
  -> 前置行情/筹码/基本面/趋势
  -> build_agent_executor()
  -> Agent 调用工具
  -> Agent 输出决策仪表盘 JSON
  -> 转换为 AnalysisResult
  -> 保存历史
```

## 4. 分析维度分类

当前报告大致按以下维度综合：

| 大类 | 具体内容 | 主要来源 |
| --- | --- | --- |
| 技术面 | K 线、MA5/MA10/MA20/MA60、趋势状态、乖离率、MACD、RSI、支撑压力、形态 | 历史日线 + `StockTrendAnalyzer` |
| 量价面 | 成交量、成交额、量比、换手率、昨日成交量对比、实时涨跌幅 | 实时行情 + 历史行情 |
| 筹码面 | 获利比例、平均成本、70%/90% 筹码集中度、筹码健康状态 | 筹码分布接口 |
| 基本面 | PE、PB、市值、营收、净利润、经营现金流、ROE、分红、股息率 | `get_fundamental_context()` |
| 资金面 | 主力净流入、5日/10日流入、龙虎榜、板块资金排行 | 基本面聚合和 Agent 工具 |
| 板块/行业面 | 所属板块、板块涨跌榜、行业位置、主题热点 | 板块接口 + 搜索情报 |
| 消息面 | 最新新闻、公告、政策、合同、业绩预告、处罚、减持、诉讼 | `SearchService` |
| 舆情面 | 新闻情绪、美股 Reddit/X/Polymarket 社交情绪 | 搜索服务 + `SocialSentimentService` |
| 策略面 | 多头趋势、均线金叉、放量突破、缩量回踩、龙头、缠论、波浪等策略技能 | Agent Skill / `strategies/*.yaml` |
| 风控面 | 止损、仓位、目标位、操作检查清单、风险优先级 | LLM / Agent 决策层 |

## 5. 数据源与工具

### 5.1 行情数据源

行情统一通过 `DataFetcherManager` 管理。不同市场和字段会走不同 provider，并具备 fallback 能力。

常见数据源包括：

- efinance / 东方财富
- AkShare
- Tushare
- Pytdx
- Baostock
- YFinance
- Longbridge

数据源优先级可以通过环境变量调整，例如：

```env
EFINANCE_PRIORITY=0
AKSHARE_PRIORITY=1
TUSHARE_PRIORITY=2
YFINANCE_PRIORITY=4
REALTIME_SOURCE_PRIORITY=tencent,akshare_sina,efinance,akshare_em
```

实时行情常见字段：

- 当前价格
- 涨跌幅
- 成交量
- 成交额
- 量比
- 换手率
- PE
- PB
- 总市值
- 流通市值
- 60 日涨跌幅

如果实时行情失败，普通分析不会直接失败，会降级为历史收盘价继续分析。

### 5.2 历史日线

历史日线用于计算：

- 今日/昨日行情对比
- 均线
- MACD
- RSI
- 量价变化
- 支撑压力
- 形态识别

Agent 工具 `get_daily_history` 会优先读数据库缓存，缓存不足时再调用数据源，并把新数据回写数据库，减少重复网络请求。

### 5.3 筹码分布

筹码分布字段包括：

- `profit_ratio`：获利比例
- `avg_cost`：平均成本
- `cost_90_low` / `cost_90_high`
- `concentration_90`：90% 筹码集中度
- `cost_70_low` / `cost_70_high`
- `concentration_70`：70% 筹码集中度

筹码用于辅助判断：

- 当前价上方是否有大量套牢盘
- 获利盘是否过重
- 平均成本与现价关系
- 拉升阻力是否大
- 筹码是否集中

筹码接口不稳定，云端部署可以关闭：

```env
ENABLE_CHIP_DISTRIBUTION=false
```

### 5.4 基本面聚合

基本面上下文由 `DataFetcherManager.get_fundamental_context()` 聚合，结构化为多个 block：

| block | 含义 |
| --- | --- |
| `valuation` | 估值：PE、PB、市值、流通市值 |
| `growth` | 成长性相关字段 |
| `earnings` | 财报、营收、归母净利润、现金流、ROE、分红 |
| `institution` | 机构相关信息 |
| `capital_flow` | 资金流 |
| `dragon_tiger` | 龙虎榜相关信息 |
| `boards` | 板块、行业排行 |

每个 block 都带有：

- `status`：`ok` / `partial` / `failed` / `not_supported`
- `coverage`：覆盖状态
- `source_chain`：来源链路
- `errors`：错误信息
- `data`：实际数据

基本面聚合是 fail-open 设计：失败不会阻断主分析，只会在上下文中标记缺失或失败。

相关配置：

```env
ENABLE_FUNDAMENTAL_PIPELINE=true
FUNDAMENTAL_STAGE_TIMEOUT_SECONDS=8.0
FUNDAMENTAL_FETCH_TIMEOUT_SECONDS=3.0
AGENT_CAPITAL_FLOW_TIMEOUT_SECONDS=3.0
FUNDAMENTAL_RETRY_MAX=1
FUNDAMENTAL_CACHE_TTL_SECONDS=120
FUNDAMENTAL_CACHE_MAX_ENTRIES=256
```

`FUNDAMENTAL_FETCH_TIMEOUT_SECONDS` 控制聚合上下文里单个基本面 block 的快速预算；
`AGENT_CAPITAL_FLOW_TIMEOUT_SECONDS` 只控制 Agent 显式调用 `get_capital_flow`
时的预算。资金流端点通常比估值/板块类接口慢，显式工具应保留更完整的错误诊断，
不能因为基本面聚合的短预算把数据源连接断开、权限或 DNS 问题全部压缩成 timeout。

注意：当前基本面聚合对 A 股支持更完整，港股/美股会有部分 `not_supported` 降级。

### 5.5 新闻、公告、风险与业绩情报

`SearchService.search_comprehensive_intel()` 会按多个维度搜索。

对 A 股常见维度：

| 维度 | 查询目的 |
| --- | --- |
| `latest_news` | 最新新闻、重大事件 |
| `market_analysis` | 研报、评级、目标价、深度分析 |
| `risk_check` | 减持、处罚、违规、诉讼、利空 |
| `announcements` | 公司公告、交易所公告、cninfo |
| `earnings` | 业绩预告、财报、营收、净利润 |
| `industry` | 所在行业、竞争对手、市场份额、行业前景 |

对美股/港股会改用英文查询，例如 latest news、analyst rating、earnings、lawsuit、industry competitors 等。

新闻窗口由以下配置控制：

```env
NEWS_STRATEGY_PROFILE=short
NEWS_MAX_AGE_DAYS=3
```

窗口规则：

- `ultra_short`：1 天
- `short`：3 天
- `medium`：7 天
- `long`：30 天
- 实际窗口为策略窗口和 `NEWS_MAX_AGE_DAYS` 的较小值

Prompt 强制要求：

- 输出到 `risk_alerts`、`positive_catalysts`、`latest_news` 的信息必须带具体日期
- 超出新闻窗口的新闻要忽略
- 时间未知的新闻要忽略

### 5.6 美股社交舆情

如果配置 `SOCIAL_SENTIMENT_API_KEY`，美股会额外接入 `SocialSentimentService`，补充：

- Reddit 情绪
- X/Twitter 情绪
- Polymarket 相关市场情绪

这部分只对美股 ticker 生效，A 股和港股会跳过。

### 5.7 Agent 工具清单

Agent 模式会注册工具，供 LLM 按阶段调用。主要工具包括：

| 工具 | 类型 | 作用 |
| --- | --- | --- |
| `get_realtime_quote` | data | 获取实时行情、量比、换手率、估值、市值 |
| `get_daily_history` | data | 获取日线 OHLCV 和缓存状态 |
| `get_chip_distribution` | data | 获取筹码分布；默认优先使用 Tushare `cyq_chips`，失败时返回 `status/errors/source_chain/error_summary`，区分禁用、不支持、熔断和数据源异常 |
| `get_analysis_context` | data | 获取数据库中的技术上下文 |
| `get_stock_info` | data | 获取估值、基本面、板块等压缩上下文 |
| `get_capital_flow` | data | 获取 A 股个股主力资金流；按 Tushare `moneyflow_dc`、`moneyflow_ths`、legacy `moneyflow` 顺序 failover，首个可用来源成功即返回，全部失败时回退 StockAPI `codeFlow`；未选中的 Tushare 来源后台 best-effort 审计，冲突仅写入 `warnings/source_conflicts` |
| `discover_watchlist_candidates` | market | 生成选股候选池；默认使用 AlphaSift YAML 多因子策略、Sequoia 量化策略、强势板块成分股等多路召回并统一评分，仅在无候选时使用固定种子池兜底 |
| `get_market_capital_flow` | data | 获取 A 股市场资金快照、个股/行业/概念资金流排名 |
| `get_northbound_capital_flow` | data | 获取北向资金摘要和近期历史 |
| `get_margin_trading_summary` | data | 获取融资融券余额、融资买入和交易所两融摘要 |
| `get_stockapi_limit_up_pool` | data | 获取 StockAPI 涨停股池，返回涨停原因、连板、封单、概念和板块原因 |
| `get_stockapi_hot_sectors` | data | 获取 StockAPI 热点板块/概念净流入、强度和趋势 |
| `get_stockapi_sector_constituents` | data | 按 `bkCode` 获取板块/概念成分股及个股主力资金字段 |
| `get_stockapi_sector_flow_history` | data | 按 `bkCode` 获取板块/概念历史资金流，验证资金持续性 |
| `get_stockapi_popularity_rank` | data | 获取 StockAPI 股票人气榜和 AI 原因，用于情绪/关注度候选 |
| `get_stockapi_hot_money_activity` | data | 获取 StockAPI 游资上榜或单股游资活动，用于短线资金确认 |
| `get_portfolio_snapshot` | data | 获取持仓和风险摘要 |
| `analyze_trend` | analysis | 执行确定性趋势分析 |
| `calculate_ma` | analysis | 计算自定义均线和乖离率 |
| `get_volume_analysis` | analysis | 分析量价关系、放缩量、量价背离 |
| `analyze_pattern` | analysis | 识别 K 线和形态，如十字星、吞没、双底、突破、箱体 |
| `analyze_price_structure` | analysis | 识别 Stage3 价格结构，输出缠论包含合并、分型、笔、中枢、力度、未完成笔，以及 SMC 摆动、BOS/CHoCH、OB/FVG |
| `search_stock_news` | search | 搜索单维最新新闻 |
| `search_comprehensive_intel` | search | 多维情报搜索 |
| `get_market_indices` | market | 获取主要指数 |
| `get_sector_rankings` | market | 获取同花顺行业板块热榜，优先 Tushare `ths_hot(market=行业板块)` |
| `get_skill_backtest_summary` | backtest | 获取策略/技能回测摘要 |
| `get_strategy_backtest_summary` | backtest | 获取策略回测摘要 |
| `get_stock_backtest_summary` | backtest | 获取个股历史信号验证摘要 |

工具注册位置在 `src/agent/factory.py`，通过 `ToolRegistry` 统一暴露给 LiteLLM/OpenAI-compatible tool calling。

## 6. 确定性技术信号如何产生

确定性技术分析由 `src/stock_analyzer.py` 的 `StockTrendAnalyzer` 完成。这是系统中最接近“规则引擎”的部分。

### 6.1 输入

输入是至少 20 个交易日的 OHLCV DataFrame：

| 字段 | 含义 |
| --- | --- |
| `date` | 交易日期 |
| `open` | 开盘价 |
| `high` | 最高价 |
| `low` | 最低价 |
| `close` | 收盘价 |
| `volume` | 成交量 |

如果盘中实时行情可用，`pipeline` 会用实时价格增强当天数据，使 MA 和涨跌幅更贴近盘中状态。

### 6.2 计算指标

`StockTrendAnalyzer.analyze()` 会依次计算：

1. MA5、MA10、MA20、MA60
2. MACD：DIF、DEA、MACD BAR
3. RSI：RSI6、RSI12、RSI24
4. 趋势状态
5. 乖离率
6. 量能状态
7. 支撑压力
8. MACD 状态
9. RSI 状态
10. 综合分和买卖信号

### 6.3 趋势状态

趋势状态由 MA5、MA10、MA20 的排列决定：

| 状态 | 条件 | 含义 |
| --- | --- | --- |
| 强势多头 | MA5 > MA10 > MA20，且 MA5-MA20 间距扩大并超过 5% | 强趋势上行 |
| 多头排列 | MA5 > MA10 > MA20 | 顺势偏多 |
| 弱势多头 | MA5 > MA10，但 MA10 <= MA20 | 短线转强但中期未完全确认 |
| 盘整 | 均线缠绕 | 方向不明 |
| 弱势空头 | MA5 < MA10，但 MA10 >= MA20 | 短线转弱 |
| 空头排列 | MA5 < MA10 < MA20 | 趋势偏空 |
| 强势空头 | MA5 < MA10 < MA20，且 MA20-MA5 间距扩大并超过 5% | 强趋势下行 |

趋势强度是一个 0-100 的粗评分：

- 强势多头：约 90
- 多头排列：约 75
- 弱势多头：约 55
- 盘整：约 50
- 弱势空头：约 40
- 空头排列：约 25
- 强势空头：约 10

### 6.4 乖离率

乖离率公式：

```text
bias_ma5 = (current_price - MA5) / MA5 * 100
bias_ma10 = (current_price - MA10) / MA10 * 100
bias_ma20 = (current_price - MA20) / MA20 * 100
```

项目的核心交易理念之一是“不追高”，所以 MA5 乖离率是重要输入。

默认阈值：

```env
BIAS_THRESHOLD=5.0
```

解释：

- `bias_ma5 < 0`：价格低于 MA5，可能是回踩
- `0 <= bias_ma5 < 2`：贴近 MA5，通常是较舒服的介入区
- `2 <= bias_ma5 < BIAS_THRESHOLD`：略高于 MA5，可小仓或谨慎跟踪
- `bias_ma5 > BIAS_THRESHOLD`：追高风险
- 强势多头且趋势强度高时，阈值会放宽到 `BIAS_THRESHOLD * 1.5`

### 6.5 量能状态

量能用当日成交量与过去 5 日均量比较：

```text
volume_ratio_5d = latest_volume / average_volume_last_5_days
```

阈值：

- 放量：`volume_ratio_5d >= 1.5`
- 缩量：`volume_ratio_5d <= 0.7`

再结合当日价格涨跌，得到：

| 状态 | 条件 | 解读 |
| --- | --- | --- |
| 放量上涨 | 放量 + 价格上涨 | 多头力量强 |
| 放量下跌 | 放量 + 价格下跌 | 风险较高 |
| 缩量上涨 | 缩量 + 价格上涨 | 上攻动能不足 |
| 缩量回调 | 缩量 + 价格下跌 | 可能是洗盘或健康回踩 |
| 量能正常 | 其他 | 中性 |

### 6.6 支撑压力

支撑主要看价格是否在 MA5 / MA10 附近：

```text
abs(price - MA) / MA <= 2%
且 price >= MA
```

满足时：

- MA5 附近：认为 MA5 支撑有效
- MA10 附近：认为 MA10 支撑有效
- MA20：作为更重要的中期支撑参考

压力位主要取近 20 日高点，如果近 20 日高点高于现价，则作为压力。

### 6.7 MACD 状态

MACD 使用标准 12/26/9：

```text
EMA12 = close 的 12 日指数移动平均
EMA26 = close 的 26 日指数移动平均
DIF = EMA12 - EMA26
DEA = DIF 的 9 日指数移动平均
MACD_BAR = (DIF - DEA) * 2
```

状态包括：

| 状态 | 条件 | 解读 |
| --- | --- | --- |
| 零轴上金叉 | DIF 上穿 DEA，且 DIF > 0 | 强买入信号 |
| 金叉 | DIF 上穿 DEA | 趋势向上 |
| 上穿零轴 | DIF 从负转正 | 趋势转强 |
| 多头 | DIF > 0 且 DEA > 0 | 多头延续 |
| 空头 | DIF < 0 且 DEA < 0 | 空头延续 |
| 下穿零轴 | DIF 从正转负 | 趋势转弱 |
| 死叉 | DIF 下穿 DEA | 偏空 |

### 6.8 RSI 状态

RSI 计算 RSI6、RSI12、RSI24，主要用 RSI12 判断：

| RSI12 | 状态 | 解读 |
| --- | --- | --- |
| > 70 | 超买 | 短期回调风险高 |
| > 60 | 强势买入 | 多头力量较强 |
| 40-60 | 中性 | 震荡整理 |
| 30-40 | 弱势 | 关注反弹 |
| < 30 | 超卖 | 可能有反弹机会 |

### 6.9 技术评分公式

技术信号满分 100 分，由 6 个模块构成：

| 模块 | 权重 | 高分条件 |
| --- | ---: | --- |
| 趋势 | 30 | 多头排列，尤其强势多头 |
| 乖离率 | 20 | 接近 MA5 或回踩 MA5，不追高 |
| 量能 | 15 | 缩量回调、放量上涨 |
| 支撑 | 10 | MA5 / MA10 支撑有效 |
| MACD | 15 | 零轴上金叉、金叉、上穿零轴、多头 |
| RSI | 10 | 超卖反弹或强势区间 |

详细权重如下。

趋势分：

| 趋势状态 | 分数 |
| --- | ---: |
| 强势多头 | 30 |
| 多头排列 | 26 |
| 弱势多头 | 18 |
| 盘整 | 12 |
| 弱势空头 | 8 |
| 空头排列 | 4 |
| 强势空头 | 0 |

乖离率分：

| MA5 乖离率 | 分数 | 含义 |
| --- | ---: | --- |
| -3% 到 0% | 20 | 价格略低于 MA5，回踩买点 |
| -5% 到 -3% | 16 | 回踩 MA5，观察支撑 |
| 小于 -5% | 8 | 可能破位 |
| 0% 到 2% | 18 | 贴近 MA5，介入较舒服 |
| 2% 到默认阈值 | 14 | 略高于 MA5，可小仓 |
| 超过阈值 | 4 | 追高风险 |
| 强趋势中超过基础阈值但未超过放宽阈值 | 10 | 可轻仓跟踪 |

量能分：

| 量能状态 | 分数 |
| --- | ---: |
| 缩量回调 | 15 |
| 放量上涨 | 12 |
| 量能正常 | 10 |
| 缩量上涨 | 6 |
| 放量下跌 | 0 |

支撑分：

| 条件 | 分数 |
| --- | ---: |
| MA5 支撑有效 | +5 |
| MA10 支撑有效 | +5 |

MACD 分：

| MACD 状态 | 分数 |
| --- | ---: |
| 零轴上金叉 | 15 |
| 金叉 | 12 |
| 上穿零轴 | 10 |
| 多头 | 8 |
| 空头 | 2 |
| 下穿零轴 | 0 |
| 死叉 | 0 |

RSI 分：

| RSI 状态 | 分数 |
| --- | ---: |
| 超卖 | 10 |
| 强势买入 | 8 |
| 中性 | 5 |
| 弱势 | 3 |
| 超买 | 0 |

### 6.10 技术信号映射

技术分产生 `buy_signal`：

| 条件 | 输出信号 |
| --- | --- |
| 分数 >= 75 且趋势为强势多头/多头排列 | 强烈买入 |
| 分数 >= 60 且趋势为强势多头/多头排列/弱势多头 | 买入 |
| 分数 >= 45 | 持有 |
| 分数 >= 30 | 观望 |
| 分数 < 30 且趋势为空头/强势空头 | 强烈卖出 |
| 其他低分情况 | 卖出 |

这个信号是确定性技术信号，不等同于最终报告的 `operation_advice`。最终操作建议还会结合新闻、风险、基本面、资金、筹码、策略和 LLM 判断。

## 7. 普通 LLM 分析如何生成最终报告

普通模式由 `src/analyzer.py` 的 `GeminiAnalyzer` 负责，虽然类名里有 Gemini，但实际通过 LiteLLM 支持 Gemini、DeepSeek、OpenAI-compatible、Anthropic、Ollama 等模型。

### 7.1 Prompt 输入结构

`_format_prompt()` 会把上下文组织为一个结构化分析请求，主要包含：

1. 股票基础信息
2. 今日行情
3. 均线系统
4. 实时行情增强数据
5. 财报与分红
6. 筹码分布
7. 趋势分析预判
8. 昨日量价对比
9. 新闻与舆情情报
10. 数据缺失警告
11. 输出 JSON 要求

示例结构：

```text
# 决策仪表盘分析请求

## 股票基础信息
股票代码、股票名称、分析日期

## 技术面数据
今日行情、均线系统、实时行情增强数据

## 财报与分红
营收、净利润、现金流、ROE、股息率

## 筹码分布
获利比例、平均成本、筹码集中度

## 技术与结构分析
趋势状态、均线排列、乖离率、量能、系统评分、支持因素、风险因素

## 舆情情报
最新消息、公告、风险、业绩、行业

## 分析任务
输出决策仪表盘 JSON
```

### 7.2 LLM 的职责

LLM 不负责从零计算 MA、MACD、RSI。它的职责是：

- 解释已计算好的技术指标
- 识别技术面和消息面的冲突
- 将新闻归类为风险、利好、业绩预期
- 综合基本面和资金面做定性判断
- 生成操作建议、止损、目标价、仓位策略
- 生成用户可读的报告语言
- 输出严格 JSON

### 7.3 技术一致性约束

Prompt 会要求：

- 不得同时把“空头排列”和“多头排列”都当成有效依据
- 若基本面/事件面与技术面冲突，必须写成“事件先行、技术待确认”或“基本面偏多，但技术面尚未确认”
- 如果数据缺失，必须说明“数据缺失，无法判断”，不得编造
- 如果量能异常放大，例如成交量较昨日超过 10 倍，量能信号必须降权解读

`_sanitize_trend_analysis_for_prompt()` 还会在注入 Prompt 前清洗互斥理由：

- 空头结构下移除看多结构理由
- 多头结构下移除空头结构风险表述
- 异常放量时增加降权提示

### 7.4 输出 JSON 结构

LLM 必须输出 `AnalysisResult` 可解析的 JSON。核心字段：

```json
{
  "stock_name": "股票名称",
  "sentiment_score": 0,
  "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
  "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
  "decision_type": "buy/hold/sell",
  "confidence_level": "高/中/低",
  "dashboard": {
    "core_conclusion": {},
    "data_perspective": {},
    "intelligence": {},
    "battle_plan": {}
  },
  "analysis_summary": "",
  "key_points": "",
  "risk_warning": "",
  "buy_reason": "",
  "technical_analysis": "",
  "fundamental_analysis": "",
  "news_summary": ""
}
```

`dashboard` 是当前报告最重要的结构化部分：

| 区块 | 内容 |
| --- | --- |
| `core_conclusion` | 一句话结论、信号类型、时效、空仓/持仓建议 |
| `data_perspective` | 趋势、价格位置、量能、筹码 |
| `intelligence` | 最新新闻、风险、利好、业绩预期、舆情总结 |
| `battle_plan` | 理想买点、次优买点、止损、止盈、仓位、检查清单 |

### 7.5 完整性校验

LLM 返回后，系统会：

1. 提取 JSON
2. 解析为 `AnalysisResult`
3. 检查必填字段
4. 缺字段时根据配置重试或占位补全

关键必填包括：

- `sentiment_score`
- `operation_advice`
- `dashboard.core_conclusion.one_sentence`
- `dashboard.intelligence.risk_alerts`
- `dashboard.battle_plan.sniper_points.stop_loss`

相关配置：

```env
REPORT_INTEGRITY_ENABLED=true
REPORT_INTEGRITY_RETRY=1
```

如果主模型返回非 JSON，系统会尝试 fallback 模型；所有模型都无法返回合法 JSON 时，才降级为纯文本 fallback。

## 8. Agent 模式如何工作

Agent 模式由 `src/agent/` 实现，适合需要工具调用、策略技能、多轮问股或更复杂推理的场景。

启用方式：

```env
AGENT_MODE=true
```

或配置具体策略技能时，调度任务可能自动进入 Agent 模式：

```env
AGENT_SKILLS=bull_trend,ma_golden_cross,shrink_pullback
```

### 8.1 单 Agent ReAct 模式

默认：

```env
AGENT_ARCH=single
```

单 Agent 的系统提示要求按阶段调用工具：

1. 行情与 K 线：`get_realtime_quote`、`get_daily_history`
2. 技术与筹码：`analyze_trend`、`get_chip_distribution`
3. 情报搜索：`search_stock_news`
4. 生成报告：输出决策仪表盘 JSON

规则：

- 必须调用工具获取真实数据
- 不允许编造数字
- 工具失败要记录原因并基于已有数据继续
- 最终输出有效 JSON

### 8.2 多 Agent 编排模式

启用：

```env
AGENT_ARCH=multi
AGENT_ORCHESTRATOR_MODE=standard
```

模式：

| 模式 | 链路 |
| --- | --- |
| `quick` | TechnicalAgent -> DecisionAgent |
| `standard` | TechnicalAgent -> IntelAgent -> DecisionAgent |
| `full` | TechnicalAgent -> IntelAgent -> RiskAgent -> DecisionAgent |
| `specialist` | TechnicalAgent -> IntelAgent -> RiskAgent -> 策略专家 -> DecisionAgent |

各 Agent 分工：

| Agent | 职责 |
| --- | --- |
| TechnicalAgent | 技术面、趋势、均线、量价 |
| IntelAgent | 新闻、公告、业绩、行业、情报 |
| RiskAgent | 风险审查，必要时否决买入信号 |
| SkillAgent | 单个策略技能评估 |
| DecisionAgent | 汇总意见并输出最终决策仪表盘 |

风险 Agent 是否可以否决买入信号由以下配置控制：

```env
AGENT_RISK_OVERRIDE=true
```

### 8.3 策略技能

内置策略文件在 `strategies/`：

| 策略 | 含义 |
| --- | --- |
| `bull_trend` | 多头趋势 |
| `ma_golden_cross` | 均线金叉 |
| `volume_breakout` | 放量突破 |
| `shrink_pullback` | 缩量回踩 |
| `bottom_volume` | 底部放量 |
| `dragon_head` | 龙头策略 |
| `one_yang_three_yin` | 一阳夹三阴 |
| `box_oscillation` | 箱体震荡 |
| `chan_theory` | 缠论 |
| `wave_theory` | 波浪理论 |
| `emotion_cycle` | 情绪周期 |

配置：

```env
AGENT_SKILLS=all
# 或
AGENT_SKILLS=bull_trend,ma_golden_cross,shrink_pullback
```

策略技能不是简单替换技术分析，而是作为额外约束/偏好参与判断。最终报告要说明当前结构是否满足激活技能的触发条件，若不满足要给等待条件。

### 8.4 Agent 记忆与回测校准

如果启用：

```env
AGENT_MEMORY_ENABLED=true
AGENT_SKILL_AUTOWEIGHT=true
```

Agent 可读取历史回测摘要，用策略历史表现校准置信度和权重。相关工具包括：

- `get_skill_backtest_summary`
- `get_strategy_backtest_summary`
- `get_stock_backtest_summary`

这不是直接决定买卖，而是影响策略意见权重和置信度。

## 9. 信号从哪里来：分层解释

最终报告有多个“信号”相关字段，容易混淆。

### 9.1 `signal_score`

来源：`StockTrendAnalyzer`

含义：确定性技术分，0-100。

由趋势、乖离、量能、支撑、MACD、RSI 六部分组成。

### 9.2 `buy_signal`

来源：`StockTrendAnalyzer`

含义：确定性技术买卖信号。

枚举：

- 强烈买入
- 买入
- 持有
- 观望
- 卖出
- 强烈卖出

它不会直接等同于最终 `operation_advice`。

### 9.3 `sentiment_score`

来源：LLM / Agent 输出。

含义：综合评分，0-100。它会综合：

- 技术分
- 消息面
- 风险警报
- 基本面
- 资金面
- 策略技能
- 风控计划完整度

因此可能出现技术分较高但综合分不高的情况，例如：

- 技术多头，但新闻出现减持/处罚
- 技术突破，但放量异常且资金流出
- 技术回踩良好，但基本面恶化

### 9.4 `trend_prediction`

来源：LLM / Agent 输出。

含义：最终趋势判断，常见值：

- 强烈看多
- 看多
- 震荡
- 看空
- 强烈看空

它比 `trend_status` 更高层，包含消息和基本面影响。

### 9.5 `operation_advice`

来源：LLM / Agent 输出。

含义：用户最终操作建议：

- 买入
- 加仓
- 持有
- 减仓
- 卖出
- 观望

这是报告最核心的动作字段。

### 9.6 `decision_type`

来源：LLM / Agent 输出，失败时系统会根据 `operation_advice` 推断。

取值：

- `buy`
- `hold`
- `sell`

用于统计、Web 展示和后续回测。

### 9.7 `dashboard.core_conclusion.signal_type`

来源：LLM / Agent 输出。

含义：更适合 UI/通知展示的信号标签，例如：

- 买入信号
- 持有观望
- 卖出信号
- 风险警告

## 10. 最终输出是什么

### 10.1 内部对象：`AnalysisResult`

核心字段：

| 字段 | 含义 |
| --- | --- |
| `code` | 股票代码 |
| `stock_name` | 股票名称 |
| `sentiment_score` | 综合评分 |
| `trend_prediction` | 综合趋势 |
| `operation_advice` | 操作建议 |
| `decision_type` | buy/hold/sell |
| `confidence_level` | 置信度 |
| `dashboard` | 决策仪表盘 |
| `analysis_summary` | 综合摘要 |
| `key_points` | 核心看点 |
| `risk_warning` | 风险提示 |
| `buy_reason` | 操作理由 |
| `technical_analysis` | 技术面分析 |
| `fundamental_analysis` | 基本面分析 |
| `news_summary` | 新闻摘要 |
| `current_price` | 分析时价格 |
| `change_pct` | 分析时涨跌幅 |

### 10.2 Markdown 报告

模板位于 `templates/report_markdown.j2`。报告展示重点：

- 总览表
- 核心结论
- 舆情情报
- 数据视角
- 交易计划
- 风险提示
- 历史信号对比

### 10.3 通知输出

通知渠道包括：

- 企业微信
- 飞书
- Telegram
- 邮件
- Pushover
- PushPlus
- Server酱
- Discord
- Slack
- 自定义 Webhook

不同渠道会按自身限制裁剪或转换内容。例如飞书/企业微信有字节限制，部分渠道可把 Markdown 转图片。

### 10.4 Web/API 输出

Web/API 会返回结构化结果，供前端展示：

- 历史报告列表
- 报告详情
- 任务状态
- SSE 进度
- 新闻情报关联记录
- 回测结果
- 配置状态

## 11. 报告中的操作建议如何形成

可以把最终建议理解成三层叠加。

### 11.1 第一层：技术底座

先看：

- 是否多头排列
- 是否离 MA5 太远
- 是否缩量回踩或放量突破
- 是否有 MA5/MA10 支撑
- MACD 是否转强
- RSI 是否超买/超卖

这一层给出 `signal_score` 和 `buy_signal`。

### 11.2 第二层：证据增强或削弱

再看：

- 实时行情是否支持历史技术判断
- 筹码是否健康
- 基本面是否支撑估值
- 资金是否流入
- 板块是否共振
- 新闻是否有催化
- 是否有减持、处罚、业绩变脸、诉讼等风险

这一层会增强或削弱 LLM 的最终综合分。

### 11.3 第三层：交易计划和风控

最终建议必须落到：

- 空仓者怎么做
- 持仓者怎么做
- 理想买点
- 次优买点
- 止损位
- 目标位
- 建议仓位
- 触发条件
- 失效条件
- 行动检查清单

如果只有“看多”但没有合理买点，系统应倾向于“等待回踩/观望”而不是直接追买。

## 12. 数据缺失和失败降级

项目大量使用 fail-open 设计，目标是单个外部依赖失败不拖垮主流程。

### 12.1 实时行情失败

行为：

- 记录 warning
- 使用历史收盘价继续
- Prompt 中缺失字段显示 N/A

影响：

- 盘中价格、量比、换手率、PE/PB 可能缺失
- 技术面仍可基于历史日线分析

### 12.2 筹码失败

行为：

- 记录 warning 或 debug
- 跳过筹码区块

影响：

- 报告不能判断筹码集中度和获利盘压力
- 不影响技术分析和 LLM 生成

### 12.3 基本面失败

行为：

- 返回 `failed` 或 `partial` block
- 保留错误信息
- 不阻塞技术面/新闻链路

影响：

- 基本面分析会写“数据缺失，无法判断”
- 估值、财报、资金、板块字段可能为空

### 12.4 搜索服务失败

行为：

- 没有可用搜索 key 时跳过
- provider 失败时记录 warning
- 有其他 provider 时尝试 fallback

影响：

- 新闻、公告、风险、业绩预期质量下降
- 报告更依赖技术面和已有数据

### 12.5 LLM 返回非法 JSON

行为：

- 尝试解析 JSON
- 失败时尝试备用模型
- 仍失败时使用文本 fallback

影响：

- 可能缺少结构化 dashboard
- 仍尽量给出基础分析结果

### 12.6 Agent 工具失败

行为：

- 工具返回 error
- Agent 被要求记录失败原因
- 使用已有数据继续

影响：

- 对应维度置信度下降
- 多 Agent 模式下某阶段失败可降级生成报告

## 13. 配置如何改变信号

### 13.1 `BIAS_THRESHOLD`

影响追高判断。

默认：

```env
BIAS_THRESHOLD=5.0
```

调低会更保守，更容易判定“乖离率过高”；调高会更激进，但也更容易追高。

### 13.2 `ENABLE_REALTIME_TECHNICAL_INDICATORS`

启用时用实时价增强当天 K 线和均线判断：

```env
ENABLE_REALTIME_TECHNICAL_INDICATORS=true
```

盘中分析会更敏感，但也更受实时数据质量影响。

### 13.3 `ENABLE_CHIP_DISTRIBUTION`

关闭后不再尝试筹码：

```env
ENABLE_CHIP_DISTRIBUTION=false
```

适合接口不稳定或云端部署场景。

### 13.4 `NEWS_STRATEGY_PROFILE` / `NEWS_MAX_AGE_DAYS`

影响消息面时间窗口。

短窗口更适合短线交易，长窗口更适合中线或事件复盘。

### 13.5 `AGENT_SKILLS`

影响策略约束。

例如：

```env
AGENT_SKILLS=shrink_pullback
```

系统会更强调缩量回踩的触发条件，而不是泛泛看多。

### 13.6 `AGENT_ARCH` / `AGENT_ORCHESTRATOR_MODE`

影响 Agent 推理深度。

- `single`：成本较低，工具调用集中
- `multi` + `quick`：技术优先，速度快
- `multi` + `standard`：技术 + 情报 + 决策
- `multi` + `full`：增加风险 Agent
- `multi` + `specialist`：增加策略专家

### 13.7 `REPORT_TYPE`

影响输出详略：

- `brief`：短摘要
- `simple`：默认精简报告
- `full`：完整报告

它不应改变底层分析逻辑，只改变展示内容。

## 14. 普通模式与 Agent 模式的区别

| 项目 | 普通模式 | Agent 模式 |
| --- | --- | --- |
| 数据获取 | Pipeline 预先获取并拼 Prompt | LLM 可按阶段调用工具 |
| 技术分析 | 必定执行 `StockTrendAnalyzer` | 可执行 `analyze_trend` 工具，也使用前置趋势 |
| 新闻 | Pipeline 多维搜索后拼入 Prompt | Agent 可调用搜索工具 |
| 策略技能 | 主要作为 Prompt 策略约束 | 可由 SkillAgent 专门评估 |
| 成本 | 较低 | 较高 |
| 可解释性 | 依赖 Prompt 和上下文 | 工具调用路径更清晰 |
| 适用场景 | 定时日报、普通批量分析 | 问股、多策略、复杂推理 |

## 15. 大盘复盘与个股分析的关系

个股分析关注单个标的，大盘复盘关注市场区域：

- A 股
- 港股
- 美股
- 合并区域

大盘复盘会看：

- 指数走势
- 板块涨跌
- 市场温度
- 资金情绪
- 新闻催化
- 次日交易计划
- 风险提示

个股报告中也会使用部分市场/板块数据，但个股主流程以单股上下文为中心。

## 16. 回测如何参与

回测不是当前信号的直接输入，除非启用 Agent 记忆和策略自动加权。

常规回测用于：

- 验证历史报告方向是否正确
- 统计不同策略/股票的表现
- 给 Web 回测页展示
- 给 Agent 记忆提供校准数据

相关配置：

```env
BACKTEST_ENABLED=true
BACKTEST_EVAL_WINDOW_DAYS=10
BACKTEST_MIN_AGE_DAYS=14
BACKTEST_ENGINE_VERSION=v1
BACKTEST_NEUTRAL_BAND_PCT=2.0
```

## 17. 典型案例：一个买入信号如何出现

假设某股票出现：

- MA5 > MA10 > MA20
- 当前价离 MA5 只有 1.5%
- 缩量回踩
- MA5 支撑有效
- MACD 金叉
- RSI 60-70 强势区间
- 没有减持/处罚/业绩雷
- 板块近期强势

确定性技术层可能给出：

```text
signal_score = 75+
buy_signal = 强烈买入
signal_reasons = [
  多头排列，顺势做多,
  价格贴近 MA5,
  缩量回调,
  MA5 支撑有效,
  MACD 金叉,
  RSI 强势
]
```

LLM 综合层可能输出：

```json
{
  "sentiment_score": 78,
  "trend_prediction": "看多",
  "operation_advice": "买入",
  "decision_type": "buy",
  "confidence_level": "中",
  "dashboard": {
    "core_conclusion": {
      "one_sentence": "回踩不破MA5可分批低吸",
      "signal_type": "买入信号"
    },
    "battle_plan": {
      "sniper_points": {
        "ideal_buy": "靠近MA5回踩确认后买入",
        "stop_loss": "跌破MA10或关键支撑止损"
      }
    }
  }
}
```

如果同样技术条件下出现重大利空，例如监管处罚或大股东减持，LLM 应该降低综合分，甚至从“买入”改为“观望”。

## 18. 典型案例：为什么技术看多但最终建议观望

这种情况通常来自证据冲突：

技术面：

- 多头排列
- 放量突破
- MACD 强

但其他维度：

- 当日乖离率超过阈值
- 获利盘比例过高
- 新闻出现减持
- 资金流出
- 板块没有共振

此时确定性技术信号可能仍偏强，但最终建议可能是：

```text
趋势看多，但当前追高风险较大，等待回踩 MA5/MA10 后再考虑。
```

这符合项目的交易理念：不是“看多就立刻买”，而是要求入场位置、风险收益比和止损条件都合理。

## 19. 二次开发时应注意的边界

### 19.1 想改技术信号

优先看：

- `src/stock_analyzer.py`
- `BIAS_THRESHOLD`
- `VOLUME_SHRINK_RATIO`
- `VOLUME_HEAVY_RATIO`
- `MA_SUPPORT_TOLERANCE`

如果改评分权重，要同步考虑：

- 单元测试
- Prompt 对评分语义的描述
- 文档说明
- 回测可比性

### 19.2 想改报告结构

优先看：

- `src/analyzer.py` 的 JSON schema prompt
- `AnalysisResult`
- `templates/report_markdown.j2`
- `src/notification.py`
- Web 前端报告展示
- API schema

报告结构变更通常是用户可见变更，需要同步文档和 changelog。

### 19.3 想接入新数据源

优先放在：

- `data_provider/`
- `DataFetcherManager`
- 对应 fetcher 的 priority / fallback

原则：

- 单一数据源失败不应拖垮主流程
- 字段语义要标准化
- 注意市场差异，例如 A 股/港股/美股单位不同
- 新配置要更新 `.env.example` 和文档

### 19.4 想增加 Agent 工具

优先放在：

- `src/agent/tools/`
- `ToolDefinition`
- `src/agent/factory.py` 的工具注册列表

工具输出要尽量结构化、短小、可 JSON 序列化，避免把超长原始数据直接塞给模型。

### 19.5 想增加策略

优先放在：

- `strategies/*.yaml`
- `src/agent/skills/`

策略应说明：

- 触发条件
- 失效条件
- 需要哪些工具
- 适用市场
- 风险约束
- 与默认策略的关系

## 20. 当前设计的核心取舍

### 20.1 稳定性优先

外部数据源经常不稳定，所以系统大多采用 fail-open：

- 行情失败不阻断
- 筹码失败不阻断
- 基本面失败不阻断
- 搜索失败不阻断
- 通知失败不应拖垮分析主流程

### 20.2 技术信号先确定，再交给 LLM 解释

项目避免让 LLM 自己凭感觉计算指标。确定性指标由代码计算，LLM 主要负责综合解释和生成用户可读决策。

### 20.3 风险优先于乐观结论

Prompt 和 Agent 规则都强调：

- 必须排查减持、处罚、诉讼、业绩预警
- 消息面风险要醒目标出
- 技术与事件冲突时不能硬写买入
- 必须给止损和失效条件

### 20.4 不追高

MA5 乖离率是核心约束。即便趋势强，如果价格已经明显偏离 MA5，系统也倾向于提示等待回踩或轻仓跟踪。

### 20.5 输出必须可执行

报告不只输出“看多/看空”，还要求：

- 空仓者建议
- 持仓者建议
- 买点
- 止损
- 目标价
- 仓位
- 检查清单

这就是“决策仪表盘”的核心。

## 21. 快速定位代码

| 想了解什么 | 文件 |
| --- | --- |
| 单股分析主流程 | `src/core/pipeline.py` |
| 技术评分和买卖信号 | `src/stock_analyzer.py` |
| Prompt 和 JSON 输出要求 | `src/analyzer.py` |
| 新闻搜索维度 | `src/search_service.py` |
| 行情和基本面聚合 | `data_provider/base.py` |
| Agent 工具注册 | `src/agent/factory.py` |
| Agent 工具定义 | `src/agent/tools/` |
| 多 Agent 编排 | `src/agent/orchestrator.py` |
| 策略技能 | `strategies/`、`src/agent/skills/` |
| 报告模板 | `templates/report_markdown.j2`、`templates/report_wechat.j2` |
| 推送渠道 | `src/notification.py` |
| 配置字段 | `.env.example`、`src/config.py`、`src/core/config_registry.py` |

## 22. 一句话总结

当前系统的分析原理可以概括为：

```text
用确定性规则计算技术底座，
用多数据源补齐行情、筹码、基本面、资金、板块和消息，
用 LLM/Agent 在严格 JSON 结构和风控约束下生成可执行交易计划，
再通过历史记录、报告模板和通知渠道交付给用户。
```

其中最核心的信号链条是：

```text
历史日线 + 实时行情
  -> 均线 / 乖离率 / 量能 / MACD / RSI / 支撑压力
  -> signal_score + buy_signal
  -> 加入筹码 / 基本面 / 资金 / 板块 / 新闻 / 策略
  -> sentiment_score + operation_advice + battle_plan
```

理解这条链路，就能判断报告里的结论到底来自哪里，也能知道应该从哪个模块开始调参或排查。
