# StockAnalyser Agent 术语速查

> 用于查阅面试问答中的专业名词。示例只用于说明含义，不代表真实股票结论。

## 1. 数据质量

| 名词 | 白话解释 | 简单例子 |
| --- | --- | --- |
| `source` | 最终采用的数据来源。 | `source = "baostock"`，表示当前结果来自 Baostock。 |
| `source_chain` | 获取数据时依次尝试过哪些来源，以及每个来源是否成功。 | 本地数据库数据不足，随后通过 Baostock 获取成功。 |
| `as_of` | 这份数据最晚统计到哪个时间点。 | `as_of = "2026-08-07"`，表示不能用它说明 8 月 7 日之后的情况。 |
| `freshness` | 数据有多新。 | `realtime` 表示实时，`eod_current` 表示最新收盘数据，`stale` 表示已经过期。 |
| `fallback` | 主数据源失败后使用的备用方案。 | TuShare 超时后改用 StockAPI；如果只能使用旧缓存，需要标记为 `stale`。 |
| `status` | 当前结果是否可用。 | `ok` 为正常，`partial` 为部分可用，`unavailable` 为不可用，`failed` 为执行失败。 |
| `stale` | 数据存在，但时间太旧，不能当作最新数据使用。 | 今天分析股票，但资金流只有三天前的数据。 |
| `partial` | 返回了一部分有效数据，同时还有字段缺失或来源失败。 | 行情和成交量正常，但资金流字段缺失。 |
| `provisional` | 数据周期还没结束，只能作为临时证据。 | 本周还未收盘，当前周 K 线只能展示，不能计入正式评分。 |
| `missing_fields` | 当前结果缺少哪些关键字段。 | `missing_fields = ["volume_ratio", "turnover_rate"]`。 |
| `raw_ref` | 完整原始数据保存在哪里。 | `tool_raw/analyze_trend/600519.json`。 |

### `source_chain` 示例

```json
[
  {"source": "local_db", "status": "insufficient"},
  {"source": "baostock", "status": "ok"}
]
```

含义：先读取本地数据库，但数据量不足；随后使用 Baostock 补齐并成功返回。

## 2. Agent 执行

| 名词 | 白话解释 | 简单例子 |
| --- | --- | --- |
| `Planner` | 在执行前决定需要分析什么、调用哪些工具。 | 用户问“600519 是否适合买入”，Planner 选择行情、技术面、新闻和风险检查。 |
| `intent` | 用户这次请求的主要目的。 | `entry_analysis` 是入场分析，`position_review` 是持仓复盘，`watchlist_scan` 是选股。 |
| `capability` | 一类分析能力，不是具体工具。 | `technical_analysis` 是能力，可以映射到趋势、均线和价格结构工具。 |
| `AgentContext` | 单股多 Agent 共用的运行状态。 | 保存 600519 的行情、技术意见和风险标记，供后续 Agent 读取。 |
| `SelectionRunContext` | 一次选股任务共用的阶段状态。 | 保存候选池、逐股取证、组合配置、Judge 结果和工具日志。 |
| `AgentState` | 专家图使用的、经过 Pydantic 校验的 Trace 快照。 | 集中保存证据卡、专家意见和 Judge 结果，用于展示和复盘，不驱动选股主流程。 |
| `Tool` | 实际执行取数或计算的函数。 | `get_realtime_quote` 获取实时行情。 |
| `ToolRegistry` | 系统允许 Agent 使用的工具清单。 | 模型只能调用已经注册的行情、新闻或技术分析工具。 |
| `ReAct` | 模型根据当前结果决定下一步要不要继续调用工具。 | 先查行情，发现波动异常后再查新闻，最后生成结论。 |
| `Replan` | 原计划无法完成时，重新选择路径。 | 主资金流工具超时，改用同类备用工具；仍失败就降低结论强度。 |
| `stop condition` | Agent 应该停止继续调用工具的条件。 | 关键证据已经齐全，新增工具不会改变最终动作。 |
| `token` | 模型读取和生成文本的计费/长度单位。 | 上下文越长、报告越长，一般消耗的 token 越多。 |

## 3. 证据与决策

| 名词 | 白话解释 | 简单例子 |
| --- | --- | --- |
| `EvidenceCard` | 从一大段工具结果中提取出的结构化证据摘要。 | 保存股票、数据来源、时间、关键指标、支持/反对方向和原始数据位置。 |
| `ExpertEvidencePacket` | 一个专家维度把多张 EvidenceCard 汇总后的精简证据包。 | 保存技术面的主要支持点、风险点、置信度和缺失证据，供专家图展示和审计。 |
| `Evidence Ledger` | 本次任务使用过的全部证据目录。 | 可以查看哪些证据支持买入、哪些证据反对，以及缺了什么。 |
| `counter evidence` | 与当前判断相反的证据。 | 技术趋势向上，但出现放量滞涨和顶部背驰。 |
| `Judge` | 汇总多个专家和正反证据，生成最终裁决的模块。 | 技术专家支持、资金专家反对，Judge 综合后给出等待。 |
| `Risk Gate` | 最终动作输出前执行的确定性风险检查。 | 计划建议开仓 30%，但单票上限为 20%，Risk Gate 记录或拦截。 |
| `Fact Guard` | 检查报告中的事实是否来自真实工具结果。 | 工具没有返回筹码成本时，阻止模型自行编造该数值。 |
| `shadow mode` | 新规则只记录结果，不立即改变实际决策。 | Strategy4 评分先写入 Trace，验证稳定后再决定是否限制仓位。 |
| `confidence` | 当前证据对判断的支持强度，不等于真实胜率。 | `confidence = 0.8` 表示模型较有把握，不表示未来上涨概率一定是 80%。 |

### `EvidenceCard` 示例

```json
{
  "stock": {"code": "600519", "name": "贵州茅台"},
  "dimension": "technical",
  "data_quality": {
    "status": "ok",
    "source": "baostock",
    "source_chain": [
      {"source": "local_db", "status": "insufficient"},
      {"source": "baostock", "status": "ok"}
    ],
    "as_of": "2026-08-07",
    "freshness": "eod_current",
    "missing_fields": []
  },
  "signals": [
    {
      "name": "ma_trend",
      "direction": "positive",
      "interpretation": "短期均线保持向上"
    }
  ],
  "impact": {
    "stance": "support",
    "action_bias": "wait",
    "confidence": 0.78
  },
  "raw_ref": "tool_raw/analyze_trend/600519.json"
}
```

含义：技术面提供了一条偏正面的证据，但单一技术信号还不足以直接买入，因此动作倾向仍为等待；需要继续结合资金、消息和风险证据。

## 4. 记录与评估

| 名词 | 白话解释 | 简单例子 |
| --- | --- | --- |
| `Trace` | 一次 Agent 运行的完整过程记录。 | 包含输入、计划、工具调用、证据、耗时和最终报告。 |
| `artifact` | Trace 中落盘保存的某个文件。 | `planner.json`、`tool_calls.json`、`final_report.json`。 |
| `Schema` | 规定 JSON 必须有哪些字段、字段是什么类型。 | `confidence` 必须是 0 到 1 的数字。 |
| `Pydantic` | Python 中用于校验结构化数据的工具。 | 模型把数字输出成错误文本时，Pydantic 校验会失败。 |
| `Memory` | 保存用户会话或历史分析，供后续任务参考。 | 再次分析 600519 时读取上一次分析摘要。 |
| `RAG` | 根据当前问题，从外部资料中检索相关证据。 | 分析公司事件时检索最近公告和新闻。 |
| `P50 / P95` | 50% / 95% 请求可以在多长时间内完成。 | `P95 = 30s` 表示 95% 的请求在 30 秒内完成。 |
| `Badcase` | 系统输出错误或不符合预期的真实案例。 | 工具已实现，但 Planner 没选中，导致最终报告缺少该证据。 |

## 5. 最容易混淆的区别

| 容易混淆 | 区别 |
| --- | --- |
| 工具成功 vs 数据可用 | 函数没有报错，不代表数据足够新、字段完整或口径正确。 |
| `partial` vs `failed` | `partial` 还有部分有效数据；`failed` 表示本次执行失败。 |
| `stale` vs `unavailable` | `stale` 有旧数据；`unavailable` 没有可用数据。 |
| `confidence` vs 胜率 | confidence 是本次判断把握；胜率需要大量历史样本统计。 |
| Memory vs RAG | Memory 读取过去的会话/分析；RAG 检索当前问题需要的外部资料。 |
| EvidenceCard vs 原始结果 | EvidenceCard 是摘要；原始结果通过 `raw_ref` 保留。 |
| 候选分 vs 买入分 | 候选分表示值得继续研究，不代表已经满足买入条件。 |
| 影子模式 vs 正式生效 | 影子模式只观察和记录；正式生效才会改变动作或仓位。 |

## 6. Agent 内部通信示意

Agent 之间不直接发送聊天消息，而是依次读写共享状态：

```text
Technical Agent
      |
      | 写入 technical_opinion
      v
AgentContext（共享状态）
      |
      | Intel / Capital / Fundamental
      | 分别写入消息、资金、基本面意见
      v
AgentContext（更新后的共享状态）
      |
      | Risk 读取四维证据并写入风险标记
      | Decision 读取全部结果
      v
最终报告
```

可以把它理解成多人填写同一张结构化表格：每个人只负责自己的栏目，后续人员读取前面已经填写的内容，最终由 Decision Agent 或 Judge 汇总。

## 7. `AgentContext` 具体示例

下面是一次“分析 600519 是否适合买入”的模拟状态。价格和结论均为示例值：

```json
{
  "query": "分析 600519 是否适合买入",
  "stock_code": "600519",
  "stock_name": "贵州茅台",
  "session_id": "trace-demo-001",
  "data": {
    "realtime_quote": {
      "last_price": 1500.0,
      "pct_change": 0.8,
      "as_of": "2026-08-07 15:00:00",
      "source": "quote_provider"
    },
    "trend_result": {
      "status": "ok",
      "trend": "bullish",
      "ma_alignment": "bullish",
      "volume_status": "normal"
    },
    "news_context": {
      "status": "partial",
      "summary": "未发现会立即改变判断的重大事件",
      "missing_fields": ["latest_announcement_check"]
    },
    "capital_opinion": {
      "signal": "buy",
      "confidence": 0.7,
      "reasoning": "个股主力口径近 5 日净流入",
      "quality": "partial",
      "missing_evidence": ["margin_trading"]
    },
    "fundamental_opinion": {
      "signal": "hold",
      "confidence": 0.68,
      "reasoning": "盈利质量稳定，估值处于历史中位附近",
      "quality": "ok",
      "missing_evidence": []
    }
  },
  "opinions": [
    {
      "agent_name": "technical",
      "signal": "buy",
      "confidence": 0.78,
      "reasoning": "趋势向上，但仍需确认资金和风险条件",
      "key_levels": {
        "support": 1450.0,
        "resistance": 1550.0
      },
      "raw_data": {
        "ma_alignment": "bullish",
        "trend_score": 72
      },
      "timestamp": 1780000001.0
    },
    {
      "agent_name": "intel",
      "signal": "hold",
      "confidence": 0.62,
      "reasoning": "消息面没有强催化，部分公告证据待补充",
      "key_levels": {},
      "raw_data": {},
      "timestamp": 1780000002.0
    },
    {
      "agent_name": "capital",
      "signal": "buy",
      "confidence": 0.7,
      "reasoning": "资金流偏正向，但融资融券证据缺失",
      "key_levels": {},
      "raw_data": {
        "quality": "partial",
        "capital_flow_signal": "inflow",
        "missing_evidence": ["margin_trading"]
      },
      "timestamp": 1780000002.1
    },
    {
      "agent_name": "fundamental",
      "signal": "hold",
      "confidence": 0.68,
      "reasoning": "盈利质量稳定，估值没有明显低估",
      "key_levels": {},
      "raw_data": {
        "quality": "ok",
        "valuation_assessment": "fair",
        "financial_health": "strong",
        "missing_evidence": []
      },
      "timestamp": 1780000002.2
    }
  ],
  "risk_flags": [
    {
      "category": "missing_evidence",
      "description": "最新公告检查尚未完成",
      "severity": "medium",
      "timestamp": 1780000003.0
    }
  ],
  "meta": {
    "response_mode": "dashboard",
    "skills_requested": [],
    "conversation_history": [],
    "dimension_status": {
      "technical": {"quality": "ok", "missing_evidence": []},
      "intel": {"quality": "partial", "missing_evidence": ["latest_announcement_check"]},
      "capital": {"quality": "partial", "missing_evidence": ["margin_trading"]},
      "fundamental": {"quality": "ok", "missing_evidence": []}
    }
  },
  "created_at": 1780000000.0
}
```

### 7.1 顶层字段

| 字段 | 类型 | 可以填写什么 | 示例 |
| --- | --- | --- | --- |
| `query` | 字符串 | 用户原始问题，内容不限。 | `"分析 600519 是否适合买入"` |
| `stock_code` | 字符串 | 当前股票代码；没有明确标的时可以为空。 | `"600519"`、`"hk00700"`、`"AAPL"` |
| `stock_name` | 字符串 | 股票名称；名称没取到时可以为空。 | `"贵州茅台"` |
| `session_id` | 字符串 | 本次会话或 Trace 的唯一标识。 | `"trace-demo-001"` |
| `data` | 对象 | 工具结果和阶段共享数据，字段可以扩展。 | 行情、趋势、新闻、筹码等 |
| `opinions` | 数组 | 各 Agent 的结构化判断。 | technical、intel、capital、fundamental、risk 的意见 |
| `risk_flags` | 数组 | 已发现的风险项。 | 监管、业绩、估值或证据缺失风险 |
| `meta` | 对象 | 不属于股票事实的运行配置和会话信息。 | 输出模式、语言、Skill、聊天历史 |
| `created_at` | 数字 | 对象创建时的 Unix 时间戳，由系统自动填写。 | `1780000000.0` |

### 7.2 `data` 可以放什么

`data` 没有固定的完整字段表。它是共享数据区，常见内容如下：

| Key | 内容 | 常见字段示例 |
| --- | --- | --- |
| `realtime_quote` | 实时或最新行情 | `last_price`、`pct_change`、`volume`、`as_of`、`source` |
| `daily_history` | 历史日 K 数据 | 日期、开高低收、成交量 |
| `trend_result` | 技术趋势结果 | `trend`、`trend_score`、`ma_alignment`、`volume_status` |
| `chip_distribution` | 筹码分布 | 获利比例、平均成本、筹码区间 |
| `capital_flow` | 资金流结果 | 主力净流入、5 日/10 日资金趋势、来源口径 |
| `news_context` | 新闻和事件摘要 | 新闻列表、情绪、风险事件、缺失项 |
| `technical_opinion` | Technical Agent 的原始结构化输出 | 趋势、量价、形态、关键价位、质量和缺失项 |
| `intel_opinion` | Intel Agent 的原始结构化输出 | 催化、事件风险、情绪、质量和缺失项 |
| `capital_opinion` | Capital Agent 的原始结构化输出 | 资金方向、口径化指标、质量和缺失项 |
| `fundamental_opinion` | Fundamental Agent 的原始结构化输出 | 估值、财务健康度、关键指标、质量和缺失项 |
| `skill_consensus` | 多个交易 Skill 的汇总 | `signal`、`confidence`、`reasoning` |
| `final_dashboard` | Decision Agent 生成的最终仪表盘 | 最终动作、分析摘要、风险和交易计划 |
| `final_response_text` | Chat 模式的最终文本 | 一段自然语言回答 |

注意：`data` 中的 value 不一定都是对象，例如 `final_response_text` 是字符串，`daily_history` 可能是列表。新增 Agent 也可以写入新的 key，但应避免和已有字段重名。

### 7.3 `opinions` 中每条意见可以填什么

| 字段 | 类型/范围 | 可以填写什么 |
| --- | --- | --- |
| `agent_name` | 字符串 | 产生意见的 Agent 名称，如 `technical`、`intel`、`capital`、`fundamental`、`risk`、`decision`。 |
| `signal` | 字符串 | 通常是 `strong_buy`、`buy`、`hold`、`sell`、`strong_sell`。最终仪表盘会归一为 `buy/hold/sell`。 |
| `confidence` | `0.0-1.0` | Agent 对本次意见的把握程度；超出范围会被截到 0 或 1。它不等于历史胜率。 |
| `reasoning` | 字符串 | 当前判断的简短依据。 |
| `key_levels` | 数值对象 | 常用 `support`、`resistance`、`stop_loss`；没有关键价位时可以是 `{}`。 |
| `raw_data` | 对象 | 该 Agent 特有的附加结果，内容不固定。 |
| `timestamp` | 数字 | 意见写入时的 Unix 时间戳，由系统自动补充。 |

不同 Agent 的 `raw_data` 示例：

| Agent | `raw_data` 常见内容 |
| --- | --- |
| Technical | `trend_score`、`ma_alignment`、`volume_status`、`pattern` |
| Intel | `risk_alerts`、`positive_catalysts`、`sentiment_label`、`key_news` |
| Capital | `capital_flow_signal`、`capital_metrics`、`quality`、`missing_evidence` |
| Fundamental | `valuation_assessment`、`financial_health`、`key_metrics`、`quality`、`missing_evidence` |
| Risk | `risk_level`、`risk_score`、`flags`、`veto_buy`、`signal_adjustment` |
| Decision | 最终 dashboard 的完整结构 |

### 7.4 `risk_flags` 中每条风险可以填什么

| 字段 | 类型/范围 | 可以填写什么 |
| --- | --- | --- |
| `category` | 字符串 | 常见为 `insider`、`earnings`、`regulatory`、`industry`、`lockup`、`valuation`、`technical`、`intel`；无法分类时可用 `unknown`。 |
| `description` | 字符串 | 风险事实和原因。 |
| `severity` | 字符串 | `high`、`medium`、`low`，默认是 `medium`。 |
| `timestamp` | 数字 | 风险写入时间，由系统自动填写。 |

风险示例：

```json
{
  "category": "regulatory",
  "description": "检索到监管调查信息，需要人工确认影响范围",
  "severity": "high",
  "timestamp": 1780000000.0
}
```

### 7.5 `meta` 可以放什么

| Key | 类型/取值 | 含义 |
| --- | --- | --- |
| `response_mode` | `dashboard` 或 `chat` | 输出结构化仪表盘还是自然语言对话。 |
| `skills_requested` | 字符串数组 | 本次明确启用的交易 Skill。 |
| `strategies_requested` | 字符串数组 | `skills_requested` 的兼容字段。 |
| `report_language` | `zh` 或 `en` | 最终报告语言。 |
| `conversation_history` | 消息数组 | 历史消息；每条通常包含 `role` 和 `content`。 |
| `dimension_status` | 对象 | 四维取证质量总览；每维包含 `quality` 和 `missing_evidence`。 |

`conversation_history` 示例：

```json
[
  {"role": "user", "content": "分析 600519"},
  {"role": "assistant", "content": "你更关注入场还是持仓风险？"}
]
```

`meta` 只保存运行信息，不应把实时价格、资金流等业务数据放在这里；这些内容应放入 `data`。

状态变化可以简化为：

```text
刚创建：data={}，opinions=[]，risk_flags=[]
   |
Technical 完成：data 增加行情/趋势，opinions 增加 technical
   |
Intel 完成：data 增加新闻，opinions 增加 intel
   |
Capital 完成：data 增加资金意见，opinions 增加 capital
   |
Fundamental 完成：data 增加基本面意见，opinions 增加 fundamental
   |
Risk 完成：opinions 增加 risk，risk_flags 增加风险项
   |
Decision 读取完整 AgentContext，生成最终报告
```

`AgentContext` 本质上是一次运行期间的共享内存对象，不是 Agent 之间发送的聊天消息，也不是直接展示给用户的最终报告。
