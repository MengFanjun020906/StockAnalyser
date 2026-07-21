# 新闻信号维护与 Graphiti Repair

新闻信号关系型表是页面和重建的真源，Neo4j 是可重建投影。日常维护默认优先确定性事件与显式关系，不应让 Graphiti Core 的慢 LLM episode 抽取阻塞盘后数据任务。

## 启用盘后维护

在 `.env` 中配置：

```bash
NEWS_SIGNAL_DAILY_ENABLED=true
NEWS_SIGNAL_GRAPH_REPAIR_MODE=edges
NEWS_SIGNAL_GRAPH_REPAIR_LIMIT=100
NEWS_SIGNAL_BACKFILL_LIMIT=500
NEWS_SIGNAL_MAINTENANCE_TIMEOUT_SECONDS=300
NEWS_SIGNAL_INCLUDE_SEMANTIC_EDGES=false
NEWS_SIGNAL_CLS_INCREMENTAL_ENABLED=true
NEWS_SIGNAL_CLS_INCREMENTAL_INTERVAL_MINUTES=10
NEWS_SIGNAL_CLS_INCREMENTAL_LIMIT=50
GRAPHITI_OUTBOX_WORKER_ENABLED=true
GRAPHITI_OUTBOX_INTERVAL_SECONDS=60
GRAPHITI_OUTBOX_BATCH_SIZE=10
GRAPHITI_OUTBOX_MAX_ATTEMPTS=5
GRAPHITI_OUTBOX_RETRY_BASE_SECONDS=30
GRAPHITI_OUTBOX_JOB_TIMEOUT_SECONDS=120
NEWS_EVENT_SENTINEL_ENABLED=true
NEWS_EVENT_SENTINEL_INTERVAL_MINUTES=30
NEWS_EVENT_SENTINEL_ACTIVE_WINDOWS=08:00-02:30
NEWS_EVENT_SENTINEL_MAX_ITEMS_PER_SOURCE=50
NEWS_EVENT_SENTINEL_CARD_MAX_AGE_MINUTES=30
NEWS_EVENT_SENTINEL_MIN_SEVERITY=mid
NEWS_EVENT_SENTINEL_COOLDOWN_MINUTES=120
NEWS_EVENT_SENTINEL_TRIGGER_MODE=notify_only
NEWS_EVENT_SENTINEL_FEISHU_ENABLED=true
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/your_key_here
# FEISHU_WEBHOOK_SECRET=your_secret_if_enabled
# FEISHU_WEBHOOK_KEYWORD=StockAnalyser
```

随后执行：

```bash
bash scripts/daily_run.sh
```

第三步会依次执行当日卡片重建、公司映射修复、同事件归并、缺失事件回填、seed outcome 刷新、Graphiti repair 和 outbox 消费。每个交易日有独立续跑标记。
事件回填出现部分失败，或明确请求的 Graphiti repair 未成功时，维护命令返回非零退出码，第三步不会写完成标记，后续续跑会重试。

`python main.py --schedule` 还会注册两个独立后台任务：财联社电报按 5-10 分钟增量入库，Graphiti outbox 按秒级间隔分批消费。财联社电报主源使用 `https://www.cls.cn/v1/roll/get_roll_list` 签名接口，`https://orz.ai/api/v1/dailynews/?platform=cls` 仅作为兜底；高峰期建议保持 `NEWS_SIGNAL_CLS_INCREMENTAL_LIMIT=50` 与 `NEWS_EVENT_SENTINEL_MAX_ITEMS_PER_SOURCE=50`，避免只扫首页少量快讯导致漏抓。关系库写入不等待 Graphiti；episode 超时后进入指数退避，删除任务优先于写入任务。

启用 `NEWS_EVENT_SENTINEL_ENABLED=true` 后，schedule 模式会额外注册 `news_event_sentinel` 后台任务。第一版底层哨兵使用 Portfolio active positions 与 `STOCK_LIST` 构建关注宇宙，复用现有 CLS 增量入库和 `NewsSignalCard` 读取路径，再按 active window、card freshness、severity、company mapping、macro market、cooldown 生成 `news_event_sentinel_runs` 与 `news_event_sentinel_triggers` 审计。公司级事件要求命中持仓/自选；宏观事件可在命中 A 股或美股宏观白名单时触发，分别以 `MACRO:A_SHARE`、`MACRO:US` 记录；方向性产业线索在 `status=active`、`news_tone=positive|negative`、`signal_score>=50` 时分别以 `SIGNAL:POSITIVE` / `SIGNAL:NEGATIVE` 记录并推送，正向用于入场观察，负向用于避险预警。默认 `NEWS_EVENT_SENTINEL_TRIGGER_MODE=notify_only`，底层生成通知信封和触发记录；`NEWS_EVENT_SENTINEL_FEISHU_ENABLED=true` 且配置 `FEISHU_WEBHOOK_URL` 后，会通过飞书 webhook 发送 interactive card。`NEWS_EVENT_SENTINEL_HEARTBEAT_ENABLED=true` 时，如果一轮扫描没有产生市场触发，会按 `NEWS_EVENT_SENTINEL_HEARTBEAT_INTERVAL_MINUTES` 写入并发送低等级存活卡片，便于确认 10 分钟后台轮询仍在运行。飞书卡片属于外层汇报适配器，只负责监控汇报，不负责拉取、入库或 LLM 抽取。Agent Trace 按钮和持仓 provider 属于后续外层适配器，不参与第一版触发语义。

`NEWS_SIGNAL_GRAPH_REPAIR_MODE`：

| 值 | 行为 |
| --- | --- |
| `off` | 不连接 Neo4j，只维护关系型数据 |
| `edges` | 推荐；重建并投影 typed relation / event clue，跳过慢 episode 抽取 |
| `episodes` | 同步 episode 后再投影显式边，适合小批量人工维护 |

## 手动维护

只回填关系库事件并投影全部 active 卡片关系：

```bash
python scripts/maintain_news_signals.py \
  --skip-rebuild \
  --skip-outcomes \
  --graph-repair edges \
  --graph-limit 500 \
  --backfill-limit 500
```

只维护指定交易日：

```bash
python scripts/maintain_news_signals.py --target-date 2026-07-10
```

API 入口：

```text
POST /api/v1/news-signals/events/backfill
POST /api/v1/news-signals/mapping-repair
POST /api/v1/news-signals/clusters/reconcile
POST /api/v1/news-signals/outcomes/refresh
POST /api/v1/news-signals/graph-sync?include_episodes=false
GET  /api/v1/news-signals/graph-outbox/metrics
POST /api/v1/news-signals/graph-outbox/run
```

## 质量门与关系展示

- 公司名或股票代码未在标题、摘要或正文中出现时，不允许由主题词典扩散到具体公司。
- “这家公司”“该公司”“公司上半年净利”等代称式导语在没有明确公司锚点时降为 `suppressed`，只保留关系库审计，不进入 active 图谱主路径。
- `same_event` 需要事件类型、公司或实体锚点、文本相似度和时间窗口同时满足；同主题只生成较弱的 `same_theme`。
- `实时快讯`、`市场资讯`、`行业动态` 等兜底标签不参与 typed relation 或 same-theme 建边，避免无关新闻因通用分类互连。
- Web 事件线索以关联新闻标题、日期、关系名称、建边理由和传导路径为主，不再把 `card:xueqiu:...` 作为用户可读信息。
- Neo4j 显式投影只接受 active 卡片，并清理 suppressed 卡片的关系；outbox 会删除已存在的 suppressed Graphiti episode。
- 关系库 edge rebuild 会同时删除当前范围内非 active 卡片的旧边，避免 metrics、API 手动查询和 Neo4j 投影出现状态不一致。

## 故障恢复

- Graphiti 不可用：卡片、事件和 outcome 仍写关系库；修复 Neo4j 后重新运行 `--graph-repair edges`。
- episode 抽取过慢：保持 `NEWS_SIGNAL_GRAPH_REPAIR_MODE=edges`，由 outbox 异步增强；按需下调 `GRAPHITI_OUTBOX_BATCH_SIZE` 或 `GRAPHITI_OUTBOX_JOB_TIMEOUT_SECONDS`。
- outbox 出现 `retry`：检查 `/graph-outbox/metrics`，修复 LLM/Neo4j 后等待退避到期或手动调用 `/graph-outbox/run`；达到最大次数才进入 `dead`。
- 需要重算事件：调用 backfill API 时设置 `only_missing=false`，或在维护脚本前明确清理目标事件投影。
- 需要回滚关系库：停止写入后恢复维护前的 SQLite backup，再运行 edge repair 重建 Neo4j 投影。
