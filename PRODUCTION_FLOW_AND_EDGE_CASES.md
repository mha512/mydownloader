# VidzFlow Production Flow and Edge-Case Review

This document describes how the current system behaves in production, what a user can experience at each step, and what can go wrong. It is written for review before opening the service to many users.

## 1. System in Simple Terms

VidzFlow has two services:

- **Web service:** receives URLs, validates them, creates database jobs, and reports job status.
- **Worker service:** reads jobs from the database, asks `yt-dlp` for media information, downloads and converts the media, uploads the result to private S3 storage, and stores a temporary download link.

The database is the shared meeting point between the web service and the worker. PostgreSQL is required for production. SQLite is only suitable for one local process.

The browser does not download the source video directly. It talks to the web service first. The worker does the slow work in the background.

## 2. Normal Browser Flow

### Step 1: User opens the home page

1. The web service renders `templates/pages/home/index.html`.
2. The browser displays the URL input and the supported platforms.
3. The browser can also open the privacy and terms pages.
4. No account is created and no job exists yet.

Possible problems:

- The web service is asleep, unavailable, or starting.
- The page loads but external Google Fonts are blocked.
- A browser with JavaScript disabled cannot use the downloader controls.
- The page can show a platform detection message based on text matching even when the server will later reject the URL.

### Step 2: User enters a URL

The browser shows a friendly platform hint by searching for words such as `youtube.com`. This is only a visual hint. The server performs the real validation.

The server accepts only:

- HTTPS URLs
- Supported hostnames
- URLs without embedded usernames or passwords
- URLs without explicit ports
- URLs without fragments
- URLs within the maximum URL length

The server does not yet contact the platform at this point.

Possible problems:

- A URL contains a supported word in the wrong place and receives a misleading browser hint.
- A valid-looking URL is private, deleted, age-restricted, login-required, or unsupported by the current `yt-dlp` extractor.
- A platform changes its URL behavior after this version is deployed.
- A very long URL is rejected.

### Step 3: User clicks `Fetch Video`

The browser sends `POST /preview`.

The web service:

1. Parses the JSON body.
2. Validates the URL and platform.
3. Checks the active-job capacity.
4. Creates a job with status `preview`.
5. Creates a signed temporary job token.
6. Returns HTTP `202` with the job token.

The browser then starts polling `GET /jobs/<job-token>`.

At this point, the source media has not been downloaded. A database row and a queue item exist.

Possible problems:

- The body is not valid JSON.
- The request is larger than the configured request limit.
- The service is at capacity.
- The database is unavailable.
- The response is delayed, lost, or returned as HTML instead of JSON.
- The browser loses the job token because the tab is closed or storage is cleared.

### Step 4: Worker claims the preview job

The worker periodically looks for jobs with status `queued` or `preview`.

When it claims a job, it changes the status to `processing`, increments the attempt count, and starts a metadata-only `yt-dlp` extraction.

The worker then changes the status to `extracting` and asks the platform for information such as:

- Title
- Thumbnail URL
- Available formats
- Resolution
- Frame rate
- Estimated file size

No complete video file should be downloaded during this step.

Possible problems:

- No worker is running.
- The worker cannot reach the platform.
- The platform requires authentication.
- The extractor is outdated.
- The source returns a playlist or multiple entries unexpectedly.
- Metadata has no reliable file-size information.
- The metadata response is extremely large or slow.
- The thumbnail URL is unavailable or later becomes invalid.

### Step 5: Worker prepares visible formats

The worker considers video-capable formats and applies the configured size limit.

It then:

1. Removes formats estimated to be over the limit.
2. Groups formats that look the same to a user.
3. Keeps the stronger variant inside each visible group.
4. Sorts formats from higher resolution to lower resolution.
5. Adds an estimated size when one is available.
6. Saves the resulting list in `available_formats`.
7. Changes the job status to `awaiting_format`.

The browser receives the format list on its next status poll and renders one button per returned format.

Example visible result:

```text
1080p MP4    1920x1080 / 30 fps / 420.0 MB
720p MP4     1280x720 / 30 fps / 180.0 MB
480p MP4     854x480 / 30 fps / 95.0 MB
```

Possible problems:

- Two technically different formats may still look the same if their displayed fields match.
- A format with unknown size can remain visible and fail later.
- The size estimate may be conservative because it uses available audio information rather than the exact final selection.
- A format list from an old job may still contain duplicates; the browser has a defensive visible-deduplication step.
- The best format is not automatically added when other formats exist.
- No format may remain after size filtering.
- The worker can fail the job if the best available option is also over the limit.

### Step 6: User selects a format

The browser marks one format button as selected and sends `POST /download-quality`.

The web service:

1. Validates the signed job token.
2. Checks that the job is in `awaiting_format`.
3. Checks that the selected ID was included in the saved format list.
4. Stores the selected format.
5. Changes the job status to `queued`.

The worker later picks up the job.

Possible problems:

- The token expired.
- The preview was cleaned up before the user clicked download.
- The format list is stale because the platform changed its formats.
- The user clicks twice quickly.
- Two browser tabs submit the same job at the same time.
- The selected format disappears between metadata extraction and actual download.

### Step 7: Worker validates and downloads

Before downloading, the worker extracts metadata again. It checks that the selected format still exists and checks the estimated size again.

The worker builds a selector:

- `best` means best video plus best audio.
- A progressive format with audio uses that exact format.
- A video-only format is paired with best audio.

There is no intentional fallback from a requested format to a different quality.

During the transfer, the worker tracks downloaded bytes and stops when the configured limit is exceeded. This protects sources that do not provide a file-size estimate.

Possible problems:

- The selected format no longer exists.
- The source changes its signed media URL.
- Video and audio cannot be combined.
- The source reports incomplete or incorrect sizes.
- A stream is fragmented and progress accounting is imperfect.
- A download fails after consuming bandwidth.
- The user’s requested quality is technically available but cannot be converted.
- Multiple users compete for one worker’s CPU, network, temporary disk, or memory.

### Step 8: Worker converts and checks the file

After download, the worker:

1. Finds the largest supported output file in the temporary directory.
2. Rejects it if it exceeds the size limit.
3. Runs FFmpeg to create an H.264 MP4.
4. Rejects the converted file if it exceeds the limit.
5. Uses FFprobe to confirm the video codec is H.264.

Possible problems:

- FFmpeg or FFprobe is missing.
- Conversion takes longer than the fixed conversion timeout.
- Conversion creates a file larger than the original.
- The source has no video stream.
- The output is corrupt or has an unexpected codec.
- Temporary disk space runs out.
- Several users fill the worker disk at the same time.

### Step 9: Worker uploads to private storage

The worker uploads the converted MP4 to S3-compatible storage under a job-specific key.

It generates a presigned URL and stores that URL in the job record. The browser receives the URL when polling shows status `ready`.

Possible problems:

- Storage credentials are wrong.
- The bucket does not exist.
- The storage provider is unavailable.
- Upload succeeds but URL generation fails.
- Database update fails after upload.
- The presigned URL expires before the user clicks it.
- The object is deleted by cleanup while the user is starting the download.

The worker attempts to delete the uploaded object when the final database update fails.

### Step 10: Browser downloads the result

The browser creates a download link to the presigned storage URL. The actual file transfer normally goes from storage to the user, not through Flask.

Possible problems:

- The browser blocks automatic downloads.
- The presigned URL has expired.
- The user has a slow or interrupted connection.
- Storage returns an error or an expired signature.
- The browser downloads the file with an unhelpful filename.
- The user loses the result after closing the page because there is no job history.

## 3. Direct API Flow

`POST /download` skips the browser preview workflow.

The API:

1. Validates the URL.
2. Validates that an optional format is a simple format ID.
3. Creates a queued job.
4. Returns a signed token.

If no format is provided, the worker performs the preview-style metadata step first and waits for a selected format. An API client must understand the `awaiting_format` state and then call `/download-quality`.

API clients must handle:

- HTTP `202`: accepted, not finished.
- HTTP `400`: malformed or unsupported input.
- HTTP `413`: request or URL too large.
- HTTP `429`: capacity reached.
- HTTP `404`: invalid, expired, or deleted token/job.
- HTTP `409`: job is not in the required state.
- Job status `failed` with a user-facing message.

A direct API client can still misuse the service by distributing requests across many IP addresses. The shared database-backed limiter applies per client IP, but it is not a complete identity, bot, or distributed-abuse control.

## 4. Bulk API Flow

`POST /bulk-download` accepts up to ten URLs in one request.

For each URL, the server returns either:

- A queued job and signed token
- A URL-specific validation error
- A capacity error

Bulk processing continues for later URLs when an earlier URL is invalid.

Possible production behavior:

- One request can consume many capacity slots.
- The request can contain duplicate URLs, creating duplicate jobs.
- There is no per-user bulk quota beyond the request size and active capacity.
- A client must poll each returned job independently.
- A partial success response is normal.
- The frontend currently does not provide a bulk-download interface.

## 5. Job States

| State | Meaning | Normal next state |
|---|---|---|
| `preview` | Preview job is waiting for metadata extraction | `processing` |
| `queued` | Download is waiting for a worker | `processing` |
| `processing` | Worker has claimed the job | `extracting`, `downloading`, or `failed` |
| `extracting` | Metadata is being requested | `awaiting_format`, `downloading`, or `failed` |
| `awaiting_format` | User must choose a format | `queued`, `failed`, or cleanup deletion |
| `downloading` | Media bytes are being transferred | `converting` or `failed` |
| `converting` | FFmpeg is creating the final MP4 | `checking` or `failed` |
| `checking` | FFprobe is validating the result | `uploading` or `failed` |
| `uploading` | Object is being sent to storage | `ready` or `failed` |
| `ready` | Presigned URL is available | cleanup deletion |
| `failed` | The job cannot continue | cleanup deletion |

The most important state rule is that only the worker that owns the current attempt should be allowed to change the job. The current design reduces stale-worker conflicts by failing stale jobs and blocking late completion, but a full lease or worker-owner field would be needed for stronger crash recovery.

## 6. Production Edge-Case Catalogue

### User and input edge cases

- Empty URL
- Whitespace-only URL
- URL over the configured length
- HTTP instead of HTTPS
- Unsupported hostname
- Lookalike hostname such as `youtube.com.attacker.example`
- URL with username, password, port, or fragment
- Invalid JSON body
- JSON object with the wrong field type
- Bulk list containing non-string values
- Bulk list containing ten duplicate URLs
- Request body larger than the configured limit
- Valid URL for deleted or private media
- URL requiring login or cookies
- URL that points to a playlist even though playlists are disabled

### Platform and extractor edge cases

- Platform changes its page structure
- `yt-dlp` version becomes outdated
- Source rate-limits the worker
- Source returns HTTP 403 or 429
- Source returns a consent or login page
- Source returns no formats
- Source returns formats without file sizes
- Source returns only audio
- Source returns only video without audio
- Source returns separate video and audio streams
- Source returns HDR, AV1, VP9, WebM, or unusual codec variants
- Source returns duplicate-looking formats
- Source returns a very large format list
- Source changes media URLs between metadata and download

### Queue and concurrency edge cases

- Two users submit at the same time
- One user opens multiple browser tabs
- Two requests submit the same URL
- Two clients submit the same job token simultaneously
- Web service restarts after creating a job but before responding
- Worker restarts after claiming a job
- Worker dies during download
- Worker dies during conversion
- Worker dies during upload
- Two workers attempt to claim the same job
- Cleanup runs while a worker is claiming a job
- Database connection drops during a state update
- Database transaction is rolled back after a response was sent
- Capacity is reached between admission check and insert
- A bulk request partially fills the remaining capacity

### Resource edge cases

- Many users download at the same time
- One user submits repeated bulk requests
- Temporary disk fills up
- Memory pressure increases during FFmpeg conversion
- Network bandwidth is exhausted
- S3 upload bandwidth is exhausted
- A file has unknown size metadata
- A file exceeds the limit only after conversion
- A malicious or unusual title creates an unsafe filename
- A conversion runs until its timeout
- Old failed jobs accumulate
- Orphaned S3 objects accumulate
- Database table grows without bounds

### Browser and user-experience edge cases

- User refreshes during polling
- User closes the tab
- User loses the signed token
- Token expires while the job is still running
- Network temporarily fails during polling
- Browser receives a non-JSON error response
- Browser retries a request after a timeout even though the server accepted it
- User double-clicks Download
- User selects a format while the preview is still changing
- Presigned URL expires before download begins
- Browser blocks the download
- Mobile browser suspends the tab
- User starts a second job while the first is still active
- Dark mode or small-screen layout makes a status message hard to read

## 7. Remaining Risks to Review Before Public Launch

### High priority

1. **Anonymous identity is limited to client IP.** Signed tokens identify jobs, not users. The shared per-IP limiter reduces bursts, but a person can distribute requests across IPs or clients.

2. **Capacity protection is not a complete abuse control.** The active-job cap and per-IP limiter protect the common path, but they do not limit coordinated submissions, repeated failures, or all expensive metadata extraction.

3. **Worker throughput still needs measurement.** The default is one concurrent job and `WORKER_CONCURRENCY` is configurable, but capacity has not been measured against real CPU, memory, disk, network, and storage limits.

4. **Storage monitoring is incomplete.** Temporary disk reservations and free-space admission checks exist, but there is no operator alerting or complete S3 usage monitor.

5. **Unknown-size streams can still consume resources up to the streaming guard.** This limits damage but cannot prevent all bandwidth and temporary-file cost.

6. **Crash recovery is deliberately conservative.** A stale job is failed rather than safely retried. This avoids duplicate work but requires the user to submit again after a worker crash.

7. **Full production integration coverage is still incomplete.** Local tests cover concurrency, retries, quotas, security, and observability, while PostgreSQL/S3 crash and storage-failure scenarios remain environment-gated.

### Medium priority

1. **Retry coverage is intentionally classified.** Common network, timeout, HTTP 403/429/5xx, and storage failures retry within the configured attempt limit; permanent format, size, and validation failures remain terminal.

2. **The direct API and browser workflow are different.** API users must understand the preview and format-selection state machine. This is valid, but it needs explicit API documentation and tests.

3. **Duplicate jobs are allowed.** The same URL submitted repeatedly creates repeated extraction and downloads. This may be acceptable, but it should be a deliberate product decision.

4. **Migration operations need deployment discipline.** Alembic migrations exist and are run by both Render services, but production operators should still review migration ordering, backups, and rollback procedures.

5. **SSRF and egress protection are not complete.** The initial hostname allowlist is useful, but yt-dlp can follow redirects and access URLs returned by platforms. Production hosting should restrict worker egress and monitor unusual destinations.

6. **Some worker failures remain generic to users.** Logs contain more diagnostic information than the user-facing error message.

7. **The browser assumes JSON for many responses.** A proxy, platform, or Flask error page can return HTML and cause a JSON parsing error in the browser.

### Low priority

1. The frontend platform hint uses substring matching instead of the same hostname parser as the server.
2. The quality UI hides technical differences that may explain why two formats are different.
3. There is no cancellation button. A user cannot stop a job once it is queued or downloading.
4. There is no operator dashboard; `/metrics` provides a JSON snapshot instead.

## 8. Complexity That May Not Be Worth Adding Yet

These ideas can be useful later, but they should not be added before the simpler controls are tested:

- A full message broker such as Redis or Celery when PostgreSQL queueing is still sufficient.
- User accounts, profiles, download history, and permanent libraries.
- A complex worker lease system before basic restart and concurrency tests exist.
- Deduplication of identical URLs across all users. It creates privacy and ownership questions.
- A large frontend framework for one downloader page.
- Automatic quality recommendation based on many codec and bitrate rules.
- Playlist downloads before single-video reliability is proven.
- Multiple storage providers or cross-region replication before storage usage is measured.
- A complex global quota service before a simple per-IP limiter and active-job cap are deployed.

## 9. Design Choices That Are Reasonable

The following parts are not automatically overengineering:

- Separate web and worker services: appropriate because downloads and FFmpeg work are slow.
- PostgreSQL shared job records: appropriate for multiple web processes and workers.
- Private S3 storage with presigned URLs: appropriate when finished files should not pass through Flask.
- Signed temporary job tokens: appropriate for anonymous jobs, provided token leakage is accepted.
- Metadata-only preview before download: appropriate for showing quality choices and enforcing size limits early.
- A final size check after conversion: necessary because conversion can change file size.
- Automatic cleanup: necessary because anonymous jobs otherwise accumulate forever.

## 10. Recommended Production Order

### Before a private trial

- Run the existing integration-gated tests with shared PostgreSQL and S3.
- Test worker restart during every long-running state.
- Test storage failure and temporary-disk exhaustion.
- Confirm PostgreSQL and S3 settings are identical between environments where required.
- Confirm the configured size limit is the same for every worker.
- Remove real credentials from local files and rotate any credentials that may have been exposed.

### Before public launch

- Confirm the shared per-IP limiter and active-job quota across two web instances.
- Add alerting for queue, disk, memory, worker heartbeat, and storage health.
- Decide whether stale jobs should fail or use a carefully tested lease/retry system.
- Review Alembic migration and rollback procedures.
- Verify JSON error handling for `400`, `413`, `429`, and server errors in staging.
- Document token lifetime, job retention, size limits, and failure behavior.
- Add abuse reporting and a plan for platform or copyright complaints.

### After real usage data exists

- Measure queue wait time and worker throughput.
- Decide whether more workers are needed.
- Decide whether a broker is justified.
- Tune active limits and download limits from real resource usage.
- Add cancellation only if users regularly need it.
- Improve format selection using observed platform responses rather than guessing.

## 11. Launch Readiness Questions

The system should not be considered ready until the operator can answer “yes” to these questions:

- Can two users submit jobs without exceeding the active-job policy?
- Can a worker restart without producing duplicate ready files?
- Can a stale job be explained clearly to the user?
- Can the service stop an unknown-size download before it consumes too many resources?
- Are temporary files removed after success and failure?
- Are orphaned storage objects detected and removed?
- Can one user access only the job represented by their token?
- Are expired tokens and deleted jobs handled clearly?
- Do all API errors return predictable JSON?
- Are queue depth, worker health, disk usage, and storage usage observable?
- Are rate limits strong enough for anonymous public use?
- Do the privacy policy and terms describe the real retention and storage behavior?
- Does documentation match the implemented routes and features?

## 12. Short Conclusion

The project has a sensible basic shape for a small downloader: Flask handles requests, PostgreSQL coordinates jobs, a bounded worker performs media work, and private object storage serves completed files.

The largest remaining production risks are anonymous abuse beyond IP limits, resource exhaustion, conservative worker restart behavior, environment-gated integration tests, and incomplete operational monitoring.

The system should first be tested with a small private group. The most valuable next work is staging validation of the existing controls, observability and alerting, and restart/concurrency testing. Adding a larger queue framework or user-account system before those basics are measured would add complexity without solving the main risks.
