# StockAnalyser Agent Trace 分支

这是一个面向个人 A 股研究和账户决策的本地 Agent 系统。当前私有分支的重点不是维护上游完整的日报、通知、桌面端和泛化功能，而是围绕一条主链路继续演进：

```text
用户问题 / 选股需求 / 个股分析
  -> /agent-trace 调试入口
  -> AgentUserContext 账户与风险偏好
  -> planning_execute 任务规划
  -> 工具取证
  -> 阶段化选股或单股深度分析
  -> 强制反方 Debate + Judge 裁决
  -> Trace 产物落盘
  -> Graphiti 知识图谱记忆
  -> 后续方案保存、模拟盘托管、回测、自进化和量化交易
```

更细的设计和后续路线见：

- [Agent 用户上下文与分阶段改造计划](docs/agent-user-context-plan.md)
- [Agent 工具能力缺口分析](docs/agent-tool-gap-analysis.md)
- [Graphiti 时序知识图谱集成计划](docs/graphiti-integration-plan.md)
- [阶段化选股 Prompt 设计](docs/agent-stock-selection-prompts.md)
- [更新日志](docs/CHANGELOG.md)

## 当前主链路

### 1. `/agent-trace` 是主要入口

当前主要使用 Web 开发调试页 `/agent-trace`，而不是上游的普通日报链路。

它负责：

- 输入用户问题、目标股票、报告意图和风险偏好。
- 注入账户、持仓、仓位上限、最大回撤、默认止损等上下文。
- 流式展示 Planner、工具调用、Evidence Timeline、Debate Judge 和最终输出。
- 按 session 落盘 `request.json`、`context.json`、`planner.json`、`events.ndjson`、`tool_calls.json`、`evidence_ledger.json`、`stock_selection.json`、`debate.json`、`final.md`、`todo.md` 和 `summary.json`。
- 在 Graphiti 开启时，将 agent-trace 对话作为 episode 入知识图谱。

### 2. `planning_execute` 是核心执行模式

`planning_execute` 的目标不是让模型直接给买卖结论，而是先规划、再取证、再裁决。

核心步骤：

```text
Intent 识别
  -> Capability 选择
  -> Tool Execution Plan
  -> Evidence Ledger
  -> Primary Thesis
  -> Opposing Thesis
  -> Judge Decision
  -> Final Report
```

它当前支持这些报告意图：

| 意图 | 用途 |
| --- | --- |
| `position_review` | 已持仓诊断：持有、加仓、减仓、止盈、止损和风险触发 |
| `entry_analysis` | 未持仓入场分析：是否能买、入场区间、首仓比例、止损和淘汰条件 |
| `watchlist_scan` | 选股：候选发现、初筛、深度分析、组合配置和反方审查 |
| `risk_review` | 账户或组合风险检查 |
| `event_impact` | 重大事件对持仓或候选股的影响评估 |

### 3. 选股链路已经阶段化

`watchlist_scan` 不再允许在没有候选池的情况下直接输出推荐。当前选股链路是：

```text
候选发现
  -> 候选初筛
  -> 单股深度分析
  -> 组合配置
  -> 反方审查
  -> Judge 裁决
```

关键约束：

- 用户提供股票时，优先分析用户给出的股票。
- 用户没有提供股票时，必须先调用候选发现工具生成候选池。
- 候选发现只代表“值得继续取证”，不是买入推荐。
- 最终排序必须基于行情、技术、消息、资金、基本面、账户约束和数据质量。
- 工具失败必须进入 Evidence Ledger，不能在前端显示假 OK。

### 4. Graphiti 用于长期记忆

Graphiti/Neo4j 当前用于把分析 episode 写成时序知识图谱。

目前目标：

- 保存 agent-trace 链路中的分析结论、股票、板块、事件和关系。
- 支持后续查询历史分析、事件演化和同类股票关系。
- 为连续对话、策略复盘、自进化和回测提供长期记忆。

默认一键启动脚本会尝试同时准备 Neo4j。没有配置 Graphiti 时，主分析链路仍可运行。

## 后续路线

当前路线已经收敛为八个方向：

| 阶段 | 方向 | 目标 |
| --- | --- | --- |
| A | 工具缺口补全 | 补市场情绪、地缘风险、跨资产、板块催化、组合压力测试 |
| B | 连续对话 | 让 Agent 记住同一 session、用户偏好、历史方案和事实关系 |
| C | 方案保存与模拟盘托管 | 把每次链路生成的交易方案结构化保存，并在模拟盘按条件跟踪 |
| D | 回测系统 | 验证单次计划、策略、regime 和工具组合是否有效 |
| E | Regime 识别 | 判断当前市场状态，并约束仓位、入场条件和止损纪律 |
| F | 策略库建设 | 沉淀可版本化、可回测、可复盘的策略 |
| G | 自进化系统 | 基于回测、模拟盘、Trace 和用户反馈提出候选优化 |
| H | 量化交易系统 | 在模拟盘和风控成熟后，再考虑规则化执行和真实交易接口 |

量化交易和真实自动交易是后期目标。当前优先级是：先把工具、记忆、方案保存、模拟盘和回测闭环打牢。

## 本地启动

推荐使用 uv：

```bash
uv venv
uv pip install -r requirements.txt
cp .env.example .env
./start_all.sh
```

启动后访问：

- Web：`http://127.0.0.1:5173`
- 后端 API：`http://127.0.0.1:8000`
- Agent Trace：`http://127.0.0.1:5173/agent-trace`
- Neo4j Browser：`http://127.0.0.1:7474/browser/`

停止：

```bash
./stop_all.sh
```

如果只需要后端：

```bash
python main.py --serve-only
```

## 关键配置

最少需要配置一个可用 LLM。Graphiti 如需启用，还要配置 Neo4j 和 embedding。

```env
# Agent
AGENT_MODE=true
AGENT_ANALYSIS_MODE=planning_execute

# LLM，按实际服务填写一个即可
OPENAI_API_KEY=
OPENAI_BASE_URL=
OPENAI_MODEL=

# Graphiti，可选
GRAPHITI_ENABLED=false
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=changeme
GRAPHITI_EMBEDDING_MODEL=
GRAPHITI_EMBEDDING_BASE_URL=
GRAPHITI_EMBEDDING_API_KEY=
GRAPHITI_GROUP_STRATEGY=market
```

环境检查：

```bash
python test_env.py
python test_env.py --graph
```

完整配置仍以 `.env.example` 和 [完整指南](docs/full-guide.md) 为准。

## 当前保留但非重点的能力

仓库中仍保留不少上游能力，例如：

- 普通单股分析流水线
- Web 工作台的历史报告、配置、持仓、回测等页面
- Bot 和通知渠道
- GitHub Actions 定时任务
- 桌面端相关目录
- 多市场数据源和旧 Agent 问股模式

这些能力可能仍能使用，但不是当前分支的主要维护对象。后续如果和 agent-trace 主链路冲突，会优先保留主链路，其他功能可能被删除、收敛或重写。

## 开发验证

常用验证：

```bash
python -m py_compile <changed_python_files>
python -m pytest -m "not network"
./scripts/ci_gate.sh
```

Web 变更：

```bash
cd apps/dsa-web
npm run lint
npm run build
```

文档-only 变更不强制跑测试，但需要同步 [docs/CHANGELOG.md](docs/CHANGELOG.md)。

## 免责声明

本项目仅供个人学习、研究和模拟验证使用，不构成任何投资建议。A 股、港股、美股均存在本金亏损风险。任何自动化分析、模拟盘或未来交易接口都必须由使用者自行承担风险。
