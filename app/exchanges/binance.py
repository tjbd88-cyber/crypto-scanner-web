from datetime import datetime, timezone

from app.exchanges.base import ApiError, BaseExchange
from app.models import Candle, Symbol, Ticker


class BinanceExchange(BaseExchange):
    name = 'binance'

    def url(self, market: str) -> str:
        return 'https://api.binance.com/api/v3' if market == 'spot' else 'https://fapi.binance.com/fapi/v1'

    async def get_symbols(self, market: str) -> list[Symbol]:
        data = await self.http.get(self.url(market) + '/exchangeInfo', ttl=600)
        return [Symbol(self.name, row['symbol'], row['baseAsset'], market) for row in data['symbols']
                if row.get('status') == 'TRADING' and row.get('quoteAsset') == 'USDT'
                and (market == 'spot' or row.get('contractType') == 'PERPETUAL')]

    async def get_tickers(self, market: str) -> dict[str, Ticker]:
        data = await self.http.get(self.url(market) + '/ticker/24hr', ttl=30)
        return {row['symbol']: Ticker(row['symbol'], float(row['lastPrice']), float(row['priceChangePercent']), float(row['quoteVolume'])) for row in data if float(row['lastPrice']) > 0}

    async def get_klines(self, symbol: Symbol, period: str, limit: int) -> list[Candle]:
        rows: list = []
        end_time: int | None = None
        while len(rows) < limit:
            params = {'symbol': symbol.pair, 'interval': period, 'limit': min(limit-len(rows), 1000)}
            if end_time is not None:
                params['endTime'] = end_time
            page = await self.http.get(self.url(symbol.market) + '/klines', params, ttl=40)
            if not isinstance(page, list):
                raise ApiError('Binance K线格式异常')
            if not page:
                break
            rows = page + rows
            end_time = int(page[0][0]) - 1
            if len(page) < params['limit']:
                break
        return [Candle(datetime.fromtimestamp(row[0]/1000, timezone.utc), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]), float(row[7])) for row in rows]
