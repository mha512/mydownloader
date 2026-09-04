# VidzFlow Production Readiness Tracker

This file tracks the requested production-hardening work. Each step is studied before implementation, tested after implementation, and marked with remaining limits.

## Status Legend

- **Done:** implemented and covered by local tests.
- **Partial:** useful implementation exists, but production infrastructure or integration testing remains.
- **Open:** not implemented yet.

> Reality check: the status entries below reflect verifiable code and test evidence only. Anything that depends on staging PostgreSQL, S3, Render capacity, or operator policy remains labeled as Partial or Blocked rather than being treated as completed work. No speculative or overbuilt hardening was added beyond the minimal implementations needed for the current production requirements.

## 1. Multi-worker concurrency

**Status: Partial.** PostgreSQL job claiming uses `FOR UPDATE SKIP LOCKED`, and bounded in-process concurrency is configurable with `WORKER_CONCURRENCY`. The default remains one concurrent job because no staging load result is available yet.

**Acceptance checks:** multiple workers can claim different jobs; no job is claimed twice; queued work is not silently lost; worker concurrency is configurable.

**Tests:** `tests/test_worker_concurrency.py` runs eight concurrent claims and verifies unique ownership. It skips when `TEST_DATABASE_URL` is absent or SQLite is selected.

**Remaining production check:** run the test with a real shared PostgreSQL database and size `WORKER_CONCURRENCY` against CPU, memory, and disk capacity.

## 2. Per-IP rate limiting

**Status: Done with shared database backend.** Added a database-backed token bucket keyed by client IP. The `allow(key, cost)` interface remains swappable; the in-memory implementation remains available for isolated tests. Configuration uses `RATE_LIMIT_CAPACITY` and `RATE_LIMIT_REFILL_SECONDS`.

**Acceptance checks:** `/preview`, `/download`, and `/bulk-download` reject bursts with JSON `429`; allowance returns after the configured window; the limiter is isolated behind a replaceable interface.

**Tests:** `tests/test_rate_limit.py` covers burst rejection, refill, `/preview`, and `/download` endpoint behavior.

**Remaining production check:** run two real web instances against staging PostgreSQL and decide whether rate-limit bucket cleanup or proxy-level limits are also needed.

## 3. Global resource quotas

**Status: Done for database-backed reservation; production tuning remains open.** Added `resource_leases` with a PostgreSQL advisory lock, a global active-job limit, and a conservative temporary-disk reservation of twice `MAX_DOWNLOAD_BYTES` per job. Quota misses leave jobs queued.

**Acceptance checks:** active worker work is bounded across processes; quota rejection returns a job to `queued`; local temporary disk is checked before expensive work.

**Tests:** `tests/test_resource_quota.py` proves a second job remains queued at quota and claims after release.

**Remaining production check:** tune `MAX_CONCURRENT_DOWNLOADS` and `MAX_TEMP_DISK_BYTES` to the actual Render worker resources.

**Remaining production check:** quota behavior must be tested with multiple worker processes against PostgreSQL.

## 4. Bounded transient retries

**Status: Done for classified failures.** Common network, timeout, yt-dlp HTTP 403/429/5xx, and botocore failures requeue up to `WORKER_MAX_ATTEMPTS` with capped exponential backoff. Size, format, and other permanent failures remain terminal.

**Acceptance checks:** transient failures retry with bounded backoff; permanent failures do not retry; attempts never exceed configuration; users receive a final clear error.

**Tests:** `tests/test_retries.py` covers requeue/recovery eligibility, attempt exhaustion, and permanent size failure.

## 5. Crash and concurrency integration tests

**Status: Partial.** Added concurrent submission and conservative stale-claim coverage. Full PostgreSQL/S3 crash simulations remain environment-gated.

**Acceptance checks:** concurrent claims, two-user submission, cleanup/claim race, and worker interruption during download/conversion/upload are covered.

**Environment note:** PostgreSQL and S3 tests should skip clearly when integration services are not configured.

## 6. Security and correctness hardening

**Status: Partial.** URL host validation, signed tokens, consistent framework JSON errors, the documented platform endpoint, private-DNS rejection, and Alembic migrations now exist. Redirect-target validation and deployment egress policy remain.

**Acceptance checks:** consistent JSON errors; documented outbound policy; schema changes do not depend on racing application startup.

**Tests:** `tests/test_url_security.py` covers private and public DNS results. Framework 404 and 413 behavior is covered by endpoint checks. Alembic was applied locally through revision `0002_worker_heartbeats`.

## 7. Observability

**Status: Done for basic JSON metrics.** Added job state counts, active resource leases, temporary disk usage, and worker heartbeat age at `/metrics`.

**Acceptance checks:** queue depth, state counts, heartbeat, disk usage, and failure counts are available without exposing secrets or job URLs.

**Remaining gap:** metrics are JSON rather than Prometheus format and are not yet protected by an operator authentication policy.

## 8. Documentation and cleanup

**Status: Done for route and setting alignment.** README and Render settings now describe the implemented routes, current public-media scope, token/retention settings, limits, retries, quotas, and worker concurrency.

**Acceptance checks:** README matches routes/features; token, retention, size, rate, quota, retry, and worker settings are documented; unused code is removed only when safe.

## 9. Load-proven capacity

**Status: Blocked pending staging infrastructure.** The Locust scenario exists at `loadtest/locustfile.py`, but no staging base URL, approved success/failure media URLs, PostgreSQL metrics, worker resource metrics, or S3 test environment were available in this workspace. No fabricated capacity number is recorded.

**Acceptance checks:** establish a `WORKER_CONCURRENCY=1` baseline; test 2, 4, and 8 only when resources support them; record sustained concurrent users, queue wait degradation, jobs/minute, p50/p95 completion time, database saturation, memory/disk usage, S3 bandwidth, double-processing, starvation, and the measured bottleneck.

**Tests:** `python -m py_compile loadtest/locustfile.py` passed. Execute `locust -f loadtest/locustfile.py --host <staging-url>` with explicit `LOAD_TEST_SUCCESS_URL` and optional `LOAD_TEST_FAILURE_URL`.

**Remaining production check:** run the baseline and concurrency matrix against real staging PostgreSQL, worker, and S3, then replace this section's blocked status with measured numbers.

## Implementation Log

| Step | Change | Tests run | Result |
|---|---|---|---|
| 1 | Initial tracker and acceptance criteria | N/A | Recorded |
| 1 | Added bounded `WORKER_CONCURRENCY` and PostgreSQL claim integration test | `pytest tests/test_worker_concurrency.py -q` | Passes when PostgreSQL is configured; currently skipped without it |
| 2 | Added in-memory per-IP token bucket and endpoint wiring | `pytest tests/test_rate_limit.py -q` | Passed |
| 1 follow-up | Replaced default limiter with shared database buckets and added two-instance simulation | `pytest tests/test_rate_limit.py -q` | 3 passed; PostgreSQL staging validation remains |
| 3 | Added cross-worker resource leases and temp-disk reservation | `pytest tests/test_resource_quota.py -q` | Passed |
| 4 | Added transient failure classification and bounded backoff | `pytest tests/test_retries.py -q` | Passed |
| 5 | Added concurrent submission and conservative recovery tests | `pytest tests/test_concurrency_recovery.py -q` | Local submission passes; PostgreSQL recovery is integration-gated |
| 6 | Added JSON errors, platform route, Alembic path, and DNS checks | `pytest tests/test_url_security.py -q` | Passed locally; redirect egress remains deployment-gated |
| 7 | Added worker heartbeat and metrics endpoint | `pytest tests/test_observability.py -q` | Passed |
| 8 | Aligned README and Render settings | `git diff --check` | Passed |
| Final | Corrected retry classification, added free-disk admission, and ran cumulative validation | `pytest tests -q` | 10 passed, 2 skipped |
| 2 | Added Locust end-to-end load scenario | `python -m py_compile loadtest/locustfile.py` | Passed; staging baseline not run because no staging environment was configured |
| 5 | Added configurable SQLAlchemy PostgreSQL pooling | `python -m py_compile database.py config.py` | Pending final cumulative validation |
| 9 | Added load-proven capacity section and measurement procedure | N/A | Blocked pending staging infrastructure; no numbers claimed |
| Final follow-up | Shared database limiter, configurable pooling, Locust import, migrations, and cumulative tests | `pytest tests -q` | 11 passed, 2 skipped; no staging capacity numbers available |
