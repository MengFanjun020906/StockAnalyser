# Loop Constraints

> Add rules below with `/constraints <rule>` in your agent.
> The `loop-constraints` skill reads this file at the start of every run.
> Constraints here are **binding** — the agent MUST follow them.

## Push & Merge
- Default mode: don't push before telling me.
- Autonomous loop mode: after the maintainer provides a target document and grants loop autonomy, commit, push to `dev`, create/update PR/MR, create issues, and prepare releases without per-action approval.
- Never push directly to `main`; use `dev` plus PR/MR as the integration surface.
- Main merge or release is allowed only after target acceptance conditions and required checks pass. If branch protection, credentials, or platform permissions require human action, stop and report the exact blocker.

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
- For goal development loops, generate a goal and plan from the target document, then own implementation and verification end-to-end.
- Stop immediately on unhandled tool failure, silent fallback, lower-quality data, unknown data quality, auth/permission blocker, destructive ambiguity, or unverifiable acceptance criteria.

## Communication
- Always tell me what you're about to do before doing it
- Never close an issue or PR without my approval

## Budget
- If token spend hits 80% of daily cap, switch to report-only
- If loop-pause-all is active, exit immediately

---
<!-- Add your own rules below. Use plain English. The loop reads this verbatim. -->
