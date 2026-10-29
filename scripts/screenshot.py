"""Capture docs/_static/dashboard.png from a running agentboss.

    agentboss --port 8848 &
    uv run python scripts/screenshot.py [--url http://127.0.0.1:8848/]

Session names, project names, branches and the Claude home path come from the
machine the shot is taken on, so they are replaced in the DOM before capture.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "_static" / "dashboard.png"

TASKS = [
    "Refactor the storage layer",
    "Fix flaky integration test",
    "Add pagination to the API",
    "Write release notes",
    "Investigate memory growth",
    "Port CLI to async",
    "Review open pull requests",
    "Update dependency pins",
]
#: Project-defined agent types are named after private work; show generic ones.
AGENTS = ["code-reviewer", "test-runner", "doc-writer"]
BUILTIN_AGENTS = ["general-purpose", "Explore", "Plan"]
PROJECTS = ["webapp", "api-server", "data-pipeline", "docs-site", "infra"]

ANONYMIZE = """([tasks, projects, agents, builtins]) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    n.textContent = n.textContent.replace(/\/(?:Users|home)\/[^/\s]+/g, '~');
  }
  let custom = 0;
  document.querySelectorAll('#agent-bars .bar-row').forEach((row, i) => {
    const name = row.querySelector('.name');
    if (i >= 6) { row.remove(); return; }
    if (!builtins.includes(name.textContent)) name.textContent = agents[custom++ % agents.length];
  });
  document.querySelectorAll('.home').forEach((n) => { n.textContent = '~/.claude'; });
  document.querySelectorAll('#rows tr').forEach((row, i) => {
    const name = row.querySelector('.sess-name');
    const sub = row.querySelector('.sess-sub');
    if (name) name.textContent = tasks[i % tasks.length];
    if (sub) sub.textContent = projects[i % projects.length] + ' · main';
    row.querySelectorAll('[title]').forEach((n) => n.removeAttribute('title'));
  });
  document.querySelectorAll('#quota-daily [title]').forEach((n) => n.removeAttribute('title'));
}"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8848/")
    args = parser.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1500}, device_scale_factor=2)
        page.emulate_media(color_scheme="light")
        page.goto(args.url)
        page.wait_for_selector("#rows tr .sess-name", timeout=120_000)
        page.wait_for_selector("#quota-meters .bar-row", timeout=60_000)
        # Rewrite and capture back to back, so a live repaint cannot land between.
        page.evaluate("() => document.querySelectorAll('[title]').forEach((n) => n.removeAttribute('title'))")
        page.evaluate(ANONYMIZE, [TASKS, PROJECTS, AGENTS, BUILTIN_AGENTS])
        height = page.evaluate("() => document.querySelector('.shell').scrollHeight + 40")
        page.screenshot(path=str(OUT), clip={"x": 0, "y": 0, "width": 1440, "height": height})
        browser.close()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
