# Configuration

```text
agentboss [--host 127.0.0.1] [--port 8848]
          [--claude-home ~/.claude]
          [--refresh 1.0]
          [--allow-control]
          [--open] [--reload] [--log-level warning]
```

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--host` | | `127.0.0.1` | Bind address. |
| `--port` | | `8848` | Port. |
| `--claude-home` | `AGENTBOSS_CLAUDE_HOME` | `~/.claude` | Claude Code state directory to read. |
| `--refresh` | `AGENTBOSS_REFRESH_INTERVAL` | `1.0` | Minimum seconds between filesystem rescans. |
| `--allow-control` | `AGENTBOSS_ALLOW_CONTROL` | off | Mount the interrupt and terminate endpoints. See [Controlling sessions](controlling-sessions.md). |
| — | `AGENTBOSS_STATE_DIR` | `~/.agentboss` | Where agentboss keeps its own state (ticked-off recommendations). Never inside `~/.claude`. |
| `--open` | | off | Open the dashboard in a browser. |
| `--reload` | | off | Auto-reload on code changes (development). |
| `--log-level` | | `warning` | uvicorn log level. |

agentboss binds to localhost by default. It shows prompts, project paths and
session ids, so think before binding it to a public interface.
