"""Bottom double-transition research screener. Input candles must all be closed."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean

from app.indicators.core import bollinger, heikin_ashi, macd, parabolic_sar
from app.models import Candle


@dataclass
class Signal:
    stage: str
    score: int
    reasons: list[str]
    warnings: list[str]
    decline_bars: int
    decline_pct: float
    rebound_pct: float
    sar_flip_bars_ago: int | None
    ha_flip_bars_ago: int | None
    macd_histogram: float
    kdj_k: float
    kdj_d: float
    kdj_j: float
    volume_ratio: float
    close: float
    candle_time: str

    def to_dict(self) -> dict:
        return asdict(self)


def kdj(candles: list[Candle], period: int = 13, smooth: int = 3) -> tuple[list[float], list[float], list[float]]:
    # Trading chart convention: RSV(13), then smoothed K and D with 1/3 weight.
    k, d = 50.0, 50.0
    ks, ds, js = [], [], []
    for i, candle in enumerate(candles):
        window = candles[max(0, i-period+1):i+1]
        low, high = min(c.low for c in window), max(c.high for c in window)
        rsv = 50.0 if high == low else 100 * (candle.close-low)/(high-low)
        k, d = ((smooth-1)*k+rsv)/smooth, ((smooth-1)*d+k)/smooth
        ks.append(k); ds.append(d); js.append(3*k-2*d)
    return ks, ds, js


def _flips(states: list[bool], lookback: int = 6) -> list[int]:
    return [i for i in range(max(1, len(states)-lookback), len(states)) if not states[i-1] and states[i]]


def analyze(candles: list[Candle]) -> Signal | None:
    if len(candles) < 70:
        return None
    c = candles[-160:]
    n = len(c)
    middle, upper, lower = bollinger(c, 20, 2)
    ha = heikin_ashi(c)
    sar = parabolic_sar(c)
    _, _, hist = macd(c, 5, 21, 4)
    kval, dval, jval = kdj(c)
    ha_bull = [x.close > x.open for x in ha]
    sar_bull = [value < x.low for value, x in zip(sar, c)]
    ha_flips, sar_flips = _flips(ha_bull), _flips(sar_bull)
    pairs = [(h, s) for h in ha_flips for s in sar_flips if abs(h-s) <= 3]
    pair = max(pairs, key=lambda p: max(p)) if pairs else None
    anchor = min(pair) if pair else n-1
    # A preceding bearish stretch must exist before the first transition.
    best = (0, 0.0, False)
    for length in range(5, 13):
        start, end = anchor-length, anchor
        if start < 25:
            continue
        segment = c[start:end]
        bears = sum(not ha_bull[i] for i in range(start, end))
        drop = 100 * (segment[0].open-segment[-1].close)/segment[0].open
        if bears >= length-1 and drop >= 3 and drop > best[1]:
            best = (length, drop, True)
    decline_bars, decline_pct, has_decline = best
    local_low = min(x.low for x in c[max(0, anchor-20):n])
    rebound = 100*(c[-1].close/local_low-1) if local_low else 0
    # Lower band was approached around the candidate low; current close remains near bottom.
    lower_touch = any(lower[i] is not None and c[i].low <= lower[i]*1.025 for i in range(max(19, anchor-5), min(n, anchor+3)))
    bottom = lower_touch and rebound <= 12
    # Negative MACD histogram shrinks by at least 25% from its recent trough.
    preceding = hist[max(0, anchor-8):anchor+1]
    trough = min(preceding) if preceding else 0
    momentum = trough < 0 and hist[-1] > trough*0.75 and hist[-1] >= hist[-2]
    kdj_low = min(kval[-5:]) <= 35 and (jval[-1] > jval[-2] or kval[-1] > kval[-2])
    body_recent = mean(abs(x.close-x.open) for x in c[-3:])
    body_prior = mean(abs(x.close-x.open) for x in c[-6:-3])
    fading = body_prior > 0 and body_recent <= body_prior*.75
    sar_gap = 100*(sar[-1]/c[-1].close-1)
    sar_near = not sar_bull[-1] and 0 <= sar_gap <= 3 and sar_gap < 100*(sar[-3]/c[-3].close-1)
    ha_recent = bool(ha_flips and n-1-ha_flips[-1] <= 3)
    volume_base = mean(x.volume for x in c[-6:-1])
    volume_ratio = c[-1].volume/volume_base if volume_base else 0
    confirmed = pair is not None and has_decline and bottom and momentum and kdj_low
    launched = confirmed and n-1-max(pair) <= 6 and c[-1].close > middle[-1] and volume_ratio >= 1.3
    critical = has_decline and bottom and momentum and kdj_low and (ha_recent or sar_near)
    observed = has_decline and bottom and (momentum or (kdj_low and fading))
    if launched:
        stage = '启动'
    elif confirmed and n-1-max(pair) <= 6:
        stage = '双转换确认'
    elif critical:
        stage = '临界'
    elif observed:
        stage = '观察'
    else:
        return None
    score = min(100, 25 + 15*has_decline + 10*bottom + 10*momentum + 8*kdj_low + 18*confirmed + 8*(volume_ratio>=1.3) + 6*(c[-1].close>middle[-1]))
    reasons = [f'前置回落 {decline_bars} 根 / {decline_pct:.1f}%', '接近布林下轨']
    if momentum: reasons.append('MACD 空头柱缩短')
    if kdj_low: reasons.append('KDJ 低位拐头')
    if fading: reasons.append('阴线实体缩短')
    if ha_recent: reasons.append('平均K线翻阳')
    if sar_near: reasons.append('SAR 接近价格')
    if confirmed: reasons.append(f'双转换相距 {abs(pair[0]-pair[1])} 根')
    if launched: reasons.append('站上布林中轨且放量')
    warnings = []
    if rebound > 8: warnings.append('已离低点超过 8%')
    if not confirmed: warnings.append('双转换尚未确认')
    return Signal(stage, score, reasons, warnings, decline_bars, round(decline_pct,2), round(rebound,2),
                  n-1-sar_flips[-1] if sar_flips else None, n-1-ha_flips[-1] if ha_flips else None,
                  round(hist[-1],8), round(kval[-1],2), round(dval[-1],2), round(jval[-1],2),
                  round(volume_ratio,2), c[-1].close, c[-1].time.isoformat())
