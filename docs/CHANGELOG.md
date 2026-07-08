# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

> For user-friendly release highlights, see the [GitHub Releases](https://github.com/ZhuLinsen/daily_stock_analysis/releases) page.

## [Unreleased](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.14.2...HEAD)

- [新功能] 新闻消息面新增可选轻量 LLM 事件抽取器，支持 `deepseek/deepseek-v4-flash` JSON 事件事实抽取，失败时自动降级到规则兜底并记录 diagnostics。
- [新功能] 新闻消息面新增 `NewsExtractedEvent` 事件抽取层，重建时保存结构化事件事实，Web“消息”详情展示事件事实、核验状态、置信度和实体链接。
- [修复] 入场执行回测缺失信号有效期时默认 5 个交易日买入窗口，并将默认成交后超时退出从 20 个交易日调整为 30 个交易日。
- [改进] 新闻消息面按主题级 raw episode 入库，并将传导路径升级为事件类型、链路步骤、分数拆解和结论摘要，减少聚合新闻刷屏。
- [改进] 新闻信号边新增质量评分、强弱等级、质量 flags 和语义边 top-k 收敛，Web“消息”详情与 Neo4j 显式关系同步展示边质量诊断。
- [新功能] 新闻消息入库新增规范化正文、质量评分、低质量保留审计和卡片降权门，Web“消息”详情展示来源质量诊断。
- [文档] Graphiti 消息卡片计划记录 2026-07-04 本地 Neo4j/Ollama 验证进度，并明确下一阶段聚焦入库质量和边质量提升。
- [新功能] 新闻信号卡片新增 `news_signal_edges` 真源表、边重建/查询 API、单卡局部图 API，并在 Graphiti 同步时将规则边和 embedding 语义边 best-effort 投影为 Neo4j 显式关系。
- [改进] Web“消息”页详情区新增事件线索面板，选中卡片后展示局部图边类型、权重、目标和建边理由。
- [改进] Graphiti 新闻卡片 episode content 改为分段可读文本，保留摘要、产业、公司影响、传导路径和原始消息引用，避免 Neo4j Browser 展示整块 JSON。
- [修复] Graphiti 同步改用常驻事件循环复用 Neo4j async client，并强化 LiteLLM 结构化输出处理，避免新闻卡片同步后 Neo4j 无节点。
- [修复] 新闻信号原始消息入库按 `episode_id` 与 `dedup_key` 双键幂等合并，避免同一条财联社消息因 `dedup_key` 变化在重建时报唯一键冲突。
- [改进] Web“消息”页将“重建卡片”和“同步图谱”拆成两个按钮，重建默认不等待 Graphiti，并为新闻重建/图谱同步请求配置更长超时。
- [修复] 新闻信号卡片 SQLite 轻量迁移对重复加列竞态做幂等容错，避免打开“消息”页时报 `duplicate column name: signal_layer`。
- [新功能] 新闻信号卡片保存后默认 best-effort 写入 Graphiti episode，并新增 `/api/v1/news-signals/graph-sync` 从关系型真源补同步 pending/failed 卡片。
- [改进] 新闻信号卡片新增 `get_macro_finance_news` 宏观财经源，默认从 orz dailynews 的 `sina_finance`、`eastmoney` 平台过滤非农、逆回购、利率、流动性等宏观消息，并用 `SearchService.search_general_news()` 做关键词 fallback，接入重建 API、Web 重建按钮和主题催化席白名单。
- [改进] 新闻信号卡片新增 `signal_layer=industry/company/macro` 三层分类和筛选，重建链路默认纳入雪球热榜，并补充三类消息工具真实 smoke 验证记录。
- [改进] `get_cls_telegraph_news` 改用 `https://orz.ai/api/v1/dailynews/?platform=cls`，并新增 `get_xueqiu_hot_news` 雪球热榜工具，主题催化席白名单同步开放两个消息面工具。
- [修复] daily run 在周末或休市日会检查最新已完成交易日的数据缺口，缺失时继续补跑对应股票，避免周六无法补齐周五行情。
- [新功能] 新增新闻信号卡片第一版，包含关系型真源表、重建 API、EvidenceCard 适配、反馈 overlay、指标接口和 Web“消息”页面。
- [改进] Web“消息”页反馈按钮会按已有反馈保持高亮并展示次数，避免重复点击后无法确认状态。
- [测试] 新增新闻信号卡片服务单测，覆盖幂等入库、主题公司映射、EvidenceCard 适配和反馈降权读路径。
- [文档] Graphiti 消息卡片计划补充与 EvidenceCard、主题词典、主题催化席和 Seed Pool T+1 评估的复用边界、反馈闭环、调度预算、幂等重建和观测契约。
- [修复] Agent 入场执行回测按 A 股 T+1 规则处理退出，买入当日不再触发止盈、止损或超时卖出。
- [改进] Web 入场执行回测表格新增信号有效期和到期日展示，明确超期未触发的入场信号不成交。
- [修复] Agent 入场执行回测可从 AI 有效期字段或文案提取有效窗口，超过有效期未触发入场时按信号作废处理。
- [改进] Agent 选股链路和单股分析输出表格新增信号/动作有效期，条件型入场有效期由 AI 明确给出，超期未触发需失效或复查。
- [新功能] Agent 新增 `get_cls_telegraph_news` 财联社电报实时新闻工具，先作为独立搜索工具保留，用于后续消息卡片链路补充日内突发消息证据。
- [文档] Graphiti 消息卡片设计文档补充财联社电报第一版接入方案、字段契约、轻量过滤和失败降级策略。
- [修复] `scripts/daily_run.sh` 的 Sequoia 日线步骤按最新已完成交易日写入续跑标记，避免盘中运行更新到上一交易日后阻止收盘后补齐当日行情。
- [修复] 交易日历模块在 `exchange-calendars` 依赖版本错配导致导入异常时改为 fail-open，避免 `main.py` / daily run 入口被交易日检查依赖拖垮。
- [修复] CI checkout 补齐 `graphiti` 子模块映射，并在 backend-gate / docker-build 拉取子模块，避免 `-e ./graphiti` 依赖安装在 GitHub Actions 中失败。
- [文档] 更新 openInvest 原理接入索引，补充新版 openInvest 的信息隔离委员会、事件层、path profile、运行时治理、回测防前视和 PnL/benchmark 自审计等可借鉴原则。
- [修复] `scripts/ci_gate.sh` 默认优先使用仓库 `.venv` Python，并在 flake8 critical check / offline pytest 排除本地外部 clone 与 gitlink 目录，避免系统 Python 或 `openInvest`、`graphiti`、`Sequoia-X`、`alphasift` 干扰 DSA 后端门禁。
- [修复] 系统配置 API schema 同步 `graphiti` 分类与 `float` 数据类型，并修正未显式指定 `ENV_FILE` 时 schedule immediate 配置被默认 `.env` 抢占的问题。
- [改进] Agent planning_execute 的 `planner.json` / `todo.md` 补齐主辅维度、初始假设、工具预期结果、下游使用方式、失败降级、停止条件和 replan 策略，避免 Trace 计划产物只展示重复工具清单。
- [改进] 四席位主题催化席收窄为 AI/科技产业链候选，并要求新闻证据摘要化为产品品类出口、国产替代政策、业务映射和资金验证，避免原文堆砌和非科技行业消息面拖慢链路。
- [改进] Agent 选股候选源对齐新版 AlphaSift / Sequoia-X：AlphaSift YAML 的 scoring/risk profile 会参与分数扣罚，Sequoia 新增定增公告事件策略并以可降级方式接入 seed pool。
- [新功能] Agent Seed Pool 新增主线动量 Theme Regime 分型，基于热点板块、涨停池、热榜和热点龙头证据输出 `theme_momentum`，并给候选 seed 标注 `theme_profile`、`stock_role`、`momentum_setup` 与超买解释。
- [新功能] Agent 单股 `entry_analysis` / `position_review` 新增主线动量画像预取，围绕当前股票生成 `single_stock_theme_profile`，并将高潮、退潮、后排和无关主题显式降级为不可追高。
- [修复] StockAPI 工具入口会加载仓库 `.env` 中的 `STOCKAPI_TOKEN`，并将 Eastmoney patch 的 `fake_useragent` 改为可选依赖，避免续费 token 未被工具进程读取或无关导入错误拖垮 StockAPI 检测。
- [改进] 补齐智谱 GLM `glm-5.2` 渠道示例与前端预设，设置页会显示当前主模型；Agent 调用 `glm-5.2` 时自动携带 thinking 与 `reasoning_effort=max`。
- [改进] Agent 市场状态识别改为方向优先、波动单独约束，高波动上涨趋势不再被误判为纯风险环境；不追高规则改为按回踩、突破、涨停和资金接力 setup 分层处理。
- [修复] Agent 工具批量执行超时时保留已完成工具结果，并为资金流、筹码、Tushare 财务指标和量能等慢工具设置外层等待下限；历史 K 线 loader 在主库缺日线时可读取 Sequoia 本地 `stock_daily`，减少误报工具超时。
- [修复] Seed Pool 质量评估改为根据本地上证指数 OHLC 识别 seed 后的下一交易日，跳过节假日休市，避免误要求同步休市日行情。
- [修复] Seed Pool 质量快照改为优先保存完整 `seed_fact_packets`，不再只保存 `seed_pool_summary.preview` 前 20 条，避免预览外 seed 无法在质量页复盘。
- [修复] Agent Trace 的 Seed Preview 改为显示“预览数 / 完整 seed 数”，并修正四席位 `seed_pool_summary.total_limit` 误写为 seed gate 输出上限导致的 `Seed 32 / 12` 误导。
- [改进] Web“入场”页交互日 K 默认只保留当前策略入场/出场标记，并补充图例说明，减少基准策略紫色点位与当前策略信号混读。
- [改进] Agent 入场执行回测 summary 新增策略级累计 PnL、胜率、成交率、最好/最差收益和盈亏比，Web“入场”页增加系统总览指标面板。
- [改进] Web“入场”页保留当前日期总览，并新增历史全量总览，用于对比当日表现与截至当前的长期平均指标。
- [改进] Agent 入场执行回测导入规则改为只纳入最终组合中的可执行等待/条件入场项，排除明确 reject 项，并支持每个 Trace 最多 4 个最终标的。
- [新功能] Agent 入场执行回测新增 baostock 5 分钟线手动同步、本地 `stock_minute_bars` 缓存表和分钟线优先撮合；Web“入场”页可同步最终报告标的分钟 K，并用 ECharts 交互日 K 展示入场区间、止盈止损和入出场点。
- [修复] 单股 Planner 与四席位专家工具白名单补齐资金水位、北向/两融、单股 Regime 概率、公告披露、StockAPI 热点和 openInvest/综合新闻补证工具，避免工具已注册但模型或席位不可感知。
- [修复] 排障脚本移除固定本地路径和固定 Trace 默认值，`probe_thesis_desks_from_trace.py` 改为显式 `--trace-dir`/`AGENT_TRACE_DIR`，Judge smoke 输出目录改为 `DSA_SMOKE_OUTPUT_DIR` 或系统临时目录。
- [修复] `get_capital_flow` 纳入 Agent 重工具外层等待下限并将默认显式预算提升到 30 秒，避免单股 Trace 中与 Tushare 重工具并发时被 15 秒资金流壳超时截断为 `capital_flow timeout`。
- [修复] 选股最终报告核心表的“可观察标的”不再重复展示已作为“机会首选”或“执行首选”的股票，避免同一标的在首选和观察位产生语义混淆。
- [新功能] Agent Trace 选股页面支持从历史 Trace 继续运行，后端可复用已落盘的选股阶段 artifact，并为未来中断的选股流水线增量保存阶段结果。
- [新功能] 新增 Agent 入场执行回测离线链路、只读/重建 API 与 Web“入场”标签页，只评估选股报告最终输出标的，并用 strict/next-open/ATR 弹性/突破跟随四套策略诊断 AI 入场点位过保守问题。
- [新功能] 新增 `get_symbol_regime_probability` 单股 Regime 概率工具；选股流水线仅对 deep dive 标的和持仓标的计算，单股 `entry_analysis` / `position_review` 会对明确单一标的预取 `symbol_regime_probability` / `reentry_reference` 作为弱证据。
- [改进] `get_regime_forward_probability` 增加 Tushare `index_daily` 本地缓存、非重叠有效样本数、ATR 自适应路径画像、regime 持续天数、样本质量摘要和基于窗口内低点分位的 `reentry_reference`。
- [修复] 选股最终报告的加分条件只有多个备选项时才显示“满足其一即可”，避免单个条件被误读为还有缺失条件。
- [改进] `get_sector_rankings` 优先使用 Tushare `ths_hot(market=行业板块)` 获取同花顺行业板块热榜，保留 Eastmoney 与 StockAPI 作为降级来源，并在 `source_chain` 暴露热榜口径。
- [新功能] 新增 `get_tushare_stk_factor` 股票技术因子工具，接入 Tushare `stk_factor` 的 MACD、KDJ、RSI、BOLL、CCI 等前复权技术指标，并纳入 planning 技术分析能力及结构反转、动量、质量修复、主题催化席工具 YAML。
- [改进] `get_tushare_moneyflow_mkt_dc` 补齐东财大盘资金流向 `start_date/end_date` 参数，并接入 planning `capital_flow` 能力及结构反转、动量、主题催化席工具 YAML，用于大盘资金水位背景判断。
- [改进] `get_tushare_moneyflow_ind_ths` 补齐 Tushare 行业资金流向的 `start_date/end_date` 参数，并同步纳入主题催化席与动量席工具 YAML 白名单，确保行业资金流工具可被候选专家实际调用。
- [新功能] 新增 `get_board_capital_flow` 板块资金统一工具，合成 Tushare `moneyflow_ind_dc`、`moneyflow_ind_ths` 和 `moneyflow_cnt_ths`，统一 CNY 字段并保留 `source_definitions/flow_sources`，避免 DC 行业/概念/地域与 THS 行业/概念口径混读。
- [改进] `get_capital_flow` 合成 Tushare `moneyflow_dc`、`moneyflow_ths` 与 legacy `moneyflow` 三套个股资金流来源，统一输出 CNY 和 `selected_flow_source/flow_sources`，并保留各来源独立统计定义，避免 DC、THS、legacy 口径混读。
- [修复] `get_capital_flow` 单票资金流改为真正 failover：优先 Tushare `moneyflow_dc` 成功即返回，仅失败时才查 THS、legacy moneyflow 和 StockAPI，并将 `ok/partial` 且有有效数据的降级 errors 作为 warning 保留，避免并发 Tushare 工具排队时被 15 秒预算误判失败。
- [改进] `get_capital_flow` 增加后台资金流审计：主链路首源成功即返回，后台 best-effort 比较未选中的 THS/legacy 来源，发现日期、方向或量级冲突时仅写入 `warnings/source_conflicts/capital_flow_audit`，不覆盖顶层资金流字段。
- [修复] Agent 运行态资金流、板块榜单和 Regime 前向概率工具修正预算竞争：`get_capital_flow` 在 Agent 预算内并发探测三路 Tushare 个股资金流并保留 StockAPI fallback 时间，`get_sector_rankings` 不再把后续 fallback 挤成 0 秒，`get_regime_forward_probability` 优先复用指数历史缓存，降低单测通过但实际 Trace 超时失败的概率。
- [修复] `scripts/update_sequoia_candidates.py` 的断点续跑目标改用 A 股最新已完成交易日，避免周末或节假日 daily run 因自然日无行情而反复从头扫描日线缓存。
- [修复] 四席位专家最终 JSON 输出轮启用 DeepSeek/OpenAI-compatible `response_format={"type":"json_object"}` 并提高最终 JSON `max_tokens`，减少 `final_output_not_json`。
- [改进] 四席位开发排障默认提高单 seed 行级等待和 SeedFact 工具超时，降低 `analyze_trend` 等工具未返回导致的席位 timeout。
- [改进] 选股最终报告正文改为中文动作/裁决口径，合并重叠推荐与证据段落，移除字段说明和 Execute 证据摘要，并将入场条件拆成必要条件与加分条件。
- [改进] 四席位单 seed 超时会保留超时前 LLM/tool 进度，Trace 可显示“LLM 已返回工具调用但卡在工具执行”等原因，避免只看到 `timeout`、`工具 0` 或“本席位未输出候选”。
- [修复] Agent LLM fallback 调用不再把单次 timeout 均分给主模型和备选模型，候选专家可用完整剩余预算调用当前模型，降低 DeepSeek 慢响应导致的 30 秒硬超时。
- [修复] 四席位候选发现的 LLM telemetry 支持跨线程写入 Trace，`llm_usage.jsonl` 能记录席位内单 seed LLM 成功、失败和超时，便于排查候选发现降级。
- [修复] Web Trace 页区分四席位 partial 降级与真实 fallback 候选池，避免把已保留可用席位候选误提示为“候选池回退到召回结果”。
- [修复] Seed Pool 质量快照按 `code+desk` 去重落库并优先使用 seed 自身 `freshness/as_of` 归属日期，避免四席位意见重复写入失败后质量页继续显示旧快照；Trace 运行会把真实 `trace_id` 写入快照。
- [改进] 四席位候选发现新增 `negative_conclusion_reasons`，对“未入席/未进入最终候选/席位拒绝/席位超时/未产出结构化结论”等负面结论写明原因，并让报告缺失席位意见时优先展示原因而非只显示未落盘。
- [改进] 四席位并行超时诊断补充总预算、专家预算、实际等待时长和 `overall_timeout_exhausted_before_expert_returned` 原因，`.env.example` 明确开发排障可临时调高 `AGENT_CANDIDATE_EXPERT_TIMEOUT_SECONDS`。
- [新功能] 新增 `search_openinvest_news` Agent 工具，复用 `openInvest/services/news_sources` 的多源新闻适配，默认通过 `yfinance.Ticker.news` 获取 ticker 关联新闻，RSS/DDGS 作为可选源并在 `source_chain` 暴露缺依赖或单源失败原因。
- [改进] Seed Pool 质量页将未进入某席位评估范围显示为“未入席”，不再把缺失占位误写成“未落盘该席位理由”或暗示拒绝意见。
- [修复] 四席位委员会在单个席位超时或失败但其它席位已有候选时改为 partial degraded 输出，保留可用候选和 `partial_errors`，避免因质量修复席失败把动量席/主题催化席候选整体丢弃。
- [改进] 选股 Judge 阶段接入 openInvest 迁移评估中的确定性 sanity check，主动交易裁决在 worker 不可用、无可执行仓位、risk_off/panic/extreme 市场或单票仓位超限时会降级或截断，并在 Trace `judge_decision.full.sanity_checks` 留审计。
- [改进] Agent Trace 接入 openInvest 迁移评估中的 LLM telemetry，阶段 LLM 调用会写入 `llm_usage.jsonl`，记录 `trace_id/stage/agent_role/symbol/provider/model/token/latency/tool_calls/ok/error`，写入失败不阻断主流程。
- [改进] Agent Trace API、历史会话和 Web Trace 页新增 openInvest 可观测性汇总，展示 `llm_telemetry` 的调用次数、token、latency、estimated cost、按阶段统计，以及 `judge_sanity` 的裁决修正规则和 required plan changes。
- [新功能] 接入 openInvest Phase 2 Regime 概率层：新增 `get_regime_forward_probability` 工具和 `src/agent/regime_probability.py`，基于现有 `detect_market_regime` 分类历史切片并输出 forward return、路径画像和 `reentry_reference`；选股链路会把概率摘要挂到 `market_regime.forward_probability`，点位 fallback 输出 `regime_probability` / `reentry_reference` 作为弱证据。
- [改进] 点位计算 prompt、fallback 和最终报告适配 Regime 概率层：`low_confidence` 只能作弱证据，`reentry_reference` 只作为等待回踩或 TRIM 后买回参考，报告会展示 `Regime 概率证据` 便于复盘。
- [新功能] 新增 Agent verdict 后验复盘离线链路：`scripts/build_agent_verdict_reviews.py` 会从 Agent Trace 和本地 `StockDaily` 生成 `data/agent_reviews/verdict_review.jsonl`，选股链路输出 `chain_type=stock_selection`，单股 ReAct 链路输出 `chain_type=single_stock_analysis`，标记 `hit`、`missed_up`、`avoided_down`、`wrong_direction` 或 `insufficient_data`，第一版不自动注入线上决策。
- [新功能] 新增 Agent 后验复盘只读 API 与 Web 页面：`GET /api/v1/agent-verdict-reviews` 可按链路、标签和股票代码筛选 `verdict_review.jsonl`，Web `/agent-verdict-reviews` 展示样本数、完成率、平均后验收益、链路覆盖、标签分布和明细表。
- [改进] Agent 后验复盘页新增样本生成/刷新闭环：`POST /api/v1/agent-verdict-reviews/rebuild` 和 Web “重建样本”按钮会从本地 Trace 与本地 `StockDaily` 重建 `verdict_review.jsonl`，默认扫描最近 300 个 Trace，不重跑 Agent、不拉取外部行情、不自动注入线上决策。
- [新功能] Agent 后验复盘新增离线 insight Markdown：`scripts/build_agent_verdict_insights.py` 会从本地 `verdict_review.jsonl` 聚合稳定样本分组，写入 `data/agent_reviews/insights/agent_verdict_insights.md`，默认至少 20 条 completed 样本才形成洞察，仍不注入线上 Agent、Meta-Agent 或 Judge。
- [文档] 整理 `docs/` 目录结构，将架构、模块说明、方案计划、部署迁移、外部集成和多语言首页分层归档，并新增 `docs/README.md` 作为分类入口。
- [文档] 新增 `docs/integrations/openinvest-integration-assessment.md`，并补充 `docs/modules/regime-state-machine.md` 的 Regime forward probability、路径画像与买回点参考方案，评估 openInvest 投资委员会、后处理、Dreaming 复盘、telemetry 和收益率展示页面等能力在当前仓库的可接入位置与迁移边界。
- [改进] Agent 单股 ReAct 工具结果回灌改为工具级 ETL 事实卡，76 个注册工具均映射到明确 profile；模型只接收业务有效字段、错误状态和摘要，原始长度、hash 与预览留在 Trace，避免长 K 线、公告正文、新闻原文和 source/query 诊断字段污染上下文注意力。
- [改进] Seed Pool 默认移除 `low_base_structure` 低位结构来源，不再调用低位结构扫描；AlphaSift 与 Sequoia 调整为加权主干来源，source cap 分别提升到 14/12，让实际效果更好的两个席位获得更多入池名额。
- [修复] `scripts/update_sequoia_candidates.py` 识别 baostock 长批量更新中的“用户未登录”会话失效，自动重新登录并重试当前标的，避免日线刷新中途掉线后连续污染后续股票为失败。
- [修复] Agent 单股 `fundamental_analysis` 不再只依赖易超时的 `get_stock_info` 聚合工具，Planner 默认追加 `get_tushare_daily_basic`、`get_tushare_financial_indicators` 和 `get_tushare_financial_statements` 做估值与财报兜底验证。
- [修复] Agent Trace 单股问题在注入组合上下文时固定以用户明确股票代码作为 `primary_symbol/target_symbols`，避免被已有持仓标的污染为错误的持仓复盘。
- [修复] Agent 最终报告新增事实校验门禁，缺少工具来源的资金流、筹码分布和均线精确值会被替换为 `N/A`，并拦截 A 股非法零散买入股数建议。
- [修复] `get_capital_flow` 区分 Tushare `moneyflow` 全口径净流入与大单/特大单主力净流入，避免把 `net_mf_amount` 误标为主力资金。
- [改进] `get_capital_flow` 的工具描述、模型压缩上下文和 Agent 提示词显式保留资金口径定义，防止模型把 `net_inflow*` 解读为主力资金。
- [改进] Agent 新增高风险指标语义注册表，只向模型压缩上下文注入资金流、筹码分布等易误读字段的短说明，避免所有工具全量解释挤占上下文。
- [修复] planning_execute 最终报告生成前会审计 Planner 核心工具是否已执行，防止 `calculate_ma`、`analyze_pattern` 等计划内技术工具未调用时提前收尾。
- [改进] `analyze_trend` 单股技术工具新增布林带中轨/上下轨/带宽/位置输出，并让均线、量价和 K 线形态工具进入证据卡适配。
- [改进] `scripts/daily_run.sh` 的 Sequoia 日线更新步骤同步写入上证指数 `000001.SH`，为 Seed Pool 质量评估的基准 Alpha 提供本地 OHLC。
- [文档] 新增 `docs/plans/seed-pool-quality-monitor-plan.md`，规划按日期长期记录 seed pool 快照、次日收盘表现评估、四席位支持/拒绝理由追溯和前端质量监控页。
- [文档] 完善 Seed Pool 质量监控计划，将上证指数 Alpha、MFE/MAE、一字涨停不可买入过滤、催化剂字段、K 线 price lines 和归因分析面板纳入第一版验收范围。
- [新功能] 新增 Seed Pool 质量监控 API 与 Web 页面，可按日期查看 seed 次日 Alpha、MFE/MAE、流动性状态、来源/席位/Catalyst 归因，并用 ECharts 展示 K 线、seed close 和 T+1 标记。
- [改进] Seed Pool 质量页改为固定四席位理由矩阵展示，缺失席位也显示 `missing`；K 线参考线不再从四席位自然语言理由中正则抽取，避免把席位判断误读为交易点位。
- [改进] Seed Pool 质量页展示快照生成时间、T+1 评估更新时间和缺价/未评估数量，并将评估按钮明确为“手动更新 T+1”。
- [改进] 四席位候选发现将动量席升级为“趋势/形态延续席”并提高趋势市默认配额；`early_turn_desk` 业务语义降级为“结构反转席”，低位必须叠加明确转强证据才参与，防守 regime 会跳过零配额动量席以减少无效 LLM 超时。
- [修复] Agent Runner 对 `discover_watchlist_candidates` 和 `detect_market_regime` 采用重工具外层等待预算，避免内部已有结构化诊断的候选发现/市场状态工具被统一 30 秒壳超时截断。
- [修复] Agent Trace 在注入持仓账户上下文时，明确“选股/下周可入手股票/候选池”请求会覆盖组合上下文默认的 `position_review`，确保进入 `watchlist_scan` 五阶段候选池 + 四席位链路。
- [修复] 修复 Agent Trace 跳转 Seed Pool 质量页时 `YYYYMMDD` 日期参数未规范化导致页面请求失败并显示空状态的问题。
- [修复] 修复 Seed Pool 质量页直接渲染四席位 `risks` 对象导致 React 运行时崩溃、整页空白的问题。
- [改进] Seed Pool 质量页手动更新 T+1 前改为先检查评估日是否应有行情，并优先使用本地数据库；缺少指数或 Seed 股票 OHLC 时返回结构化错误，不再静默写入空评估。
- [修复] Seed Pool 质量 API 对上游只给代码或占位名称的 seed 自动从股票索引补齐中文名，避免页面显示 `000050 / 000050`。
- [修复] Seed Pool 质量评估和 K 线复盘在主库缺少 OHLC 时读取 Sequoia `stock_daily(symbol, date, open, high, low, close, volume, turnover)`；上证基准仅使用 `000001.SH`，避免误用平安银行 `000001`。
- [修复] Seed Pool 质量页将选择日期同步到 URL；周末 seed 的 K 线窗口按最近交易日锚定并保证包含 T+1，主库只有部分行情时会合并 Sequoia 历史 K 线，K 线 hover 精简为日期和开收高低。
- [修复] Seed Pool 质量评估改为 Sequoia K 线窗口优先，避免主库 Tushare 原始价与 Sequoia 复权/策略价混用导致 Alpha 错算；手动更新 T+1 会刷新当天全部 seed，质量页不再展示 MFE/MAE 买卖点式口径。
- [修复] Seed Pool 质量快照幂等键加入 `seed_date`，并为默认 `selection-run` 快照 ID 增加日期后缀，避免新一天 seed pool 覆盖前一天快照但页面仍显示旧日期。
- [修复] Seed Pool 质量页改为 A 股红涨绿跌显示，并将同一 `seed_date` 的候选池保存语义改为最新池替换旧池，T+1 评估只针对当天最新池。
- [改进] Seed Pool 质量快照的默认归属日改为北京时间 09:00 前归前一自然日，避免次日开盘前生成的候选池误计入新交易日。
- [修复] `start_all.sh` 后端启动改用 `uvicorn server:app`，避免本地脚本报告 ready 后 `main.py --serve-only` 后台进程退出导致 8000 不可访问。
- [新功能] 新增 `get_stock_disclosure_events` 底层工具，通过巨潮公开公告检索公司年报、投资者关系记录和公告标题，结构化返回文档类型、URL、命中词、source_chain 与失败诊断，为主题催化席后续做“行业主题 × 公司公开资料”匹配提供基础证据。
- [新功能] 新增 `search_stock_prompt_intel` 单股用户问题检索工具，将股票代码/名称与用户原始 prompt 合成搜索查询，复用现有搜索引擎返回公告、消息、走势背景等结构化结果，支持 Agent 在单股问答中按用户问题主动查证。
- [新功能] 持仓管理新增单账户“重设持仓基准”能力，可清空旧流水并按指定日期、本金/现金余额、持仓数量和成本价重建基准流水，方便补录或校准未及时更新的账户持仓。
- [改进] 持仓基准重设从自由文本解析改为逐行字段录入，分别填写代码、数量、成本价、市场和币种，降低补录持仓时的格式误填风险。
- [修复] 修复 DatabaseManager 半初始化单例被复用时导致持仓流水列表请求失败的问题。
- [改进] 四席位 seed pool 默认上限从 20 扩到 32，提升 AlphaSift、Sequoia、资金、主题等多来源召回覆盖；逐股深挖上限仍保持默认 4。
- [新功能] 四席位 seed pool 新增 `news_theme_daily` 盘前日报主题来源，接入东方财富财经早餐 `stock_info_cjzc_em`，按 `trade_date` 匹配当天 6 点日报，并通过本地概念字典、可选 `jieba` 分词和规则引擎映射主题成分股；日报直接点名公司仅做诊断与避雷，不直接生成 seed。
- [文档] 重写根 README 为专业化项目首页，突出账户感知 AI 投资研究、四席位选股、Meta 约束、点位计算、组合配置、Judge 和 Trace 复盘；新增 `docs/architecture/stock-selection-pipeline.md` 专题文档说明选股链路输入输出、机会首选/执行首选双轴语义和 Trace artifact。
- [文档] 在 README 与 `docs/architecture/stock-selection-pipeline.md` 的 L1 seed pool 说明中显式列出 `AlphaSift` 与 `Sequoia` 本地主干候选源，避免把两者泛化成“本地价量与形态”。
- [新功能] 四席位委员会新增 `theme_catalyst_desk` 主题催化席，专门判断日报/当日主题是否与个股业务归属匹配、是否已有板块或资金验证，并输出 `theme_catalyst` setup；聚合默认名额表同步给主题席保留 2 个常规名额、事件驱动 regime 保留 4 个名额。
- [修复] `news_theme_daily` 不再只读取东方财富财经早餐列表摘要；工具会根据文章链接抓取东财原文 `ContentBody`，按 `每日精选/热点题材/公司新闻` 分区抽取主题并在 Trace 暴露 `article_fetch_status/article_sections/evidence_section`，避免摘要漏掉 MLCC 等正文主题。
- [改进] `news_theme_daily` 主题评分新增高影响产业催化词加权，命中 `英伟达/NVIDIA/黄仁勋/CUDA/Blackwell` 等词的非公司新闻主题会提高 `theme_score` 并在 Trace 暴露 `high_impact_terms`；公司新闻否认/澄清仍只做诊断不产 seed。
- [改进] 扩充 `news_theme_daily` 本地概念映射字典，覆盖 AI服务器、GPU算力芯片、先进封装、PCB、CPO光通信、AI玩具、低空经济、数据中心、钠离子电池、创新药、航运、房地产等常见财经早餐主题，便于原文主题召回后的成分股映射查询。
- [改进] 四席位 seed pool 移除 `event_impact`、`news_momentum`、`fundamental_snapshot` 三个低精度主召回来源，并将 `news_theme_daily` source cap 从 6 提高到 8，给高质量盘前消息池多 2 个候选名额。
- [改进] `news_theme_daily` 本地概念映射补充存储领域、光模块和光连接主题，覆盖 `存储领域/存储产业链/光器件/高速光模块/光互连/光引擎/光I/O` 等别名及对应宽口径成分股。
- [改进] `news_theme_daily` 日期路由改为每日 06:00 前自动使用上一自然日财经早餐、06:00 后使用当日财经早餐，并在 Trace 暴露 `requested_target_date`、`target_date`、`target_date_rule`。
- [修复] Agent 主分析工具批次改为正常宽度真正并行执行，并为 `get_stock_info`、`get_market_capital_flow`、`search_comprehensive_intel` 增加工具内硬超时/快搜边界，避免 Step 1 大批量工具排队后被统一 30s 标记为 `Tool execution timed out`。
- [改进] Agent Trace 工具诊断细化 `score_stock_news_sentiment`、`get_tushare_announcements`、`get_northbound_capital_flow` 的空结果与部分数据语义：公告/新闻暴露查询窗口和状态，北向资金暴露 `data_quality` 与无核心数值字段 warning，避免把 `empty`、`partial-null` 和 `error` 混读。
- [修复] `get_sector_rankings` 改为快路径板块工具，优先东方财富行业板块实时行情 `stock_board_industry_name_em` 背后的 boardlist 数据接口，StockAPI `/v1/hotBkJlrDr` 仅作可用时兜底；权限/额度/网络失败时直接返回结构化 `failed/timeout`，不再进入 Tushare/AkShare/Efinance 慢速轮询导致外层 30 秒超时。
- [改进] Agent Trace 页历史记录由横向 pill 列表改为竖向列表，历史问题摘要支持多行显示，避免横向滚动时内容显示不完整。
- [修复] 选股最终报告核心结论表改为“机会首选/执行首选”双轴展示：机会首选不因账户过于谨慎而被抹掉，执行首选才受账户、市场和风控约束；未深挖候选仅进入观察池，Meta-Agent 链路标题改为“链路对齐（非推荐排序）”，避免表头与后续章节像两套独立排序。
- [新功能] Agent 新增 `get_stock_business_context` 轻量业务归属工具，只返回名称、行业、板块、业务线索摘要、来源与日期；四席位 `SeedFactPacket` 默认改用该工具补齐第一层业务上下文，`get_stock_info` 保留给深层基本面。
- [改进] 四席位 `SeedFactPacket` 新增宽口径 `business_context`，从所属板块、FactSheet 和召回特征中合并宽行业、原始板块名与主题线索，避免高位事件票只因价量过热而丢失业务催化证据。
- [改进] Web 全局字体切换为优先使用 `Noto Sans SC` / 思源黑体，并移除 Agent Trace 页面的衬线字体覆盖，提升中文页面清晰度。
- [改进] 四席位 seed pool 移除当前权限不可用的 `northbound_stock_connect/hsgt_top10` 主来源，`hot_rank` 改用 StockAPI `renQi` 人气榜，并用 StockAPI `hotBkJlrDr/hotBkJlrLongTou` 补充 `sector_theme` 板块来源。
- [修复] 四席位 seed pool 最终合并改为除 `user_watchlist` 外按可用 source 总数平均分配名额，并将不足来源的空余名额再分配；Trace 同步拆出 `sector_theme` 在线补充 bucket 的诊断，避免成功返回的来源被前三类挤出或被汇总入口遮蔽。
- [修复] 四席位 LLM 首轮输入改用压缩版 `SeedFactPacket`，保留结构、支撑压力、趋势、均线、量能、资金、板块和基本面等决策事实，同时让 LLM fallback 共享超时预算，避免单个慢模型吃完整个 60s 后导致席位连续失败熔断；Trace probe 会回挂落盘的 `seed_facts.json`，用于真实复跑排障。
- [新功能] Agent 新增 Tushare `get_tushare_today_news` 当日新闻快讯工具，固定查询今天 `00:00:00` 到当前时刻的 `news` 数据，并复用 `TUSHARE_TOKEN`/`TUSHARE_HTTP_URL` 环境配置。
- [新功能] 选股流水线接入 `meta_orchestrator` 与 `pricing_agent` 两个真实阶段：四席位深挖后生成资产定性、硬约束与必算场景包，再由点位计算层输出 If-Then 条件单矩阵并传递给组合配置、反方审查、Judge 和最终报告。
- [改进] 选股 `final.md` 改为面向 Meta-Agent 链路展示：新增“四席位 → Meta 约束包 → 点位计算 If-Then 条件单”专章，显式解释 Meta 字段语义、硬约束、必算场景、入场区间、止盈止损和缺失证据；深挖/报告标的数量收敛为 1-5 只。
- [文档] 明确 Meta-Agent 后续运行顺序：Meta → 点位计算 → 组合规划 → 反方审查 → Judge 为 5 次串行 LLM stage 调用，`final.md` 只读取已落盘 JSON 做确定性渲染。
- [改进] 选股 Meta-Agent 后续阶段新增 prompt 控长保护：下游只接收最多 5 只股票、每只最多 3 个必算场景的压缩约束包和条件单矩阵，并按可执行性稳定排序，避免完整 Meta/深挖结果撑爆模型上下文。
- [改进] 暂停四席位主链路主动使用 `moneyflow_ths`：种子池资金异常源和动量席工具白名单不再调用 THS 个股资金流；单票资金验证继续走 `get_capital_flow`，由 Tushare `moneyflow` 失败后回退 StockAPI `codeFlow`。
- [改进] StockAPI 历史资金流 fallback 支持 `STOCKAPI_URL` 覆盖 codeFlow 地址，默认仍为 `https://www.stockapi.com.cn/v1/base/codeFlow`。
- [改进] L1 四席位种子池新增 `AGENT_SEED_POOL_TOTAL_LIMIT` 上限配置，默认 20 保持原行为；单票真实 smoke 或排障时可临时设为 1-3，避免候选事实包补数拖慢 Meta/点位计算闭环验证。
- [修复] 四席位种子池恢复 `user_watchlist` 最高优先级，显式传入的 `target_symbols` 会先进入 seed pool；当 `AGENT_SEED_POOL_TOTAL_LIMIT` 已被用户 seed 填满时不再继续拉在线候选源，保证单票排障稳定命中指定股票。
- [修复] 四席位最终 JSON 解析兼容真实模型输出的短前后缀文本，自动提取首尾大括号中的 JSON object；纯自然语言仍按 `final_output_not_json` 失败处理。
- [文档] 补充选股链路 Meta-Agent / Orchestrator 架构：明确四席位报告如何被整理成资产定性、市场环境过滤、硬约束与必算场景包，并定义点位计算层只做 If-Then 条件单计算的职责边界。
- [改进] 四席位选股链路新增 `SeedFactPacket` 前置并行取数层，按 `(seed,tool)` 构建共享 facts JSON，并把压缩后的模型输入版 `seed_facts.json` 写入 Trace 便于定位取数慢、缺失或失败，避免原始工具数据撑爆后续模型上下文。
- [改进] `detect_market_regime` 准确性优先：上调市场环境辅助组件默认预算，`AGENT_REGIME_COMPONENT_TIMEOUT_SECONDS` 从 `8.0` 调至 `25.0`、`AGENT_SECTOR_RANKINGS_TIMEOUT_SECONDS` 从 `3.0` 调至 `10.0`、`AGENT_TUSHARE_TOOL_TIMEOUT_SECONDS` 从 `5.0` 调至 `20.0`；指数历史快路径和北向/两融/市场资金/板块排行均使用完整组件预算，降低短超时导致辅助输入缺失的概率。
- [文档] 补充选股链路重构方案的深入探究层设计：将选股候选深挖从通用 `planning_prompts.py`/个股分析 prompt 中拆出，明确最多 3 只、最少 1 只的深挖目标选择、默认 3 次单股 prompt 调用、输入 payload、输出 schema 与报告消费规则。
- [修复] 放宽 `get_stock_info` 默认超时预算：基本面阶段总预算从 1.5s 调整为 8s，单源 fetch 从 0.8s 调整为 3s，所属板块补充从 1s 调整为 3s，降低 AkShare/efinance 慢响应导致的 `partial` 与板块缺失。
- [修复] 四席位最终输出收紧为 JSON contract + Few-shot JSON 示例，并在席位 LLM 调用中启用 `response_format={"type":"json_object"}`，减少工具调用后回灌阶段输出自然语言导致的 `final_output_not_json`。
- [文档] 补充 `docs/architecture/选股链路重构-实施方案.md` 的端到端链路总览，明确种子池多来源、三打法席位输出、聚合层和后续 screening/deep dive/judge 的传递关系。
- [修复] 四席位单 seed LLM 默认超时从 20s 调整为 60s，并将外层 per-seed guard 上限提高到 180s、整体 committee 预算按「首轮 LLM + 工具 + 后续 LLM」同步放宽；实测工具本身多为 1-8s，主要耗时来自工具结果回灌后的 LLM 二/三轮。
- [修复] 四席位候选发现的 LLM 调用增加 adapter 级硬超时并关闭限时调用内的 LiteLLM 重试拖延；LLM provider 超时/错误会作为逐 seed `failed` 暴露并触发连续失败熔断，不再被外层 seed guard 提前吞成无原因 `timeout`。
- [chore] 新增 `scripts/probe_thesis_desks_from_trace.py`，可从保存的 Agent Trace 复用 seed preview 直接驱动四席位，隔离验证 seed/recall/desk loop、工具 schema 和真实 LLM 首轮调用耗时。
- [修复] 四席位候选发现取消 seed pool 失败兜底：seed pool 只作为输入与诊断，四席位未产出 L1 候选时 `candidate_discovery.status=failed`、`candidates=[]`，选股流水线直接终止并在 Trace/报告错误中暴露席位与逐 seed 失败原因，不再继续 screening/deep dive。
- [修复] seed pool 入池后不再对外展示跨来源统一分数：`priority_score` 降级为 `source_diagnostics`，Agent Trace 与候选池页只展示来源证据/初筛分，避免把 AlphaSift、新闻主题等不同来源的 90 分直接比较。
- [修复] 四席位逐 seed 执行增加 seed 级 wall-clock 保护与连续 timeout 熔断：单只 seed 超时会保存为 `per_seed_packets[]`，席位返回 `partial` 而不是等整席位 400s 后被外层合成空 timeout；Trace 页面同步展示每个席位下逐 seed 的状态、耗时、工具数和错误。
- [修复] 四席位候选发现改为逐 seed 调用 LLM 并逐票保存 `per_seed_packets`，同时显式把 `AGENT_CANDIDATE_EXPERT_TIMEOUT_SECONDS` 传给每次 LLM 工具调用，避免 LiteLLM 默认 `6000s` 导致席位线程晚到、Trace 先报 timeout，并透传 `thesis_desk_packets` 供前端查看四席位报告。
- [改进] 选股候选发现当前调试阶段收敛到 `thesis_desk_committee` 四席位链路：后端默认模式改为四席位，前端 Trace 只暴露四席位选项；若 API/旧配置显式传 `deterministic` 或 `llm_expert_committee`，流水线会在候选发现前直接退出并写入 `candidate_discovery.status=skipped`，避免误跑旧链路干扰四席位排障。
- [修复] 选股打法席位委员会：修复 LLM 适配器 `_convert_messages` 只识别扁平 `tool_calls` 结构、遇到候选专家委员会的嵌套 OpenAI 结构（`{"function": {"name", "arguments"}}`）时抛 `KeyError('name')` 的问题，曾导致三个打法席位（低位启动/动量/质量修复）在多轮工具调用后全部 failed/timeout、候选池静默回退到 60 只召回结果。
- [改进] 打法席位聚合诊断（`thesis_desk_diagnostics`）补充每个席位的真实 `errors`/`warnings`，降级时不再只记 `status` 而丢失失败原因，避免靠盲目重跑定位问题。
- [改进] 统一后端入口日志落盘：`server.py` 和 `webui.py` 改为复用项目日志配置并关闭 uvicorn 默认日志配置，避免 uvicorn / FastAPI 日志只停留在终端，方便后续排查时直接查看 `logs/*.log`。
- [修复] 选股链路重连候选池与最终报告：候选证据包改为对席位已收敛的「候选入池榜」逐只建证据（与候选池、筛选、深挖共用同一份名单），`balanced_candidate_evidence.summary.selection_mode` 标注走 `canonical_desk_shortlist` 还是 `balanced_buckets_fallback`；席位未打标签（降级回退原始召回）时才退回四类来源分桶选取，避免报告深挖标的与候选入池榜对不上。
- [修复] 深挖目标 provenance 透明化：当 `candidate_screening` 放行不足、深挖按候选池顺序兜底补足时，对应标的标注 `deep_dive_provenance=pool_fallback`（报告写明「进入深挖：筛选未通过，按候选池顺序兜底」），避免读者误判为已通过筛选。
- [改进] Agent Trace 候选入池榜新增席位可见性：候选卡片展示 `primary_desk`/`setup_type`/`stance`/多席共振/冲突标签，并新增席位委员会状态横幅读取 `candidate_discovery.thesis_desk_committee` 的 `status`/`degraded`/`dimensions_covered`/`error`，降级或报错时高亮告警。
- [修复] 席位聚合崩溃可见化与加固：`committee.py` 聚合/召回/LLM coerce 各 except 捕获并落 `traceback` 诊断字段（不再只存 `str(exc)`），`aggregator.py` 对所有 set/dict-key（stance/code/tool/setup_type）加 `str()` 强转防 `unhashable type: 'dict'` 静默吞噬导致席位收敛失败回退至原始召回池。
- [改进] `committee.py` 席位→候选 dict 序列化输出真实 `stance`（取代硬编码 `support`），`_fallback_candidate_discovery` 透传顶层 `degraded`/`dimensions_covered`，并在 desk 子诊断报降级时置顶层 `candidate_discovery.degraded`。

- [修复] 修复 `llm_expert_committee` 委托 P4 席位链路时 `build_recall_pool(prebuilt_pool=...)` 参数不匹配导致 thesis desk 在召回阶段直接失败的问题；当 thesis desk 空输出时不再保留 L1 seed fallback 候选，只在 Trace 候选发现产物中保留 thesis/recall 诊断字段。
- [新功能] 选股链路重构 P4：新增三打法席位（低位启动/动量/质量修复）及聚合层。`BaseDeskExpert`（`experts/desk_base.py`）接受 `List[FeatureRow]` + `regime`，席位内部按 OR 兜底做 eligibility 过滤（low_base/bullish_trend/fundamental flag）防空跑；`EarlyTurnDeskExpert`/`MomentumDeskExpert`/`QualityRepairDeskExpert` 各自覆盖白名单工具与 eligibility 规则。
- [新功能] 选股链路重构 P4 聚合层：新增 `aggregator.py`，`aggregate_desk_picks()` 按工具覆盖率确定性计算 `AggregatedCandidate.confidence`（不读 LLM 输出的 score/confidence），打标 4 类冲突 flag；`allocate_slots()` 按 `MarketRegime` 分配各席位名额，空额按定向回填表补充，防守市（risk_off/panic/trending_down）严格禁止回填动量席。
- [新功能] 选股链路重构 P4 入口：`committee.py` 新增 `run_thesis_desk_committee()`，调用链：`build_recall_pool → [3 desks parallel] → aggregate_desk_picks → allocate_slots → payload`，`candidate_source="thesis_desk_committee"`，与 `run_committee_discovery` payload 形状兼容。
- [新功能] 新增 9 项 P4 配置项：`DESK_SLOT_ALLOCATION_JSON`/`DESK_BACKFILL_RULES_JSON`/`DESK_PICK_TOP_N`/`SELECTION_TOTAL_SLOTS`/`DESK_BACKFILL_MAX`/`LOW_BASE_RANGE_PCT_MAX`/`DESK_MAX_LLM_ROUNDS`/`DESK_MAX_TOOL_CALLS`/`THESIS_COMMITTEE_TIMEOUT_S`，均配有占位默认值，不配置可正常运行。
- [新功能] 新增三个席位的工具白名单 YAML（`tools_manifest/early_turn_desk.yaml`/`momentum_desk.yaml`/`quality_repair_desk.yaml`）及对应 system prompt（`prompts/early_turn_desk.py`/`momentum_desk.py`/`quality_repair_desk.py`/`_desk_base.py`）。
- [改进] API `AgentTraceRunRequest.candidate_discovery_mode` 新增 `thesis_desk_committee` 枚举值；前端 `AgentTracePage` 下拉选项同步新增"打法席位委员会 (P4)"。
- [chore] 选股链路重构 P5：删除旧 `CapitalFlowExpert`/`EarlyTurnExpert` 专家实现（`experts/capital_flow.py`、`experts/early_turn.py`、`prompts/capital.py`、`prompts/early_turn.py`、`tools_manifest/capital.yaml`、`tools_manifest/early_turn.yaml`）；`run_committee_discovery`（llm_expert_committee 模式）改为透传委托至 `run_thesis_desk_committee`，保留 payload 形状向后兼容；删除失效的 `_run_seed_gate`/`_committee_candidate_score`/`_sort_committee_candidates`/`_merge_*_evidence`/`_packet_summary`/`_packet_dimension` 内部函数；`_registry_lookup` 从 `capital_flow.py` 迁移至 `experts/base.py`，所有席位专家统一从 `base` 导入。
- [测试] P5 同步更新 `tests/test_tools_manifest.py`（改用 `early_turn_desk` manifest 验证 YAML/validate/renderer/prompt 注入）和 `tests/test_candidate_experts_v2.py`（移除已删除的 CapitalFlowExpert/EarlyTurnExpert 专项用例，保留 BaseExpert/cache/runtime 通用覆盖）。


- [新功能] 选股链路重构 P3：新增召回层 `src/agent/candidate_experts_v2/recall.py`，策略→特征 — 每只票产出 `FeatureRow`（`FeatureFlag` 列表 + FactSheet Phase A），按探测器命中数粗筛截断，**不排序、不打全局优先分**；同步在 `schemas.py` 新增 `FeatureFlag`/`FeatureRow`/`RecallResult` 数据结构。
- [新功能] 新增 `AGENT_RECALL_COARSE_CAP`（默认 `120`，召回粗筛安全阀，并列全保）、`AGENT_DESK_FALLBACK_SUPPLEMENT_N`（默认 `10`，席位子集为空时的补充上限）。
- [测试] 新增 `tests/test_recall_layer.py`（30 个单测），覆盖 FeatureFlag/FeatureRow schema、源→detector 映射、多源合并、并列边界全保 coarse cap、FactSheet 挂载、召回层不产出全局 priority_score 的 P3 核心不变量。
- [新功能] 选股链路重构 P2：新增确定性 `FactSheet` 事实底表（`src/agent/candidate_experts_v2/fact_sheet.py`），每票算一次，Phase A 从本地日线推导位置分位/趋势/量比/RSI/乖离/流动性，Phase B 由调用方透传资金方向与板块上下文（缺失保持 `unknown`，不阻塞）；新增看空红线否决门 `veto_gate.apply_veto`（`src/agent/candidate_experts_v2/veto_gate.py`），非对称共识只否决不加分，默认仅 `hard_risk_flags` 非空触发。
- [新功能] 新增 `AGENT_VETO_GATE_ENABLED`（默认 `true`，回滚开关）、`AGENT_VETO_VIOLENT_OUTFLOW_THRESHOLD`、`AGENT_VETO_BREAKDOWN_ACCEL_THRESHOLD`（均留空=禁用，永不触发）：软红线 `capital_violent_outflow`/`breakdown_accelerating` 阈值留空时保持 `False`，避免误杀资金未进的干净低位票。
- [测试] 新增 `tests/test_fact_sheet.py` 与 `tests/test_veto_gate.py`，覆盖 FactSheet 位置/趋势/量比/RSI/流动性字段确定性可复现、红线阈值留空不触发（防误杀回归）、`intraday_bucket` 跳过午休的缓存分桶；veto 门 hard_risk 必否决、软红线 bool 否决、低位票保留、gate 关闭全放行。
- [新功能] 选股链路重构 P1：单股深入探究按 `setup_type` 路由 5 套打法手册（低位启动/强势延续/资金连板/质量修复/题材补涨），`setup_subtype=theme_follow` 优先于 `setup_type`，未识别落保守通用模板；每套手册解耦 `failure_condition`（论点证伪，可非价格）与 `stop_loss`（价格风控线），并按 `market` 注入 A股/港股/美股专属口径与"已有证据只补缺口"复用提示。深挖输出 schema 保持不变，下游 allocation/adversarial/judge 零改动。
- [新功能] 新增 `AGENT_DEEP_DIVE_SETUP_ROUTER_ENABLED`（默认 `true`）作为 P1 回滚开关：关闭时深挖回退到旧版单一 prompt（逐字不变）。上游未产出 `setup_type` 时由调用方按召回 source/策略标签做过渡期推断，并透传 `fact_sheet`/`conflict_flags`/上游证据。
- [测试] 新增 `tests/test_deep_dive_setup_router.py`，覆盖 `deep_dive_router` 子类型优先级与未知兜底、各 setup 注入对应 playbook、flag-off 回退 legacy、输出 schema 不变、`failure_condition`≠`stop_loss`、`_infer_setup_type` 过渡期推断与 `_deep_dive_setup_fields` 可选上下文透传。
- [修复] `scripts/update_sequoia_candidates.py` 默认支持断点续跑：逐股票落库后重启会跳过已达到最新本地日期的股票，并在连续网络失败时提前停止且跳过裁剪，避免 5000 只股票全量刷新断网后从 0 重跑。
- [修复] Agent Trace 与候选池页面将“候选决策榜/评分”改为“候选入池榜/入池优先级”，默认区分 L1 召回优先级与 `candidate_screening` 初筛分，并压缩本地价量种子池高分段，避免大量 `100` 被误读为买入评分。
- [修复] `detect_market_regime` 的 `000300` 指数历史改为无全局 Tushare 锁的 HTTP 快路径，并在指数快路径失败时只查本地缓存、不再落入股票数据源轮询，避免并发选股时市场状态被误判为历史 K 线超时。
- [修复] Agent 选股单股深挖目标改为优先使用 `candidate_screening` 的 deep_dive / monitor 结果，并排除已标记 `reject` 的候选，避免弱势或不匹配标的仅因候选池排序靠前进入正文“等待确认”。
- [修复] Tushare 私有网关代理绕过逻辑从固定旧 IP 扩展为所有数字 IP 地址，避免切换 `TUSHARE_HTTP_URL` 后请求仍被本机代理链路拖慢或超时。
- [新功能] `llm_expert_committee` 种子池新增北向/互联互通、融资融券和大宗交易三类独立资金 seed source：补齐 Tushare `moneyflow_hsgt`、`moneyflow_mkt_dc`、`hsgt_top10`、`margin_detail`、`block_trade` Agent 工具，并将 `northbound_stock_connect`、`margin_financing`、`block_trade` 纳入 source cap、诊断和资金面专家工具手册。
- [改进] `llm_expert_committee` 共享种子池重做为确定性本地扫描为主、在线来源补充的闭环：新增 `local_price_volume` 全市场价量异常源、板块/消息/事件补充源、结构化 `trigger_signals`、来源质量诊断、硬排除摘要和 source cap 汇总，并在专家前接入无工具 LLM 门卫做噪音过滤；门卫或单一信息源失败时保留确定性种子池继续并行专家分析。
- [改进] `llm_expert_committee` 共享种子池新增结构化运行日志：逐来源输出状态/数量/错误，最终输出 source cap 后的种子预览、硬排除摘要、LLM 门卫输入输出和专家合并 top codes，便于定位信息源缺失、门卫误杀和空候选问题。
- [改进] Agent Trace 为 `llm_expert_committee` 新增 `selection_seed_pool_built` 与 `selection_seed_gate_done` 进度事件，并即时落盘 `seed_pool.json` / `seed_gate.json`；最终 `candidate_discovery.json` 同步保留 seed pool 和 LLM 门卫字段，确保排查时能先看到种子池再看到后续工具调用。
- [改进] `llm_expert_committee` 种子池按《种子池设计》补齐本地全市场硬排除和多维异常探测：`local_price_volume` 基于本地 `stock_daily` 先过滤新股/停牌或无交易/低流动性/连续一字板，再按 OR 逻辑输出价量、突破、均线、缩量蓄势、缺口和低位转强信号；同时接入资金异动、龙虎榜、日度估值流动性补充源，并让 LLM 门卫仅在 seed 超阈值时启用。
- [新功能] `llm_expert_committee` 新增低位启动专家 `early_turn_expert`，接入 `get_realtime_quote`、`analyze_trend`、`calculate_ma`、`get_volume_analysis`、`analyze_price_structure`、`get_capital_flow`、`get_stock_info` 白名单；committee 共享种子池同步补充 `fundamental_snapshot` 与 `low_base_structure` 低位来源，避免低位专家成为仅在强势样本上运行的空壳接入。
- [修复] Agent Trace 在 `watchlist_scan` 无唯一目标股票时不再错误复用持仓股和最后一条实时行情生成单股票 `risk_gate`；同时按股票代码精确匹配 `get_realtime_quote` 结果，并让单股深挖用真实 `quote_trade_date/price_label/freshness_note` 覆盖 LLM 错误行情口径，避免 Trace 报告出现串票或把休市行情写成盘中数据。
- [新功能] 候选发现支持 LLM 专家委员会模式：新增 `src/agent/candidate_experts_v2/committee.py` facade（`run_committee_discovery`），`AgentTraceRunRequest.candidate_discovery_mode` 单次请求级生效，request 优先级 > `.env` `AGENT_CANDIDATE_DISCOVERY_MODE` > 默认 `deterministic`；当前三席位调试链路禁用失败回退，committee 失败会 emit `selection_candidate_discovery_mode` 事件携带 `fallback=False` 并终止选股流水线。
- [新功能] 前端 `AgentTracePage` 在配置面板新增"候选发现模式"下拉，选项 `deterministic`（默认）/`llm_expert_committee`（实验），选择会持久化到 `localStorage.dsa.candidateDiscoveryMode` 并按当次随 `traceStream` payload 提交。
- [测试] 新增 `tests/test_candidate_committee.py`，覆盖 committee facade 的 schema 兼容、资金面证据 attach 与 deterministic 异常 coerce 路径；`tests/test_agent_stock_selection.py` 补充 `SelectionRunContext.candidate_discovery_mode` 默认值、`_resolve_candidate_discovery_mode` fallback 与 `_run_candidate_discovery_tool` LLM 分流/降级用例；`tests/test_agent_models_api.py` 补充 `AgentTraceRunRequest.candidate_discovery_mode` 合法/非法/默认值 Pydantic 校验。
- [新功能] 新增 macOS 一键部署：`scripts/install-mac.sh`（自动检测 Intel / Apple Silicon，装 Xcode CLT + Homebrew + `python@3.11` + `node@22` + `uv` + 项目依赖，剔除 graphiti / neo4j 并强制 `GRAPHITI_ENABLED=false`，可选 `SEQUOIA_DB_URL` 下载候选 DB），并提供 `scripts/start-backend.command` / `scripts/start-web.command` 双击启动器（内部 `eval brew shellenv` 兼容 Finder 启动）。
- [文档] 新增 `docs/deployment/deploy-to-new-mac.md`，覆盖从空白 Mac -> Homebrew -> 项目源码 -> 一键装依赖 -> 启动后端/前端的完整迁移流程，并说明数据库与 graphiti 不在迁移范围内。

- [新功能] 资金面候选专家新增 per-dimension 工具手册 (`src/agent/candidate_experts_v2/tools_manifest/capital.yaml`)，覆盖 11 个白名单工具的 priority / when_to_use / typical_args / returns_summary / key_fields / combo_hints / cost / failure_modes 9 字段业务语义；YAML 在 `CapitalFlowExpert.__init__` 阶段加载并按 priority 渲染进 SYSTEM prompt，并对 whitelist / ToolRegistry / typical_args 参数名做三层一致性校验，typical_args 支持 `{today}` / `{seed_codes}` 运行时占位符替换；`get_tushare_moneyflow_ths` 保留为手动工具但不在主链路主动调用。
- [测试] 新增 `tests/test_tools_manifest.py`，覆盖 manifest 加载、白名单与 registry 交叉校验、参数名子集校验、占位符替换、Markdown 渲染顺序和最终 SYSTEM prompt 注入路径，共 16 个用例。

- [新功能] 新增新机器部署脚本：`scripts/install-windows.ps1`（Windows 宿主启用 WSL2 并安装 Ubuntu）、`scripts/bootstrap-wsl.sh`（WSL 内一键装 Python 3.11 / Node 22 / uv / 依赖，自动剔除 graphiti、neo4j 行并强制 `GRAPHITI_ENABLED=false`，可选 `SEQUOIA_DB_URL` 下载候选 DB）、`scripts/start-backend.sh` 与 `scripts/start-web.sh`（前台启动器）。
- [文档] 新增 `docs/deployment/deploy-to-new-windows.md`，覆盖从空白 Windows -> WSL2 -> 项目源码 -> 一键装依赖 -> 启动后端/前端的完整迁移流程，并说明数据库与 graphiti 不在迁移范围内。
- [新功能] 新增 `src/agent/candidate_experts_v2` 多专家选股委员会骨架（LLM 驱动 + 工具白名单 enforcement + 跨 session 文件缓存），首发资金面专家 `capital_flow_expert`；默认开关 `AGENT_CANDIDATE_DISCOVERY_MODE=deterministic` 不影响现网，需显式切换 `llm_expert_committee` 才会启用。
- [文档] 新增 Agent 选股阶段当前实现说明，梳理 `watchlist_scan` 阶段流水线、底层工具取证、Trace artifact 和 `NaN` 非标准 JSON 排障路径。
- [改进] Agent Trace 最终报告新增 Markdown 导出按钮，便于保存和复盘本轮分析结果。
- [改进] Agent Trace 与候选池页面统一为候选决策榜展示，默认突出建议动作、评分、核心依据和可展开证据，原始专家分组降级为折叠调试区。
- [改进] 选股最终报告将高分 wait 拆分为条件入场、强观察和等待确认，并把标题分数标注为候选分，补充次日触发、禁止追高和失效条件。
- [文档] 新增 Agent 选股 wait 过度保守校准方案，规划将强等待候选拆分为条件入场、强观察和普通等待，并优化候选分与报告渲染语义。
- [新功能] Agent TuShare 工具补齐板块主题、消息事件、基本面和技术侧直连接口，并将 THS 板块资金流、结构化事件和 `daily_basic` 快照接入候选专家。
- [新功能] 资金面候选专家补齐 TuShare 东财资金流、龙虎榜、涨停榜、连板天梯和热榜工具，并接入 L1 候选发现链路，所有新增 TuShare 工具均不做跨数据源 fallback。
- [新功能] 新增 `get_tushare_moneyflow_ths` Agent 手动工具；当前因权限不足不在 L1/三席位主链路主动调用，不做跨数据源 fallback。
- [文档] 新增 TuShare 候选专家工具补全记录，梳理资金、板块、消息、基本面和技术候选专家可复用的 TuShare 接口。
- [修复] Agent Trace 选中具体账户时强制注入该账户持仓上下文，并在执行层为持仓快照和搜索工具补齐账户/代码名称约束，避免报告误用全账户汇总或代码名称错配。
- [修复] `get_capital_flow` 在 StockAPI `codeFlow` fallback 上按接口文档使用 `pageSize=50`，并开放 `start_date/end_date/page_no/page_size` 参数，支持按指定日期窗口查询历史资金流。
- [修复] `get_capital_flow` 明确以 Tushare `moneyflow` 为资金面主数据源，失败时回退 StockAPI `codeFlow`，避免私有网关正常时仍落到滞后历史窗口。
- [新功能] `watchlist_scan` 新增均衡候选证据包，按策略、消息、资金、基本面各最多 2 只候选统一取证并落盘 `candidate_evidence.json` / `candidate_evidence.md`；候选级取证并行执行，重复候选按维度顺序跳过并继续补位，后续深挖优先复用已获取证据。
- [修复] Tushare 官方接口和私有网关请求默认绕过本机代理，避免资金流 fallback 被 `127.0.0.1` 代理超时拖垮。
- [修复] 统一 Tushare 查询入口改为优先使用官方 SDK 并显式设置 `pro._DataApi__http_url`，兼容私有网关调用方式，HTTP 轻量客户端仅作为兜底；SDK 超时隔离使用线程而非进程级信号，避免影响后端服务稳定性。
- [修复] `get_capital_flow` 的 Tushare `moneyflow` 兜底不再先请求慢速交易日历，并为私有网关预留更长单次预算，避免 5 秒超时导致真实资金流数据被判失败。
- [改进] 默认将 `AGENT_CAPITAL_FLOW_TIMEOUT_SECONDS` 提升到 15 秒，并将 `watchlist_scan` 的 `AGENT_SELECTION_DEEP_DIVE_LIMIT` 下调到 2，减少资金流误超时并缩短整轮选股耗时。
- [修复] `get_chip_distribution` 继续以 Tushare `cyq_chips` 为主链路，并修复 Tushare 子请求预算向下取整导致 `1.5s` 被截断为 `1s` 的超时误判；同时上调默认筹码预算以适配私有网关最近交易日查询耗时。
- [修复] Tushare 私有网关查询改为进程内串行调用，并在 `get_capital_flow` 的 `moneyflow` 空表结果上自动重试一次，同时记录查询参数，避免并发 Trace 下偶发空表被误判为资金流缺失。
- [修复] Tushare 默认 HTTP 入口统一切换为 `http://118.89.66.41:8010/`，并保留 `TUSHARE_HTTP_URL` 作为部署覆盖项。
- [改进] 候选发现底层补齐更丰富的技术指标输出：AlphaSift/Sequoia 技术候选现在可携带 MA、MACD、RSI 与布林带位置等信息；同时修复 provider 侧 `_short_metric` 缺失导致技术专家链路报错的问题。
- [文档] 新增 Agent 候选池多专家架构方案，明确当前串行候选召回与真正多专家候选生成的区别，并规划 AlphaSift、Sequoia、sector、消息和资金工具的复用路径。
- [文档] 新增 Agent 候选池策略缺口与建设路线，梳理 AlphaSift、Sequoia、资金、基本面、消息和回评闭环的待办事项。
- [文档] 新增消息事件 Graphiti 知识图谱方案，规划新闻入库、事件归一、成熟度追踪、验证事实、图谱候选生成和前端事件链路展示。
- [新功能] Agent L1 候选池新增硬排除层、候选质量摘要和生命周期诊断，前端同步展示硬策略主干、共振、兜底和排除原因。
- [新功能] Agent L1 候选池新增基本面发现专家，基于本地预计算 `fundamental_candidate_snapshot` 表输出质量、成长、估值和现金流候选，并提供 Tushare 刷新脚本。
- [新功能] Agent L1 候选池新增 SQLite 运行记录、查询 API 和独立 Web 页面，支持查看最新/历史候选、来源维度、生命周期、兜底和硬排除摘要。
- [改进] P2 基本面候选闭环补齐前端可见性，候选池 API、Trace 和独立候选池页展示基本面专家状态、快照行数、报告期、DB/table 诊断和候选财务指标。
- [修复] `scripts/update_fundamental_candidates.py` 启动时加载项目 `.env`，避免 `TUSHARE_TOKEN` 已配置但脚本进程读不到导致 `fundamental_candidate_snapshot` 为空。
- [改进] `scripts/update_fundamental_candidates.py` 新增断点续跑和分批落库，支持 `--resume` 跳过已写入股票、`--force` 强制重刷、`--flush-every` 控制批量写入，降低长任务中断损耗。
- [修复] `detect_market_regime` 的历史 K 线缓存按最近有效交易日判断新鲜度，并将组件默认预算调至 8 秒，避免周末或盘前误判缓存失效后被 2 秒预算提前截断。
- [改进] 选股流水线传给 LLM 的候选发现、初筛和单股深挖上下文改用 EvidenceCard 压缩视图，保留 raw_ref 供 Trace 展开，避免大段原始工具 JSON 稀释模型注意力。
- [改进] 选股最终报告将反方审查和 Judge 裁决收敛为辅助摘要，结论区优先展示组合配置与逐股证据，避免辩论内容喧宾夺主。
- [改进] Agent Trace 前端运行时将 `session_id` 写入 `/agent-trace/<session_id>` URL，并让后端日志显式记录 Trace session，便于按页面地址定位日志和 artifact。
- [修复] Agent Trace 打开 `/agent-trace/<session_id>` 时若浏览器本地历史为空，会从后端已落盘 artifact 恢复结果，避免已有 trace URL 页面为空。
- [修复] Agent Trace 历史记录改为只在浏览器本地保存轻量 session 索引，完整结果从后端 artifact 恢复，避免大型选股 Trace 完成后因 `localStorage` 超配额导致页面黑屏。
- [新功能] `discover_watchlist_candidates(auto)` 接入候选池多专家发现层，策略、技术、板块、消息、情绪等专家独立输出 `ExpertCandidatePacket` 后统一合并，并保留主题观察、容量控制和专家诊断。
- [改进] Agent Trace 前端展示候选池多专家发现结果，包含各发现专家状态、候选数、主题观察、容量控制和候选来源专家标签。
- [修复] Agent Trace 将“多专家选股”限定为 L1 候选发现专家，不再把市场环境、维度验证或组合风控专家展示为选股专家，并以 `discover_watchlist_candidates` 的候选池作为 L1 权威来源。
- [修复] 选股最终报告补充候选池来源、入池理由、逐股深度分析和证据缺口，并将“有候选但证据不足”的 Judge 结果从 `reject` 稳定降级为 `wait`，避免最终报告只剩拒绝结论。
- [改进] 选股最终报告新增运行链路说明、逐股维度证据展开和 Judge monitor 覆盖同步，并扩大多候选场景下的深度分析覆盖面，避免报告只显示浅摘要。
- [改进] 选股最终报告改为结果优先结构，先展示推荐排序、入场区间、仓位、止损和证据摘要，将候选池来源与逐股调试证据降级为附录。
- [修复] 选股最终报告区分“入池召回分”和“可执行推荐”，弱等待、反向证据或仅候选池命中的股票不再被包装为首选/次选。
- [修复] Agent Trace 注入真实持仓上下文时，若 MiMo 意图分类失败，不再用默认 `watchlist_scan` 覆盖 `position_review`，避免持仓问答误进入候选池选股链路。
- [改进] planning_execute 系统 Prompt 新增候选池边界协议和 watchlist_scan 独立输出格式，明确 L1 候选池 schema、二阶段注入模板、入池分语义、Judge 裁决字段和 watchlist/持仓/单票入场触发边界。
- [改进] 选股报告候选池附录按“入池分”降序展示，并标注每只候选是否进入逐股深度分析；新增 `AGENT_SELECTION_DEEP_DIVE_LIMIT` 控制深挖覆盖数量。
- [修复] Agent Trace 无明确选股意图时不再默认进入 `watchlist_scan`，即使 MiMo 误判为选股也会被显式选股意图护栏降级为 `qa`，避免无关问题构建候选池。
- [文档] README 新增两层系统框架图，明确 L1 多专家候选池与 `planning_execute` 分析报告链路的边界和数据流。
- [文档] 新增 Agent Evidence Card 协议文档，定义多专家链路中工具 raw、EvidenceCard、ExpertEvidencePacket 和 JudgeInputPacket 的压缩传递契约。
- [新功能] 多专家选股链路新增 EvidenceCard 中间层，工具 raw 结果通过 evidence_adapter 压缩为专家证据包和 Judge 输入包，并保留 raw/full_ref 供 Trace 展开。
- [改进] Agent Trace 选股 artifact 新增 `evidence_cards.json`、`expert_packets.json` 和 `judge_input_packet.json`，便于前端与排障工具直接读取压缩证据层。
- [修复] `AGENT_ARCH=multi` 下的 `watchlist_scan` 重新接入阶段化选股链路，确保 `AGENT_ORCHESTRATION_MODE=expert_graph` 时前端能收到 `expert_state` 和 `selection_expert_graph_done`。
- [修复] Agent Trace 的 MiMo 意图分类改用小米接口实际模型 id `mimo-v2.5`，提高输出 token 上限并暴露 `intent_resolution` 诊断；无显式股票代码时默认进入 `watchlist_scan` 候选池，避免“下周可入手股票”误落到非选股链路后仍显示 legacy 多专家提示。
- [修复] L1 候选池多专家发现将单专家超时默认提高到 20 秒并修复板块成分调用参数，避免 AlphaSift/Sequoia 本地全市场策略扫描被 8 秒预算误杀后只剩消息面候选。
- [新功能] 新增统一 Tushare 客户端配置，支持 `TUSHARE_HTTP_URL` 私有网关，并补充基础列表、低频行情、三大财报和参考事件 Agent 工具。
- [修复] `get_chip_distribution`、`get_capital_flow`、`get_sector_rankings` 优先使用 Tushare 私有网关快路径，避免慢速 AkShare/Eastmoney 链路导致 Agent 工具超时，并保留旧数据源诊断兜底。
- [修复] Agent 慢数据工具补齐内部超时与 source_chain 诊断，`get_stock_info`、`get_chip_distribution`、`get_sector_rankings` 和 `detect_market_regime` 不再被单一三方源拖到外层 30 秒超时。
- [改进] StockAPI `codeFlow` 在最近窗口为空时继续回查更早窗口，并将空数据与端点失败区分展示，便于判断是否需要补数据权限。
- [新功能] Agent 新增 StockAPI 涨停股池、热点板块、板块成分资金、板块资金历史、股票人气和游资活动工具，补强资金面、情绪面和短线候选发现证据。
- [修复] StockAPI 增强工具改为按需调用，不再并入资金面默认全量工具计划，并新增 Agent 单批工具调用超时以避免慢接口拖垮整轮 Trace。
- [修复] `get_capital_flow` 调用 StockAPI `codeFlow` 时按 15:30 更新时间选择查询日期，15:30 前默认查前一天，避免当天数据未更新时报 `60047`。
- [修复] StockAPI 请求层新增串行限流、`88888` 退避重试和人气榜短缓存，避免 `get_stockapi_popularity_rank` 在多工具并发时因 StockAPI 不支持并发请求而失败。
- [修复] `get_stockapi_hot_sectors` 在 StockAPI 热点板块接口返回 `60050` 权限/参数错误时降级到 AKShare 行业板块排行，并保留原始错误诊断，避免板块热度证据直接为空。
- [修复] `get_stockapi_hot_money_activity` 的 rank 模式在 StockAPI 游资排行接口返回 `60050` 时降级到 AKShare 龙虎榜个股代理数据，并明确标记降级来源和代理类型。
- [修复] 选股流水线新增股票代码/名称一致性硬校验，阶段间传递和最终报告按代码覆盖错误名称并输出 `stock_identity_audit`，避免如 `301028` 被写成“友升股份”的错配进入报告。
- [修复] Agent Trace 返回非敏感运行配置，新增 `/api/v1/agent/runtime-config` 诊断接口，并让前端优先展示后端实际 `AGENT_ORCHESTRATION_MODE`，避免历史选股结果或旧字段导致多专家模式误显示为 legacy。
- [修复] Agent Trace 前端收到 `selection_expert_graph_done` 后立即同步本次选股状态为 `expert_graph`，避免最终载荷不完整或历史状态残留时误提示“本次选股结果仍为 legacy”。
- [修复] `selection_expert_graph_done` 事件直接携带 `expert_state`，且 Agent Trace 前端合并最终载荷时不再让过期 legacy 结果覆盖已生成的专家图谱。
- [改进] Agent Trace 将 `fallback_seed_pool` 固定种子候选单独展示为“兜底观察池”，避免误归入策略候选并误导为真实策略筛选结果。
- [修复] `discover_watchlist_candidates` 的 `sector` 模式在板块成分接口超时或为空时先回退到本地 AlphaSift/Sequoia 候选，并在 Trace 中输出板块接口诊断，避免直接展示固定种子池。
- [修复] 选股流水线在账户持仓上下文存在时不再访问不存在的 `PositionContext.name/weight_pct` 字段，深度取证搜索工具补齐 `stock_name` 参数，并在阶段失败时保留 `expert_graph` 部分报告，避免 Agent Trace 退回 legacy 提示。
- [改进] Agent Trace 意图识别接入 `XIAOMI_MIMO_URL` / `XIAOMI_MIMO_KEY` 的 MiMo-V2.5 分类器，前端不再用关键字判断是否发送默认股票代码，避免“下周可入手股票”这类自然表达被误路由到单股分析。
- [新功能] planning_execute 的 `watchlist_scan` 接入五阶段选股流水线，按候选发现、初筛、单股深度分析、组合配置、反方审查和 Judge 裁决输出结构化 `stock_selection` 结果。
- [新功能] 新增 Graphiti 时序知识图谱最小集成路径，支持可选 Neo4j 配置、分析结果入图和 Agent 知识图谱检索工具。
- [改进] `test_env.py` 新增 `--graph` 检查，支持检测 Neo4j 连通性和 Graphiti embedding 模型配置。
- [修复] Graphiti 写入和查询前显式初始化 Neo4j 索引，避免首次空库查询因缺少 `edge_name_and_fact` 全文索引失败。
- [修复] Graphiti 自定义实体字段改名，避免 `Stock.name` / `Sector.name` 与 Graphiti 保留字段冲突导致 `agent-trace` 入图被 schema 校验拒绝。
- [改进] Agent 选股 Prompt 拆分到独立 `src/agent/stock_selection_prompts.py`，并新增 `SelectionRunContext` 管理阶段 `summary/full/full_ref`，避免选股上下文持续膨胀。
- [改进] Agent Trace 落盘新增 `stock_selection.json`、`selection_context.json` 和 `final_report.json`，便于复盘选股阶段状态、证据摘要和最终裁决。
- [修复] `get_capital_flow` 调用 AkShare 个股资金流时按 A 股代码补充 `market=sh/sz/bj`，并修复结构化工具失败仍在 Trace 中显示 OK 的状态误判。
- [文档] 新增 Agent 工具能力缺口分析，梳理行情、技术、资金、消息、情绪、宏观和图谱工具边界，并提出市场情绪与地缘风险工具路线图。
- [文档] 扩展 Agent 用户上下文计划，补充工具补全、连续对话、方案保存、模拟盘托管、自进化、回测、regime、策略库和量化交易长期路线图。
- [文档] 重写 README，将首页说明收敛到当前私有分支的 agent-trace / planning_execute 主链路，并标注上游遗留能力为非当前维护重点。
- [文档] 细化方案保存与模拟盘托管路线，补充 Agent 自出方案、虚拟下单、结果反馈、经验注入和自进化提案闭环。
- [文档] 融合 A 股未来架构设计，重排 Agent 长期实现顺序为结构化信号协议、A 股硬风控、市场环境感知、L1/L2/L3 聚合、模拟盘质量校准和自进化闭环。
- [新功能] 新增 Agent 结构化信号协议与独立 A 股 `risk_gate` 底座，覆盖 L1/L2/L3、TradePlan、T+1、涨跌停、特殊股票状态、止损、数据质量、仓位和现金约束。
- [新功能] Agent Trace 接入确定性 `risk_gate`，运行结束后生成 `TradePlan`、落盘 `risk_gate.json`，并在 `/trace/run` 与 `/trace/stream` 完成载荷返回风控通过、阻断或降级结果。
- [改进] Agent Trace 前端新增 `Risk Gate` 面板，展示风控状态、允许动作、TradePlan、行情状态、规则检查、阻断原因和警告。
- [改进] 资金面工具扩展为个股主力资金、市场资金快照、北向资金和融资融券摘要，并同步更新 Planner、提示词和回归测试。
- [修复] `get_capital_flow` 移除慢速全市场 fallback，保留真实东方财富连接错误摘要，避免个股资金流接口不可达时被笼统包装成 `capital_flow timeout`。
- [修复] `get_capital_flow` 显式工具改用独立超时预算，并让 `get_chip_distribution` 返回结构化失败诊断，避免资金流/筹码工具失败时前端或模型误判为可用证据。
- [新功能] `discover_watchlist_candidates` 接入 Sequoia 风格量化候选池，支持按均线放量、海龟突破、高窄旗形、涨停洗盘、上升趋势跌停和 RPS 突破生成结构化候选，并保留板块和固定种子降级路径。
- [新功能] 新增 `scripts/update_sequoia_candidates.py`，可从 baostock 拉取 A 股最近 260 个交易日日线并落库到 Sequoia 候选池 SQLite，避免运行全量历史回填。
- [改进] `discover_watchlist_candidates` 的 `auto` 模式改为多路召回 + 统一评分，同时合并 Sequoia 量化候选和强势板块成分股，避免硬策略未命中股票被粗筛阶段直接排除。
- [改进] `discover_watchlist_candidates` 接入 AlphaSift YAML 策略候选召回，优先复用可配置硬筛、因子打分和策略标签，再与 Sequoia 形态策略和板块候选统一合并。
- [文档] README 新增 `L1-L9` 系统分层命名规范，明确数据层、候选池层、证据层、信号层、决策层、风控闸门、方案层、托管跟踪层和复盘进化层的职责边界。
- [改进] Agent Trace 页面新增 `Layered Trace` 可折叠视图，按 `L1-L9` 展示 Prompt 输入、上下文取数、候选池召回、SSE 流、工具调用、信号摘要、裁决、风控、TradePlan 和复盘入口。
- [改进] Agent Trace 的 L2 候选池层改为候选股票列表视图，展示入池来源、策略标签、候选理由、评分和证据指标。
- [改进] Agent Trace 的 L1 数据层收敛为数据工具调用视图，并将本地 `AGENT_MAX_STEPS` 示例提高到 20，降低复杂选股链路超步数概率。
- [改进] Agent Trace 的 L1 数据层新增候选来源审计，直接展示候选股票如何由 Sequoia、强势板块、用户种子或 fallback 召回，以及为什么后续工具调用这些个股。
- [改进] Agent Trace 将 L1 数据层与 L2 候选池层合并为 `L1 Data & Candidate Layer`，并为候选发现工具保留结构化 `result_json`，避免截断预览导致候选池无法展示。
- [改进] Agent Trace 的候选池展示改用中文策略名、中文来源和中文候选理由，并移除 L1 中重复的候选说明块，提升可读性。
- [文档] 同步 Agent 用户上下文计划的当前状态，补齐 `risk_gate`、Sequoia 候选池、Graphiti 最小链路和已落地阶段记录，避免旧阶段说明误导。
- [修复] 增强 Agent Debate JSON 解析，支持从模型解释文本和多个 JSON 对象中提取最终裁决，并提高 Debate 角色输出 token 预算，减少误降级为“Debate JSON 解析失败”。
- [改进] `get_capital_flow` 默认优先使用 StockAPI 历史资金流 `codeFlow`，不再默认调用东方财富个股资金流端点，并补充最近可用主力净流入与 5/10 日累计资金流。
- [文档] 补充 Agent 工具缺口的数据源调研，明确 GDELT、Alpha Vantage、Trading Economics、Tushare、ACLED 等 API URL、Token 配置和推荐接入顺序。
- [修复] Agent ReAct 循环新增渐进式工具预算护栏，达到 60%/80% 步数后提示收敛、重复工具同参数复用已有结果，并在最后一步基于现有证据强制综合，避免复杂任务直接报超步数。
- [修复] DeepSeek 官方渠道支持在未配置 `LLM_<NAME>_API_KEY(S)` 时复用 `DEEPSEEK_API_KEY(S)`，并在 Agent 鉴权失败时明确提示应更新 DeepSeek key，避免误判为 OpenAI key 或普通 fallback 失败。
- [新功能] 新增 A 股 `detect_market_regime` 工具，基于经验 CDF 波动分档、阻尼、A 股情绪替代分量、Wyckoff 相位和本地 SQLite 确认状态生成结构化市场环境约束。
- [改进] `watchlist_scan` 选股流水线接入 `detect_market_regime`，在候选初筛、单股深挖、组合配置、反方审查和 Judge 裁决中传递市场状态，并对 `risk_off`、`panic` 和极端波动环境执行确定性开仓降档。
- [文档] 新增 A 股 Regime 状态机原理文档，说明经验 CDF 波动分档、阻尼、情绪合成、Wyckoff 相位、SQLite 持久化和选股降档机制。
- [新功能] 新增 `analyze_price_structure` Stage3 价格结构工具，输出缠论包含合并、分型、笔、中枢、力度、未完成笔，以及 SMC 摆动、BOS/CHoCH、OB/FVG 结构证据。
- [文档] 新增 Stage3 价格结构分析引擎原理文档，说明 Chan/SMC 结构识别边界和选股链路接入方式。
- [文档] 重写 README 为用户视角首页，突出账户感知、候选池融合、对抗辩论、A 股风控、Regime、价格结构、长期记忆和未来闭环规划。
- [改进] Agent Trace 的 L1 数据与候选池层改为候选卡片视图，展示股票名称、召回来源、策略标签，以及策略/技术面/资金面/情绪热点等入池理由。
- [修复] Agent 候选池为 Sequoia/AlphaSift 本地行情候选补齐中文股票名称，并修复 auto 模式下 Sequoia 策略名导致 AlphaSift YAML 召回被误过滤为空的问题。
- [文档] 新增 Agent 情绪面工具实施调研与闭环方案，明确 A 股情绪/消息候选的数据源、工具契约、评分、存储、候选池接入和验收标准。
- [新功能] `watchlist_scan` 新增实验性 `AGENT_ORCHESTRATION_MODE=expert_graph`，在保留原阶段化选股流水线的同时向 Trace 输出市场、候选、技术、资金/筹码、消息/情绪、基本面和组合风险专家意见。
- [修复] Agent Trace 多专家模式在候选池为空时仍输出专家图谱状态，并修正文案避免把缺失 `expert_state` 误提示为 legacy 链路。
- [改进] Agent Trace 候选池归因拆分策略、技术、资金和情绪维度，避免策略候选与技术候选重复，并明确资金候选当前多为成交额/换手/量比等流动性代理。
- [改进] `discover_watchlist_candidates` 新增 `event_impact` 事件影响链，按 1 天突发事件与 7 天验证窗口区分主题观察和已验证个股候选，并将事件 best-effort 写入 Graphiti。
- [改进] Agent Trace 新增“消息/事件观察”区块，展示 `event_impact` 未验证事件、影响变量、观察主题和验证状态，避免 watch-only 事件在前端不可见。
- [新功能] Agent 新增 `score_stock_news_sentiment` 个股消息评分工具，并将 `news_momentum` 公司级新闻/公告硬事件接入 `discover_watchlist_candidates`，使候选池可出现“消息面候选”。
- [改进] `score_stock_news_sentiment` 和 `news_momentum` 的个股新闻补证默认只做前两个搜索 provider 的快速尝试，避免辅助消息面取证拖慢整轮 Trace。
- [修复] Agent Trace SSE 长工具执行期间改为发送 heartbeat，并为候选发现板块成分接口增加短超时与事件主题取样上限，避免 `discover_watchlist_candidates` 慢接口导致前端误报 Trace 分析超时。



- [新功能] Web 新增开发者 Agent Trace 页面与 `/api/v1/agent/trace/run`、`/api/v1/agent/trace/stream` 调试接口，流式展示 planning_execute 的 Planner、账户/持仓/用户画像摘要、事件时间线、工具调用参数/结果预览和最终输出。
- [新功能] planning_execute 新增对抗式 Debate Agent，基于同一份 Evidence Bundle 生成主观点、强制反方观点和 Judge 裁决，并在 Agent Trace 展示 `Debate Judge` 模块和落盘 `debate.json`。
- [改进] Agent Trace 的 `Debate Judge` 模块补充同一 session 内的原始主报告、Primary/Opposing/Judge 原始输出和最终合并输出，便于开发模式下排查对抗链路。
- [改进] Debate Judge 输出改为摘要、分维度证据和要点化裁决，并强制审视账户风险、技术面、资金面、消息面与数据质量，避免裁决被技术面单一维度主导。
- [修复] Agent Trace 选股/组合配置类 Prompt 不再误发送默认 `600519` 股票代码；无持仓注入时也会生成最小 planning 上下文，避免 Planner 为空后退回单股分析链路。
- [修复] Agent Trace 流式执行入口改为使用 planning_execute system prompt 和 Execute Protocol，避免前端显示 Planner 计划但后端按普通聊天链路提前结束。
- [修复] watchlist_scan 新增候选股发现工具与执行审计，用户未提供股票代码时先生成候选池再进入单股取证，避免只基于大盘/板块工具提前输出选股结论。
- [修复] 对抗式 Debate Agent 覆盖 `watchlist_scan` 选股/组合配置场景，避免选股 Trace 已生成候选报告但 `debate.json` 为空。
- [修复] Agent Trace 对工具返回显式 `error/errors` 的结果一律显示失败，并避免流式事件缺少 `success` 字段时前端默认显示 OK。
- [修复] planning_execute 的最终 Markdown 输出会清理“第三步/第二步”这类执行步骤标题，避免入场报告把证据摘要误写成流程编号。
- [改进] Agent Trace 调试表单补充报告意图、单票上限、总权益仓位上限、最大回撤和默认止损输入，便于在开发模式下完整模拟投资者画像约束。
- [改进] Agent Trace 页面新增浏览器本地历史，保留最近 10 次运行结果，便于回看已完成的工具调用链路。
- [改进] Agent Trace 页面改为可点击 Evidence Timeline 与大尺寸 Markdown 输出窗口；持仓报告输出规范补充未来价格情景与分层持仓策略要求。
- [改进] planning Agent prompt 新增未持仓入场报告输出规范，采用可见 Planning -> Execute -> 入场决策格式，并约束入场区间、禁止追高线、首仓比例、加仓条件、止损目标、淘汰条件和复查触发。
- [改进] Agent Trace 后端新增本地调试产物落盘目录，按 session 保存 request、context、planner、events、tool calls、evidence ledger、final 报告和 todo.md，便于开发者离线复盘调用链路。
- [改进] planning Agent prompt 新增 Execute Protocol，明确 Evidence Ledger、工具失败降级、停止条件、Trace artifacts 和最终输出审计门槛；Trace `todo.md` 会在执行结束后反映工具成功/失败状态。
- [改进] 新增 `start_all.sh` / `stop_all.sh` 本地开发启动脚本，一键启动或停止 FastAPI 后端与 Vite 前端，并将 PID 与日志保存到本地目录。
- [改进] `start_all.sh` / `stop_all.sh` 纳入 Graphiti Neo4j 容器，本地一键启动默认同时准备知识图谱存储。
- [改进] Agent 实时行情工具补充市场会话与最新可用交易日元数据，并约束持仓报告在休市/非交易日标注行情口径，避免把最近交易日涨跌幅误写成“今日涨跌幅”。
- [改进] Agent SSE 完成事件补充模型、token 与工具调用日志，便于前端调试调用链路。
- [新功能] 新增 `test_env.py` 环境 API 连通性检测工具，支持 LLM、搜索、行情、社媒情绪和通知通道 smoke test。
- [新功能] Agent 普通单股分析新增 `AGENT_ANALYSIS_MODE=planning_execute` 实验模式，接入账户感知 planning prompt 与 Planner 工具执行计划。
- [新功能] 新增 Agent capability -> tools 映射与 `PortfolioService` 到 `AgentUserContext` 的上下文构造器，支持把持仓成本、仓位、浮盈亏注入 Planner。
- [文档] 更新 Agent 用户上下文计划、完整配置指南和环境变量模板，说明二阶段 Planner 外壳与三阶段持仓上下文接入状态。
- [测试] 补充 Agent Planner、持仓上下文构造和 planning_execute prompt 注入回归测试。
- [新功能] 自定义 Webhook 支持 `CUSTOM_WEBHOOK_BODY_TEMPLATE` JSON body 模板，便于适配 AstrBot、NapCat 和自建推送服务。
- [修复] 统一持仓快照输出现价/市值/浮盈亏/收益率与价格元信息，并为 LLM 渠道测试补充结构化诊断与设置页排障提示。
- [文档] 补充 LLM 渠道编辑器的官方来源、依赖兼容窗口、保存时的运行时模型清理规则，以及旧配置回退路径说明。
- [测试] 补齐 task_queue 运行时配置同步回归证据，明确 `tests/test_task_queue_config_sync.py` 作为本轮验收项。
- [改进] Bot `/status` 展示统一 LLM 主模型、Agent 模型、渠道模式、YAML 配置和更多通知渠道状态。
- [文档] 新增分析原理专题文档，系统说明输入输出、数据工具、信号评分、LLM/Agent 决策和降级机制。
- [文档] 新增 Agent 用户上下文与分阶段改造计划，并补充账户感知分析的 schema 契约。
- [文档] README 增加当前状态与路线图，明确账户感知 Agent 的已完成底座、近期计划和暂不处理范围。
- [文档] README 重写项目定位，明确未来转向按需/重大事件触发的账户感知分析，不再以每日仪表盘、大盘报告或固定推送为核心。
- [新功能] 新增 planning Agent system prompt 契约模块，沉淀账户感知、按需触发和 planning-execute 的角色原则与输出约束。
- [改进] planning Agent prompt 补充股票/账户领域分析维度与能力域，覆盖技术面、行情量价、筹码、资金、基本面、板块、消息事件、舆情、持仓和回测。
- [文档] Agent 改造计划补充 capability -> tools 映射层，明确 planning-execute 后续如何从能力域展开到现有 ToolRegistry 工具。
- [改进] planning Agent prompt 新增已持仓报告格式草案，约束持仓结论、账户影响、关键价格、行动计划和风险缺口。
- [改进] planning Agent prompt 新增数据引用与 confidence 内部使用约束，禁止最终输出暴露置信度字段或编造缺失数据。
- [测试] 新增 Agent 用户上下文 schema 与 planning prompt 拼接回归测试，锁定第一阶段契约不被后续改动破坏。

## [3.14.2](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.14.1...v3.14.2) - 2026-04-30

### 发布亮点

- 大盘复盘扩展到港股，并让 Bot `/market` 与 CLI/调度入口使用一致的交易日过滤语义。
- 问股与 Agent 链路增强配置缺失、决策 fallback 和多策略选择体验。
- LLM 与分析报告链路提升稳定性：非法 JSON 响应会继续尝试备用模型，LiteLLM DEBUG 日志默认降噪。
- 新增只读首次启动配置状态接口，为后续配置向导和 smoke run 奠定基础。

### 新功能

- 大盘复盘支持港股市场：`MARKET_REVIEW_REGION` 新增 `hk` 选项；`both` 扩展为 A股+港股+美股，并新增港股指数（HSI/HSTECH/HSCEI）复盘链路。
- 新增只读首次启动配置状态接口 `GET /api/v1/system/config/setup/status`，用于识别 LLM、Agent、自选股、通知和本地存储配置缺口；该接口不会重载运行时、写入 `.env` 或创建数据库文件。

### 改进

- 问股页面支持组合选择多个 Agent 策略。

### 修复

- Bot `/market` 命令复用 `get_open_markets_today()` / `compute_effective_region()` 做交易日过滤：结果作为 `override_region` 透传给 `run_market_review`；若结果为空字符串则跳过复盘并推送“今日相关市场休市”，与 CLI/调度入口行为一致。
- 问股 Agent 在未配置可用 LLM 时保留后端真实错误原因并维持 `done.success=false` 失败语义，避免前端把配置缺失误当成成功回答。
- Agent 模式未生成有效决策仪表盘时保留本地趋势分析的评分、趋势和操作建议，并将强买/强卖 fallback 归一到兼容的 `buy`/`sell` 决策类型，避免首页结果被 `50 / 观望 / 未知` 缺省值覆盖。
- 持仓快照现价缺失时不再静默回退为持仓成本；当天快照优先使用历史收盘价，仅在缺失时使用实时价 fallback，缺价持仓不再污染市值与未实现盈亏汇总，并为持仓明细返回价格来源、日期、stale 与缺价状态。
- 分析 Prompt 在注入 `trend_analysis` 前按最终 `trend_status` / `ma_alignment` 清洗互斥理由：空头结构移除看多理由、多头结构移除空头结构风险，并在事件/技术冲突与异常放量（>10 倍）时强制提示“事件先行、技术待确认”与量能降权。
- LLM 返回非 JSON 响应时同样触发备用模型切换：主模型成功返回但无法解析 JSON 时，不再立即降级为纯文本 fallback，而是依次尝试 `LITELLM_FALLBACK_MODELS` 中的备用模型；所有模型均无法返回合法 JSON 时，再降级为文本 fallback。
- LiteLLM 内部 DEBUG 日志默认压低到 WARNING，避免流式生成时 token 级日志污染 `stock_analysis_debug_*.log`；如需排查 LiteLLM 内部细节，可临时设置 `LITELLM_LOG_LEVEL=DEBUG`（Fixes #1156）。

### 文档

- 补充 LLM 配置指南与 FAQ，明确问股 Agent 对 `LITELLM_CONFIG` / `LLM_CHANNELS` / legacy `GEMINI_*` `OPENAI_*` `ANTHROPIC_*` 的兼容优先级、回退路径与“不静默迁移旧配置”的结论。

### 测试

- 新增 `tests/test_bot_market_command.py`，覆盖 `MARKET_REVIEW_REGION=both` + open markets `{"cn","us"}` / `{"cn","hk"}` 的 `override_region` 透传断言，并覆盖全市场休市跳过与关闭交易日检查路径；新增 `tests/test_yfinance_hk_indices.py` 覆盖港股指数符号映射与部分/全部失败降级路径。
- 补齐 `task_queue` 轻量导入 stub 的股票代码规范化函数，恢复 `tests/test_task_queue_config_sync.py` 收集与运行。

## [3.14.1](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.14.0...v3.14.1) - 2026-04-26

- [测试] 修正大盘复盘 prompt 测试对“明日交易计划”标题的断言，并同步桌面端版本号，恢复发布 gate。

## [3.14.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.13.0...v3.14.0) - 2026-04-26

### 发布亮点

- 📊 **大盘复盘升级为盘后工作台式结构** — A 股复盘固定输出盘面温度、指数明细、板块 Top 表、新闻催化、明日交易计划和风险提示，减少纯文字复盘的重复与空泛。
- 🖥️ **桌面端新增 GitHub Release 更新提醒** — Windows/macOS 桌面端启动后自动检测新版本，也可从设置页手动检查并跳转下载页。
- 🤖 **Pipeline Agent 数据加载大幅降噪** — K 线工具改为 DB-first 并预热 240 天历史数据，避免同一只股票重复 HTTP 请求。
- 🐳 **Docker 发布链路整理** — 发布工作流收敛为正式发布与手动补发两条路径，官方 Docker Hub 镜像名统一为 `zhulinsen/daily_stock_analysis`。
- 🔧 **LLM 渠道与 DeepSeek V4 配置补强** — GitHub Actions 定时分析补齐多渠道变量透传，DeepSeek 官方渠道预设与示例同步到 V4。
- 🧩 **桌面端静态资源一致性校验** — 打包链路和运行时都能更早发现静态资源错配，降低 Release 包白屏排查成本。

### 新功能

- 🏠 **Web 首页历史报告区新增重新分析入口** — 支持基于原始 prompt 重做同一只股票同日期的分析。
- 🖥️ **Windows/macOS 桌面端新增 GitHub Release 更新提醒** — 启动后自动检测新版本，并支持从设置页手动检查后跳转下载页。

### 改进

- 📊 **A 股大盘复盘报告改为结构化盘后工作台版式** — 固定输出盘面温度、指数明细、板块 Top 表、新闻催化和明日交易计划。
- 🐳 **Docker 发布工作流收敛** — 更清晰地区分正式发布与手动补发链路，并统一官方 Docker Hub 镜像名为 `zhulinsen/daily_stock_analysis`。
- 🤖 **Agent 日线工具优先复用本地缓存** — 同时持久化新获取的日线与新闻情报，减少重复数据源调用。

### 修复

- 🤖 **Pipeline Agent K 线工具 DB-first 加载** — `get_daily_history` / `analyze_trend` / `calculate_ma` / `get_volume_analysis` / `analyze_pattern` 改为优先读取本地 DB，消除同一只股票 9x5=45 次重复 HTTP 请求（Fixes #1066）。
- 🤖 **Pipeline Agent 执行前按需预热 240 天 K 线历史到 DB** — 正常情况下 K 线工具调用无需重复网络请求。
- 🕒 **冻结 `target_date` 并通过 ContextVar 透传到 Pipeline Agent K 线工具线程** — 消除跨收盘边界时间漂移。
- 🪟 **Windows 桌面端后端日志转抄编码修复** — 转抄 stdout/stderr 时优先使用 UTF-8，并兼容本地代码页回退，避免中文日志乱码。
- ⚙️ **GitHub Actions 每日分析工作流补齐 LLM 渠道变量透传** — 支持 `LLM_CHANNELS`、多 Key 与常用 `LLM_<NAME>_*`，避免本地可用的多模型配置在云端定时任务中失效（Fixes #1063, #872）。
- 📈 **历史报告详情接口修正 `change_pct` 取值** — 使用 `is None` 判断避免把 0.0（平盘）当作缺失值丢弃，移除错误的 `change_60d` 兜底，并在缺失时回退到原始实时行情字段（Fixes #1084）。
- 🔧 **DeepSeek 官方渠道预设与示例配置同步到 V4** — 保留 legacy `deepseek-chat` 默认值并增加废弃提示，同时修正模型发现后旧运行时选择导致保存失败的问题（Fixes #1108, #1109）。
- 🧩 **桌面端打包链路新增静态资源一致性检查** — `scripts/check_static_assets.py` 会在源 `static/` 与 PyInstaller 产物中校验 `index.html` 引用的资源是否真实存在，运行时也会在错配时写入明确日志，避免重现 Release 包打开后白屏（Refs #1064 / #1065 / #1050）。
- 🧩 **后端 `/assets/*` 改为显式路由托管** — 资源缺失时返回与请求扩展名匹配的 `text/javascript` / `text/css` 404，减少默认 JSON 错误响应带来的排查误导（Refs #1064）。
- 🌙 `**kimi-k2.6` 自动使用固定温度** — 主分析、大盘复盘和 Agent 调用该模型时自动使用 `temperature=1.0`，避免模型拒绝默认温度请求（Fixes #1102）。

### 文档

- 🐳 **补充官方 Docker 镜像使用说明** — 增加镜像拉取、`docker run` 用法与 `.env` / 数据目录映射说明，不再只覆盖 Compose 部署路径。
- 📨 **修正飞书自定义机器人 Webhook 示例** — `feishu_sender.py` 中的示例改为 interactive card JSON，并补充飞书自动化 Webhook 触发器配置教程。
- 📚 **优化根 README 结构** — 保留首页级功能特性、技术栈、快速开始、推送效果、Web、Agent、赞助商和新闻源入口，将细配置、交易纪律和基本面语义收口到完整指南，并将 Docker 徽章指向官方镜像页。
- 🌐 **同步英文与繁中 README 的精简入口结构** — 同时补齐完整指南中的 LLM 用量 API 与持仓管理说明。
- 🤝 **调整 AI 协作与 PR 模板中的 README 维护规则** — 明确 README 非必要不更新，细节优先进入专题文档。

### 测试

- 🧪 **稳定市场复盘相关测试的 LiteLLM stub 行为** — 避免本机安装的 LiteLLM 在测试收集顺序变化时影响市场复盘单元测试。
- 🧪 **pytest 默认跳过前端依赖目录** — 本地存在 `apps/dsa-web/node_modules` 时不再被后端测试递归扫描，避免发布前 gate 被无关目录拖慢。

## [3.13.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.12.0...v3.13.0) - 2026-04-21

### 发布亮点

- 🌉 **长桥 OpenAPI 数据源接入** — 美股/港股行情优先使用 Longbridge，YFinance / AkShare 自动兜底；未配置时行为不变。
- 📈 **Tushare 港股全链路扩展** — 港股日线通过 `hk_daily` 获取；筹码分布对港股返回 `None`；换算单位跟随港股口径，不再套用 A 股手/千元规则。
- 🔍 **Anspire Search 语义搜索接入** — 配置 `ANSPIRE_*` 后即可使用 Anspire Search 获取实时行情及资讯，未配置时完全透明。
- 🚀 **普通分析链路支持 LLM 流式生成** — 首页任务 SSE 新增 `task_progress` 事件，进度更细化；不支持流式的 provider 自动回退到非流式调用。
- 🤖 **Web 渠道编辑器支持按需拉取可用模型列表** — `/v1/models` 统一模型发现入口，多选写回 `LLM_{CHANNEL}_MODELS`，拉取失败时保留手动输入降级。
- 🛡️ **Agent 稳定性与预算护栏全面补强** — `AGENT_MAX_STEPS` 语义统一、技能降级不中断管线、SSE 异常透传、技能加载 warning 日志补齐。
- 🛠️ **SQLite 写入链路原子化** — 批量原子 upsert + WAL + `busy_timeout` + 有限写入重试，显著降低批量分析并发锁竞争。

### 新功能

- 🌉 **集成 Longbridge OpenAPI 作为美股/港股可选数据源**（fixes #981）— 配置 `LONGBRIDGE_*` 后优先使用长桥获取日线与实时行情，YFinance / AkShare 兜底；未配置时行为与此前一致。联调使用 `tests/longbridge_live_smoke.py`（手动脚本，不参与 pytest 收集）。
- 📈 **Tushare 支持港股日线查询** — 配置 Tushare 凭证后调用 `hk_daily` 接口获取港股数据；权限不足时抛出异常，与原流程一致。
- 🔍 **集成 Anspire Search 可选语义搜索后端** — 配置 `ANSPIRE_*` 可使用 Anspire Search 获取实时行情及新闻资讯；未配置时行为与此前一致。联调使用 `tests/test_anspire_search.py`（手动脚本）。
- 🚀 **普通分析链路支持 LiteLLM 流式生成与更细任务进度** — 股票分析在 LLM 阶段优先尝试 `stream=True` 并在服务端累积 chunk，首页任务 SSE 新增 `task_progress` 事件与更细的 `message/progress` 更新；仅在最终 JSON 解析成功后持久化历史报告；不支持流式的 provider 自动回退到非流式调用。
- 🤖 **Web AI 模型配置支持按渠道获取可用模型列表** — 渠道编辑器支持调用 `/v1/models` 拉取可用模型，并以多选方式写回 `LLM_{CHANNEL}_MODELS`；拉取失败时保留手动输入作为降级路径。

### 改进

- 🔎 **SerpAPI 正文补抓范围收敛** — 自然搜索结果不再逐条同步抓取网页正文；仅对极少数高位且摘要不足的结果做延迟补抓，优先复用 SerpAPI 已返回的结构化摘要，降低搜索链路尾延迟与慢站点放大风险。
- 🤖 **LLM 接入体验简化** — 面向用户的 AI 模型接入文案统一为"主模型 / Agent 主模型 / 备选模型 / 模型渠道"，不再把 LiteLLM 当作普通用户必学概念，现有 `LITELLM_*` / `LLM_CHANNELS` 配置键保持兼容。
- 🧠 **IntelAgent 新增公司公告搜索与主力资金流工具** — 增加上交所/深交所/cninfo 公告搜索维度与 `get_capital_flow` 工具，修复 Agent 模式下公告和资金流数据经常缺失的问题。
- 📦 **后端股票名称解析优先复用 `stocks.index.json`** — 懒加载缓存前端静态索引，纯后端/缺失静态资源场景静默降级回 `STOCK_NAME_MAP` 与原有数据源回退链路。
- 📊 **TushareFetcher 港股单位适配** — `get_chip_distribution` 对港股直接返回 `None`（港股暂不支持筹码分布）；`_normalize_data` 对港股（`hk_daily`）不再做 A 股手→股、千元→元的缩放，与 Tushare 港股字段语义一致。
- ⏱️ **Agent 超步数错误增加 `AGENT_MAX_STEPS` 调整提示** — 帮助用户自助排查步数限制问题。
- ⚙️ **GitHub Actions 分析任务超时支持 `vars` 配置** — `daily_analysis.yml` 任务超时从 repository variables 读取，无需修改代码即可调整运行超时上限（fixes #1014）。

### 修复

- 📣 **大盘复盘链路接入 `REPORT_LANGUAGE`** — `REPORT_LANGUAGE=en` 时，A 股/合并复盘的 Prompt、章节标题、模板兜底文案与通知包装标题统一输出英文，避免英文正文搭配中文标题的混排问题。
- 📈 **EfinanceFetcher 指数开盘价映射兼容**（fixes #1043）— `get_main_indices()` 的开盘价映射改为兼容 `今开 → 开盘 → open`，修复部分 efinance 版本下指数开盘价被读成缺失值的问题。
- 🤖 **AGENT_MAX_STEPS 语义统一**（fixes #1026）— 在 orchestrator 多 Agent 模式下明确为"各子 Agent 步数上限而非硬覆盖"；TechnicalAgent 等高默认值 Agent 会被封顶，低默认值 Agent 保持原值；用户主动调高（>10）时统一覆盖所有子 Agent。修复了用户设置 12 但 TechnicalAgent 仍以默认 6 步运行并报 "Agent exceeded max steps" 的问题。
- 🛡️ **Specialist（Skill）Agent 失败改为优雅降级** — 技能 Agent 失败不再中断整个分析管线，与 intel/risk 保持相同的降级策略。
- 🔧 **MiniMax-M2.7 连接测试修复** — 修复 LLM 通道连接测试在 MiniMax-M2.7 下返回 "Empty response" 的问题；将 `max_tokens` 上限从 8 提升至 256 以容纳思考过程，并添加 `content_blocks` 格式解析逻辑。
- 📊 **移除 `sentiment_score` 范围约束**（fixes #942）— 移除 `HistoryItem` 与 `ReportSummary` 响应 Schema 中 `sentiment_score` 的 `ge=0/le=100` 约束，历史库中存储的超范围值不再触发 Pydantic ValidationError。
- 🖥️ **WebUI 前端资源缺失时发出明确警告** — `webui_frontend.py` 在 `static/index.html` 存在但 `static/assets/` 缺失时发出 warning，避免 CSS/JS 资源缺失导致页面异常变大却无从排查（fixes #944）。
- 🔗 **分析管线可选服务降级初始化** — `StockAnalysisPipeline` 搜索服务与社交舆情服务任一初始化异常时，记录 warning 并以禁用状态继续运行，避免外部依赖抖动阻塞主分析链路。
- 🖥️ **桌面端版本展示统一读取 `package.json`** — 统一读取 `apps/dsa-desktop/package.json`，移除 preload 中硬编码的 `0.1.0`，设置页展示真实桌面端版本；修复版本号显示错误（fixes #1048）。
- 🐋 **港股名称获取失败修复**（fixes #940）— 修复主数据源字段缺失时无法正确回退到备用字段获取港股名称的问题。
- 🔄 **SSE 任务流断开时 `CancelledError` 正确 re-raise**（fixes #967）— 修复 SSE 流中断时异常被静默吞掉导致故障无日志可查的问题。
- 🔄 **Agent SSE 清理阶段后台任务异常正确上报**（fixes #969）— 流结束时后台执行器异常现在正确记录并上报，避免错误无法感知。
- 🔇 **技能加载异常补充 `logger.warning` 日志**（fixes #970）— 在 `ask.py`、`skills/aggregator.py`、`skills/router.py` 的静默 except 块补充日志，确保技能列表为空时有日志可查。
- 🛠️ **SQLite 写入链路原子化**（fixes #878）— `stock_daily(code,date)` 使用批量原子 upsert；文件型 SQLite 连接默认启用 WAL + `busy_timeout` + 有限写入重试；"新增数"改按本次真正插入窗口计算。
- 💰 **多 Agent / 单 Agent 预算护栏语义统一** — 剩余预算低于最小阈值时主动跳过并降级；已完成阶段可构建降级报告时返回 `success=True` 并携带非空内容，否则返回 `success=False`。
- ⚙️ **GitHub Actions `daily_analysis.yml` 补齐 `REPORT_LANGUAGE` 注入**（fixes #1013）— 修复用户在 Secrets/Variables 中配置 `REPORT_LANGUAGE` 后不生效的问题。
- 📊 **任务状态 API 补齐实时价格字段**（fixes #983）— `GET /api/v1/analysis/status/{task_id}` 从数据库回填已完成任务时补齐 `current_price` / `change_pct`，修复首页报告股票名旁不显示实时价格的问题。
- 📅 **非交易日数据返回最近交易日**（fixes #1009）— 修复非交易日（周末/节假日）筹码分布与板块排行返回倒数第二个交易日数据的问题，现在正常返回最近交易日数据。
- 🔍 **A 股资讯搜索恢复中文优先** — `search_stock_news()` 在首个 provider 主要返回英文资讯时继续尝试后续引擎，并将同批结果中的中文资讯排到前面；非美股查询不再默认沿用 Brave 的 `en/US` 区域语言偏好。
- 📨 **飞书群机器人通知支持签名校验** — 飞书通知现在支持 `FEISHU_WEBHOOK_SECRET` / `FEISHU_WEBHOOK_KEYWORD`；Web 设置与文档明确区分 Webhook 推送模式和 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 应用模式，降低误配风险。
- ⚡ **LLM 适配层新增 `RateLimitError` 和 `ContextWindowExceeded` 检测** — 识别并处理速率限制与上下文窗口超出错误，提升分析链路在高负载或长文本场景下的健壮性（fixes #1002）。

### 测试

- 🧪 **TushareFetcher 港股相关单元测试** — 新增 `get_chip_distribution` 筹码分布获取与 `_normalize_data` 港股/A 股/ETF 单位处理的单元测试，覆盖港股特殊路径。

### 文档

- 📘 **DEPLOY.md 补充 UI 元素异常变大排查步骤** — 新增重建 Docker 镜像或手动执行 `npm run build` 的排查指南；`deployment/deploy-webui-cloud.md` 同步更新。
- 📨 **飞书 Webhook 配置说明补全** — 强调 `FEISHU_WEBHOOK_URL` 是群通知必填项、签名校验须两端同时启用或关闭、`FEISHU_APP_SECRET` 仅用于应用/Stream Bot 模式；`.env.example` 补充内联注释；同步英文指南。
- 🤝 **FAQ 补充 Ollama 连接失败排障条目（Q12c）** — 覆盖服务未启动、URL 配置错误、模型前缀缺失、模型未下载、远程防火墙等 5 个检查点（fixes #854）。
- 🌉 **README 补充长桥数据源使用说明** — 中/英/繁 README 明确长桥"首选 / 兜底 / 未配置不调用"边界；`docs/` 内相对路径链接修复；`LONGBRIDGE_PRINT_QUOTE_PACKAGES` 配置与代码及 `.env.example` 对齐。
- 🐋 **Docker 安装场景版本说明** — 补充最小化文档，明确 Docker 安装场景下应以 Git tag / 镜像 tag 判断版本（fixes #1091）。

## [3.12.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.11.0...v3.12.0) - 2026-04-01

### 发布亮点

- 📊 **回测页新增"次日验证"视图** — 可按股票与日期范围查看 AI 预测 vs 次日实际涨跌，复用历史分析与 1 日回测结果，快速验证分析准确率。
- 🔧 **LLM 接入体验简化** — 用户侧文案统一收口为"主模型 / 备选模型 / 模型渠道"，不再把 LiteLLM 当作普通用户必学概念，现有配置键保持兼容。
- 🐳 **Docker / WebUI 运行时稳态补强** — 修复系统设置保存后配置不生效、启动早期日志缺失、预构建静态资源复用等问题，降低容器化部署的运维摩擦。
- 🔒 **安全与并发稳定性同步增强** — Discord 入站 Webhook 补齐 Ed25519 验签，修复并发执行时共享状态未加锁、单股推送模式通知并发复用等问题。
- 🖥️ **桌面端与定时任务细节打磨** — Windows 安装器支持自选安装目录，内置定时调度器感知运行中 SCHEDULE_TIME 变更，断点续传改按市场时区判断。

### 新功能

- 📊 **回测页新增"次日验证 / 1 日窗口"视图** — 可按股票代码与分析日期范围查看 AI 预测、次日实际涨跌及筛选区间准确率，复用历史分析与 1 日回测结果实现。
- 🏷️ **Web 设置页新增版本信息卡片** — `apps/dsa-web` 现在会在构建时注入前端包版本与构建时间，系统设置页新增只读"版本信息"区块，展示 `WebUI 版本 / 构建标识 / 构建时间`；当 `package.json` 仍为占位版本 `0.0.0` 时，会自动回退为构建标识，方便 Docker 重建后快速确认当前静态资源是否已经生效。
- 🪟 **Windows 桌面安装器支持自选安装目录** — 安装器改为支持在安装向导中自定义安装目录，安装到非默认盘符后仍沿用现有打包态目录逻辑在安装目录旁读写 `.env`、`data/stock_analysis.db` 和 `logs/desktop.log`，同时保留 `win-unpacked` 免安装分发方式。安装器仅支持当前用户安装、已禁用管理员提权（`allowElevation: false`），并通过 NSIS `.onVerifyInstDir` 阻止选择系统保护目录。

### 改进

- 🔎 **SerpAPI 正文补抓范围收敛** — 自然搜索结果不再逐条同步抓取网页正文；现在仅对极少数高位且摘要明显不足的结果，在更短超时预算内做延迟补抓，并优先复用 SerpAPI 已返回的结构化摘要，降低搜索链路尾延迟与慢站点放大风险。
- 🤖 **LLM 接入体验简化** — 面向用户的 AI 模型接入文案已统一收口为"主模型 / Agent 主模型 / 备选模型 / 模型渠道 / 高级模型路由配置"；Web 设置页、配置元数据、校验提示与中英文文档不再把 LiteLLM 当作普通用户默认必学概念，现有 `LITELLM_*` / `LLM_CHANNELS` 配置键仍保持兼容。

### 修复

- 🚀 **启动早期失败时暴露真实根因** — `python main.py` 现在通过 stderr 暴露真实根因，bootstrap 阶段不再向硬编码 `logs/` 目录写入文件日志，文件日志推迟到 `config.log_dir` 可用后创建，避免健康启动在非预期路径残留日志文件。
- 🐳 **Docker WebUI 运行时优先复用预构建静态资源** — `prepare_webui_frontend_assets()` 现在会先检查镜像内已有的 `static/index.html` 是否可直接复用；当容器运行时不包含 `apps/dsa-web` 源码目录且未安装 `npm` 时，也不会误报"未找到前端项目，无法自动构建"，从而恢复 Docker 部署后的 WebUI 打开能力。
- 🐳 **Docker WebUI 系统设置保存后配置生效** — Docker 场景下 WebUI 保存 `STOCK_LIST`、`SCHEDULE_ENABLED`、`SCHEDULE_TIME`、`SCHEDULE_RUN_IMMEDIATELY`、`RUN_IMMEDIATELY` 后，`Config` 会优先读取持久化 `.env` 中的新值，避免被容器创建时注入的旧环境变量覆盖。
- 📈 **市场复盘 LLM max_tokens 提升** — 市场复盘生成链路将 LLM `max_tokens` 从 `2048` 提升到 `8192`，降低长复盘输出因 `MAX_TOKENS` 提前截断导致内容未完成的概率。
- ⏰ **内置定时调度器感知 SCHEDULE_TIME 运行时变更** — 调度器现在会在运行中感知 WebUI 保存后的 `SCHEDULE_TIME` 变化，并在下一轮检查时重绑 daily job。
- 🪟 **Windows Release 渠道编辑器保留 MiniMax 模型前缀** — 渠道模式下填写 `minimax/<模型名>` 时，后端归一化与 Web 设置页运行时模型列表都会保留该值原样，不再误改写成 `openai/minimax/<模型名>`。
- 🤖 **Discord 入站 Webhook 补齐 Ed25519 验签** — `DiscordPlatform` 现在会基于 `X-Signature-Ed25519`、`X-Signature-Timestamp` 和原始请求体校验 Discord Interaction 签名；缺失签名头、公钥格式非法或签名不匹配时直接拒绝请求，同时对 timestamp 做 ±5 分钟时效窗口校验以防御重放攻击。
- ⚙️ **STOCK_GROUP_N / EMAIL_GROUP_N 配置关系明确化** — 明确与 `STOCK_LIST` 的关系，并在配置校验中对超出 `STOCK_LIST` 的邮件分组给出 warning。
- 🗓️ **断点续传改按市场时区和交易日历判断**（fixes #880）— 股票数据存在性检查不再直接使用服务器自然日，而是按 A 股 / 港股 / 美股各自市场时区解析"最新可复用交易日"。
- 📨 **单股推送模式不再并发复用共享通知实例** — `StockAnalysisPipeline.run()` 现在会保留个股分析并发，但把 `SINGLE_STOCK_NOTIFY=true` 下的即时通知挪到结果收集侧串行发送。
- 🔇 **实时行情降级提示收口为单次告警** — 分析主流程获取股票名称时不再提前触发一次实时行情查询，只有在全部数据源都不可用时才提示已降级为历史收盘价继续分析。
- 🔍 **A 股中文资讯搜索恢复中文优先** — `search_stock_news()` 现在会在首个 provider 主要返回英文资讯时继续尝试后续引擎，并将同批结果中的中文资讯排到前面。
- 🔒 **并发执行时共享状态补齐统一加锁** — 修复并发执行时共享状态缺少统一加锁的问题，避免多线程场景下的数据竞争。

### 测试

- 🧪 **补充设置页版本信息回归测试** — 新增 Web 设置页版本信息渲染断言，并覆盖占位版本 `0.0.0` 自动回退为构建标识的逻辑。
- 🧪 **UI 治理与关键路径回归补强** — 补充 `SidebarNav`、`ChatPage`、`BacktestPage` 等组件测试，并新增 UI governance 守卫，持续防止交互元素重新引入原生 `title` 属性或旧 `input-terminal` 样式回流。同步更新 smoke / markdown drawer 相关验证，覆盖主题升级后的关键主链路。

## [3.11.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.10.1...v3.11.0) - 2026-03-27

### 发布亮点

- 🎨 **Web 工作台完成一轮 UI 统一与双主题升级** — 首页、问股、回测、持仓和设置页进一步收口到统一设计 token、输入表面和状态表达；新增完整浅色主题，并支持浅色 / 深色一键切换与持久化保存。
- 🤖 **Bot / Agent 能力重新补回主分支** — 恢复 `/history`、`/strategies`、`/research` 等命令，`/ask` 继续支持多股对比与组合视角；Deep Research、事件监控与 schedule 轮询链路重新接回主线能力。
- 🔒 **安全性与运行稳态同步补强** — 修复 `X-Forwarded-For` 限流绕过风险，恢复 LiteLLM 官方 PyPI 安装路径，Tushare 初始化不再依赖本地 SDK，降低 Docker、桌面打包和环境重建时的脆弱点。
- 🖥️ **日常使用细节继续打磨** — 修复首页港股自动补全提交、登录页首屏主题闪烁、历史长股票名重叠，以及 Telegram Markdown 解析失败时整条通知发送中断等问题。

### 新功能

- 🎨 **全新浅色主题与双主题切换上线** — Web 工作台新增完整浅色主题，并支持在侧边栏中一键切换浅色 / 深色模式；主题选择会持久化保存，刷新页面后仍保持当前偏好。此次升级不是局部配色微调，而是对卡片层级、边界对比、输入表面、状态提示和页面背景做了一整套 light theme 重绘。
- 🤖 **补回主分支缺失的 Agent / Bot 能力** — `#648` / `#649` 已重新补回 `main`：Bot 恢复 `/history`、`/strategies`、`/research`，`/ask` 保留多股对比与组合视角；Deep Research 与 Event Monitor 的配置重新在 Web 设置页可见并可编辑，schedule 模式也重新接入事件告警轮询。

### 改进

- 🖥️ **核心页面统一到同一套工作台视觉语言** — `Home / Chat / Backtest / Portfolio / Settings` 进一步收口到共享设计 token、`input-surface` 输入体系、空态/错误态表达和抽屉遮罩语义，减少页面之间的视觉割裂与局部私有样式漂移。
- 💬 **问股交互可达性与反馈增强** — 问股页补强了会话导出、通知发送、消息复制、历史删除与追问上下文提示；AI 回复操作不再过度依赖 hover，触屏设备和小屏场景下也能直接触达关键按钮。
- 📊 **回测与持仓页表面和状态表达继续标准化** — 回测页筛选控件、布尔状态、结果表格与汇总卡片统一到共享输入/状态原语；持仓页的导入反馈、汇率刷新提示、空态与警示信息进一步归口到共享组件，减少页面级重复实现。
- 🧭 **导航与页面壳层协同优化** — 侧边栏主题切换、问股完成角标、移动端抽屉遮罩和主内容滚动契约进一步统一，首页、问股和回测在桌面端与移动端的切页体验更稳定。

### 测试

- 🧪 **UI 治理与关键路径回归补强** — 补充 `SidebarNav`、`ChatPage`、`BacktestPage` 等组件测试，并新增 UI governance 守卫，持续防止交互元素重新引入原生 `title` 属性或旧 `input-terminal` 样式回流。同步更新 smoke / markdown drawer 相关验证，覆盖主题升级后的关键主链路。

### 修复

- 🌗 **Web 首屏默认主题预设为深色** — `apps/dsa-web/index.html` 现在会在 React 挂载前读取本地保存的主题偏好；若没有已保存值，则立即给 `<html>` 预设 `dark` 并同步 `color-scheme`，避免首页和登录页首屏先闪出浅色主题。
- 🔐 **登录页独立主题层收口** — 登录页输入框、标签、切换按钮和按钮文案现在使用独立的 `--login-*` 视觉 token，不再继承全局浅/深主题文字色；即使浏览器缓存了浅色主题，登录页仍保持稳定的深色视觉与青色密码输入表现，避免密码圆点和文案落成黑色。
- 🖥️ **首页港股代码输入修复** — Web 首页分析输入框现在可正确接受港股代码与自动完成选中的港股项，补齐 `00700.HK` / `HK00700` 等格式识别，避免提交时误报“请输入有效的股票代码或股票名称”。
- 🔒 **认证限流 X-Forwarded-For 取值修复（CWE-345）**（#841 / #842）— `get_client_ip()` 从取 `X-Forwarded-For` 最左值改为最右值，防止攻击者通过伪造首部旋转限流桶绕过暴力破解保护；仅影响 `TRUST_X_FORWARDED_FOR=true` 且单层可信反向代理的部署场景，多级代理环境需按部署文档评估配置。
- 📦 **恢复 LiteLLM 官方 PyPI 安装并锁定安全上限** — `requirements.txt` 重新使用 `pip install litellm` 的官方 PyPI 安装路径，并在保留历史最低要求 `>=1.80.10` 的同时增加 `<1.82.7` 的安全上限，避免误装已被移除的 `1.82.7` / `1.82.8` 风险版本；Windows 桌面打包脚本也同步回退到标准 `pip install -r requirements.txt` 链路，减少特殊下载分支带来的维护成本。
- 📨 **Telegram Markdown 解析失败回退纯文本**（fixes #850）— `src/notification_sender/telegram_sender.py` 现在会在 Telegram 返回 `HTTP 400` 且包含 `can't parse entities` / Markdown 解析错误时，自动去掉 `parse_mode` 后重试纯文本发送，避免 `*ST` 等正文内容直接导致整条通知失败。
- 🔢 **A 股同码实时行情保留交易所提示**（fixes #852）— `DataFetcherManager` 与 `TushareFetcher` 现在会保留 `SZ000001` / `000001.SZ` 这类显式沪深提示，旧版 Tushare 实时行情降级分支不再把深市 `000001` 误判成 `sh000001` 上证指数。
- 🎯 **多 Agent 次优买点不再盲目复制理想买点**（fixes #851）— 当多智能体结果缺少独立 `secondary_buy` 时，仪表盘现在优先展示 `N/A` 而不是把 fallback 值硬拷贝成与 `ideal_buy` 完全相同，减少误导性的双买点展示。
- 🧩 **Tushare 初始化不再强依赖本地 SDK 包** — `TushareFetcher` 现在直接使用内置 HTTP client 访问 Tushare Pro，不再在启动阶段先 `import tushare` 才能初始化；修复了 Docker、桌面打包或环境重建后因缺少 `tushare` 包而提前报 `No module named 'tushare'` 的问题，并补充对应回归测试。
- ⚙️ `**daily_analysis` 工作流补齐 `DEEPSEEK_API_KEY` 映射** — GitHub Actions 每日分析工作流现在会正确透传 `DEEPSEEK_API_KEY`，避免云端任务配置了密钥却在运行时拿不到对应环境变量。
- 🖥️ **历史列表过长股票名称截断与悬停展示**（fixes #815）— 历史列表中过长的股票名称, 现在会按字符类型自动截断（英文15/中文8/混合10字符），默认显示截断结果，悬停时展示完整名称；解决 1920x1080 分辨率下股票名称与右侧状态标签文字重叠的问题。新增 `stockName.ts` 工具函数并补充对应测试。

### 文档

- 🧾 **README 捐赠入口更新为小红书二维码** — README 及中英文说明中的赞助入口更新为小红书二维码素材，保持展示口径一致。

## [3.10.1](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.10.0...v3.10.1) - 2026-03-24

### 新功能

- 🔔 **Web 端分析推送通知开关**（#808）— 首页分析按钮旁新增「推送通知」复选框，默认勾选；取消勾选时本次分析不发送 Telegram/企业微信等推送。API `POST /api/v1/analysis/analyze` 新增 `notify` 字段（`bool`，默认 `true`），不传时行为与修改前一致，Bot 和定时任务不受影响。

### 改进

- 🖥️ **问股 / 回测页面布局与壳层协同优化** — 统一 Chat / Backtest 页面容器、共享 UI 状态和跟随问答交互路径，移除部分硬编码高度限制，让导航框架内的填充与滚动行为更连贯。
- 🎨 **全局视觉与共享组件继续收敛** — Light theme 引入动态 HSL 阴影体系，统一侧边栏激活态、告警组件对比度和聊天气泡样式，并把部分零散内联样式收口为语义化 CSS 变量，提升一致性与可维护性。

### 修复

- 🖼️ **系统设置智能导入文件选择恢复** — 修复了“系统设置 > 基础设置 > 智能导入”模块中 “选择图片 / 选择文件” 两个按钮点击无响应的问题。
- 🖥️ **移动端滚动与交互层级修复** — 解决主题切换菜单在移动端被主内容遮挡的 z-index 冲突，并恢复首页长报告场景下的正常纵向滚动，不影响其他页面现有滚动行为。
- 🧾 **Markdown 纯文本复制清洗增强** — 改进纯文本导出算法，复制分析报告时会更稳定地清除表格分隔符等 Markdown 痕迹，提升分享和归档内容的纯净度。
- 🧠 **Trading philosophy injection 覆盖 legacy + Agent 全链路**（#810）— `GeminiAnalyzer`、单 Agent 模式和 skill-aware Prompt 现在共享同一套策略注入状态；只有隐式回落到内置默认 `bull_trend` 时才保留旧的趋势型提示，显式策略选择或自定义默认 skill 不再被偷偷叠加 `MA5>MA10>MA20` 多头基线。
- 🛠️ **后端 CI 依赖安装链路稳态化**（#835）— 拆分 backend gate 阶段、为依赖安装增加重试，并把 CI 用的 `litellm` 安装来源调整为更稳定的 GitHub 源，降低依赖解析抖动导致的 backend gate 偶发失败。
- 🪟 **Windows 桌面发版构建恢复 LiteLLM 安装兼容性** — `scripts/build-backend.ps1` 现在会先过滤 `requirements.txt` 中的 LiteLLM GitHub 源包，再下载对应 tag 的 zipball 到本地移除上游可选 `enterprise/` 目录后安装，绕过 Windows runner 上 Poetry 构建 wheel 时把目录误当文件打包导致的失败；同时补上 `pip install` 退出码检查，避免依赖安装失败后只在后续 `python-multipart` 校验阶段才暴露成次生报错。

### 测试

- 🧪 **问股 / 回测 / 智能导入回归覆盖补齐** — 同步更新 E2E 冒烟期望，补充 `DashboardStateBlock`、Chat 页、智能导入文件选择与相关交互回归断言，确保近期 UI 调整后的关键路径仍可稳定通过。

## [3.10.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.9.0...v3.10.0) - 2026-03-24

### 发布亮点

- 🔎 **自动补全与索引工具扩展到三市场** — 补全索引生成链路现在同时覆盖 A 股、港股、美股，配套新增 Tushare 股票列表抓取工具与更完整的静态索引数据，让首页搜索入口从“能用”走向“更全、更稳”。
- 🖥️ **Dashboard 与报告查看体验继续收口** — 首页 Dashboard 面板、状态边界、字体层级和完整报告表格密度完成一轮统一；报告详情也补齐了 Markdown/纯文本复制与更可靠的按钮交互，减少历史报告查看与分享时的摩擦。
- 🤖 **Agent skill 与市场语义边界更清晰** — skill bundle、默认策略、回测汇总语义和兼容接口进一步收敛；同时分析 Prompt 不再默认写死 A 股上下文，美股和港股分析也能按各自市场规则生成更贴切的内容。
- ⏰ **定时与桌面配置能力更贴近真实使用场景** — 桌面端支持 `.env` 导入导出；`python main.py --schedule --stocks ...` 也不再把启动时股票快照错误带入后续计划执行，定时任务会跟随最新保存的 `STOCK_LIST`。

### 新功能

- 💾 **桌面端 `.env` 备份/恢复入口**（#754）— 桌面模式下的系统设置页新增 `导出 .env` / `导入 .env` 按钮，可直接备份当前已保存配置，或把备份文件中的键值合并恢复到当前桌面端 `.env`；导入沿用现有 `config_version` 冲突保护与运行时重载链路，不改变现有桌面端便携模式路径。
- 📊 **Tushare 股票列表获取工具** — 新增 `scripts/fetch_tushare_stock_list.py`，支持从 Tushare Pro 获取 A股、港股、美股列表信息并保存为 CSV，配有分页读取、智能限流、错误处理和进度提示；新增对应使用文档 `docs/modules/TUSHARE_STOCK_LIST_GUIDE.md`。
- 🔎 **索引生成脚本多市场支持** — `generate_index_from_csv.py` 重构为支持 Tushare 和 AkShare 双数据源，同时覆盖 A股、港股、美股三个市场；新增按市场分类的别名映射（A股、港股常见别名，美股常用股票英文缩写）；添加 `--source` 参数切换数据源、`--test` 参数验证模式；严格过滤美股 DUMMY 记录。
- 🔎 **索引生成脚本增强** — `generate_stock_index.py` 新增 `--test`/`-t` 测试模式和 `--verbose`/`-v` 详细输出模式，添加市场分布统计，优化 JSON 输出格式。
- 📋 **首页完整报告支持双模式复制** — 历史报告详情头部新增“复制 Markdown 源码”和“复制纯文本”工具按钮；前者保留原始 Markdown 结构，后者去除常见 Markdown 格式符号，方便分享、归档和跨报告比对。复制按钮文案会跟随 `REPORT_LANGUAGE` 保持中英文一致，避免英文报告页出现中文固定文案。
- 🧩 **个股分析页补齐关联板块展示**（#669）— A 股分析写路径现在会把 `belong_boards` 一次性写入 `fundamental_context` / `fundamental_snapshot`，结构化报告详情同步新增 `belong_boards` 与 `sector_rankings` 字段，Web 个股分析页首屏可直接展示所属板块及其是否命中当日板块涨跌榜；无数据时保持 fail-open 隐藏，不影响现有分析主流程。

### 改进

- 🖥️ **Dashboard 面板统一化（PR7-2）** — 新增 `DashboardPanelHeader` 和 `DashboardStateBlock` 作为历史、报告、资讯、任务和透明度等面板的通用组件；统一了各面板标题层级、加载/空态/错误态和 CSS 变量 token。
- 🖥️ **HomePage 状态边界收口（PR7-2）** — 引入 `useHomeDashboardState` hook，集中 `stockPoolStore` 状态选取逻辑，移除 `HomePage` 中重复的本地状态派生和回调定义。
- 🧭 **Agent skill 统一到单一配置语义** — Multi-Agent runtime、API、Web chat 和配置元数据统一围绕 `skill` 概念收敛；`/api/v1/agent/skills` 成为主发现入口，`AGENT_SKILL_*` 成为主配置面，内置 skill 元数据也开始声明默认启用、排序优先级、market regime tag 等信息，减少默认策略散落在代码里的隐式耦合。
- 🔎 **自动补全索引数据更新** — 重新生成 `stocks.index.json`，涵盖 A股、港股、美股三个市场，提升自动补全覆盖率。
- 🧾 **Dashboard 字体与完整报告表格密度微调** — 收敛首页侧栏、空状态、历史操作区的字体层级，并将完整 Markdown 报告表格 `th/td` 的内边距调整到更紧凑的 4-6px 区间，让信息密度与现有 Dashboard 视觉节奏更一致。

### 修复

- ⏰ **定时模式不再锁定启动时 CLI 股票快照** — `python main.py --schedule --stocks ...` 现在不会让后续计划执行沿用启动时的旧股票列表；定时任务每次触发前都会重新读取最新保存的 `STOCK_LIST`，确保 WebUI 或 `.env` 更新后的自选股配置能参与后续推送。
- 🌍 **LLM Prompt 按股票市场动态注入上下文** — 分析链路不再把市场规则写死成 A 股；系统 Prompt 会根据股票代码识别 A 股、港股或美股，并注入对应的角色描述与交易规则提示，减少跨市场分析出现口径错位或结论失真的问题。
- 🔎 **美股自动补全复用 ticker 去重** — `generate_index_from_csv.py` 在导入 Tushare `us_basic` CSV 时会先按 `ts_code` 折叠复用的美股 ticker，优先保留更可能仍在使用的记录，避免 `stocks.index.json` 出现重复 `canonicalCode` 后让 Web 自动补全展示历史名称或提交歧义代码。
- 🧾 **Web 报告详情复制交互稳定性修复**（#749）— `ReportDetails` 中“原始分析结果 / 分析快照”的复制按钮补齐可点击层级，避免被下方 JSON 内容覆盖；两个面板的复制提示也改为各自独立，不再出现复制一个后两个按钮同时显示“已复制”的误导反馈。
- 📊 **Agent skill 回测与兼容接口语义收敛** — `get_skill_backtest_summary` 现在要求显式传入 `skill_id`，缺失时返回明确校验提示；仓库尚未持久化真实 skill 级汇总时会返回明确的 unsupported/info 响应，并保留 `normalized` 与 `*_pct` 兼容字段，避免沿用 overall 指标误导 Agent 或用户。
- 🔧 **Skill 默认选择与兼容层行为加固** — `allowed-tools` 会继续仅作为 `SKILL.md` bundle 元数据保留，不再泄露到运行时工具选择；`/api/v1/agent/strategies` 恢复旧 payload 形状；显式传入 `skills: []` 时会清空陈旧上下文；当用户明确选择策略 skill 时不再偷偷叠加默认 bull-trend，而在 `AGENT_SKILLS` 为空时则统一只回落到单一主默认 skill。

### 测试

- 🧪 **Dashboard 组件测试覆盖率扩展（PR7-2）** — 新增 `ReportNews` 和 `TaskPanel` 测试；对 `HistoryList`、`ReportDetails`、`HomePage`、`useDashboardLifecycle` 和 `stockPoolStore` 增强了断言覆盖，包括删除回退、移动端抽屉和任务生命周期等场景。
- 🧪 **多市场索引生成测试补齐** — 新增 `tests/test_generate_index_from_csv.py`，覆盖 Tushare/AkShare 双数据源解析、多市场判断、美股 DUMMY 过滤与重复 ticker 去重等核心路径。
- 🧪 **关联板块写入与 API 契约回归** — 新增 `tests/test_pipeline_related_boards.py`，并补充分析历史与分析接口契约测试，确保 `belong_boards` / `sector_rankings` 只做增量扩展且保持 fail-open。
- 🧪 **定时模式股票列表语义回归测试** — 新增 `tests/test_main_schedule_mode.py`，覆盖定时模式忽略启动时 `--stocks` 快照、单次运行仍保留 CLI 股票覆盖的边界场景。

### 文档

- 📘 **新增 Tushare 股票列表工具文档** — 新增 `docs/modules/TUSHARE_STOCK_LIST_GUIDE.md`，说明股票列表抓取工具的使用方法、数据格式和常见问题。
- 🌍 **补齐定时模式与关联板块的双语说明** — `docs/full-guide.md` / `docs/full-guide_EN.md` 现在明确说明 scheduled mode 会在每次执行前重新读取 `STOCK_LIST`，并同步补充个股关联板块展示能力说明，减少配置预期偏差。
- 🧭 **调整 Agent 术语兼容文案** — README、双语文档、设置页与问股界面继续以“策略”作为用户入口主称呼，同时补充 `skill` 作为内部统一命名，降低迁移期理解成本。

## [3.9.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.8.0...v3.9.0) - 2026-03-20

### 发布亮点

- 🤖 **模型链路与报告语言更灵活** — Agent 现在可以通过 `AGENT_LITELLM_MODEL` 独立选择模型链路，普通分析与 Agent 报告也可通过 `REPORT_LANGUAGE=zh|en` 输出统一语言，减少“英文内容 + 中文壳子”这类混排问题，并允许团队分别权衡主分析与 Agent 的成本、速度和能力。
- 🔎 **首页分析体验完成一轮闭环优化** — 首页新增 A 股自动补全，支持代码、中文名、拼音和别名检索；同时 Dashboard 状态收口到统一 store，历史、报告、新闻与 Markdown 抽屉的交互更稳定，“Ask AI” 追问也会优先携带当前报告上下文。
- 💬 **通知与检索能力继续外扩** — 新增 Slack 一等通知渠道；SearXNG 在未配置自建实例时可以自动发现公共实例并按受控轮询降级；Tavily 时效新闻链路修复后，严格时效过滤不再错误丢光有效结果。
- 💼 **持仓与市场复盘链路更稳** — A 股 market review 可选接入 TickFlow 强化指数与涨跌统计；持仓账本写入改为串行化以缩小并发超卖窗口；汇率刷新入口和禁用态提示也更加清晰，减少用户误判。

### 新功能

- 🔎 **Web 股票自动补全 MVP** — 首页分析输入框新增本地索引驱动的自动补全，支持股票代码、中文名、拼音和别名匹配；选中候选后会提交 canonical code，并透传 `stock_name`、`original_query`、`selection_source` 到分析请求、任务状态和 SSE 事件；索引加载失败时自动退回旧输入模式，不阻断原有提交流程。同步补充了静态索引加载器、索引生成脚本和前后端契约测试。分阶段进行开发，第一阶段仅支持 A 股。
- 💬 **Slack 一等通知渠道** — 新增 Slack 原生通知支持，同时支持 Bot Token 和 Incoming Webhook 两种接入方式；同时配置时优先使用 Bot API，确保文本与图片发送到同一频道；Bot Token 模式支持图片上传（raw body POST，不使用 multipart）；新增 `SLACK_BOT_TOKEN`、`SLACK_CHANNEL_ID`、`SLACK_WEBHOOK_URL` 配置项，GitHub Actions 工作流同步补齐对应 Secrets 传递。
- 🌍 **报告输出语言可配置**（Issue #758）— 新增 `REPORT_LANGUAGE=zh|en`，默认 `zh`；语言设置会同步注入普通分析与 Agent Prompt，并覆盖 Markdown/Jinja 模板、通知 fallback、历史/API `report_language` 元数据及 Web 报告页固定文案，避免“英文内容 + 中文壳子”的混合输出。
- 🚀 **Agent 与普通分析模型解耦**（Issue #692）— 新增 `AGENT_LITELLM_MODEL`（留空继承 `LITELLM_MODEL`，无前缀按 `openai/<model>` 归一）；Agent 执行链路与 `/api/v1/agent/models` 的 `is_primary/is_fallback` 标记改为基于 Agent 实际模型链路；系统配置与启动期校验补齐 `AGENT_LITELLM_MODEL` 的 `unknown_model/missing_runtime_source` 检查；Web 设置页新增 Agent 主模型选择并与渠道模式运行时配置同步。
- 🔎 **SearXNG 公共实例自动发现与受控轮询**（#752）— 新增 `SEARXNG_PUBLIC_INSTANCES_ENABLED`，在未配置 `SEARXNG_BASE_URLS` 时默认从 `searx.space` 拉取公共实例列表，并按受控轮询顺序选择实例；同次请求内遇到超时、连接错误、HTTP 非 200 或无效 JSON 会自动切换到下一个实例。已配置自建实例的用户保持原有优先级与语义不变；`daily_analysis` GitHub Actions 工作流也已支持显式透传该开关并在启动日志中展示当前状态。
- 📈 **TickFlow market review enhancement** (#632) — 新增可选 `TICKFLOW_API_KEY`；配置后，A 股大盘复盘的主要指数行情优先尝试 TickFlow；若当前 TickFlow 套餐支持标的池查询，市场涨跌统计也会优先尝试 TickFlow。失败或权限不足时立即回退到现有 `AkShare / Tushare / efinance` 链路；板块涨跌榜回退顺序保持不变。接入层同时适配了真实 SDK 契约：主指数查询按单次请求上限分批拉取，并将 TickFlow 返回的比例型 `change_pct` / `amplitude` 统一转换为项目内部的百分比口径。

### 改进

- **Dashboard state slice and workspace closure** — moved Home / Dashboard state into `stockPoolStore`, consolidated history selection, report loading, task syncing, polling refresh, and markdown drawer handling under a single state slice.
- **Dashboard panel standardization** — kept the current dashboard layout contract stable while unifying history, report, news, and markdown presentation with shared tokens, standardized states, and bounded in-panel scrolling for the history list.
- **Dashboard-to-chat follow-up bridge** — routed “Ask AI” follow-ups through report-context hydration instead of direct cross-page state coupling, while keeping chat sends usable when enriched history context is still loading.
- 💼 **持仓账本并发写入串行化**（#742）— 持仓源事件写入/删除现在会在 SQLite 下先获取串行化写锁，减少并发卖出把超售流水写入账本的窗口；直接持仓写接口在锁竞争时返回 `409 portfolio_busy`，CSV 导入保持逐条提交并把 busy 计入 `failed_count`。
- 💱 **持仓页汇率手动刷新入口补齐**（#748）— Web `/portfolio` 页面现在会在“汇率状态”卡片中展示“刷新汇率”按钮，直接调用现有 `POST /api/v1/portfolio/fx/refresh` 接口；刷新后会仅重载快照与风险数据，并以内联摘要反馈“已更新 / 仍 stale / 刷新失败”的结果，减少用户对 `fxStale` 长时间停留的误解。

### 修复

- 🔎 **Web 自动补全 Enter 提交语义修正** — 股票自动补全在搜索命中候选时不再默认高亮第一项；候选列表展开但用户尚未用方向键或鼠标明确选中时，按 Enter 会继续提交原始输入，避免手动输入被第一条候选静默覆盖。
- 🌍 **补齐 `REPORT_LANGUAGE` 启动解析与历史展示本地化边界** — `Config` 在启动时继续遵循“真实环境变量优先、`.env` 兜底”的既有语义，并在两者冲突时输出显式告警，减少 `REPORT_LANGUAGE` 来源不清带来的误判；同时 `/api/v1/history/{id}` 英文详情响应会同步本地化 `sentiment_label`，历史 Markdown 也会正确识别英文 `bias_status` 的风险等级 emoji，避免出现 `乐观` 或 `🚨Safe` 这类中英混排/误报展示。
- 📰 **Tavily 时效新闻检索发布时间映射修复**（#782）— Tavily 在股票新闻和严格时效的情报维度中现在会显式使用 `topic="news"`，并兼容 `published_date` / `publishedDate` 两种发布时间字段；修复了 Tavily 明明返回结果却在后续硬过滤阶段被全部记为 `drop_unknown` 丢弃的问题，同时将机构分析、业绩预期、行业分析等分析型维度恢复为宽源搜索，不再被统一压缩成新闻模式。
- 💱 **持仓页汇率刷新禁用语义修正**（#772）— 当 `PORTFOLIO_FX_UPDATE_ENABLED=false` 时，`POST /api/v1/portfolio/fx/refresh` 现在会返回显式 `refresh_enabled=false` 与 `disabled_reason`，Web `/portfolio` 页面会明确提示“汇率在线刷新已被禁用”，不再误报“当前范围无可刷新的汇率对”。
- 🤖 **Agent timeout and config hardening** — `AGENT_ORCHESTRATOR_TIMEOUT_S` now also protects the legacy single-agent ReAct loop, parallel tool batches stop waiting once the remaining budget is exhausted, and invalid numeric `.env` values fall back to safe defaults with warnings instead of crashing startup.
- 🌐 **CORS wildcard + credentials compatibility** — `CORS_ALLOW_ALL=true` no longer combines `allow_origins=["*"]` with credentialed requests, avoiding browser-side cross-origin failures in demo/development setups.
- 🧭 **Unavailable Agent settings hidden from Web UI** — Deep Research / Event Monitor controls are now treated as compatibility-only metadata in the current branch and are removed from the Settings page to avoid exposing non-functional toggles.

### 文档

- 新增 Ollama 本地模型配置说明，同步更新 `README.md` 与 `docs/i18n/README_EN.md`（Fixes #690）
- 完善 Ollama 配置说明：`docs/full-guide.md` / `docs/full-guide_EN.md` 环境变量表与 Note 补充 `OLLAMA_API_BASE`，避免英文用户误以为 Ollama 不能作为独立配置入口；合并重复的 `OLLAMA_API_BASE` 条目为单一条目
- 明确文档同步治理边界：补充 `README.md`、专题文档、双语文档与交付说明之间的默认同步规则，减少后续文档漂移

## [3.8.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.7.0...v3.8.0) - 2026-03-17

### 发布亮点

- 🎨 **Web 界面完成一轮骨架升级** — 新的 App Shell、侧边导航、主题能力、登录与系统设置流程已经串成统一体验，桌面端加载背景也完成对齐。
- 📈 **分析上下文继续补强** — 美股新增社交舆情情报，A 股补齐财报与分红结构化上下文，Tushare 新接入筹码分布和行业板块涨跌数据。
- 🔒 **运行稳定性与配置兼容性提升** — 退出登录会立即让旧会话失效，定时启动兼容旧配置，运行中的 `MAX_WORKERS` 调整和新闻时效窗口反馈更清晰。
- 💼 **持仓纠错链路更完整** — 超售会被前置拦截，错误交易/资金流水/公司行为可以直接删除回滚，便于修复脏数据。

### 新功能

- 📱 **美股社交舆情情报** — 新增 Reddit / X / Polymarket 社交媒体情绪数据源，为美股分析提供实时社交热度、情绪评分和提及量等补充指标；完全可选，仅在配置 `SOCIAL_SENTIMENT_API_KEY` 后对美股生效。
- 📊 **A 股财报与分红结构化增强**（Issue #710）— `fundamental_context.earnings.data` 新增 `financial_report` 与 `dividend` 字段；分红统一按“仅现金分红、税前口径”计算，并补充 `ttm_cash_dividend_per_share` 与 `ttm_dividend_yield_pct`；分析/历史 API 的 `details` 追加 `financial_report`、`dividend_metrics` 可选字段，保持 fail-open 与向后兼容。
- 🔍 **接入 Tushare 筹码与行业板块接口** — 新增筹码分布、行业板块涨跌数据获取能力，并统一纳入配置化数据源优先级；默认按上海时间区分盘中/盘后交易日取数，优先使用 Tushare 同花顺接口，必要时降级到东财。
- 🧱 **Web UI 基础骨架升级** — 重建共享设计令牌与通用组件，新增 App Shell、Theme Provider、侧边导航，并同步调整 Electron 加载背景，为 Web / Desktop 的统一体验打底。
- 🔐 **登录与系统设置流程重做** — 重构 Login、Settings 与 Auth 管理流程，补上显式的认证 setup-state 处理，并让 Web 端与运行时认证配置 API 行为对齐。
- 🧪 **前端回归与冒烟覆盖补强** — 新增并扩展登录、首页、聊天、移动端 Shell、设置页、回测入口等关键路径的组件测试与 Playwright smoke coverage。

### 变更

- 🧭 **页面接入新 Shell 布局契约** — Home、Chat、Settings、Backtest 已统一接入新的页面容器、抽屉和滚动约定，降低 UI 迁移期间的页面行为不一致。
- 💾 **设置页状态同步更稳** — 优化草稿保留、直接保存同步与冲突处理，减少模块级保存后前后端配置状态不一致的问题。
- 🎭 **登录页视觉基线回归** — 登录页恢复到既有 `006` 分支的视觉基线，同时保留新的认证状态逻辑和统一表单交互模型。
- 🏛️ **AI 协作治理资产加固** — 收敛并加强 `AGENTS.md`、`CLAUDE.md`、Copilot 指令和校验脚本的一致性约束，降低治理资产长期漂移风险。

### Added

- **Web UI foundation refresh** — rebuilt shared design tokens and common primitives, introduced the app shell, theme provider, sidebar navigation, and Electron loading background alignment for the upgraded desktop/web experience
- **Settings and auth workflow overhaul** — rebuilt the Login, Settings, and Auth management flows, added explicit auth setup-state handling, and aligned the Web UI with the runtime auth configuration APIs
- **UI regression coverage and smoke checks** — expanded targeted frontend tests and added Playwright smoke coverage for login, home, chat, mobile shell, settings, and backtest entry flows

### Changed

- **Shell-driven page integration** — aligned Home, Chat, Settings, and Backtest with the new shell layout contract so routing, drawer behavior, and page-level scrolling are consistent during the UI migration
- **Settings state consistency** — refined draft preservation, direct-save synchronization, and conflict handling so module-level saves no longer leave the page out of sync with backend config state
- **Login visual baseline** — restored the login page visual treatment to the established `006` branch baseline while keeping the newer auth-state logic and unified form interaction model

### 修复

- ⏰ **定时启动立即执行兼容旧配置**（Issue #726）— `SCHEDULE_RUN_IMMEDIATELY` 未设置时会回退读取 `RUN_IMMEDIATELY`，修复升级后旧 `.env` 在定时模式下的兼容性问题；同时澄清 `.env.example` / README 中两个配置项的适用范围，并注明 Outlook / Exchange 强制 OAuth2 暂不支持。
- 🧵 **运行期 `MAX_WORKERS` 配置生效与可解释性增强**（#633）— 修复异步分析队列未按 `MAX_WORKERS` 同步的问题；新增任务队列并发 in-place 同步机制（空闲即时生效、繁忙延后），并在设置保存反馈与运行日志中明确输出 `profile/max/effective`，减少“参数未生效”误解。
- 🔐 **退出登录立即失效现有会话** — `POST /api/v1/auth/logout` 现在会轮换 session secret，避免旧 cookie 在退出后仍可继续访问受保护接口；同浏览器标签页和并发页面会被同步登出。认证开启时，该接口也不再属于匿名白名单，未登录请求会返回 `401`，避免匿名请求触发全局 session 失效。
- 🧮 **Tushare 板块/筹码调用限流与跨日缓存修复** — 新增的 `trade_cal`、行业板块排行、筹码分布链路统一接入 `_check_rate_limit()`；交易日历缓存改为按自然日刷新，避免服务跨天运行后继续沿用旧交易日判断取数日期。
- 💼 **持仓超售拦截与错误流水恢复**（#718）— `POST /api/v1/portfolio/trades` 现在会在写入前校验可卖数量，超售返回 `409 portfolio_oversell`；持仓页新增交易 / 资金流水 / 公司行为删除能力，删除后会同步失效仓位缓存与未来快照，便于从错误流水中直接恢复。
- 📧 **邮件中文发件人名编码**（#708）— 邮件通知现在会对包含中文的 `EMAIL_SENDER_NAME` 自动做 RFC 2047 编码，并在异常路径补充 SMTP 连接清理，修复 GitHub Actions / QQ SMTP 下 `'ascii' codec can't encode characters` 导致的发送失败。
- 🐛 **港股 Agent 实时行情去重与快速路由** — 统一 `HK01810` / `1810.HK` / `01810` 等港股代码归一规则；港股实时行情改为直接走单次 `akshare_hk` 路径，避免按 A 股 source priority 重复触发同一失败接口；Agent 运行期对显式 `retriable=false` 的工具失败增加短路缓存，减少同轮分析中的重复失败调用。
- 📰 **新闻时效硬过滤与策略分窗**（#697）— 新增 `NEWS_STRATEGY_PROFILE`（`ultra_short/short/medium/long`）并与 `NEWS_MAX_AGE_DAYS` 统一计算有效窗口；搜索结果在返回后执行发布时间硬过滤（时间未知剔除、超窗剔除、未来仅容忍 1 天），并在历史 fallback 链路追加相同约束，避免旧闻再次进入“最新动态/风险警报”。

### 文档

- ☁️ **新增云服务器 Web 界面部署与访问教程**（Fixes #686）— 补充从云端部署到外部访问的落地说明，降低远程自托管门槛。
- 🌍 **补齐英文文档索引与协作文档** — 新增英文文档索引、贡献指南、Bot 命令文档，并补充中英双语 issue / PR 模板，方便中英文协作与外部贡献者理解项目入口。
- 🏷️ **本地化 README 补充 Trendshift badge** — 在多语言 README 中同步补上新版能力入口标识，减少中英文说明面不一致。

## [3.7.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.6.0...v3.7.0) - 2026-03-15

### 新功能

- 💼 **持仓管理 P0 全功能上线**（#677，对应 Issue #627）
  - **核心账本与快照闭环**：新增账户、交易、现金流水、企业行为、持仓缓存、每日快照等核心数据模型与 API 端点；支持 FIFO / AVG 双成本法回放；同日事件顺序固定为 `现金 → 企业行为 → 交易`；持仓快照写入采用原子事务。
  - **券商 CSV 导入**：支持华泰 / 中信 / 招商首批适配，含列名别名兼容；两阶段接口（解析预览 + 确认提交）；`trade_uid` 优先、key-field hash 兜底的幂等去重；前导零股票代码完整保留。
  - **组合风险报告**：集中度风险（Top Positions + A 股板块口径）、历史回撤监控（支持回填缺失快照）、止损接近预警；多币种统一换算 CNY 口径；汲取失败时回退最近成功汇率并标记 stale。
  - **Web 持仓页**（`/portfolio`）：组合总览、持仓明细、集中度饼图、风险摘要、全组合 / 单账户切换；手工录入交易 / 资金流水 / 企业行为；内嵌账户创建入口；CSV 解析 + 提交闭环与券商选择器。
  - **Agent 持仓工具**：新增 `get_portfolio_snapshot` 数据工具，默认紧凑摘要，可选持仓明细与风险数据。
  - **事件查询 API**：新增 `GET /portfolio/trades`、`GET /portfolio/cash-ledger`、`GET /portfolio/corporate-actions`，支持日期过滤与分页。
  - **可扩展 Parser Registry**：应用级共享注册，支持运行时注册新券商；新增 `GET /portfolio/imports/csv/brokers` 发现接口。
- 🎨 **前端设计系统与原子组件库**（#662）
  - 引入渐进式双主题架构（HSL 变量化设计令牌），清理历史 Legacy CSS；重构 Button / Card / Badge / Collapsible / Input / Select 等 20+ 核心组件；新增 `clsx` + `tailwind-merge` 类名合并工具；提升历史记录、LLM 配置等页面可读性。
- ⚡ **分析 API 异步契约与启动优化**（#656）
  - 规范 `POST /api/v1/analysis/analyze` 异步请求的返回契约；优化服务启动辅助逻辑；修复前端报告类型联合定义与后端响应对齐问题。

### 修复

- 🔔 **Discord 环境变量向后兼容**（#659）：运行时新增 `DISCORD_CHANNEL_ID` → `DISCORD_MAIN_CHANNEL_ID` 的 fallback 读取；历史配置用户无需修改即可恢复 Discord Bot 通知；全部相关文档与 `.env.example` 对齐。
- 🔧 **GitHub Actions Node 24 升级**（#665）：将所有 GitHub 官方 actions 升级至 Node 24 兼容版本，消除 CI 日志中的 Node.js 20 deprecation warning（影响 2026-06-02 强制升级窗口）。
- 📅 **持仓页默认日期本地化**：手工录入表单默认日期改用本地时间（`getFullYear/Month/Date`），修复 UTC-N 时区用户在当天晚间出现日期偏移的问题。
- 🔁 **CSV 导入去重逻辑加固**：dedup hash 纳入行序号作为区分因子，确保同字段合法分笔成交不被误折叠；同时在 `trade_uid` 存在时也持久化 hash，防止混合来源重复写入。

### 变更

- `POST /api/v1/portfolio/trades` 在同账户内 `trade_uid` 冲突时返回 `409`。
- 持仓风险响应新增 `sector_concentration` 字段（增量扩展），原有 `concentration` 字段保持不变。
- 分析 API `analyze` 接口异步行为契约文档化；前端报告类型联合更新。

### 测试

- 新增持仓核心服务测试（FIFO / AVG 部分卖出、同日事件顺序、重复 `trade_uid` 返回 409、快照 API 契约）。
- 新增 CSV 导入幂等性、合法分笔成交不误去重、去重边界、风险阈值边界、汇率降级行为测试。
- 新增 Agent `get_portfolio_snapshot` 工具调用测试。
- 新增分析 API 异步契约回归测试。

## [3.6.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.5.0...v3.6.0) - 2026-03-14

### Added

- 📊 **Web UI Design System** — implemented dual-theme architecture and terminal-inspired atomic UI components
- 📊 **UI Components Refactoring** — integrated `clsx` and `tailwind-merge` for robust class composition across Web UI
- 🗑️ **History batch deletion** — Web UI now supports multi-selection and batch deletion of analysis history; added `POST /api/v1/history/batch-delete` endpoint and `ConfirmDialog` component.
- 🔐 **Auth settings API** — new `POST /api/v1/auth/settings` endpoint to enable or disable Web authentication at runtime and set the initial admin password when needed
- openclaw Skill 集成指南 — 新增 [docs/integrations/openclaw-skill-integration.md](integrations/openclaw-skill-integration.md)，说明如何通过 openclaw Skill 调用 DSA API
- ⚙️ **LLM channel protocol/test UX** — `.env` and Web settings now share the same channel shape (`LLM_CHANNELS` + `LLM_<NAME>_PROTOCOL/BASE_URL/API_KEY/MODELS/ENABLED`); settings page adds per-channel connection testing, primary/fallback/vision model selection, and protocol-aware model prefixing
- 🤖 **Agent architecture Phase 0+1** — shared protocols (`AgentContext`, `AgentOpinion`, `StageResult`), extracted `run_agent_loop()` runner, `AGENT_ARCH` switch (`single`/`multi`), config registry entries
- 🔍 **Bot NL routing** — two-layer natural-language routing: cheap regex pre-filter (stock codes + finance keywords) → lightweight LLM intent parsing; controlled by `AGENT_NL_ROUTING=true`; supports multi-stock and strategy extraction
- 💬 `**/ask` multi-stock analysis** — comma or `vs` separated codes (max 5), parallel thread execution with 150s timeout (preserves partial results), Markdown comparison summary table at top
- 📋 `**/history` command** — per-user session isolation via `{platform}_{user_id}:{scope}` format (colon delimiter prevents prefix collision); lists both `/chat` and `/ask` sessions; view detail or clear
- 📊 `**/strategies` command** — lists available strategy YAML files grouped by category (趋势/形态/反转/框架) with ✅/⬜ activation status
- 🔧 **Backtest summary tools** — `get_strategy_backtest_summary` and `get_stock_backtest_summary` registered as read-only Agent tools
- ⚙️ **Agent auto-detection** — `is_agent_available()` auto-detects from `LITELLM_MODEL`; explicit `AGENT_MODE=true/false` takes full precedence
- 🏗️ **Multi-Agent orchestrator (Phase 2)** — `AgentOrchestrator` with 4 modes (`quick`/`standard`/`full`/`strategy`); drop-in replacement for `AgentExecutor` via `AGENT_ARCH=multi`; `BaseAgent` ABC with tool subset filtering, cached data injection, and structured `AgentOpinion` output
- 🧩 **Specialised agents (Phase 2-4)** — `TechnicalAgent` (8 tools, trend/MA/MACD/volume/pattern analysis), `IntelAgent` (news & sentiment, risk flag propagation), `DecisionAgent` (synthesis into Decision Dashboard JSON), `RiskAgent` (7 risk categories, two-level severity with soft/hard override)
- 📈 **Strategy system (Phase 3)** — `StrategyAgent` (per-strategy evaluation from YAML skills), `StrategyRouter` (rule-based regime detection → strategy selection), `StrategyAggregator` (weighted consensus with backtest performance factor)
- 🔬 **Deep Research agent (Phase 5)** — `ResearchAgent` with 3-phase approach (decompose → research sub-questions → synthesise report); token budget tracking; new `/research` bot command with aliases (`/深研`, `/deepsearch`)
- 🧠 **Memory & calibration (Phase 6)** — `AgentMemory` with prediction accuracy tracking, confidence calibration (activates after minimum sample threshold), strategy auto-weighting based on historical win rate
- 📊 **Portfolio Agent (Phase 7)** — `PortfolioAgent` for multi-stock portfolio analysis (position sizing, sector concentration, correlation risk, cross-market linkage, rebalance suggestions)
- 🔔 **Event-driven alerts (Phase 7)** — `EventMonitor` with `PriceAlert`, `VolumeAlert`, `SentimentAlert` rules; async checking, callback notifications, serializable persistence
- ⚙️ **New config entries** — `AGENT_ORCHESTRATOR_MODE`, `AGENT_RISK_OVERRIDE`, `AGENT_DEEP_RESEARCH_BUDGET`, `AGENT_MEMORY_ENABLED`, `AGENT_STRATEGY_AUTOWEIGHT`, `AGENT_STRATEGY_ROUTING` — all registered in `config.py` + `config_registry.py` (WebUI-configurable)

### Changed

- 🔐 **Auth password state semantics** — stored password existence is now tracked independently from auth enablement; when auth is disabled, `/api/v1/auth/status` returns `passwordSet=false` while preserving the saved password for future re-enable
- 🔐 **Auth settings re-enable hardening** — re-enabling auth with a stored password now requires `currentPassword`, and failed session creation rolls back the auth toggle to avoid lockout
- ♻️ **AgentExecutor refactored** — `_run_loop` delegates to shared `runner.run_agent_loop()`; removed duplicated serialization/parsing/thinking-label code
- ♻️ **Unified agent switch** — Bot, API, and Pipeline all use `config.is_agent_available()` instead of divergent `config.agent_mode` checks
- 📖 **README.md** — expanded Bot commands section (ask/chat/strategies/history), added NL routing note, updated agent mode description
- 📖 **.env.example** — added `AGENT_ARCH` and `AGENT_NL_ROUTING` configuration documentation
- 🔌 **Analysis API async contract** — `POST /api/v1/analysis/analyze` now documents distinct async `202` payloads for single-stock vs batch requests, and `report_type=full` is treated consistently with the existing full-report behavior

### Fixed

- 🐛 **Analysis API blank-code guardrails** — `POST /api/v1/analysis/analyze` now drops whitespace-only entries before batch enqueue and returns `400` when no valid stock code remains
- 🐛 **Bare `/api` SPA fallback** — unknown API paths now return JSON `404` consistently for both `/api/...` and the exact `/api` path
- 🎮 **Discord channel env compatibility** — runtime now accepts legacy `DISCORD_CHANNEL_ID` as a fallback for `DISCORD_MAIN_CHANNEL_ID`, and the docs/examples now use the same variable name as the actual workflow/config implementation
- 🐛 **Session secret rotation on Windows** — use atomic replace so auth toggles invalidate existing sessions even when `.session_secret` already exists
- 🐛 **Auth toggle atomicity** — persist `ADMIN_AUTH_ENABLED` before rotating session secret; on rotation failure, roll back to the previous auth state
- 🔧 **LLM runtime selection guardrails** — YAML 模式下渠道编辑器不再覆盖 `LITELLM_MODEL` / fallback / Vision；系统配置校验补上全部渠道禁用后的运行时来源检查，并修复 `vertexai/...` 这类协议别名模型被重复加前缀的问题
- 🐛 **Multi-stock `/ask` follow-up regressions** — portfolio overlay now shares the same timeout budget as the per-stock phase and is skipped on timeout instead of blocking the bot reply; `/history` now stores the readable per-stock summary instead of raw dashboard JSON; condensed multi-stock output now renders numeric `sniper_points` values
- 🐛 **Decision dashboard enum compatibility** — multi-agent `DecisionAgent` now keeps `decision_type` within the legacy `buy|hold|sell` contract and normalizes stray `strong_*` outputs before risk override, pipeline conversion, and downstream统计/通知汇总
- 🛟 **Multi-Agent partial-result fallback** — `IntelAgent` now caches parsed intel for downstream reuse, shared JSON parsing tolerates lightly malformed model output, and the orchestrator preserves/synthesizes a minimal dashboard on timeout or mid-pipeline parse failure instead of always collapsing to `50/观望/未知`
- 🐛 **Shared LiteLLM routing restored** — bot NL intent parsing and `ResearchAgent` planning/synthesis now reuse the same LiteLLM adapter / Router / fallback / `api_base` injection path as the main Agent flow, so `LLM_CHANNELS` / `LITELLM_CONFIG` / OpenAI-compatible deployments behave consistently
- 🐛 **Bot chat session backward compatibility** — `/chat` now keeps using the legacy `{platform}_{user_id}` session id when old history already exists, and `/history` can still list / view / clear those pre-migration sessions alongside the new `{platform}_{user_id}:chat` format
- 🐛 **EventMonitor unsupported rule rejection** — config validation/runtime loading now reject or skip alert types the monitor cannot actually evaluate yet, so schedule mode no longer silently accepts permanent no-op rules
- 🐛 **P0 基本面聚合稳定性修复** (#614) — 修复 `get_stock_info` 板块语义回归（新增 `belong_boards` 并保留 `boards` 兼容别名）、引入基本面上下文精简返回以控制 token、为基本面缓存增加最大条目淘汰，并补齐 ETF 总体状态聚合与 NaN 板块字段过滤，保证 fail-open 与最小入侵。
- 🔧 **GitHub Actions 搜索引擎环境变量补充** — 工作流新增 `MINIMAX_API_KEYS`、`BRAVE_API_KEYS`、`SEARXNG_BASE_URLS` 环境变量映射，使 GitHub Actions 用户可配置 MiniMax、Brave、SearXNG 搜索服务（此前 v3.5.0 已添加 provider 实现但缺少工作流配置）
- 🤖 **Multi-Agent runtime consistency** — `AGENT_MAX_STEPS` now propagates to each orchestrated sub-agent; added cooperative `AGENT_ORCHESTRATOR_TIMEOUT_S` budget to stop overlong pipelines before they cascade further
- 🔌 **Multi-Agent feature wiring** — `AGENT_RISK_OVERRIDE` now actively downgrades final dashboards on hard risk findings; `AGENT_MEMORY_ENABLED` now injects recent analysis memory + confidence calibration into specialised agents; multi-stock `/ask` now runs `PortfolioAgent` to add portfolio-level allocation and concentration guidance
- 🔔 **EventMonitor runtime wiring** — schedule mode can now load alert rules from `AGENT_EVENT_ALERT_RULES_JSON`, poll them at `AGENT_EVENT_MONITOR_INTERVAL_MINUTES`, and send triggered alerts through the existing notification service
- 🛠️ **Follow-up stability fixes** — multi-stock `/ask` now falls back to usable text output when dashboard JSON parsing fails; EventMonitor skips semantically invalid rules instead of aborting schedule startup; background alert polling now runs independently of the main scheduled analysis loop
- 🧪 **Multi-Agent regression coverage** — added orchestrator execution tests for `run()`, `chat()`, critical-stage failure, graceful degradation, and timeout handling
- 🧹 **PortfolioAgent cleanup** — `post_process()` now reuses shared JSON parsing and removed stale unused imports
- 🚦 **Bot async dispatch** — `CommandDispatcher` now exposes `dispatch_async()`; NL intent parsing and default command execution are offloaded from the event loop, DingTalk stream awaits async handlers directly, and Feishu stream processing is moved off the SDK callback thread
- 🌐 **Async webhook handler** — new `handle_webhook_async()` function in `bot/handler.py` for use from async contexts (e.g. FastAPI); calls `dispatch_async()` directly without thread bridging
- 🧵 **Feishu stream ThreadPoolExecutor** — replaced unbounded per-message `Thread` spawning with a capped `ThreadPoolExecutor(max_workers=8)` to prevent thread explosion under message bursts
- 🔒 **EventMonitor safety** — `_check_volume()` now safely handles `get_daily_data` returning `None` (no tuple-unpacking crash); `on_trigger` callbacks support both sync and async callables via `asyncio.to_thread`/`await`
- 🧹 **ResearchAgent dedup** — `_filtered_registry()` now delegates to `BaseAgent._filtered_registry()` instead of duplicating the filtering logic
- 🧹 **Bot trailing whitespace cleanup** — removed W291/W293 whitespace issues across `bot/handler.py`, `bot/dispatcher.py`, `bot/commands/base.py`, `bot/platforms/feishu_stream.py`, `bot/platforms/dingtalk_stream.py`
- 🐛 **Dispatcher `_parse_intent_via_llm` safety** — replaced fragile `'raw' in dir()` with `'raw' in locals()` for undefined-variable guard in `JSONDecodeError` handler
- 🐛 **筹码结构 LLM 未填写时兜底补全** (#589) — DeepSeek 等模型未正确填写 `chip_structure` 时，自动用数据源已获取的筹码数据补全，保证各模型展示一致；普通分析与 Agent 模式均生效
- 🐛 **历史报告狙击点位显示原始文本** (#452) — 历史详情页现优先展示 `raw_result.dashboard.battle_plan.sniper_points` 中的原始字符串，避免 `analysis_history` 数值列把区间、说明文字或复杂点位压缩成单个数字；保留原有数值列作为回退
- 🐛 **Session prefix collision** — user ID `123` could see sessions of user `1234` via `startswith`; fixed with colon delimiter in session_id format
- 🐛 **NL pre-filter false positives** — `re.IGNORECASE` caused `[A-Z]{2,5}` to match common English words like "hello"; removed global flag, use inline `(?i:...)` only for English finance keywords
- 🐛 **Dotted ticker in strategy args** — `_get_strategy_args()` didn't recognize `BRK.B` as a stock code, leaving it in strategy text; now accepts `TICKER.CLASS` format
- ⏱️ **efinance 长调用挂起修复** (#660) — 为所有 efinance API 调用引入 `_ef_call_with_timeout()` 包装（默认 30 秒，可通过 `EFINANCE_CALL_TIMEOUT` 配置）；使用 `executor.shutdown(wait=False)` 确保超时后不再阻塞主线程，彻底消除 81 分钟挂起问题
- 🛡️ **类型安全内容完整性检查** (#660) — `check_content_integrity()` 现在将非字符串类型的 `operation_advice` / `analysis_summary` 视为缺失字段，避免下游 `get_emoji()` 因 `dict.strip()` 崩溃
- 📄 **报告保存与通知解耦** (#660) — `_save_local_report()` 不再依赖 `send_notification` 标志触发，`--no-notify` 模式下本地报告照常保存
- 🔄 **operation_advice 字典归一化** (#660) — Pipeline 和 BacktestEngine 现在将 LLM 返回的 `dict` 格式 `operation_advice` 通过 `decision_type`（不区分大小写）映射为标准字符串，防止因模型输出格式变化导致崩溃
- 🛡️ **runner.py usage None 防护** (#660) — `response.usage` 为 `None` 时不再抛出 `AttributeError`，回退为 0 token 计数
- 📋 **orchestrator 静默失败改为日志警告** (#660) — `IntelAgent` / `RiskAgent` 阶段失败现在记录 `WARNING` 而非静默跳过，便于诊断

### Notes

- ⚠️ **Multi-worker auth toggles** — runtime auth updates are process-local; multi-worker deployments must restart/roll workers to keep auth state consistent

## [3.5.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.4.10...v3.5.0) - 2026-03-12

### Added

- 📊 **Web UI full report drawer** (Fixes #214) — history page adds "Full Report" button to display the complete Markdown analysis report in a side drawer; new `GET /api/v1/history/{record_id}/markdown` endpoint
- 📊 **LLM cost tracking** — all LLM calls (analysis, agent, market review) recorded in `llm_usage` table; new `GET /api/v1/usage/summary?period=today|month|all` endpoint returns aggregated token usage by call type and model
- 🔍 **SearXNG search provider** (Fixes #550) — quota-free self-hosted search fallback; priority: Bocha > Tavily > Brave > SerpAPI > MiniMax > SearXNG
- 🔍 **MiniMax web search provider** — `MiniMaxSearchProvider` with circuit breaker (3 failures → 300s cooldown) and dual time-filtering; configured via `MINIMAX_API_KEYS`
- 🤖 **Agent models discovery API** — `GET /api/v1/agent/models` returns available model deployments (primary/fallback/source/api_base) for Web UI model selector
- 🤖 **Agent chat export & send** (#495) — export conversation to .md file; send to configured notification channels; new `POST /api/v1/agent/chat/send`
- 🤖 **Agent background execution** (#495) — analysis continues when switching pages; badge notification on completion; auto-cancel in-progress stream on session switch
- 📝 **Report Engine P0** — Pydantic schema validation for LLM JSON; Jinja2 templates (markdown/wechat/brief) with legacy fallback; content integrity checks with retry; brief mode (`REPORT_TYPE=brief`); history signal comparison
- 📦 **Smart import** — multi-source import from image/CSV/Excel/clipboard; Vision LLM extracts code+name+confidence; name→code resolver (local map + pinyin + AkShare); confidence-tiered confirmation
- ⚙️ **GitHub Actions LiteLLM config** — workflow supports `LITELLM_CONFIG`/`LITELLM_CONFIG_YAML` for flexible AI provider configuration
- ⚙️ **Config engine refactor & system API** (#602) — unified config registry, validation and API exposure
- 📖 **LLM configuration guide** — new `docs/LLM_CONFIG_GUIDE.md` covering 3-tier config, quick start, Vision/Agent/troubleshooting

### Fixed

- 🐛 **analyze_trend always reports No historical data** (#600) — now fetches from DB/DataFetcher instead of broken `get_analysis_context`
- 🐛 **Chip structure fallback when LLM omits it** (#589) — auto-fills from data source chip data for consistent display across models
- 🐛 **History sniper points show raw text** (#452) — prioritizes original strings over compressed numeric values
- 🐛 **GitHub Actions ENABLE_CHIP_DISTRIBUTION configurable** (#617) — no longer hardcoded, supports vars/secrets override
- 🐛 `**.env` save preserves comments and blank lines** — Web settings no longer destroys `.env` formatting
- 🐛 **Agent model discovery fixes** — legacy mode includes LiteLLM-native providers; source detection aligned with runtime; fallback deployments no longer expanded per-key
- 🐛 **Stooq US stock previous close semantics** — no longer misuses open price as previous close
- 🐛 **Stock name prefetch regression** — prioritizes local `STOCK_NAME_MAP` before remote queries
- 🐛 **AkShare limit-up/down calculation** (#555) — fixed market analysis statistics
- 🐛 **AkShare Tencent source field index & ETF quote mapping** (#579)
- 🐛 **Pytdx stock name cache pagination** (#573) — prevents cache overflow
- 🐛 **PushPlus oversized report chunking** (#489) — auto-segments long content
- 🐛 **Agent chat cancel & switch** (#495) — cancel no longer misreports as failure; fast switch no longer overwrites stream state
- 🐛 **MiniMax search status in `/status` command** (#587)
- 🐛 **config_registry duplicate BOCHA_API_KEYS** — removed duplicate dict entry that silently overwrote config

### Changed

- 🔎 **Fetcher failure observability** — logs record start/success/failure with elapsed time, failover transitions; Efinance/Akshare include upstream endpoint and classified failure categories
- ♻️ **Data source resilience & cleanup** (#602) — fallback chain optimization
- ♻️ **Image extract API response extension** — new `items` field (code/name/confidence); `codes` preserved for backward compatibility
- ♻️ **Import parse error messages** — specific failure reasons for Excel/CSV; improved logging with file type and size

### Docs

- 📖 LLM config guide refactored for clarity (#583)
- 📖 `modules/image-extract-prompt.md` with full prompt documentation
- 📖 AkShare fallback cache TTL documentation

## [3.4.10](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.4.9...v3.4.10) - 2026-03-07

### Fixed

- 🐛 **EfinanceFetcher ETF OHLCV data** (#541, #527) — switch `_fetch_etf_data` from `ef.fund.get_quote_history` (NAV-only, no OHLCV, no `beg`/`end` params) to `ef.stock.get_quote_history`; ETFs now return proper open/high/low/close/volume/amount instead of zeros; remove obsolete NAV column mappings from `_normalize_data`
- 🐛 **tiktoken 0.12.0 `Unknown encoding cl100k_base`** (#537) — pin `tiktoken>=0.8.0,<0.12.0` in requirements.txt to avoid plugin-registration regression introduced in 0.12.0
- 🐛 **Web UI API error classification** (#540) — frontend no longer treats every HTTP 400 as the same "server/network" failure; now distinguishes Agent disabled / missing params / model-tool incompatibility / upstream LLM errors / local connection failures
- 🐛 **北交所代码识别失败** (#491, #533) — 8/4/92 开头的 6 位代码现正确识别为北交所；Tushare/Akshare/Yfinance 等数据源支持 .BJ 或 bj 前缀；Baostock/Pytdx 对北交所代码显式切换数据源；避免误判上海 B 股 900xxx
- 🐛 **狙击点位解析错误** (#488, #532) — 理想买入/二次买入等字段在无「元」字时误提取括号内技术指标数字；现先截去第一个括号后内容再提取

### Added

- **Markdown-to-image for dashboard report** (#455, #535) — 个股日报汇总支持 markdown 转图片推送（Telegram、WeChat、Custom、Email），与大盘复盘行为一致
- **markdown-to-file engine** (#455) — `MD2IMG_ENGINE=markdown-to-file` 可选，对 emoji 支持更好，需 `npm i -g markdown-to-file`
- **PREFETCH_REALTIME_QUOTES** (#455) — 设为 `false` 可禁用实时行情预取，避免 efinance/akshare_em 全市场拉取
- **Stock name prefetch** (#455) — 分析前预取股票名称，减少报告中「股票xxxxx」占位符
- 📊 **分析报告模型标记** (#528, #534) — 在分析报告 meta、报告末尾、推送内容中展示 `model_used`（完整 LLM 模型名）；Agent 多轮调用时记录并展示每轮实际使用的模型（支持 fallback 切换）

### Changed

- **Enhanced markdown-to-image failure warning** (#455) — 转图失败时提示具体依赖（wkhtmltopdf 或 m2f）
- **WeChat-only image routing optimization** (#455) — 仅配置企业微信图片时，不再对完整报告做冗余转图，避免误导性失败日志
- **Stock name prefetch lightweight mode** (#455) — 名称预取阶段跳过 realtime quote 查询，减少额外网络开销

## [3.4.9](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.4.8...v3.4.9) - 2026-03-06

### Added

- 🧠 **Structured config validation** — `ConfigIssue` dataclass and `validate_structured()` with severity-aware logging; `CONFIG_VALIDATE_MODE=strict` aborts startup on errors
- 🖼️ **Vision model config** — `VISION_MODEL` and `VISION_PROVIDER_PRIORITY` for image stock extraction; provider fallback (Gemini → Anthropic → OpenAI → DeepSeek) when primary fails
- 🚀 **CLI init wizard** — `python -m dsa init` 3-step interactive bootstrap (model → data source → notification), 9 provider presets, incremental merge by default
- 🔧 **Multi-channel LLM support** with visual channel editor (#494)

### Changed

- ♻️ **Vision extraction** — migrated from gemini-3 hardcode to `litellm.completion()` with configurable model and provider fallback; `OPENAI_VISION_MODEL` deprecated in favor of `VISION_MODEL`
- ♻️ **Market analyzer** — uses `Analyzer.generate_text()` for LLM calls; fixes bypass and Anthropic `AttributeError` when using non-Router path
- ♻️ **Config validation refinements** — test_env output format syncs with `validate_structured` (severity-aware ✓/✗/⚠/·); Vision key warning when `VISION_MODEL` set but no provider API key; market_analyzer test covers `generate_market_review` fallback when `generate_text` returns None
- ⚙️ **Auto-tag workflow defaults to NO tag** — only tags when commit message explicitly contains `#patch`, `#minor`, or `#major`
- ♻️ **Formatter and notification refactor** (#516)

### Fixed

- 🐛 **STOCK_LIST not refreshed on scheduled runs** — `.env` or WebUI changes to `STOCK_LIST` now hot-reload before each scheduled analysis (#529)
- 🐛 **WebUI fails to load with MIME type error** — SPA fallback route now resolves correct `Content-Type` for JS/CSS files (#520)
- 🐛 **AstrBot sender docstring misplaced** — `import time` placed before docstring in `_send_astrbot`, causing it to become dead code
- 🐛 **Telegram Markdown link escaping** — `_convert_to_telegram_markdown` escaped `[]()` characters, breaking all Markdown links in reports
- 🐛 **Duplicate `discord_bot_status` field** in Config dataclass — second declaration silently shadowed the first
- 🧹 **Unused imports** — removed `shutil`/`subprocess` from `main.py`
- 🔧 **Config validation and Vision key check** (#525)

### Docs

- 📝 Clarified GitHub Actions non-trading-day manual run controls (`TRADING_DAY_CHECK_ENABLED` + `force_run`) for Issue #461 / PR #466

## [3.4.8](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.4.7...v3.4.8) - 2026-03-02

### Fixed

- 🐛 **Desktop exe crashes on startup with `FileNotFoundError`** — PyInstaller build was missing litellm's JSON data files (e.g. `model_prices_and_context_window_backup.json`). Added `--collect-data litellm` to both Windows and macOS build scripts so the files are correctly bundled in the executable.

### CI

- 🔧 Cache Electron binaries on macOS CI runners to prevent intermittent EOF download failures when fetching `electron-vX.Y.Z-darwin-*.zip` from GitHub CDN
- 🔧 Fix macOS DMG `hdiutil Resource busy` error during desktop packaging

### Docs

- 📝 Clarify non-trading-day manual run controls for GitHub Actions (`TRADING_DAY_CHECK_ENABLED` + `force_run`) (#474)

## [3.4.7](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.4.0...v3.4.7) - 2026-02-28

### Added

- 🧠 **CN/US Market Strategy Blueprint System** (#395) — market review prompt injects region-specific strategy blueprints with position sizing and risk trigger recommendations

### Fixed

- 🐛 `**TRADING_DAY_CHECK_ENABLED` env var and `--force-run` for GitHub Actions** (#466)
- 🐛 **Agent pipeline preserved resolved stock names** (#464) — placeholder names no longer leak into reports
- 🐛 **Code cleanup** (#462, Fixes #422)
- 🐛 **WebUI auto-build on startup** (#460)
- 🐛 **ARCH_ARGS unbound variable** (#458)
- 🐛 **Time zone inconsistency & right panel flash** (#439)

### Docs

- 📝 Clarify potential ambiguities in code (#343)
- 📝 ENABLE_EASTMONEY_PATCH guidance for Issue #453 (#456)

## [3.4.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.3.22...v3.4.0) - 2026-02-27

### Added

- 📡 **LiteLLM Direct Integration + Multi API Key Support** (#454, Fixes #421 #428)
  - Removed native SDKs (google-generativeai, google-genai, anthropic); unified through `litellm>=1.80.10`
  - New config: `LITELLM_MODEL`, `LITELLM_FALLBACK_MODELS`, `GEMINI_API_KEYS`, `ANTHROPIC_API_KEYS`, `OPENAI_API_KEYS`
  - Multi-key auto-builds LiteLLM Router (simple-shuffle) with 429 cooldown
  - **Breaking**: `.env` `GEMINI_MODEL` (no prefix) only for fallback; explicit config must include provider prefix

### Changed

- ♻️ **Notification Refactoring** (#435) — extracted 10 sender classes into `src/notification_sender/`

### Fixed

- 🐛 LLM NoneType crash, history API 422, sniper points extraction
- 🐛 Auto-build frontend on WebUI startup — `WEBUI_AUTO_BUILD` env var (default `true`)
- 🐛 Docker explicit project name (#448)
- 🐛 Bocha search SSL retry (#445, #446) — transient errors retry up to 3 times
- 🐛 Gemini google-genai SDK migration (Fixes #440, #444)
- 🐛 Mobile home page scrolling (Fixes #419, #433)
- 🐛 History list scroll reset (#431)
- 🐛 Settings save button false positive (fixes #417, #430)

## [3.3.22](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.3.12...v3.3.22) - 2026-02-26

### Added

- 💬 **Chat History Persistence** (Fixes #400, #414) — `/chat` page survives refresh, sidebar session list
- 🎨 Project VI Assets — logo icon set, PSD, vector, banner (#425)
- 🚀 Desktop CI Auto-Release (#426) — Windows + macOS parallel builds

### Fixed

- 🐛 Agent Reasoning 400 & LiteLLM Proxy (fixes #409, #427)
- 🐛 Discord chunked sending (#413) — `DISCORD_MAX_WORDS` config
- 🐛 yfinance shared DataFrame (#412)
- 🐛 sniper_points parsing (#408)
- 🐛 Agent framework category missing (#406)
- 🐛 Date inconsistency & query id (fixes #322, #363)

## [3.3.12](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.2.11...v3.3.12) - 2026-02-24

### Added

- 📈 **Intraday Realtime Technical Indicators** (Issue #234, #397) — MA calculated from realtime price, config: `ENABLE_REALTIME_TECHNICAL_INDICATORS`
- 🤖 **Agent Strategy Chat** (#367) — full ReAct pipeline, 11 YAML strategies, SSE streaming, multi-turn chat
- 📢 PushPlus Group Push — `PUSHPLUS_TOPIC` (#402)
- 📅 Trading Day Check (Issue #373, #375) — `TRADING_DAY_CHECK_ENABLED`, `--force-run`

### Fixed

- 🐛 DeepSeek reasoning mode (Issue #379, #386)
- 🐛 Agent news intel persistence (Fixes #396, #405)
- 🐛 Bare except clauses replaced with `except Exception` (#398)
- 🐛 UUID fallback for HTTP non-secure context (fixes #377, #381)
- 🐛 Docker DNS resolution (Fixes #372, #374)
- 🐛 Agent session/strategy bugs — multiple follow-up fixes for #367
- 🐛 yfinance parallel download data filtering

### Changed

- Market review strategy consistency — unified cn/us template
- Agent test assertions updated (`6 -> 11`)

## [3.2.11](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v3.2.10...v3.2.11) - 2026-02-23

### 修复（#patch）

- 🐛 **StockTrendAnalyzer 从未执行** (Issue #357)
  - 根因：`get_analysis_context` 仅返回 2 天数据且无 `raw_data`，pipeline 中 `raw_data in context` 始终为 False
  - 修复：Step 3 直接调用 `get_data_range` 获取 90 日历天（约 60 交易日）历史数据用于趋势分析
  - 改善：趋势分析失败时用 `logger.warning(..., exc_info=True)` 记录完整 traceback

## [3.2.10] - 2026-02-22

### 新增

- ⚙️ 支持 `RUN_IMMEDIATELY` 配置项，设为 `true` 时定时任务触发后立即执行一次分析，无需等待首个定时点

### 修复

- 🐛 修复 Web UI 页面居中问题
- 🐛 修复 Settings 返回 500 错误

## [3.2.9] - 2026-02-22

### 修复

- 🐛 **ETF 分析仅关注指数走势**（Issue #274）
  - 美股/港股 ETF（如 VOO、QQQ）与 A 股 ETF 不再纳入基金公司层面风险（诉讼、声誉等）
  - 搜索维度：ETF/指数专用 risk_check、earnings、industry 查询，避免命中基金管理人新闻
  - AI 提示：指数型标的分析约束，`risk_alerts` 不得出现基金管理人公司经营风险

## [3.2.8] - 2026-02-21

### 修复

- 🐛 **BOT 与 WEB UI 股票代码大小写统一**（Issue #355）
  - BOT `/analyze` 与 WEB UI 触发分析的股票代码统一为大写（如 `aapl` → `AAPL`）
  - 新增 `canonical_stock_code()`，在 BOT、API、Config、CLI、task_queue 入口处规范化
  - 历史记录与任务去重逻辑可正确识别同一股票（大小写不再影响）

## [3.2.7] - 2026-02-20

### 新增

- 🔐 **Web 页面密码验证**（Issue #320, #349）
  - 支持 `ADMIN_AUTH_ENABLED=true` 启用 Web 登录保护
  - 首次访问在网页设置初始密码；支持「系统设置 > 修改密码」和 CLI `python -m src.auth reset_password` 重置

## [3.2.6] - 2026-02-20

### ⚠️ 破坏性变更（Breaking Changes）

- **历史记录 API 变更 (Issue #322)**
  - 路由变更：`GET /api/v1/history/{query_id}` → `GET /api/v1/history/{record_id}`
  - 参数变更：`query_id` (字符串) → `record_id` (整数)
  - 新闻接口变更：`GET /api/v1/history/{query_id}/news` → `GET /api/v1/history/{record_id}/news`
  - 原因：`query_id` 在批量分析时可能重复，无法唯一标识单条历史记录。改用数据库主键 `id` 确保唯一性
  - 影响范围：使用旧版历史详情 API 的所有客户端需同步更新

### 修复

- 修复美股（如 ADBE）技术指标矛盾：akshare 美股复权数据异常，统一美股历史数据源为 YFinance（Issue #311）
- 🐛 **历史记录查询和显示问题 (Issue #322)**
  - 修复历史记录列表查询中日期不一致问题：使用明天作为 endDate，确保包含今天全天的数据
  - 修复服务器 UI 报告选择问题：原因是多条记录共享同一 `query_id`，导致总是显示第一条。现改用 `analysis_history.id` 作为唯一标识
  - 历史详情、新闻接口及前端组件已全面适配 `record_id`
  - 新增后台轮询（每 30s）与页面可见性变更时静默刷新历史列表，确保 CLI 发起的分析完成后前端能及时同步，使用 `silent` 模式避免触发 loading 状态
- 🐛 **美股指数实时行情与日线数据** (Issue #273)
  - 修复 SPX、DJI、IXIC、NDX、VIX、RUT 等美股指数无法获取实时行情的问题
  - 新增 `us_index_mapping` 模块，将用户输入（如 SPX）映射为 Yahoo Finance 符号（如 ^GSPC）
  - 美股指数与美股股票日线数据直接路由至 YfinanceFetcher，避免遍历不支持的数据源
  - 消除重复的美股识别逻辑，统一使用 `is_us_stock_code()` 函数

### 优化

- 🎨 **首页输入栏与 Market Sentiment 布局对齐优化**
  - 股票代码输入框左缘与历史记录 glass-card 框左对齐
  - 分析按钮右缘与 Market Sentiment 外框右对齐
  - Market Sentiment 卡片向下拉伸填满格子，消除与 STRATEGY POINTS 之间的空隙
  - 窄屏时输入栏填满宽度，响应式对齐保持一致

## [3.2.5] - 2026-02-19

### 新增

- 🌍 **大盘复盘可选区域**（Issue #299）
  - 支持 `MARKET_REVIEW_REGION` 环境变量：`cn`（A股）、`us`（美股）、`both`（两者）
  - us 模式使用 SPX/纳斯达克/道指/VIX 等指数；both 模式可同时复盘 A 股与美股
  - 默认 `cn`，保持向后兼容

## [3.2.4] - 2026-02-18

### 修复

- 🐛 **统一美股数据源为 YFinance**（Issue #311）
  - akshare 美股复权数据异常，统一美股历史数据源为 YFinance
  - 修复 ADBE 等美股股票技术指标矛盾问题

## [3.2.3] - 2026-02-18

### 修复

- 🐛 **标普500实时数据缺失**（Issue #273）
  - 修复 SPX、DJI、IXIC、NDX、VIX、RUT 等美股指数无法获取实时行情的问题
  - 新增 `us_index_mapping` 模块，将用户输入（如 SPX）映射为 Yahoo Finance 符号（如 `^GSPC`）
  - 美股指数与美股股票日线数据直接路由至 YfinanceFetcher，避免遍历不支持的数据源

## [3.2.2] - 2026-02-16

### 新增

- 📊 **PE 指标支持**（Issue #296）
  - AI System Prompt 增加 PE 估值关注
- 📰 **新闻时效性筛查**（Issue #296）
  - `NEWS_MAX_AGE_DAYS`：新闻最大时效（天），默认 3，避免使用过时信息
- 📈 **强势趋势股乖离率放宽**（Issue #296）
  - `BIAS_THRESHOLD`：乖离率阈值（%），默认 5.0，可配置
  - 强势趋势股（多头排列且趋势强度 ≥70）自动放宽乖离率到 1.5 倍

## [3.2.1] - 2026-02-16

### 新增

- 🔧 **东财接口补丁可配置开关**
  - 支持 `EFINANCE_PATCH_ENABLED` 环境变量开关东财接口补丁（默认 `true`）
  - 补丁不可用时可降级关闭，避免影响主流程

## [3.2.0] - 2026-02-15

### 新增

- 🔒 **CI 门禁统一（P0）**
  - 新增 `scripts/ci_gate.sh` 作为后端门禁单一入口
  - 主 CI 改为 `backend-gate`、`docker-build`、`web-gate` 三段式
  - CI 触发改为所有 PR，避免 Required Checks 因路径过滤缺失而卡住合并
  - `web-gate` 支持前端路径变更按需触发
  - 新增 `network-smoke` 工作流承载非阻断网络场景回归
- 📦 **发布链路收敛（P0）**
  - `docker-publish` 调整为 tag 主触发，并增加发布前门禁校验
  - 手动发布增加 `release_tag` 输入与 semver/changelog 强校验
  - 发布前新增 Docker smoke（关键模块导入）
- 📝 **PR 模板升级（P0）**
  - 增加背景、范围、验证命令与结果、回滚方案、Issue 关联等必填项
- 🤖 **AI 审查覆盖增强（P0）**
  - `pr-review` 纳入 `.github/workflows/`** 范围
  - 新增 `AI_REVIEW_STRICT` 开关，可选将 AI 审查失败升级为阻断

## [3.1.13] - 2026-02-15

### 新增

- 📊 **仅分析结果摘要**（Issue #262）
  - 支持 `REPORT_SUMMARY_ONLY` 环境变量，设为 `true` 时只推送汇总，不含个股详情
  - 默认 `false`，多股时适合快速浏览

## [3.1.12] - 2026-02-15

### 新增

- 📧 **个股与大盘复盘合并推送**（Issue #190）
  - 支持 `MERGE_EMAIL_NOTIFICATION` 环境变量，设为 `true` 时将个股分析与大盘复盘合并为一次推送
  - 默认 `false`，减少邮件数量、降低被识别为垃圾邮件的风险

## [3.1.11] - 2026-02-15

### 新增

- 🤖 **Anthropic Claude API 支持**（Issue #257）
  - 支持 `ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL`、`ANTHROPIC_TEMPERATURE`、`ANTHROPIC_MAX_TOKENS`
  - AI 分析优先级：Gemini > Anthropic > OpenAI
- 📷 **从图片识别股票代码**（Issue #257）
  - 上传自选股截图，通过 Vision LLM 自动提取股票代码
  - API: `POST /api/v1/stocks/extract-from-image`；支持 JPEG/PNG/WebP/GIF，最大 5MB
  - 支持 `OPENAI_VISION_MODEL` 单独配置图片识别模型
- ⚙️ **通达信数据源手动配置**（Issue #257）
  - 支持 `PYTDX_HOST`、`PYTDX_PORT` 或 `PYTDX_SERVERS` 配置自建通达信服务器

## [3.1.10] - 2026-02-15

### 新增

- ⚙️ **立即运行配置**（Issue #332）
  - 支持 `RUN_IMMEDIATELY` 环境变量，`true` 时定时任务启动后立即执行一次
- 🐛 修复 Docker 构建问题

## [3.1.9] - 2026-02-14

### 新增

- 🔌 **东财接口补丁机制**
  - 新增 `patch/eastmoney_patch.py` 修复 efinance 上游接口变更
  - 不影响其他数据源的正常运行

## [3.1.8] - 2026-02-14

### 新增

- 🔐 **Webhook 证书校验开关**（Issue #265）
  - 支持 `WEBHOOK_VERIFY_SSL` 环境变量，可关闭 HTTPS 证书校验以支持自签名证书
  - 默认保持校验，关闭存在 MITM 风险，仅建议在可信内网使用

## [3.1.7] - 2026-02-14

### 修复

- 🐛 修复包导入错误（package import error）

## [3.1.6] - 2026-02-13

### 修复

- 🐛 修复 `news_intel` 中 `query_id` 不一致问题

## [3.1.5] - 2026-02-13

### 新增

- 📷 **Markdown 转图片通知**（Issue #289）
  - 支持 `MARKDOWN_TO_IMAGE_CHANNELS` 配置，对 Telegram、企业微信、自定义 Webhook（Discord）、邮件发送图片格式报告
  - 邮件为内联附件，增强对不支持 HTML 客户端的兼容性
  - 需安装 `wkhtmltopdf` 和 `imgkit`

## [3.1.4] - 2026-02-12

### 新增

- 📧 **股票分组发往不同邮箱**（Issue #268）
  - 支持 `STOCK_GROUP_N` + `EMAIL_GROUP_N` 配置，不同股票组报告发送到对应邮箱
  - 大盘复盘发往所有配置的邮箱

## [3.1.3] - 2026-02-12

### 修复

- 🐛 修复 Docker 内运行时通过页面修改配置报错 `[Errno 16] Device or resource busy` 的问题

## [3.1.2] - 2026-02-11

### 修复

- 🐛 修复 Docker 一致性问题，解决关键批次处理与通知 Bug

## [3.1.1] - 2026-02-11

### 变更

- ♻️ `API_HOST` → `WEBUI_HOST`：Docker Compose 配置项统一

## [3.1.0] - 2026-02-11

### 新增

- 📊 **ETF 支持增强与代码规范化**
  - 统一各数据源 ETF 代码处理逻辑
  - 新增 `canonical_stock_code()` 统一代码格式，确保数据源路由正确

## [3.0.5] - 2026-02-08

### 修复

- 🐛 修复信号 emoji 与建议不一致的问题（复合建议如"卖出/观望"未正确映射）
- 🐛 修复 `*ST` 股票名在微信/Dashboard 中 markdown 转义问题
- 🐛 修复 `idx.amount` 为 None 时大盘复盘 TypeError
- 🐛 修复分析 API 返回 `report=None` 及 ReportStrategy 类型不一致问题
- 🐛 修复 Tushare 返回类型错误（dict → UnifiedRealtimeQuote）及 API 端点指向

### 新增

- 📊 大盘复盘报告注入结构化数据（涨跌统计、指数表格、板块排名）
- 🔍 搜索结果 TTL 缓存（500 条上限，FIFO 淘汰）
- 🔧 Tushare Token 存在时自动注入实时行情优先级
- 📰 新闻摘要截断长度 50→200 字

### 优化

- ⚡ 补充行情字段请求限制为最多 1 次，减少无效请求

## [3.0.4] - 2026-02-07

### 新增

- 📈 **回测引擎** (PR #269)
  - 新增基于历史分析记录的回测系统，支持收益率、胜率、最大回撤等指标评估
  - WebUI 集成回测结果展示

## [3.0.3] - 2026-02-07

### 修复

- 🐛 修复狙击点位数据解析错误问题 (PR #271)

## [3.0.2] - 2026-02-06

### 新增

- ✉️ 可配置邮件发送者名称 (PR #272)
- 🌐 外国股票支持英文关键词搜索

## [3.0.1] - 2026-02-06

### 修复

- 🐛 修复 ETF 实时行情获取、市场数据回退、企业微信消息分块问题
- 🔧 CI 流程简化

## [3.0.0] - 2026-02-06

### 移除

- 🗑️ **移除旧版 WebUI**
  - 删除基于 `http.server.ThreadingHTTPServer` 的旧版 WebUI（`web/` 包）
  - 旧版 WebUI 的功能已完全被 FastAPI（`api/`）+ React 前端替代
  - `--webui` / `--webui-only` 命令行参数标记为弃用，自动重定向到 `--serve` / `--serve-only`
  - `WEBUI_ENABLED` / `WEBUI_HOST` / `WEBUI_PORT` 环境变量保持兼容，自动转发到 FastAPI 服务
  - `webui.py` 保留为兼容入口，启动时直接调用 FastAPI 后端
  - Docker Compose 中移除 `webui` 服务定义，统一使用 `server` 服务

### 变更

- ♻️ **服务层重构**
  - 将 `web/services.py` 中的异步任务服务迁移至 `src/services/task_service.py`
  - Bot 分析命令（`bot/commands/analyze.py`）改为使用 `src.services.task_service`
  - Docker 环境变量 `WEBUI_HOST`/`WEBUI_PORT` 更名为 `API_HOST`/`API_PORT`（旧名仍兼容）

## [2.3.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.5...v2.3.0) - 2026-02-01

### 新增

- 🇺🇸 **增强美股支持** (Issue #153)
  - 实现基于 Akshare 的美股历史数据获取 (`ak.stock_us_daily()`)
  - 实现基于 Yfinance 的美股实时行情获取（优先策略）
  - 增加对不支持数据源（Tushare/Baostock/Pytdx/Efinance）的美股代码过滤和快速降级

### 修复

- 🐛 修复 AMD 等美股代码被误识别为 A 股的问题 (Issue #153)

## [2.2.5](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.4...v2.2.5) - 2026-02-01

### 新增

- 🤖 **AstrBot 消息推送** (PR #217)
  - 新增 AstrBot 通知渠道，支持推送到 QQ 和微信
  - 支持 HMAC SHA256 签名验证，确保通信安全
  - 通过 `ASTRBOT_URL` 和 `ASTRBOT_TOKEN` 配置

## [2.2.4](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.3...v2.2.4) - 2026-02-01

### 新增

- ⚙️ **可配置数据源优先级** (PR #215)
  - 支持通过环境变量（如 `YFINANCE_PRIORITY=0`）动态调整数据源优先级
  - 无需修改代码即可优先使用特定数据源（如 Yahoo Finance）

## [2.2.3](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.2...v2.2.3) - 2026-01-31

### 修复

- 📦 更新 requirements.txt，增加 `lxml_html_clean` 依赖以解决兼容性问题

## [2.2.2](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.1...v2.2.2) - 2026-01-31

### 修复

- 🐛 修复代理配置区分大小写问题 (fixes #211)

## [2.2.1](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.2.0...v2.2.1) - 2026-01-31

### 修复

- 🐛 **YFinance 兼容性修复** (PR #210, fixes #209)
  - 修复新版 yfinance 返回 MultiIndex 列名导致的数据解析错误

## [2.2.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.14...v2.2.0) - 2026-01-31

### 新增

- 🔄 **多源回退策略增强**
  - 实现了更健壮的数据获取回退机制 (feat: multi-source fallback strategy)
  - 优化了数据源故障时的自动切换逻辑

### 修复

- 🐛 修复 analyzer 运行后无法通过改 .env 文件的 stock_list 内容调整跟踪的股票

## [2.1.14](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.13...v2.1.14) - 2026-01-31

### 文档

- 📝 更新 README 和优化 auto-tag 规则

## [2.1.13](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.12...v2.1.13) - 2026-01-31

### 修复

- 🐛 **Tushare 优先级与实时行情** (Fixed #185)
  - 修复 Tushare 数据源优先级设置问题
  - 修复 Tushare 实时行情获取功能

## [2.1.12](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.11...v2.1.12) - 2026-01-30

### 修复

- 🌐 修复代理配置在某些情况下的区分大小写问题
- 🌐 修复本地环境禁用代理的逻辑

## [2.1.11](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.10...v2.1.11) - 2026-01-30

### 优化

- 🚀 **飞书消息流优化** (PR #192)
  - 优化飞书 Stream 模式的消息类型处理
  - 修改 Stream 消息模式默认为关闭，防止配置错误运行时报错

## [2.1.10](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.9...v2.1.10) - 2026-01-30

### 合并

- 📦 合并 PR #154 贡献

## [2.1.9](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.8...v2.1.9) - 2026-01-30

### 新增

- 💬 **微信文本消息支持** (PR #137)
  - 新增微信推送的纯文本消息类型支持
  - 添加 `WECHAT_MSG_TYPE` 配置项

## [2.1.8](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.7...v2.1.8) - 2026-01-30

### 修复

- 🐛 修正日志中 API 提供商显示错误 (PR #197)

## [2.1.7](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.6...v2.1.7) - 2026-01-30

### 修复

- 🌐 禁用本地环境的代理设置，避免网络连接问题

## [2.1.6](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.5...v2.1.6) - 2026-01-29

### 新增

- 📡 **Pytdx 数据源 (Priority 2)**
  - 新增通达信数据源，免费无需注册
  - 多服务器自动切换
  - 支持实时行情和历史数据
- 🏷️ **多源股票名称解析**
  - DataFetcherManager 新增 `get_stock_name()` 方法
  - 新增 `batch_get_stock_names()` 批量查询
  - 自动在多数据源间回退
  - Tushare 和 Baostock 新增股票名称/列表方法
- 🔍 **增强搜索回退**
  - 新增 `search_stock_price_fallback()` 用于数据源全部失败时
  - 新增搜索维度：市场分析、行业分析
  - 最大搜索次数从 3 增加到 5
  - 改进搜索结果格式（每维度 4 条结果）

### 改进

- 更新搜索查询模板以提高相关性
- 增强 `format_intel_report()` 输出结构

## [2.1.5](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.4...v2.1.5) - 2026-01-29

### 新增

- 📡 新增 Pytdx 数据源和多源股票名称解析功能

## [2.1.4](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.3...v2.1.4) - 2026-01-29

### 文档

- 📝 更新赞助商信息

## [2.1.3](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.2...v2.1.3) - 2026-01-28

### 文档

- 📝 重构 README 布局
- 🌐 新增繁体中文翻译 (i18n/README_CHT.md)

### 修复

- 🐛 修复 WebUI 无法输入美股代码问题
  - 输入框逻辑改成所有字母都转换成大写
  - 支持 `.` 的输入（如 `BRK.B`）

## [2.1.2](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.1...v2.1.2) - 2026-01-27

### 修复

- 🐛 修复个股分析推送失败和报告路径问题 (fixes #166)
- 🐛 修改 CR 错误，确保微信消息最大字节配置生效

## [2.1.1](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.1.0...v2.1.1) - 2026-01-26

### 新增

- 🔧 添加 GitHub Actions auto-tag 工作流
- 📡 添加 yfinance 兜底数据源及数据缺失警告

### 修复

- 🐳 修复 docker-compose 路径和文档命令
- 🐳 Dockerfile 补充 copy src 文件夹 (fixes #145)

## [2.1.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v2.0.0...v2.1.0) - 2026-01-25

### 新增

- 🇺🇸 **美股分析支持**
  - 支持美股代码直接输入（如 `AAPL`, `TSLA`）
  - 使用 YFinance 作为美股数据源
- 📈 **MACD 和 RSI 技术指标**
  - MACD：趋势确认、金叉死叉信号（零轴上金叉⭐、金叉✅、死叉❌）
  - RSI：超买超卖判断（超卖⭐、强势✅、超买⚠️）
  - 指标信号纳入综合评分系统
- 🎮 **Discord 推送支持** (PR #124, #125, #144)
  - 支持 Discord Webhook 和 Bot API 两种方式
  - 通过 `DISCORD_WEBHOOK_URL` 或 `DISCORD_BOT_TOKEN` + `DISCORD_MAIN_CHANNEL_ID` 配置
- 🤖 **机器人命令交互**
  - 钉钉机器人支持 `/分析 股票代码` 命令触发分析
  - 支持 Stream 长连接模式
- 🌡️ **AI 温度参数可配置** (PR #142)
  - 支持自定义 AI 模型温度参数
- 🐳 **Zeabur 部署支持**
  - 添加 Zeabur 镜像部署工作流
  - 支持 commit hash 和 latest 双标签

### 重构

- 🏗️ **项目结构优化**
  - 核心代码移至 `src/` 目录，根目录更清爽
  - 文档移至 `docs/` 目录
  - Docker 配置移至 `docker/` 目录
  - 修复所有 import 路径，保持向后兼容
- 🔄 **数据源架构升级**
  - 新增数据源熔断机制，单数据源连续失败自动切换
  - 实时行情缓存优化，批量预取减少 API 调用
  - 网络代理智能分流，国内接口自动直连
- 🤖 Discord 机器人重构为平台适配器架构

### 修复

- 🌐 **网络稳定性增强**
  - 自动检测代理配置，对国内行情接口强制直连
  - 修复 EfinanceFetcher 偶发的 `ProtocolError`
  - 增加对底层网络错误的捕获和重试机制
- 📧 **邮件渲染优化**
  - 修复邮件中表格不渲染问题 (#134)
  - 优化邮件排版，更紧凑美观
- 📢 **企业微信推送修复**
  - 修复大盘复盘推送不完整问题
  - 增强消息分割逻辑，支持更多标题格式
  - 增加分批发送间隔，避免限流丢失
- 👷 **CI/CD 修复**
  - 修复 GitHub Actions 中路径引用的错误

## [2.0.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.6.0...v2.0.0) - 2026-01-24

### 新增

- 🇺🇸 **美股分析支持**
  - 支持美股代码直接输入（如 `AAPL`, `TSLA`）
  - 使用 YFinance 作为美股数据源
- 🤖 **机器人命令交互** (PR #113)
  - 钉钉机器人支持 `/分析 股票代码` 命令触发分析
  - 支持 Stream 长连接模式
  - 支持选择精简报告或完整报告
- 🎮 **Discord 推送支持** (PR #124)
  - 支持 Discord Webhook 推送
  - 添加 Discord 环境变量到工作流

### 修复

- 🐳 修复 WebUI 在 Docker 中绑定 0.0.0.0 (fixed #118)
- 🔔 修复飞书长连接通知问题
- 🐛 修复 `analysis_delay` 未定义错误
- 🔧 启动时 config.py 检测通知渠道，修复已配置自定义渠道情况下仍然提示未配置问题

### 改进

- 🔧 优化 Tushare 优先级判断逻辑，提升封装性
- 🔧 修复 Tushare 优先级提升后仍排在 Efinance 之后的问题
- ⚙️ 配置 TUSHARE_TOKEN 时自动提升 Tushare 数据源优先级
- ⚙️ 实现 4 个用户反馈 issue (#112, #128, #38, #119)

## [1.6.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.5.0...v1.6.0) - 2026-01-19

### 新增

- 🖥️ WebUI 管理界面及 API 支持（PR #72）
  - 全新 Web 架构：分层设计（Server/Router/Handler/Service）
  - 核心 API：支持 `/analysis` (触发分析), `/tasks` (查询进度), `/health` (健康检查)
  - 交互界面：支持页面直接输入代码并触发分析，实时展示进度
  - 运行模式：新增 `--webui-only` 模式，仅启动 Web 服务
  - 解决了 [#70](https://github.com/ZhuLinsen/daily_stock_analysis/issues/70) 的核心需求（提供触发分析的接口）
- ⚙️ GitHub Actions 配置灵活性增强（[#79](https://github.com/ZhuLinsen/daily_stock_analysis/issues/79)）
  - 支持从 Repository Variables 读取非敏感配置（如 STOCK_LIST, GEMINI_MODEL）
  - 保持对 Secrets 的向下兼容

### 修复

- 🐛 修复企业微信/飞书报告截断问题（[#73](https://github.com/ZhuLinsen/daily_stock_analysis/issues/73)）
  - 移除 notification.py 中不必要的长度硬截断逻辑
  - 依赖底层自动分片机制处理长消息
- 🐛 修复 GitHub Workflow 环境变量缺失（[#80](https://github.com/ZhuLinsen/daily_stock_analysis/issues/80)）
  - 修复 `CUSTOM_WEBHOOK_BEARER_TOKEN` 未正确传递到 Runner 的问题

## [1.5.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.4.0...v1.5.0) - 2026-01-17

### 新增

- 📲 单股推送模式（[#55](https://github.com/ZhuLinsen/daily_stock_analysis/issues/55)）
  - 每分析完一只股票立即推送，不用等全部分析完
  - 命令行参数：`--single-notify`
  - 环境变量：`SINGLE_STOCK_NOTIFY=true`
- 🔐 自定义 Webhook Bearer Token 认证（[#51](https://github.com/ZhuLinsen/daily_stock_analysis/issues/51)）
  - 支持需要 Token 认证的 Webhook 端点
  - 环境变量：`CUSTOM_WEBHOOK_BEARER_TOKEN`

## [1.4.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.3.0...v1.4.0) - 2026-01-17

### 新增

- 📱 Pushover 推送支持（PR #26）
  - 支持 iOS/Android 跨平台推送
  - 通过 `PUSHOVER_USER_KEY` 和 `PUSHOVER_API_TOKEN` 配置
- 🔍 博查搜索 API 集成（PR #27）
  - 中文搜索优化，支持 AI 摘要
  - 通过 `BOCHA_API_KEYS` 配置
- 📊 Efinance 数据源支持（PR #59）
  - 新增 efinance 作为数据源选项
- 🇭🇰 港股支持（PR #17）
  - 支持 5 位代码或 HK 前缀（如 `hk00700`、`hk1810`）

### 修复

- 🔧 飞书 Markdown 渲染优化（PR #34）
  - 使用交互卡片和格式化器修复渲染问题
- ♻️ 股票列表热重载（PR #42 修复）
  - 分析前自动重载 `STOCK_LIST` 配置
- 🐛 钉钉 Webhook 20KB 限制处理
  - 长消息自动分块发送，避免被截断
- 🔄 AkShare API 重试机制增强
  - 添加失败缓存，避免重复请求失败接口

### 改进

- 📝 README 精简优化
  - 高级配置移至 `docs/full-guide.md`

## [1.3.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.2.0...v1.3.0) - 2026-01-12

### 新增

- 🔗 自定义 Webhook 支持
  - 支持任意 POST JSON 的 Webhook 端点
  - 自动识别钉钉、Discord、Slack、Bark 等常见服务格式
  - 支持配置多个 Webhook（逗号分隔）
  - 通过 `CUSTOM_WEBHOOK_URLS` 环境变量配置

### 修复

- 📝 企业微信长消息分批发送
  - 解决自选股过多时内容超过 4096 字符限制导致推送失败的问题
  - 智能按股票分析块分割，每批添加分页标记（如 1/3, 2/3）
  - 批次间隔 1 秒，避免触发频率限制

## [1.2.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.1.0...v1.2.0) - 2026-01-11

### 新增

- 📢 多渠道推送支持
  - 企业微信 Webhook
  - 飞书 Webhook（新增）
  - 邮件 SMTP（新增）
  - 自动识别渠道类型，配置更简单

### 改进

- 统一使用 `NOTIFICATION_URL` 配置，兼容旧的 `WECHAT_WEBHOOK_URL`
- 邮件支持 Markdown 转 HTML 渲染

## [1.1.0](https://github.com/ZhuLinsen/daily_stock_analysis/compare/v1.0.0...v1.1.0) - 2026-01-11

### 新增

- 🤖 OpenAI 兼容 API 支持
  - 支持 DeepSeek、通义千问、Moonshot、智谱 GLM 等
  - Gemini 和 OpenAI 格式二选一
  - 自动降级重试机制

## [1.0.0](https://github.com/ZhuLinsen/daily_stock_analysis/releases/tag/v1.0.0) - 2026-01-10

### 新增

- 🎯 AI 决策仪表盘分析
  - 一句话核心结论
  - 精确买入/止损/目标点位
  - 检查清单（✅⚠️❌）
  - 分持仓建议（空仓者 vs 持仓者）
- 📊 大盘复盘功能
  - 主要指数行情
  - 涨跌统计
  - 板块涨跌榜
  - AI 生成复盘报告
- 🔍 多数据源支持
  - AkShare（主数据源，免费）
  - Tushare Pro
  - Baostock
  - YFinance
- 📰 新闻搜索服务
  - Tavily API
  - SerpAPI
- 💬 企业微信机器人推送
- ⏰ 定时任务调度
- 🐳 Docker 部署支持
- 🚀 GitHub Actions 零成本部署

### 技术特性

- Gemini AI 模型（gemini-3-flash-preview）
- 429 限流自动重试 + 模型切换
- 请求间延时防封禁
- 多 API Key 负载均衡
- SQLite 本地数据存储

---
