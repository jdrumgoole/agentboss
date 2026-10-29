# Development

```bash
uv venv --python 3.12
uv pip install -e ".[dev,docs]"
```

Tasks are run with [invoke](https://www.pyinvoke.org/):

| Command | Does |
|---|---|
| `uv run python -m invoke test` | Run the test suite. |
| `uv run python -m invoke lint` | Lint with ruff. |
| `uv run python -m invoke docs` | Build this site into `docs/_build/html`, failing on any warning. |
| `uv run python -m invoke docs-serve` | Serve the site at <http://127.0.0.1:8000/> with live reload. |

The test suite builds a synthetic `~/.claude` tree, so it never touches your
real one.

## Documentation

The site is Sphinx with Markdown pages parsed by MyST, hosted on Read the Docs.
Update it in the same change as the code it describes, and keep the README in
step.
