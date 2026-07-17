# StockAnalyser

自部署的账户感知 AI 投资研究与选股系统。系统把候选发现、逐股取证、Meta 场景约束、点位计算、组合配置、反方审查、Judge 裁决和风控闸门拆成可追溯阶段，输出带条件的研究 memo 和交易计划。系统不会自动下单，最终决策权始终属于使用者。

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React%20%2B%20Vite-web-61DAFB?logo=react&logoColor=black)](https://vite.dev/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](#)

## 当前定位

StockAnalyser 不是“输入股票代码后生成一篇看似完整报告”的工具，而是一个个人投资研究工作台：

- 对没有明确股票代码的选股需求，先构建候选池，再逐只取证。
- 对单股入场或持仓复查，注入账户、仓位、成本、回撤约束和风险偏好。
- 对每次结论落盘 Trace，保留工具调用、证据缺口、阶段 JSON 和最终报告，方便复盘。
- 对工具失败、行情过期、资金缺口、账户约束和市场高波动显式降级，不把不确定性包装成确定性建议。

当前分支重点服务 A 股研究，同时保留港股、美股等原有能力。项目仍处于个人研究和开发调试阶段，不构成投资建议。

## AI Operating Model

StockAnalyser 把 AI 链路拆成可审计的 `workflow`、`pipeline` 和 `loop`：

- `Workflow`：阶段顺序，例如 seed pool -> 四席位 -> Meta -> 点位计算 -> 组合配置 -> 反方审查 -> Judge。
- `Pipeline`：阶段间 artifact 的输入输出，例如 `candidate_discovery.json`、`pricing_agent.json`、`final_report.json`。
- `Loop`：plan -> execute -> evaluate -> replan -> stop 的反馈控制，用于决定补证、降级或停止。
- `Planning Ledger / 计划账本`：`todo.md` 的正式语义。它保存工具预期结果、下游用途、失败降级、下一步和复用契约，恢复历史 Trace 时只复用计划摘要，不复用过期行情或旧新闻事实。

主 Agent 命名为 **Serenity Investment Orchestrator / 静研投资编排官**。当前文档层命名如下，代码类名和 JSON 字段保持兼容：

| 层级 | English / 中文 | 当前实现 |
| --- | --- | --- |
| 主 Agent | Serenity Investment Orchestrator / 静研投资编排官 | `AgentExecutor`, Trace API |
| 计划层 | Planning Steward / 计划管控官 | `src.agent.planner` |
| 技术层 | Market Structure Analyst / 市场结构分析师 | `TechnicalAgent` |
| 消息层 | Intelligence Analyst / 消息情报分析师 | `IntelAgent` |
| 风控层 | Risk Control Analyst / 风险控制分析师 | `RiskAgent`, `risk_gate` |
| 四席位 | Structural Reversal / Momentum Continuation / Quality Repair / Theme Catalyst | 结构反转席、动量延续席、质量修复席、主题催化席 |
| 裁决层 | Investment Arbiter / 投资裁决官 | `judge_decision` |

完整概念边界和命名表见 [Agent Loop / Workflow Glossary](docs/architecture/agent-loop-workflow-glossary.md)。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 账户感知分析 | 同一只股票会根据现金、仓位、成本、最大回撤、单票上限和风险偏好给出不同计划。 |
| 四席位候选池 | 低位启动、动量、质量修复、主题催化四类打法并行观察同一批事实包，只负责候选发现，不直接给买入结论。 |
| 盘前主题种子 | 东方财富财经早餐原文按 06:00 路由，结合本地概念字典、分词和规则引擎生成宽口径主题候选。 |
| 结构化取证 | 行情、均线、量能、价格结构、资金流、筹码、消息、基本面和市场 Regime 以工具结果和 EvidenceCard 进入后续阶段。 |
| Meta-Agent 约束层 | Meta 只做资产定性、事实共识、策略分歧和硬约束包，不直接排序或给买卖点。 |
| 点位计算层 | 基于 Meta 硬约束生成 If-Then 条件单，明确触发、入场区间、止损、失效条件和风险收益说明。 |
| 组合配置与 Judge | 组合配置负责账户约束下的执行排序；反方审查挑战主方案；Judge 决定 open、wait、monitor 或 reject。 |
| Risk Gate | T+1、涨跌停、ST/退市风险、止损、仓位和数据质量由确定性风控闸门检查。 |
| Agent Trace | 每次分析落盘 `data/agent_traces/<trace-id>/`，前端可查看阶段状态、工具诊断、候选池和最终报告。 |

## 选股链路概览

选股链路的核心原则是：候选池不等于推荐，机会质量不等于立即可执行。

```text
用户选股需求
  -> L1 seed pool: AlphaSift / Sequoia / 资金 / 热点板块 / 盘前主题 / 用户自选
  -> SeedFactPacket: 每只 seed 并行补齐共享事实包
  -> 四席位并行: 低位启动 / 动量 / 质量修复 / 主题催化
  -> 聚合与名额分配: 去重、冲突标记、按市场状态分配席位名额
  -> candidate_screening: 初筛深挖对象
  -> single_stock_deep_dive: 按 setup playbook 逐股补证与裁决冲突
  -> Meta-Agent: 资产定性、事实共识、策略分歧、硬约束、必算场景
  -> pricing_agent: If-Then 条件单和失效条件
  -> portfolio_allocation: 账户约束下的执行排序和仓位计划
  -> adversarial_review: 反方审查证据缺口与风险
  -> judge_decision: 最终裁决
  -> final.md: 机会首选 / 执行首选 / 可观察标的 / 观察池
```

最终报告表头采用双轴：

- `机会首选`：股票本身的机会质量，不因账户过于谨慎而被抹掉。
- `执行首选`：账户、市场和风控约束下当前最接近可执行的标的，可能只是“条件触发”。
- `可观察标的`：已经进入主线深挖、但执行优先级低于首选的候选。
- `观察池`：未进入本轮深挖或证据不足的候选，只用于后续跟踪。

完整链路、输入输出和字段契约见 [选股链路说明](docs/architecture/stock-selection-pipeline.md)。

## 示例报告与回测快照

- 完成报告样例：[2026-07-15 watchlist report snapshot](docs/examples/agent-watchlist-report-20260715.md)
- 本地原始 Trace：`data/agent_traces/20260715-161824-trace-93c77fcb2ea64fec8c124304bf4c8719/final.md`
- 回测数据集：`data/agent_reviews/entry_execution_backtest.jsonl`

截至 2026-07-17 的本地入场执行回测快照覆盖 48 个 Trace、94 条最终计划，决策日期范围为 2026-05-15 至 2026-07-14。样本全部解析成功；严格 AI 入场策略成交 48 次、未触发 43 次、缺少起始价 3 次。

| 策略 | 成交数 | 胜率 | 平均 PnL | 中位 PnL | 最好 | 最差 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `strict_ai_entry` | 48 | 18.75% | -1.09% | -2.45% | 20.00% | -11.67% |
| `next_open_baseline` | 91 | 29.67% | -1.81% | -3.40% | 49.38% | -24.70% |
| `atr_elastic_entry` | 55 | 18.18% | -2.47% | -3.71% | 17.65% | -13.40% |

这组结果说明：当前 AI 条件入场相对下一开盘基准降低了成交数和平均亏损，但整体仍为负收益样本，后续重点应继续改进候选质量、入场有效期、资金确认和 risk-off 降级规则。回测只用于诊断，不构成收益承诺。

## 主要使用场景

| 场景 | 典型问题 | 输出 |
| --- | --- | --- |
| 选股扫描 | “下周有哪些值得关注的股票？” | 候选池、深挖主线、机会首选、执行首选、观察池、证据缺口。 |
| 单股入场 | “这只股票现在能不能买？” | 入场条件、禁止追高线、止损/失效条件、首仓建议和风控阻断。 |
| 持仓复查 | “这只持仓还能不能拿？” | 持有、减仓、加仓、止盈、止损和复查触发条件。 |
| 风险复盘 | “今天这个判断为什么变了？” | Trace、工具失败、市场状态、Judge 裁决和风控原因。 |

## 快速开始

推荐使用 `uv`：

```bash
uv venv
uv pip install -r requirements.txt
cp .env.example .env
./start_all.sh
```

启动后访问：

- Web: `http://127.0.0.1:5173`
- Agent Trace: `http://127.0.0.1:5173/agent-trace`
- 后端 API: `http://127.0.0.1:8000`
- Neo4j Browser: `http://127.0.0.1:7474/browser/`

停止服务：

```bash
./stop_all.sh
```

只启动后端：

```bash
python main.py --serve-only
```

## 最小配置

至少需要配置一个 OpenAI 兼容 LLM：

```env
AGENT_MODE=true
AGENT_ANALYSIS_MODE=planning_execute

OPENAI_API_KEY=
OPENAI_BASE_URL=
OPENAI_MODEL=
```

选股候选增强可选配置：

```env
SEQUOIA_CANDIDATE_DB_PATH=Sequoia-X/data/sequoia_v2.db
ALPHASIFT_STRATEGY_DIR=alphasift/alphasift/strategies
ALPHASIFT_CANDIDATE_DB_PATH=Sequoia-X/data/sequoia_v2.db
AGENT_SEED_POOL_TOTAL_LIMIT=32
```

长期记忆可选配置：

```env
GRAPHITI_ENABLED=false
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=changeme
GRAPHITI_EMBEDDING_MODEL=
GRAPHITI_EMBEDDING_BASE_URL=
GRAPHITI_EMBEDDING_API_KEY=
```

环境检查：

```bash
python test_env.py
python test_env.py --graph
```

完整配置见 [.env.example](.env.example) 和 [完整指南](docs/full-guide.md)。

## 项目结构

```text
src/agent/                         Agent 编排、选股流水线、Prompt、风控和工具注册
src/agent/candidate_experts_v2/    四席位候选池、SeedFactPacket、FactSheet、聚合与召回
src/agent/tools/                   行情、资金、新闻、结构、市场状态等工具
data_provider/                     多数据源适配
api/                               FastAPI API
apps/dsa-web/                      Web 前端和 Agent Trace 页面
docs/                              架构、选股链路、配置、排障和实施文档
tests/                             后端与前端回归测试
data/agent_traces/                 本地 Trace artifact，默认不提交
```

## 相关文档

- [Agent Loop / Workflow Glossary](docs/architecture/agent-loop-workflow-glossary.md)
- [选股链路说明](docs/architecture/stock-selection-pipeline.md)
- [完成报告样例](docs/examples/agent-watchlist-report-20260715.md)
- [选股链路重构实施方案](docs/architecture/选股链路重构-实施方案.md)
- [候选池路线图](docs/plans/agent-candidate-pool-roadmap.md)
- [阶段化选股 Prompt 设计](docs/plans/agent-stock-selection-prompts.md)
- [A 股 Regime 状态机](docs/modules/regime-state-machine.md)
- [价格结构分析引擎](docs/modules/price-structure-engine.md)
- [Agent 工具能力缺口分析](docs/plans/agent-tool-gap-analysis.md)
- [完整指南](docs/full-guide.md)
- [更新日志](docs/CHANGELOG.md)

## 验证与开发

后端：

```bash
python -m pytest -m "not network"
python -m py_compile src/agent/stock_selection.py
```

前端：

```bash
cd apps/dsa-web
npm run lint
npm run build
```

CI 入口见 `.github/workflows/ci.yml`。

## 免责声明

本项目仅用于个人学习、研究、复盘和模拟验证，不构成任何投资建议。LLM 会出错，数据源会失败，市场会发生无法预期的跳变。系统不会自动下单，任何分析、模拟盘或未来交易接口都需要使用者自行承担风险。
