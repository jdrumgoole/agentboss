"""Quota mapping: cost weighting, windows, and calibration from limit hits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentboss.models import LimitHit
from agentboss.parser import IncrementalTranscript
from agentboss.quota import Spend, build_report, five_hour_blocks, turn_cost, weekly_window

from .conftest import assistant_record, write_jsonl

NOW = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)


def test_turn_cost_uses_model_price_and_cache_multipliers():
    # Opus 5.5: $4 in / $20 out; cache read 0.1x, cache write 1.25x.
    cost = turn_cost("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert cost == 4 + 20 + 0.4 + 5
    # Longest prefix wins: opus-5-5 is not priced as opus-5.
    assert turn_cost("claude-opus-5", 1_000_000, 0, 0, 0) == 5


def test_five_hour_blocks_open_on_first_turn_after_previous_close():
    t0 = datetime(2026, 10, 28, 9, 35, tzinfo=UTC)
    blocks = five_hour_blocks([t0, t0 + timedelta(hours=2), t0 + timedelta(hours=6)])
    assert blocks == [
        (t0.replace(minute=0), t0.replace(minute=0) + timedelta(hours=5)),
        (t0.replace(minute=0) + timedelta(hours=6), t0.replace(minute=0) + timedelta(hours=11)),
    ]


def test_weekly_window_projects_a_known_reset_forward():
    anchor = datetime(2026, 10, 19, 10, tzinfo=UTC)
    start, end = weekly_window(NOW, anchor)
    assert (start, end) == (
        datetime(2026, 10, 26, 10, tzinfo=UTC),
        datetime(2026, 11, 2, 10, tzinfo=UTC),
    )


def test_report_calibrates_cap_from_limit_hit():
    reset = datetime(2026, 10, 26, 10, tzinfo=UTC)
    hit_at = reset - timedelta(days=1)
    ledger = {
        (reset - timedelta(days=3), "claude-opus-5-5"): Spend(turns=1, tokens=10, cost=100.0),
        (NOW - timedelta(hours=1), "claude-opus-5-5"): Spend(turns=1, tokens=10, cost=25.0),
    }
    hits = [LimitHit(at=hit_at, limit_type="seven_day", resets_at=reset)]
    report = build_report(ledger, hits, now=NOW)

    assert report.limit_hits[0].spend_at_hit == 100.0
    week = report.seven_day
    assert week.start == reset and week.cost == 25.0
    assert week.estimated_cap == 100.0 and week.used_fraction == 0.25
    assert report.five_hour.active and report.five_hour.cost == 25.0


def test_parser_records_rejections_and_spend(tmp_path):
    path = tmp_path / "s.jsonl"
    rejected = assistant_record(
        session_id="s",
        message_id="m2",
        model="<synthetic>",
        offset=60,
        usage={"input_tokens": 0, "output_tokens": 0},
    )
    rejected["quotaLimits"] = {
        "status": "rejected",
        "rateLimitType": "seven_day",
        "resetsAt": 1792404000,
    }
    write_jsonl(path, [assistant_record(session_id="s", message_id="m1"), rejected])

    reader = IncrementalTranscript(path)
    reader.refresh()
    stats = reader.stats
    assert len(stats.limit_hits) == 1 and stats.limit_hits[0][1]["rateLimitType"] == "seven_day"
    assert [model for _, model in stats.spend] == ["claude-opus-5"]  # synthetic turn not billed
    assert sum(c.cost for c in stats.spend.values()) > 0
