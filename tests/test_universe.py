import asyncio
from datetime import datetime, timedelta, timezone

from app.models import Candle, Symbol
from app.universe import eligible_symbols, previous_utc_day


def test_previous_day_screen_checks_every_symbol_and_requires_exact_day():
    now = datetime(2026, 10, 1, 11, tzinfo=timezone.utc)
    yesterday = previous_utc_day(now)
    pairs = ['AUSDT', 'BUSDT', 'CUSDT', 'DUSDT']

    class Exchange:
        async def get_symbols(self, market):
            return [Symbol('binance', pair, pair[:-4], market) for pair in pairs]

        async def get_klines(self, symbol, period, limit):
            assert (period, limit) == ('1d', 3)
            if symbol.pair == 'DUSDT':
                raise RuntimeError('HTTP 429')
            volume = {'AUSDT': 12_000_000, 'BUSDT': 11_000_000, 'CUSDT': 20_000_000}[symbol.pair]
            day = yesterday - timedelta(days=1) if symbol.pair == 'CUSDT' else yesterday
            return [Candle(day, 1, 1, 1, 1, 1, volume)]

    updates = []
    day, eligible, errors = asyncio.run(eligible_symbols(
        Exchange(), 'spot', 10_000_000, now=now,
        progress=lambda done, total, pair: updates.append((done, total, pair)),
    ))
    assert day == yesterday
    assert [item[0].pair for item in eligible] == ['AUSDT', 'BUSDT']
    assert all(item[1] >= 10_000_000 for item in eligible)
    assert len(updates) == 4 and updates[-1] == (4, 4, 'DUSDT')
    assert len(errors) == 1 and 'DUSDT' in errors[0]
