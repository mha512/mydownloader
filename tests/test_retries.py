from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from botocore.exceptions import ClientError

import worker
from models import Base, Job


def test_transient_failure_is_requeued_until_attempt_limit(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "retry.db"}')
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    with session_factory.begin() as session:
        session.add(Job(id='retry-job', source_url='https://example.com',
                        platform='youtube', status='processing', attempts=1))

    monkeypatch.setattr(worker, 'SessionLocal', session_factory)
    monkeypatch.setattr(worker, 'WORKER_MAX_ATTEMPTS', 3)
    monkeypatch.setattr(worker, 'WORKER_RETRY_BACKOFF_SECONDS', 0)

    assert worker.record_job_failure(
        'retry-job', ConnectionError('temporary network problem')) == 0
    with session_factory() as session:
        assert session.get(Job, 'retry-job').status == 'queued'

    with session_factory.begin() as session:
        session.get(Job, 'retry-job').status = 'processing'
        session.get(Job, 'retry-job').attempts = 3
    assert worker.record_job_failure(
        'retry-job', ConnectionError('temporary network problem')) is None
    with session_factory() as session:
        assert session.get(Job, 'retry-job').status == 'failed'


def test_size_failure_is_permanent(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "permanent.db"}')
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    with session_factory.begin() as session:
        session.add(Job(id='permanent-job', source_url='https://example.com',
                        platform='youtube', status='processing', attempts=1))

    monkeypatch.setattr(worker, 'SessionLocal', session_factory)
    assert worker.record_job_failure(
        'permanent-job', worker.DownloadSizeExceeded('too large')) is None
    with session_factory() as session:
        job = session.get(Job, 'permanent-job')
        assert job.status == 'failed'
        assert job.error_message == 'too large'


def test_storage_error_retry_classification_uses_status():
    error = ClientError(
        {'Error': {'Code': 'AccessDenied'},
         'ResponseMetadata': {'HTTPStatusCode': 403}},
        'PutObject',
    )
    assert worker.is_transient_error(error)

    error = ClientError(
        {'Error': {'Code': 'InvalidAccessKeyId'},
         'ResponseMetadata': {'HTTPStatusCode': 400}},
        'PutObject',
    )
    assert not worker.is_transient_error(error)
