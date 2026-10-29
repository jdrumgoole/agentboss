"""Quota consumption.

Claude Code never writes the plan's live utilisation to disk.  What it does
write is (a) the usage of every API turn and (b) a `quotaLimits` object on the
synthetic assistant record it emits when a limit rejects a request::

    {"status": "rejected", "rateLimitType": "seven_day", "resetsAt": 1792404000, ...}

So consumption is *mapped*, not read: every turn is weighted by its
API-equivalent price (the closest public proxy for how plans meter usage), summed
over the same windows the limits use, and each recorded rejection calibrates an
estimated cap -- the spend in that window at the moment it was refused.

* `five_hour` -- a block opens at the hour of the first turn after the
  previous block closed, and lasts five hours.
* `seven_day` -- fixed weekly windows.  A recorded `resetsAt` pins the
  anchor; without one the window is the trailing seven days.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .models import LimitHit, QuotaReport, QuotaWindow, SpendBucket

#: USD per million tokens: (input, output).  Cache reads bill at 0.1x input,
#: cache writes at 1.25x.  Matched by prefix, longest first.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-5": (0.10, 0.50),
    "claude-haiku-4": (1.0, 5.0),
}
_DEFAULT_PRICE = (5.0, 25.0)
_PREFIXES = sorted(PRICES, key=len, reverse=True)

#: Granularity of the spend ledger.  Fine enough for a five-hour block edge.
BUCKET = timedelta(minutes=5)
FIVE_HOURS = timedelta(hours=5)
SEVEN_DAYS = timedelta(days=7)


def price_of(model: str | None) -> tuple[float, float]:
    name = (model or "").lower()
    for prefix in _PREFIXES:
        if name.startswith(prefix):
            return PRICES[prefix]
    return _DEFAULT_PRICE


def turn_cost(model: str | None, inp: int, out: int, cache_read: int, cache_creation: int) -> float:
    """API-equivalent USD for one turn."""
    p_in, p_out = price_of(model)
    return (inp * p_in + out * p_out + cache_read * p_in * 0.1 + cache_creation * p_in * 1.25) / 1e6


def bucket_of(when: datetime) -> datetime:
    return when.replace(minute=when.minute - when.minute % 5, second=0, microsecond=0)


@dataclass
class Spend:
    """One ledger cell: a model's turns in one five-minute bucket."""

    turns: int = 0
    tokens: int = 0
    cost: float = 0.0


def five_hour_blocks(starts: Iterable[datetime]) -> list[tuple[datetime, datetime]]:
    """Session blocks over sorted bucket starts."""
    blocks: list[tuple[datetime, datetime]] = []
    for when in starts:
        if blocks and when < blocks[-1][1]:
            continue
        start = when.replace(minute=0, second=0, microsecond=0)
        blocks.append((start, start + FIVE_HOURS))
    return blocks


def weekly_window(now: datetime, anchor: datetime | None) -> tuple[datetime, datetime]:
    """The seven-day window containing `now`, pinned to a known reset if any."""
    if anchor is None:
        return now - SEVEN_DAYS, now
    steps = (now - anchor) // SEVEN_DAYS
    end = anchor + SEVEN_DAYS * (steps + 1)
    return end - SEVEN_DAYS, end


def build_report(
    ledger: dict[tuple[datetime, str], Spend],
    hits: list[LimitHit],
    *,
    now: datetime | None = None,
    days: int = 30,
) -> QuotaReport:
    now = now or datetime.now(UTC)
    per_bucket: dict[datetime, float] = {}
    for (bucket, _model), spend in ledger.items():
        per_bucket[bucket] = per_bucket.get(bucket, 0.0) + spend.cost
    ordered = sorted(per_bucket)

    def spent(start: datetime, end: datetime) -> float:
        return sum(per_bucket[b] for b in ordered if start <= b < end)

    hits = sorted(hits, key=lambda h: h.at)
    weekly_resets = [h.resets_at for h in hits if h.limit_type == "seven_day" and h.resets_at]
    anchor = max(weekly_resets) if weekly_resets else None
    blocks = five_hour_blocks(ordered)

    # Calibrate: what had been spent in the window when each rejection landed.
    for hit in hits:
        if hit.limit_type == "seven_day" and hit.resets_at:
            hit.spend_at_hit = round(spent(hit.resets_at - SEVEN_DAYS, hit.at), 2)
        elif hit.limit_type == "five_hour":
            block = next((b for b in reversed(blocks) if b[0] <= hit.at), None)
            if block:
                hit.spend_at_hit = round(spent(block[0], hit.at), 2)

    def window(kind: str, start: datetime, end: datetime, active: bool) -> QuotaWindow:
        caps = [h.spend_at_hit for h in hits if h.limit_type == kind and h.spend_at_hit]
        cap = caps[-1] if caps else None
        cost = spent(start, end) if active else 0.0
        models: dict[str, float] = {}
        tokens = turns = 0
        if active:
            for (bucket, model), s in ledger.items():
                if start <= bucket < end:
                    models[model] = round(models.get(model, 0.0) + s.cost, 4)
                    tokens += s.tokens
                    turns += s.turns
        elapsed = max((min(now, end) - start).total_seconds(), 1.0)
        projected = cost * (end - start).total_seconds() / elapsed if active else 0.0
        return QuotaWindow(
            kind=kind,
            start=start,
            end=end,
            active=active,
            cost=round(cost, 4),
            tokens=tokens,
            turns=turns,
            models=dict(sorted(models.items(), key=lambda kv: -kv[1])),
            estimated_cap=cap,
            used_fraction=round(cost / cap, 4) if cap else None,
            projected_cost=round(projected, 4),
        )

    current = next((b for b in reversed(blocks) if b[0] <= now < b[1]), None)
    five = window("five_hour", *(current or (now, now + FIVE_HOURS)), active=current is not None)
    week = window("seven_day", *weekly_window(now, anchor), active=True)

    first_day = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily: dict[datetime, SpendBucket] = {}
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        daily[day] = SpendBucket(start=day)
    for (bucket, model), s in ledger.items():
        day = bucket.replace(hour=0, minute=0)
        slot = daily.get(day)
        if slot is None:
            continue
        slot.cost = round(slot.cost + s.cost, 4)
        slot.tokens += s.tokens
        slot.models[model] = round(slot.models.get(model, 0.0) + s.cost, 4)

    recent_blocks = [
        QuotaWindow(
            kind="five_hour", start=s, end=e, active=s <= now < e, cost=round(spent(s, e), 4)
        )
        for s, e in blocks
        if e > first_day
    ]
    return QuotaReport(
        generated_at=now,
        five_hour=five,
        seven_day=week,
        limit_hits=hits,
        daily=list(daily.values()),
        blocks=recent_blocks,
    )
