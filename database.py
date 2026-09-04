"""Database engine and session factory shared by web and worker services."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import (
    DATABASE_URL,
    DB_MAX_OVERFLOW,
    DB_POOL_RECYCLE,
    DB_POOL_SIZE,
    DB_POOL_TIMEOUT,
)
from models import Base


_engine_options = {'pool_pre_ping': True}
if DATABASE_URL.startswith('sqlite'):
    _engine_options['connect_args'] = {'check_same_thread': False}
else:
    _engine_options.update({
        'pool_size': DB_POOL_SIZE,
        'max_overflow': DB_MAX_OVERFLOW,
        'pool_timeout': DB_POOL_TIMEOUT,
        'pool_recycle': DB_POOL_RECYCLE,
    })

engine = create_engine(DATABASE_URL, **_engine_options)
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(bind=engine)
