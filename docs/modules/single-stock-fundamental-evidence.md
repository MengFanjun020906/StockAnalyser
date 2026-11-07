# 单股基本面证据补全

## 目标

单股分析先读取结构化财务数据；结构化数据缺失或不足时，自动调用 AnySearch 补充带来源的公开资料。搜索结果只补全证据，不替代公司公告和正式财报。

## 搜索口径

每次最多并行搜索两个维度，每个维度最多保留 3 条结果：

- `financial_performance`：年报、半年报、营收、净利润、现金流、ROE、资产负债率、业绩预告。
- `business_and_risk`：主营业务、收入构成、应收账款、存货、商誉、行业竞争、经营风险。

`get_stock_info` 已从 Agent 运行时注册表废弃。新链路使用 `get_stock_business_context` 获取公司与行业上下文，并由 `get_tushare_daily_basic`、`get_tushare_financial_indicators`、`get_tushare_financial_statements`、`get_tushare_forecast` 等结构化工具负责估值和财务证据。结构化证据仍有缺口时再调用网页检索，避免无意义增加等待时间。

旧 Trace 中已有的 `get_stock_info` 结果只用于历史回放，不再参与新请求的工具规划，也不会因其失败降低当前基本面完整度。

## 工具结果

`search_stock_fundamentals` 返回：

```json
{
  "status": "partial",
  "stock_code": "600330",
  "stock_name": "天通股份",
  "provider": "AnySearch",
  "as_of": "2026-08-13",
  "coverage": {
    "financial_performance": "available",
    "business_and_risk": "available"
  },
  "materials": [
    {
      "dimension": "financial_performance",
      "title": "资料标题",
      "snippet": "与基本面有关的摘要",
      "url": "https://example.com/report",
      "source": "example.com",
      "published_date": "2026-07-15"
    }
  ],
  "missing_fields": [],
  "source_chain": [],
  "errors": []
}
```

只要用了网页检索，`status` 最高为 `partial`。LLM 可以引用这些资料解释结论，但不能把网页摘要中的数字当作无条件可信的正式财务数据。

## 存储

- `news_intel`：逐条保存网页资料，按 URL 去重；保存股票、检索维度、查询词、标题、摘要、来源、发布日期、抓取时间和 Trace ID。
- `fundamental_snapshot`：按 Trace 保存本次完整聚合结果，包括 `materials`、`coverage` 和 `source_chain`，用于回放“当次分析实际看到了什么”。
- `tool_calls.json`：保存工具参数、状态、耗时和结果摘要，供 Trace 审计。

数据库写入失败时主分析保持可用，但工具日志会保留当次结果；搜索失败时基本面保持 `partial` 或 `unavailable`，不能静默写成完整。

## 用户报告

内部 JSON 只用于模型与程序通信。最终报告展示为“结论、多维证据、交易计划、确定性评分、风险与缺失”等 Markdown 章节。AnySearch 资料并入基本面维度的证据状态和执行影响，不单独拼接“基本面补充资料”附录；标题、来源、日期和链接保留在 Trace 工具记录中供审计。历史 Trace 在读取时应用同一展示规则，但不会改写原 artifact。
