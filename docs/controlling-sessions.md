# Controlling sessions

Read endpoints are always available. The two endpoints that signal a real
process are mounted only when agentboss is started with `--allow-control`:

```bash
agentboss --allow-control
curl -X POST localhost:8848/api/sessions/<id>/interrupt   # SIGINT
curl -X POST localhost:8848/api/sessions/<id>/terminate   # SIGTERM
```

They act on a live CLI that may be partway through an edit, so they are off by
default, and the dashboard asks for confirmation before sending either one.

Without `--allow-control`, both endpoints return `403`. A session that is not
running returns `409`.
