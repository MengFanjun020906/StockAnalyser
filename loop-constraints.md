# Loop Constraints

> Add rules below with `/constraints <rule>` in your agent.
> The `loop-constraints` skill reads this file at the start of every run.
> Constraints here are **binding** — the agent MUST follow them.

## Push & Merge
- Don't push before telling me
- Never auto-merge to main without human approval
- Always create a draft PR first; let me review before marking ready
- Never run `git commit`, `git tag`, `git push`, `gh pr create`, merge, or release actions without explicit human approval

## Paths
- Never edit .env, .env.*, auth/, payments/, secrets/, credentials/
- Never edit infrastructure configs without human approval
- Treat `AGENTS.md` as the repository-wide agent behavior source. `LOOP.md`, `STATE.md`, and `loop-*` files may narrow loop behavior but must not override `AGENTS.md`.
- Require human review before changing `src/agent/`, `src/services/`, `data_provider/`, `api/`, `apps/dsa-web/`, `.github/`, `docker/`, `.env.example`, or AI governance assets.

## Code
- Always run tests before proposing a fix
- Never disable tests to make CI green
- Never refactor unrelated code — one fix per run
- Max 3 fix attempts per item; escalate after
- Enforce the attempt limit mechanically: log each try to `loop-ledger.json` and run `loop-context --check` before retrying (see the `loop-guard` skill)
- Keep the daily triage loop report-only until L2 is explicitly enabled.
- Do not make source edits from a triage-only loop.

## Communication
- Always tell me what you're about to do before doing it
- Never close an issue or PR without my approval

## Budget
- If token spend hits 80% of daily cap, switch to report-only
- If loop-pause-all is active, exit immediately

---
<!-- Add your own rules below. Use plain English. The loop reads this verbatim. -->
