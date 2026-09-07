# VidzFlow Production Readiness Tracker

This file tracks the requested production-hardening work. Each step is studied before implementation, tested after implementation, and marked with remaining limits.

## Status Legend

- **Done:** implemented and covered by local tests.
- **Partial:** useful implementation exists, but production infrastructure or integration testing remains.
- **Open:** not implemented yet.

> Reality check: the status entries below reflect verifiable code and test evidence only. Anything that depends on staging PostgreSQL, S3, Render capacity, or operator policy remains labeled as Partial or Blocked rather than being treated as completed work. No speculative or overbuilt hardening was added beyond the minimal implementations needed for the current production requirements.

## 1. Multi-worker concurrency

**Status: Partial.** PostgreSQL job claiming uses `FOR UPDATE SKIP LOCKED`, and bounded in-process concurrency is configurable with `WORKER_CONCURRENCY`. A real local PostgreSQL 16 run passed `tests/test_worker_concurrency.py` with one test passed and zero skipped. The test required explicit test-only quota settings (`MAX_CONCURRENT_DOWNLOADS=8`, `MAX_DOWNLOAD_BYTES=1M`, `MAX_TEMP_DISK_BYTES=16M`) because its eight-claim assertion intentionally exceeds the production default quota.

**Acceptance checks:** multiple workers can claim different jobs; no job is claimed twice; queued work is not silently lost; worker concurrency is configurable.

**Tests:** `.venv\Scripts\python.exe -m pytest tests/test_worker_concurrency.py -v` against PostgreSQL 16: 1 passed, 0 skipped. The test runs eight concurrent claims and verifies unique ownership.

**Remaining production check:** run the test with multiple real worker processes and size `WORKER_CONCURRENCY` against CPU, memory, and disk capacity.

## 2. Per-IP rate limiting

**Status: Done with shared database backend.** Added a database-backed token bucket keyed by client IP. The `allow(key, cost)` interface remains swappable; the in-memory implementation remains available for isolated tests. Configuration uses `RATE_LIMIT_CAPACITY` and `RATE_LIMIT_REFILL_SECONDS`.

**Acceptance checks:** `/preview`, `/download`, and `/bulk-download` reject bursts with JSON `429`; allowance returns after the configured window; the limiter is isolated behind a replaceable interface.

**Tests:** `tests/test_rate_limit.py` covers burst rejection, refill, `/preview`, and `/download` endpoint behavior.

**Remaining production check:** run two real web instances against staging PostgreSQL and decide whether rate-limit bucket cleanup or proxy-level limits are also needed.

## 3. Global resource quotas

**Status: Done for database-backed reservation; production tuning remains open.** Added `resource_leases` with a PostgreSQL advisory lock, a global active-job limit, and a conservative temporary-disk reservation of twice `MAX_DOWNLOAD_BYTES` per job. Quota misses leave jobs queued.

**Acceptance checks:** active worker work is bounded across processes; quota rejection returns a job to `queued`; local temporary disk is checked before expensive work.

**Tests:** `.venv\Scripts\python.exe -m pytest tests/test_resource_quota.py -v` against PostgreSQL 16: 1 passed, 0 skipped. The test proves a second job remains queued at quota and claims after release.

**Remaining production check:** tune `MAX_CONCURRENT_DOWNLOADS` and `MAX_TEMP_DISK_BYTES` to the actual Render worker resources.

**Remaining production check:** quota behavior must be tested with multiple worker processes against PostgreSQL.

## 4. Bounded transient retries

**Status: Done for classified failures.** Common network, timeout, yt-dlp HTTP 403/429/5xx, and botocore failures requeue up to `WORKER_MAX_ATTEMPTS` with capped exponential backoff. Size, format, and other permanent failures remain terminal.

**Acceptance checks:** transient failures retry with bounded backoff; permanent failures do not retry; attempts never exceed configuration; users receive a final clear error.

**Tests:** `tests/test_retries.py` covers requeue/recovery eligibility, attempt exhaustion, and permanent size failure.

## 5. Crash and concurrency integration tests

**Status: Partial.** Local PostgreSQL integration now runs without skips: `tests/test_concurrency_recovery.py` passed both concurrent submission and stale-claim recovery tests. Full PostgreSQL/S3 crash simulations during download, conversion, upload, and interruption remain open.

**Acceptance checks:** concurrent claims, two-user submission, cleanup/claim race, and worker interruption during download/conversion/upload are covered.

**Environment note:** PostgreSQL and MinIO were started locally and healthy. S3 crash simulations were not exercised by the current test files.

## 6. Security and correctness hardening

**Status: Partial.** URL host validation, signed tokens, consistent framework JSON errors, the documented platform endpoint, private-DNS rejection, redirect-hop validation, and Alembic migrations now exist. Deployment egress policy remains a production concern.

**Acceptance checks:** consistent JSON errors; documented outbound policy; schema changes do not depend on racing application startup.

**Tests:** `.venv\Scripts\python.exe -m pytest tests/test_url_security.py -v`: 7 passed, including a mocked public URL returning `302 Location: http://127.0.0.1/internal`; the initial public host check passes and the redirect hop is rejected after re-validation. Framework 404 and 413 behavior is covered by endpoint checks. Alembic was applied locally through revision `0002_worker_heartbeats`.

## 7. Observability

**Status: Done for basic JSON metrics with operator bearer authentication.** Added job state counts, active resource leases, temporary disk usage, and worker heartbeat age at `/metrics`.

**Acceptance checks:** queue depth, state counts, heartbeat, disk usage, and failure counts are available without exposing secrets or job URLs.

**Remaining gap:** metrics are JSON rather than Prometheus format.

**Verification:** `.venv\Scripts\python.exe -m pytest tests/test_observability.py -v`: 2 passed, including `401` without credentials and `200` with the `METRICS_AUTH_TOKEN` bearer token.

## 8. Documentation and cleanup

**Status: Done for route and setting alignment.** README and Render settings now describe the implemented routes, current public-media scope, token/retention settings, limits, retries, quotas, and worker concurrency.

**Acceptance checks:** README matches routes/features; token, retention, size, rate, quota, retry, and worker settings are documented; unused code is removed only when safe.

## 9. Load-proven capacity

**Status: Blocked pending staging infrastructure.** The Locust scenario exists at `loadtest/locustfile.py`, but no staging base URL, approved success/failure media URLs, PostgreSQL metrics, worker resource metrics, or S3 test environment were available in this workspace. No fabricated capacity number is recorded.

**Acceptance checks:** establish a `WORKER_CONCURRENCY=1` baseline; test 2, 4, and 8 only when resources support them; record sustained concurrent users, queue wait degradation, jobs/minute, p50/p95 completion time, database saturation, memory/disk usage, S3 bandwidth, double-processing, starvation, and the measured bottleneck.

**Tests:** `python -m py_compile loadtest/locustfile.py` passed. Execute `locust -f loadtest/locustfile.py --host <staging-url>` with explicit `LOAD_TEST_SUCCESS_URL` and optional `LOAD_TEST_FAILURE_URL`.

**Remaining production check:** run the baseline and concurrency matrix against real staging PostgreSQL, worker, and S3, then replace this section's blocked status with measured numbers.

**Local correctness smoke test (not capacity):** with local PostgreSQL/MinIO healthy and `WORKER_CONCURRENCY=1`, ran `.venv\Scripts\python.exe -m locust -f loadtest/locustfile.py --headless --host http://127.0.0.1:5000 -u 2 -r 2 -t 10s --only-summary` using deliberately invalid supported URLs. Actual result: 22 requests, 0 failures, exit code 0 (2 previews and 20 job polls). This exercised preview and polling only; it did not produce production throughput numbers or reach format selection/download-quality because the worker was not run against valid media.

**Scope note:** this Locust scenario does not itself assert 429 responses, quota rejection, retries, or double-claim prevention; those correctness checks are represented by the focused pytest results above and `tests/test_rate_limit.py` / `tests/test_retries.py`.

**Production capacity remains Blocked:** only a staging Render/PostgreSQL/S3 run can close Section 9. This local run validates correctness smoke behavior, not throughput.

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
| Local integration | Started PostgreSQL 16 and MinIO with Docker Compose; ran PostgreSQL-backed worker concurrency, quota, recovery, and full suite | `docker compose up -d`; `pytest tests/test_worker_concurrency.py -v`; `pytest tests/test_resource_quota.py -v`; `pytest tests/test_concurrency_recovery.py -v`; `pytest tests -q` | Containers healthy; 1 + 1 + 2 integration tests passed; full suite 17 passed, 0 skipped |
| Local correctness smoke | Ran two-user, ten-second Locust smoke with `WORKER_CONCURRENCY=1` | `python -m locust -f loadtest/locustfile.py --headless --host http://127.0.0.1:5055 -u 2 -r 2 -t 10s --only-summary` | 10 requests, 0 failures; correctness smoke only, not capacity |
| Final follow-up | Shared database limiter, configurable pooling, Locust import, migrations, and cumulative tests | `pytest tests -q` | 11 passed, 2 skipped; no staging capacity numbers available |
| Verification | Re-verified Docker, local services, integration tests, regression tests, full suite, and local Locust smoke | `docker --version`; `docker compose version`; `docker info --format '{{.ServerVersion}}'`; `.venv\Scripts\python.exe -m pytest tests -q`; `.venv\Scripts\python.exe -m locust -f loadtest/locustfile.py --headless --host http://127.0.0.1:5000 -u 2 -r 2 -t 10s --only-summary` | Docker 29.7.2 / Compose v5.5.0; containers healthy; regression tests 7 passed; integration tests 4 passed; full suite 17 passed; Locust 22 requests, 0 failures; Section 9 remains Blocked pending staging Render/PostgreSQL/S3 |
| Security follow-up | Added real redirect-hop validation and bearer authentication for `/metrics` | `.venv\Scripts\python.exe -m pytest tests/test_url_security.py tests/test_observability.py -v`; `.venv\Scripts\python.exe -m pytest tests -q` | Focused security tests 9 passed; full suite 19 passed; redirect validation and metrics auth verified locally; deployment egress and staging capacity remain open |
