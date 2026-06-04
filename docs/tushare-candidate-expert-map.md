# TuShare 候选专家工具补全记录

> 这是一份工作备忘，不是最终接口契约。目标是把“候选专家需要什么工具、TuShare 现在能补什么、下一步该补哪一层”先固定下来，避免后续接接口时来回翻代码。

## 1. 先说明一个容易混淆的点

- `doc_id=93` 是左侧目录里的“功能序号”，对应的是“个股资金流向（THS）”这一项。
- 真正的接口页是 `doc_id=348`，接口名是 `moneyflow_ths`。
- 后续接 TuShare 时，应以 `api_name` 为准，不要把目录序号当成接口编号。

## 2. 当前候选专家和已接工具

| 候选专家 | 现有职责 | 当前主要工具 |
| --- | --- | --- |
| 策略多因子专家 | AlphaSift YAML 策略召回 | `AlphaSiftCandidateProvider` |
| 技术形态专家 | 突破、RPS、形态、动量 | `SequoiaCandidateProvider` |
| 板块主题专家 | 强势板块扩散到个股 | `get_sector_rankings`、AkShare 板块成分 |
| 资金面专家 | 涨停池、人气、游资、资金承接 | `get_tushare_moneyflow_ths`、`get_tushare_moneyflow_dc`、`get_tushare_dragon_tiger_list`、`get_tushare_dragon_tiger_inst`、`get_tushare_limit_list_ths`、`get_tushare_limit_list_d`、`get_tushare_limit_step`、`get_tushare_hot_rank`、`get_capital_flow`、`get_market_capital_flow`、StockAPI 资金相关工具 |
| 消息事件专家 | 公司级新闻、公告、硬事件 | `search_stock_news`、`search_comprehensive_intel`、`get_tushare_reference_events` |
| 情绪/宏观事件专家 | 宏观、地缘、主题扩散 | `event_impact`、`news_momentum` |
| 基本面专家 | 估值、成长、财务、参考事件 | `get_stock_info`、`get_tushare_basic_data`、`get_tushare_financial_statements` |

## 3. 现在已经能直接用的 TuShare 接口

| 接口 | 作用 | 仓库状态 |
| --- | --- | --- |
| `moneyflow` | 个股主力资金流 | 已接到 `get_capital_flow` 主链路 |
| `moneyflow_ths` | 个股资金流向（THS） | 已接到资金面候选专家首位工具 |
| `moneyflow_ind_ths` | 行业资金流向（THS） | 已接到 `get_tushare_moneyflow_ind_ths`，并用于板块主题专家快路径 |
| `moneyflow_ind_dc` | 行业资金流向（东财） | 已接到 `get_tushare_moneyflow_ind_dc`，并用于板块主题专家快路径 |
| `moneyflow_dc` | 个股资金流向（东财） | 已接到 `get_tushare_moneyflow_dc` |
| `cyq_chips` | 筹码分布 | 已接到 `get_chip_distribution` |
| `margin` | 融资融券汇总 | 已接到 `get_margin_trading_summary` |
| `top_list` | 龙虎榜每日明细 | 已接到 `get_tushare_dragon_tiger_list`，也保留在参考事件工具中 |
| `top_inst` | 龙虎榜机构席位明细 | 已接到 `get_tushare_dragon_tiger_inst` |
| `limit_list_ths` | 涨跌停榜单（THS） | 已接到 `get_tushare_limit_list_ths` |
| `limit_list_d` | 涨跌停榜单（日级） | 已接到 `get_tushare_limit_list_d` |
| `limit_step` | 连板天梯 | 已接到 `get_tushare_limit_step` |
| `ths_hot` / `dc_hot` | 同花顺 / 东方财富热榜 | 已接到 `get_tushare_hot_rank` |
| `share_float` | 限售解禁 | 已在参考事件工具中覆盖 |
| `stk_holdertrade` | 股东增减持 | 已在参考事件工具中覆盖 |
| `pledge_stat` / `pledge_detail` | 质押统计和明细 | 已接到 `get_tushare_pledge_stat` / `get_tushare_pledge_detail` |
| `share_float` | 限售解禁 | 已接到 `get_tushare_share_float` |
| `stk_holdertrade` | 股东增减持 | 已接到 `get_tushare_holder_trade` |
| `anns_d` | 公司公告 | 已接到 `get_tushare_announcements` |
| `stk_alert` | 风险提示 | 已接到 `get_tushare_stock_alerts` |
| `stk_shock` | 异常波动 | 已接到 `get_tushare_stock_shock` |
| `repurchase` | 回购 | 已接到 `get_tushare_repurchase` |
| `stock_basic` | 股票基础信息 | 已接到 `get_tushare_basic_data` |
| `daily_basic` | 每日指标 | 已接到 `get_tushare_daily_basic` |
| `fina_indicator` | 财务指标 | 已接到 `get_tushare_financial_indicators` |
| `forecast` | 业绩预告 | 已接到 `get_tushare_forecast` |
| `express` | 业绩快报 | 已接到 `get_tushare_express` |
| `dividend` | 分红送股 | 已接到 `get_tushare_dividend` |
| `adj_factor` | 复权因子 | 已接到 `get_tushare_adj_factor` |
| `index_daily` | 指数日线行情 | 已接到 `get_tushare_index_daily` |
| `trade_cal` | 交易日历 | 已接到 `get_tushare_trade_calendar` |
| `daily` / `weekly` / `monthly` | K 线 | 已接到 `get_tushare_daily_bars` |
| `income` / `balancesheet` / `cashflow` | 三表 | 已接到 `get_tushare_financial_statements` |

## 4. 最适合补给哪个专家

### 4.1 资金面专家

优先补这些接口：

- `moneyflow_ths`
- `moneyflow_dc`
- `top_list`
- `top_inst`
- `limit_list_ths`
- `limit_list_d`
- `limit_step`
- `ths_hot`
- `dc_hot`

`moneyflow_ths` 已先接成 `get_tushare_moneyflow_ths`，在 L1 资金面候选专家里优先调用。随后已补 `moneyflow_dc`、`top_list`、`top_inst`、`limit_list_ths`、`limit_list_d`、`limit_step`、`ths_hot`、`dc_hot`，统一进入资金面候选包；这些工具本身都只查 TuShare，不做其他数据源 fallback。

当前工具映射：

| TuShare 接口 | Agent 工具 | 候选专家来源名 |
| --- | --- | --- |
| `moneyflow_ths` | `get_tushare_moneyflow_ths` | `tushare_moneyflow_ths` |
| `moneyflow_dc` | `get_tushare_moneyflow_dc` | `tushare_moneyflow_dc` |
| `top_list` | `get_tushare_dragon_tiger_list` | `tushare_dragon_tiger_list` |
| `top_inst` | `get_tushare_dragon_tiger_inst` | `tushare_dragon_tiger_inst` |
| `limit_list_ths` | `get_tushare_limit_list_ths` | `tushare_limit_list_ths` |
| `limit_list_d` | `get_tushare_limit_list_d` | `tushare_limit_list_d` |
| `limit_step` | `get_tushare_limit_step` | `tushare_limit_step` |
| `ths_hot` / `dc_hot` | `get_tushare_hot_rank` | `tushare_hot_rank` |

### 4.2 板块主题专家

已接这些接口：

- `moneyflow_ind_ths`
- `moneyflow_ind_dc`
- `moneyflow_cnt_ths`
- `ths_member`

这层现在可以直接输出“板块为什么热”，不只是“板块涨了多少”。

### 4.3 消息事件专家

已接这些接口：

- `anns_d`
- `stk_alert`
- `stk_shock`
- `share_float`
- `stk_holdertrade`
- `pledge_stat`
- `pledge_detail`
- `repurchase`

这层现在可以输出硬事件，不只依赖新闻摘要。

自动候选链路已把 `anns_d`、`stk_alert`、`stk_shock`、`share_float`、`stk_holdertrade`、`repurchase` 作为结构化事件源接入消息事件专家；负向或风险类事件只作为反证/风险提示进入后续审查，不作为单独利好。

### 4.4 基本面专家

已接这些接口：

- `daily_basic`
- `fina_indicator`
- `forecast`
- `express`
- `dividend`

这层更适合做候选快照，而不是实时盘中判断。

自动候选链路当前只接 `daily_basic` 作为全市场基本面快照源；`forecast`、`express`、`dividend` 在当前 TuShare 网关下需要 `stock_code`、`period`、`ann_date` 或 `record_date` 等精确条件，保留为单股深挖工具，不做裸参数全市场召回。

### 4.5 技术/候选发现

已接这些接口：

- `adj_factor`
- `daily_basic`
- `index_daily`
- `trade_cal`

这层服务 AlphaSift / Sequoia 和候选池离线构建。

## 5. 建议接入顺序

1. 已补 `moneyflow_ths`，接到资金面专家首位候选源。
2. 已补 `moneyflow_dc`、`top_list`、`top_inst`、`limit_list_ths`、`limit_list_d`、`limit_step`、`ths_hot`、`dc_hot`，把资金候选做成可交叉验证。
3. 已补 `moneyflow_ind_ths`、`moneyflow_ind_dc`、`moneyflow_cnt_ths`、`ths_member`，板块主题专家可以直接做主题归因。
4. 已补 `anns_d`、`stk_alert`、`stk_shock`、`share_float`、`stk_holdertrade`、`pledge_stat`、`pledge_detail`、`repurchase`，消息事件已经变成结构化硬事件。

## 6. 代码落点

后续如果继续扩展，主要改这些位置：

- `src/agent/tools/data_tools.py`
- `src/agent/tools/market_tools.py`
- `data_provider/fundamental_adapter.py`
- `src/agent/candidate_experts/orchestrator.py`

## 7. 当前结论

- 底层 TuShare 客户端已经通了，不缺通用调用层。
- 真正缺的是按候选专家拆开的结构化接口映射。
- 资金面候选专家已补齐 TuShare 资金流、龙虎榜、涨停榜、连板和热榜入口，默认按最近完成交易日拉全市场 rows，归一化成候选专家可直接合并的 `code/name/source/reason/metrics` 结构。
