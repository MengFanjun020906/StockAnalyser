<div align="center">

# 📈 StockAnalyser 股票智能分析系统

[![Repository](https://img.shields.io/badge/repo-MengFanjun020906%2FStockAnalyser-2088FF?logo=github)](https://github.com/MengFanjun020906/StockAnalyser)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-Ready-2088FF?logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![Private Fork](https://img.shields.io/badge/private%20fork-account--aware%20analysis-6f42c1)](https://github.com/MengFanjun020906/StockAnalyser)

> 🤖 面向个人账户和持仓决策的 A股/港股/美股智能分析系统。目标不是每日固定输出报告，而是在用户主动询问或出现重大事件时，结合持仓、成本、账户类型和风险偏好，给出更贴近个人账户的分析和行动建议。

[**功能特性**](#-功能特性) · [**快速开始**](#-快速开始) · [**目标输出**](#-目标输出) · [**分析原理**](docs/analysis-principles.md) · [**Agent 改造计划**](docs/agent-user-context-plan.md) · [**完整指南**](docs/full-guide.md) · [**常见问题**](docs/FAQ.md) · [**更新日志**](docs/CHANGELOG.md)


</div>

## 🧭 当前状态与路线图

### 当前已经具备

- **多市场分析**：支持 A股、港股、美股、美股指数和常见 ETF。
- **多维度数据链路**：覆盖技术面、实时行情、筹码、新闻舆情、公告、资金流、板块和基本面聚合。
- **确定性技术信号**：基于均线、乖离率、量能、支撑压力、MACD、RSI 生成 `signal_score` 和技术买卖信号。
- **LLM/Agent 综合分析**：将结构化数据转成趋势判断、账户影响、操作建议、买卖点位、止损、仓位和风险清单。
- **Agent 问股**：支持工具调用、策略技能和多 Agent 编排，可调用行情、K线、技术分析、新闻搜索、持仓快照等工具。
- **持仓管理底座**：已有账户、交易、现金流水、公司行动、FIFO/AVG 成本法、持仓快照、风险摘要和 CSV 导入能力。
- **账户感知第一阶段**：已新增 `AgentUserContext` 契约，定义投资者画像、账户上下文、持仓上下文和报告意图，但尚未接入运行时。
- **文档沉淀**：已补充 [分析原理](docs/analysis-principles.md) 和 [Agent 用户上下文改造计划](docs/agent-user-context-plan.md)。

### 近期计划

1. **Planning -> Execute Agent 外壳**
   新增 Planner，先识别任务意图和所需能力域，再通过 capability -> tools 映射展开为实际工具调用，最后交给决策 Agent 生成报告。初期作为实验模式，不直接替换现有 Agent。

2. **账户/持仓上下文接入**
   复用现有 `PortfolioService` 和 `get_portfolio_snapshot`，把账户类型、融资融券状态、持仓数量、成本、仓位、浮盈亏等转成 `AgentUserContext` 注入 Agent。

3. **持仓诊断报告 `position_review`**
   对已经持仓的股票输出继续持有、加仓、减仓、止盈、止损、仓位调整和风险触发条件。

4. **选股入场报告 `entry_analysis`**
   对未持仓或候选股票输出是否值得进入候选池、理想入场点、次优入场点、首仓比例、止损位和淘汰条件。

5. **Web 配置入口**
   在 Web 端补充投资者画像和账户分析偏好，例如风险偏好、交易周期、单票仓位上限、默认止损比例和是否允许融资融券。

6. **回测与个性化校准**
   将历史信号表现、个股胜率、策略表现用于 Agent 内部可靠性判断和策略权重校准。

### 暂不做的事

- 不重建一套新的持仓账本，后续账户感知分析会复用现有持仓管理模块。
- 不强制用户配置真实账户；没有账户上下文时仍保留普通选股分析模式。
- 不在第一阶段改变当前 Web 手动分析和现有 Agent 的行为。
- 不把未来产品方向定位为每日固定报告、大盘复盘报告或固定时间推送。

## ✨ 功能特性

| 模块 | 功能 | 说明 |
|------|------|------|
| AI | 账户感知分析 | 结合个股数据、账户约束、持仓成本、风险偏好输出行动建议 |
| 分析 | 多维度分析 | 技术面、实时行情、筹码分布、新闻舆情、公告、资金流与基本面聚合 |
| 市场 | 全球市场 | 支持 A股、港股、美股、美股指数及常见 ETF |
| 策略 | 市场策略系统 | 内置均线、缠论、波浪、情绪周期等策略能力，后续用于个性化计划 |
| 触发 | 按需与事件触发 | 用户主动询问或重大事件出现时触发分析，避免固定日报噪音 |
| Web | 双主题工作台 | 支持手动分析、配置管理、任务进度、历史报告、回测、持仓管理 |
| 导入 | 智能导入与补全 | 支持图片、CSV/Excel、剪贴板导入，自选股输入支持代码/名称/拼音/别名补全 |
| 历史 | 报告管理 | 支持历史报告查看、完整 Markdown 报告、重新分析与批量管理 |
| 回测 | AI 回测验证 | 对历史分析进行事后验证，查看方向准确率和模拟收益 |
| Agent 问股 | 策略对话 | 多轮策略问答，支持均线金叉/缠论/波浪等 11 种内置策略，Web/Bot/API 全链路 |
| 账户感知 | 持仓上下文规划 | 已定义投资者画像、账户、持仓和报告意图 schema，后续用于持仓诊断和选股入场分型报告 |
| 通知 | 事件通知 | 保留企业微信、飞书、Telegram、Discord、Slack、邮件等渠道，用于主动请求或事件触发结果 |
| 服务 | 本地/Web/API | 支持本地运行、Docker、FastAPI 服务和 Web 工作台 |

> 功能细节、字段契约、基本面 P0 超时语义、交易纪律、数据源优先级、Web/API 行为请看 [完整配置与部署指南](docs/full-guide.md)。分析链路、信号评分和 Agent 工具说明见 [分析原理文档](docs/analysis-principles.md)。账户感知 Agent 的第一阶段计划见 [Agent 用户上下文与分阶段改造计划](docs/agent-user-context-plan.md)。

### 技术栈与数据来源

| 类型 | 支持 |
|------|------|
| AI 模型 | [AIHubMix](https://aihubmix.com/?aff=CfMq)、Gemini、OpenAI 兼容、DeepSeek、通义千问、Claude、Ollama 本地模型等 |
| 行情数据 | [TickFlow](https://tickflow.org/auth/register?ref=WDSGSPS5XC)、AkShare、Tushare、Pytdx、Baostock、YFinance、Longbridge |
| 新闻搜索 | [Anspire](https://aisearch.anspire.cn/)、[SerpAPI](https://serpapi.com/baidu-search-api?utm_source=github_daily_stock_analysis)、[Tavily](https://tavily.com/)、[Bocha](https://open.bocha.cn/)、[Brave](https://brave.com/search/api/)、[MiniMax](https://platform.minimaxi.com/)、SearXNG |
| 社交舆情 | [Stock Sentiment API](https://api.adanos.org/docs)（Reddit / X / Polymarket，仅美股，可选） |

> 完整规则见 [数据源配置](docs/full-guide.md#数据源配置)。

## 🚀 快速开始

### 方式一：本地运行 / Web 工作台（推荐）

```bash
git clone https://github.com/MengFanjun020906/StockAnalyser.git
cd StockAnalyser
pip install -r requirements.txt
cp .env.example .env
python main.py --serve-only
```

访问 `http://127.0.0.1:8000` 后，可以在 Web 工作台中配置模型、录入持仓、手动发起分析或进入 Agent 问股。

最小配置：

**AI 模型配置（至少配置一个）**

默认先选一个模型服务商并填写 API Key；需要多模型、图片识别、本地模型或高级路由时，再参考 [LLM 配置指南](docs/LLM_CONFIG_GUIDE.md)。

> 💡 **推荐 [AIHubMix](https://aihubmix.com/?aff=CfMq)**：一个 Key 即可使用 Gemini、GPT、Claude、DeepSeek 等全球主流模型，无需科学上网，含免费模型（glm-5、gpt-4o-free 等），付费模型高稳定性无限并发。本项目可享 **10% 充值优惠**。

| Secret 名称 | 说明 | 必填 |
|------------|------|:----:|
| `AIHUBMIX_KEY` | [AIHubMix](https://aihubmix.com/?aff=CfMq) API Key，一 Key 切换使用全系模型 | 可选 |
| `GEMINI_API_KEY` | Google Gemini API Key | 可选 |
| `ANTHROPIC_API_KEY` | Anthropic Claude API Key | 可选 |
| `OPENAI_API_KEY` | OpenAI 兼容 API Key（支持 DeepSeek、通义千问等） | 可选 |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | 使用 OpenAI 兼容服务时填写 | 可选 |

> Ollama 适合本地 / Docker 部署；云端模型更适合需要稳定联网和多设备访问的场景。

**通知渠道配置（可选）**

| Secret 名称 | 说明 |
|------------|------|
| `WECHAT_WEBHOOK_URL` | 企业微信机器人 |
| `FEISHU_WEBHOOK_URL` | 飞书机器人 |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram |
| `DISCORD_WEBHOOK_URL` | Discord Webhook |
| `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_ID` | Slack Bot |
| `EMAIL_SENDER` + `EMAIL_PASSWORD` | 邮件通知 |

通知不是必需项。未来目标是只在用户主动请求或重大事件触发时发送，而不是每天固定推送。更多渠道、签名校验、分组邮件、Markdown 转图片等配置见 [通知渠道详细配置](docs/full-guide.md#通知渠道详细配置)。

**自选股配置（必填）**

| Secret 名称 | 说明 | 必填 |
|------------|------|:----:|
| `STOCK_LIST` | 自选股代码，如 `600519,hk00700,AAPL,TSLA` | ✅ |

**新闻源配置（推荐）**

新闻源会显著影响舆情、公告、事件和催化因素质量，建议至少配置一个搜索服务。

| Secret 名称 | 说明 | 必填 |
|------------|------|:----:|
| `ANSPIRE_API_KEYS` | [Anspire AI Search](https://aisearch.anspire.cn/)：中文内容特别优化，可增强 A 股分析效果 | 推荐 |
| `SERPAPI_API_KEYS` | [SerpAPI](https://serpapi.com/baidu-search-api?utm_source=github_daily_stock_analysis)：搜索引擎结果补强，适合实时金融新闻 | 推荐 |
| `TAVILY_API_KEYS` | [Tavily](https://tavily.com/)：通用新闻搜索 API | 可选 |
| `BOCHA_API_KEYS` | [博查搜索](https://open.bocha.cn/)：中文搜索优化，支持 AI 摘要 | 可选 |
| `BRAVE_API_KEYS` | [Brave Search](https://brave.com/search/api/)：隐私优先，美股资讯补强 | 可选 |
| `MINIMAX_API_KEYS` | [MiniMax](https://platform.minimaxi.com/)：结构化搜索结果 | 可选 |
| `SEARXNG_BASE_URLS` | SearXNG 自建实例：无配额兜底，适合私有部署 | 可选 |

更多搜索源、社交舆情和降级规则见 [搜索服务配置](docs/full-guide.md#搜索服务配置)。

### 方式二：命令行按需分析

```bash
python main.py --stocks 600519,hk00700,AAPL
python main.py --dry-run
python main.py --debug
```

### 方式三：可选的 GitHub Actions 手动触发

如果希望不用服务器也能运行，可以保留 GitHub Actions，但建议作为手动触发或事件触发入口，而不是每天固定跑。

基本步骤：

1. 在私有仓库中配置 Actions Secrets / Variables。
2. 填写至少一个 LLM Key 和 `STOCK_LIST`。
3. 进入 Actions 页面，手动运行工作流。

当前仓库仍保留上游的定时任务能力；如果不需要每日分析，应关闭或调整对应 workflow 的 schedule。

### 方式四：Docker 部署

```bash
docker-compose -f ./docker/docker-compose.yml up -d server
```

> Docker、云服务器访问和桌面客户端打包请参考 [完整指南](docs/full-guide.md) 与 [桌面端打包说明](docs/desktop-package.md)。

## 🎯 目标输出

未来输出不再以“每日决策仪表盘”或“大盘复盘报告”为中心，而是围绕用户问题和事件触发生成更短、更具体的账户建议。

### 用户主动询问

```text
用户：我持有 600519，成本 1580，普通账户，仓位 18%，现在要不要减？

系统目标输出：
- 当前结论：继续持有 / 减仓 / 止盈 / 止损
- 与用户成本的关系：安全垫、浮盈亏、回撤空间
- 技术状态：趋势、均线、量价、支撑压力
- 事件影响：近期公告、新闻、资金流和行业变化
- 操作计划：减仓条件、止损位、观察点、仓位上限
- 不确定项：哪些数据缺失，哪些需要用户确认
```

### 重大事件触发

```text
触发源：持仓股出现减持、处罚、业绩预告、异常波动、重大公告或价格跌破关键位。

系统目标输出：
- 事件摘要：发生了什么，影响哪只持仓
- 账户影响：影响持仓成本、安全垫、仓位和风险暴露
- 风险等级：低 / 中 / 高
- 建议动作：继续观察 / 降仓 / 止损 / 等待澄清
- 触发条件：什么情况下升级为必须处理
```

### 选股或准备入场

```text
用户：帮我看看 300750 现在能不能开仓。

系统目标输出：
- 是否适合进入候选池
- 当前是否适合买入
- 理想入场点和次优入场点
- 禁止追高线
- 首仓比例
- 止损位和淘汰条件
```

## ⚙️ 配置说明

完整环境变量、模型渠道、通知渠道、数据源优先级、交易纪律、基本面 P0 语义和部署说明请参考 [完整配置指南](docs/full-guide.md)。

## 🖥️ Web 界面

![img.png](sources/fastapi_server.png)

Web 工作台提供配置管理、任务监控、手动分析、历史报告、回测、持仓管理、智能导入和浅色 / 深色主题。启动方式：

```bash
python main.py --webui
python main.py --webui-only
```

访问 `http://127.0.0.1:8000` 即可使用。认证、智能导入、搜索补全、历史报告复制、云服务器访问等细节见 [本地 WebUI 管理界面](docs/full-guide.md#本地-webui-管理界面)。

## 🤖 Agent 策略问股

配置任意可用 AI API Key 后，Web `/chat` 页面即可使用策略问股；如需显式关闭可设置 `AGENT_MODE=false`。

- 支持均线金叉、缠论、波浪理论、多头趋势等内置策略
- 支持实时行情、K 线、技术指标、新闻和风险信息调用
- 支持多轮追问、会话导出、发送到通知渠道和后台执行
- 支持自定义策略文件与多 Agent 编排（实验性）

> Agent 具体参数、`skill` 命名兼容、多 Agent 模式和预算护栏见 [完整指南](docs/full-guide.md#本地-webui-管理界面) 与 [LLM 配置指南](docs/LLM_CONFIG_GUIDE.md)。

## 相关项目 (Related Projects)

DSA 聚焦日常分析报告；下面两个同系列项目分别覆盖选股、策略验证与策略进化，适合按需延伸使用。它们当前独立维护，后续会优先探索与 DSA 的候选股导入、回测验证和报告联动。

- [AlphaSift](https://github.com/ZhuLinsen/alphasift)：多因子选股与全市场扫描，用于从股票池中提取候选标的。
- [AlphaEvo](https://github.com/ZhuLinsen/alphaevo)：策略回测与自我进化，用于验证策略规则，并通过迭代探索策略参数与组合。

---


## ⚠️ 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。股市有风险，投资需谨慎。作者不对使用本项目产生的任何损失负责。

---
