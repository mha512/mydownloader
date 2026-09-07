# 🌐 Universal Social Media Downloader

A scalable Flask web service with a separate Render worker, PostgreSQL job queue and S3-compatible private file storage. Users do not need accounts.

**Made with ❤️ by Soham Roy Chowdhury**

---

## 🚀 Key Features

### 🔄 Multi-Platform Download Support

* **YouTube**: Public videos and Shorts
* **Instagram**: Public posts and Reels where supported by yt-dlp
* **TikTok**: High-quality video downloads with metadata
* **Facebook**: Public videos and posts

### ⚙️ Advanced Functionality

* **Automatic Platform Detection** via URL
* **Bulk Downloads**: Submit up to ten URLs through the API
* **Best Available Quality** (up to 1080p)
* **Web UI + REST API**
* **Separate worker processing** for scalable downloads
* **Anonymous temporary jobs** without user accounts

---

## 📦 Installation

### ✅ Prerequisites

* Python 3.9+
* pip (Python package manager)
* PostgreSQL for shared job records
* S3-compatible storage for finished files

### Install Dependencies

```bash
pip install -r requirements.txt
```

The web service does not install or run media download tools. The separate worker service performs downloads and uploads finished files to S3-compatible storage. The complete runtime dependency list is maintained in `requirements.txt`.

Configure the server before starting it:

```text
VIDZFLOW_SECRET_KEY=<long-random-secret>
DATABASE_URL=postgresql+psycopg://user:password@host:5432/database
S3_BUCKET=your-private-bucket
S3_ACCESS_KEY_ID=your-storage-access-key
S3_SECRET_ACCESS_KEY=your-storage-secret-key
```

For local development, copy `.env.example` to `.env` and adjust the values for your environment. SQLite can be used locally, but production requires shared PostgreSQL and S3-compatible storage.

---

## 💻 Usage

### ▶️ Run the Server

```bash
python app.py
```

Then open `http://localhost:5000` in your browser.

### 🌐 Web Interface

1. Paste a supported public-media URL
2. Click `Fetch Video` and choose an available format
3. Download the completed MP4 when the job is ready

Bulk downloads are available through `POST /bulk-download`; the web interface currently handles one URL at a time.

---

## 🔌 API Endpoints

### `POST /preview`

Create a metadata-preview job. Poll the returned token until the job reaches `awaiting_format`, then submit the selected format to `/download-quality`.

### `POST /download`

Create a single download job. An optional `format` can be supplied; otherwise the worker may return `awaiting_format` after metadata extraction.

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID"
}
```

### `POST /bulk-download`

Download multiple URLs:

```json
{
  "urls": [
    "https://youtube.com/...",
    "https://instagram.com/...",
    "https://tiktok.com/..."
  ]
}
```

### `GET /jobs/<job-token>`

Get the status and final file link for one anonymous job. The token is returned by `POST /preview`, `POST /download`, or `POST /bulk-download` and expires automatically.

### `POST /download-quality`

Select one of the format IDs returned by a preview job and queue the download.

### `GET /supported-platforms`

Returns the supported platform names.

### `GET /metrics`

Returns a JSON operational snapshot containing job counts, active resource leases, worker heartbeat age, and temporary disk usage. Requires `Authorization: Bearer <METRICS_AUTH_TOKEN>`.

### `GET /health`

Checks that the web service can reach the database.

---

## ⚙️ Configuration

Copy `.env.example` to the environment settings for both Render services and replace every placeholder. Keep the database password, secret key and storage credentials private. Render must provide the same `DATABASE_URL` and `VIDZFLOW_SECRET_KEY` to the web and worker services.

Set `VIDZFLOW_SECRET_KEY` to exactly the same long random value in both services. Set `METRICS_AUTH_TOKEN` for operator access to `/metrics`. The web service limits active jobs with `MAX_ACTIVE_JOBS` and anonymous submission bursts with `RATE_LIMIT_CAPACITY` and `RATE_LIMIT_REFILL_SECONDS`. The worker uses bounded `WORKER_CONCURRENCY`, `MAX_CONCURRENT_DOWNLOADS`, and `MAX_TEMP_DISK_BYTES`. Temporary failures retry up to `WORKER_MAX_ATTEMPTS` with `WORKER_RETRY_BACKOFF_SECONDS`. Anonymous job tokens last `VIDZFLOW_JOB_TOKEN_MAX_AGE` seconds and files/jobs are retained for `JOB_RETENTION_SECONDS`.

---

## 🧱 Project Structure

```
SocialMediaDownloader/
├── app.py                # Lightweight Flask coordinator
├── config.py             # Environment configuration
├── database.py           # Shared database engine and sessions
├── models.py             # Shared job model
├── worker.py             # Render background worker
├── anonymous_jobs.py     # Expiring anonymous job tokens
├── url_validation.py     # URL and platform validation
├── .env.example         # Safe configuration template
├── alembic/              # Database migrations
├── loadtest/             # Locust staging scenario
├── templates/            # Jinja templates
├── tests/                # Automated tests
├── requirements.txt      # Server dependencies
└── README.md             # Project documentation
```

---

## 📱 Platform-Specific Highlights

### YouTube

* Supports public videos and Shorts where supported by yt-dlp
* Best quality + uploader/title metadata

### Instagram

* Public posts and reels where supported by yt-dlp
* Private content and login-required content are not supported

### TikTok

* High-resolution videos
* Public videos where supported by yt-dlp

### Facebook

* Public videos where supported by yt-dlp

---

## 🛠 Error Handling & Troubleshooting

* Clear errors for invalid or private URLs
* Logs platform-specific failures
* Bulk downloads continue despite individual errors

### Common Fixes

* `ModuleNotFoundError` → Run `pip install -r requirements.txt`
* Download failure → Check if the URL is public
* Storage error → Check the S3 bucket credentials and permissions

### Render deployment

Deploy `render.yaml` as a Blueprint. It creates a web service and a separate background worker. Both services must use the same PostgreSQL database and `VIDZFLOW_SECRET_KEY`. Configure the S3-compatible storage credentials on the worker.

SQLite is only the local-development fallback. Do not use SQLite for the Render deployment because Render instances can restart and multiple services cannot safely share a local SQLite file. The PostgreSQL plan and S3-compatible storage may have a cost; confirm current Render and storage pricing before deploying.

## Load testing

Install development tools with `pip install -r requirements-dev.txt`. Run the Locust scenario only against staging:

```bash
set LOAD_TEST_SUCCESS_URL=https://www.youtube.com/watch?v=...
set LOAD_TEST_FAILURE_URL=https://www.youtube.com/watch?v=known-invalid-test-id
locust -f loadtest/locustfile.py --host https://staging.example.com
```

The scenario exercises preview, realistic job polling, format selection, download polling, and an optional failing URL. Record worker concurrency, database pool settings, queue wait time, jobs per minute, p50/p95 completion time, database saturation, memory, disk, and S3 bandwidth for every run. No production load test should use real users or unapproved media.

---

## 🧪 Production mode

Render starts the web service with Gunicorn through the Docker configuration. The application runs with debug mode disabled. For local development, use `python app.py` and a local SQLite database.

---

## License

No license file is currently included. Add a license before distributing or accepting external contributions.

---

## ⚠️ Legal & Ethical Use

This tool is intended strictly for **personal and educational** purposes.
Please make sure you:

* Follow platform terms of service
* Do **not** download private or copyrighted material
* Use responsibly and ethically

---

**Made with ❤️ by Soham Roy Chowdhury**
