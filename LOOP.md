# Loop Configuration — Daily Stock Analysis (Codex)

This file configures operational loops only. `AGENTS.md` remains the single source of truth for repository-wide agent behavior.

## Operating Mode

- Mode: autonomous dev loop after the maintainer provides a target document.
- The maintainer supplies goals and final acceptance only; the loop owns planning, implementation, tests, dev-branch push, PR/MR, issue creation, and release steps inside the active target scope.
- Stop immediately and escalate when an error cannot be handled, a tool silently degrades, fallback data quality is lower or unknown, credentials/permissions block required work, or verification cannot prove the result.

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
