from fastapi.testclient import TestClient

from app.main import app


def test_home_health_and_static_files_without_exchange_requests():
    with TestClient(app) as client:
        home = client.get('/')
        health = client.get('/health')
        legacy_health = client.get('/api/health')
        css = client.get('/static/style.css')

    assert home.status_code == 200
    assert '底部双转换选币器' in home.text
    assert health.status_code == 200 and health.json() == {'status': 'ok'}
    assert legacy_health.status_code == 200 and legacy_health.json() == {'ok': True}
    assert css.status_code == 200 and 'text/css' in css.headers['content-type']


def test_scan_api_rejects_invalid_options_without_exchange_requests():
    with TestClient(app) as client:
        task = client.get('/api/task')
        invalid = client.post('/api/task', json={
            'exchanges': ['not-an-exchange'],
            'markets': ['spot'],
            'periods': ['4h'],
        })

    assert task.status_code == 200
    assert invalid.status_code == 422
