"""Developer tasks: `uv run python -m invoke --list`."""

from invoke import task

DOCS_FLAGS = "-W --keep-going -n"


@task
def test(c):
    """Run the test suite."""
    c.run("pytest -q", pty=True)


@task
def lint(c):
    """Lint with ruff."""
    c.run("ruff check src tests tasks.py docs/conf.py", pty=True)


@task
def docs(c):
    """Build the docs site into docs/_build/html, failing on any warning."""
    c.run(f"sphinx-build {DOCS_FLAGS} -b html docs docs/_build/html", pty=True)


@task
def docs_serve(c, port=8000):
    """Serve the docs with live reload."""
    c.run(f"sphinx-autobuild {DOCS_FLAGS} --port {port} docs docs/_build/html", pty=True)
