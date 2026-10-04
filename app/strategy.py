"""Bottom double-transition research screener. Input candles must all be closed."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import ceil
from statistics import mean

from app.indicators.core import bollinger, heikin_ashi, macd, parabolic_sar
from app.models import Candle

CORE_WINDOW = 7
OBSERVATION_WINDOW = 10
STRATEGY_VERSION = 'bottom_base_v2'


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
    pattern_type: str = ''
    resonance_bars: int | None = None
    freshness: str = ''
    strength: str = ''
    score_breakdown: dict[str, int] = field(default_factory=dict)
    strategy_version: str = STRATEGY_VERSION

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


def _flips(states: list[bool], lookback: int = OBSERVATION_WINDOW) -> list[int]:
    # Inclusive bars-ago boundary: age 7 and age 10 must not be lost to slicing.
    return [i for i in range(max(1, len(states)-1-lookback), len(states))
            if not states[i-1] and states[i]]


def _range_pct(candles: list[Candle]) -> float:
    return 100 * (max(c.high for c in candles)-min(c.low for c in candles)) / mean(c.close for c in candles)


def _compression(candles: list[Candle], anchor: int) -> tuple[bool, bool]:
    """Compression uses OHLC ranges; BOLL is evidence, never an admission gate."""
    window = candles[anchor-12:anchor]
    older, recent = _range_pct(window[:6]), _range_pct(window[6:])
    compressed = _range_pct(window) <= 8 and (recent <= older*.8 or max(older, recent) <= 2)
    context = candles[max(0, anchor-60):anchor]
    low, high = min(c.low for c in context), max(c.high for c in context)
    low_position = window[-1].close <= low + .6*(high-low)
    return compressed, low_position


def _decline(candles: list[Candle], ha_bull: list[bool], anchor: int) -> tuple[int, float]:
    best = (0, 0.0)
    for length in range(4, 17):
        segment = candles[anchor-length:anchor]
        drop = 100*(segment[0].open-segment[-1].close)/segment[0].open
        bearish = sum(not x for x in ha_bull[anchor-length:anchor]) >= ceil(length*.6)
        falling_center = mean(c.close for c in segment[:length//2]) > mean(c.close for c in segment[length//2:])
        if bearish and falling_center and drop >= 1 and drop > best[1]:
            best = (length, drop)
    return best


def _secondary(candles: list[Candle], ha_bull: list[bool], sar_bull: list[bool], anchor: int) -> bool:
    """A prior double flip plus a held-low consolidation is a secondary setup.

    This labels the setup, not a new confirmation: confirmation still needs two recent flips.
    """
    ha_old, sar_old = _flips(ha_bull, 40), _flips(sar_bull, 40)
    pairs = [(h, s) for h in ha_old for s in sar_old
             if abs(h-s) <= CORE_WINDOW and max(h, s) < anchor-12
             and ha_bull[max(h,s)] and sar_bull[max(h,s)]]
    if not pairs:
        return False
    h, s = max(pairs, key=lambda pair: max(pair))
    end = max(h, s)
    low = min(c.low for c in candles[max(0, min(h, s)-12):end+1])
    peak = max(c.high for c in candles[end:anchor])
    platform_low = min(c.low for c in candles[anchor-12:anchor])
    return peak >= candles[end].close*1.01 and low*.995 <= platform_low <= peak*.99


def analyze(candles: list[Candle]) -> Signal | None:
    if len(candles) < 70:
        return None
    c = candles[-160:]
    n = len(c)
    middle, upper, lower = bollinger(c, 20, 2)
    ha = heikin_ashi(c)
    sar = parabolic_sar(c)
    dif, dea, hist = macd(c, 5, 21, 4)
    kval, dval, jval = kdj(c)
    ha_bull = [x.close > x.open for x in ha]
    # SAR can equal the candle low because of its two-bar clamp; equality is not a bearish flip.
    sar_bull = []
    for value, candle in zip(sar, c):
        bullish = value <= candle.low
        if candle.low == candle.high == value:
            bullish = sar_bull[-1] if sar_bull else False
        sar_bull.append(bullish)
    ha_flips, sar_flips = _flips(ha_bull), _flips(sar_bull)
    # Use the latest transitions; never reuse an older attractive pair after a bearish reversal.
    active_ha = ha_flips[-1] if ha_flips and ha_bull[-1] else None
    active_sar = sar_flips[-1] if sar_flips and sar_bull[-1] else None
    pair = (active_ha, active_sar) if active_ha is not None and active_sar is not None else None
    anchor = min(pair) if pair else active_ha if active_ha is not None else active_sar if active_sar is not None else n-1
    decline_bars, decline_pct = _decline(c, ha_bull, anchor)
    compressed, low_position = _compression(c, anchor)
    secondary = compressed and _secondary(c, ha_bull, sar_bull, anchor)
    pattern = ('双转换后二次启动' if secondary else '底部下跌反转' if decline_bars
               else '低位横盘蓄势' if compressed and low_position else '')
    if not pattern:
        return None
    ha_age = n-1-ha_flips[-1] if ha_flips else None
    sar_age = n-1-sar_flips[-1] if sar_flips else None
    gap = abs(pair[0]-pair[1]) if pair else None
    age = max(ha_age, sar_age) if pair else None
    both_bull = ha_bull[-1] and sar_bull[-1]
    confirmed = both_bull and pair is not None and age <= CORE_WINDOW and gap <= CORE_WINDOW
    extended = both_bull and pair is not None and not confirmed and age <= OBSERVATION_WINDOW
    local_low = min(x.low for x in c[max(0, anchor-20):n])
    rebound = 100*(c[-1].close/local_low-1) if local_low else 0
    lower_touch = any(lower[i] is not None and c[i].low <= lower[i]*1.025 for i in range(max(19, anchor-5), min(n, anchor+3)))
    preceding = hist[max(0, anchor-8):anchor+1]
    trough = min(preceding) if preceding else 0
    momentum = trough < 0 and hist[-1] > trough*0.75 and hist[-1] >= hist[-2]
    dif_up = dif[-1] > dif[-2]
    macd_cross = dif[-2] <= dea[-2] and dif[-1] > dea[-1]
    histogram_up = hist[-1] > 0 and hist[-1] > hist[-2]
    kdj_up = jval[-1] > jval[-2] or kval[-1] > kval[-2]
    kdj_cross = kval[-2] <= dval[-2] and kval[-1] > dval[-1]
    body_recent = mean(abs(x.close-x.open) for x in ha[-3:])
    body_prior = mean(abs(x.close-x.open) for x in ha[-6:-3])
    fading = body_prior > 0 and body_recent <= body_prior*.75
    sar_gap = 100*(sar[-1]/c[-1].close-1)
    sar_near = not sar_bull[-1] and 0 <= sar_gap <= 3 and sar_gap < 100*(sar[-3]/c[-3].close-1)
    ha_recent = ha_age is not None and ha_age <= CORE_WINDOW and ha_bull[-1]
    held_low = min(x.low for x in c[-3:]) >= min(x.low for x in c[-8:-3])*.995
    volume_base = mean(x.volume for x in c[-6:-1])
    volume_ratio = c[-1].volume/volume_base if volume_base else 0
    volume_prior = mean(x.volume for x in c[anchor-24:anchor-12])
    volume_dry = volume_prior > 0 and mean(x.volume for x in c[anchor-12:anchor]) <= volume_prior*.8
    width = [(u-l)/m if m else 0 for m, u, l in zip(middle[19:], upper[19:], lower[19:])]
    boll_squeeze = mean(width[-6:]) <= mean(width[-12:-6])*.8
    above_middle = c[-1].close > middle[-1]
    upper_expanding = upper[-1] > upper[-2] and width[-1] > width[-2]
    ha_body = abs(ha[-1].close-ha[-1].open)
    lower_wick = min(ha[-1].open, ha[-1].close)-ha[-1].low
    wick_ratio = lower_wick/ha_body if ha_body else float('inf')
    bullish_run = 0
    for bullish in reversed(ha_bull):
        if not bullish:
            break
        bullish_run += 1
    ha_strong = ha_bull[-1] and wick_ratio <= .2 and bullish_run >= 2
    evidence = sum((upper_expanding, volume_ratio >= 1.3, histogram_up or macd_cross, kdj_up, ha_strong))
    launched = confirmed and above_middle and evidence >= 2
    critical = (ha_recent or sar_near) and (held_low or fading or compressed)
    observed = fading or held_low or compressed or momentum
    if launched:
        stage = '启动'
    elif confirmed:
        stage = '双转换确认'
    elif extended:
        stage = '观察'
    elif both_bull and not secondary:
        # A stale dual flip must not be promoted to a new startup through auxiliary evidence.
        return None
    elif critical:
        stage = '临界'
    elif observed:
        stage = '观察'
    else:
        return None
    resonance = 10 if gap == 0 else 8 if gap is not None and gap <= 2 else 5 if gap is not None and gap <= 5 else 2
    freshness = ('非常新鲜' if age is not None and age <= 2 else '正常' if age is not None and age <= 5
                 else '仍然有效' if confirmed else '扩展观察' if extended else '等待转换')
    kdj_points = (8 if kdj_cross and min(kval[-2:]) <= 35 else 5 if kdj_cross else 4 if kdj_up and min(kval[-5:]) <= 35 else 2 if kdj_up else 0) if kval[-1] <= 80 else 0
    ha_points = (4 if wick_ratio <= 1e-6 else 3 if wick_ratio <= .2 else 0) + min(3, max(0, bullish_run-1)) if ha_bull[-1] else 0
    points = {'基础结构':20, '双转换':20 if confirmed else 0,
              '共振间隔':resonance if confirmed else 0,
              '转换新鲜度':(5 if age <= 2 else 3 if age <= 5 else 1) if confirmed else 0,
              'MACD':4*int(momentum)+2*int(dif_up)+2*int(macd_cross)+2*int(histogram_up),
              'KDJ':kdj_points, '布林带':2*int(lower_touch)+2*int(boll_squeeze)+4*int(above_middle)+4*int(upper_expanding),
              '成交量':3*int(volume_dry)+5*int(volume_ratio>=1.3), '平均K线':ha_points,
              '过度上涨扣分':-12 if rebound > 20 else -6 if rebound > 12 else 0}
    score = max(0, min(100, sum(points.values())))
    platform_high = max(x.high for x in c[anchor-12:anchor])
    strength = '强势启动' if launched and c[-1].close > platform_high and evidence >= 3 else '启动' if launched else '强共振' if confirmed and gap <= 2 else '正常共振' if confirmed and gap <= 5 else '候选共振' if confirmed else '临界预警' if stage == '临界' else '观察信号'
    reasons = [pattern]
    if decline_bars: reasons.append(f'前置回落 {decline_bars} 根 / {decline_pct:.1f}%')
    if compressed: reasons.append('平台振幅压缩')
    if lower_touch: reasons.append('接近布林下轨')
    if boll_squeeze: reasons.append('布林收口')
    if momentum: reasons.append('MACD 空头柱缩短')
    if dif_up: reasons.append('MACD DIF 拐头向上')
    if macd_cross: reasons.append('MACD 金叉')
    if histogram_up: reasons.append('MACD 正柱扩大')
    if kdj_points: reasons.append('KDJ 低位拐头' if min(kval[-5:]) <= 35 else 'KDJ 向上')
    if fading: reasons.append('阴线实体缩短')
    if ha_recent: reasons.append('平均K线翻阳')
    if sar_near: reasons.append('SAR 接近价格')
    if confirmed: reasons.append(f'双转换相距 {gap} 根 · {freshness}')
    if extended: reasons.append('双转换处于 8–10 根扩展观察窗口')
    if above_middle: reasons.append('站上布林中轨')
    if upper_expanding: reasons.append('布林上轨向上扩张')
    if volume_dry: reasons.append('底部缩量')
    if volume_ratio >= 1.3: reasons.append('成交量放大')
    if ha_bull[-1] and wick_ratio <= 1e-6: reasons.append('平均K线无下影线')
    elif ha_bull[-1] and wick_ratio <= .2: reasons.append('平均K线下影线短')
    if strength == '强势启动': reasons.append('突破前平台高点')
    warnings = []
    if rebound > 8: warnings.append('已离低点超过 8%')
    if not confirmed and not extended: warnings.append('双转换尚未确认')
    if extended: warnings.append('转换已超过 7 根，仅作观察，不算新启动')
    if ha_flips and not ha_bull[-1]: warnings.append('平均K线已重新转阴，旧转换不作确认')
    if sar_flips and not sar_bull[-1]: warnings.append('SAR 已重新转空，旧转换不作确认')
    if ha_bull[-1] and wick_ratio > .2: warnings.append('平均K线下影线偏长，启动强度较弱')
    return Signal(stage, score, reasons, warnings, decline_bars, round(decline_pct,2), round(rebound,2),
                  sar_age, ha_age,
                  round(hist[-1],8), round(kval[-1],2), round(dval[-1],2), round(jval[-1],2),
                  round(volume_ratio,2), c[-1].close, c[-1].time.isoformat(),
                  pattern, gap, freshness, strength, points)
