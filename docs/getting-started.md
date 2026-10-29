# Getting started

## Install

agentboss needs Python 3.11 or later.

```bash
git clone https://github.com/jdrumgoole/agentboss.git
cd agentboss
uv venv --python 3.12
uv pip install -e .
```

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
