from datetime import datetime, timezone

from app.exchanges.base import ApiError, BaseExchange
from app.models import Candle, Symbol, Ticker


class GateExchange(BaseExchange):
    name = 'gate'
    base_url = 'https://api.gateio.ws/api/v4'

    def path(self, market: str) -> str:
        return '/spot' if market == 'spot' else '/futures/usdt'

    async def get_symbols(self, market: str) -> list[Symbol]:
        rows = await self.http.get(self.base_url + self.path(market) + ('/currency_pairs' if market == 'spot' else '/contracts'), ttl=600)
        if not isinstance(rows, list):
            raise ApiError('Gate 交易对格式异常')
        if market == 'spot':
            return [Symbol(self.name, r['id'], r['base'], market) for r in rows if r.get('quote') == 'USDT' and r.get('trade_status') == 'tradable']
        return [Symbol(self.name, r['name'], r['name'].split('_')[0], market) for r in rows if r['name'].endswith('_USDT') and not r.get('in_delisting')]

    async def get_tickers(self, market: str) -> dict[str, Ticker]:
        rows = await self.http.get(self.base_url + self.path(market) + '/tickers', ttl=30)
        if not isinstance(rows, list):
            raise ApiError('Gate Ticker 格式异常')
        result = {}
        for row in rows:
            pair = row.get('currency_pair') if market == 'spot' else row.get('contract')
            price = float(row.get('last') or 0)
            if pair and price > 0:
                volume = float(row.get('quote_volume') or 0) if market == 'spot' else float(row.get('volume_24h_quote') or 0)
                result[pair] = Ticker(pair, price, float(row.get('change_percentage') or 0), volume)
        return result

    async def get_klines(self, symbol: Symbol, period: str, limit: int) -> list[Candle]:
        rows: list = []
        if limit <= 1000:
            params = {'currency_pair': symbol.pair, 'interval': period, 'limit': limit} if symbol.market == 'spot' else {'contract': symbol.pair, 'interval': period, 'limit': limit}
            rows = await self.http.get(self.base_url + self.path(symbol.market) + '/candlesticks', params, ttl=40)
        else:
            # Gate 的 from/to 与 limit 不能同时使用，按不超过 900 根分段请求。
            from datetime import timedelta
            from app.services.periods import SECONDS
            end = int(datetime.now(timezone.utc).timestamp())
            while len(rows) < limit:
                start = end - min(900, limit-len(rows)) * SECONDS[period]
                params = {'currency_pair': symbol.pair, 'interval': period, 'from': start, 'to': end-1} if symbol.market == 'spot' else {'contract': symbol.pair, 'interval': period, 'from': start, 'to': end-1}
                page = await self.http.get(self.base_url + self.path(symbol.market) + '/candlesticks', params, ttl=40)
                if not isinstance(page, list) or not page:
                    break
                rows.extend(page)
                end = start
        if not isinstance(rows, list):
            raise ApiError('Gate K线格式异常')
        if symbol.market == 'spot':
            rows = list({str(row[0]): row for row in rows}.values())
        else:
            rows = list({str(row['t']): row for row in rows}.values())
        if symbol.market == 'spot':
            return sorted([Candle(datetime.fromtimestamp(int(r[0]), timezone.utc), float(r[5]), float(r[3]), float(r[4]), float(r[2]), float(r[6]), float(r[1])) for r in rows], key=lambda c: c.time)
        return sorted([Candle(datetime.fromtimestamp(int(r['t']), timezone.utc), float(r['o']), float(r['h']), float(r['l']), float(r['c']), float(r['v']), float(r['sum'])) for r in rows], key=lambda c: c.time)
