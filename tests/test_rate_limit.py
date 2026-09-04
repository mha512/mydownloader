from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base
from rate_limit import DatabaseRateLimiter, InMemoryRateLimiter


def test_token_bucket_rejects_burst_and_refills():
    now = [0.0]
    limiter = InMemoryRateLimiter(2, 10, clock=lambda: now[0])

    assert limiter.allow('client')[0]
    assert limiter.allow('client')[0]
    assert not limiter.allow('client')[0]
    now[0] = 5
    assert limiter.allow('client')[0]


def test_submission_endpoints_return_json_429():
    from app import create_app

    app = create_app(InMemoryRateLimiter(1, 60))
    client = app.test_client()
    valid_url = 'https://www.youtube.com/watch?v=example'

    first = client.post('/preview', json={'url': valid_url})
    second = client.post('/preview', json={'url': valid_url})
    download = client.post('/download', json={'url': valid_url})

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.is_json
    assert second.headers['Retry-After']
    assert download.status_code == 429


def test_database_limit_is_shared_between_web_processes(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "rate-limit.db"}')
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    first_process = DatabaseRateLimiter(session_factory, 1, 60)
    second_process = DatabaseRateLimiter(session_factory, 1, 60)

    assert first_process.allow('shared-client')[0]
    assert not second_process.allow('shared-client')[0]
