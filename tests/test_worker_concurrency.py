import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import worker
from models import Base, Job


TEST_DATABASE_URL = os.environ.get('TEST_DATABASE_URL')


@pytest.mark.integration
def test_concurrent_workers_claim_each_job_once(monkeypatch):
    if not TEST_DATABASE_URL or TEST_DATABASE_URL.startswith('sqlite'):
        pytest.skip(
            'Set TEST_DATABASE_URL to PostgreSQL for this integration test')

    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    try:
        with session_factory.begin() as session:
            session.add_all([
                Job(id=f'concurrency-{index}', source_url='https://example.com',
                    platform='youtube', status='queued')
                for index in range(8)
            ])

        monkeypatch.setattr(worker, 'SessionLocal', session_factory)
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed = list(pool.map(lambda _: worker.claim_job(), range(8)))

        assert len(claimed) == 8
        assert None not in claimed
        assert len(set(claimed)) == 8
        with session_factory() as session:
            statuses = session.query(Job.status).all()
            assert all(status == ('processing',) for status in statuses)
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
