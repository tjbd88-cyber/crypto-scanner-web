from app.chart import chart_payload
from app.main import app, chart_cache
from test_strategy import sample
from fastapi.testclient import TestClient
import time


def test_chart_parameters_recalculate_indicators_without_changing_ohlc():
    bars = sample()
    normal = chart_payload(bars)
    adjusted = chart_payload(bars, boll_period=30, boll_std=2.5,
                             macd_fast=8, macd_slow=26, macd_signal=9,
                             sar_step=.04, sar_max=.3, kdj_period=9)
    assert len(normal['bars']) == len(bars)
    assert normal['bars'][-1]['close'] == adjusted['bars'][-1]['close']
    assert normal['bars'][-1]['boll_upper'] != adjusted['bars'][-1]['boll_upper']
    assert normal['bars'][-1]['macd_hist'] != adjusted['bars'][-1]['macd_hist']
    assert normal['bars'][-1]['kdj_k'] != adjusted['bars'][-1]['kdj_k']


def test_chart_api_uses_saved_bars_and_validates_parameters():
    key = ('binance', 'spot', 'TESTUSDT', '4h')
    chart_cache[key] = (time.monotonic()+60, sample(), 'scan')
    with TestClient(app) as client:
        response = client.get('/api/chart', params=dict(zip(
            ('exchange','market','pair','period'), key)))
        assert response.status_code == 200
        body = response.json()
        assert body['source'] == 'scan'
        assert body['bars_count'] == len(sample())
        assert body['bars'][-1]['ha_close'] is not None
        invalid = client.get('/api/chart', params={
            'exchange':'binance','market':'spot','pair':'TESTUSDT','period':'4h',
            'macd_fast':30,'macd_slow':20})
        assert invalid.status_code == 422


def test_chart_accepts_listed_unicode_pair_name():
    key = ('binance', 'perpetual', '龙虾USDT', '1d')
    chart_cache[key] = (time.monotonic()+60, sample(), 'scan')
    with TestClient(app) as client:
        response = client.get('/api/chart', params=dict(zip(
            ('exchange','market','pair','period'), key)))
    assert response.status_code == 200
    assert response.json()['pair'] == '龙虾USDT'
