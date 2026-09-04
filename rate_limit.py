"""Replaceable rate-limiting primitives for anonymous request endpoints."""

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import text

from models import RateLimitBucket


class InMemoryRateLimiter:
    def __init__(self, capacity, refill_seconds, clock=None):
        self.capacity = max(1, int(capacity))
        self.refill_seconds = max(1.0, float(refill_seconds))
        self.clock = clock or time.monotonic
        self._buckets = {}
        self._lock = threading.Lock()

    def allow(self, key, cost=1):
        cost = max(1, int(cost))
        now = self.clock()
        with self._lock:
            tokens, updated_at = self._buckets.get(
                key, (float(self.capacity), now))
            tokens = min(
                self.capacity,
                tokens + (now - updated_at) *
                self.capacity / self.refill_seconds,
            )
            if tokens < cost:
                retry_after = max(
                    1, int((cost - tokens) * self.refill_seconds / self.capacity)
                )
                self._buckets[key] = (tokens, now)
                return False, retry_after
            self._buckets[key] = (tokens - cost, now)
            return True, 0


class DatabaseRateLimiter:
    """Shared token bucket using the application's transactional database."""

    def __init__(self, session_factory, capacity, refill_seconds):
        self.session_factory = session_factory
        self.capacity = max(1, int(capacity))
        self.refill_seconds = max(1.0, float(refill_seconds))

    def allow(self, key, cost=1):
        cost = max(1, int(cost))
        now = datetime.now(timezone.utc)
        with self.session_factory.begin() as session:
            if session.get_bind().dialect.name == 'postgresql':
                session.execute(text(
                    'SELECT pg_advisory_xact_lock(hashtext(:key))'
                ), {'key': str(key)})
            bucket = session.get(RateLimitBucket, str(key))
            if bucket is None:
                tokens = float(self.capacity)
                updated_at = now
            else:
                updated_at = bucket.updated_at
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                tokens = min(
                    self.capacity,
                    bucket.tokens + max(0, (now - updated_at).total_seconds())
                    * self.capacity / self.refill_seconds,
                )
            if tokens < cost:
                retry_after = max(
                    1, int((cost - tokens) * self.refill_seconds / self.capacity)
                )
                allowed = False
            else:
                tokens -= cost
                retry_after = 0
                allowed = True
            if bucket is None:
                session.add(RateLimitBucket(
                    key=str(key), tokens=tokens, updated_at=now))
            else:
                bucket.tokens = tokens
                bucket.updated_at = now
            return allowed, retry_after
