from __future__ import annotations

from app.models import Candle
from math import sqrt


def ema(values: list[float], period: int) -> list[float]:
    result = [values[0]]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def macd(candles: list[Candle], fast: int = 5, slow: int = 21, signal: int = 4) -> tuple[list[float], list[float], list[float]]:
    closes = [c.close for c in candles]
    dif = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    dea = ema(dif, signal)
    return dif, dea, [(a - b) * 2 for a, b in zip(dif, dea)]


def bollinger(candles: list[Candle], period: int = 20, deviations: float = 2.0) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """布林轨：收盘价的 N 根简单均线，加减指定倍数的总体标准差。"""
    closes = [c.close for c in candles]
    middle: list[float | None] = []
    upper: list[float | None] = []
    lower: list[float | None] = []
    for index in range(len(closes)):
        if index + 1 < period:
            middle.append(None)
            upper.append(None)
            lower.append(None)
            continue
        window = closes[index - period + 1:index + 1]
        mean = sum(window) / period
        standard_deviation = sqrt(sum((value - mean) ** 2 for value in window) / period)
        middle.append(mean)
        upper.append(mean + deviations * standard_deviation)
        lower.append(mean - deviations * standard_deviation)
    return middle, upper, lower


def heikin_ashi(candles: list[Candle]) -> list[Candle]:
    result: list[Candle] = []
    for candle in candles:
        close = (candle.open + candle.high + candle.low + candle.close) / 4
        opened = (candle.open + candle.close) / 2 if not result else (result[-1].open + result[-1].close) / 2
        result.append(Candle(candle.time, opened, max(candle.high, opened, close), min(candle.low, opened, close), close, candle.volume, candle.quote_volume))
    return result


def parabolic_sar(candles: list[Candle], step: float = 0.02, maximum: float = 0.2) -> list[float]:
    """标准 PSAR：反转时取上一趋势极值，并以此前两根K线约束 SAR。"""
    if len(candles) < 2:
        return [candles[0].low] if candles else []
    rising = candles[1].close >= candles[0].close
    extreme = max(c.high for c in candles[:2]) if rising else min(c.low for c in candles[:2])
    sar = min(c.low for c in candles[:2]) if rising else max(c.high for c in candles[:2])
    result = [sar, sar]
    acceleration = step
    for i in range(2, len(candles)):
        candidate = sar + acceleration * (extreme - sar)
        if rising:
            candidate = min(candidate, candles[i-1].low, candles[i-2].low)
            if candles[i].low < candidate:
                rising, candidate, extreme, acceleration = False, extreme, candles[i].low, step
            elif candles[i].high > extreme:
                extreme, acceleration = candles[i].high, min(maximum, acceleration + step)
        else:
            candidate = max(candidate, candles[i-1].high, candles[i-2].high)
            if candles[i].high > candidate:
                rising, candidate, extreme, acceleration = True, extreme, candles[i].high, step
            elif candles[i].low < extreme:
                extreme, acceleration = candles[i].low, min(maximum, acceleration + step)
        sar = candidate
        result.append(sar)
    return result
