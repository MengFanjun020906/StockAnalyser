# Loop Budget — Daily Stock Analysis

> Primary scheduled loop: **Daily Triage** (scaffolded by loop-init)
> Manual target loop: **Autonomous Goal Loop** after the maintainer provides a target document.

## Daily limits

| Loop | Max runs/day | Max tokens/day | Max sub-agent spawns/run |
|------|--------------|----------------|--------------------------|
| Daily Triage | 2 | 100k | 0 (L1) / 2 (L2) |
| Autonomous Goal Loop | Per target document | User-provided budget; otherwise checkpoint every 200k estimated tokens | As needed by task, with failures and degraded data escalated |

Daily Triage is report-only unless explicitly upgraded. The Autonomous Goal Loop is not constrained by the L1 triage run cap; it must instead keep `STATE.md`, PR descriptions, and validation evidence current. If a run crosses the 200k-token checkpoint, write the current evidence, risk, and next action before continuing.

## On budget exceed

1. Pause schedulers (`scheduler_delete` or disable automations)
2. Append event to `loop-run-log.md`
3. Notify human by writing a concise item under `STATE.md` High Priority

For an Autonomous Goal Loop with no explicit token cap, switch to report-only only when progress is blocked, verification becomes impossible, a checkpoint exposes uncontrolled scope expansion, or a hard stop from `LOOP.md` / `loop-constraints.md` is reached.

## Kill switch

- Command or issue label: `loop-pause-all`
- Resume only after human clears the flag in STATE.md

## Estimate spend

```bash
npx @cobusgreyling/loop-cost --pattern daily-triage --level L1 --cadence 1d
```
