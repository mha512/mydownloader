"""Lightweight ToxicDownloader web service.

The web service validates requests and coordinates jobs through the database.
The Render worker performs downloading and storage.
"""

import json
import hmac
import logging
import shutil
import tempfile
from datetime import datetime, timezone
import uuid

from flask import Flask, Response, abort, jsonify, render_template, request
from sqlalchemy import func, select, text
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from anonymous_jobs import issue_job_token, read_job_token
from config import (
    MAX_ACTIVE_JOBS,
    MAX_REQUEST_BYTES,
    MAX_URL_LENGTH,
    METRICS_AUTH_TOKEN,
    RATE_LIMIT_CAPACITY,
    RATE_LIMIT_REFILL_SECONDS,
    SECRET_KEY,
)
from database import SessionLocal, engine, init_db
from models import ACTIVE_JOB_STATUSES, Job, ResourceLease, WorkerHeartbeat
from platforms import PLATFORM_CONFIG, PLATFORM_ORDER
from url_validation import detect_platform, is_valid_format_id
from rate_limit import DatabaseRateLimiter


SUPPORTED_PLATFORMS = frozenset(PLATFORM_ORDER)
MAX_BULK_URLS = 10
CAPACITY_LOCK_KEY = 917342
logger = logging.getLogger(__name__)


def lock_capacity(session):
    if engine.dialect.name == 'postgresql':
        session.execute(text(
            f'SELECT pg_advisory_xact_lock({CAPACITY_LOCK_KEY})'
        ))


def active_job_count(session):
    return session.scalar(
        select(func.count()).select_from(Job).where(
            Job.status.in_(ACTIVE_JOB_STATUSES)
        )
    ) or 0


def create_app(rate_limiter=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=SECRET_KEY,
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
        METRICS_AUTH_TOKEN=METRICS_AUTH_TOKEN,
    )
    init_db()
    limiter = rate_limiter or DatabaseRateLimiter(
        SessionLocal, RATE_LIMIT_CAPACITY, RATE_LIMIT_REFILL_SECONDS)

    def check_rate_limit():
        allowed, retry_after = limiter.allow(request.remote_addr or 'unknown')
        if allowed:
            return None
        response = jsonify({
            'status': 'error',
            'message': 'Too many download requests. Please try again shortly.',
        })
        response.status_code = 429
        response.headers['Retry-After'] = str(retry_after)
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_error):
        return jsonify({
            'status': 'error',
            'message': 'The request is too large. Please submit a smaller URL or request.',
        }), 413

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify({
            'status': 'error',
            'message': error.description or 'The request could not be completed.',
        }), error.code

    @app.errorhandler(Exception)
    def unexpected_error(error):
        logger.exception('Unhandled web request error', exc_info=error)
        return jsonify({
            'status': 'error',
            'message': 'The service could not complete the request.',
        }), 500

    @app.after_request
    def add_security_headers(response):
        if request.path == '/':
            response.headers['Cache-Control'] = 'no-store, max-age=0'
        elif request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'no-referrer')
        response.headers.setdefault(
            'Permissions-Policy', 'camera=(), microphone=(), geolocation=()'
        )
        return response

    @app.get('/')
    def index():
        return render_template(
            'pages/home/index.html',
            platforms=[PLATFORM_CONFIG[key] | {
                'slug': key} for key in PLATFORM_ORDER],
        )

    @app.get('/<platform_slug>-video-downloader')
    def platform_page(platform_slug):
        if platform_slug not in PLATFORM_CONFIG:
            abort(404)
        platform = PLATFORM_CONFIG[platform_slug] | {'slug': platform_slug}
        return render_template(
            f'pages/platforms/{platform_slug}.html',
            platform=platform,
            platforms=[PLATFORM_CONFIG[key] | {
                'slug': key} for key in PLATFORM_ORDER],
        )

    @app.get('/privacy')
    def privacy_page():
        return render_template('pages/legal/privacy.html')

    @app.get('/terms')
    def terms_page():
        return render_template('pages/legal/terms.html')

    @app.get('/how-to-download-public-videos')
    def how_to_download_public_videos():
        return render_template(
            'pages/information/how-to-download-public-videos.html',
            platforms=[PLATFORM_CONFIG[key] | {
                'slug': key} for key in PLATFORM_ORDER],
        )

    @app.get('/video-download-quality-and-formats')
    def video_download_quality_and_formats():
        return render_template(
            'pages/information/video-download-quality-and-formats.html',
            platforms=[PLATFORM_CONFIG[key] | {
                'slug': key} for key in PLATFORM_ORDER],
        )

    @app.get('/robots.txt')
    def robots_txt():
        site_root = request.url_root.rstrip('/')
        body = '\n'.join((
            'User-agent: *',
            'Allow: /',
            'Disallow: /health',
            'Disallow: /metrics',
            'Disallow: /supported-platforms',
            f'Sitemap: {site_root}/sitemap.xml',
            '',
        ))
        return Response(body, mimetype='text/plain')

    @app.get('/sitemap.xml')
    def sitemap_xml():
        site_root = request.url_root.rstrip('/')
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'<url><loc>{site_root}/</loc></url>'
            + ''.join(
                f'<url><loc>{site_root}/{key}-video-downloader</loc></url>'
                for key in PLATFORM_ORDER
            )
            + f'<url><loc>{site_root}/how-to-download-public-videos</loc></url>'
            + f'<url><loc>{site_root}/video-download-quality-and-formats</loc></url>'
            '</urlset>'
        )
        return Response(body, mimetype='application/xml')

    @app.get('/supported-platforms')
    def supported_platforms():
        return jsonify({
            'platforms': sorted(SUPPORTED_PLATFORMS),
        })

    @app.get('/health')
    def health():
        with SessionLocal() as session:
            session.execute(text('SELECT 1'))
        return jsonify({'status': 'ok'})

    @app.get('/metrics')
    def metrics():
        authorization = request.headers.get('Authorization', '')
        expected = app.config['METRICS_AUTH_TOKEN']
        if (not expected or not hmac.compare_digest(
                authorization, f'Bearer {expected}')):
            response = jsonify({
                'status': 'error',
                'message': 'Metrics authorization is required.',
            })
            response.status_code = 401
            response.headers['WWW-Authenticate'] = 'Bearer'
            return response
        with SessionLocal() as session:
            state_rows = session.execute(
                select(Job.status, func.count()).group_by(Job.status)
            ).all()
            lease_count = session.scalar(
                select(func.count()).select_from(ResourceLease)
            ) or 0
            heartbeat_rows = session.scalars(
                select(WorkerHeartbeat.heartbeat_at)
            ).all()
        disk = shutil.disk_usage(tempfile.gettempdir())
        now = datetime.now(timezone.utc)
        heartbeat_ages = [
            max(0, int((now - (
                heartbeat.replace(tzinfo=timezone.utc)
                if heartbeat.tzinfo is None else heartbeat
            )).total_seconds()))
            for heartbeat in heartbeat_rows
        ]
        return jsonify({
            'status': 'ok',
            'jobs_by_state': dict(state_rows),
            'active_leases': lease_count,
            'disk': {
                'total_bytes': disk.total,
                'free_bytes': disk.free,
                'used_bytes': disk.used,
            },
            'worker_heartbeat_age_seconds': min(heartbeat_ages)
            if heartbeat_ages else None,
        })

    @app.post('/download')
    def download():
        rate_error = check_rate_limit()
        if rate_error:
            return rate_error
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'status': 'error', 'message': 'Invalid request.'}), 400

        url = str(data.get('url', '')).strip()
        if len(url) > MAX_URL_LENGTH:
            return jsonify({'status': 'error', 'message': 'URL is too long.'}), 413
        platform = detect_platform(url)
        if platform not in SUPPORTED_PLATFORMS:
            return jsonify({
                'status': 'error',
                'message': 'Only supported HTTPS media URLs are accepted.',
            }), 400
        format_value = data.get('format')
        if format_value is not None and not is_valid_format_id(format_value):
            return jsonify({
                'status': 'error',
                'message': 'The format must be a single valid format ID.',
            }), 400

        with SessionLocal.begin() as session:
            lock_capacity(session)
            active_jobs = active_job_count(session)
            if active_jobs >= MAX_ACTIVE_JOBS:
                return jsonify({
                    'status': 'error',
                    'message': 'The service is busy. Please try again shortly.',
                }), 429
            job_id = uuid.uuid4().hex
            session.add(Job(
                id=job_id,
                source_url=url,
                platform=platform,
                status='queued',
                selected_format=(str(format_value).strip()
                                 if format_value is not None else None),
            ))
        return jsonify({
            'status': 'queued',
            'job_id': job_id,
            'job_token': issue_job_token(job_id),
            'platform': platform,
        }), 202

    @app.get('/jobs/<job_token>')
    def job_status(job_token):
        job_id = read_job_token(job_token)
        if not job_id:
            return jsonify({'status': 'error', 'message': 'Invalid or expired job token.'}), 404
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if not job:
                return jsonify({'status': 'error', 'message': 'Job not found.'}), 404
            result = {
                'job_id': job.id,
                'status': job.status,
                'filename': job.filename,
                'title': job.title,
                'thumbnail': job.thumbnail_url,
                'progress': job.progress,
                'total_bytes': job.total_bytes,
            }
            if job.available_formats:
                result['formats'] = json.loads(job.available_formats)
            if job.file_url:
                result['file_url'] = job.file_url
            if job.error_message:
                result['message'] = job.error_message
            return jsonify(result)

    @app.post('/preview')
    def preview():
        rate_error = check_rate_limit()
        if rate_error:
            return rate_error
        data = request.get_json(silent=True)
        url = str(data.get('url', '')).strip(
        ) if isinstance(data, dict) else ''
        platform = detect_platform(url)
        if len(url) > MAX_URL_LENGTH or platform not in SUPPORTED_PLATFORMS:
            return jsonify({'status': 'error', 'message': 'Only supported HTTPS media URLs are accepted.'}), 400
        with SessionLocal.begin() as session:
            lock_capacity(session)
            active_jobs = active_job_count(session)
            if active_jobs >= MAX_ACTIVE_JOBS:
                return jsonify({'status': 'error', 'message': 'The service is busy. Please try again shortly.'}), 429
            job_id = uuid.uuid4().hex
            session.add(Job(id=job_id, source_url=url,
                        platform=platform, status='preview'))
        return jsonify({
            'status': 'queued',
            'job_id': job_id,
            'job_token': issue_job_token(job_id),
            'platform': platform,
        }), 202

    @app.post('/download-quality')
    def download_quality():
        data = request.get_json(silent=True)
        token = data.get('job_token') if isinstance(data, dict) else None
        format_id = str(data.get('quality', '')).strip(
        ) if isinstance(data, dict) else ''
        job_id = read_job_token(token)
        if not job_id or not format_id or len(format_id) > 64:
            return jsonify({'status': 'error', 'message': 'A valid job and format are required.'}), 400
        with SessionLocal.begin() as session:
            job = session.get(Job, job_id)
            if not job or job.status != 'awaiting_format':
                return jsonify({'status': 'error', 'message': 'The preview is not ready.'}), 409
            formats = json.loads(job.available_formats or '[]')
            if not any(str(item.get('id')) == format_id for item in formats):
                return jsonify({'status': 'error', 'message': 'That format is not available.'}), 400
            job.selected_format = format_id
            job.status = 'queued'
        return jsonify({'status': 'queued', 'job_token': token}), 202

    @app.post('/bulk-download')
    def bulk_download():
        rate_error = check_rate_limit()
        if rate_error:
            return rate_error
        data = request.get_json(silent=True)
        urls = data.get('urls') if isinstance(data, dict) else None
        if not isinstance(urls, list) or not urls:
            return jsonify({'status': 'error', 'message': 'A URLs list is required.'}), 400
        if len(urls) > MAX_BULK_URLS:
            return jsonify({
                'status': 'error',
                'message': f'At most {MAX_BULK_URLS} URLs may be submitted at once.',
            }), 413

        results = []
        with SessionLocal.begin() as session:
            lock_capacity(session)
            active_jobs = active_job_count(session)
            accepted_jobs = 0
            for raw_url in urls:
                url = str(raw_url).strip()
                if len(url) > MAX_URL_LENGTH:
                    results.append({
                        'status': 'error',
                        'url': url,
                        'message': 'URL is too long.',
                    })
                    continue
                platform = detect_platform(url)
                if platform not in SUPPORTED_PLATFORMS:
                    results.append({
                        'status': 'error',
                        'url': url,
                        'message': 'Unsupported or invalid HTTPS media URL.',
                    })
                    continue
                job_id = uuid.uuid4().hex
                if active_jobs + accepted_jobs >= MAX_ACTIVE_JOBS:
                    results.append({
                        'status': 'error',
                        'url': url,
                        'message': 'The service is busy. Please try again shortly.',
                    })
                    continue
                session.add(Job(id=job_id, source_url=url, platform=platform))
                accepted_jobs += 1
                results.append({
                    'status': 'queued',
                    'job_id': job_id,
                    'job_token': issue_job_token(job_id),
                    'url': url,
                    'platform': platform,
                })
        return jsonify({
            'status': 'accepted',
            'message': f'Accepted {len(results)} URLs.',
            'results': results,
        }), 202

    return app


app = create_app()


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
