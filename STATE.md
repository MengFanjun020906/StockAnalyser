# Loop State — Daily Stock Analysis

Last run: 2026-07-16 infrastructure diagnosis loop

Mode: autonomous-dev-loop after target document is provided
Contract: `LOOP.md` Autonomous Goal Loop Contract

## High Priority (loop is acting or waiting on human)

- Kronos A-share daily pattern detection goal loop checkpoint (2026-07-25): design doc, backend service/API/storage, config registry, and web visualization page are implemented. Full backend gate, targeted Kronos/API tests, web lint/build, nav test, and `/kronos` browser smoke passed. Seed-pool automation remains gated until offline backtest acceptance.
- Infrastructure diagnosis loop Stage 0/1 is validated locally: loop governance is aligned, offline checks cover tool/context/planner/todo/evidence contracts, and PR #8 handoff is queued.
- Do not change trading strategy, memory default behavior, database schema, `main`, or release state without a direction-changing decision.

## Watch List

- Agent planning/execute traces: watch for repeated `todo.md`, unclear tool handoff expectations, and replan drift.
- Agent infrastructure audit: run the offline audit before future goal loops when source, tools, planner, trace artifacts, or loop governance change.
- News-signal and seed-pool changes: watch for evidence summarization quality, industry gating, and stale fallback data.
- Entry-execution backtests: watch for data rebuilds that hide older decision dates after backend restarts.
- Graphiti Core episode extraction can be slow even when Neo4j is healthy; synchronous ingest callers must keep bounded timeout and treat timeout as warning, not as a hidden success.
- Kronos integration remains default-off; any seed-pool automation, trading signal use, or online inference should stay gated until offline backtest metrics, data freshness, and look-ahead-bias checks are accepted.
- Never hide tool failures, partial fallback success, or lower-quality data. If quality is degraded or unknown, stop and escalate instead of presenting the result as successful.
- Final acceptance package must include PR link, completion level, key changes, validation evidence, unfinished/degraded items, risks, rollback, and next-stage recommendations.

## Recent Noise (ignored this run)

---
Run log: `loop-run-log.md`
