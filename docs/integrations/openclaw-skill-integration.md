# openclaw Skill 集成指南

本文档说明如何通过 [openclaw](https://github.com/openclaw/openclaw) Skill 调用 StockAnalyser 的 REST API，在 openclaw 对话中运行业务分析链路。

## 概述

- **集成方式**：openclaw Skill 通过 HTTP 调用 StockAnalyser REST API
- **适用场景**：已部署 StockAnalyser API 服务，希望在 openclaw 对话中触发 Agent 链路或查询信号结果
- **变更说明**：Web 首页、问股页面及其 `/api/v1/analysis/*`、`/api/v1/agent/chat*` 接口已移除；需要生成分析报告时请使用 CLI、定时任务或 Bot，需要查看 Agent 全链路时使用 Agent Trace API

## 前置条件

1. **StockAnalyser 必须已运行**：执行 `python main.py --serve-only` 或通过 Docker 部署，使 API 长期可用
2. **openclaw 需具备 HTTP 调用能力**：如 `system.run` 执行 curl，或内置 HTTP 工具（如 api-tester 等）
3. **Agent 模式已启用**：`.env` 中设置 `AGENT_MODE=true`
4. **说明**：GitHub Actions 仅做定时任务，不长期暴露 API，需本地或 Docker 运行 StockAnalyser

## 核心 API 参考

| 接口 | 方法 | 用途 |
|------|------|------|
| `/api/v1/agent/trace/run` | POST | 运行一次 Agent 全链路并返回结构化 Trace |
| `/api/v1/agent/trace/stream` | POST (SSE) | 运行 Agent 全链路并流式返回事件 |
| `/api/v1/signal-preview` | GET | 查询信号与持仓监控结果 |
| `/api/health` | GET | 健康检查 |

### Agent Trace 请求体

```json
{
  "message": "分析 600519 的入场机会",
  "session_id": "optional-session-id",
  "stock_code": "600519",
  "stock_name": "贵州茅台",
  "analysis_mode": "planning_execute",
  "inject_portfolio_context": true
}
```

- `message`：自然语言问题（必填）
- `session_id`：可选，未传时自动生成
- `stock_code` / `stock_name`：可选，显式指定标的
- `analysis_mode`：默认 `planning_execute`
- `inject_portfolio_context`：是否注入持仓上下文，默认 `true`

响应包含 `success`、`session_id`、`content`、`tool_calls`、`events`、`risk_gate`、`price_plan` 等字段。

## 股票代码格式

- A股：6位数字（600519、000001）
- 港股：hk + 5位数字（hk00700）
- 美股：1–5 字母（AAPL、TSLA、BRK.B）
- 美股指数：SPX、DJI、IXIC 等

## 调用示例

```bash
# 健康检查
curl http://127.0.0.1:8000/api/health

# 运行 Agent Trace
curl -X POST http://127.0.0.1:8000/api/v1/agent/trace/run \
  -H 'Content-Type: application/json' \
  -d '{"message": "分析 600519 的入场机会", "stock_code": "600519"}'

# 查询信号预览
curl http://127.0.0.1:8000/api/v1/signal-preview
```

## 故障排查

| 现象 | 可能原因 | 处理建议 |
|------|----------|----------|
| 连接失败 | StockAnalyser 未运行、端口错误、防火墙 | 确认 `python main.py --serve-only` 已启动，检查 `DSA_BASE_URL` |
| 400 错误 | 股票代码格式错误、Agent 模式未启用 | 检查代码格式，确认 `.env` 中 `AGENT_MODE=true` |
| 422 错误 | 问题标的与显式 `stock_code` 冲突 | 清空或修正 `stock_code` 后重试 |
| 500 错误 | AI 配置、数据源、网络问题 | 查看 StockAnalyser 日志，确认模型与数据源配置 |
| 分析超时 | 同步等待时间过长 | 增加 HTTP 客户端超时，或改用 `/api/v1/agent/trace/stream` |

## 认证说明

默认情况下 StockAnalyser API 无需认证。若在 `.env` 中启用了 `ADMIN_AUTH_ENABLED=true`，则需在 Skill 调用时携带登录后获得的 Cookie，具体方式取决于 openclaw 的 HTTP 工具能力（当前 API 仅支持 Cookie 认证，不支持 Bearer Token）。
