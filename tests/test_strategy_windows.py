"""Synthetic closed bars and controlled indicators isolate candidate admission from scoring.

These are boundary scenarios, not real-market or screenshot acceptance results.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import app.strategy as strategy
from app.models import Candle


def platform():
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    prices = [115-i*.1 for i in range(40)] + [100]*60
    return [Candle(start+timedelta(hours=4*i), price, price+.2, price-.2,
                   price, 100, price*100) for i, price in enumerate(prices)]


def controlled(monkeypatch, bars, ha_age=0, sar_age=0, lower_wick=0):
    n = len(bars)
    ha = []
    sar = []
    for i, bar in enumerate(bars):
        bull = 0 <= n-1-ha_age <= i
        opened = bar.close-.5 if bull else bar.close+.5
        ha.append(replace(bar, open=opened, high=max(opened, bar.close)+.2,
                          low=min(opened, bar.close)-lower_wick))
        sar.append(bar.low-.1 if 0 <= n-1-sar_age <= i else bar.high+1)
    monkeypatch.setattr(strategy, 'heikin_ashi', lambda c: ha)
    monkeypatch.setattr(strategy, 'parabolic_sar', lambda c: sar)
    # Deliberately no MACD improvement, high KDJ, no volume spike, and far from lower BOLL.
    monkeypatch.setattr(strategy, 'macd', lambda *args: ([-1]*n, [-.5]*n, [-1]*n))
    monkeypatch.setattr(strategy, 'kdj', lambda c: ([85]*n, [86]*n, [83]*n))
    monkeypatch.setattr(strategy, 'bollinger', lambda *args: ([110]*n, [170]*n, [50]*n))
    return ha, sar


@pytest.mark.parametrize('ha_age,sar_age', [(0,0),(1,0),(2,0),(3,0),(5,0),(6,0),(7,0),(0,7)])
def test_seven_bar_resonance_admits_without_auxiliary_gates(monkeypatch, ha_age, sar_age):
    bars = platform()
    controlled(monkeypatch, bars, ha_age, sar_age)
    signal = strategy.analyze(bars)
    assert signal.stage == '双转换确认'
    assert signal.resonance_bars == abs(ha_age-sar_age)
    assert signal.pattern_type == '低位横盘蓄势' and signal.decline_bars == 0
    assert signal.score_breakdown['MACD'] == signal.score_breakdown['KDJ'] == 0
    assert signal.score_breakdown['布林带'] == signal.score_breakdown['成交量'] == 0
    assert signal.score == sum(signal.score_breakdown.values())


@pytest.mark.parametrize('ha_age,sar_age', [(8,0),(10,0),(8,8),(10,10)])
def test_eight_to_ten_bars_are_observation_only(monkeypatch, ha_age, sar_age):
    bars = platform()
    controlled(monkeypatch, bars, ha_age, sar_age)
    signal = strategy.analyze(bars)
    assert signal.stage == '观察' and signal.freshness == '扩展观察'
    assert signal.score_breakdown['双转换'] == 0
    assert any('超过 7 根' in warning for warning in signal.warnings)


@pytest.mark.parametrize('age', [11,20])
def test_stale_flips_are_not_recycled_into_new_startups(monkeypatch, age):
    bars = platform()
    controlled(monkeypatch, bars, age, age)
    assert strategy.analyze(bars) is None


def test_bearish_reversal_cancels_confirmation(monkeypatch):
    bars = platform()
    _, sar = controlled(monkeypatch, bars, 2, 1)
    sar[-1] = bars[-1].high+1
    signal = strategy.analyze(bars)
    assert signal is None or signal.stage in ('观察','临界')


def test_sar_equal_to_low_does_not_create_a_fake_second_flip(monkeypatch):
    bars = platform()
    _, sar = controlled(monkeypatch, bars, 3, 3)
    sar[-2] = bars[-2].low
    signal = strategy.analyze(bars)
    assert signal.stage == '双转换确认' and signal.sar_flip_bars_ago == 3


def test_long_ha_shadow_lowers_score_but_does_not_remove_candidate(monkeypatch):
    bars = platform()
    controlled(monkeypatch, bars, 1, 0, lower_wick=0)
    strong = strategy.analyze(bars)
    controlled(monkeypatch, bars, 1, 0, lower_wick=2)
    weak = strategy.analyze(bars)
    assert strong.stage == weak.stage == '双转换确认'
    assert strong.score > weak.score
    assert any('下影线偏长' in warning for warning in weak.warnings)


def test_mild_decline_does_not_require_three_percent(monkeypatch):
    bars = platform()
    prices = [100-i*.15 for i in range(12)]
    for i, price in enumerate(prices, 87):
        bars[i] = replace(bars[i], open=price+.15, close=price, high=price+.35, low=price-.2)
    bars[-1] = replace(bars[-1], open=98.5, high=99, low=98.3, close=98.8)
    controlled(monkeypatch, bars)
    signal = strategy.analyze(bars)
    assert signal.stage == '双转换确认' and signal.pattern_type == '底部下跌反转'
    assert 1 <= signal.decline_pct < 3


def test_recent_flips_without_a_base_do_not_admit_a_strong_uptrend(monkeypatch):
    bars = platform()
    bars = [replace(bar, open=50+i*2, close=51+i*2, high=52+i*2, low=49+i*2)
            for i, bar in enumerate(bars)]
    controlled(monkeypatch, bars)
    assert strategy.analyze(bars) is None


def test_secondary_setup_is_labeled_but_still_requires_new_flips(monkeypatch):
    bars = platform()
    for i in range(67,78):
        price = 100+(i-66)
        bars[i] = replace(bars[i], open=price, close=price, high=price+.2, low=price-.2)
    for i in range(78,100):
        bars[i] = replace(bars[i], open=105, close=105, high=105.2, low=104.8)
    ha, sar = controlled(monkeypatch, bars, 3, 2)
    for i in range(65,80):
        ha[i] = replace(ha[i], open=ha[i].close-.5)
    for i in range(66,80):
        sar[i] = bars[i].low-.1
    signal = strategy.analyze(bars)
    assert signal.pattern_type == '双转换后二次启动' and signal.stage == '双转换确认'
    sar[-1] = bars[-1].high+1
    signal = strategy.analyze(bars)
    assert signal.stage in ('观察','临界')


def test_low_platform_before_conversion_is_only_observation(monkeypatch):
    bars = platform()
    controlled(monkeypatch, bars, 200, 200)
    signal = strategy.analyze(bars)
    assert signal.stage == '观察' and signal.score_breakdown['双转换'] == 0
