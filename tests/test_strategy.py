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


def test_volume_boosts_launch_score_without_being_required():
    bars = sample()
    assert analyze(bars[:-1]).stage == '双转换确认'
    normal = analyze(bars)
    assert normal.stage == '启动' and normal.volume_ratio == 1
    last = bars[-1]
    bars[-1] = Candle(last.time,last.open,last.high,last.low,last.close,400,400*last.close)
    boosted = analyze(bars)
    assert boosted.stage == '启动' and boosted.score > normal.score


def test_narrowing_platform_with_real_indicators_can_launch_without_three_percent_decline():
    prices = [110-i*.15 for i in range(50)]
    prices += [95+(1-i/40)*(.8 if i%2 else -.8) for i in range(40)]
    prices += [95.2,95.5,96,96.5,97,97.5]
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    bars = []
    for i, price in enumerate(prices):
        opened = prices[i-1] if i else price
        bars.append(Candle(start+timedelta(hours=4*i),opened,max(opened,price)+.1,
                           min(opened,price)-.1,price,100,100*price))
    early = analyze(bars[:90])
    launched = analyze(bars)
    assert early.stage == '观察'
    assert launched.stage == '启动' and launched.pattern_type == '低位横盘蓄势'
    assert launched.decline_bars == 0 and launched.volume_ratio == 1
    assert launched.ha_flip_bars_ago == launched.sar_flip_bars_ago == 5
