import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
from app.exchanges.base import HostPolicy, PublicHttp
from app.exchanges.binance import BinanceExchange
from app.models import Candle, Symbol
from app.universe import previous_utc_day
from test_public_http import Clock, make_http
from test_strategy import sample


@pytest.fixture
def clean_main(monkeypatch):
    monkeypatch.setattr(main,'task',None)
    monkeypatch.setattr(main,'scan_runner',None)
    monkeypatch.setattr(main,'cancel',False)
    monkeypatch.setattr(main,'scan_next_start',0)
    monkeypatch.setattr(main,'save_task',lambda job:None)
    monkeypatch.setattr(main,'chart_cache',OrderedDict())


def markets_handler(calls, *, ban_futures=False):
    def handler(request):
        calls.append((request.url.host,request.url.path,dict(request.url.params)))
        host,path=request.url.host,request.url.path
        if path.endswith('exchangeInfo'):
            return httpx.Response(200,json={'symbols':[{'symbol':p+'USDT','baseAsset':p,'quoteAsset':'USDT','status':'TRADING','contractType':'PERPETUAL'} for p in ('BTC','ETH')]})
        if path.endswith('instruments'):
            swap=request.url.params.get('instType')=='SWAP'
            return httpx.Response(200,json={'code':'0','data':[{'instId':p+'-USDT'+('-SWAP' if swap else ''),'baseCcy':p,'ctValCcy':p,'quoteCcy':'USDT','state':'live'} for p in ('BTC','ETH')]})
        if path.endswith('currency_pairs'):
            return httpx.Response(200,json=[{'id':p+'_USDT','base':p,'quote':'USDT','trade_status':'tradable'} for p in ('BTC','ETH')])
        if path.endswith('contracts'):
            return httpx.Response(200,json=[{'name':p+'_USDT','in_delisting':False} for p in ('BTC','ETH')])
        if host=='fapi.binance.com' and ban_futures:
            return httpx.Response(418,headers={'Retry-After':'3600'},json={'code':-1003,'msg':'IP 203.0.113.10 banned'})
        period=request.url.params.get('interval') or request.url.params.get('bar')
        candles=[Candle(previous_utc_day(),1,2,.5,1,100,20_000_000)] if period in ('1d','1Dutc') else sample()
        if host in ('api.binance.com','fapi.binance.com'):
            rows=[[int(c.time.timestamp()*1000),str(c.open),str(c.high),str(c.low),str(c.close),str(c.volume),0,str(c.quote_volume)] for c in candles]
            return httpx.Response(200,json=rows)
        if host=='www.okx.com':
            rows=[[str(int(c.time.timestamp()*1000)),str(c.open),str(c.high),str(c.low),str(c.close),str(c.volume),'0',str(c.quote_volume),'1'] for c in reversed(candles)]
            return httpx.Response(200,json={'code':'0','data':rows})
        if '/spot/' in path:
            rows=[[str(int(c.time.timestamp())),str(c.quote_volume),str(c.close),str(c.high),str(c.low),str(c.open),str(c.volume)] for c in candles]
        else:
            rows=[{'t':int(c.time.timestamp()),'o':str(c.open),'h':str(c.high),'l':str(c.low),'c':str(c.close),'v':str(c.volume),'sum':str(c.quote_volume)} for c in candles]
        return httpx.Response(200,json=rows)
    return handler


def test_binance_spot_fields_symbol_filter_and_market_paths():
    async def run():
        calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(markets_handler(calls))) as client:
            exchange=BinanceExchange(make_http(client,Clock()))
            symbols=await exchange.get_symbols('spot')
            assert symbols[0]==Symbol('binance','BTCUSDT','BTC','spot')
            candles=await exchange.get_klines(symbols[0],'1d',3)
            assert candles[0].quote_volume==20_000_000
            assert (candles[0].open,candles[0].high,candles[0].low,candles[0].close)==(1,2,.5,1)
            assert all(x[0]=='api.binance.com' for x in calls)
            await exchange.get_symbols('perpetual')
            assert calls[-1][:2]==('fapi.binance.com','/fapi/v1/exchangeInfo')
    asyncio.run(run())


def test_futures_418_skips_market_and_other_five_markets_finish(clean_main,monkeypatch):
    async def run():
        calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(markets_handler(calls,ban_futures=True))) as client:
            http=make_http(client,Clock())
            monkeypatch.setattr(main.app.state,'http',http,raising=False)
            await main.start(main.ScanOptions(exchanges=['binance','okx','gate'],markets=['spot','perpetual']))
            await main.scan_runner
            job=main.task
            assert job['status']=='completed_with_warnings'
            futures=job['market_stats']['binance:perpetual']
            assert futures['status']=='rate_limited' and futures['upstream_error']['status_code']==418
            assert futures['http']['http_418']==1
            assert sum(x[0]=='fapi.binance.com' and x[1].endswith('klines') for x in calls)==1
            assert all(m['status']=='success' for key,m in job['market_stats'].items() if key!='binance:perpetual')
            assert job['stats']==dict(total_symbols=12,processed_symbols=10,matched_symbols=10,failed_symbols=1,skipped_symbols=1)
            assert all('203.0.113.10' not in w for w in job['warnings'])
            assert not any('ticker' in x[1] for x in calls)
            # exchangeInfo/instruments/contracts is fetched once per market, not per symbol.
            assert sum(x[1].endswith('exchangeInfo') for x in calls)==2
    asyncio.run(run())


def test_controlled_symbol_limit_keeps_full_scan_as_default(clean_main,monkeypatch):
    async def run():
        calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(markets_handler(calls))) as client:
            monkeypatch.setattr(main.app.state,'http',make_http(client,Clock()),raising=False)
            await main.start(main.ScanOptions(max_symbols=1))
            await main.scan_runner
            assert main.task['stats']['total_symbols']==1
            assert main.task['market_stats']['binance:spot']['available_symbols']==2
            assert not any(x[2].get('symbol')=='ETHUSDT' for x in calls)
            assert main.ScanOptions().max_symbols is None
    asyncio.run(run())


def test_duplicate_start_and_finish_cooldown_are_server_side(clean_main,monkeypatch):
    async def run():
        calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(markets_handler(calls))) as client:
            monkeypatch.setattr(main.app.state,'http',make_http(client,Clock()),raising=False)
            first=await main.start(main.ScanOptions())
            with pytest.raises(HTTPException) as duplicate:
                await main.start(main.ScanOptions())
            assert duplicate.value.status_code==409
            assert main.task['id']==first['id']
            await main.scan_runner
            with pytest.raises(HTTPException) as cooldown:
                await main.start(main.ScanOptions())
            assert cooldown.value.status_code==429
            assert int(cooldown.value.headers['Retry-After'])>0
    asyncio.run(run())


def test_immediate_stop_before_worker_starts_cannot_leave_running_task(clean_main):
    async def run():
        await main.start(main.ScanOptions())
        await main.stop()
        assert main.task['status']=='stopped'
        assert main.scan_runner.done()
        assert main.get_task()['scan_cooldown_seconds']>0
    asyncio.run(run())


def test_stop_cancels_waiting_request_and_counts_remaining_symbols(clean_main,monkeypatch):
    async def run():
        started=asyncio.Event()
        normal=markets_handler([])
        async def handler(request):
            if request.url.path.endswith('klines'):
                started.set()
                await asyncio.Event().wait()
            return normal(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(main.app.state,'http',make_http(client,Clock()),raising=False)
            await main.start(main.ScanOptions())
            await asyncio.wait_for(started.wait(),1)
            await asyncio.wait_for(main.stop(),1)
            assert main.task['status']=='stopped'
            assert main.task['stats']['skipped_symbols']==2
    asyncio.run(run())


def test_health_and_status_do_not_request_exchanges(clean_main,monkeypatch):
    async def forbidden(*args,**kwargs):
        raise AssertionError('Health/status must not call an exchange')
    monkeypatch.setattr(httpx.AsyncClient,'get',forbidden)
    with TestClient(main.app) as client:
        for path in ('/','/health','/api/health','/api/task','/api/exchanges/status'):
            assert client.get(path).status_code==200


def test_chart_reuses_service_ban_without_retrying(clean_main):
    calls=[]
    with TestClient(main.app) as client:
        upstream=httpx.AsyncClient(transport=httpx.MockTransport(markets_handler(calls,ban_futures=True)))
        main.app.state.http=make_http(upstream,Clock())
        for pair in ('BTCUSDT','ETHUSDT'):
            r=client.get('/api/chart',params={'exchange':'binance','market':'perpetual','pair':pair,'period':'4h'})
            assert r.status_code==503 and 'Binance 永续' in r.json()['detail']
            assert r.headers['Retry-After']=='3600'
        spot=client.get('/api/chart',params={'exchange':'binance','market':'spot','pair':'BTCUSDT','period':'4h'})
        assert spot.status_code==200
        assert sum(x[0]=='fapi.binance.com' for x in calls)==1
    asyncio.run(upstream.aclose())


def test_strategy_only_receives_closed_candles(clean_main,monkeypatch):
    async def run():
        original=main.EXCHANGES['binance']
        class Exchange(original):
            async def get_klines(self,symbol,period,limit):
                if period=='1d':
                    return [Candle(previous_utc_day(),1,1,1,1,1,20_000_000)]
                bars=sample()
                now=datetime.now(timezone.utc)
                current=now.replace(hour=now.hour//4*4,minute=0,second=0,microsecond=0)
                return bars+[Candle(current,1,100,1,100,1,100)]
        received=[]
        original_analyze=main.analyze
        def analyze(bars):
            received.extend(bars)
            assert all(c.time+timedelta(hours=4)<=datetime.now(timezone.utc) for c in bars)
            return original_analyze(bars)
        monkeypatch.setitem(main.EXCHANGES,'binance',Exchange)
        monkeypatch.setattr(main,'analyze',analyze)
        async with httpx.AsyncClient(transport=httpx.MockTransport(markets_handler([]))) as client:
            monkeypatch.setattr(main.app.state,'http',make_http(client,Clock()),raising=False)
            await main.start(main.ScanOptions(max_symbols=1))
            await main.scan_runner
            assert main.task['status']=='completed' and received
    asyncio.run(run())


def test_chart_cache_is_bounded_and_purges_expired_entries(clean_main):
    main.chart_cache[('binance','spot','OLD','4h')]=(0,[],'scan')
    for i in range(main.CHART_CACHE_LIMIT+5):
        main.cache_chart(('binance','spot',str(i),'4h'),sample(),'scan')
    assert len(main.chart_cache)==main.CHART_CACHE_LIMIT
    assert ('binance','spot','OLD','4h') not in main.chart_cache


def test_duplicate_markets_and_exchanges_are_rejected(clean_main):
    with TestClient(main.app) as client:
        for values in ({'exchanges':['binance','binance']},{'markets':['spot','spot']}):
            assert client.post('/api/task',json=values).status_code==422


def test_chart_after_scan_reuses_same_candles_without_network(clean_main,monkeypatch):
    async def run():
        calls=[]
        async with httpx.AsyncClient(transport=httpx.MockTransport(markets_handler(calls))) as client:
            monkeypatch.setattr(main.app.state,'http',make_http(client,Clock()),raising=False)
            await main.start(main.ScanOptions(max_symbols=1))
            await main.scan_runner
            count=len(calls)
            response=await main.chart('binance','spot','BTCUSDT','4h',20,2,5,21,4,.02,.2,13,3)
            assert response['source']=='scan' and len(calls)==count
    asyncio.run(run())


def test_429_market_cooldown_does_not_block_other_markets(clean_main,monkeypatch):
    async def run():
        calls=[]
        normal=markets_handler(calls)
        def handler(request):
            if request.url.host=='fapi.binance.com':
                calls.append((request.url.host,request.url.path,dict(request.url.params)))
                return httpx.Response(429,headers={'Retry-After':'120'},json={'msg':'Too many requests'})
            return normal(request)
        clock=Clock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(main.app.state,'http',make_http(client,clock),raising=False)
            await main.start(main.ScanOptions(exchanges=['binance','okx','gate'],markets=['perpetual'],max_symbols=1))
            await main.scan_runner
            assert main.task['status']=='completed_with_warnings'
            assert main.task['market_stats']['binance:perpetual']['status']=='rate_limited'
            assert main.task['market_stats']['okx:perpetual']['status']=='success'
            assert main.task['market_stats']['gate:perpetual']['status']=='success'
            assert sum(x[0]=='fapi.binance.com' for x in calls)==1
            assert 120 not in clock.waits
    asyncio.run(run())


def test_single_symbol_error_counts_failure_without_failing_entire_scan(clean_main,monkeypatch):
    async def run():
        normal=markets_handler([])
        def handler(request):
            if request.url.params.get('symbol')=='ETHUSDT':
                return httpx.Response(400,json={'msg':'invalid symbol'})
            return normal(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(main.app.state,'http',make_http(client,Clock()),raising=False)
            await main.start(main.ScanOptions())
            await main.scan_runner
            assert main.task['status']=='completed_with_warnings'
            assert main.task['stats']['processed_symbols']==1
            assert main.task['stats']['failed_symbols']==1
            assert main.task['stats']['total_symbols']==2
    asyncio.run(run())
