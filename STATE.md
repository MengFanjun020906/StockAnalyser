# Loop State — Daily Stock Analysis

Last run: never

Mode: autonomous-dev-loop after target document is provided
Contract: `LOOP.md` Autonomous Goal Loop Contract

## High Priority (loop is acting or waiting on human)

- Waiting for maintainer target document. Once received, generate a concrete goal and plan, then run autonomously until completion or hard blocker.
- Before coding, normalize the target into outcome, non-goals, acceptance criteria, priorities/stages, and known constraints.

## Watch List

- Agent planning/execute traces: watch for repeated `todo.md`, unclear tool handoff expectations, and replan drift.
- News-signal and seed-pool changes: watch for evidence summarization quality, industry gating, and stale fallback data.
- Entry-execution backtests: watch for data rebuilds that hide older decision dates after backend restarts.
- Never hide tool failures, partial fallback success, or lower-quality data. If quality is degraded or unknown, stop and escalate instead of presenting the result as successful.
- Final acceptance package must include PR link, completion level, key changes, validation evidence, unfinished/degraded items, risks, rollback, and next-stage recommendations.

## Recent Noise (ignored this run)

---
Run log: `loop-run-log.md`
