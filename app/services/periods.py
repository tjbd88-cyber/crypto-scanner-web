from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import Candle

BASE_PERIOD = {'2d': ('1d', 2), '3d': ('1d', 3), '5d': ('1d', 5), '15d': ('1d', 15), '1M': ('1d', 31), '2h': ('1h', 2), '6h': ('1h', 6), '12h': ('1h', 12), '8h': ('4h', 2), '1w': ('1d', 7)}
SECONDS = {'15m': 900, '30m': 1800, '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600, '8h': 28800, '12h': 43200, '1d': 86400, '2d': 172800, '3d': 259200, '5d': 432000, '1w': 604800, '15d': 1296000, '1M': 2678400}


def bucket_start(value: datetime, period: str) -> datetime:
    value = value.astimezone(timezone.utc)
    if period == '1M':
        return datetime(value.year, value.month, 1, tzinfo=timezone.utc)
    if period == '1w':
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc) - timedelta(days=value.weekday())
    seconds = SECONDS[period]
    anchor = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return anchor + timedelta(seconds=int((value - anchor).total_seconds()) // seconds * seconds)


def period_end(start: datetime, period: str) -> datetime:
    if period == '1M':
        return datetime(start.year + (start.month == 12), start.month % 12 + 1, 1, tzinfo=timezone.utc)
    return start + timedelta(seconds=SECONDS[period])


def build_candles(candles: list[Candle], period: str, now: datetime | None = None) -> list[Candle]:
    """按 UTC 时间边界聚合，并丢弃未完成的首尾分组。"""
    if not candles:
        return []
    now = now or datetime.now(timezone.utc)
    base, _ = BASE_PERIOD.get(period, (period, 1))
    base_seconds = SECONDS[base]
    groups: dict[datetime, list[Candle]] = {}
    for candle in sorted(candles, key=lambda item: item.time):
        groups.setdefault(bucket_start(candle.time, period), []).append(candle)
    result: list[Candle] = []
    for start, group in groups.items():
        end = period_end(start, period)
        expected = int((end - start).total_seconds() / base_seconds)
        if end > now or len(group) != expected or group[0].time != start:
            continue
        if any((b.time - a.time).total_seconds() != base_seconds for a, b in zip(group, group[1:])):
            continue
        result.append(Candle(start, group[0].open, max(c.high for c in group), min(c.low for c in group), group[-1].close, sum(c.volume for c in group), sum(c.quote_volume for c in group)))
    return result
