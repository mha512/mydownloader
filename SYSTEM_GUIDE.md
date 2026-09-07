# VidzFlow System Guide

This document explains how VidzFlow is implemented and how it behaves when people use it at the same time.

## 1. What the system does

VidzFlow accepts public video URLs from supported platforms. It can:

1. Check whether the URL belongs to a supported platform.
2. Create a job for preview or download.
3. Find available video formats.
4. Download and convert the selected video.
5. Upload the finished MP4 to private S3-compatible storage.
6. Give the user a temporary signed download URL.

The system is designed for public media URLs. It does not provide user accounts or a permanent user library.

## 2. Main hosted parts

### Web service

The web service is the Flask application in `app.py`. On Render, it runs with Gunicorn and two web worker processes:

```text
gunicorn --bind 0.0.0.0:$PORT --workers 2 app:app
```

The web service handles short operations:

- Accepting submissions
- Validating URLs
- Creating database jobs
- Returning signed job tokens
- Returning job status
- Returning temporary file links
- Serving the website and API

The web service does not perform the long video download itself.

### Background worker

The background worker is `worker.py`. It continuously checks PostgreSQL for queued jobs, claims one safely, downloads it with yt-dlp, converts it with FFmpeg, uploads it to S3, and updates the job status.

This separation keeps web requests from staying open during a long download.

### PostgreSQL

PostgreSQL is the shared source of truth for:

- Jobs and their statuses
- Job attempts and errors
- Resource leases
- Worker heartbeats
- Shared IP rate-limit buckets

Because all web processes and workers use the same database, they can coordinate safely.

### S3-compatible storage

Finished files are stored in S3-compatible object storage. The hosted worker uses the configured S3 credentials and bucket. The browser receives a temporary signed URL instead of receiving the file directly from the worker's local disk.

## 3. What happens when a user submits a job

### Direct download flow

1. The browser sends a request to `POST /download`.
2. The web service checks the rate limit and request format.
3. It checks that the URL uses HTTPS and belongs to a supported platform.
4. It checks the active-job limit.
5. It writes a new `queued` job to PostgreSQL.
6. It returns a signed job token immediately with HTTP `202`.
7. The browser polls `GET /jobs/<job-token>` for status.
8. The worker claims the job and changes its status as work progresses.
9. The worker stores the finished file in S3.
10. The job becomes `ready`, and the status response contains a temporary file URL.

### Preview and format-selection flow

1. The browser sends a URL to `POST /preview`.
2. The job enters the `preview` queue.
3. The worker extracts metadata and available formats.
4. The job becomes `awaiting_format`.
5. The browser sends the selected format to `POST /download-quality`.
6. The job returns to `queued`.
7. The worker downloads, converts, uploads, and marks it `ready`.

## 4. How multiple users are handled

Many users can submit requests at approximately the same time because the web service only records jobs and responds quickly.

The jobs are placed in one shared PostgreSQL queue. Workers claim jobs using PostgreSQL row locking with `FOR UPDATE SKIP LOCKED`. This means two workers should not claim the same job.

With the current Render settings:

```text
Web processes:              2
WORKER_CONCURRENCY:         1
MAX_CONCURRENT_DOWNLOADS:   2
MAX_ACTIVE_JOBS:            20
```

The practical behavior is:

- Up to 20 active or queued jobs can be tracked.
- One video download is processed at a time by the current worker process.
- Additional jobs wait in PostgreSQL.
- A new job is rejected with HTTP `429` after the active-job limit is reached.
- The two web processes continue handling submissions and status polling while the worker downloads.

`MAX_CONCURRENT_DOWNLOADS=2` is a global resource limit. It does not cause two downloads by itself because `WORKER_CONCURRENCY=1` currently limits the worker process to one job.

The exact number of website visitors the hosted service can support has not been measured. Render plan size, database performance, download source behavior, network speed, CPU, memory, and disk all affect that number.

## 5. Job statuses

Jobs move through statuses similar to these:

```text
queued -> processing -> extracting -> awaiting_format
queued -> processing -> extracting -> downloading -> converting
       -> checking -> uploading -> ready
```

A job can also become `failed` when it reaches a permanent error, exceeds its retry attempts, times out, or is found stale after a worker interruption.

The browser does not need to keep a download request open. It uses the signed token to check the current status.

## 6. Concurrency and safety controls

### Database job claiming

The worker locks a selected queued job before claiming it. `SKIP LOCKED` allows another worker to skip a job that is already being claimed instead of waiting on the same row.

### Resource leases

Before claiming work, the worker checks:

- Number of active resource leases
- Reserved temporary disk space
- Actual free temporary disk space

It creates a resource lease for the job. The lease is removed after completion or failure. This prevents workers from starting more work than the configured global resource limits allow.

### Advisory locks

PostgreSQL advisory locks protect shared decisions such as capacity checks and rate-limit updates when multiple processes act at the same time.

### Rate limiting

`POST /preview`, `POST /download`, and `POST /bulk-download` use a database-backed token bucket keyed by client IP. The default hosted values allow about 20 requests per IP in a 60-second period before returning HTTP `429`.

This limit is shared between the two web processes because the bucket is stored in PostgreSQL.

### Active-job limit

The default `MAX_ACTIVE_JOBS` is 20. This protects the database, worker queue, disk, and storage from unlimited submissions.

### Retries

Temporary network, timeout, storage, and selected HTTP errors can be retried. The default maximum is three attempts. Permanent validation and size errors are not retried.

### Stale-job recovery

The worker marks jobs as failed when their active status has not changed within `WORKER_JOB_TIMEOUT_SECONDS`. This prevents an interrupted worker from leaving a job permanently stuck.

## 7. URL and SSRF protection

The web service accepts only supported HTTPS platform URLs.

Before yt-dlp runs, the worker:

1. Resolves the original hostname.
2. Rejects private, loopback, link-local, multicast, reserved, and unspecified addresses.
3. Checks HTTP redirect responses without automatically following them.
4. Resolves and validates every redirect `Location` target.
5. Stops after a maximum of five redirects.

A redirect to addresses such as these is rejected:

```text
127.0.0.1
localhost
10.0.0.0/8
169.254.169.254
```

This application-level protection is tested locally. Production network egress restrictions and monitoring are still recommended.

## 8. File and download protections

The worker enforces configured limits for:

- Maximum download size
- Temporary disk reservation
- Minimum free disk space
- Download timeout
- Number of retries

Downloaded media is converted to a playable MP4 with FFmpeg. Files are placed in a per-job temporary directory and uploaded to S3 after processing.

Old jobs and files are cleaned up after `JOB_RETENTION_SECONDS`. Orphaned storage objects are also checked by the worker cleanup process.

## 9. Anonymous job tokens

There are no user accounts. A job token is returned when a job is created.

The token:

- Is signed with `VIDZFLOW_SECRET_KEY`.
- Contains the job identifier.
- Expires after `VIDZFLOW_JOB_TOKEN_MAX_AGE` seconds.
- Is required to read that job's status.

The secret key must be long, random, and the same wherever tokens need to be created or read.

## 10. Monitoring endpoint

`GET /metrics` returns a JSON operational snapshot containing:

- Job counts by state
- Active resource leases
- Temporary disk totals, used space, and free space
- Worker heartbeat age

It requires:

```text
Authorization: Bearer <METRICS_AUTH_TOKEN>
```

Without the correct token, it returns HTTP `401`.

The endpoint is intended for operators, not normal users. It currently returns JSON rather than Prometheus format.

## 11. Hosted deployment

The Render configuration defines:

- A web service running the Flask application with Gunicorn
- A worker service running `worker.py`
- A managed PostgreSQL database

Both services run database migrations before starting. The worker also receives the S3 settings needed to store finished files.

Important hosted environment variables include:

```text
DATABASE_URL
VIDZFLOW_SECRET_KEY
METRICS_AUTH_TOKEN
S3_ENDPOINT_URL
S3_REGION
S3_BUCKET
S3_ACCESS_KEY_ID
S3_SECRET_ACCESS_KEY
WORKER_CONCURRENCY
MAX_CONCURRENT_DOWNLOADS
MAX_ACTIVE_JOBS
MAX_DOWNLOAD_BYTES
MAX_TEMP_DISK_BYTES
WORKER_MAX_ATTEMPTS
WORKER_JOB_TIMEOUT_SECONDS
```

Secrets and storage credentials should be configured as private Render environment variables.

## 12. Current capacity answer

Based on the current configuration, not a production load test:

- One video download progresses at a time.
- Up to 20 jobs can be active or waiting.
- Two web processes handle short web requests.
- More people may visit the site, but the safe number of simultaneous visitors is not known.

A rough planning assumption is that 10 to 30 ordinary users may be able to use the web interface at once, but this is only an estimate. It must not be treated as a measured capacity guarantee.

To obtain a real number, run Locust against the deployed Render service using valid test media and observe:

- Successful jobs per minute
- Queue waiting time
- p50 and p95 completion time
- PostgreSQL load
- Worker CPU and memory
- Temporary disk usage
- S3 bandwidth and errors
- Failure and retry rates

Only a staging Render/PostgreSQL/S3 test can establish production capacity.

## 13. Verified local behavior

The repository has verified the following locally:

- PostgreSQL-backed worker concurrency: passed without skips
- PostgreSQL-backed resource quota behavior: passed without skips
- Concurrent submissions and stale-job recovery: passed
- Redirect-hop rejection using a mocked `302 Location` to loopback: passed
- Metrics authentication: `401` without credentials and `200` with the correct bearer token
- Full test suite: 19 tests passed
- Local two-user Locust smoke: 22 requests, 0 failures

The Locust smoke used deliberately invalid media URLs, so it checked request and polling behavior only. It was not a throughput or production-capacity measurement.

## 14. Remaining production work

The most important remaining work is a real staging load test. It must use the deployed web service, worker, PostgreSQL, and S3 together.

The following are still not proven by local tests:

- Maximum sustained simultaneous users on Render
- Download throughput with valid media
- Behavior under real Render CPU and memory limits
- PostgreSQL saturation point
- S3 bandwidth limits
- Multiple separate worker processes under production load
- Production network egress policy

Until that test is completed, capacity should be described as configured limits and estimates, not as a guaranteed number of users.
