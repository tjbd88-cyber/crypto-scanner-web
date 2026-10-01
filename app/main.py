from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from app.exchanges.base import PublicHttp
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


class ScanOptions(BaseModel):
    exchanges: list[str] = ['binance']
    markets: list[str] = ['spot']
    periods: list[str] = ['4h']
    min_previous_day_turnover: float = Field(default=10_000_000, ge=0)

    @model_validator(mode='after')
    def valid(self):
        if not self.exchanges or any(x not in EXCHANGES for x in self.exchanges):
            raise ValueError('请选择有效交易所')
        if not self.markets or any(x not in ('spot','perpetual') for x in self.markets):
            raise ValueError('请选择现货或永续')
        if not self.periods or any(x not in PERIODS for x in self.periods):
            raise ValueError('请选择有效周期')
        if len(set(self.periods)) != len(self.periods):
            raise ValueError('周期不能重复')
        return self


app = FastAPI(title='底部双转换选币器')
app.mount('/static', StaticFiles(directory=STATIC), name='static')
def load_previous_task() -> dict | None:
    try:
        previous = json.loads(STATE.read_text(encoding='utf-8'))
        return previous if previous.get('schema_version') == 2 and previous.get('status') in ('completed', 'stopped', 'failed') else None
    except (OSError, ValueError, AttributeError):
        return None


def save_task(job: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    temporary.replace(STATE)


task: dict | None = load_previous_task()
cancel = False
chart_cache: dict[tuple[str,str,str,str], tuple[float,list,str]] = {}


@app.get('/')
def index():
    return FileResponse(STATIC / 'index.html')


@app.get('/api/health')
def health():
    return {'ok': True}


@app.get('/api/task')
def get_task():
    return task or {'status':'idle','results':[],'errors':[],'done':0,'total':0}


@app.post('/api/task')
async def start(options: ScanOptions):
    global task, cancel
    if task and task['status'] == 'running':
        raise HTTPException(409, '扫描正在进行')
    cancel = False
    task = {'schema_version':2,'id':str(uuid.uuid4()),'status':'running','phase':'filtering',
            'started_at':datetime.now(timezone.utc).isoformat(),'options':options.model_dump(),
            'results':[],'universe':[],'errors':[],'done':0,'total':0,
            'filter_done':0,'filter_total':0,'filter_error_count':0,
            'current':'', 'finished_at':None}
    asyncio.create_task(_scan(options, task))
    return {'id':task['id']}


@app.post('/api/task/stop')
def stop():
    global cancel
    cancel = True
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
    else:
        base, multiplier = BASE_PERIOD.get(period, (period, 1))
        try:
            async with httpx.AsyncClient(headers={'User-Agent':'BottomReversalScanner/1.0','Accept':'application/json'}) as client:
                adapter = EXCHANGES[exchange](PublicHttp(client, concurrency=2))
                symbol = Symbol(exchange, pair, pair, market)
                raw = await adapter.get_klines(symbol, base, 120*multiplier+3)
            now = datetime.now(timezone.utc)
            closed = [c for c in raw if c.time.timestamp()+SECONDS[base] <= now.timestamp()]
            candles = build_candles(closed, period, now)
            if len(candles) < 70:
                raise ValueError(f'完整K线不足，仅有 {len(candles)} 根')
        except Exception as exc:
            raise HTTPException(502, f'图表行情获取失败：{type(exc).__name__}: {exc}') from exc
        source = 'latest'
        chart_cache[key] = (time.monotonic()+120, candles, source)
    return {'exchange':exchange, 'market':market, 'pair':pair, 'period':period,
            'source':source, 'bars_count':len(candles),
            **chart_payload(candles, boll_period=boll_period, boll_std=boll_std,
                            macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
                            sar_step=sar_step, sar_max=sar_max,
                            kdj_period=kdj_period, kdj_smooth=kdj_smooth)}


async def _scan(options: ScanOptions, job: dict):
    try:
        scan_now = datetime.fromisoformat(job['started_at'])
        async with httpx.AsyncClient(headers={'User-Agent':'BottomReversalScanner/1.0','Accept':'application/json'}) as client:
            http = PublicHttp(client, concurrency=4)
            exchanges = {name: EXCHANGES[name](http) for name in options.exchanges}
            for name, exchange in exchanges.items():
                for market in options.markets:
                    if cancel: break
                    try:
                        job['phase'] = 'filtering'
                        job['filter_done'] = 0
                        job['filter_total'] = 0
                        job['current'] = f'{name} {market}: 正在读取上一完整 UTC 自然日成交额'

                        def update_filter(done: int, total: int, pair: str) -> None:
                            job['filter_done'] = done
                            job['filter_total'] = total
                            job['current'] = f'{name} {market}: 前一日成交额筛选 {pair}'

                        volume_day, choices, volume_errors = await eligible_symbols(
                            exchange, market, options.min_previous_day_turnover,
                            progress=update_filter, stopped=lambda: cancel, now=scan_now,
                        )
                        for error in volume_errors:
                            if len(job['errors']) < 100:
                                job['errors'].append(f'{name} {market} 日成交额: {error}')
                        job['filter_error_count'] += len(volume_errors)
                        job['universe'].extend({'exchange':name,'market':market,'pair':symbol.pair,
                                                'previous_day_turnover':round(turnover,2),
                                                'volume_date':volume_day.date().isoformat()}
                                               for symbol, turnover in choices)
                        if cancel:
                            break
                        if not choices:
                            job['current'] = f'{name} {market}: 前一日成交额门槛下没有符合交易对'
                        job['total'] += len(choices)*len(options.periods)
                        job['phase'] = 'scanning'
                        for symbol, turnover in choices:
                            if cancel: break
                            raw_cache = {}
                            for period in options.periods:
                                if cancel: break
                                job['current'] = f'{name} {market} {symbol.pair} {period}'
                                try:
                                    base, multiplier = BASE_PERIOD.get(period,(period,1))
                                    # 100 closed bars is enough for indicator warmup and recent structure.
                                    needed = 103*multiplier + 3
                                    if base not in raw_cache or len(raw_cache[base]) < needed:
                                        raw_cache[base] = await exchange.get_klines(symbol,base,needed)
                                    now = datetime.now(timezone.utc)
                                    raw = [c for c in raw_cache[base] if c.time.timestamp()+SECONDS[base] <= now.timestamp()]
                                    candles = build_candles(raw,period,now)
                                    if len(candles) < 70:
                                        raise ValueError(f'完整K线不足，仅有 {len(candles)} 根')
                                    signal = analyze(candles)
                                    if signal:
                                        chart_cache[(name, market, symbol.pair, period)] = (time.monotonic()+300, candles, 'scan')
                                        job['results'].append({'exchange':name,'market':market,'pair':symbol.pair,
                                            'period':period,'previous_day_turnover':round(turnover,2),
                                            'volume_date':volume_day.date().isoformat(),
                                            **signal.to_dict()})
                                        job['results'].sort(key=lambda x:({'启动':0,'双转换确认':1,'临界':2,'观察':3}[x['stage']],-x['score'],x['pair']))
                                except Exception as exc:
                                    if len(job['errors']) < 100:
                                        job['errors'].append(f'{job["current"]}: {type(exc).__name__}: {exc}')
                                finally:
                                    job['done'] += 1
                    except Exception as exc:
                        job['errors'].append(f'{name} {market}: {type(exc).__name__}: {exc}')
                if cancel: break
        job['status'] = 'stopped' if cancel else 'completed'
    except Exception as exc:
        job['status'] = 'failed'
        job['errors'].append(f'扫描程序错误: {type(exc).__name__}: {exc}')
    finally:
        job['current'] = (f'前一日成交额有 {job["filter_error_count"]} 个币种读取失败，详见错误列表'
                          if job.get('filter_error_count') else '')
        job['finished_at'] = datetime.now(timezone.utc).isoformat()
        try:
            save_task(job)
        except OSError as exc:
            job['errors'].append(f'本地结果保存失败: {exc}')
