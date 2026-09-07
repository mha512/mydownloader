from app import app


def test_metrics_requires_operator_token(monkeypatch):
    monkeypatch.setitem(app.config, 'METRICS_AUTH_TOKEN', 'test-metrics-token')
    unauthorized = app.test_client().get('/metrics')
    authorized = app.test_client().get(
        '/metrics',
        headers={'Authorization': 'Bearer test-metrics-token'},
    )
    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_metrics_returns_safe_operational_snapshot(monkeypatch):
    monkeypatch.setitem(app.config, 'METRICS_AUTH_TOKEN', 'test-metrics-token')
    response = app.test_client().get(
        '/metrics',
        headers={'Authorization': 'Bearer test-metrics-token'},
    )
    body = response.get_json()
    assert response.status_code == 200
    assert body['status'] == 'ok'
    assert 'jobs_by_state' in body
    assert 'active_leases' in body
    assert set(body['disk']) == {'total_bytes', 'free_bytes', 'used_bytes'}
