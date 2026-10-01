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
    repository=None,
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
        async def daily(symbol):
            key = f'{symbol.exchange}:{market}:{symbol.pair}'
            cached = repository.turnover(key, day.date().isoformat()) if repository else None
            if cached is not None:
                return cached
            candles = await exchange.get_klines(symbol, '1d', 3)
            candle = next((c for c in candles if c.time.astimezone(timezone.utc) == day), None)
            if candle and repository:
                repository.save_turnover(key, day.date().isoformat(), candle.quote_volume)
            return candle.quote_volume if candle else None

        outcomes = await asyncio.gather(
            *(daily(symbol) for symbol in batch),
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
                if outcome is not None and outcome >= min_turnover:
                    eligible.append((symbol, outcome))
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
