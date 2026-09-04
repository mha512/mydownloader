from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import worker
from models import Base, Job, ResourceLease


def test_claim_leaves_job_queued_when_global_quota_is_full(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "quota.db"}')
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    with session_factory.begin() as session:
        session.add_all([
            Job(id='quota-one', source_url='https://example.com/1',
                platform='youtube', status='queued'),
            Job(id='quota-two', source_url='https://example.com/2',
                platform='youtube', status='queued'),
        ])

    monkeypatch.setattr(worker, 'SessionLocal', session_factory)
    monkeypatch.setattr(worker, 'MAX_CONCURRENT_DOWNLOADS', 1)
    monkeypatch.setattr(worker, 'MAX_DOWNLOAD_BYTES', 100)
    monkeypatch.setattr(worker, 'MAX_TEMP_DISK_BYTES', 200)
    monkeypatch.setattr(worker, 'MIN_FREE_DISK_BYTES', 0)

    first = worker.claim_job()
    second = worker.claim_job()
    assert first == 'quota-one'
    assert second is None

    with session_factory.begin() as session:
        assert session.get(Job, 'quota-two').status == 'queued'
        session.query(ResourceLease).filter_by(job_id=first).delete()

    assert worker.claim_job() == 'quota-two'
