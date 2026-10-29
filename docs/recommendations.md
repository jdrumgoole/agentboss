# Recommendations

The Recommendations panel lists concrete ways to use less quota, most urgent
first. Each one names an action and, where it can, an estimated saving in the
same API-equivalent dollars as the [quota](quota.md) panel.

Everything comes from what the transcripts already record. Nothing is sent
anywhere.

## Why context depth matters most

Every turn re-sends the whole conversation. Cached context is cheap per token
(a tenth of the input price), but it is paid on *every* turn, so a session
carrying 500k tokens of context costs roughly ten times as much per turn as one
carrying 50k. On a typical heavy Claude Code history, re-reading context is
most of the total.

## The rules

### Compact or clear a deep session

Flags a session active in the last 24 hours whose most recent turn was given
more than 150k tokens of context (ranked high above 400k).

- **Action:** `/compact` to carry on with the same task, or `/clear` before
  starting an unrelated one.
- **Saving:** the cache-read cost of the context above 40k, per 100 further
  turns, at the session's model price.

### A session holding much of this week's usage

Flags up to three sessions that each hold at least 25% (and at least $50) of
the current 7-day window, with how much of it went to their subagents.

### Run agents on Sonnet

Groups subagents from the last 30 days that ran on a model priced above
Sonnet 5.5, and estimates the saving had they run on Sonnet 5.5. Shown when the
saving is at least $10.

- Built-in agents (`general-purpose`, `Explore`, `Plan`): ask for a cheaper
  model when delegating.
- Agents with their own definition: set `model: sonnet` in the definition.
- Named **teammates** are grouped under the definition they run rather than
  their names, and the recommendation says when the premium model was
  requested explicitly at spawn time.

Keep the premium model for work that needs it; the estimate assumes none does.

### Large tool results

Flags a tool whose results, across sessions active in the last week, average
more than about 5k tokens over at least five calls. A result stays in context
and is re-read on every later turn, so large ones keep costing. Each
recommendation carries a tool-specific tip, such as reading file ranges with
`offset` and `limit`, or trimming command output with `head`, `tail` or `grep`.

Tool output is measured in characters and converted at about four characters
per token.

## API

`GET /api/recommendations` returns the same list:

```json
[
  {
    "kind": "context",
    "severity": "high",
    "title": "Compact or clear “refactor storage”",
    "detail": "Each turn re-reads about 427k tokens of context. …",
    "session_id": "…",
    "estimated_savings": 15.48,
    "savings_basis": "per 100 further turns"
  }
]
```

`kind` is one of `context`, `window`, `subagent_model` or `tool_output`.
Thresholds are constants at the top of `src/agentboss/recommend.py`.
