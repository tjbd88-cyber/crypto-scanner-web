from datetime import datetime, timedelta, timezone

from app.models import Candle
from app.strategy import analyze


def sample():
    prices = [100 + (i % 5 - 2)*.12 for i in range(75)]
    prices += [100,98.5,97,95.5,94,92.5,91,89.5,88.5,88.2,88.4,89,90,91,92,93,94]
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    bars = []
    for i, price in enumerate(prices):
        opened = prices[i-1] if i else price
        volume = 100 if i < 84 else 150
        bars.append(Candle(start+timedelta(hours=4*i),opened,max(opened,price)+.5,
                           min(opened,price)-.5,price,volume,volume*price))
    return bars


def test_stage_progression_and_no_premature_confirmation():
    bars = sample()
    assert analyze(bars[:82]) is None
    assert analyze(bars[:85]).stage == '观察'
    assert analyze(bars[:88]).stage == '临界'
    signal = analyze(bars[:89])
    assert signal.stage == '双转换确认'
    assert signal.ha_flip_bars_ago == 1
    assert signal.sar_flip_bars_ago == 0


def test_launch_requires_volume_and_middle_band_recovery():
    bars = sample()
    assert analyze(bars).stage == '双转换确认'
    last = bars[-1]
    bars[-1] = Candle(last.time,last.open,last.high,last.low,last.close,400,400*last.close)
    assert analyze(bars).stage == '启动'
