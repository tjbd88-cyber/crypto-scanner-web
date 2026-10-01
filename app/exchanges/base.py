from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

import httpx

from app.models import Candle, Symbol, Ticker

logger = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """可展示给用户的公开行情错误。"""


class PublicHttp:
    """公开 REST 客户端，带缓存、并发限制及 429 退避。"""

    def __init__(self, client: httpx.AsyncClient, concurrency: int = 5):
        self.client = client
        self.semaphore = asyncio.Semaphore(concurrency)
        self.cache: dict[str, tuple[float, object]] = {}
        self.slow_until = 0.0

    async def get(self, url: str, params: dict | None = None, ttl: int = 0) -> object:
        key = str((url, sorted((params or {}).items())))
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        async with self.semaphore:
            for attempt in range(3):
                wait = self.slow_until - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    response = await self.client.get(url, params=params, timeout=15)
                    if response.status_code in (418, 429):
                        delay = min(30, max(3, int(response.headers.get('Retry-After', '3'))) * (attempt + 1))
                        self.slow_until = time.monotonic() + delay
                        raise ApiError(f'HTTP {response.status_code}，已限速 {delay} 秒')
                    if response.status_code >= 500:
                        raise ApiError(f'HTTP {response.status_code}，交易所服务异常')
                    response.raise_for_status()
                    data = response.json()
                    if ttl:
                        self.cache[key] = (time.monotonic() + ttl, data)
                    return data
                except (httpx.HTTPError, ValueError, ApiError) as exc:
                    logger.warning('行情请求失败 %s %s: %s', url, params, exc)
                    if attempt == 2 or isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                        raise ApiError(f'{url}: {exc}') from exc
                    await asyncio.sleep(min(8, 2 ** attempt))
        raise ApiError('行情请求失败')


class BaseExchange(ABC):
    """交易所数据转换为统一的 Symbol、Ticker、Candle。"""

    name: str

    def __init__(self, http: PublicHttp):
        self.http = http

    @abstractmethod
    async def get_symbols(self, market: str) -> list[Symbol]: ...

    @abstractmethod
    async def get_tickers(self, market: str) -> dict[str, Ticker]: ...

    @abstractmethod
    async def get_klines(self, symbol: Symbol, period: str, limit: int) -> list[Candle]: ...

    async def get_24h_volume(self, symbol: Symbol) -> float:
        return (await self.get_tickers(symbol.market))[symbol.pair].volume_24h

    async def get_price(self, symbol: Symbol) -> float:
        return (await self.get_tickers(symbol.market))[symbol.pair].price
