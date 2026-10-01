from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
import math
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from app.exchanges.base import PublicHttp, RateLimitError
from app.exchanges.binance import BinanceExchange
from app.exchanges.gate import GateExchange
from app.exchanges.okx import OKXExchange
from app.chart import chart_payload
from app.models import Symbol
from app.services.periods import BASE_PERIOD, SECONDS, build_candles
from app.strategy import analyze
from app.universe import eligible_symbols

PERIODS = ('15m','30m','1h','2h','4h','6h','8h','12h','1d','2d','3d','5d','1w')
EXCHANGES = {'binance': BinanceExchange, 'okx': OKXExchange, 'gate': GateExchange}
STATIC = Path(__file__).resolve().parent.parent / 'static'
STATE = Path(__file__).resolve().parent.parent / 'data' / 'last_task.json'
SCAN_COOLDOWN_SECONDS = 60
CHART_CACHE_LIMIT = 64
logger = logging.getLogger(__name__)


class ScanOptions(BaseModel):
    exchanges: list[str] = ['binance']
    markets: list[str] = ['spot']
    periods: list[str] = ['4h']
    min_previous_day_turnover: float = Field(default=10_000_000, ge=0)
    max_symbols: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode='after')
    def valid(self):
        if not self.exchanges or any(x not in EXCHANGES for x in self.exchanges):
            raise ValueError('请选择有效交易所')
        if not self.markets or any(x not in ('spot','perpetual') for x in self.markets):
            raise ValueError('请选择现货或永续')
        if not self.periods or any(x not in PERIODS for x in self.periods):
            raise ValueError('请选择有效周期')
        if any(len(set(values)) != len(values) for values in (self.exchanges, self.markets, self.periods)):
            raise ValueError('交易所、市场和周期不能重复')
        return self


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Scan and chart requests share one host limiter and a bounded cache for this process.
    async with httpx.AsyncClient(headers={'User-Agent':'BottomReversalScanner/1.0','Accept':'application/json'},
                                 timeout=15, limits=httpx.Limits(max_connections=12, max_keepalive_connections=8)) as client:
        application.state.http = PublicHttp(client)
        try:
            yield
        finally:
            if scan_runner and not scan_runner.done():
                scan_runner.cancel()
                await asyncio.gather(scan_runner, return_exceptions=True)


app = FastAPI(title='底部双转换选币器', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=STATIC), name='static')
def load_previous_task() -> dict | None:
    try:
        previous = json.loads(STATE.read_text(encoding='utf-8'))
        return previous if previous.get('schema_version') in (2, 3) and previous.get('status') in ('completed', 'completed_with_warnings', 'stopped', 'failed') else None
    except (OSError, ValueError, AttributeError):
        return None


def save_task(job: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    temporary.replace(STATE)


task: dict | None = load_previous_task()
cancel = False
scan_runner: asyncio.Task | None = None
scan_next_start = 0.0
chart_cache: OrderedDict[tuple[str,str,str,str], tuple[float,list,str]] = OrderedDict()


def cache_chart(key, candles, source):
    for old_key, item in list(chart_cache.items()):
        if item[0] <= time.monotonic():
            del chart_cache[old_key]
    chart_cache[key] = (time.monotonic()+300, candles, source)
    chart_cache.move_to_end(key)
    while len(chart_cache) > CHART_CACHE_LIMIT:
        chart_cache.popitem(last=False)


def market_label(name: str, market: str) -> str:
    return f'{dict(binance="Binance", okx="OKX", gate="Gate")[name]} {"现货" if market == "spot" else "永续"}'


def rate_warning(name: str, market: str) -> str:
    return f'{market_label(name, market)} 暂时受上游 IP 限流影响，本轮已跳过，不影响其他市场扫描。'


def totals(job):
    for value in job['market_stats'].values():
        value['matched_symbols'] = len({row['pair'] for row in job['results']
                                      if row['exchange'] == value['exchange'] and row['market'] == value['market']})
    fields = ('total_symbols', 'processed_symbols', 'matched_symbols', 'failed_symbols', 'skipped_symbols')
    job['stats'] = {key: sum(value.get(key, 0) for value in job['market_stats'].values()) for key in fields}


def warn(job, message):
    if message not in job['warnings'] and len(job['warnings']) < 100:
        job['warnings'].append(message)


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')


@app.get('/api/health')
def health():
    return {'ok': True}


@app.get('/health')
def public_health():
    return {'status': 'ok'}


@app.get('/api/task')
def get_task():
    return {**(task or {'status':'idle','results':[],'errors':[],'done':0,'total':0}),
            'scan_cooldown_seconds': math.ceil(max(0, scan_next_start-time.monotonic()))}


@app.get('/api/exchanges/status')
def exchange_status():
    return {'hosts': [app.state.http.snapshot(host) for host in
                     ('api.binance.com', 'fapi.binance.com', 'www.okx.com', 'api.gateio.ws')]}


@app.post('/api/task')
async def start(options: ScanOptions):
    global task, cancel, scan_runner
    if task and task['status'] == 'running':
        raise HTTPException(409, '扫描正在进行')
    remaining = math.ceil(scan_next_start-time.monotonic())
    if remaining > 0:
        raise HTTPException(429, f'扫描冷却中，请 {remaining} 秒后重试', headers={'Retry-After':str(remaining)})
    cancel = False
    task = {'schema_version':3,'id':str(uuid.uuid4()),'status':'running','phase':'filtering',
            'started_at':datetime.now(timezone.utc).isoformat(),'options':options.model_dump(),
            'results':[],'universe':[],'errors':[],'done':0,'total':0,
            'filter_done':0,'filter_total':0,'filter_error_count':0,
            'current':'', 'finished_at':None, 'warnings':[], 'duration':0,
            'market_stats': {f'{name}:{market}': {
                'exchange':name, 'market':market, 'status':'pending', 'available_symbols':None,
                'total_symbols':0, 'processed_symbols':0, 'matched_symbols':0,
                'failed_symbols':0, 'skipped_symbols':0, 'duration':0, 'warnings':[]}
                for name in options.exchanges for market in options.markets}}
    totals(task)
    scan_runner = asyncio.create_task(_scan(options, task))
    return {'id':task['id']}


@app.post('/api/task/stop')
async def stop():
    global cancel, scan_next_start
    cancel = True
    if scan_runner and not scan_runner.done():
        scan_runner.cancel()
        await asyncio.gather(scan_runner, return_exceptions=True)
    # Cancellation before the coroutine's first instruction must also finish the task.
    if task and task['status'] == 'running':
        task['status'] = 'stopped'
        task['finished_at'] = datetime.now(timezone.utc).isoformat()
        for stats in task['market_stats'].values():
            if stats['status'] in ('pending','filtering','scanning'):
                stats['status'] = 'stopped'
                stats['skipped_symbols'] = max(0, stats['total_symbols']-stats['processed_symbols']-stats['failed_symbols'])
        totals(task)
        scan_next_start = time.monotonic()+SCAN_COOLDOWN_SECONDS
        try:
            save_task(task)
        except OSError:
            warn(task, '结果文件暂时无法保存；当前页面结果仍可查看')
    return {'stopping': True}


@app.get('/api/chart')
async def chart(exchange: str, market: str, pair: str, period: str,
                boll_period: int = Query(20, ge=5, le=60),
                boll_std: float = Query(2, gt=0, le=5),
                macd_fast: int = Query(5, ge=1, le=50),
                macd_slow: int = Query(21, ge=2, le=100),
                macd_signal: int = Query(4, ge=1, le=50),
                sar_step: float = Query(.02, gt=0, le=.2),
                sar_max: float = Query(.2, gt=0, le=1),
                kdj_period: int = Query(13, ge=3, le=50),
                kdj_smooth: int = Query(3, ge=2, le=10)):
    if exchange not in EXCHANGES or market not in ('spot','perpetual') or period not in PERIODS:
        raise HTTPException(422, '交易所、市场或周期无效')
    if not re.fullmatch(r'[\w-]{3,40}', pair, re.UNICODE):
        raise HTTPException(422, '交易对格式无效')
    if macd_fast >= macd_slow or sar_step > sar_max:
        raise HTTPException(422, '指标参数无效：MACD Fast 必须小于 Slow，SAR Step 不得大于 Maximum')
    key = (exchange, market, pair, period)
    cached = chart_cache.get(key)
    if cached and cached[0] > time.monotonic():
        candles, source = cached[1], cached[2]
        chart_cache.move_to_end(key)
    else:
        base, multiplier = BASE_PERIOD.get(period, (period, 1))
        try:
            adapter = EXCHANGES[exchange](app.state.http)
            symbol = Symbol(exchange, pair, pair, market)
            raw = await adapter.get_klines(symbol, base, 103*multiplier+3)
            now = datetime.now(timezone.utc)
            closed = [c for c in raw if c.time.timestamp()+SECONDS[base] <= now.timestamp()]
            candles = build_candles(closed, period, now)
            if len(candles) < 70:
                raise ValueError(f'完整K线不足，仅有 {len(candles)} 根')
        except RateLimitError as exc:
            raise HTTPException(503, f'{market_label(exchange, market)} 暂时被上游限流，请稍后重试',
                                headers={'Retry-After':str(exc.details['retry_after'])}) from exc
        except Exception as exc:
            logger.warning('Chart request failed exchange=%s market=%s pair=%s type=%s', exchange, market, pair, type(exc).__name__)
            raise HTTPException(502, '图表行情获取失败：API 暂时不可用或完整 K 线不足，请稍后重试') from exc
        source = 'latest'
        cache_chart(key, candles, source)
    return {'exchange':exchange, 'market':market, 'pair':pair, 'period':period,
            'source':source, 'bars_count':len(candles),
            **chart_payload(candles, boll_period=boll_period, boll_std=boll_std,
                            macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
                            sar_step=sar_step, sar_max=sar_max,
                            kdj_period=kdj_period, kdj_smooth=kdj_smooth)}


async def _scan(options: ScanOptions, job: dict):
    global scan_next_start
    began = time.monotonic()
    try:
        scan_now = datetime.fromisoformat(job['started_at'])
        http = app.state.http
        for name in options.exchanges:
            exchange = EXCHANGES[name](http)
            for market in options.markets:
                if cancel:
                    break
                stats = job['market_stats'][f'{name}:{market}']
                market_start = time.monotonic()
                host = exchange.host(market)
                before = http.snapshot(host)
                stats['status'] = 'filtering'
                try:
                    http.check_available(host)
                    job['phase'] = 'filtering'
                    job['filter_done'] = job['filter_total'] = 0
                    job['current'] = f'{market_label(name, market)}: 正在读取上一完整 UTC 自然日成交额'

                    def update_filter(done: int, total: int, pair: str) -> None:
                        job['filter_done'], job['filter_total'] = done, total
                        job['current'] = f'{market_label(name, market)}: 前一日成交额筛选 {pair}'
                        stats['duration'] = round(time.monotonic()-market_start, 2)
                        totals(job)

                    volume_day, choices, volume_errors = await eligible_symbols(
                        exchange, market, options.min_previous_day_turnover,
                        progress=update_filter, stopped=lambda: cancel, now=scan_now,
                        max_symbols=options.max_symbols, stats=stats,
                    )
                    job['errors'].extend(f'{market_label(name, market)} 日成交额: {error}' for error in volume_errors[:max(0,100-len(job['errors']))])
                    job['filter_error_count'] += stats['failed_symbols']
                    job['universe'].extend({'exchange':name,'market':market,'pair':symbol.pair,
                                            'previous_day_turnover':round(turnover,2),
                                            'volume_date':volume_day.date().isoformat()}
                                           for symbol, turnover in choices)
                    if stats.get('halted'):
                        raise RateLimitError(host, stats['upstream_error'])
                    job['total'] += len(choices)*len(options.periods)
                    job['phase'] = 'scanning'
                    stats['status'] = 'scanning'
                    for symbol, turnover in choices:
                        if cancel:
                            break
                        raw_cache = {}
                        failed, matched = False, False
                        for period in options.periods:
                            job['current'] = f'{market_label(name, market)} {symbol.pair} {period}'
                            try:
                                base, multiplier = BASE_PERIOD.get(period,(period,1))
                                # Keep the existing warmup length and closed-candle strategy input.
                                needed = 103*multiplier + 3
                                if base not in raw_cache or len(raw_cache[base]) < needed:
                                    raw_cache[base] = await exchange.get_klines(symbol,base,needed)
                                now = datetime.now(timezone.utc)
                                raw = [c for c in raw_cache[base] if c.time.timestamp()+SECONDS[base] <= now.timestamp()]
                                candles = build_candles(raw,period,now)
                                if len(candles) < 70:
                                    raise ValueError('完整K线不足')
                                signal = analyze(candles)
                                cache_chart((name, market, symbol.pair, period), candles, 'scan')
                                if signal:
                                    matched = True
                                    job['results'].append({'exchange':name,'market':market,'pair':symbol.pair,
                                        'period':period,'previous_day_turnover':round(turnover,2),
                                        'volume_date':volume_day.date().isoformat(), **signal.to_dict()})
                            except RateLimitError as exc:
                                if exc.request_sent:
                                    stats['failed_symbols'] += 1
                                raise
                            except Exception as exc:
                                failed = True
                                logger.warning('Symbol analysis failed exchange=%s market=%s pair=%s period=%s type=%s',
                                               name, market, symbol.pair, period, type(exc).__name__)
                                if len(job['errors']) < 100:
                                    job['errors'].append(f'{job["current"]}: API 请求失败或完整 K 线不足')
                            finally:
                                job['done'] += 1
                        stats['failed_symbols' if failed else 'processed_symbols'] += 1
                        stats['matched_symbols'] += int(matched)
                        stats['duration'] = round(time.monotonic()-market_start, 2)
                        totals(job)
                    stats['status'] = 'completed_with_warnings' if stats['failed_symbols'] else 'success'
                    if stats['failed_symbols']:
                        message = f'{market_label(name, market)} 有 {stats["failed_symbols"]} 个币种读取失败'
                        stats['warnings'].append(message)
                        warn(job, message)
                except RateLimitError as exc:
                    stats['status'] = 'rate_limited'
                    stats['upstream_error'] = exc.details
                    message = rate_warning(name, market)
                    stats['warnings'].append(message)
                    warn(job, message)
                except Exception as exc:
                    stats['status'] = 'unavailable'
                    logger.warning('Market scan unavailable exchange=%s market=%s type=%s', name, market, type(exc).__name__)
                    message = f'{market_label(name, market)} API 暂时不可用，本轮已跳过'
                    stats['warnings'].append(message)
                    warn(job, message)
                finally:
                    if stats['status'] in ('rate_limited', 'unavailable'):
                        accounted = stats['processed_symbols']+stats['failed_symbols']+stats['skipped_symbols']
                        stats['skipped_symbols'] += max(0, stats['total_symbols']-accounted)
                    stats['duration'] = round(time.monotonic()-market_start, 2)
                    after = http.snapshot(host)
                    stats['http'] = {key:after[key]-before[key] for key in ('requests','http_429','http_418','timeouts','cache_hits')}
                    totals(job)
            if cancel:
                break
        job['results'].sort(key=lambda x:({'启动':0,'双转换确认':1,'临界':2,'观察':3}[x['stage']],-x['score'],x['pair']))
        job['status'] = 'stopped' if cancel else 'completed_with_warnings' if job['warnings'] or job['errors'] else 'completed'
    except asyncio.CancelledError:
        job['status'] = 'stopped'
    except Exception:
        job['status'] = 'failed'
        logger.exception('Unexpected scan error')
        warn(job, '扫描发生内部错误，请稍后重试')
    finally:
        scan_next_start = time.monotonic()+SCAN_COOLDOWN_SECONDS
        for stats in job['market_stats'].values():
            if stats['status'] in ('pending','filtering','scanning'):
                stats['status'] = 'stopped' if job['status'] == 'stopped' else 'unavailable'
                accounted = stats['processed_symbols']+stats['failed_symbols']+stats['skipped_symbols']
                stats['skipped_symbols'] += max(0, stats['total_symbols']-accounted)
        totals(job)
        job['duration'] = round(time.monotonic()-began, 2)
        job['current'] = '部分市场完成，详见交易所状态' if job['status'] == 'completed_with_warnings' else ''
        job['finished_at'] = datetime.now(timezone.utc).isoformat()
        try:
            save_task(job)
        except OSError:
            warn(job, '结果文件暂时无法保存；当前页面结果仍可查看')
            if job['status'] == 'completed':
                job['status'] = 'completed_with_warnings'
