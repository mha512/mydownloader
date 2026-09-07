# VidzFlow Audit Issues

This file records findings from the full audit pass run on 2026-09-04. Findings are based on commands run during this pass only.

## Recently Resolved

### Dependency vulnerabilities in declared requirements

Before the upgrade, `pip-audit -r requirements.txt` reported 12 known advisories in three declared packages. OSV supplied CVSS vectors, and the CVSS calculator classified them as 3 High and 9 Medium findings. The requirements were upgraded in one isolated change to Flask `3.1.3`, Requests `2.34.2`, and Werkzeug `3.1.8`.

- `flask 2.3.3`: `PYSEC-2026-2151`; CVSS 4.3 Medium; session access could miss `Vary: Cookie`, enabling cache leakage; fixed by Flask `3.1.3`.
- `requests 2.31.0`: `PYSEC-2026-1873`; CVSS 5.6 Medium; a prior `verify=False` session request could disable later TLS verification; fixed by Requests `2.32.0`.
- `requests 2.31.0`: `PYSEC-2026-1872`; CVSS 5.3 Medium; crafted URLs could leak `.netrc` credentials to third parties; fixed by Requests `2.32.4`.
- `requests 2.31.0`: `PYSEC-2026-2275`; CVSS 5.5 Medium; zipped-path extraction used a predictable temporary filename; fixed by Requests `2.33.0`.
- `werkzeug 2.3.7`: `PYSEC-2023-221`; CVSS 7.5 High; crafted multipart data could cause resource exhaustion; fixed by Werkzeug `3.1.8`.
- `werkzeug 2.3.7`: `PYSEC-2026-2045`; CVSS 6.3 Medium; Windows UNC paths could bypass `safe_join` on Python below 3.11; fixed by Werkzeug `3.1.8`.
- `werkzeug 2.3.7`: `PYSEC-2026-2046`; CVSS 6.3 Medium; Windows device names could bypass `safe_join`; fixed by Werkzeug `3.1.8`.
- `werkzeug 2.3.7`: `PYSEC-2026-2044`; CVSS 5.3 Medium and 6.3 Medium; device names with extensions or trailing spaces could bypass `safe_join`; fixed by Werkzeug `3.1.8`.
- `werkzeug 2.3.7`: `PYSEC-2026-2043`; CVSS 7.5 High; debugger interaction with an attacker-controlled domain could enable code execution; fixed by Werkzeug `3.1.8`.
- `werkzeug 2.3.7`: `PYSEC-2026-2320`; CVSS 5.3 Medium; path-prefixed Windows device names could bypass `safe_join`; fixed by Werkzeug `3.1.8`.
- `werkzeug 2.3.7`: `PYSEC-2026-3417`; CVSS 7.5 High and 6.9 Medium; multipart parsing could permit resource exhaustion; fixed by Werkzeug `3.1.8`.

The duplicate `werkzeug` `PYSEC-2023-221` entry was emitted by the original audit output. Post-upgrade audit output was `No known vulnerabilities found`.

## Medium Priority

### Bandit B104: bind-all-interfaces warning

Bandit reported `app.py:436` for `app.run(host='0.0.0.0')`. This is intentional for a container service and is required for Render/container port exposure. It is not treated as an application defect, but the development server must not be used as the production server; Render uses Gunicorn.

### Worker exception logging: fixed

Before the fix, the retry path used:

```text
2026-09-04 17:34:43,298 WARNING Job job-synthetic will retry after 5.0 seconds: ERROR: [generic] synthetic: Unable to download webpage: HTTP Error 403: Forbidden
2026-09-04 17:34:43,298 WARNING Job job-synthetic will retry after 5.0 seconds: An error occurred (AccessDenied) when calling the PutObject operation: synthetic storage failure
```

The code now uses `safe_error_text()` in the retry and terminal-failure paths. It removes URL query strings and fragments and truncates messages to 240 characters. Actual post-fix output was:

```text
2026-09-04 17:34:58,769 WARNING Job job-synthetic will retry after 5.0 seconds (DownloadError): ERROR: [generic] https://www.youtube.com/watch HTTP Error 403: Forbidden
2026-09-04 17:34:58,771 ERROR Job job-synthetic failed (ClientError): An error occurred (AccessDenied) when calling the PutObject operation: synthetic storage failure
```

The submitted URL itself is not passed directly to these log calls. yt-dlp may include a public source URL in an exception, so query and fragment redaction is now applied. Botocore's normal exception string contains the operation, error code, and provider message, not response headers; it is still bounded before logging.

## Low Priority / Informational

Bandit also reported:

- `config.py:29`, `B105`: the development secret fallback is detected as a hardcoded password. Production rejects this fallback and short secrets. This is a deliberate local-development guard, not a production credential.
- `worker.py:8`, `B404`: importing `subprocess`. This is expected because FFmpeg and FFprobe must be invoked.
- `worker.py:533`, `B603`: subprocess invocation for FFmpeg. It uses an argument list, no shell, resolved local executable paths, and a timeout; assessed as a false positive for command injection in this context.
- `worker.py:553`, `B603`: subprocess invocation for FFprobe. It uses an argument list, no shell, resolved local executable paths, and a timeout; assessed as a false positive for command injection in this context.

## Verified Clean or Passing Checks

- Docker PostgreSQL 16 and MinIO containers were running and healthy.
- Full post-upgrade test suite: `19 passed in 3.23s` during this pass.
- JavaScript syntax: both `static/js/downloader.js` and `static/js/site.js` passed `node --check`.
- `git diff --check`: no whitespace errors; Git emitted only an unrelated line-ending warning for an existing template.
- No `package.json` was found, so there are no JavaScript package dependencies to audit.
- No `safe_join` or `send_from_directory` usage exists. The only external-title path is yt-dlp's per-job output template; a post-upgrade test with reserved names, separators, and trailing characters kept every generated path inside the per-job directory with no path separator in the filename.
- `.env` is ignored and not tracked. Git history contained no committed file literally named `.env` and no AWS access-key-shaped history match.
- Client-facing Flask error handlers return generic JSON and do not return stack traces, filesystem paths, database URLs, or S3 credentials.
- `/metrics` is protected by bearer-token authentication and fails closed when the token is empty.
- Startup now rejects `JOB_RETENTION_SECONDS < S3_PRESIGNED_URL_SECONDS`.

## Not Verified

- Production Render throughput and simultaneous-user capacity.
- Production egress restrictions.
- Real staging S3 crash simulations.
