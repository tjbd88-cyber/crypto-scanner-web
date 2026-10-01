from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from abc import ABC, abstractmethod

import httpx

from app.models import Candle, Symbol, Ticker

logger = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """可展示给用户的公开行情错误。"""


class RateLimitError(ApiError):
    def __init__(self, host: str, details: dict, *, request_sent: bool = False):
        self.host = host
        self.details = details
        self.status_code = details['status_code']
        self.request_sent = request_sent
        super().__init__('上游 IP 暂时被封禁，请稍后重试' if self.status_code == 418 else '上游暂时限流，请稍后重试')


@dataclass(frozen=True)
class HostPolicy:
    concurrency: int = 3
    interval: float = .35


HOST_POLICIES = {
    'api.binance.com': HostPolicy(3, .35),
    'fapi.binance.com': HostPolicy(2, .5),
    'www.okx.com': HostPolicy(3, .35),
    'api.gateio.ws': HostPolicy(3, .3),
}


@dataclass
class HostState:
    policy: HostPolicy
    semaphore: asyncio.Semaphore = field(init=False)
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_request_at: float = 0
    cooldown_until: float = 0
    last_429: str | None = None
    last_418: str | None = None
    used_weight: int | None = None
    blocked: dict | None = None
    requests: int = 0
    http_429: int = 0
    http_418: int = 0
    timeouts: int = 0
    cache_hits: int = 0

    def __post_init__(self):
        self.semaphore = asyncio.Semaphore(self.policy.concurrency)


def safe_upstream_message(response: httpx.Response) -> str:
    """Only retain the public error message; never store HTML or request credentials."""
    try:
        data = response.json()
        message = str(data.get('msg') or data.get('message') or '') if isinstance(data, dict) else ''
    except ValueError:
        message = ''
    message = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[IP]', message)
    message = re.sub(r'\b[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,}\b', '[IP]', message)
    return message[:240]


class PublicHttp:
    """One service-wide client: isolated host limits, bounded cache and finite retries."""

    def __init__(self, client: httpx.AsyncClient, concurrency: int = 3, *,
                 policies: dict[str, HostPolicy] | None = None,
                 clock=time.monotonic, wall_clock=time.time, sleep=asyncio.sleep,
                 max_wait: float = 30, cache_entries: int = 128, cache_bytes: int = 8*1024*1024):
        self.client = client
        self.concurrency = concurrency
        self.policies = HOST_POLICIES if policies is None else policies
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.max_wait = max_wait
        self.hosts: dict[str, HostState] = {}
        self.cache: OrderedDict[str, tuple[float, object, int]] = OrderedDict()
        self.cache_entries, self.cache_bytes = cache_entries, cache_bytes
        self._cache_size = 0
        self._key_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.on_block = None

    def restore_cooldown(self, host: str, details: dict) -> None:
        remaining = datetime.fromisoformat(details['cooldown_until']).timestamp()-self.wall_clock()
        if remaining > 0:
            state = self.state(host)
            state.cooldown_until = self.clock()+remaining
            state.blocked = details

    def state(self, host: str) -> HostState:
        if host not in self.hosts:
            policy = self.policies.get(host, HostPolicy(self.concurrency))
            self.hosts[host] = HostState(HostPolicy(min(policy.concurrency, self.concurrency), policy.interval))
        return self.hosts[host]

    def snapshot(self, host: str) -> dict:
        state = self.state(host)
        remaining = max(0, state.cooldown_until-self.clock())
        return {'host': host, 'status': 'rate_limited' if remaining else 'available',
                'cooldown_until': (state.blocked or {}).get('cooldown_until') if remaining else None,
                'retry_after': math.ceil(remaining), 'last_429': state.last_429, 'last_418': state.last_418,
                'used_weight': state.used_weight, 'requests': state.requests, 'http_429': state.http_429,
                'http_418': state.http_418, 'timeouts': state.timeouts, 'cache_hits': state.cache_hits,
                'last_error': state.blocked}

    def check_available(self, host: str) -> None:
        state = self.state(host)
        remaining = state.cooldown_until-self.clock()
        if remaining > 0 and state.blocked and (state.blocked['status_code'] == 418 or remaining > self.max_wait):
            raise RateLimitError(host, {**state.blocked, 'retry_after': math.ceil(remaining)})

    def _retry_after(self, response: httpx.Response) -> float:
        raw = response.headers.get('Retry-After', '')
        try:
            delay = float(raw)
            if math.isfinite(delay) and delay >= 0:
                return max(1, delay)
        except ValueError:
            pass
        try:
            delay = parsedate_to_datetime(raw).timestamp()-self.wall_clock()
            return max(1, delay)
        except (ValueError, TypeError, OverflowError):
            pass
        # Binance sometimes supplies a ban expiry in its JSON message without a header.
        expiry = re.search(r'banned until\s+(\d{10,13})', safe_upstream_message(response), re.I)
        if response.status_code == 418 and expiry:
            timestamp = int(expiry[1])
            if timestamp > 10**11:
                timestamp /= 1000
            return max(1, timestamp-self.wall_clock())
        return 900 if response.status_code == 418 else 5

    def _block(self, host: str, state: HostState, response: httpx.Response) -> RateLimitError:
        delay = self._retry_after(response)
        active_ban = state.blocked and state.blocked['status_code'] == 418 and state.cooldown_until > self.clock()
        state.cooldown_until = max(state.cooldown_until, self.clock()+delay)
        utc_now = datetime.fromtimestamp(self.wall_clock(), timezone.utc).isoformat()
        if response.status_code == 418:
            state.last_418 = utc_now
            state.http_418 += 1
        else:
            state.last_429 = utc_now
            state.http_429 += 1
        # An already observed ban must never be shortened/downgraded by a late 429.
        status = 418 if active_ban else response.status_code
        remaining = max(0, state.cooldown_until-self.clock())
        details = {'status_code': status, 'retry_after': math.ceil(remaining),
                   'retry_after_header': response.headers.get('Retry-After'),
                   'cooldown_until': datetime.fromtimestamp(self.wall_clock()+remaining, timezone.utc).isoformat(),
                   'used_weight': state.used_weight, 'endpoint': str(response.request.url.copy_with(query=None)),
                   'message': safe_upstream_message(response)}
        state.blocked = details
        if self.on_block:
            self.on_block(host, details)
        logger.warning('Public market throttled host=%s status=%s retry_after=%s used_weight=%s message=%s',
                       host, response.status_code, delay, state.used_weight, details['message'])
        return RateLimitError(host, details, request_sent=True)

    async def _pace(self, host: str, state: HostState) -> None:
        # Check again after every wait: another in-flight request may have received 418.
        while True:
            self.check_available(host)
            async with state.gate:
                wait = max(state.next_request_at, state.cooldown_until)-self.clock()
                if wait <= 0:
                    self.check_available(host)
                    state.next_request_at = self.clock()+state.policy.interval
                    return
            await self.sleep(wait)

    def _cached(self, key: str, state: HostState) -> object | None:
        item = self.cache.get(key)
        if item and item[0] > self.clock():
            self.cache.move_to_end(key)
            state.cache_hits += 1
            return item[1]
        if item:
            self._cache_size -= self.cache.pop(key)[2]
        return None

    def _store(self, key: str, data: object, ttl: int, size: int):
        for old_key, item in list(self.cache.items()):
            if item[0] <= self.clock():
                self._cache_size -= self.cache.pop(old_key)[2]
        if size > self.cache_bytes:
            return
        if key in self.cache:
            self._cache_size -= self.cache.pop(key)[2]
        self.cache[key] = (self.clock()+ttl, data, size)
        self._cache_size += size
        while len(self.cache) > self.cache_entries or self._cache_size > self.cache_bytes:
            self._cache_size -= self.cache.popitem(last=False)[1][2]

    async def get(self, url: str, params: dict | None = None, ttl: int = 0) -> object:
        key = str((url, sorted((params or {}).items())))
        host = httpx.URL(url).host
        state = self.state(host)
        # Identical simultaneous requests share the first completed cached result.
        lock, users = self._key_locks.get(key, (asyncio.Lock(), 0))
        self._key_locks[key] = (lock, users+1)
        try:
            async with lock:
                cached = self._cached(key, state)
                if cached is not None:
                    return cached
                self.check_available(host)
                return await self._request(url, params, ttl, key, host, state)
        finally:
            lock, users = self._key_locks[key]
            if users == 1:
                del self._key_locks[key]
            else:
                self._key_locks[key] = (lock, users-1)

    async def _request(self, url, params, ttl, key, host, state):
        async with state.semaphore:
            for attempt in range(3):
                await self._pace(host, state)
                try:
                    state.requests += 1
                    response = await self.client.get(url, params=params, timeout=15)
                    try:
                        state.used_weight = int(response.headers['X-MBX-USED-WEIGHT-1M'])
                    except (KeyError, ValueError):
                        pass
                    if response.status_code == 418:
                        raise self._block(host, state, response)
                    # OKX can return its rate-limit code in an HTTP 200 JSON response.
                    if host == 'www.okx.com' and response.status_code == 200:
                        data = response.json()
                        if isinstance(data, dict) and data.get('code') == '50011':
                            response = httpx.Response(429, headers=response.headers, json=data, request=response.request)
                    if response.status_code == 429:
                        error = self._block(host, state, response)
                        if attempt == 2 or error.status_code == 418 or error.details['retry_after'] > self.max_wait:
                            raise error
                        continue
                    if response.status_code >= 500:
                        response.raise_for_status()
                    response.raise_for_status()
                    data = response.json()
                    if ttl:
                        self._store(key, data, ttl, len(response.content))
                    return data
                except RateLimitError:
                    raise
                except (httpx.HTTPError, ValueError) as exc:
                    if isinstance(exc, httpx.TimeoutException):
                        state.timeouts += 1
                    logger.warning('Public market request failed host=%s path=%s type=%s attempt=%s',
                                   host, httpx.URL(url).path, type(exc).__name__, attempt+1)
                    if attempt == 2 or isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                        message = f'API 请求失败（HTTP {exc.response.status_code}）' if isinstance(exc, httpx.HTTPStatusError) else 'API 请求超时或网络不可用' if isinstance(exc, httpx.TimeoutException) else 'API 网络或响应格式异常'
                        raise ApiError(message) from exc
                    await self.sleep(2 ** attempt)
        raise ApiError('行情请求失败')


class BaseExchange(ABC):
    """交易所数据转换为统一的 Symbol、Ticker、Candle。"""

    name: str

    def __init__(self, http: PublicHttp):
        self.http = http

    def host(self, market: str) -> str:
        return httpx.URL(self.base_url).host

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
