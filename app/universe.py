"""Find every active USDT pair whose previous completed UTC day meets the quote-volume floor."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.models import Symbol
from app.exchanges.base import RateLimitError


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
    max_symbols: int | None = None,
    stats: dict | None = None,
) -> tuple[datetime, list[tuple[Symbol, float]], list[str]]:
    day = previous_utc_day(now)
    symbols = await exchange.get_symbols(market)
    stats = stats if stats is not None else {}
    stats.update(available_symbols=len(symbols), total_symbols=len(symbols[:max_symbols]),
                 processed_symbols=0, failed_symbols=0, skipped_symbols=0, filter_checked=0)
    symbols = symbols[:max_symbols]
    eligible: list[tuple[Symbol, float]] = []
    errors: list[str] = []
    checked = 0
    # Each host also has its own concurrency and pacing gate in PublicHttp.
    for offset in range(0, len(symbols), 3):
        if stopped and stopped():
            break
        batch = symbols[offset:offset + 3]
        outcomes = await asyncio.gather(
            *(exchange.get_klines(symbol, '1d', 3) for symbol in batch),
            return_exceptions=True,
        )
        for symbol, outcome in zip(batch, outcomes):
            checked += 1
            if isinstance(outcome, BaseException):
                if isinstance(outcome, RateLimitError):
                    stats['upstream_error'] = outcome.details
                    stats['halted'] = True
                    stats['failed_symbols' if outcome.request_sent else 'skipped_symbols'] += 1
                else:
                    stats['failed_symbols'] += 1
                    if len(errors) < 100:
                        errors.append(f'{symbol.pair}: 行情读取失败')
            else:
                candle = next((c for c in outcome if c.time.astimezone(timezone.utc) == day), None)
                if candle and candle.quote_volume >= min_turnover:
                    eligible.append((symbol, candle.quote_volume))
                else:
                    stats['processed_symbols'] += 1
            stats['filter_checked'] = checked
            if progress:
                progress(checked, len(symbols), symbol.pair)
        if stats.get('halted'):
            break
    stats['skipped_symbols'] += len(symbols)-checked
    stats['turnover_passed'] = len(eligible)
    eligible.sort(key=lambda item: item[1], reverse=True)
    return day, eligible, errors
