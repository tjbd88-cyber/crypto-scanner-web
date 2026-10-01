"""Incremental closed-candle orchestration, independent of the strategy definition."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from app.models import Candle
from app.repository import SignalRepository
from app.notifications import NotificationService
from app.services.periods import SECONDS, bucket_start, build_candles

logger = logging.getLogger(__name__)
DEFAULT_CONFIG = {'enabled':False,'period':'4h','interval_seconds':3600,
                  'exchanges':['binance','okx','gate'],'markets':['spot','perpetual'],
                  'min_previous_day_turnover':10_000_000,'max_symbols':None}


def utc_now():
    return datetime.now(timezone.utc)


def next_hour(now=None):
    return (now or utc_now()).replace(minute=0,second=0,microsecond=0)+timedelta(hours=1)


def stream_key(exchange, market, pair, period):
    return ':'.join((exchange,market,pair,period))


def signal_id(row):
    timestamp = datetime.fromisoformat(row['candle_time'].replace('Z','+00:00')).astimezone(timezone.utc)
    return f'{stream_key(row["exchange"],row["market"],row["pair"],row["period"])}:{row["signal_type"]}:{timestamp.isoformat().replace("+00:00","Z")}'


def encode_candle(candle):
    return {**asdict(candle), 'time':candle.time.isoformat()}


class AutoScanService:
    def __init__(self, repository: SignalRepository, notifications: NotificationService):
        self.repository, self.notifications = repository, notifications

    def config(self):
        return {**DEFAULT_CONFIG, **self.repository.get_setting('auto_config', {})}

    def configure(self, **values):
        config = {**self.config(), **values}
        self.repository.set_setting('auto_config', config)
        return config

    async def candles(self, exchange, symbol, period, now):
        """Skip between boundaries with zero requests; at a boundary verify exchange candles.

        The clock is only a lower-cost skip hint. Formal signals require actual closed bars.
        Catch-up requests are sized to the gap; an absent history uses the manual warmup length.
        """
        key = stream_key(symbol.exchange,symbol.market,symbol.pair,period)
        previous = self.repository.checkpoint(key)
        expected = bucket_start(now,period)-timedelta(seconds=SECONDS[period])
        if previous and datetime.fromisoformat(previous['last_closed_candle_time']) >= expected:
            return key, previous, None
        stored = [Candle(**{**row,'time':datetime.fromisoformat(row['time'])}) for row in previous['candles']] if previous else []
        gap = int((expected-stored[-1].time).total_seconds() / SECONDS[period]) if stored else 106
        limit = min(106,max(3,gap+2)) if stored else 106
        raw = await exchange.get_klines(symbol,period,limit)
        closed = build_candles([c for c in raw if c.time+timedelta(seconds=SECONDS[period]) <= now],period,now)
        if not closed:
            raise ValueError('完整K线不足')
        if previous and closed[-1].time.isoformat() == previous['last_closed_candle_time']:
            return key, previous, None
        merged = {c.time:c for c in stored+closed}
        candles = sorted(merged.values(),key=lambda c:c.time)[-106:]
        # Never feed a gap in OHLC history into SAR/HA recursion.
        if any(b.time-a.time != timedelta(seconds=SECONDS[period]) for a,b in zip(candles,candles[1:])):
            raw = await exchange.get_klines(symbol,period,106)
            candles = build_candles([c for c in raw if c.time+timedelta(seconds=SECONDS[period]) <= now],period,now)
        if len(candles)<70 or any(b.time-a.time != timedelta(seconds=SECONDS[period]) for a,b in zip(candles,candles[1:])):
            raise ValueError('完整K线不足或缺失')
        return key, previous, candles

    async def record(self, key, previous, candles, row, now):
        result = {'new_signals':0,'existing_signals':0,'baseline_signals':0,'notification_failed':0}
        if row:
            value = {**row, 'stream_key':key, 'signal_type':'double_flip',
                     'detected_at':now.isoformat(),'status':'active','notified':previous is None,
                     'baseline':previous is None,
                     'created_at':now.isoformat(),'updated_at':now.isoformat()}
            value['id'] = signal_id(value)
            added = self.repository.insert_signal(value)
            if not added:
                result['existing_signals'] = 1
            elif previous is None:
                result['baseline_signals'] = 1
            else:
                result['new_signals'] = 1
                if self.repository.claim_notification(value['id']):
                    try:
                        delivered = await self.notifications.send_signal(value)
                    except Exception:
                        delivered = False
                        logger.warning('Signal notification failed id=%s', value['id'])
                    result['notification_failed'] = int(not delivered)
        else:
            # A later closed bar no longer meets the existing strategy: invalidate old active states.
            self.repository.invalidate(key,candles[-1].time.isoformat(),now.isoformat())
        self.repository.save_checkpoint(key, {'last_closed_candle_time':candles[-1].time.isoformat(),
                                              'candles':[encode_candle(c) for c in candles]})
        return result


class AutoScanScheduler:
    """One managed clock task; callback reserves the shared manual/auto scan slot."""
    def __init__(self, service, callback, *, mode='internal', clock=utc_now):
        self.service, self.callback, self.mode, self.clock = service, callback, mode, clock
        self.runner = None
        self.next_run = None
        self.wake = asyncio.Event()

    def start(self):
        if self.mode == 'internal' and (self.runner is None or self.runner.done()):
            self.runner = asyncio.create_task(self.run(),name='auto-scan-scheduler')

    def changed(self):
        self.wake.set()

    async def close(self):
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner,return_exceptions=True)
        self.next_run = None

    async def run(self):
        while True:
            enabled = self.service.config()['enabled']
            self.next_run = next_hour(self.clock()) if enabled else None
            delay = max(0,(self.next_run-self.clock()).total_seconds()) if self.next_run else 3600
            try:
                await asyncio.wait_for(self.wake.wait(),timeout=delay)
                self.wake.clear()
            except asyncio.TimeoutError:
                if self.service.config()['enabled']:
                    try:
                        await self.callback()
                    except Exception:
                        logger.exception('Automatic scan scheduler callback failed')
