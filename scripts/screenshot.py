"""Capture docs/_static/dashboard.png from a running agentboss.

    agentboss --port 8848 &
    uv run python scripts/screenshot.py [--url http://127.0.0.1:8848/]

Session names, project names, branches and the Claude home path come from the
machine the shot is taken on, so they are replaced in the DOM before capture.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
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


def fake_quota() -> dict:
    """Plausible, made-up quota data: real usage figures stay off the page."""
    rng = random.Random(7)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    day0 = (now - timedelta(days=29)).replace(hour=0)
    week_start = now - timedelta(days=3, hours=4)
    hit_day = day0 + timedelta(days=12)

    def window(kind, start, end, cost, cap, tokens):
        return {
            "kind": kind,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "active": True,
            "cost": cost,
            "tokens": tokens,
            "turns": 0,
            "models": {},
            "estimated_cap": cap,
            "used_fraction": round(cost / cap, 4) if cap else None,
            "projected_cost": round(cost * 1.9, 2),
        }

    daily = [
        {
            "start": (day0 + timedelta(days=i)).isoformat(),
            "cost": round(rng.uniform(40, 260) * (0.3 if i % 7 in (5, 6) else 1), 2),
            "tokens": 0,
            "models": {},
        }
        for i in range(30)
    ]
    return {
        "generated_at": now.isoformat(),
        "five_hour": window(
            "five_hour", now - timedelta(hours=2), now + timedelta(hours=3), 38.40, 96.0, 61_000_000
        ),
        "seven_day": window(
            "seven_day", week_start, week_start + timedelta(days=7), 512.75, 1180.0, 820_000_000
        ),
        "limit_hits": [
            {
                "at": (hit_day + timedelta(hours=15)).isoformat(),
                "limit_type": "seven_day",
                "spend_at_hit": 1180.0,
            }
        ],
        "daily": daily,
        "blocks": [],
    }


#: Recommendations name real sessions and agents; show representative fakes.
FAKE_RECS = [
    {
        "kind": "context",
        "severity": "high",
        "title": "Compact or clear “Refactor storage”",
        "detail": "Each turn re-reads about 412k tokens of context. Run /compact to keep going on "
        "the same task, or /clear before starting an unrelated one; that brings each turn "
        "near 40k.",
        "session_id": None,
        "estimated_savings": 14.88,
        "savings_basis": "per 100 further turns",
    },
    {
        "kind": "subagent_model",
        "severity": "medium",
        "title": "Run general-purpose agents on Sonnet",
        "detail": "38 general-purpose agents ran on a premium model in the last 30 days, $412 "
        "API-equivalent. Ask for a cheaper model when delegating; the Agent tool takes a model "
        "argument. Keep the premium model for work that needs it.",
        "session_id": None,
        "estimated_savings": 206.0,
        "savings_basis": "over the last 30 days, at Sonnet 5.5 prices",
    },
    {
        "kind": "tool_output",
        "severity": "low",
        "title": "Bash results are large",
        "detail": "184 Bash calls in recently active sessions averaged about 6.2k tokens each "
        "(1.1M in all). Each result stays in context and is re-read on every later turn. Trim "
        "command output with head, tail or grep, and run noisy builds and tests quietly (-q) so "
        "only failures come back.",
        "session_id": None,
        "estimated_savings": None,
        "savings_basis": None,
    },
]

#: Headline totals in the tiles and model panel, replaced with round fakes.
FAKE_TEXT = {
    "#tiles .tile:nth-child(5) .value": "1.2B",
    "#tiles .tile:nth-child(5) .sub": "4.1M out · 1.1B cached",
    "#tiles .tile:nth-child(6) .value": "9.8k",
    "#tiles .tile:nth-child(6) .sub": "12k messages",
    "#tiles .tile:nth-child(2) .value": "34",
    "#tiles .tile:nth-child(3) .value": "57",
    "#pulse-text": "live · scanned in 42ms",
}

ANONYMIZE = """([tasks, projects, agents, builtins]) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    n.textContent = n.textContent.replace(/\/(?:Users|home)\/[^/\s]+/g, '~');
  }
  const fakes = __FAKES__;
  for (const [sel, text] of Object.entries(fakes)) {
    document.querySelectorAll(sel).forEach((n) => { n.textContent = text; });
  }
  const modelFakes = ['980M tok · 8.2k turns', '190M tok · 1.6k turns', '30M tok · 210 turns'];
  document.querySelectorAll('#model-bars .amount').forEach((n, i) => {
    n.textContent = modelFakes[i] || n.textContent;
  });
  const agentCounts = ['41', '9', '3', '2', '1', '1'];
  document.querySelectorAll('#agent-bars .amount').forEach((n, i) => {
    n.textContent = agentCounts[i] || n.textContent;
  });
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
        quota = json.dumps(fake_quota())
        page.route(
            "**/api/quota*",
            lambda route: route.fulfill(status=200, content_type="application/json", body=quota),
        )
        recs = json.dumps(FAKE_RECS)
        page.route(
            "**/api/recommendations*",
            lambda route: route.fulfill(status=200, content_type="application/json", body=recs),
        )
        page.goto(args.url)
        page.wait_for_selector("#rows tr .sess-name", timeout=120_000)
        page.wait_for_selector("#quota-meters .bar-row", timeout=60_000)
        page.wait_for_selector("#recs .rec", timeout=60_000)
        # Rewrite and capture back to back, so a live repaint cannot land between.
        page.evaluate(
            "() => document.querySelectorAll('[title]').forEach((n) => n.removeAttribute('title'))"
        )
        page.evaluate(
            ANONYMIZE.replace("__FAKES__", json.dumps(FAKE_TEXT)),
            [TASKS, PROJECTS, AGENTS, BUILTIN_AGENTS],
        )
        height = page.evaluate("() => document.querySelector('.shell').scrollHeight + 40")
        page.screenshot(path=str(OUT), clip={"x": 0, "y": 0, "width": 1440, "height": height})
        browser.close()
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
