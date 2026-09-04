import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app
from models import Job
from rate_limit import InMemoryRateLimiter
import worker


@pytest.mark.integration
def test_two_users_can_submit_simultaneously():
    app = create_app(InMemoryRateLimiter(10, 60))

    def submit():
        with app.test_client() as client:
            return client.post(
                '/preview',
                json={'url': 'https://www.youtube.com/watch?v=parallel'},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: submit(), range(2)))
    assert [response.status_code for response in responses] == [202, 202]


@pytest.mark.integration
def test_stale_claim_is_failed_for_conservative_recovery(monkeypatch):
    if not os.environ.get('TEST_DATABASE_URL'):
        pytest.skip(
            'Set TEST_DATABASE_URL for crash-recovery integration tests')
    monkeypatch.setattr(worker, 'WORKER_JOB_TIMEOUT_SECONDS', 1)
    with worker.SessionLocal.begin() as session:
        job = Job(
            id='stale-recovery-test', source_url='https://example.com',
            platform='youtube', status='processing', attempts=1,
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        session.merge(job)
    worker.claim_job()
    with worker.SessionLocal() as session:
        job = session.get(Job, 'stale-recovery-test')
        assert job.status == 'failed'
