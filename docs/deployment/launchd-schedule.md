# macOS launchd 常驻调度（`main.py --schedule`）

用用户级 LaunchAgent 常驻 `main.py --schedule`，保证工作日 15:30–16:00（Asia/Shanghai）的板块资金流影子采集窗口内调度进程在线。

- Label：`com.stockanalyser.schedule`
- plist：`~/Library/LaunchAgents/com.stockanalyser.schedule.plist`
- 进程：`<repo>/.venv/bin/python <repo>/main.py --schedule --no-run-immediately`
- 工作目录：仓库根
- 归属：当前登录用户的 GUI 域（`gui/$(id -u)`），不需要 `sudo`

## 1. plist

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>com.stockanalyser.schedule</string>
	<key>ProgramArguments</key>
	<array>
		<string>/Users/mengfanjun/Desktop/mfj/StockAnalyser/.venv/bin/python</string>
		<string>/Users/mengfanjun/Desktop/mfj/StockAnalyser/main.py</string>
		<string>--schedule</string>
		<string>--no-run-immediately</string>
	</array>
	<key>WorkingDirectory</key>
	<string>/Users/mengfanjun/Desktop/mfj/StockAnalyser</string>
	<key>EnvironmentVariables</key>
	<dict>
		<key>TZ</key>
		<string>Asia/Shanghai</string>
		<key>PYTHONUNBUFFERED</key>
		<string>1</string>
		<key>PYTHONPYCACHEPREFIX</key>
		<string>/private/tmp/stockanalyser-pycache</string>
		<key>PATH</key>
		<string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
	</dict>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<dict>
		<key>SuccessfulExit</key>
		<false/>
	</dict>
	<key>ThrottleInterval</key>
	<integer>30</integer>
	<key>ProcessType</key>
	<string>Background</string>
	<key>StandardOutPath</key>
	<string>/Users/mengfanjun/Desktop/mfj/StockAnalyser/logs/launchd-schedule.out.log</string>
	<key>StandardErrorPath</key>
	<string>/Users/mengfanjun/Desktop/mfj/StockAnalyser/logs/launchd-schedule.err.log</string>
</dict>
</plist>
```

字段说明：

| 字段 | 取值 | 作用 |
| --- | --- | --- |
| `RunAtLoad` | `true` | 登录后 / 加载时自动启动 |
| `KeepAlive` | `{SuccessfulExit: false}` | 异常退出（崩溃、非 0 退出码）才拉起；正常退出不自动重启 |
| `ThrottleInterval` | `30` | 拉起间隔下限，避免崩溃后疯狂重启 |
| `TZ` | `Asia/Shanghai` | 让进程内 naive 时间与影子窗口判定一致 |
| `PYTHONUNBUFFERED` | `1` | stdout 立即落盘，避免 launchd 日志长时间看不到内容 |

`--no-run-immediately` 只跳过启动时的“全量分析”，不影响影子采集：影子后台任务以 `run_immediately=True` 注册，进程启动时会立刻检查窗口。

## 2. 安装与启动

```bash
# 1) 写入 plist（内容见上）
plutil -lint ~/Library/LaunchAgents/com.stockanalyser.schedule.plist

# 2) 加载到当前用户 GUI 域
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.stockanalyser.schedule.plist

# 3) 查看状态（state = running / pid）
launchctl print gui/$(id -u)/com.stockanalyser.schedule

# 4) 确认只有一个实例
pgrep -fl "main.py --schedule"
```

若 label 已加载但进程未起来，用 `launchctl kickstart -k gui/$(id -u)/com.stockanalyser.schedule` 强制重启一次。

## 3. 停止（保留配置）

```bash
launchctl bootout gui/$(id -u)/com.stockanalyser.schedule
```

`bootout` 发 SIGTERM，`Scheduler` 的 `GracefulShutdown` 会等当前任务结束后退出；因为退出码为 0，`KeepAlive` 不会把它拉回来。下次登录或手动 `bootstrap` 会重新启动。

只想“本次登录内临时停掉”也可以用：

```bash
pkill -TERM -f "main.py --schedule"
```

注意：`pkill -f` 会误伤手动启动的同类进程，优先用 `bootout`。

## 4. 卸载

```bash
launchctl bootout gui/$(id -u)/com.stockanalyser.schedule 2>/dev/null
rm ~/Library/LaunchAgents/com.stockanalyser.schedule.plist
```

卸载后不会随登录启动，`logs/launchd-schedule.*.log` 会保留，可自行删除。

## 5. 查日志与确认在跑

| 文件 | 内容 |
| --- | --- |
| `logs/launchd-schedule.out.log` | 进程 stdout，即控制台日志（INFO 及以上） |
| `logs/launchd-schedule.err.log` | 进程 stderr（未捕获异常、启动期告警） |
| `logs/stock_analysis_YYYYMMDD.log` | 应用主日志（INFO，10MB × 5 轮转） |
| `logs/stock_analysis_debug_YYYYMMDD.log` | 应用调试日志（DEBUG，50MB × 3 轮转） |
| `data/factor_eval_cache/sector_flow_shadow_runs/{YYYY-MM-DD}.jsonl` | 影子采集每次触发的运行记录 |

```bash
tail -f logs/launchd-schedule.out.log
grep "SectorFlowShadow" logs/launchd-schedule.out.log | tail
ls -l data/factor_eval_cache/sector_flow_shadow_runs/
tail -n 3 data/factor_eval_cache/sector_flow_shadow_runs/$(date +%F).jsonl
```

预期：启动后先出现

```
[SectorFlowShadow] 已注册每日影子采集：工作日 15:30–16:00（Asia/Shanghai），每 600s 检查一次
```

窗口内再出现 `[SectorFlowShadow]` 的采集结果行；同日重复触发是 `cache_hit=true`，不会重复请求东财。

启动后到注册行出现约有 20–30 秒延迟（首次导入 `src.services.*` 与 Agent 依赖较慢），属正常现象；`ls -l logs/` 中能同时看到 `stock_analysis_YYYYMMDD.log` 在增长。

## 6. 日志轮转

- 应用内的 `logs/stock_analysis_*.log`、`logs/stock_analysis_debug_*.log` 由 `src/logging_config.py` 的 `RotatingFileHandler` 自动轮转，无需额外配置。
- `logs/launchd-schedule.*.log` 是 launchd 直接追加的固定文件，**不会自动轮转**。它们只是 stdout/stderr 的副本，内容与应用主日志大量重复，可直接截断：

```bash
: > logs/launchd-schedule.out.log
: > logs/launchd-schedule.err.log
```

launchd 以追加模式持有句柄，运行中截断不会导致写偏移错误；如仍担心，可先 `bootout` 再截断、再 `bootstrap`。
需要按大小/日期自动轮转时，可加 `/etc/newsyslog.d/stockanalyser.conf`（需 `sudo`），例如按 1MB 轮转、保留 3 份。

## 7. 重复实例与幂等

常驻 Agent 与手动 `main.py --schedule` 可能同时存在，两者各自跑一套 scheduler 与后台任务：

- 影子采集同交易日命中缓存 `data/factor_eval_cache/sector_flow_snapshot_{date}.json`，后到者记录 `cache_hit=true`，不会重复抓 496 个板块。
- 但如果两个进程在同一交易日都尚未构建快照、又同时进入窗口，会各发一轮东财请求。**手动调试前先停掉 Agent**：

```bash
launchctl bootout gui/$(id -u)/com.stockanalyser.schedule
.venv/bin/python main.py --schedule --no-run-immediately   # 手动调试
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.stockanalyser.schedule.plist
```

- CI 的 `daily_analysis.yml` 在 18:00 一次性分析，不持久化 `data/`，不注册影子任务，与本地 Agent 不重叠。

## 8. 需要注意的副作用

`--schedule` 除了影子采集，还会按 `SCHEDULE_TIME`（默认 `18:00`）执行每日全量分析（写报告、发通知）。常驻后这一步也会每天自动发生；如果只想在 15:30–16:00 有进程，需在窗口结束后停掉 Agent，或调整 `.env` 中的 `SCHEDULE_TIME` / 通知开关。
