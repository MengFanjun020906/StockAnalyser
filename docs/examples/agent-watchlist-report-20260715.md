# Agent Watchlist Report Snapshot

> Sample generated from a local Agent Trace on 2026-07-15. This document is a compact report snapshot for product documentation and workflow review. It is not investment advice.

## 核心结论

| 项目 | 结论 |
| --- | --- |
| Trace | `20260715-161824-trace-93c77fcb2ea64fec8c124304bf4c8719` |
| 请求类型 | `watchlist_scan` |
| 最终动作 | 等待 |
| 裁决 | 有条件采纳 |
| 机会首选 | 300791 仙乐健康 |
| 执行首选 | 300791 仙乐健康（条件触发） |
| 可观察标的 | 601138 工业富联、000977 浪潮信息、002603 以岭药业 |
| 最大约束 | 市场状态偏 risk_off，且部分资金流/筹码证据缺失 |

本轮没有无条件买入标的。系统给出的主动作是等待条件触发：候选股票需要回踩确认、资金改善或板块承接转强后才进入执行区间。候选池只解决召回，不直接等于推荐。

## 推荐排序

| 排序 | 标的 | 动作 | 主要条件 | 主要风险 |
| --- | --- | --- | --- | --- |
| 1 | 300791 仙乐健康 | 条件入场 | 等待回踩或平台突破确认，成交量不放大追高 | 资金流证据缺失，市场 risk_off 可能压制个股表现 |
| 2 | 601138 工业富联 | 条件入场 | AI 服务器/PCB 主题继续、价格回踩企稳、资金承接恢复 | 主题退潮、量能不足、个股未随板块反弹 |
| 3 | 000977 浪潮信息 | 条件入场 | 缩量回落到计划区间后再观察分时承接 | 高位波动、板块热度证伪、资金持续流出 |
| 4 | 002603 以岭药业 | 观察 | 需要更多基本面与资金确认 | 题材持续性和业绩兑现不确定 |

## 执行原则

- 不追第一根放量阳线。
- 条件单必须同时具备触发条件、入场区间、止损、失效条件和有效期。
- 市场继续 risk_off 时，所有主动开仓动作降级。
- 工具失败或资金/筹码缺失时，不能声称“资金确认”。
- 没有账户现金和仓位约束时，只能写试探仓位范围，不能写确定买入数量。

## 证据链摘要

| 阶段 | 产物 | 作用 |
| --- | --- | --- |
| Planner | `planner.json`, `todo.md` | 记录能力域、工具计划、预期结果、下游用途和 replan 策略 |
| Candidate Discovery | `candidate_discovery.json` | 生成候选池和入池原因 |
| Seed Facts | `seed_facts.json` | 统一补齐候选共享事实包 |
| Four Desks | `expert_packets.json` | 结构反转、动量延续、质量修复、主题催化四类视角 |
| Meta | `meta_orchestrator.json` | 资产定性、硬约束和必算场景 |
| Pricing | `pricing_agent.json` | If-Then 条件单、止损和失效条件 |
| Allocation | `portfolio_allocation.json` | 账户约束下的执行排序 |
| Review / Judge | `adversarial_review.json`, `judge_decision.json` | 反方审查和最终裁决 |

## 主要缺口

- 市场 Regime 使用了保守降级口径，指数历史样本不足时不能输出强概率结论。
- 个别资金流工具返回失败或超时，报告只能保留技术/主题判断，不能把资金面写成已确认。
- 候选报告中的条件单需要在执行前用最新行情、涨跌停状态、账户现金和持仓上限重新校验。

## 复盘口径

该 Trace 进入本地入场执行回测数据集后，会按以下策略诊断 AI 点位：

- `strict_ai_entry`：严格使用 AI 给出的入场条件。
- `next_open_baseline`：下一交易日开盘基准。
- `atr_elastic_entry`：ATR 弹性入场。
- `breakout_fallback_entry`：突破触发备选；缺少突破价时跳过。

回测只用于诊断计划质量，不自动注入实时决策。
