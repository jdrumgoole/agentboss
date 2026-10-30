# Getting started

## Install

agentboss needs Python 3.11 or later. Install it as a standalone command, in
its own isolated environment:

```bash
uv tool install git+https://github.com/jdrumgoole/agentboss.git
```

or, with pipx, `pipx install git+https://github.com/jdrumgoole/agentboss.git`.
The `agentboss` command is then on your `PATH` from any directory. Upgrade with
`uv tool upgrade agentboss` and remove it with `uv tool uninstall agentboss`.

From a clone, `uv tool install .` does the same. To work on agentboss itself,
see [Development](development.md).

## Run

```bash
agentboss --open
```

The dashboard is served at <http://127.0.0.1:8848/> and `--open` opens it in
your browser. The interactive OpenAPI docs are at `/docs`.

On a machine with a long Claude Code history, the first scan reads every
transcript once and can take several seconds. Later scans only parse the bytes
appended since the previous one, so they take milliseconds.

## Next steps

- [The dashboard](dashboard.md): what each panel shows.
- [Quota](quota.md): how agentboss maps your plan's usage limits.
- [Configuration](configuration.md): every flag and environment variable.
