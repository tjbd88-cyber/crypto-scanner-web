from datetime import datetime, timedelta, timezone

from app.models import Candle
from app.services.periods import build_candles


def daily(start, count):
    return [Candle(start+timedelta(days=i),100+i,101+i,99+i,100.5+i,10,1000) for i in range(count)]


def test_weekly_uses_complete_utc_monday_groups_only():
    monday = datetime(2026,9,7,tzinfo=timezone.utc)
    bars = daily(monday,16)
    result = build_candles(bars,'1w',monday+timedelta(days=16))
    assert len(result) == 2
    assert result[0].time == monday
    assert result[0].open == bars[0].open
    assert result[0].close == bars[6].close
    assert result[0].quote_volume == 7000


def test_multi_day_drops_partial_group():
    anchor = datetime(1970,1,1,tzinfo=timezone.utc)
    bars = daily(anchor,7)
    result = build_candles(bars,'3d',anchor+timedelta(days=7))
    assert len(result) == 2
    assert result[1].close == bars[5].close
