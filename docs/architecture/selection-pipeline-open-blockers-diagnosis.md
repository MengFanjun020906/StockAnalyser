# 选股链路「几乎不输出 open」阻断点诊断

> 范围：仅诊断，不含任何代码、配置、阈值改动。
> 数据来源：本地 `data/agent_traces/` 真实 trace，全部为只读统计，未做估算。
> 生成日期：2026-11-19

---

## 0. 结论摘要

「几乎不输出 open」不是单点故障，而是三层独立问题叠加：

| 层 | 阻断机制 | 直接证据 | 性质 |
| --- | --- | --- | --- |
| 规则层 | 深度分析 prompt 规定「6 维中 ≥2 维 missing/tool_failed/unknown → action_bias 不得 open」 | 131 条深度结果里 67 条满足该条件，其中 57 条输出 `wait`、9 条 `monitor`、1 条 `conditional_open`，0 条 `open` | 规则仅存在于 prompt，代码层无强制校验 |
| 数据层 | 深度分析取证不覆盖板块/市场；新闻卡字段契约错误 | 深度分析证据中 sector/regime 卡为 0 张；131 张新闻卡 `missing_fields` 全部为 `["report"]` | 结构性缺口 + 机械性缺陷，与新闻源覆盖面无关 |
| 阈值层 | `strong_watch=85` / `conditional_entry=88` 与真实分数分布不匹配 | 232 条初筛分 p50=46、p90=64、max=83，≥85 为 0、≥88 为 0；127 条计划 `candidate_score` 全为 `None` | 阈值并非「0 open」的直接原因，但属于经验不可达 + 字段缺失 + 被显式字段短路 |

四条判断必须分开看：

1. **67 条不是代码拒绝**。仓库里没有任何代码读取 `dimension_summary` 去否决 `open`，规则只写在 prompt 文本里（`src/agent/stock_selection_prompts.py:228`）。67 是「LLM 输出恰好符合该规则约束」的记录数。
2. **消息面 66 条缺失不是新闻源没覆盖**。66 条中 58 条证据卡状态为 `ok`、6 条 `partial`、仅 2 条 `empty`；新闻工具 236 次调用中 232 次成功。主因是模型把已有数据判成 `missing`，叠加字段契约把 `report` 当作期望字段。
3. **85/88 阈值在真实链路中没有成为主要门槛**。阈值读取的是 `candidate_score`，而 127 条计划里该字段 100% 为 `None`；同时 106/127 条计划自带 LLM 显式 `execution_mode`，`_execution_mode()` 对显式值直接短路（`src/agent/stock_selection.py:6371-6373`），根本不进入阈值判断。
4. **补齐维度不等于解锁 open（本轮新增的关键反证）**。131 条里「0 维缺失」的 24 条没有一条 `open`（19 wait / 2 monitor / 3 reject）；唯一 `open` 的贵州茅台（600519）反而有 1 维缺失。规则层理论上可让 64 条不再被约束，但按实证清洁样本外推，补齐后的期望 open 约为 0-2 条。

---

## 1. 样本口径与可复现方法

### 1.1 样本范围

| 项 | 数量 | 说明 |
| --- | --- | --- |
| `data/agent_traces/` 目录 | 449 | 全部 trace |
| `request.json` 中 `candidate_discovery_mode=thesis_desk_committee` | 206 | 本次诊断的有效全集 |
| 有 `candidate_discovery.json` 且候选非空 | 44 | 共 257 条候选 |
| 有 `candidate_screening.json` 且 shortlist 非空 | 43 | 共 232 条初筛记录 |
| 有 `single_stock_deep_dive.json` 且 results 非空 | 43 | 共 131 条深度结果（108 个唯一代码，23 条重复） |
| 有 `portfolio_allocation.json` 且 `positions_plan` 非空 | 42 | 共 127 条计划 |
| 深度分析日期范围 | — | 2026-05-30 ~ 2026-11-16 |

> 口径说明：早期曾把 449 个 trace 全量混入统计，得到 358 条深度结果，与用户观察的 131 条不符。按 `candidate_discovery_mode=thesis_desk_committee` 且深度结果非空过滤后精确复现 131 / 129 / 67 / 66 / 47 这组数字，故本文件全部采用该口径。

### 1.2 关键字段路径

- 深度结果：`single_stock_deep_dive.json` → `full.results[].summary.action_bias`、`full.dimension_summary`
- 证据卡：`evidence_cards.json` → `[].dimension`、`[].data_quality.status`、`[].data_quality.missing_fields`
- 初筛：`candidate_screening.json` → `full.shortlist[].score`、`full.shortlist[].score_breakdown`
- 候选种子：`candidate_discovery.json` → `full.candidates[].signal_score|final_score|score`
- 组合计划：`portfolio_allocation.json` → `full.positions_plan[].execution_mode`、`[].candidate_score`
- 工具调用：`tool_calls.json` → `[].tool`、`[].arguments.stock_code`、`[].success`、`[].duration`

### 1.3 复现方式

统计使用仓库内 `.venv/bin/python` 内联脚本，只 `json.load` 上述文件并聚合，未写回、未生成中间文件。任何一条数字均可用同样字段路径重算。

---

## 2. 问题一：维度缺失硬规则

### 2.1 六个维度是什么

`src/agent/stock_selection_prompts.py:271-277` 的 `dimension_summary` schema 定义：

| 维度 key | 含义 | 期望证据来源 |
| --- | --- | --- |
| `technical` | 技术结构 | `analyze_trend` / `calculate_ma` / `get_volume_analysis` / `analyze_pattern` |
| `fundamental` | 基本面 | `get_stock_info` / `get_tushare_*` / `search_stock_fundamentals` |
| `news_event` | 消息面 | `search_comprehensive_intel` |
| `capital_flow` | 资金/筹码 | `get_capital_flow` / `get_chip_distribution` |
| `market_sector` | 板块/市场 | `get_sector_rankings` / `detect_market_regime` / `get_market_indices` |
| `account_fit` | 账户适配 | 用户账户上下文，非工具 |

每维 verdict 枚举（`src/agent/stock_selection_prompts.py:35`）：

```
dimension_verdict = support | weaken | neutral | missing | tool_failed | unknown
```

### 2.2 规则写在哪里

| 规则 | 位置 | 原文要点 |
| --- | --- | --- |
| 深度分析：≥2 维坏 → 不得 open | `src/agent/stock_selection_prompts.py:228` | 「6 个核心维度中有 2 个以上为 missing / tool_failed / unknown 时，action_bias 不得为 open。」 |
| 初筛：≥2 维坏 → 最高 monitor | `src/agent/stock_selection_prompts.py:184` | 「2 个以上核心维度为 missing / tool_failed 时，screening_result 最高只能为 monitor。」 |
| 深度分析：无止损不得 open | `src/agent/stock_selection_prompts.py`（同段第 7 条） | 「如果无法给止损位，不得建议 open。」 |

**关键事实：这三条都只是 prompt 指令，`src/agent/stock_selection.py` 中不存在任何读取 `dimension_summary` 来判定 `action_bias` 是否合法的代码。** 检索 `dimension_summary` 的消费点只出现在 prompt 构造与 fallback 生成处，没有任何 veto 分支。

### 2.3 「缺失」的精确判定条件

存在两条不同的判定链，且互不一致：

#### A. LLM 输出的 verdict（主链）

模型在 `full.dimension_summary.<dim>.verdict` 里自行给值。是否算「缺失」完全由模型判断，代码只做字符串归类。

#### B. `EvidenceCard.data_quality.status`（证据层）

`src/agent/evidence/adapter.py:213-221` `_quality_status`：

```
timeout           → "timeout"
raw 显式 status   → 原样透传（ok / empty / partial / failed ...）
有 error 字段     → "failed"
其余              → "partial"
```

`src/agent/evidence/adapter.py:253-268` `_missing_fields_for`：按工具名给期望字段，缺失即记入 `missing_fields`。

这两条链的语义差异：

- `status=ok` 只代表工具返回了结构合法的结果，不代表字段齐全；
- `missing_fields` 只代表「期望字段名没出现」，不代表数据真的没有；
- verdict `missing` 是模型对「这条证据能不能支撑结论」的判断，三者可以互相矛盾。

#### C. 是否区分「确实没数据」与「查询失败」

**部分区分，但不彻底。**

- `tool_failed` 明确对应工具失败；
- `missing` 同时被用于「工具没调用」「工具返回空」「模型认为信息不足」三种情况；
- fallback 实现 `_dimension_summary_from_evidence`（`src/agent/stock_selection.py:5972-6001`）里，`news_event` 的判定是 `"tool_failed" if "search_comprehensive_intel" in failures else ("neutral" if evidence.get(...) else "missing")`——即「有 dict 就算 neutral」，不检查 dict 是否为空、items 是否为 0；
- `market_sector` 在 fallback 中**永远不会**产出 `missing`/`tool_failed`（只有 `risk_off/panic` → `weaken`，否则一律 `neutral`）。

因此「确实没数据」和「查询失败」只在 `tool_failed` 这一档被区分，其余全部压缩进 `missing`/`neutral`。

### 2.4 现状数字

131 条深度结果的 `action_bias` 分布：

| action_bias | 条数 |
| --- | --- |
| `wait` | 109 |
| `monitor` | 17 |
| `reject` | 3 |
| `open` | 1 |
| `conditional_open` | 1 |

→ 按「仅 open 视为开仓」口径，**129 条非 open**；即使把 `conditional_open` 计入，也只有 2 条。

67 条命中「≥2 维坏」的记录，其 action 分布：

| action_bias | 条数 |
| --- | --- |
| `wait` | 57 |
| `monitor` | 9 |
| `conditional_open` | 1 |
| `open` | 0 |

→ 命中规则的 67 条里**没有任何 open**。

各维度 verdict 分布（131 条）：

| 维度 | 分布 |
| --- | --- |
| `technical` | support 58 / weaken 58 / neutral 14 / 非枚举值 1 |
| `fundamental` | neutral 75 / support 28 / missing 23 / unknown 4 / tool_failed 1 |
| `news_event` | missing 66 / neutral 31 / unknown 12 / weaken 8 / support 4 / tool_failed 3 / wait_confirm 6 / weak 1 |
| `capital_flow` | support 69 / neutral 26 / weaken 22 / tool_failed 12 / oppose 1 / mixed 1 |
| `market_sector` | missing 47 / weaken 25 / neutral 25 / tool_failed 16 / support 16 / unknown 2 |
| `account_fit` | neutral 82 / weaken 32 / support 16 / missing 1 |

**注意：共 10 个非 schema 枚举值**（technical 1、news 7、capital 2，如 `wait_confirm`/`weak`/`oppose`/`mixed`）。它们不在规则的 `missing/tool_failed/unknown` 集合内，因此**规则实际统计可能低估了「缺失」**。

67 条的子结构：

| 项 | 条数 |
| --- | --- |
| news 维坏 | 60 |
| sector 维坏 | 55 |
| news 与 sector 同时坏 | 49 |
| 仅 news 坏（单维，全 131 口径） | 21 |
| 仅 sector 坏（单维，全 131 口径） | 10 |
| 仅 capital_flow 坏（单维，全 131 口径） | 7 |
| 仅 fundamental 坏（单维，全 131 口径） | 2 |

坏维度数直方图（全 131 条）：

| 坏维度数 | 条数 |
| --- | --- |
| 0 | 24 |
| 1 | 40 |
| 2 | 55 |
| 3 | 11 |
| 4 | 1 |

组合分布（67 条）：

| 缺失维度组合 | 条数 |
| --- | --- |
| `news_event` + `market_sector` | 38 |
| `fundamental` + `news_event` | 11 |
| `fundamental` + `news_event` + `market_sector` | 9 |
| `fundamental` + `market_sector` | 3 |
| `account_fit` + `market_sector` | 1 |
| `capital_flow` + `fundamental` | 1 |
| `capital_flow` + `fundamental` + `market_sector` + `news_event` | 1 |
| `capital_flow` + `market_sector` + `news_event` | 1 |
| `capital_flow` + `fundamental` + `market_sector` | 1 |
| `capital_flow` + `market_sector` | 1 |

#### 2.4.1 逐条归因（67 条）

原因代码：

| 代码 | 含义 |
| --- | --- |
| `N1` | 新闻工具成功、证据卡 `ok/partial`、items ≥3，却被判 `missing`（有数据但判缺失） |
| `N2` | 新闻工具成功但 items 只有 1-2 条（数据稀疏） |
| `N3` | 新闻工具成功但返回 0 条，证据卡 `empty`（无返回） |
| `N4` | 新闻工具失败/超时 |
| `S1` | 深度分析未采集板块证据，但上游 `schema.base.industry` **存在**（纯注入缺失） |
| `S2` | 深度分析未采集板块证据，且上游 `industry` **也缺失**（需板块映射/数据源） |
| `F1` | 基本面证据卡失败/超时 |
| `F3` | 基本面有证据卡但被判 `missing` |
| `C1` | 资金流证据卡失败/超时 |
| `A1` | `account_fit` 由账户上下文推导，无对应工具数据 |

| 时间 | 代码/名称 | action | 缺失维度 | 逐维原因 |
| --- | --- | --- | --- | --- |
| 05-30 19:56 | 000506 招金黄金 | wait | fundamental / market_sector | F3 / S2 |
| 05-30 19:56 | 688677 海泰新光 | wait | market_sector / news_event | S2 / N1 |
| 05-30 19:56 | 000883 湖北能源 | wait | market_sector / news_event | S2 / N1 |
| 05-30 19:56 | 000875 电投绿能 | wait | account_fit / market_sector | A1 / S2 |
| 05-30 21:43 | 600027 华电国际 | wait | market_sector / news_event | S2 / N1 |
| 05-30 21:43 | 000567 海德股份 | wait | fundamental / market_sector / news_event | F3 / S2 / N1 |
| 05-30 21:43 | 688677 海泰新光 | wait | market_sector / news_event | S2 / N1 |
| 05-31 21:20 | 600027 华电国际 | wait | market_sector / news_event | S1 / N1 |
| 06-01 19:24 | 000426 兴业银锡 | wait | market_sector / news_event | S1 / N1 |
| 06-01 19:24 | 000623 吉林敖东 | wait | market_sector / news_event | S1 / N1 |
| 06-02 19:54 | 300408 三环集团 | wait | fundamental / news_event | F3 / N1 |
| 06-04 08:28 | 603124 江南新材 | wait | market_sector / news_event | S1 / N1 |
| 06-04 08:28 | 301115 联检科技 | wait | fundamental / market_sector / news_event | F3 / S1 / N1 |
| 06-05 08:04 | 002106 莱宝高科 | wait | fundamental / market_sector | F3 / S2 |
| 06-12 11:10 | 300307 慈星股份 | monitor | market_sector / news_event | S1 / N1 |
| 06-12 14:20 | 002718 友邦吊顶 | monitor | market_sector / news_event | S1 / N1 |
| 06-13 11:10 | 000630 铜陵有色 | wait | market_sector / news_event | S2 / N1 |
| 06-13 11:10 | 603993 洛阳钼业 | wait | market_sector / news_event | S1 / N1 |
| 06-13 11:10 | 600141 兴发集团 | wait | market_sector / news_event | S1 / N1 |
| 06-13 22:35 | 603993 洛阳钼业 | wait | market_sector / news_event | S1 / N1 |
| 06-13 22:35 | 000032 深桑达Ａ | wait | market_sector / news_event | S1 / N1 |
| 06-15 19:21 | 000758 中色股份 | wait | fundamental / news_event | F3 / N3 |
| 06-15 19:21 | 000510 新金路 | monitor | market_sector / news_event | S2 / N3 |
| 06-16 22:54 | 002155 湖南黄金 | wait | market_sector / news_event | S1 / N1 |
| 06-17 22:08 | 300308 中际旭创 | wait | capital_flow / fundamental | C1 / F3 |
| 06-17 22:08 | 688401 路维光电 | wait | capital_flow / fundamental / market_sector / news_event | C1 / F3 / S2 / N1 |
| 06-21 14:27 | 300861 美畅股份 | wait | market_sector / news_event | S1 / N2 |
| 06-21 14:27 | 600353 旭光电子 | monitor | fundamental / news_event | F3 / N1 |
| 06-22 18:12 | 603072 天和磁材 | monitor | fundamental / news_event | F3 / N1 |
| 06-29 17:51 | 603986 兆易创新 | wait | fundamental / news_event | F3 / N2 |
| 06-29 17:51 | 002180 纳思达 | wait | fundamental / market_sector / news_event | F3 / S1 / N2 |
| 06-29 17:51 | 301197 工大科雅 | wait | market_sector / news_event | S2 / N3 |
| 07-01 10:31 | 603726 朗迪集团 | wait | market_sector / news_event | S1 / N1 |
| 07-02 10:56 | 603607 京华激光 | wait | fundamental / news_event | F3 / N1 |
| 07-04 22:13 | 301051 信濠光电 | monitor | fundamental / news_event | F3 / N1 |
| 07-04 22:13 | 688017 绿的谐波 | wait | fundamental / news_event | F3 / N1 |
| 07-07 10:35 | 002249 大洋电机 | monitor | market_sector / news_event | S1 / N1 |
| 07-08 14:32 | 688002 睿创微纳 | wait | market_sector / news_event | S1 / N1 |
| 07-08 14:32 | 688012 中微公司 | wait | fundamental / market_sector / news_event | F3 / S1 / N1 |
| 07-08 14:32 | 002268 电科网安 | conditional_open | market_sector / news_event | S1 / N1 |
| 07-14 10:45 | 600285 羚锐制药 | wait | fundamental / market_sector / news_event | F3 / S1 / N1 |
| 07-15 16:18 | 300791 仙乐健康 | wait | capital_flow / market_sector / news_event | C1 / S1 / N1 |
| 07-15 16:18 | 000977 浪潮信息 | wait | capital_flow / fundamental / market_sector | C1 / F3 / S1 |
| 07-15 16:18 | 002603 以岭药业 | wait | capital_flow / market_sector | C1 / S1 |
| 07-17 14:56 | 603566 普莱柯 | monitor | market_sector / news_event | S1 / N1 |
| 07-17 14:56 | 002128 电投能源 | wait | market_sector / news_event | S1 / N1 |
| 07-17 14:56 | 605050 福然德 | wait | market_sector / news_event | S1 / N1 |
| 07-20 10:47 | 603118 共进股份 | wait | fundamental / market_sector / news_event | F3 / S1 / N1 |
| 07-20 10:47 | 002396 星网锐捷 | wait | fundamental / market_sector | F3 / S1 |
| 07-23 18:18 | 000333 美的集团 | wait | fundamental / market_sector / news_event | F3 / S1 / N1 |
| 07-23 18:18 | 300191 潜能恒信 | wait | fundamental / market_sector / news_event | F3 / S1 / N1 |
| 08-03 18:25 | 603988 中电电机 | wait | market_sector / news_event | S1 / N1 |
| 11-07 22:14 | 603706 东方环宇 | wait | market_sector / news_event | S2 / N1 |
| 11-07 22:14 | 301697 贝特利 | wait | fundamental / news_event | F3 / N1 |
| 11-09 18:23 | 688286 敏芯股份 | wait | fundamental / market_sector / news_event | F3 / S2 / N1 |
| 11-09 18:23 | 920491 奥迪威 | wait | fundamental / news_event | F1 / N1 |
| 11-10 20:33 | 002273 水晶光电 | wait | market_sector / news_event | S2 / N1 |
| 11-10 20:33 | 605123 派克新材 | wait | market_sector / news_event | S2 / N1 |
| 11-10 20:33 | 002729 好利科技 | wait | market_sector / news_event | S2 / N4 |
| 11-11 18:10 | 002273 水晶光电 | wait | market_sector / news_event | S1 / N1 |
| 11-11 18:10 | 603296 华勤技术 | wait | market_sector / news_event | S1 / N1 |
| 11-11 18:10 | 605228 神通科技 | wait | market_sector / news_event | S1 / N1 |
| 11-11 18:10 | 300332 天壕能源 | wait | market_sector / news_event | S1 / N1 |
| 11-11 18:10 | 002050 三花智控 | monitor | fundamental / news_event | F3 / N1 |
| 11-11 18:10 | 300285 国瓷材料 | wait | market_sector / news_event | S1 / N1 |
| 11-12 18:34 | 603296 华勤技术 | wait | market_sector / news_event | S1 / N1 |
| 11-12 18:34 | 002158 汉钟精机 | wait | market_sector / news_event | S1 / N1 |

逐维汇总（67 条内）：

| 维度 | 坏条数 | 原因分布 |
| --- | --- | --- |
| `news_event` | 60 | N1 53 / N2 3 / N3 3 / N4 1 |
| `market_sector` | 55 | S1 38 / S2 17；深度分析 sector/regime 卡 **0 张** |
| `fundamental` | 26 | F3 25 / F1 1 |
| `capital_flow` | 5 | C1 5 |
| `account_fit` | 1 | A1 1 |

> 注：以上为 67 条内的坏维度计数（多维度行会重复计入各维），故各维之和大于 67。

前提条件核查：67 条**全部**已有 `stop_loss`（67/67）、**全部**已有 `ideal_entry_zone`（67/67）；action_strength 为 weak 58 / medium 7 / strong 1 / none 1。也就是说，点位前置条件并不缺，规则是唯一显式阻挡项。

反事实（把某维全部改成 `neutral` 后，仍命中「≥2 维坏」的条数）：

| 反事实 | 仍命中 | 减少 |
| --- | --- | --- |
| `market_sector` 全 neutral | 24 | 43 |
| `news_event` 全 neutral | 18 | 49 |
| 两者全 neutral | 3 | 64 |

→ 这两个维度合计解释了 67 条中的 64 条；把它们修好，理论上「≥2 维坏」的命中会从 67 降到 3。

补充事实：131 条中 14 条是 fallback 生成（维度集合含 `price_structure`，与 LLM 输出不同），这 14 条的 `market_sector` 全部为 `neutral`，因此 67/81/65/47 这些坏维度统计**全部来自 LLM 输出**，不是 fallback 产物。

### 2.5 消息面缺失 66 条的真实原因

#### 证据卡状态

66 条 `news_event=missing` 对应的证据卡状态：

| status | 条数 |
| --- | --- |
| `ok` | 58 |
| `partial` | 6 |
| `empty` | 2 |

更大的口径（81 条 news 维「坏」，含 missing/unknown/tool_failed/非枚举）：

| status | 条数 |
| --- | --- |
| `ok` | 67 |
| `partial` | 9 |
| `empty` | 4 |
| `timeout` | 1 |

**即 88%（58/66）的「消息面 missing」发生在证据卡状态为 `ok` 的情况下，97% 为 `ok/partial`。**

#### 新闻工具调用

| 指标 | 数值 |
| --- | --- |
| `search_comprehensive_intel` 调用次数 | 236 |
| 成功 | 232 |
| 失败 | 4 |
| 返回 items > 0 | 214 |
| 返回 0 items | 22 |
| 耗时 p50 | 10.05 s |
| 耗时 p95 | 49.31 s |

#### 66 条的 items 数量分布

| items 数 | 条数 |
| --- | --- |
| 3-5 条 | 47 |
| ≥6 条 | 11 |
| 1-2 条 | 6 |
| 0 条 | 2 |

（逐条明细见 2.5.1；items 取该标的当次 trace 内新闻工具返回的最大条数。）

其中 43 条内容含事件类关键词（如业绩、公告、减持、监管等），23 条不含。

#### 机械性缺陷：字段契约不一致

`src/agent/evidence/adapter.py:264` 为 `search_comprehensive_intel` 声明的期望字段是 `["report"]`，但工具实际返回体用的是 `intel`（`src/agent/dimension_evidence.py:26` 的映射即 `"search_comprehensive_intel": "intel"`）。结果是 **131 张新闻卡全部带 `missing_fields=["report"]`**，且这个字段名会被 `_dimension_summary_from_cards`（`src/agent/stock_selection.py:3782-3797`）原样写进深度分析 prompt。

这给模型的信号是「消息面字段缺失」，与证据卡 `status=ok` 自相矛盾。

#### 原因归类

| 原因 | 条数（估自证据） | 判断依据 |
| --- | --- | --- |
| 有数据但模型判 missing | 58 | 66 条中 58 条 `status=ok/partial` 且 items ≥3 |
| 数据稀疏（1-2 条） | 6 | items ≤2 的 6 条 |
| 工具成功但 0 条返回 | 2 | 证据卡 `empty` |

#### 2.5.1 逐条核对（66 条）

| 时间 | 代码/名称 | 证据卡 | items | 判定 |
| --- | --- | --- | --- | --- |
| 05-30 19:56 | 000883 湖北能源 | partial | 12 | N1 有数据判 missing |
| 05-30 21:43 | 000567 海德股份 | partial | 11 | N1 有数据判 missing |
| 05-30 21:43 | 688677 海泰新光 | partial | 12 | N1 有数据判 missing |
| 05-31 21:20 | 600027 华电国际 | partial | 10 | N1 有数据判 missing |
| 06-01 19:24 | 000623 吉林敖东 | partial | 10 | N1 有数据判 missing |
| 06-02 19:54 | 300408 三环集团 | partial | 9 | N1 有数据判 missing |
| 06-12 11:10 | 601698 中国卫通 | ok | 3 | N1 有数据判 missing |
| 06-12 11:10 | 300307 慈星股份 | ok | 3 | N1 有数据判 missing |
| 06-12 14:20 | 002718 友邦吊顶 | ok | 3 | N1 有数据判 missing |
| 06-13 11:10 | 000630 铜陵有色 | ok | 3 | N1 有数据判 missing |
| 06-13 11:10 | 603993 洛阳钼业 | ok | 3 | N1 有数据判 missing |
| 06-13 11:10 | 600141 兴发集团 | ok | 3 | N1 有数据判 missing |
| 06-13 22:35 | 603993 洛阳钼业 | ok | 3 | N1 有数据判 missing |
| 06-13 22:35 | 000032 深桑达Ａ | ok | 3 | N1 有数据判 missing |
| 06-15 19:21 | 000510 新金路 | empty | 0 | N3 无返回 |
| 06-16 22:54 | 002155 湖南黄金 | ok | 3 | N1 有数据判 missing |
| 06-17 22:08 | 688401 路维光电 | ok | 3 | N1 有数据判 missing |
| 06-21 14:27 | 603072 天和磁材 | ok | 2 | N2 数据稀疏 |
| 06-21 14:27 | 300861 美畅股份 | ok | 1 | N2 数据稀疏 |
| 06-21 14:27 | 600353 旭光电子 | ok | 3 | N1 有数据判 missing |
| 06-22 18:12 | 603072 天和磁材 | ok | 3 | N1 有数据判 missing |
| 06-22 18:12 | 002861 瀛通通讯 | ok | 3 | N1 有数据判 missing |
| 06-24 18:25 | 603281 江瀚新材 | ok | 2 | N2 数据稀疏 |
| 06-24 18:25 | 301333 诺思格 | ok | 2 | N2 数据稀疏 |
| 06-25 22:41 | 688376 美埃科技 | ok | 3 | N1 有数据判 missing |
| 06-29 17:51 | 603986 兆易创新 | ok | 1 | N2 数据稀疏 |
| 06-29 17:51 | 002180 纳思达 | ok | 1 | N2 数据稀疏 |
| 06-29 17:51 | 301197 工大科雅 | empty | 0 | N3 无返回 |
| 07-01 10:31 | 603726 朗迪集团 | ok | 3 | N1 有数据判 missing |
| 07-02 10:56 | 603607 京华激光 | ok | 5 | N1 有数据判 missing |
| 07-02 10:56 | 000566 海南海药 | ok | 6 | N1 有数据判 missing |
| 07-04 22:13 | 301051 信濠光电 | ok | 6 | N1 有数据判 missing |
| 07-04 22:13 | 688017 绿的谐波 | ok | 6 | N1 有数据判 missing |
| 07-07 10:35 | 000066 中国长城 | ok | 6 | N1 有数据判 missing |
| 07-07 10:35 | 002249 大洋电机 | ok | 4 | N1 有数据判 missing |
| 07-08 14:32 | 688002 睿创微纳 | ok | 5 | N1 有数据判 missing |
| 07-08 14:32 | 688012 中微公司 | ok | 6 | N1 有数据判 missing |
| 07-08 14:32 | 002268 电科网安 | ok | 4 | N1 有数据判 missing |
| 07-14 10:45 | 300519 新光药业 | ok | 3 | N1 有数据判 missing |
| 07-14 10:45 | 600285 羚锐制药 | ok | 3 | N1 有数据判 missing |
| 07-14 10:45 | 688608 恒玄科技 | ok | 4 | N1 有数据判 missing |
| 07-17 14:56 | 603566 普莱柯 | ok | 3 | N1 有数据判 missing |
| 07-17 14:56 | 002128 电投能源 | ok | 3 | N1 有数据判 missing |
| 07-17 14:56 | 605050 福然德 | ok | 3 | N1 有数据判 missing |
| 07-20 10:47 | 603118 共进股份 | ok | 3 | N1 有数据判 missing |
| 07-20 10:47 | 600763 通策医疗 | ok | 3 | N1 有数据判 missing |
| 07-20 10:47 | 603345 安井食品 | ok | 3 | N1 有数据判 missing |
| 07-23 18:18 | 000333 美的集团 | ok | 3 | N1 有数据判 missing |
| 07-23 18:18 | 300738 奥飞数据 | ok | 5 | N1 有数据判 missing |
| 07-23 18:18 | 300191 潜能恒信 | ok | 3 | N1 有数据判 missing |
| 08-03 18:25 | 603988 中电电机 | ok | 3 | N1 有数据判 missing |
| 08-03 18:25 | 300124 汇川技术 | ok | 4 | N1 有数据判 missing |
| 08-03 18:25 | 603260 合盛硅业 | ok | 3 | N1 有数据判 missing |
| 11-07 22:14 | 603706 东方环宇 | ok | 3 | N1 有数据判 missing |
| 11-07 22:14 | 301697 贝特利 | ok | 3 | N1 有数据判 missing |
| 11-10 20:33 | 002273 水晶光电 | ok | 3 | N1 有数据判 missing |
| 11-10 20:33 | 605123 派克新材 | ok | 3 | N1 有数据判 missing |
| 11-11 18:10 | 002050 三花智控 | ok | 3 | N1 有数据判 missing |
| 11-11 18:10 | 605228 神通科技 | ok | 3 | N1 有数据判 missing |
| 11-11 18:10 | 300332 天壕能源 | ok | 3 | N1 有数据判 missing |
| 11-12 18:34 | 603296 华勤技术 | ok | 3 | N1 有数据判 missing |
| 11-12 18:34 | 002158 汉钟精机 | ok | 3 | N1 有数据判 missing |
| 11-12 18:34 | 688525 佰维存储 | ok | 3 | N1 有数据判 missing |
| 11-12 18:34 | 301018 申菱环境 | ok | 3 | N1 有数据判 missing |
| 11-16 17:33 | 000062 深圳华强 | ok | 3 | N1 有数据判 missing |
| 11-16 17:33 | 002035 华帝股份 | ok | 3 | N1 有数据判 missing |

**结论：不是新闻源没覆盖这些标的，也不是查询失败。** 66 条中 64 条拿到了 ≥1 条新闻（58 条 ≥3 条），仅 2 条返回 0 条；无一条因查询失败。主因是「证据已拿到但模型判 missing」，并被 `missing_fields=["report"]` 的错误字段名放大。

---

### 2.6 板块/市场缺失 47 条的真实原因

#### 结构性缺口（主因）

- `_collect_deep_dive_evidence`（`src/agent/stock_selection.py:2578` 起）只调用 7 个工具：行情、趋势、价格结构、资金流、`get_stock_info`、筹码、新闻。**不包含** `get_sector_rankings`、`detect_market_regime`、`get_market_indices`。
- `_deep_dive_prompt_evidence`（`src/agent/stock_selection.py:3699-3722`）只构造 `evidence_cards` 视图、`dimension_summary`、`symbol_regime_probability`、`tool_failures`、`raw_refs`；**不传** `schema.base.industry`、板块排行、板块资金流。
- 因此深度分析证据中 sector/regime 卡为 **0 张**，模型没有可用的板块证据。

#### 上游其实有数据

- 65 条 `market_sector` 坏的记录中，**45 条上游 `balanced_candidate_evidence.json` 的 `schema.base.industry` 非空**（其中 38 条落在 67 条子集内）。数据存在，但没进深度分析 prompt。
- 工具侧成功率：
  - `get_stock_info`：190 次调用，全部成功；
  - `detect_market_regime`：43 次调用，43 次成功；
  - `get_sector_rankings`：43 次调用，26 成功 / **17 失败（约 40%）**。

#### 原因归类

| 原因 | 条数 | 说明 |
| --- | --- | --- |
| 深度分析未采集，但上游行业已存在（S1） | 37 | 纯注入缺失，数据本身有 |
| 深度分析未采集，且上游行业也缺失（S2） | 10 | 需补板块映射/数据源 |
| 板块/市场证据卡 | **0 张** | `_collect_deep_dive_evidence` 未调用板块、市场工具 |

补充：47 条中有 17 条所在 trace 的 `get_sector_rankings` 调用失败（成功率 0/1），30 条调用成功（1/1）。**即使板块工具调用成功，其结果也没有进入深度分析 prompt**，因此「工具失败」不是这 47 条的主因，「未注入」才是。

#### 2.6.1 逐条核对（47 条）

| 时间 | 代码/名称 | 上游 industry | 板块工具（成功/调用） | 判定 |
| --- | --- | --- | --- | --- |
| 05-30 19:56 | 688677 海泰新光 | 无 | 0/1 | S2 |
| 05-30 19:56 | 000883 湖北能源 | 无 | 0/1 | S2 |
| 05-30 21:43 | 688677 海泰新光 | 无 | 0/1 | S2 |
| 05-31 18:48 | 600519 贵州茅台 | 无 | 0/1 | S2 |
| 05-31 21:20 | 600027 华电国际 | 有 | 0/1 | S1 |
| 05-31 21:20 | 002146 荣盛发展 | 有 | 0/1 | S1 |
| 06-01 10:50 | 002146 荣盛发展 | 有 | 0/1 | S1 |
| 06-01 19:24 | 000426 兴业银锡 | 有 | 0/1 | S1 |
| 06-01 19:24 | 000623 吉林敖东 | 有 | 0/1 | S1 |
| 06-12 11:10 | 300307 慈星股份 | 有 | 1/1 | S1 |
| 06-12 14:20 | 002718 友邦吊顶 | 有 | 1/1 | S1 |
| 06-13 11:10 | 603260 合盛硅业 | 有 | 0/1 | S1 |
| 06-13 11:10 | 000630 铜陵有色 | 无 | 0/1 | S2 |
| 06-13 11:10 | 603993 洛阳钼业 | 有 | 0/1 | S1 |
| 06-13 11:10 | 600141 兴发集团 | 有 | 0/1 | S1 |
| 06-15 19:21 | 000510 新金路 | 无 | 0/1 | S2 |
| 06-16 22:54 | 002155 湖南黄金 | 有 | 1/1 | S1 |
| 06-17 22:08 | 688401 路维光电 | 无 | 0/1 | S2 |
| 06-21 14:27 | 300861 美畅股份 | 有 | 1/1 | S1 |
| 06-29 17:51 | 002180 纳思达 | 有 | 1/1 | S1 |
| 06-29 17:51 | 301197 工大科雅 | 无 | 1/1 | S2 |
| 07-01 10:31 | 603726 朗迪集团 | 有 | 1/1 | S1 |
| 07-07 10:35 | 600216 浙江医药 | 有 | 1/1 | S1 |
| 07-07 10:35 | 002249 大洋电机 | 有 | 1/1 | S1 |
| 07-08 14:32 | 605499 东鹏饮料 | 有 | 1/1 | S1 |
| 07-08 14:32 | 688002 睿创微纳 | 有 | 1/1 | S1 |
| 07-08 14:32 | 688012 中微公司 | 有 | 1/1 | S1 |
| 07-08 14:32 | 002268 电科网安 | 有 | 1/1 | S1 |
| 07-14 10:45 | 600285 羚锐制药 | 有 | 1/1 | S1 |
| 07-15 16:18 | 000977 浪潮信息 | 有 | 0/1 | S1 |
| 07-15 16:18 | 002603 以岭药业 | 有 | 0/1 | S1 |
| 07-17 14:56 | 603566 普莱柯 | 有 | 1/1 | S1 |
| 07-17 14:56 | 002128 电投能源 | 有 | 1/1 | S1 |
| 07-17 14:56 | 301335 天元宠物 | 有 | 1/1 | S1 |
| 07-17 14:56 | 605050 福然德 | 有 | 1/1 | S1 |
| 07-20 10:47 | 603118 共进股份 | 有 | 1/1 | S1 |
| 07-20 10:47 | 002396 星网锐捷 | 有 | 1/1 | S1 |
| 07-23 18:18 | 000333 美的集团 | 有 | 1/1 | S1 |
| 07-23 18:18 | 300191 潜能恒信 | 有 | 1/1 | S1 |
| 11-07 22:14 | 603706 东方环宇 | 无 | 1/1 | S2 |
| 11-10 20:33 | 605123 派克新材 | 无 | 1/1 | S2 |
| 11-11 18:10 | 002273 水晶光电 | 有 | 1/1 | S1 |
| 11-11 18:10 | 605228 神通科技 | 有 | 1/1 | S1 |
| 11-11 18:10 | 300332 天壕能源 | 有 | 1/1 | S1 |
| 11-11 18:10 | 300285 国瓷材料 | 有 | 1/1 | S1 |
| 11-12 18:34 | 603296 华勤技术 | 有 | 1/1 | S1 |
| 11-12 18:34 | 002158 汉钟精机 | 有 | 1/1 | S1 |

**结论：板块缺失是结构性缺口为主（数据有、没进 prompt），板块工具约 40% 失败率为次因。** 47 条中 37 条上游已有行业数据，仅 10 条连行业映射都缺。

---

### 2.7 外部依据：「2 个以上缺失就拒绝」有没有对应做法

**直接结论：没有找到任何公开规范、行业标准或成熟量化系统明确规定「6 个研究维度中 2 个缺失就不得开仓」。这条规则目前没有可引用的外部依据，属于产品自定义阈值，需用户决策是否保留。**

可引用的**类比性依据**（只支持局部论点，不支持该规则本身）：

| 依据 | 内容 | 与本规则的匹配度 |
| --- | --- | --- |
| SEC Rule 15c3-5（Market Access Rule，2010）<br>https://www.sec.gov/files/rules/final/2010/34-63241.pdf | 要求经纪商对市场接入设置盘前风控：价格校验、最大订单金额/数量、重复订单限制、错误订单拒绝 | 支持「订单级硬拒绝」，**不涉及研究数据不全时是否拒绝决策** |
| MiFID II RTS 6 Article 15（ESMA 监督简报）<br>https://www.esma.europa.eu/sites/default/files/2026-02/ESMA74-1505669079-10311_Supervisory_Briefing_on_Algorithmic_Trading_in_the_EU.pdf | 盘前控制：price collars、max order value/volume、message limits | 同上，属交易执行层，不是研究层 |
| FCA 算法交易多公司审查<br>https://www.fca.org.uk/publication/multi-firm-reviews/algorithmic-trading-compliance-wholesale-markets.pdf | 要求算法交易有前置风控与kill switch | 同上 |
| Selective classification / reject option（Geifman & El-Yaniv, 2017）<br>https://arxiv.org/pdf/1705.08500 | 允许模型在低置信时弃权；须用 risk-coverage 曲线衡量「弃权换来的准确率」是否值得 | 支持「可弃权」，但要求**按覆盖率校准**。当前 2/131 覆盖≈1.5%，属退化区间（几乎全弃权），不是该文献主张的用法 |
| Black-Litterman 用 Ω 表达观点不确定性<br>https://arxiv.org/pdf/2505.02185<br>https://www.quantengines.com/blog/black-litterman-model | 观点不确定时收缩权重，而非直接禁止持仓 | 支持「降置信度/降仓位」这一替代路径 |
| qSOFA ≥2/3 分档<br>https://qsofa.org/#how<br>https://www.sccm.org/clinical-resources/sepsis-definitions | 计数型（count-based）风险触发，≥2 项异常提示高风险 | 证明「计数规则」在医学分诊中存在，但用途是**提示风险**，不是一票否决 |
| Dawes, 1979, improper linear models<br>https://stanford.edu/~knutson/nfc/dawes79.pdf | 补偿性线性模型通常优于临床/合取式（conjunctive）判断规则 | 反对「2 个缺失就一票否决」这类合取式规则 |
| Sterne et al., 2009, BMJ（缺失数据）<br>https://www.bmj.com/content/338/bmj.b2393.abstract | 不建议简单删除缺失样本；应做多重插补或敏感性分析 | 支持「缺失不等于无效」，但针对统计建模，非交易决策 |

**如实说明：以上文献没有一篇讨论「股票研究 6 维中 2 维缺失是否应禁止开仓」。该规则目前无外部依据支撑。**

---

### 2.8 补齐缺失能解锁多少 open

#### 2.8.1 修复动作与可解决条数

| 修复动作 | 影响面 | 性质 | 成本 |
| --- | --- | --- | --- |
| 修 `search_comprehensive_intel` 期望字段 `report` → `intel` | 131 张新闻卡全部 | 字段契约 bug | 极低 |
| 把上游 `schema.base.industry` 注入深度分析 prompt | 67 条中 38 条（S1） | 数据已存在，纯注入 | 低 |
| 深度分析接入 `detect_market_regime` | 全部（43/43 成功） | 未采集 | 低 |
| 深度分析接入 `get_sector_rankings` + 重试 | 全部（约 40% 失败） | 未采集 + 稳定性 | 中 |
| 补板块映射/行业数据源 | 67 条中 17 条（S2） | 数据缺失 | 中-高 |
| 改新闻「缺失」判定口径 | 67 条中 53 条（N1） | 规则语义，非数据 | 需用户决策 |

#### 2.8.2 规则层可解锁上限

67 条全部具备 `stop_loss` 和 `ideal_entry_zone`（67/67），因此仅从规则条件看，若把 `news_event` 与 `market_sector` 都补到 `neutral`：

| 场景 | 命中 67 的剩余 | 减少（规则层解锁上限） |
| --- | --- | --- |
| 只补 sector 证据（S1+S2） | 24 | 43 |
| 只改 news 判定 | 18 | 49 |
| 两者都处理 | 3 | 64 |

即：**规则层理论上最多 64 条不再被「≥2 维坏」约束**，剩余 3 条因 `fundamental`/`capital_flow` 仍坏而不能解除。

#### 2.8.3 但规则不是唯一条件：实证反证

全 131 条的 open 率按坏维度数分层：

| 坏维度数 | 条数 | `open` | `conditional_open` | `wait` | `monitor` | `reject` |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 24 | **0** | 0 | 19 | 2 | 3 |
| 1 | 40 | **1**（600519 贵州茅台） | 0 | 33 | 6 | 0 |
| ≥2 | 67 | **0** | 1（002268 电科网安） | 57 | 9 | 0 |

关键反证：

- **24 条「0 维缺失」的记录里，没有一条 open**（19 wait / 2 monitor / 3 reject）；
- **唯一 open 的贵州茅台（600519）反而有 1 维缺失**（`market_sector=missing`，其余 5 维 support/neutral）；
- 002268 在 2 维缺失时仍输出 `conditional_open`，说明该规则连模型自身都未严格执行。

因此必须区分两个结论：

1. **规则层**：67 条是「规则条件成立」的记录，规则确实与 0 open 高度相关；
2. **因果层**：没有任何证据表明「补齐维度 → 模型就会 open」。0 维缺失的 24 条 0 open，说明还有规则之外的保守因素（模型倾向、点位/风控判定、action_strength 等）在压制开仓。

#### 2.8.4 结论：能解锁多少

| 口径 | 数字 | 性质 |
| --- | --- | --- |
| 规则层理论上限 | 64 条（67 → 3） | 假设补齐后模型立即改判，**未实证** |
| 补数据源能直接解决的 | 38 条（S1，行业已存在） | 纯注入，可验证 |
| 需新增/修复数据源的 | 17 条（S2，行业映射缺失） | 需板块数据治理 |
| 属规则/判定语义、不能靠补数据解决的 | 53 条（N1，有数据被判 missing） | 必须改判定口径，需用户决策 |
| 按实证清洁样本外推的期望解锁 | **0-2 条** | 24 条 0 缺失样本 open 率为 0；1 维缺失样本为 1/40 |

> 结论：**「补齐数据」能消除规则命中，但不能证明能解锁 open。** 若目标是提高 open 产出，需要同时处理规则语义与模型开仓倾向，并做 A/B 重跑验证；仅补数据源的预期收益接近 0-2 条，不是 64 条。

#### 2.8.5 新闻源覆盖 vs 程序失败（性质区分）

| 性质 | 条数（66 条内） | 说明 |
| --- | --- | --- |
| 市场现实：新闻源确实没有该标的覆盖 | 2（items=0） | 新金路、工大科雅；均为小市值/冷门标的 |
| 数据稀疏但非零覆盖 | 6（items 1-2） | 天和磁材、美畅股份、江瀚新材、诺思格、兆易创新、纳思达 |
| 程序/解析问题：有数据被判 missing | 58 | 证据卡 `ok/partial`、items ≥3，属判定口径与字段契约问题 |
| 查询失败/超时 | 0 | 66 条内无失败；全部 131 条里仅 1 条 news 超时（好利科技，已判 `tool_failed` 而非 `missing`） |

**所以「新闻源没覆盖」只解释 2/66（3%），「程序没查到」解释 0/66；剩余 58 条是模型判定问题。**

板块侧同理：

| 性质 | 条数（47 条内） | 说明 |
| --- | --- | --- |
| 市场现实：板块本身无数据/无映射 | 10（S2） | 上游 industry 缺失 |
| 程序问题：有数据但未进 prompt | 37（S1） | 纯注入缺失 |
| 接口异常 | 17 条所在 trace 的 `get_sector_rankings` 失败 | 但不是主因：30 条调用成功者同样判 missing |

---

## 3. 问题二：评分门槛不可达

### 3.1 阈值定义与消费链

| 项 | 位置 |
| --- | --- |
| `CONDITIONAL_ENTRY_SCORE_MIN_DEFAULT = 88.0` | `src/agent/stock_selection.py:60` |
| `STRONG_WATCH_SCORE_MIN_DEFAULT = 85.0` | `src/agent/stock_selection.py:61` |
| `CONDITIONAL_ENTRY_MIN_STRENGTH_DEFAULT = "medium"` | `src/agent/stock_selection.py:63` |
| env 读取 `AGENT_CONDITIONAL_ENTRY_SCORE_MIN`（clamp 0-100） | `src/agent/stock_selection.py:5804` |
| env 读取 `AGENT_STRONG_WATCH_SCORE_MIN`（clamp 0-100） | `src/agent/stock_selection.py:5808` |
| `_is_conditional_entry_item`：必须是 `wait`、强度 ≥ medium、`score ≥ 88`；或 `85 ≤ score < 88` 且有共识信号 | `src/agent/stock_selection.py:6342-6357` |
| `_is_strong_watch_item`：`score ≥ 85` 或有共识信号 | `src/agent/stock_selection.py:6360-6367` |
| `_execution_mode`：item 已有显式 `execution_mode` 时**直接返回**，不进入阈值判断 | `src/agent/stock_selection.py:6370-6383` |
| `candidate_score` 取值：`final_score or signal_score or score` | `src/agent/stock_selection.py:6188`、`:7008-7009` |

**消费环节**：`_recommendation_items`（`src/agent/stock_selection.py:6177-6232`）复制每个 plan 的 `execution_mode`（`:6206`），再对 item 调用 `_execution_mode(item)`（`:6223`）。阈值只在这一步生效。

### 3.2 初筛真实分数分布（232 条）

43 个 trace、2026-05-30 ~ 2026-11-16、232 条 `full.shortlist[].score`：

| 指标 | 值 |
| --- | --- |
| min | 20 |
| p50 | 46 |
| p90 | 64 |
| p99 | 77.69 |
| max | 83 |
| mean | 47.51 |
| ≥85 | **0** |
| ≥88 | **0** |
| ≥80 | 1 |
| ≥75 | 5 |
| ≥70 | 14 |

剔除 22 条 fallback 分数（无行情固定 40 / 有行情固定 60）后的 210 条 LLM 分数：

| 指标 | 值 |
| --- | --- |
| p50 | 43.5 |
| p90 | 65 |
| p99 | 77.91 |
| max | 83 |
| ≥85 | **0** |

### 3.3 分数构成：加权求和还是模型打分

**是 LLM 整体打分，不是代码加权求和。**

- prompt 声明权重（`src/agent/stock_selection_prompts.py:181-182`）：技术结构 25、基本面质量 20、板块环境 15、消息风险 15、账户适配 15、数据质量 10，合计 100。
- 但 `score_breakdown` 各项之和与 `score` **在 232 条中有 30 条不一致**，`score_breakdown` 合计范围 20..83。
- 说明 `score` 是模型直接给的 0-100 整数，`score_breakdown` 只是解释性字段，无代码重新计算或校验。

**理论上限 100，但经验分布集中在 40-65，观测最大值 83。**

### 3.4 阈值实际读的是什么

`candidate_score` 来自候选种子（`src/agent/stock_selection.py:6188`）：

- 257 条候选中只有 **40 条**带 `signal_score`，且只出现在 2 个 trace（`20260530-195606...`、`20260530-214336...`，各 20 条唯一候选）；
- 这 40 条的 `signal_score` 分布：min 57.56 / p50 84.45 / p90 90.19 / p99 93.43 / max 93.43；≥85 有 18 条、≥88 有 12 条；
- 其余 217 条候选没有 `signal_score`。

计划层（127 条 `positions_plan`）：

| 字段 | 观测 |
| --- | --- |
| `candidate_score` | **127/127 全部为 `None`** |
| `execution_mode`（LLM 显式给出） | `conditional_open 106` / `plain_wait 16` / `reject 3` / `strong_watch 2` |

渲染层 256 个 item 的 `execution_mode`：

| 值 | 条数 |
| --- | --- |
| `plain_wait` | 127 |
| `conditional_open` | 106 |
| `strong_watch` | 20 |
| `reject` | 3 |

阈值贡献分解：

| 来源 | 条数 |
| --- | --- |
| `conditional_open` 来自 `_is_actionable_recommendation` fallback | 106（**100%**） |
| `conditional_open` 来自 88/85 阈值 | **0** |
| `strong_watch` 来自 `score ≥ 85` | 15 |
| `strong_watch` 来自共识信号 | 5 |

### 3.5 结论：三层不同性质的问题

1. **经验不可达**：若以初筛 LLM 分为门槛，232 条里 ≥85 与 ≥88 均为 0，max=83。这是结构性事实——模型打分分布整体偏低，不是个别标的运气差。
2. **字段缺失**：阈值读的 `candidate_score` 在 127 条计划中 100% 为 `None`；只有 2 个 trace 的 40 条候选带 `signal_score`。也就是说阈值在当前链路上**基本没有可用的输入**。
3. **被显式字段绕过**：106/127 条计划由 LLM 直接给出 `execution_mode`，`_execution_mode()` 对其短路返回，阈值代码路径不执行。

因此：

- 85/88 **不是**「几乎不输出 open」的直接原因（open 与否由深度分析 `action_bias` 和 pricing/portfolio 层决定）；
- 但 85/88 确实是**死阈值**：初筛分够不到、`candidate_score` 缺失、计划层又绕过它；
- 「不可达」需要拆成「经验不可达（初筛分）」与「结构性失效（字段缺失 + 短路）」两件事，不能混为一谈。

---

## 3A. 补充诊断：为什么 24 条零维度缺失的候选，0 条 open

> 标注约定：每条结论前标【数据事实】或【分析判断】。数据事实均来自本地 trace 或仓库源码；分析判断是推论，不等同于事实。

### 3A.0 一个前置事实：trace 不保存渲染后的 prompt

【数据事实】`data/agent_traces/` 下没有存储实际发给模型的 prompt 文本（全目录检索 `"prompt":` 无匹配）。trace 只保存 stage 产物、工具调用和事件摘要。因此方向一只能分析 `src/agent/stock_selection_prompts.py` 中的模板与其拼接逻辑，无法逐字回放某次请求。模板 + `_dump(payload)` 即构成实际 prompt 的固定部分。

### 3A.1 方向一：提示词是否存在系统性保守倾向

#### 3A.1.1 关键词密度（实测）

【数据事实】用 `build_deep_dive_prompt` / `build_pricing_agent_prompt` / `build_portfolio_allocation_prompt` 构造含 `fact_sheet`/`upstream_evidence`/`stock_evidence` 的最小 payload，统计模板字符数与关键词出现次数：

| Prompt | 字符数 | 禁止/限制类<br>（不得/只能/必须/禁止/最高） | 密度<br>（次/千字） | 风险/风控类<br>（风险/止损/失效/降档） | 等待/回踩类<br>（wait/等待/观望/不追/回踩） | 确认/多重条件类<br>（确认/承接/齐备/均明确支持/且） |
| --- | --- | --- | --- | --- | --- | --- |
| 深度分析 | 3860 | 16 | 4.15 | 8 | 6 | 3 |
| 点位计算 | 3439 | 18 | 5.23 | 6 | 7 | 3 |
| 组合配置 | 5384 | 42 | **7.80** | 14 | 16 | 11 |

> 口径说明：payload 分别取 `{"fact_sheet": {}}` / `{"upstream_evidence": {}}` / `{"stock_evidence": {}}`。字符数会随 payload 长度变化，关键词以模板固定文本为主；因此应看密度而非绝对字符数。

【分析判断】三个环节的「禁止/限制」密度都高于「风险提示」和「入场确认」密度，且从深度分析 → 点位 → 组合逐级升高（4.15 → 5.23 → 7.80）。组合层每千字出现约 7.8 次强制/禁止表述，是全链路最保守的一环。

#### 3A.1.2 具体位置与原文

【数据事实】深度分析模板（`src/agent/stock_selection_prompts.py`）：

| 行号 | 原文 |
| --- | --- |
| L225 | 「资金/筹码如缺失，必须降低动作强度。」 |
| L227 | 「如果无法给止损位，不得建议 open。」 |
| L228 | 「6 个核心维度中有 2 个以上为 missing / tool_failed / unknown 时，action_bias 不得为 open。」 |
| L232 | 「即使 action_bias=wait，也要尽量给出“不能直接买的原因、次日可触发条件、禁止追高、失效条件”；无法给止损或失效条件时必须保持 weak/none，不得包装成可执行买点。」 |
| L346 | unknown 打法：「必须给出明确价格止损位，给不出则 action_bias 不得为 open。」 |
| L348 | unknown 打法「主要警惕：证据不足却包装成可执行买点；多维度缺失时强行给 open。」 |

【数据事实】点位计算模板：

| 行号 | 原文 |
| --- | --- |
| L593 | 「如果缺少实时价/ATR/盘口，只能输出 conditional/monitor 计划，并把 data_status 写为 partial。」 |
| L595 | 「如果 market_regime 为 risk_off / panic / trending_down，Breakout_Continuation 最高只能是 watch/conditional，不得生成 immediate_open。」 |
| L599 | 「即使 theme_regime=mainline_markup 且为核心龙头，仍只能在……下输出 conditional_open；不得因主线核心直接 immediate_open。」 |

【数据事实】组合配置模板：

| 行号 | 原文要点 |
| --- | --- |
| L663 | 「**首仓必须保守，除非行情、技术、基本面、消息和资金证据均明确支持。**」 |
| L664 | 「休市、资金面缺失、消息面缺失或行情时效不明时，**只能**给条件型计划。」 |
| L667 | 「候选整体质量不足时输出本轮不建仓。」 |
| L668 | 「账户摘要为空或可用现金缺失时，portfolio_action 不得为 open。」 |
| L669 | 「risk_off / panic → portfolio_action 必须为 wait 或 reject。」 |
| L670 | 「高波动 → 任何 open/conditional_open 首仓必须显著降档。」 |
| L673 | 「如果所有标的都是 plain_wait/reject，summary.core_reason **必须明确写“本轮没有可直接入手标的”**。」 |

#### 3A.1.3 默认动作、示例与评级定义

【数据事实】

- **没有 few-shot 示例**：全文件检索 `example` / `few-shot` 无匹配；「例如」仅出现在 L230（`dimension_summary.summary` 写法）和 L715（`signal_validity_label` 示例），均不涉及动作选择。
- **枚举顺序**：`action_bias = open | wait | reject | monitor`，open 排在首位；schema 中没有声明默认值。
- **代码默认值**：`_recommendation_items` 在字段缺失时用 `action="wait"`、`action_strength="weak"`（`src/agent/stock_selection.py:6146-6147`）；候选缺省 action 为 `"watch"`（`:6185`）；`_safe_float(None)` 返回 `0.0`（`:6248-6252`）。
- **输出模板**：L673 把「本轮没有可直接入手标的」写成标准文案，导致「不开仓」有现成的合格叙述。

#### 3A.1.4 判定

【分析判断】**存在结构性保守，属「没把握就 wait」的默认动作设定，不只是通用风险提示。** 依据：

1. L663 是 **all-of（合取）规则**：只有五项证据「均明确支持」才允许不保守，任一维不满足就退化到保守首仓；
2. L664 规定任一维缺失就「只能」条件型，把缺失直接映射为动作降级；
3. L667 / L673 把「本轮不建仓」写成合格默认输出，而非异常结果；
4. 深度分析模板只有一串 **必要条件**（不得 open 的情形），没有一条正向的 **充分条件**（满足 X/Y/Z 即可 open）；
5. 代码层 action/strength 缺省值是 wait/weak，缺失字段会自动落入保守档。

【分析判断】这不是说「保守」本身错误，而是说：在「必须全部满足」的合取规则下，6 个维度任意一个出问题都会把 open 概率压到接近 0；而本轮 trace 恰好有 news/sector 两个高频缺口，因此规则与数据缺陷叠加，形成 0 open。

#### 3A.1.5 外部依据

| 依据 | 内容 | 与本诊断的关系 |
| --- | --- | --- |
| Anthropic prompt engineering 指南<br>https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/claude-4-best-practices | 建议指令清晰直接、给出正向示例与明确输出格式 | 支持「只有禁止项、没有正向充分条件」会让模型偏向最安全输出 |
| OpenAI prompt engineering 指南<br>https://platform.openai.com/docs/guides/prompt-engineering | 建议明确目标、给出成功样例、区分指令与资料 | 同上；本仓库 prompt 无 few-shot 成功样例 |
| Lost in the Middle（Liu et al., 2023）<br>https://arxiv.org/abs/2307.03172 | 长上下文中间位置的信息更易被忽略；指令位置影响遵循度 | 支持「在长 prompt 多处重复禁止项」会放大其权重 |
| Dawes, 1979<br>https://stanford.edu/~knutson/nfc/dawes79.pdf | 合取式/临床式规则通常劣于补偿性线性规则 | 直接支持 L663 的 all-of 规则是结构性保守来源 |
| Selective classification（Geifman & El-Yaniv, 2017）<br>https://arxiv.org/pdf/1705.08500 | 弃权应通过 risk-coverage 曲线校准，而不是固定阈值 | 当前 open 覆盖率约 1.5%，处于退化区间 |

**如实说明：没有公开资料规定「股票研究 prompt 必须包含哪些正向开仓条件」。上述是对设计原则的类比引用，具体判定仍是分析判断。**

### 3A.2 方向二：action_strength 与评分分布

#### 3A.2.1 深度分析层没有数值评分

【数据事实】`single_stock_deep_dive.json` 的 `summary` 字段为：`code/name/action_bias/action_strength/quote_basis/ideal_entry_zone/no_chase_line/stop_loss/main_supporting_evidence/main_risks/main_missing_evidence/target_1/target_2/secondary_entry_zone/auction_trigger/breakout_trigger/pullback_trigger/failure_condition`；`full` 字段中没有 `score` / `confidence`。**深度分析层不存在数值评分，85/88 阈值不作用于这一层。**

【数据事实】把 131 条深度结果按 (trace, code) 关联上游初筛 `score`，可匹配 127 条，4 条无初筛分。

#### 3A.2.2 可关联评分的分布（127 条）

| 指标 | 数值 |
| --- | --- |
| n | 127 |
| min | 34 |
| mean | 57.55 |
| median / p50 | 60 |
| p90 | 70 |
| p99 | 78.74 |
| max | 83 |
| 标准差 | 9.50 |
| ≥85 | **0** |
| ≥88 | **0** |

直方图：

| 分数段 | 条数 |
| --- | --- |
| 34-39 | 3 |
| 40-49 | 25 |
| 50-59 | 33 |
| 60-69 | 52 |
| 70-79 | 13 |
| 80-84 | 1 |
| ≥85 | 0 |

#### 3A.2.3 action_strength 分布

【数据事实】131 条：

| strength | 条数 | 占比 |
| --- | --- | --- |
| weak | 111 | 84.7% |
| medium | 13 | 9.9% |
| none | 5 | 3.8% |
| strong | 2 | 1.5% |

#### 3A.2.4 交叉矩阵

strength × action：

| strength | wait | monitor | reject | open | conditional_open |
| --- | --- | --- | --- | --- | --- |
| strong（2） | 1 | 0 | 1 | 0 | 0 |
| medium（13） | 10 | 3 | 0 | 0 | 0 |
| weak（111） | 95 | 14 | 0 | 1 | 1 |
| none（5） | 3 | 0 | 2 | 0 | 0 |

score 段 × action（127 条有分样本）：

| 分数段 | 条数 | wait | monitor | reject | open | conditional_open |
| --- | --- | --- | --- | --- | --- | --- |
| 34-39 | 3 | 2 | 1 | 0 | 0 | 0 |
| 40-49 | 25 | 20 | 2 | 1 | 1 | 1 |
| 50-59 | 33 | 26 | 6 | 1 | 0 | 0 |
| 60-69 | 52 | 43 | 8 | 1 | 0 | 0 |
| 70-79 | 13 | **13** | 0 | 0 | 0 | 0 |
| 80-84 | 1 | **1** | 0 | 0 | 0 | 0 |

【数据事实】85 分以上 / 88 分以上的条数：

| 分数口径 | n | ≥85 | ≥88 | 这些记录的动作 |
| --- | --- | --- | --- | --- |
| 初筛 `score`（232 条） | 232 | 0 | 0 | 无 |
| 深度结果可关联初筛分（127 条） | 127 | 0 | 0 | 无 |
| 候选种子 `signal_score`（257 条中 40 条有值） | 40 | 18 | 12 | 16 条未进入深度分析；2 条进入（000506、000567），均为 `wait` / `weak`，计划均为 `plain_wait` |

【分析判断】

1. **深度层不存在「分数→动作」映射**，因为没有数值分数；用初筛分替代也不成立：70-79 段 13/13 全是 wait，80-84 段唯一 1 条也是 wait，分数越高并没有更多 open。
2. **action_strength 与 action 语义不一致**：仅有的 2 条 `strong` 分别对应 `reject` 和 `wait`，没有一条 open；唯一 open 的是 `weak`。因此 `action_strength` 不能当置信度分数解释，也不适合作为仓位/门槛依据。
3. 初筛分整体集中在 50-69（85/127），天然达不到 85/88；这一事实与上一轮结论一致。

### 3A.3 方向三：分数门槛与动作的映射

#### 3A.3.1 完整消费链路（含短路顺序）

【数据事实】

1. 阈值默认值：`CONDITIONAL_ENTRY_SCORE_MIN_DEFAULT=88.0`、`STRONG_WATCH_SCORE_MIN_DEFAULT=85.0`（`src/agent/stock_selection.py:60-61`）；env 覆盖在 `:5804` / `:5808`。
2. `_recommendation_items` 组装 item：`candidate_score = final_score or signal_score or score`（`:6188`）；把 plan 的 `execution_mode` 复制进 item（`:6206`）；最后对每个 item 调用 `_execution_mode(item)`（`:6223`）。
3. `_execution_mode`（`:6370-6383`）的判断顺序：
   - ① item 已有显式 `execution_mode` → **直接返回**（`:6371-6373`）；
   - ② action 为 reject/avoid → `reject`（`:6375-6376`）；
   - ③ `_is_actionable_recommendation` → `immediate_open` / `conditional_open`（`:6377-6378`），**在任何分数判断之前**；
   - ④ `_is_conditional_entry_item`（`:6379`）；
   - ⑤ `_is_strong_watch_item`（`:6381`）；
   - ⑥ 否则 `plain_wait`。
4. `_is_conditional_entry_item`（`:6342-6357`）内部顺序：action 必须 `wait` → strength ≥ medium → **score ≥ 88（或 85≤score<88 且有共识信号）** → 有入场条件 → 有退出条件 → 无硬空风险。
5. `_is_strong_watch_item`（`:6360-6367`）：非 reject → 无硬空风险 → `score ≥ 85` 或有共识信号。
6. `_safe_float(None)` 返回 `0.0`（`:6248-6252`）。

【数据事实】硬空风险标记包括：`强势空头/趋势空头/跌破关键支撑/破位/资金流出/净流出/监管处罚/业绩预警/重大减持/债务/流动性风险/重大诉讼/数据过期/行情严重过期/不可成交/名称代码不一致/risk_off/panic` 等（`HARD_BEARISH_RISK_MARKERS`，`:64-94`）。

#### 3A.3.2 短路与降级统计（127 条计划）

【数据事实】

| 项 | 条数 |
| --- | --- |
| 计划总数 | 127 |
| 带显式 `execution_mode`（走 ① 短路） | **127 / 127** |
| `candidate_score is None`（若走到阈值会归零） | **127 / 127** |
| 显式 mode：conditional_open / plain_wait / reject / strong_watch | 106 / 16 / 3 / 2 |
| 计划层 action 分布：wait / monitor / reject | 108 / 15 / 4 |

【数据事实】上述 127 条全部在 ①（显式 `execution_mode`）处短路，永远不会进入 ③④⑤ 的分数判断。为了量化「如果没有显式 mode 会怎样」，对 108 条 `action=wait` 的计划按源码 `_is_actionable_recommendation`（`src/agent/stock_selection.py:6268-6287`）逐字段复现一次反事实判定：

| 反事实判定项（仅对 108 条 wait 计划） | 条数 |
| --- | --- |
| 满足 `_is_actionable_recommendation`（入场 + 止损 + strength≥medium + 窄口径硬空） | **10** |
| 不满足 | 98 |
| 其中：无入场条件（`entry_condition` / `ideal_entry_zone` 均空） | 0 |
| 其中：无止损条件（`stop_loss_condition` / `stop_loss` 均空） | 2 |
| 其中：`action_strength` 为 none/weak 或空 | 92 |
| 其中：命中 `_is_actionable_recommendation` 的**窄口径**硬空标记（只看前 4 条 `risk_flags`，仅 `强势空头/资金流出/净流出/数据过期/严重过期/基本面缺失`） | 24 |

> 口径说明：以上分项可重叠，不能相加。若改用 ④⑤ 使用的**宽口径** `_has_hard_bearish_risk`（`HARD_BEARISH_RISK_MARKERS` 全表，字段为 `risk_flags/failure_conditions/missing_evidence/reason` 等），则 127 条计划中命中 63 条，108 条 wait 中命中 54 条；主要命中词为 `risk_off`（30）、`净流出`（21）、`趋势空头`（5）。两个函数用的是不同标记集，不能混用。

【数据事实】**「分数达标却被其他条件降级」的可测量条数为 0**：127 条计划没有任何一条带 `candidate_score`，因此 85/88 判断一次也没有执行。以初筛分作代理，80-84 段只有 1 条且本身是 wait（未达 85），也不存在「达标后被降级」的样本。

【数据事实】`signal_score` ≥85 的 18 条中：

| 去向 | 条数 |
| --- | --- |
| 未进入深度分析（被初筛过滤/未入选） | 15 |
| 进入深度分析 → wait / weak → plain_wait | 3（2 个唯一代码：000506 在 2 个 trace 中出现、000567 出现 1 次） |
| 进入深度分析 → open | 0 |

#### 3A.3.3 设计与实现的矛盾

【数据事实】组合配置 prompt L671 明确写：「candidate_discovery 里的 signal_score/final_score 只是“入池召回分”，不得当成买入推荐分，也不得据此决定首选排序。」

【数据事实】但代码 `_recommendation_items` 在同一条链路上把 `final_score or signal_score or score` 赋给 `candidate_score`（`:6188`），并把它作为 `_is_conditional_entry_item` / `_is_strong_watch_item` 的分数门槛输入（`:6348`、`:6366`）。

【分析判断】**同一个分数，prompt 层禁止用于买入判断，代码层却用它做开仓门槛。** 这是设计层面的语义冲突，不是数据问题。它同时解释了：

- 为什么「85 以上依然是 wait」：模型按 L671 不把 signal_score 当买入依据；
- 为什么阈值从不触发：模型给出的显式 `execution_mode` 先短路，且 `candidate_score` 普遍缺失。

【分析判断】阈值不是被某一堆「降级条件」打败的，而是**在执行顺序上从未获得执行机会**。

### 3A.4 方向四：模型开仓倾向的实验设计（只给方案，本轮不执行）

#### 3A.4.1 要回答的问题

- RQ1：把 prompt 的合取式保守措辞改成「明确的正向开仓条件」后，open 率是否上升？
- RQ2：只补齐数据（板块注入 + 新闻字段契约修复）、不改 prompt，open 率是否上升？
- RQ3：prompt 与数据是否存在交互（数据补齐只在 prompt 放宽后才生效）？

#### 3A.4.2 设计：2×2 因子 + 1 个附加臂

| 臂 | Prompt | 数据 | 用途 |
| --- | --- | --- | --- |
| A | 现状 | 现状 | 基线 |
| B | 现状 | 补齐（industry/regime/sector 注入 + 新闻字段修复） | 隔离数据效应 |
| C | 中性化（保留风控，删除合取式 all-of 与「本轮不建仓」默认文案，补正向充分条件） | 现状 | 隔离 prompt 效应 |
| D | 中性化 | 补齐 | 交互效应 |
| E（附加） | 现状 + 显式决策策略（给定基率目标，如「证据合格时应有一定比例 open」） | 补齐 | 检验「指令式纠偏」是否有效 |

控制变量：

- 同一模型与版本、同一 provider、同一 temperature（可设 0 则设 0；否则固定并重复 5 次）；
- 同一批标的：取本轮 108 个唯一代码，按代码配对，四个臂跑同一组标的；
- 同一行情快照：用 trace 中的 `as_of` 冻结行情与工具返回，避免跨日漂移；
- 同一工具集与超时、同一 max tokens、关闭缓存复用，防止跨臂污染；
- 同一账户上下文与策略阈值；
- 运行顺序随机化，避免时间漂移。

#### 3A.4.3 指标与判读

主指标：`open` 率（`open` / 总样本）。
次指标：`conditional_open` 率、`wait` 率、`reject` 率、`action_strength` 分布、维度 verdict 分布、`stop_loss` 覆盖率。

护栏指标（防止「为 open 而 open」）：

- 无止损仍 open 的比例必须为 0；
- 事实性错误/编造数据的人工抽检不合格率不得高于基线；
- 硬空风险标记命中却 open 的比例必须为 0；
- 风险收益比与失效条件完整率不得下降。

判读规则：

- 主要比较 A vs C（prompt 主效应）、A vs B（数据主效应）、交互项 D−C−B+A；
- 配对二元结果用 McNemar 检验；多代码多重复用混合效应 logistic（代码随机截距）；
- 同时报告 risk-coverage 曲线，而不只看单点 open 率；
- 每个臂随机抽 50 条做盲评（隐藏臂标签），核对是否遵守规则、证据是否可追溯。

#### 3A.4.4 样本量与统计功效

【分析判断】按两比例检验（双侧 α=0.05、power=0.8，合并方差近似）估算：

| 基线 open 率 | 目标 open 率 | 每组最少样本 |
| --- | --- | --- |
| 1.5%（本轮 2/131） | 15% | ≈ 65 |
| 2% | 10% | ≈ 138 |
| 5% | 15% | ≈ 141 |

本轮可用唯一标的只有 108 个，单次运行不足 138；建议 **108 个代码 × 每臂 5 次重复**（约 540 条/臂），以代码为聚类单位，可满足检测「1.5% → 10-15%」的功效。若只跑单次，则只能检测更大的效应，结论应标注为探索性。

#### 3A.4.5 预注册与停止规则

- 运行前固定假设、指标、样本量、判读阈值，写入独立实验记录；
- 不中途看结果改 prompt；
- 若某臂工具失败率显著高于基线（如 >10%），该臂作废重跑；
- 结论按「支持 / 不支持 / 不确定」三态报告，不把不显著解释为「无差异」。

**本轮不执行该实验，也不改任何 prompt 或数据接入。**

### 3A.5 小结与新增的用户决策项

【数据事实】24 条零缺失候选 0 open 的直接相关事实：

1. 深度分析层没有数值评分，85/88 阈值不作用于该层；
2. 84.7% 的深度结果是 `weak`，唯一 open 也是 `weak`；
3. 127 条计划全部带显式 `execution_mode`，全部 `candidate_score=None`，阈值 0 次执行；
4. prompt 中「禁止/限制」密度 4.15-7.80 次/千字，组合层最高；
5. L663 是 all-of 规则，L673 把「本轮没有可直接入手标的」写成标准输出。

【分析判断】0 open 是「合取式保守 prompt + 高频维度缺口 + 阈值被短路」三者叠加的结果；其中 prompt 保守是可独立验证的主因候选。

新增需用户决策项：

1. 是否在深度分析/组合 prompt 中补充**正向充分条件**（当前只有禁止项）？
2. 是否删除或改写 L673 的「本轮没有可直接入手标的」默认文案？
3. 是否修复 L671 与 `:6188` 的 `signal_score` 语义冲突（prompt 禁止使用、代码却用作门槛）？
4. 是否执行 3A.4 的 2×2 实验，以及是否接受「108 代码 × 5 次重复」的成本？
5. 若实验显示 prompt 为主因，是否接受把 all-of 规则改为分档/降仓位（与 2.7 的 A4 方案合并决策）？

---

## 3B. 核查：prompt L671 与 `stock_selection.py:6188` 的语义冲突

### 3B.1 两处原文

【数据事实】`src/agent/stock_selection_prompts.py:671`（`build_portfolio_allocation_prompt` 配置规则第 11 条）：

> `candidate_discovery` 里的 `signal_score`/`final_score` 只是“入池召回分”，不得当成买入推荐分，也不得据此决定首选排序。

【数据事实】`src/agent/stock_selection.py:6188`（`_recommendation_items` 构建渲染条目）：

```python
target["candidate_score"] = (
    display_item.get("final_score")
    or display_item.get("signal_score")
    or display_item.get("score")
)
```

【数据事实】该 `candidate_score` 当时有三个消费点：

| 消费点 | 用途 | 位置 |
| --- | --- | --- |
| `_is_conditional_entry_item` | `score ≥ conditional_entry(88)`（或 85-88 且有共识）→ 产出 `conditional_open`（条件入场，可执行档） | `src/agent/stock_selection.py:6348-6363` |
| `_is_strong_watch_item` | `score ≥ strong_watch(85)` 或共识 → 产出 `strong_watch`（强观察） | `src/agent/stock_selection.py:6366-6373` |
| 排序 tiebreaker | `-_safe_float(candidate_score)` 参与排序 | `src/agent/stock_selection.py:6231-6236` |

【数据事实】`candidate_discovery.full.candidates` 中，`score` 与 `signal_score` 同值（40 条有值样本全部相等），`final_score` 全部缺失；也就是说 6188 取到的就是入池召回分，不是初筛质量分。

### 3B.2 判定：是 bug

【分析判断】两处约束的是**同一件事**（`candidate_discovery` 召回分能否进入推荐判定），但代码没有落实 prompt 的规则：

1. prompt 明确「不得当成买入推荐分」；
2. 代码把它当 `candidate_score`，并在 `_is_conditional_entry_item` 里用 `≥88` 直接产出 `conditional_open` —— 这是可执行/买入档，属于“买入推荐分”；
3. `_is_strong_watch_item` 还会据此把标的升到 `strong_watch`，`_recommendation_sort_rank` 里 `strong_watch`（12）排在 `plain_wait`（99）之前，等于召回分间接影响了排序；
4. 排序 tiebreaker 也直接读该分。

【分析判断】这不是“措辞容易混淆的不同层面”，而是 prompt 立了规则、代码另一条路径没执行，符合“一处定的规则另一处没落实 = bug”的判定。补充事实：该路径在当前 trace 中是**潜在地**触发——127/127 条计划都带显式 `execution_mode`，在 `_execution_mode` 的 ① 处就短路，所以线上观测不到；但只要模型漏给 `execution_mode`，召回分就会经 ④ 升成条件入场。

### 3B.3 修复（最小改动，不碰点位生成）

【数据事实】改动集中在选股报告渲染层，**不涉及任何入场区/止损/ATR 计算或模型点位输出**：

| 文件 | 改动 |
| --- | --- |
| `src/agent/stock_selection.py` | 渲染条目新增两个字段：`recall_score`（= `final_score or signal_score or score`，仅展示/强观察）与 `entry_quality_score`（= `candidate_screening.shortlist[].score`，唯一允许驱动条件入场的分数） |
| 同上 | `_is_conditional_entry_item` 的分数来源由 `candidate_score` 改为 `entry_quality_score` |
| 同上 | `_is_strong_watch_item` 的分数来源由 `candidate_score` 改为 `recall_score`（强观察是观察档，不是买入档） |
| 同上 | 排序 tiebreaker 改为 `entry_quality_score` → `recall_score` → `code`，召回分不再优先于质量分 |
| 同上 | `render_stock_selection_markdown` 从 `candidate_screening.full.shortlist` 构建 `screening_scores` 并传入 |
| 同上 | 展示位（“候选分”）改读 `recall_score`，保持报告外观不变 |
| `tests/test_agent_stock_selection.py` | 新增 `test_recall_score_cannot_promote_to_conditional_entry_but_screening_score_can` |
| `docs/CHANGELOG.md` | 新增 `[Unreleased]` 扁平条目 |

【分析判断】为什么这是最符合原意的修法：

- prompt 只禁止 `signal_score`/`final_score`，并未禁止初筛质量分；因此把**条件入场门槛换回真正的质量分**，既落实了 prompt，又保留了 85/88 阈值（没有改数值）。
- `strong_watch` 是“观察”而非“买入”，报告里也标注“不形成交易脚本”，因此召回分继续驱动强观察不违反 L671。
- 不改 `_is_actionable_recommendation`（③，原本就不用分数），所以“有完整计划且有入场/止损”的条件入场路径完全不受影响。
- 不触碰 `pricing_agent`、`trade_plan_guard`、任何 entry/stop 计算。

【数据事实】连带影响核查：

- `_recommendation_items` 只有 `render_stock_selection_markdown` 一个调用方；
- 全仓库检索 `candidate_score`，除 `stock_selection.py` 外只有 `candidate_experts/orchestrator.py`（不同概念的事件/板块打分）与 docs/vendor，无 API/Web/Desktop 消费方；
- `stock_selection.py:2943`（附录候选卡）与 `:3594`（meta packet）是另外两种结构，未改动。

【数据事实】验证：`tests/test_agent_stock_selection.py` 59 项通过；唯一失败项 `test_stock_selection_balanced_candidate_evidence_collects_candidates_in_parallel` 是计时断言（`elapsed < 0.28`），本机实测 1.09s，与本改动无关。`py_compile` 通过。

【分析判断】未验证项：未在真实 LLM 链路重跑，无法证明模型实际漏给 `execution_mode` 的概率；本次只保证“即使漏给，召回分也不再升级为条件入场”。

---

## 3C. 对照实验：规则压的还是模型自己保守

### 3C.0 问题与约束

要回答的只有一个：深度分析不输出 `open`，是**≥2 维缺失硬规则**压的，还是**模型自身倾向保守**。

【数据事实】离线可行性：trace 只保存了模型已经产出的 `action_bias`，不保存可重放的 prompt 输入和模型原始 logits。因此**无法离线改变规则再观察模型输出**；能做的是用现有数据做「规则是否生效」的准对照。真正的因果对照需要重调 deep-dive 阶段 LLM，属于重跑，按你的要求先停下等确认（见 3C.4）。

### 3C.1 已执行的离线准对照（数据事实）

把 131 条深度结果按「规则是否可能约束」分组：

| 组 | 定义 | n | open | conditional_open | wait | monitor | reject |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 规则命中组 | ≥2 维 missing/tool_failed/unknown | 67 | 0 | 1 | 57 | 9 | 0 |
| 规则免疫组 | ≤1 维 | 64 | 1 | 0 | 52 | 8 | 3 |
| 其中零缺失组 | =0 维 | 24 | 0 | 0 | 19 | 2 | 3 |

强度分布（规则本不该影响 `action_strength`）：

| 组 | weak | medium | strong | none |
| --- | --- | --- | --- | --- |
| 规则命中组 67 | 58 | 7 | 1 | 1 |
| 规则免疫组 64 | 53 | 6 | 1 | 4 |
| 零缺失组 24 | 19 | 2 | 1 | 2 |

零缺失组里 `strength ≥ medium` 仅 3 条（2 wait / 1 reject），且 24 条中 23 条 `main_risks` 非空，理由是可复核的市场/技术/资金风险（超买、`risk_off`、趋势向下、主力净流出、乖离过高），不是“维度缺失”；其中 7 条 `main_missing_evidence` 为空。

### 3C.2 预注册判读规则（在看到结果前写定）

1. 若规则是主因：规则免疫组的 open 率应显著高于规则命中组（≥10pp）。
2. 若模型保守是主因：两组 open 率都应接近 0，且 `weak` 占比都高。
3. 中间态：差异 5-10pp，判“两者都有，方向不明”。

### 3C.3 本轮结论（方向性观察，不足以做显著性结论）

【分析判断】按 3C.2 的规则 2 判读：

- 规则免疫组 open 率 1/64 = 1.6%，规则命中组 0/67 = 0%，差值仅 1.6pp，远低于 10pp 门槛；
- 两组 `weak` 占比分别为 82.8% 和 86.6%，几乎一致；
- 零缺失组 0 open，且 79% 为 `weak`，非 open 理由主要是具体风险而非缺失。

→ **方向性判断：≥2 维缺失规则不是 0 open 的主因；模型自身在证据不足时倾向给弱强度和 wait 是更主要的解释。** 这只是一次离线观察（总共只有 1 条 open），存在选择偏差，不能当作因果或显著结论。

### 3C.4 真因果对照的最小设计（本轮不执行，需你确认）

只改一个变量：deep-dive prompt 的第 8 条（≥2 维缺失不得 open）。其余模型、温度、候选输入、其他 prompt 完全一致。

| 臂 | prompt | 样本 | 目的 |
| --- | --- | --- | --- |
| A（现状） | 保留第 8 条 | 67 条规则命中 + 24 条零缺失 | 基线 |
| B（去掉规则） | 删除第 8 条，其余不动 | 同上、同代码 | 观察规则压力 |

配对方式：同一代码在 A/B 各跑一次，只重跑 `single_stock_deep_dive` 阶段，不重跑发现/初筛/点位/组合。

判读（事前写定）：

- 主指标 Δ = B 组 open 率 − A 组 open 率（仅规则命中组）。
- 若 Δ ≥ 10pp 且零缺失组 Δ ≤ 2pp → 规则压力为主因；
- 若 Δ < 5pp → 模型保守为主因；
- 其余 → 方向不明。
- 次指标：`action_strength` 均值变化。

样本量：方向性观察 67+24 条即可；若要显著性，按 3A.4.4 需每组约 65-140 条，单次不够，需重复。

【分析判断】代价与风险：需要 LLM API、会把 deep-dive 阶段重跑 2 次（A/B），不属于“离线复现”。按你的要求，我**没有执行**，等你确认是否要跑、用什么模型/预算。

---

## 4. 外部依据：阈值型决策门槛通常怎么定

| 做法 | 代表依据 | 要点 |
| --- | --- | --- |
| 分位数法 | Fama-French 分位数组合构建<br>http://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/f-f_developed.html | 按横截面分位（如 top 30%）划分组合，阈值随样本分布自适应，不设绝对分数 |
| 历史分布标定 | 同上 + 一般回测实践 | 用历史得分分布确定分位点，保证每期都有可交易样本 |
| 代价敏感阈值调优 | scikit-learn 阈值调优示例<br>https://scikit-learn.org/stable/auto_examples/model_selection/plot_cost_sensitive_learning.html<br>precision-recall 曲线<br>https://scikit-learn.org/stable/modules/generated/sklearn.metrics.precision_recall_curve.html | 阈值由「漏判/误判代价」决定，而不是拍一个绝对分 |
| 模型风险治理 | SR 26-2（2026-04-17，取代 SR 11-7）<br>https://www.federalreserve.gov/supervisionreg/srletters/sr2602.htm | 要求对模型输出设定、监控、回溯验证；阈值属模型参数，应有文档化依据与定期复核 |
| 绝对分数打分的可靠性 | LLM-as-a-Judge（MT-Bench）<br>https://arxiv.org/pdf/2306.05685 | LLM 绝对打分存在位置/长度/自我偏好偏差；成对比较比绝对打分更稳定 |
| 仓位替代阈值否决 | Fractional Kelly<br>https://expectedvalue.co.uk/blog/fractional-kelly/ | 不确定时缩小仓位，而不是二元拒绝 |

**如实说明：没有公开资料规定「初筛分必须 ≥88 才能条件开仓」。绝对阈值本身不是错误做法，但前提是该分数分布经过标定、且阈值落在分布内有样本可达的区间。当前 232 条样本 max=83，说明阈值从未被标定过。**

---

## 5. 可选方案与副作用对比

> 以下只列选项与权衡，不代替用户决策。每条都标注依据强度。

### 5.1 问题一相关

| 方案 | 依据 | 预期效果 | 副作用 | 依据强度 |
| --- | --- | --- | --- | --- |
| A1. 修 `search_comprehensive_intel` 期望字段 `report` → `intel` | 字段契约事实（`adapter.py:264` vs `dimension_evidence.py:26`） | 消除 131 张卡的错误 `missing_fields` | 只影响 prompt 信号；不保证模型改判 | 强（机械缺陷，可直接验证） |
| A2. 把 `schema.base.industry` 注入深度分析 prompt | 47 条缺板块维度的样本中 37 条上游已有 industry | 板块维有据可依 | 需确认字段口径与板块维定义一致 | 强（数据存在性已证实） |
| A3. 深度分析接入 `detect_market_regime` / `get_sector_rankings` | 前者 43/43 成功 | 板块/市场维有实时证据 | 后者约 40% 失败，需重试/降级 | 中（成功率已量化，效果未验证） |
| A4. 把「≥2 维坏 → 不得 open」改为「降置信度 + 降仓位」 | Black-Litterman Ω、fractional Kelly | 保留风险约束，同时允许小仓位表达 | 需新增置信度字段与仓位映射；改动面大 | 中（有类比依据，无直接规范） |
| A5. 保留规则，但区分「无数据 / 查询失败 / 模型不确定」三态 | Sterne 2009（不应简单删除缺失）、Dawes 1979（反对合取式规则） | 减少「有数据却判 missing」的误伤 | 需改 prompt 枚举与 fallback 语义 | 中 |
| A6. 删除或放宽 2 维规则 | Dawes 1979 | 直接提高 open 产出 | 失去一层风险护栏；无直接外部依据支持删除 | 弱（需用户决策） |

### 5.2 问题二相关

| 方案 | 依据 | 预期效果 | 副作用 | 依据强度 |
| --- | --- | --- | --- | --- |
| B1. 按历史分布重标定阈值（如 p95/p99） | Fama-French 分位、scikit-learn 代价敏感 | 阈值落在可达区间，保持筛选性 | 分布漂移需定期重标；不同市场需分开标定 | 强 |
| B2. 改为分位驱动（每期取 top N%） | Fama-French | 自动适应分布 | 牛市/熊市绝对质量不同，分位不保证质量 | 强 |
| B3. 修复 `candidate_score` 赋值（127/127 为 None） | 字段缺失事实 | 阈值至少有输入 | 需定义 score 语义，可能与初筛分冲突 | 强（缺陷已证实，修复方式需决策） |
| B4. 删除死阈值层，统一由 `action_bias` + `execution_mode` 决定 | 106/127 已被显式字段短路 | 减少无效分支与误解 | 失去一层次级校验；需确认无其他调用方 | 中 |
| B5. 把评分门槛前移到初筛（用初筛分决定是否 deep_dive） | 初筛 232 条分布可标定 | 门槛有真实样本支撑 | 初筛分本身是 LLM 绝对分，偏差未校准 | 中 |
| B6. 把绝对打分改为成对/排序比较 | MT-Bench | 减少绝对分偏差，分布更可用 | 需改 prompt 与聚合逻辑，成本高 | 中 |
| B7. 维持现状（不改阈值） | — | 无风险 | 死阈值继续存在，误导后续维护 | — |

---

## 6. 必须用户决策的清单

以下事项**没有足够依据由我单方面拍板**，必须由用户决定：

1. **「≥2 维缺失 → 不得 open」是否保留？**
   - 现状：仅 prompt 约束，无代码强制；无公开依据支持此具体规则。
   - 选项：保留 / 改为降置信度+降仓位 / 改为分档 / 删除。
2. **板块数据是否接入深度分析？**
   - 47 条中 37 条上游已有行业数据（纯注入），10 条行业映射也缺失；接入后能否改变模型 verdict 未经实证。
3. **消息面判定口径怎么改？**
   - 已知 58/66 是「有数据判 missing」、2/66 是真正无覆盖；是修字段契约即可，还是需要同时改 prompt 判定标准，需用户确认。
4. **85/88 阈值是否删除、改分位、还是修 `candidate_score` 后保留？**
   - 三者副作用不同，且当前该阈值 0 次实际生效。
5. **是否允许「降仓位替代拒绝」作为产品原则？**
   - 涉及风控口径，不能由工程侧决定。
6. **是否接受「阈值需定期重标定」带来的运维成本？**
   - 分位/历史分布标定都要求周期性复核（SR 26-2 的模型治理要求）。
7. **是否接受「补齐维度不必然提升 open」这一事实，并另立项目排查模型开仓倾向？**
   - 24 条 0 维缺失样本 open 率为 0，说明还有规则之外的压制因素；若目标是提高 open 产出，仅修本文档列出的数据/规则缺口不够。

---

## 7. 未验证项

| 项 | 原因 |
| --- | --- |
| 「补数据后模型一定改判 neutral」的上界 43/49/64 | 基于反事实替换，非实际重跑 |
| 补齐后实际能解锁的 open 条数 | 需 A/B 重跑；本轮只能用 0 维/1 维样本的实证开仓率（0/24、1/40）外推 |
| 24 条 0 维缺失却未 open 的逐条原因 | 已确认不是维度规则，但未逐条拆 action_strength、点位、账户约束等次级因素 |
| 初筛分与 `signal_score` 的语义关系 | 两者来自不同阶段，字段定义未在代码中显式说明 |
| 模型判 missing 的内部原因 | 需 prompt/模型侧实验，静态 trace 无法区分 |

---

## 附录：统计口径备注

- 所有百分比、分位数均为样本内实算，非外推。
- 分位数采用线性插值（与 `numpy.percentile` 默认一致）。
- 「坏维度」定义严格按 prompt 文本：`missing` / `tool_failed` / `unknown`；非 schema 枚举值不计入，因此可能低估。
- 14 条 fallback 深度结果已单独标注；其 `market_sector` 一律为 `neutral`，不参与 missing 统计。
- 127 条 `positions_plan` 与 131 条深度结果不是一一对应（部分 trace 无计划），比较时以各自口径为准。
