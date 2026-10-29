# The dashboard

The page holds a server-sent events connection and repaints only when the
numbers change, so it stays current without the browser polling the disk.

## Stat tiles

Live sessions (busy versus idle), sessions on disk, agents and how many of them
are nested, distinct models in use, cumulative tokens, and tool calls.

## Quota

Spend in the current 5-hour block and 7-day window against caps estimated from
your recorded limit hits, with 30 days of daily spend underneath. See
[Quota](quota.md) for how this is worked out.

## Recommendations

Concrete ways to use less quota, most urgent first, each with an estimated
saving where one can be worked out. The top six are shown; the rest fold away.
See [Recommendations](recommendations.md).

## Activity

Assistant turns per hour across every session and subagent, for the last 24
hours.

## Models

Tokens billed per model, main loops and subagents combined.

## Agent types

How many subagents of each type (`general-purpose`, `Explore`, `Plan`, and any
project-defined agents) have been spawned.

## Session table

One row per session with status, models, agent and subagent counts, messages,
tool calls and tokens. Filter by status, project, model or agent type, and
search across ids, titles, projects and last prompts.

Click a row to open the detail drawer: full metadata, the token split, top
tools, and a card for each subagent showing its type, description, spawn
depth, model, duration and token cost. The open session is reflected in the URL
(`/#/session/<id>`), so a view can be bookmarked.

## Session status

| Status  | Meaning |
|---------|---------|
| `busy`  | Process alive and working. The CLI's own word for it (`busy`, `shell`, …) is kept in `activity_state`. |
| `idle`  | Process alive, waiting on you. |
| `stale` | Registry entry exists but the process is gone. |
| `ended` | Transcript on disk, no registry entry. |

Token counts throughout are *cumulative billed tokens* summed over every turn,
not the size of any context window.
