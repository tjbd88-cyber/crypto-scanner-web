import asyncio
from email.utils import formatdate

import httpx
import pytest

from app.exchanges.base import ApiError, HostPolicy, PublicHttp, RateLimitError


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def monotonic(self):
        return self.now

    def wall(self):
        return 1_800_000_000+self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def make_http(client, clock, **kwargs):
    return PublicHttp(client, clock=clock.monotonic, wall_clock=clock.wall, sleep=clock.sleep, **kwargs)


def test_200_cache_ttl_and_bounded_eviction():
    async def run():
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={'value': len(calls)})
        clock = Clock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http = make_http(client, clock, cache_entries=2, cache_bytes=100)
            first = await http.get('https://api.binance.com/klines', {'limit':3}, ttl=60)
            assert await http.get('https://api.binance.com/klines', {'limit':3}, ttl=60) == first
            await http.get('https://api.binance.com/klines', {'limit':106}, ttl=60)
            await http.get('https://api.binance.com/klines', {'limit':123}, ttl=60)
            assert len(calls) == 3 and len(http.cache) == 2
            assert http._cache_size <= 100
            clock.now += 61
            await http.get('https://api.binance.com/klines', {'limit':123}, ttl=60)
            assert len(calls) == 4
            assert not http._key_locks
    asyncio.run(run())


def test_429_respects_retry_after_and_reads_weight():
    async def run():
        clock, times = Clock(), []
        def handler(request):
            times.append(clock.now)
            return httpx.Response(429, headers={'Retry-After':'12','X-MBX-USED-WEIGHT-1M':'2400'}, json={'msg':'Too many requests'}) if len(times) == 1 else httpx.Response(200, json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http = make_http(client, clock)
            assert await http.get('https://fapi.binance.com/fapi/v1/klines') == []
            assert times[1]-times[0] >= 12
            state = http.snapshot('fapi.binance.com')
            assert state['http_429'] == 1 and state['used_weight'] == 2400
            assert state['status'] == 'available'
    asyncio.run(run())


def test_418_is_not_retried_and_other_hosts_are_not_delayed():
    async def run():
        clock, calls = Clock(), []
        def handler(request):
            calls.append(request.url.host)
            if request.url.host == 'fapi.binance.com':
                return httpx.Response(418, headers={'Retry-After':'3600'}, json={'code':-1003,'msg':'IP 203.0.113.10 banned until 1800003600000'})
            return httpx.Response(200, json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http = make_http(client, clock)
            with pytest.raises(RateLimitError) as caught:
                await http.get('https://fapi.binance.com/fapi/v1/klines')
            assert caught.value.status_code == 418 and caught.value.request_sent
            assert caught.value.details['retry_after'] == 3600
            assert '203.0.113.10' not in caught.value.details['message']
            with pytest.raises(RateLimitError) as blocked:
                await http.get('https://fapi.binance.com/fapi/v1/exchangeInfo')
            assert not blocked.value.request_sent
            for host in ('api.binance.com','www.okx.com','api.gateio.ws'):
                assert await http.get(f'https://{host}/market') == []
            assert calls.count('fapi.binance.com') == 1 and clock.waits == []
    asyncio.run(run())


@pytest.mark.parametrize('header,expected', [('invalid',900), ('120',120), ('Wed, 15 Jan 2027 08:00:00 GMT',None)])
def test_retry_after_malformed_seconds_and_http_date(header, expected):
    clock = Clock()
    http = make_http(None, clock)
    if expected is None:
        header = formatdate(clock.wall()+120, usegmt=True)
        expected = 120
    response = httpx.Response(418, headers={'Retry-After':header}, json={}, request=httpx.Request('GET','https://fapi.binance.com/x'))
    assert http._retry_after(response) == expected


def test_missing_retry_after_uses_binance_ban_expiry():
    clock = Clock()
    http = make_http(None, clock)
    response = httpx.Response(418, json={'msg':'IP banned until 1800000120000'}, request=httpx.Request('GET','https://fapi.binance.com/x'))
    assert http._retry_after(response) == 120


def test_long_429_cooldown_is_not_truncated_or_busy_waited():
    async def run():
        clock, calls = Clock(), []
        def handler(request):
            calls.append(request.url.host)
            return httpx.Response(429, headers={'Retry-After':'120'}, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http = make_http(client, clock)
            for _ in range(2):
                with pytest.raises(RateLimitError):
                    await http.get('https://fapi.binance.com/x')
            assert len(calls) == 1 and clock.waits == []
            assert http.snapshot('fapi.binance.com')['retry_after'] == 120
    asyncio.run(run())


def test_429_retries_are_finite():
    async def run():
        clock = Clock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(429,headers={'Retry-After':'1'},json={}))) as client:
            http = make_http(client, clock)
            with pytest.raises(RateLimitError):
                await http.get('https://api.binance.com/x')
            assert http.snapshot('api.binance.com')['requests'] == 3
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['500','timeout'])
def test_server_error_and_timeout_have_three_attempts(failure):
    async def run():
        clock, count = Clock(), 0
        def handler(request):
            nonlocal count
            count += 1
            if failure == 'timeout':
                raise httpx.ReadTimeout('test timeout',request=request)
            return httpx.Response(500,json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http = make_http(client, clock)
            with pytest.raises(ApiError):
                await http.get('https://api.binance.com/x')
            assert count == 3
            assert clock.waits == [1,2]
            assert http.snapshot('api.binance.com')['timeouts'] == (3 if failure == 'timeout' else 0)
    asyncio.run(run())


def test_500_can_recover_and_400_is_not_retried():
    async def run():
        clock, count = Clock(), 0
        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(500 if count == 1 else 200,json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await make_http(client,clock).get('https://api.binance.com/x') == []
            assert count == 2
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(400,json={}))) as client:
            http=make_http(client,clock)
            with pytest.raises(ApiError):
                await http.get('https://api.binance.com/x')
            assert http.snapshot('api.binance.com')['requests'] == 1
    asyncio.run(run())


def test_host_pacing_limits_fast_responses():
    async def run():
        clock, times = Clock(), []
        def handler(request):
            times.append(clock.now)
            return httpx.Response(200,json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http=make_http(client,clock)
            for i in range(5):
                await http.get('https://fapi.binance.com/x',{'i':i})
            assert all(b-a >= .5 for a,b in zip(times,times[1:]))
    asyncio.run(run())


def test_independent_host_concurrency_and_identical_request_deduplication():
    async def run():
        active, peaks, calls = {}, {}, 0
        async def handler(request):
            nonlocal calls
            host=request.url.host
            calls += 1
            active[host]=active.get(host,0)+1
            peaks[host]=max(peaks.get(host,0),active[host])
            await asyncio.sleep(.005)
            active[host]-=1
            return httpx.Response(200,json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http=PublicHttp(client,policies={'api.binance.com':HostPolicy(3,0),'fapi.binance.com':HostPolicy(2,0)})
            await asyncio.gather(*(http.get(f'https://{host}/x',{'i':i},ttl=60) for host in ('api.binance.com','fapi.binance.com') for i in range(8)))
            assert peaks == {'api.binance.com':3,'fapi.binance.com':2}
            before=calls
            await asyncio.gather(*(http.get('https://api.binance.com/duplicate',ttl=60) for _ in range(8)))
            assert calls-before == 1 and not http._key_locks
    asyncio.run(run())


def test_pending_requests_stop_after_first_418():
    async def run():
        clock=Clock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(418,headers={'Retry-After':'900'},json={}))) as client:
            http=make_http(client,clock)
            outcomes=await asyncio.gather(*(http.get('https://fapi.binance.com/x',{'i':i}) for i in range(20)),return_exceptions=True)
            assert all(isinstance(x,RateLimitError) for x in outcomes)
            assert http.snapshot('fapi.binance.com')['requests'] == 1
    asyncio.run(run())


def test_expired_418_does_not_turn_later_429_into_another_ban():
    async def run():
        clock,count=Clock(),0
        def handler(request):
            nonlocal count
            count+=1
            return httpx.Response(418 if count==1 else 429,headers={'Retry-After':'1'},json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http=make_http(client,clock)
            with pytest.raises(RateLimitError): await http.get('https://fapi.binance.com/x')
            clock.now+=2
            with pytest.raises(RateLimitError) as error: await http.get('https://fapi.binance.com/x')
            assert error.value.status_code==429 and count==4
    asyncio.run(run())


def test_okx_json_rate_limit_is_handled_like_429():
    async def run():
        clock,count=Clock(),0
        def handler(request):
            nonlocal count
            count+=1
            return httpx.Response(200,json={'code':'50011','msg':'Rate limit reached'}) if count==1 else httpx.Response(200,json={'code':'0','data':[]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http=make_http(client,clock)
            assert (await http.get('https://www.okx.com/api/v5/market/history-candles'))['code']=='0'
            assert count==2 and clock.waits==[5]
    asyncio.run(run())
