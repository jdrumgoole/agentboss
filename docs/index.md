# agentboss

A single dashboard for every Claude Code session on your machine: which ones are
running, what models they are burning tokens on, how many agents and subagents
each has spawned, and how much of your plan's quota you have used.

agentboss is a read-only observer. It reads the Claude Code state directory
(`~/.claude` by default) and never writes to it.

![The agentboss dashboard](_static/dashboard.png)

```{toctree}
:maxdepth: 2
:caption: Using agentboss

getting-started
dashboard
quota
recommendations
controlling-sessions
```

```{toctree}
:maxdepth: 2
:caption: Reference

configuration
api
how-it-works
development
```
