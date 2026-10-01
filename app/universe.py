"""Find every active USDT pair whose previous completed UTC day meets the quote-volume floor."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.models import Symbol


def previous_utc_day(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)


async def eligible_symbols(
    exchange,
    market: str,
    min_turnover: float,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    stopped: Callable[[], bool] | None = None,
    now: datetime | None = None,
) -> tuple[datetime, list[tuple[Symbol, float]], list[str]]:
    day = previous_utc_day(now)
    symbols = await exchange.get_symbols(market)
    eligible: list[tuple[Symbol, float]] = []
    errors: list[str] = []
    checked = 0
    for offset in range(0, len(symbols), 8):
        if stopped and stopped():
            break
        batch = symbols[offset:offset + 8]
        outcomes = await asyncio.gather(
            *(exchange.get_klines(symbol, '1d', 3) for symbol in batch),
            return_exceptions=True,
        )
        for symbol, outcome in zip(batch, outcomes):
            checked += 1
            if isinstance(outcome, BaseException):
                errors.append(f'{symbol.pair}: {type(outcome).__name__}: {outcome}')
            else:
                candle = next((c for c in outcome if c.time.astimezone(timezone.utc) == day), None)
                if candle and candle.quote_volume >= min_turnover:
                    eligible.append((symbol, candle.quote_volume))
            if progress:
                progress(checked, len(symbols), symbol.pair)
    eligible.sort(key=lambda item: item[1], reverse=True)
    return day, eligible, errors
