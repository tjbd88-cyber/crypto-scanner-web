from datetime import datetime, timezone

from app.exchanges.base import ApiError, BaseExchange
from app.models import Candle, Symbol, Ticker


class OKXExchange(BaseExchange):
    name = 'okx'
    base_url = 'https://www.okx.com/api/v5'

    async def request(self, path: str, params: dict, ttl: int = 0) -> list:
        data = await self.http.get(self.base_url + path, params, ttl)
        if not isinstance(data, dict) or data.get('code') != '0' or not isinstance(data.get('data'), list):
            raise ApiError(f'OKX 返回异常: {str(data)[:200]}')
        return data['data']

    async def get_symbols(self, market: str) -> list[Symbol]:
        rows = await self.request('/public/instruments', {'instType': 'SPOT' if market == 'spot' else 'SWAP'}, 600)
        return [Symbol(self.name, row['instId'], row['baseCcy'] if market == 'spot' else row['ctValCcy'], market) for row in rows
                if row.get('state') == 'live' and ((market == 'spot' and row.get('quoteCcy') == 'USDT') or (market != 'spot' and row['instId'].endswith('-USDT-SWAP')))]

    async def get_tickers(self, market: str) -> dict[str, Ticker]:
        rows = await self.request('/market/tickers', {'instType': 'SPOT' if market == 'spot' else 'SWAP'}, 30)
        result = {}
        for row in rows:
            price = float(row.get('last') or 0)
            opened = float(row.get('open24h') or 0)
            if price > 0:
                volume = float(row.get('volCcy24h') or 0) if market == 'spot' else float(row.get('volCcy24h') or 0) * price
                result[row['instId']] = Ticker(row['instId'], price, (price/opened-1)*100 if opened else 0, volume)
        return result

    async def get_klines(self, symbol: Symbol, period: str, limit: int) -> list[Candle]:
        bars = {'15m': '15m', '30m': '30m', '1h': '1H', '4h': '4H', '1d': '1Dutc'}
        rows = await self.request('/market/history-candles', {'instId': symbol.pair, 'bar': bars[period], 'limit': min(limit, 100)}, 40)
        # OKX 历史接口单页最多 100 根，按 before 翻页取更早的K线。
        while len(rows) < limit and rows:
            older = await self.request('/market/history-candles', {'instId': symbol.pair, 'bar': bars[period], 'limit': min(limit-len(rows), 100), 'after': rows[-1][0]}, 40)
            if not older or older[-1][0] == rows[-1][0]:
                break
            rows.extend(older)
        return sorted([Candle(datetime.fromtimestamp(int(r[0])/1000, timezone.utc), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7])) for r in rows[:limit] if len(r) >= 9 and r[8] == '1'], key=lambda c: c.time)
