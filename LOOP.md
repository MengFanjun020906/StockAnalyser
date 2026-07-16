# Loop Configuration — Daily Stock Analysis (Codex)

This file configures operational loops only. `AGENTS.md` remains the single source of truth for repository-wide agent behavior.

## Active Loops

| Pattern | Cadence | Status | Automation prompt |
|---------|---------|--------|-------------------|
| Daily Triage | 1d | L1 report-only | `Run $loop-triage. Read AGENTS.md and STATE.md first. Report only; do not edit source files.` |

## Human Gates

- No auto-fix until L2 checklist is explicitly approved by the maintainer.
- No `git commit`, `git tag`, `git push`, `gh pr create`, merge, or release action without explicit human approval.
- High-risk paths require human review before any L2 fix proposal: `src/agent/`, `src/services/`, `data_provider/`, `api/`, `apps/dsa-web/`, `.github/`, `docker/`, `.env.example`, and AI governance assets.

## Worktrees

- Codex provides a built-in worktree per thread — use it for L2+ fix attempts.
- One fix per worktree; verifier subagent must APPROVE before proposing a PR or MR.
- L1 daily triage may update only loop state files when explicitly asked; otherwise it reports findings.

## Connectors (MCP)

- MCP optional for L1 report-only loops.
- For L2+: GitHub connector should start read-only for CI/issues/PR context; write scope remains human-gated.

## Budget

- Max sub-agent spawns per run: 0 (L1)
- Review STATE.md daily + Codex Triage inbox

## Links

- Pattern: https://github.com/cobusgreyling/loop-engineering/blob/main/patterns/daily-triage.md
- Checklist: https://github.com/cobusgreyling/loop-engineering/blob/main/docs/loop-design-checklist.md
