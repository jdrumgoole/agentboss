# Quota

Claude plans limit usage over two windows: a **5-hour** block and a **7-day**
week. agentboss shows how much of each you have consumed.

## Why it is an estimate

Claude Code does not write your plan's live utilisation to disk. It does write
two things agentboss can use:

1. **The usage of every API turn**: input, output, cache-read and cache-write
   tokens, and the model.
2. **A record of every refused request.** When a limit rejects a turn, the
   transcript gets a synthetic assistant record carrying a `quotaLimits`
   object:

   ```json
   {"status": "rejected", "rateLimitType": "seven_day", "resetsAt": 1792404000}
   ```

So consumption is *mapped*, not read.

## Weighting turns by cost

Different models draw down quota at different rates, so tokens are not summed
directly. Each turn is weighted by its API-equivalent price per million tokens:

| Model family | Input | Output |
|---|---|---|
| Fable 5 / 5.1 | $10 | $50 |
| Opus 5.5 | $4 | $20 |
| Opus 5, Opus 4.x | $5 | $25 |
| Sonnet 5 / 5.5 | $2 | $10 |
| Sonnet 4.x | $3 | $15 |
| Haiku 5.5 | $0.10 | $0.50 |
| Haiku 4.x | $1 | $5 |

Cache reads are charged at 0.1× the input price and cache writes at 1.25×.
Unknown models are priced as Opus 5. The table lives in `PRICES` in
`src/agentboss/quota.py`.

The resulting dollar figures are a **proxy for quota, not a bill**. On a
subscription plan they will usually be far larger than the subscription price:
heavy Claude Code use can reach thousands of dollars a week at API prices. The
number that matters is the percentage of the estimated cap.

## The windows

5-hour block
: A block opens on the hour of the first turn after the previous block closed,
  and lasts five hours. Between blocks, no block is active.

7-day window
: Weekly windows are fixed. The most recent recorded `resetsAt` for a
  `seven_day` limit pins the anchor, and the window is projected forward in
  seven-day steps. With no recorded reset, the window is the trailing seven
  days.

## Estimating the cap

When a limit is hit, the spend in that window up to the *first* rejection is
taken as the cap. A refused request is usually retried until the reset; the
retries are counted in `attempts` but do not move the estimate. The meter uses
the most recent hit of each kind.

Caps estimated from different weeks can differ noticeably, because the rate
at which a plan meters each model is not public. Treat the percentage as a
guide.

## What the panel shows

- A meter per window: spend, percentage of the estimated cap, reset time,
  tokens, and the spend projected to the end of the window at the current rate.
  The meter turns amber at 70% and red at 90%.
- Daily spend for the last 30 days, with days that hit a limit in red. Hover a
  bar for its per-model split.
- The most recent limit hits and the spend at which each landed.

The same data is available from [`GET /api/quota`](api.md#quota).
