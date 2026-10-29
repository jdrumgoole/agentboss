# JSON API

Every panel on the dashboard is built from these endpoints. Interactive
OpenAPI docs are served at `/docs` on a running instance.

| Endpoint | Returns |
|---|---|
| `GET /api/overview?hours=24` | Tiles, model mix, agent-type mix, activity timeline. |
| `GET /api/quota?days=30` | Quota windows, estimated caps, limit hits, daily spend. |
| `GET /api/sessions` | Sessions, filtered and sorted. |
| `GET /api/sessions/{id}` | One session with every subagent. |
| `GET /api/agents` | Subagents across sessions. |
| `GET /api/models` | Tokens and turns per model. |
| `GET /api/projects` | Sessions grouped by working directory. |
| `GET /api/filters` | Distinct values for the filter controls. |
| `GET /api/stream` | Server-sent events: a fresh overview when the numbers change. |
| `POST /api/sessions/{id}/{interrupt,terminate}` | Requires `--allow-control`. |

## Sessions

`GET /api/sessions` accepts:

| Parameter | Meaning |
|---|---|
| `status` | `busy`, `idle`, `stale`, `ended`, `live` (busy or idle), or `all`. |
| `project` | Exact working directory. |
| `model` | Sessions where this model ran, in the main loop or a subagent. |
| `agent_type` | Sessions that spawned an agent of this type. |
| `q` | Substring match on id, project, name, title, last prompt or branch. |
| `sort` | `last_activity` (default), `started`, `tokens`, `messages`, `agents`, `project`, `status`. |
| `order` | `desc` (default) or `asc`. |
| `limit`, `offset` | Paging. |

## Agents

`GET /api/agents` accepts `session_id`, `agent_type`, `model`, `live_only` and
`limit`, and returns the most recently active first.

## Quota

`GET /api/quota?days=30` returns:

```json
{
  "five_hour": {"kind": "five_hour", "start": "…", "end": "…", "active": true,
                "cost": 7.33, "tokens": 11512872, "turns": 39,
                "models": {"claude-opus-5-5": 7.33},
                "estimated_cap": null, "used_fraction": null, "projected_cost": 35.47},
  "seven_day": {"kind": "seven_day", "cost": 2527.62, "estimated_cap": 3442.14,
                "used_fraction": 0.734, "…": "…"},
  "limit_hits": [{"at": "…", "limit_type": "seven_day", "resets_at": "…",
                  "attempts": 10, "spend_at_hit": 3442.14, "session_id": "…"}],
  "daily": [{"start": "…", "cost": 674.0, "tokens": 0, "models": {}}],
  "blocks": [{"kind": "five_hour", "start": "…", "end": "…", "cost": 0.0}]
}
```

Costs are API-equivalent US dollars. See [Quota](quota.md) for the method.
