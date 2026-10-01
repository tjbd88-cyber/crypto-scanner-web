import asyncio
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.automatic import AutoScanService, AutoScanScheduler, stream_key, signal_id
from app.models import Candle, Symbol
from app.notifications import WebhookNotificationService
from app.repository import SQLiteSignalRepository
from app.services.periods import bucket_start
from app.strategy import analyze
from app.universe import eligible_symbols, previous_utc_day
from test_strategy import sample
from test_scan_controls import markets_handler
from test_public_http import Clock, make_http

NOW = datetime(2026,10,1,10,tzinfo=timezone.utc)
SYMBOL = Symbol('binance','BTCUSDT','BTC','spot')
KEY = stream_key('binance','spot','BTCUSDT','4h')


def bars(now=NOW):
    source = sample()
    last = bucket_start(now,'4h')-timedelta(hours=4)
    return [replace(c,time=last-timedelta(hours=4*(len(source)-1-i))) for i,c in enumerate(source)]


def row(candles):
    return {'exchange':'binance','market':'spot','pair':'BTCUSDT','period':'4h',
            'previous_day_turnover':20_000_000,**analyze(candles).to_dict()}


class Notifications:
    def __init__(self): self.sent=[]
    async def send_signal(self,signal): self.sent.append(signal); return True


class Adapter:
    def __init__(self,candles): self.raw=candles; self.calls=[]
    async def get_klines(self,symbol,period,limit):
        self.calls.append((period,limit))
        return self.raw[-limit:]


@pytest.fixture
def store(tmp_path):
    repo=SQLiteSignalRepository(tmp_path/'scanner.db')
    yield repo
    repo.close()


@pytest.fixture
def isolated(monkeypatch,tmp_path):
    monkeypatch.setenv('SCANNER_DB_PATH',str(tmp_path/'scanner.db'))
    monkeypatch.setenv('AUTO_SCAN_SCHEDULER_MODE','internal')
    monkeypatch.delenv('RENDER',raising=False)
    monkeypatch.delenv('AUTO_SCAN_TOKEN',raising=False)
    monkeypatch.delenv('SIGNAL_WEBHOOK_URL',raising=False)
    monkeypatch.setattr(main,'task',None)
    monkeypatch.setattr(main,'scan_runner',None)
    monkeypatch.setattr(main,'cancel',False)
    monkeypatch.setattr(main,'scan_next_start',0)
    monkeypatch.setattr(main,'save_task',lambda job:None)
    monkeypatch.setattr(main,'chart_cache',OrderedDict())


def test_scheduler_lifecycle_single_task_and_shutdown(store):
    async def run():
        service=AutoScanService(store,Notifications())
        scheduler=AutoScanScheduler(service,lambda:None)
        scheduler.start(); first=scheduler.runner; scheduler.start()
        assert scheduler.runner is first
        await asyncio.sleep(0)
        await scheduler.close()
        assert first.done() and scheduler.next_run is None
    asyncio.run(run())


def test_scheduler_wakes_and_shows_next_hour_when_enabled(store):
    async def run():
        service=AutoScanService(store,Notifications())
        scheduler=AutoScanScheduler(service,lambda:None,clock=lambda:NOW)
        scheduler.start(); await asyncio.sleep(0)
        assert scheduler.next_run is None
        service.configure(enabled=True);scheduler.changed()
        for _ in range(5): await asyncio.sleep(0)
        assert scheduler.next_run == NOW.replace(hour=11)
        await scheduler.close()
    asyncio.run(run())


def test_external_mode_does_not_start_internal_task(store):
    scheduler=AutoScanScheduler(AutoScanService(store,Notifications()),lambda:None,mode='external')
    scheduler.start()
    assert scheduler.runner is None


def test_same_closed_candle_skips_http_and_full_analysis(store):
    async def run():
        service=AutoScanService(store,Notifications());adapter=Adapter(bars())
        key,prev,candles=await service.candles(adapter,SYMBOL,'4h',NOW)
        await service.record(key,prev,candles,row(candles),NOW)
        calls=len(adapter.calls)
        _,_,candles=await service.candles(adapter,SYMBOL,'4h',NOW+timedelta(hours=1))
        assert candles is None and len(adapter.calls)==calls
    asyncio.run(run())


def test_new_closed_candle_uses_increment_and_only_closed_bars(store):
    async def run():
        service=AutoScanService(store,Notifications());adapter=Adapter(bars())
        key,prev,candles=await service.candles(adapter,SYMBOL,'4h',NOW)
        await service.record(key,prev,candles,row(candles),NOW)
        old=candles[-1]
        new=replace(old,time=old.time+timedelta(hours=4))
        forming=replace(old,time=new.time+timedelta(hours=4),close=9999)
        adapter.raw+= [new,forming]
        _,_,result=await service.candles(adapter,SYMBOL,'4h',NOW+timedelta(hours=4))
        assert adapter.calls[-1] == ('4h',3)
        assert result[-1]==new and forming not in result
    asyncio.run(run())


def test_exchange_delayed_candle_is_not_reanalyzed(store):
    async def run():
        service=AutoScanService(store,Notifications());adapter=Adapter(bars())
        key,prev,candles=await service.candles(adapter,SYMBOL,'4h',NOW)
        await service.record(key,prev,candles,row(candles),NOW)
        _,_,candles=await service.candles(adapter,SYMBOL,'4h',NOW+timedelta(hours=4))
        assert candles is None and adapter.calls[-1]==('4h',3)
    asyncio.run(run())


def test_unique_signal_id_normalizes_timezone():
    value={**row(bars()),'signal_type':'double_flip'}
    other={**value,'candle_time':datetime.fromisoformat(value['candle_time']).astimezone(timezone(timedelta(hours=8))).isoformat()}
    assert signal_id(value)==signal_id(other)
    assert ':double_flip:' in signal_id(value) and signal_id(value).endswith('Z')


def test_baseline_is_saved_silently_and_duplicate_never_notified(store):
    async def run():
        notifier=Notifications();service=AutoScanService(store,notifier);candles=bars()
        first=await service.record(KEY,None,candles,row(candles),NOW)
        second=await service.record(KEY,store.checkpoint(KEY),candles,row(candles),NOW)
        assert first['baseline_signals']==1 and second['existing_signals']==1
        assert store.signals()['total']==1 and store.signals()['items'][0]['notified']
        assert not notifier.sent
    asyncio.run(run())


def test_new_signal_saved_and_notified_once_across_restart(tmp_path):
    async def run():
        path=tmp_path/'scanner.db';repo=SQLiteSignalRepository(path);notifier=Notifications()
        service=AutoScanService(repo,notifier);candles=bars()
        await service.record(KEY,None,candles,row(candles),NOW)
        previous=repo.checkpoint(KEY);new_candles=bars(NOW+timedelta(hours=4))
        result=await service.record(KEY,previous,new_candles,row(new_candles),NOW+timedelta(hours=4))
        assert result['new_signals']==1 and len(notifier.sent)==1
        repo.close();repo=SQLiteSignalRepository(path);service=AutoScanService(repo,notifier)
        result=await service.record(KEY,repo.checkpoint(KEY),new_candles,row(new_candles),NOW+timedelta(hours=5))
        assert result['existing_signals']==1 and len(notifier.sent)==1 and repo.signals()['total']==2
        assert repo.checkpoint(KEY)['last_closed_candle_time']==new_candles[-1].time.isoformat()
        repo.close()
    asyncio.run(run())


def test_new_symbol_in_extended_scope_is_silent_baseline(store):
    async def run():
        notifier=Notifications();service=AutoScanService(store,notifier)
        candles=bars();await service.record(KEY,None,candles,row(candles),NOW)
        value={**row(candles),'pair':'ETHUSDT'};key=stream_key('binance','spot','ETHUSDT','4h')
        result=await service.record(key,None,candles,value,NOW)
        assert result['baseline_signals']==1 and not notifier.sent
    asyncio.run(run())


def test_later_unmatched_bar_invalidates_prior_signal(store):
    async def run():
        service=AutoScanService(store,Notifications());candles=bars()
        await service.record(KEY,None,candles,row(candles),NOW)
        await service.record(KEY,store.checkpoint(KEY),bars(NOW+timedelta(hours=4)),None,NOW+timedelta(hours=4))
        assert store.signals()['items'][0]['status']=='invalid'
        assert store.signals(stage='失效')['total']==1
    asyncio.run(run())


def test_repository_filters_pagination_and_configuration_survive_restart(tmp_path):
    async def run():
        path=tmp_path/'scanner.db';repo=SQLiteSignalRepository(path)
        service=AutoScanService(repo,Notifications());service.configure(enabled=True,markets=['spot'])
        for i in range(3):
            candles=bars(NOW+timedelta(hours=i*4))
            await service.record(KEY,None,candles,row(candles),NOW+timedelta(hours=i*4))
        repo.save_turnover('binance:spot:BTCUSDT','2026-09-30',0)
        repo.close();repo=SQLiteSignalRepository(path)
        assert AutoScanService(repo,Notifications()).config()['enabled']
        assert repo.turnover('binance:spot:BTCUSDT','2026-09-30')==0
        assert repo.signals(limit=1,offset=1,pair='btc',exchange='binance',date_from='2026-10-01')['total']==3
        assert len(repo.signals(limit=1,offset=1)['items'])==1
        assert repo.signals(exchange='okx')['total']==0
        repo.close()
    asyncio.run(run())


def test_daily_volume_cache_avoids_network_in_same_utc_day(store):
    class Daily:
        def __init__(self):self.calls=0
        async def get_symbols(self,market):return [SYMBOL]
        async def get_klines(self,symbol,period,limit):
            self.calls+=1;return [Candle(previous_utc_day(NOW),1,2,.5,1,10,20_000_000)]
    async def run():
        adapter=Daily()
        _,choices,_=await eligible_symbols(adapter,'spot',10_000_000,now=NOW,repository=store)
        _,again,_=await eligible_symbols(adapter,'spot',10_000_000,now=NOW+timedelta(hours=1),repository=store)
        assert choices==again and adapter.calls==1
    asyncio.run(run())


def test_webhook_failure_is_bounded_and_does_not_abort_scan(store,monkeypatch,caplog):
    async def run():
        calls=[]
        def handler(request):calls.append(request);return httpx.Response(500)
        async def no_sleep(_):pass
        monkeypatch.setattr('app.notifications.asyncio.sleep',no_sleep)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service=AutoScanService(store,WebhookNotificationService(client,'https://example.org/private-token'))
            candles=bars();await service.record(KEY,None,candles,row(candles),NOW)
            candles=bars(NOW+timedelta(hours=4))
            result=await service.record(KEY,store.checkpoint(KEY),candles,row(candles),NOW+timedelta(hours=4))
            assert result['notification_failed']==1 and len(calls)==2
            assert calls[0].headers['Idempotency-Key']==calls[1].headers['Idempotency-Key']
            assert store.checkpoint(KEY)['last_closed_candle_time']==candles[-1].time.isoformat()
            assert 'private-token' not in caplog.text
    asyncio.run(run())


def test_enable_disable_and_fixed_hourly_4h_options(isolated):
    with TestClient(main.app) as client:
        assert client.get('/api/auto-scan/status').json()['enabled'] is False
        assert client.post('/api/auto-scan/enable',json={'markets':['spot']}).json()['enabled'] is True
        assert client.post('/api/auto-scan/enable',json={'interval_seconds':60}).status_code==422
        assert client.post('/api/auto-scan/enable',json={'periods':['1h']}).status_code==422
        assert client.post('/api/auto-scan/disable').json()['enabled'] is False
        assert client.get('/history').status_code==200
        assert client.get('/api/signals?limit=101').status_code==422


def test_internal_trigger_requires_secret_and_exact_bearer(isolated,monkeypatch):
    with TestClient(main.app) as client:
        assert client.post('/api/internal/auto-scan').status_code==503
        monkeypatch.setenv('AUTO_SCAN_TOKEN','test-secret-not-production')
        assert client.post('/api/internal/auto-scan').status_code==401
        assert client.post('/api/internal/auto-scan',headers={'Authorization':'Bearer wrong'}).status_code==401
        assert client.post('/api/internal/auto-scan',headers={'Authorization':'test-secret-not-production'}).status_code==401
        assert client.post('/api/internal/auto-scan',headers={'Authorization':'Bearer test-secret-not-production'}).json()['reason']=='disabled'
        assert client.post('/api/auto-scan/enable',json={}).status_code==401


def test_manual_running_skips_automatic_without_network(isolated,monkeypatch):
    with TestClient(main.app) as client:
        client.post('/api/auto-scan/enable',json={})
        monkeypatch.setattr(main,'task',{'status':'running','type':'manual'})
        result=client.post('/api/internal/auto-scan')
        # Internal endpoint remains protected even for local development.
        assert result.status_code==503
        monkeypatch.setenv('AUTO_SCAN_TOKEN','test-only')
        result=client.post('/api/internal/auto-scan',headers={'Authorization':'Bearer test-only'}).json()
        assert result['reason']=='skipped_due_to_manual_scan'
        assert client.get('/api/scan-runs').json()['items'][0]['status']=='skipped'


def test_automatic_running_blocks_manual(isolated,monkeypatch):
    with TestClient(main.app) as client:
        monkeypatch.setattr(main,'task',{'status':'running','type':'automatic'})
        assert client.post('/api/task',json={}).status_code==409


def test_automatic_418_keeps_other_markets_running_and_persists_ban(isolated,monkeypatch):
    async def run():
        repo=SQLiteSignalRepository(':memory:')
        monkeypatch.setattr(main.app.state,'repository',repo,raising=False)
        monkeypatch.setattr(main.app.state,'auto',AutoScanService(repo,Notifications()),raising=False)
        calls=[];normal=markets_handler(calls)
        def handler(request):
            if request.url.host=='fapi.binance.com':
                calls.append((request.url.host,request.url.path,{}))
                return httpx.Response(418,headers={'Retry-After':'3600'},json={'msg':'IP banned'})
            return normal(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            http=make_http(client,Clock())
            http.on_block=lambda host,details:repo.set_setting('ban',details)
            monkeypatch.setattr(main.app.state,'http',http,raising=False)
            await main.reserve_scan(main.ScanOptions(exchanges=['binance','okx','gate'],markets=['perpetual'],max_symbols=1),'automatic')
            await main.scan_runner
            assert main.task['status']=='completed_with_warnings'
            assert main.task['automatic']['rate_limited']==1
            assert main.task['market_stats']['binance:perpetual']['skip_reason']=='skipped_rate_limited'
            assert main.task['market_stats']['okx:perpetual']['status']=='success'
            assert main.task['market_stats']['gate:perpetual']['status']=='success'
            assert len([c for c in calls if c[0]=='fapi.binance.com'])==1
            assert repo.get_setting('ban')['status_code']==418
            assert repo.runs()[0]['type']=='automatic'
        repo.close()
    asyncio.run(run())


def test_hourly_guard_prevents_repeat_trigger(isolated,monkeypatch):
    with TestClient(main.app) as client:
        client.post('/api/auto-scan/enable',json={})
        main.app.state.repository.set_setting('auto_last_started',datetime.now(timezone.utc).isoformat())
        monkeypatch.setenv('AUTO_SCAN_TOKEN','test-only')
        result=client.post('/api/internal/auto-scan',headers={'Authorization':'Bearer test-only'}).json()
        assert result['reason']=='hourly_interval_not_elapsed'


def test_render_status_reports_ephemeral_storage_and_external_scheduler(isolated,monkeypatch):
    monkeypatch.setenv('RENDER','true');monkeypatch.setenv('AUTO_SCAN_SCHEDULER_MODE','external')
    with TestClient(main.app) as client:
        result=client.get('/api/auto-scan/status').json()
        assert not result['storage']['durable'] and result['scheduler_mode']=='external'
        assert result['warnings'] and result['next_run'] is None
        assert client.post('/api/auto-scan/enable',json={}).status_code==503


def test_interrupted_runs_are_recovered_after_restart(tmp_path):
    path=tmp_path/'scanner.db';repo=SQLiteSignalRepository(path)
    repo.save_run({'id':'run-1','type':'automatic','started_at':NOW.isoformat(),'status':'running'})
    repo.close();repo=SQLiteSignalRepository(path)
    assert repo.runs()[0]['status']=='failed'
    assert 'interrupted_by_restart' in repo.runs()[0]['warnings']
    repo.close()


def test_missing_or_gapped_history_is_not_analyzed(store):
    async def run():
        service=AutoScanService(store,Notifications())
        with pytest.raises(ValueError):await service.candles(Adapter(bars()[:20]),SYMBOL,'4h',NOW)
        with pytest.raises(ValueError):await service.candles(Adapter(bars()[:40]+bars()[41:]),SYMBOL,'4h',NOW)
        assert store.checkpoint(KEY) is None
    asyncio.run(run())


def test_shared_scan_only_analyzes_new_closed_candles(isolated,monkeypatch):
    async def run():
        clock=[NOW];calls=[];analyses=[]
        class FixedDate(datetime):
            @classmethod
            def now(cls,tz=None):return clock[0]
        class Exchange:
            def __init__(self,http):self.http=http
            def host(self,market):return 'api.binance.com'
            async def get_symbols(self,market):return [SYMBOL]
            async def get_klines(self,symbol,period,limit):
                calls.append((period,limit))
                if period=='1d':return [Candle(previous_utc_day(clock[0]),1,2,.5,1,100,20_000_000)]
                return bars(clock[0])[-limit:]
        original=main.analyze
        def counted(candles):analyses.append(candles[-1].time);return original(candles)
        monkeypatch.setattr(main,'EXCHANGES',{'binance':Exchange})
        monkeypatch.setattr(main,'datetime',FixedDate)
        monkeypatch.setattr(main,'analyze',counted)
        repo=SQLiteSignalRepository(':memory:');notifier=Notifications()
        monkeypatch.setattr(main.app.state,'repository',repo,raising=False)
        monkeypatch.setattr(main.app.state,'auto',AutoScanService(repo,notifier),raising=False)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(500))) as client:
            monkeypatch.setattr(main.app.state,'http',make_http(client,Clock()),raising=False)
            for hour in (0,1,4):
                clock[0]=NOW+timedelta(hours=hour)
                monkeypatch.setattr(main,'scan_next_start',0)
                await main.reserve_scan(main.ScanOptions(max_symbols=1),'automatic');await main.scan_runner
                if hour==0:assert main.task['automatic']['baseline_signals']==1 and not notifier.sent
                if hour==1:assert main.task['automatic']['no_new_candle']==1 and len(analyses)==1
            assert len(analyses)==2
            assert calls==[('1d',3),('4h',106),('4h',3)]
        repo.close()
    asyncio.run(run())


def test_last_skipped_check_keeps_completed_result_visible(isolated):
    with TestClient(main.app) as client:
        repo=main.app.state.repository
        repo.save_run({'id':'done','type':'automatic','status':'completed','started_at':NOW.isoformat(),
                       'automatic':{'new_signals':2}})
        repo.save_run({'id':'skip','type':'automatic','status':'skipped','started_at':(NOW+timedelta(hours=1)).isoformat()})
        value=client.get('/api/auto-scan/status').json()
        assert value['last_run']['id']=='done' and value['last_check']['id']=='skip'
        assert value['last_result']['new_signals']==2
