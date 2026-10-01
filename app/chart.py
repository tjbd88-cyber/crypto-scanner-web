"""Indicator series for the result detail chart. Display settings never change a scan signal."""
from __future__ import annotations

from app.indicators.core import bollinger, heikin_ashi, macd, parabolic_sar
from app.models import Candle
from app.strategy import kdj


def chart_payload(candles: list[Candle], *, boll_period: int = 20, boll_std: float = 2,
                  macd_fast: int = 5, macd_slow: int = 21, macd_signal: int = 4,
                  sar_step: float = .02, sar_max: float = .2,
                  kdj_period: int = 13, kdj_smooth: int = 3) -> dict:
    mid, upper, lower = bollinger(candles, boll_period, boll_std)
    ha = heikin_ashi(candles)
    sar = parabolic_sar(candles, sar_step, sar_max)
    dif, dea, hist = macd(candles, macd_fast, macd_slow, macd_signal)
    k, d, j = kdj(candles, kdj_period, kdj_smooth)
    return {'bars': [{
        'time': c.time.isoformat(), 'open': c.open, 'high': c.high,
        'low': c.low, 'close': c.close, 'volume': c.volume,
        'ha_open': h.open, 'ha_high': h.high, 'ha_low': h.low, 'ha_close': h.close,
        'boll_mid': mid[i], 'boll_upper': upper[i], 'boll_lower': lower[i],
        'sar': sar[i], 'macd_dif': dif[i], 'macd_dea': dea[i], 'macd_hist': hist[i],
        'kdj_k': k[i], 'kdj_d': d[i], 'kdj_j': j[i],
    } for i, (c, h) in enumerate(zip(candles, ha))]}
