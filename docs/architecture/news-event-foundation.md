# 新闻事件研究地基（字段、映射与结算）

本文只描述“让以后能做事件研究”所需的最小数据结构与标注口径。它不定义权重、不参与评分、不进入选股、不改提示词、不改规则 8 与任何阈值。

## 1. 范围

- 新闻入库时补齐：证券代码、交易日口径日期、细分事件类型。
- 历史存量一次性回填：`raw_news_episodes` 与 `news_extracted_events`，不重新抓新闻。
- 事件结果标注：T+1 入场，事件后 1 / 5 个交易日收盘的前复权超额收益，写入 `news_signal_outcomes`。
- 采集与结算均有开关，默认关闭；手工脚本不受开关限制。

非目标：事件聚类、去重决策、权重、评分、排序、仓位、action、prompt、阈值。

## 2. 新增字段

### `raw_news_episodes`

| 字段 | 含义 |
| --- | --- |
| `stock_codes_json` | 证券引用数组，元素为 `{"code","name","source"}`；`source` 取值 `text_code` / `stocks_payload` / `name_map` |
| `stock_scope` | `single` / `multi` / `no_stock`；`unknown` 表示该行未经过地基补齐（回填前的历史行，或开关关闭时新入库的行） |
| `trade_date` | 交易日口径日期；无法解析为 `NULL` |
| `event_category` | 主事件类型（见第 4 节） |
| `event_category_rule` | 命中的规则 id，例如 `kw.share_reduction.v1` |
| `event_categories_json` | 全部命中类型（可多选） |

### `news_extracted_events`

在 raw 字段基础上增加 `primary_stock_code`：仅当 `stock_scope=single` 时写入，否则为 `NULL`。

### `news_signal_outcomes`

保留原有卡片 / 种子字段，新增事件级字段：

`event_id`、`stock_code`、`stock_scope`、`trade_date`、`entry_date`、`exit_date`、`horizon`、`stock_return_pct`、`benchmark_code`、`benchmark_return_pct`、`excess_return_pct`、`cost_bps`、`reason`、`dataset_version`。

事件行的唯一键为 `(event_id, stock_code, horizon)`。旧卡片 / 种子行这些字段为 `NULL`，不受影响。

“无标的”不是空值：统一写 `stock_scope=no_stock`，`stock_codes_json=[]`。

## 3. 证券代码抽取顺序

1. 原文中显式的 6 位代码（带 `.SH` / `.SZ` / `.BJ` 或 `SH`/`SZ` 前缀也能识别），且前缀属于 A 股常见代码段。
2. 结构化 `stocks` 字段（CLS 的 `stock_list`、主题映射股票等）。
3. `entity_links` 中 `entity_type=company` 的条目。
4. 名称映射 `src/data/stock_mapping.py` 中长度 ≥3 的公司名精确子串命中。

以上来源全部保留在 `source` 字段。抽不到任何代码时写 `no_stock`，不回退猜测。

## 4. 事件类型映射

按下列顺序命中，先命中者为主分类；全部命中项写入 `event_categories_json`。

| 顺序 | 类型 | 规则 id | 关键词（节选） |
| --- | --- | --- | --- |
| 1 | 业绩预告 | `kw.earnings_forecast.v1` | 业绩预告、业绩快报、预增、预减、预亏、扭亏 |
| 2 | 财报 | `kw.financial_report.v1` | 年报、半年报、季报、营收、净利润、毛利率、业绩 |
| 3 | 减持 | `kw.share_reduction.v1` | 减持、拟减持、减持计划、大宗减持 |
| 4 | 增持回购 | `kw.share_increase_buyback.v1` | 增持、回购、股份回购、拟回购 |
| 5 | 质押 | `kw.pledge.v1` | 质押、解押、解除质押 |
| 6 | 监管处罚 | `kw.regulatory_penalty.v1` | 处罚、立案、警示函、监管函、问询函、罚款、违规 |
| 7 | 中标订单 | `kw.order_contract.v1` | 中标、订单、合同、签约、框架协议、采购、供货 |
| 8 | 分红 | `kw.dividend.v1` | 分红、派息、利润分配、送转、每10股 |
| 9 | 诉讼 | `kw.litigation.v1` | 诉讼、仲裁、起诉、判决 |
| 10 | 停复牌 | `kw.suspension_delisting.v1` | 停牌、复牌、暂停上市、退市、风险警示 |
| 11 | 重组并购 | `kw.merger_acquisition.v1` | 重大资产重组、重组、并购、收购、资产注入 |

无关键词命中时，若事件已有粗标签 `event_type`，按 `业绩验证 → 财报`、`大客户/订单 → 中标订单` 回退；否则为 `其他`。这是关键词规则，不做语义推断。

## 5. 交易日口径

`trade_date` 由本地 `stock_daily` 的交易日集合推导：

- 发布时间 ≤ 15:00 且当日为交易日：`trade_date` = 当日；
- 发布时间 > 15:00：顺延到下一个交易日；
- 非交易日：顺延到下一个交易日；
- 只有 `signal_date` 时：若为交易日取当日，否则顺延；
- 日历不足（例如新闻日晚于本地最后交易日）：`trade_date=NULL`，结算写 `trade_date_missing`。

## 6. 结算口径

对每个 `event × stock_code × horizon`：

- 事件日 `d = trade_date`；
- 入场：`d+1` 交易日开盘价；
- 1 日出场：`d+1` 收盘价；
- 5 日出场：`d+5` 交易日收盘价；
- 个股收益使用 `data/adj_factors.db` 的复权因子做前复权：`(close_exit × f_exit) / (open_entry × f_entry) - 1`；
- 基准 `000985.CSI` 读本地 `index_daily` 缓存（`data/factor_eval_cache/benchmark_000985_CSI_*.csv`），`close_exit / open_entry - 1`；**禁止**按裸 6 位代码从 `stock_daily` 取指数（`000985` 在该表是大庆华科）；
- 超额 = 个股收益 − 基准收益 − `cost_bps / 10000`（默认 20bp，只扣个股腿）；
- 结果以百分数写入（`1.23` 表示 1.23%）。

不可结算时写 `data_status=unavailable` 与原因码，不删除行：`trade_date_missing`、`entry_not_reached`、`exit_not_reached`、`stock_bar_missing`、`adj_factor_missing`、`benchmark_unavailable`、`benchmark_bar_missing`、`entry_limit_locked`。已 `settled` 的行默认不重算；`unavailable` 行每次重试，便于行情补齐后自动完成。

`dataset_version` 隔离口径：`news_event_outcome.v1` 是基准误取时期的存量（全部按大庆华科计算，原值保留可追溯）；`news_event_outcome.v2` 使用 `000985.CSI` 指数缓存重算。唯一索引包含 `dataset_version`，两版并存；幂等只针对当前版本，`--recompute` 也只重算当前版本。

## 7. 覆盖率口径

- 有标的覆盖率 = `(single + multi) / total`；`no_stock` 与 `unknown` 都不算覆盖。
- 交易日覆盖率 = `trade_date` 非空行数 / 总行数。
- 分类覆盖率 = `event_category != 其他` 的行数 / 总行数。

### 2026-11-26 回填实测

| 表 | 总数 | single | multi | no_stock | 有标的覆盖率 | trade_date 覆盖率 |
| --- | --- | --- | --- | --- | --- | --- |
| `raw_news_episodes` | 3,566 | 363 | 189 | 3,014 | 15.48% | 86.06% |
| `news_extracted_events` | 3,568 | 371 | 186 | 3,011 | 15.61% | 86.07% |

事件分类分布（`news_extracted_events`）：其他 3,005、财报 147、增持回购 121、停复牌 112、中标订单 95、重组并购 38、监管处罚 35、减持 6、分红 5、诉讼 4、业绩预告 0、质押 0。

回填后新闻仍会继续入库。`NEWS_EVENT_FOUNDATION_ENABLED=false` 时新行保持 `stock_scope=unknown`、`event_category=NULL`，不影响历史口径；开启开关后新行才补齐。截至 2026-11-26 复核，两表总数已增至 3,574 / 3,576，其中各 8 行为回填后新写入的 `unknown` 行。

v1 结算实测（`horizons=1,5`，基准误取为大庆华科 `000985`，20bp，仅作历史留档）：扫描 557 个带标的事件、1,356 个事件×股票对，写入 2,712 行；其中 `settled` 983 行（731 条 1 日、252 条 5 日），`unavailable` 1,729 行。不可结算原因：`exit_not_reached` 710、`entry_not_reached` 384、`trade_date_missing` 346、`stock_bar_missing` 271、`entry_limit_locked` 16、`benchmark_bar_missing` 2。

v2 结算实测（`horizons=1,5`，基准 `000985.CSI`，20bp）：扫描 808 个带标的事件、1,975 个事件×股票对，写入 3,950 行。指数缓存只到 2026-11-17 时，`settled` 462 行、`unavailable` 3,488 行（其中 813 行 `benchmark_bar_missing`）。补齐缓存至 2026-11-24 后重跑：`settled` 1,273 行（963 条 1 日、310 条 5 日）、`unavailable` 2,677 行；新增 811 条全部是出场日落在 2026-11-18~11-24 的行，新增部分中位 -0.0325%、均值 +0.1184%、正超额占比 49.94%。剩余不可结算原因：`exit_not_reached` 1,199、`stock_bar_missing` 776、`trade_date_missing` 680、`entry_limit_locked` 20、`benchmark_bar_missing` 2（出场日为 2026-11-25，个股行情尚未入库）。

v1/v2 同键对比（`(event_id, stock_code, horizon)`；v1 的 983 条 settled 已全部被 v2 覆盖，匹配集 983 对）：

| 口径 | 可用条数 | 超额中位数 | 超额均值 |
| --- | --- | --- | --- |
| v1 全量 settled | 983 | 0.3407% | 0.4834% |
| v2 全量 settled | 1,273 | -0.5512% | -0.3368% |
| v1 匹配集 | 983 | 0.3407% | 0.4834% |
| v2 匹配集 | 983 | -0.6786% | -0.5698% |

方向反转 156/983，即每 1,000 条约 158.70 条；匹配集超额变化均值 -1.0532pct、中位数 -0.9071pct。分类别中位超额变化（v2 − v1）：其他 -0.9071pct（847 对）、增持回购 -0.9071pct（61 对）、财报 -0.2081pct（30 对）、中标订单 -1.7608pct（19 对）、停复牌 -0.9070pct（9 对）、监管处罚 -0.9071pct（7 对）、减持 -2.6861pct（5 对）、重组并购 +0.0553pct（4 对）、分红 -0.9071pct（1 对）。

## 8. 命令

```bash
# 回填（默认写库并输出 data/news_event_foundation_backfill.json）
python scripts/backfill_news_event_foundation.py
python scripts/backfill_news_event_foundation.py --dry-run

# 结算（默认 1、5 日；--recompute 重算已结算行）
python scripts/settle_news_signal_outcomes.py
python scripts/settle_news_signal_outcomes.py --dry-run
python scripts/settle_news_signal_outcomes.py --horizons 1,5 --benchmark 000985.CSI --cost-bps 20
```

## 9. 开关

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `NEWS_EVENT_FOUNDATION_ENABLED` | `false` | 入库时补齐三个基础字段；关闭时入库行为与旧版一致 |
| `NEWS_SIGNAL_OUTCOME_SETTLEMENT_ENABLED` | `false` | 预留自动结算开关；手工脚本不受限制 |
| `NEWS_SIGNAL_OUTCOME_BENCHMARK` | `000985.CSI` | 基准指数（走 `index_daily` 缓存，禁止裸码查 `stock_daily`） |
| `INDEX_BENCHMARK_CACHE_DIR` | 空（`data/factor_eval_cache`） | 指数基准 CSV 缓存目录 |
| `INDEX_BENCHMARK_ALLOW_FETCH` | `false` | 缓存缺口时是否允许 Tushare `index_daily` 补数（失败不阻断） |
| `NEWS_SIGNAL_OUTCOME_COST_BPS` | `20` | 个股腿单边成本 |
| `NEWS_SIGNAL_OUTCOME_HORIZONS` | `1,5` | 事件窗口 |
| `NEWS_SIGNAL_OUTCOME_ADJ_DB` | `./data/adj_factors.db` | 复权因子库 |

## 10. 已知局限

- 86% 的新闻没有可解析证券代码；`multi` 行多为行业 / 主题映射股票，不等于事件主体。
- 事件类型是关键词映射，存在“业绩说明会”被判为财报、“减持”出现在否定句仍命中等误判。
- 新闻正文多为快讯摘要（`content` 中位 121 字），不是公告全文。
- `trade_date` 依赖本地 `stock_daily` 日历；新闻日晚于本地最后交易日时无法得到交易日，结算写 `trade_date_missing`。
- 指数基准缓存已补齐到 2026-11-24（`benchmark_000985_CSI_20261118_20261124.csv`）。该区间因 Tushare token 失效，改用 akshare 腾讯源 `sh000985`：baostock 无 `sh.000985`，新浪接口止于 2016-06-13；写入前与既有缓存 1,237 个重叠交易日逐日对齐（close 最大偏差 0.00013%、日涨跌幅最大偏差 0.00023pp），与上证指数日收益相关性 0.9449。2026-11-25 起出场日仍写 `benchmark_bar_missing`，需个股行情和基准缓存同时补齐后再重跑。
- 事件分布高度集中在“消息催化”，细分类型样本量很小；5 日窗口对近期事件普遍 `exit_not_reached`。
- 复权因子最后日期可能早于行情最后日期，缺失时写 `adj_factor_missing`，不用原始价代替前复权。
