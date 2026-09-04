from app import app


def test_metrics_returns_safe_operational_snapshot():
    response = app.test_client().get('/metrics')
    body = response.get_json()
    assert response.status_code == 200
    assert body['status'] == 'ok'
    assert 'jobs_by_state' in body
    assert 'active_leases' in body
    assert set(body['disk']) == {'total_bytes', 'free_bytes', 'used_bytes'}
