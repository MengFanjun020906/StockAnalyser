# Jev 日线影子评估

## 范围与权限

支持 A 股、港股、美股的日线方向、单股价格方案复核与持仓下行风险评估，窗口固定为后续 1、3、5 个交易所交易日。所有结果固定为 `shadow_mode=true`、`execution_eligible=false`，不下单、不改价格、不改变原有报告、风险门、持仓动作或通知条件。

Jev 是 TypeSafe 的结构化评估服务，不是聊天模型。采用项目已有的 httpx 调用官方 `POST /v1/systemone`，不增加 JavaScript 后端或新的 SDK 依赖。行情指标和日期比较由 Python 计算，模型仅回答有限选项的问题。

官方参考：

- https://docs.typesafe.ai/introduction/quickstart
- https://docs.typesafe.ai/api
- https://docs.typesafe.ai/model-jaggedness/jev-1.13
- 参考应用：https://github.com/jarrodwatts/jev-trader ，本次调研版本 `b587759e459ea049590102e54a0b07800864cdc3`。股票接入不使用其链上交易执行代码。

## 配置

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `JEV_ENABLED` | `false` | 启用手动及业务流程影子评估 |
| `JEV_API_KEY` | 空 | TypeSafe API 密钥；为空时依次读取 `TYPESAFE_API_KEY`、`TYPESAFE_AI_API_KEY` |
| `JEV_BASE_URL` | `https://api.typesafe.ai/v1` | HTTPS API 基础地址，不带 `/systemone` |
| `JEV_MODEL` | `jev-latest` | 请求模型；记录中另外保留服务实际返回的版本 |
| `JEV_TIMEOUT_SECONDS` | `15` | HTTP 超时秒数；429/529 等待一秒后最多重试一次 |

密钥只在后端读取，设置页按敏感字段遮罩；状态 API 仅返回是否配置。HTTP 异常保存安全错误码，不保存响应正文、授权头或密钥。不跟随 HTTP 重定向。

连通检查只产生一次很小的计费请求，不写入股票评估记录：

```bash
rtk .venv/bin/python scripts/check_jev_connection.py
```

## 业务入口

- 单股 Chat 的信号收尾流程：成功后运行一次 Jev 评估。结构化多股任务、选股和失败分析跳过。模型调用失败不影响原结果。
- 单股 Agent Trace（普通与流式）：本轮实际读取的日线、工具证据和确定性方案先冻结，原报告返回后后台执行 Jev 复核；结果追加到同一条 Trace 的流程节点和最终报告旁的对照区。
- 持仓扫描：沿用 `python main.py --schedule` 与 `POSITION_WATCH_ENABLED`；每个市场收盘后扫描时，额外记录持仓影子评估，不改变 L0/L1 判断和通知。
- Web `/jev`：手动评估、选择持仓账户、查询历史和查看 1/3/5 日的后续收盘价变化。默认仅用本地行情，勾选补取后通过现有 `refresh_history_tail` 补取缺失日线并落库，供后续观察使用。
- Chat 辅助评估只读取本地行情；Trace 复用本轮读取的日线，不另行抓取。历史不足或本轮没有日线快照时明确记录不可评估。

接口沿用应用现有认证：

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/jev/status` | 启用状态、密钥是否存在、请求模型、市场与窗口 |
| `POST /api/v1/jev/evaluate` | `stock_code`，可选 `as_of`、`account_id`、`fallback_to_network` |
| `GET /api/v1/jev/evaluations` | 最近记录，可按 `stock_code` 和 `limit` 过滤 |
| `GET /api/v1/jev/evaluations/{id}` | 冻结输入、原问题、答案、模型版本、用量及只读后续观察 |
| `GET /api/v1/agent/trace/sessions/{id}/jev` | 同一 Trace 的复核状态、原动作、分歧对照、证据及后续观察 |

手动接口只接受股票、日期、账户和行情补取选项，不接受客户端提供的价格计划或执行权限。账户模式从现有持仓服务查找持仓，缺少该股票时返回 404。

## Agent Trace 联动

- `trace/run` 和 `trace/stream` 的原返回字段保持兼容，新增 `jev_review`；模型调用在原响应交付后执行。Web 每两秒只读轮询复核状态，切换 Trace 后丢弃旧响应，连续读取失败会显示错误并允许手动刷新。
- 本地历史只缓存轻量复核状态和答案，完整冻结输入与问题从后端按 session 读取，避免证据载荷占满浏览器存储；恢复历史不会触发模型调用。
- Trace 的 `jev.json` 原子更新，保存冻结输入、基准动作和完成结果；`events.ndjson` 追加 `jev_review_ready` / `jev_review_done`。原 `final.md`、价格方案、风控动作和通知不被 Jev 改写。Jev 用量单列，不混入原 Agent 用量。
- 日线捕获仅在启用 Jev 的 Trace 作用域内生效，复制实际读到的最后 90 根；复核以对应价格方案日期或 Trace 日期为界。没有可复用快照就跳过模型调用。原 Judge 的结论不会送入 Jev 的模型输入，保留独立判断。
- 对照优先使用风控 `allowed_action`，缺失时使用成功 Judge 的结构化动作；展示原 Judge 依据、风控限制、共享证据和 Jev 原始选项。Jev 当前只提供选择题答案，界面不生成虚构的模型理由。
- 短线/波段才标记规则明确的分歧，例如条件入场遇到 Jev 回避、高下行风险，或继续持有遇到高风险。中长线、未记录周期、证据不足或历史独立快照仅并列展示。“未发现直接冲突”不表示策略正确，也不授予执行权限。
- 历史 Trace 只关联 `source=agent` 且 `source_ref` 精确等于 session ID 的既有记录；缺失显示“未记录”，GET 不补跑模型。旧记录未证明共享快照时不判定分歧。
- 后台任务是进程内任务，未引入持久化任务队列；待处理记录五分钟后仍未完成会显示中断/超时，不自动重试付费调用。复用 session 时用独立 capture ID 防止旧完成结果覆盖新复核。

## 数据与评估语义

1. 按 XSHG/XHKG/XNYS 交易日历确定最新已收盘日，覆盖时区、假期、夏令时和半日市。日历不可用时不推断工作日。盘中当天未收盘 K 线一律排除。
2. 至少需要 30 根有效日线，最多使用 90 根计算特征，模型接收最近 20 根及已计算指标。数据未覆盖目标交易日、OHLC 非法、非有限数值或重复日期均不可评估。停牌导致最新日线缺失也不会被当作正常行情。
3. 同一轮实时 Agent 的基本面、消息面、资金面工具证据按股票过滤，每维最多两条，每条摘要有长度上限。来源和证据质量保留。历史重放不使用当前 Agent 工具结果，避免混入未来证据；手动与定时扫描目前主要使用量价和持仓信息，缺失维度明确列出。
4. 买点复核只使用已经通过现有 `PricePlanArtifact` 校验、股票和市场匹配、行情日期一致且质量充分的确定性方案。当前价格引擎主要支持 A 股，港美股没有有效方案时仅评估方向与风险，不编造入场价格。
5. 回答的选项、概率范围、概率和、置信度和问题覆盖必须通过校验。原始选项概率与模型置信度分开保存，均不视为已经验证的股票胜率。
6. 评估写入新增 `jev_evaluations` 表，保存输入指纹、问题、版本、来源、用量和错误状态。相同成功输入复用记录；失败尝试独立保留，后续允许恢复。进程内串行锁防止相同输入并发付费调用；跨进程只保证首条成功记录不可覆盖，不保证调用费用完全去重。
7. 历史日期标记 `historical_replay`。历史评估仍可能受模型训练数据影响，不能用作无偏收益证明。前瞻记录保留实际创建时间；日线参考收盘价不等于该时刻可成交价格。
8. 后续观察只按交易所日历查第 1/3/5 个交易日收盘价。GET 不补取网络数据、不改历史评估。缺失行情显示 `missing_data`；发现历史参考收盘价发生变化时显示 `price_basis_changed`。当前指标是收盘价变化，不含分红、手续费、滑点或模拟成交，不能称为净收益或策略回测。

## 验证与后续

确定性测试覆盖 HTTP 契约、限流重试、非法概率、日期边界、三市场输入、影子隔离、持久化和异常恢复。上线后的 1/3/5 日表现需要等待真实时间流逝，分别按市场及评估模式统计。

当前 A 股风险门与港美股交易制度尚未统一，Jev 第一版不进入这些执行权限判断。未来将 Jev 正式用于交易建议前，需要补齐各市场规则、评估概率校准及扣除交易成本的独立验证。

### 本地验证记录（2026-09-20）

- 真实 API 连通及 `600519`、`HK00700`、`AAPL` 三市场请求通过，实际返回 `jev-1.13.0`，首次股票调用约 1.01–1.16 秒；这仅是本次模型请求耗时，不包含行情补取，不是性能或收益承诺。
- 股票日线截至 2026-09-18。首次样本均无匹配价格方案，故只产生方向和风险答案；买点复核通过确定性方案测试验证，尚未完成真实方案在线效果评估。
- 独立 Jev 入口验证：相关后端 63 项、前端单元 9 项、Playwright 桌面/手机 2 项通过，真实页面在 1440px 和 390px 宽度下完成截图与异常检查。
- Trace 联动验证：相关既有后端 112 项、新增 Trace 契约 15 项、前端单元 30 项通过；覆盖原报告先返回、快照隔离、失败保留、跨会话旧响应丢弃及轻量历史恢复。Python 编译、flake8 和 Web lint/build 通过。
- Trace 的桌面/手机 Playwright 2 项通过，覆盖长路径、长工具名及复核状态更新；真实长报告在 1440px 和 390px 下完成截图检查，无页面异常或横向溢出。
- 真实单股 Trace `trace-jev-integration-20260920` 完成：原报告约 192.4 秒返回时复核仍为 `pending`，随后实际 `jev-1.13.0` 调用耗时 1451 ms。复用了本轮 90 根日线，`final.md` 与原返回报告逐字一致，风控仍为 `monitor`；事件顺序为 `jev_review_ready`、`done`、`jev_review_done`。本轮无有效价格方案，买点不可评估，方向及风险复核成功。
- 最新全量后端 gate 为 2719 项通过、4 项失败、1 项跳过。3 项 Debate 测试受本地价格方案触发额外裁决调用影响，1 项筹码测试走了已配置的 Tushare 快速路径；前轮隔离这两个外部依赖后 4 项均通过。本轮仍出现同样 4 项失败，全量 gate 按未通过记录。
- 1/3/5 日观察窗口尚未到期。真实账户的定时扫描、Docker、桌面端打包及线上部署未做端到端验证；持仓影子隔离由确定性测试覆盖。

回滚：设置 `JEV_ENABLED=false` 并重载配置或重启服务，停止新增调用；已保存记录保留。新增表不改变既有表结构或数据。

## English Summary

Jev adds opt-in, shadow-only daily assessments for CN/HK/US equities over 1/3/5 exchange sessions. Python owns calculations, calendars and all price authority. Existing decisions and notifications remain unchanged. Missing plans/data and provider failures remain explicit. Immutable observations support read-only forward price comparisons; they are not executions, net returns or validated trading probabilities. Disable `JEV_ENABLED` to roll back while preserving audit records.

Agent Trace now delivers its original report before the background Jev review. The review reuses captured daily inputs, adds an independent timeline node and compares structured decisions without changing them. Read-only history and polling never trigger inference. Different horizons and unverified legacy snapshots remain side-by-side, without a disagreement claim. Interrupted in-process jobs become explicit after five minutes.
