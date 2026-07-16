# Loop Configuration — Daily Stock Analysis (Codex)

This file configures operational loops only. `AGENTS.md` remains the single source of truth for repository-wide agent behavior.

## Operating Mode

- Mode: autonomous dev loop after the maintainer provides a target document.
- The maintainer supplies goals and final acceptance only; the loop owns planning, implementation, tests, dev-branch push, PR/MR, issue creation, and release steps inside the active target scope.
- Stop immediately and escalate when an error cannot be handled, a tool silently degrades, fallback data quality is lower or unknown, credentials/permissions block required work, or verification cannot prove the result.

## Autonomous Goal Loop Contract

When the maintainer provides a target document, run this contract unless the maintainer explicitly overrides it for that target:

1. Rewrite the target document into a concrete goal, staged plan, and acceptance criteria before implementation.
2. Treat vague requirements such as "better quality", "optimize", or "do not fail" as incomplete until they are converted into observable checks.
3. The maintainer participates at goal input and final acceptance only; the loop owns implementation decisions in between.
4. Before final acceptance, the loop may commit, push to `dev`, update PR/MR, create issues, and prepare release notes without per-action approval.
5. Before final acceptance, do not merge to `main` and do not publish a formal release.
6. Fallback success is not full success. Record it as degraded success and stop if data quality is lower or unknown.
7. For financial analysis, stock selection, entry points, backtests, and news signals, expose evidence quality, data freshness, assumptions, and look-ahead-bias risk instead of only conclusions.
8. If the target document conflicts with loop rules, use this priority: target document > `LOOP.md` > `AGENTS.md`; hard safety rules still cannot be overridden.
9. Large targets may be split into staged issues and PRs. Keep `STATE.md` and PR descriptions current with progress, validation, and residual risk.
10. Add targeted diagnostics, logs, fixtures, scripts, docs, and small internal tools when they directly support verification or maintainability.
11. A target document should have target outcome, non-goals, acceptance criteria, priorities/stages, and known constraints. Draft missing pieces and ask only direction-changing questions.
12. Final delivery must include PR link, completion level, key changes, validation evidence, unfinished/degraded items, risks, rollback, and next-stage recommendations.

## Active Loops

| Pattern | Cadence | Status | Automation prompt |
|---------|---------|--------|-------------------|
| Daily Triage | 1d | L1 report-only | `Run $loop-triage. Read AGENTS.md and STATE.md first. Report only; do not edit source files.` |
| Goal Development Loop | Per target document | autonomous with hard stops | `Read AGENTS.md, LOOP.md, STATE.md, and the target document. Generate goal + plan, implement, verify, push dev, and update PR/MR. Stop only on explicit blocker or degraded/unknown data quality.` |

## Human Gates

- For the active autonomous loop, routine commit, dev push, PR/MR update, issue creation, and release preparation do not require per-action approval.
- No direct push to `main`. Use `dev` and PR/MR as the default integration surface.
- Main merge or release is allowed only when target acceptance conditions and required checks pass; if platform branch protection or missing credentials require human action, stop and report the exact blocker.
- High-risk paths require human review before any L2 fix proposal: `src/agent/`, `src/services/`, `data_provider/`, `api/`, `apps/dsa-web/`, `.github/`, `docker/`, `.env.example`, and AI governance assets.

## Worktrees

- Codex provides a built-in worktree per thread — use it for L2+ fix attempts.
- One fix per worktree; verifier subagent must APPROVE before proposing a PR or MR.
- L1 daily triage may update only loop state files when explicitly asked; goal development loops may edit source files within the active target scope.

## Connectors (MCP)

- MCP optional for L1 report-only loops.
- For L2+: GitHub connector should start read-only for CI/issues/PR context; write scope remains human-gated.

## Budget

- Max sub-agent spawns per run: 0 (L1)
- Review STATE.md daily + Codex Triage inbox

## Links

- Pattern: https://github.com/cobusgreyling/loop-engineering/blob/main/patterns/daily-triage.md
- Checklist: https://github.com/cobusgreyling/loop-engineering/blob/main/docs/loop-design-checklist.md
