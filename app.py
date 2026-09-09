"""Lightweight ToxicDownloader web service.

The web service validates requests and coordinates jobs through the database.
The Render worker performs downloading and storage.
"""

import hashlib
import json
import hmac
import logging
import shutil
import tempfile
from datetime import datetime, timezone
import uuid
from functools import wraps

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func, or_, select, text
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from anonymous_jobs import issue_job_token, read_job_token
from config import (
    ADMIN_PASSWORD,
    ADMIN_SESSION_TTL,
    ADMIN_USERNAME,
    MAX_ACTIVE_JOBS,
    MAX_REQUEST_BYTES,
    MAX_URL_LENGTH,
    METRICS_AUTH_TOKEN,
    RATE_LIMIT_CAPACITY,
    RATE_LIMIT_REFILL_SECONDS,
    SECRET_KEY,
)
from database import SessionLocal, engine, init_db
from models import (
    ACTIVE_JOB_STATUSES,
    AdminAuditLog,
    Job,
    ResourceLease,
    WorkerHeartbeat,
)
from platforms import PLATFORM_CONFIG, PLATFORM_ORDER
from url_validation import detect_platform, is_valid_format_id
from rate_limit import DatabaseRateLimiter


SUPPORTED_PLATFORMS = frozenset(PLATFORM_ORDER)
MAX_BULK_URLS = 10
CAPACITY_LOCK_KEY = 917342
logger = logging.getLogger(__name__)


def hash_password(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def require_admin_access(required_role='super_admin'):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get('is_admin'):
                return redirect(url_for('admin_login'))
            role = session.get('admin_role', 'super_admin')
            if required_role != 'read_only' and role == 'read_only':
                flash('This admin role cannot perform that action.', 'warning')
                return redirect(url_for('admin_dashboard'))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def record_admin_action(actor, action, entity_type='system', entity_id=None, details=None):
    with SessionLocal.begin() as db_session:
        db_session.add(AdminAuditLog(
            id=uuid.uuid4().hex,
            actor=actor or 'system',
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=json.dumps(details, default=str) if details else None,
        ))


def admin_required(view):
    return require_admin_access()(view)


def admin_job_summary(session):
    state_rows = session.execute(
        select(Job.status, func.count()).group_by(Job.status)
    ).all()
    jobs_by_state = {status: count for status, count in state_rows}
    total_jobs = sum(jobs_by_state.values())
    failed_jobs = jobs_by_state.get('failed', 0)
    ready_jobs = jobs_by_state.get('ready', 0)
    queued_jobs = jobs_by_state.get('queued', 0)
    active_jobs = session.scalar(
        select(func.count()).select_from(Job).where(
            Job.status.in_(ACTIVE_JOB_STATUSES)
        )
    ) or 0
    recent_jobs = session.scalars(
        select(Job).order_by(Job.created_at.desc()).limit(8)
    ).all()
    heartbeat_rows = session.scalars(
        select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc())
    ).all()
    worker_count = len(heartbeat_rows)
    now = datetime.now(timezone.utc)
    stale_workers = sum(
        1 for row in heartbeat_rows
        if (now - row.heartbeat_at.replace(tzinfo=timezone.utc) if row.heartbeat_at.tzinfo is None else now - row.heartbeat_at).total_seconds() > 90
    )
    return {
        'jobs_by_state': jobs_by_state,
        'total_jobs': total_jobs,
        'failed_jobs': failed_jobs,
        'ready_jobs': ready_jobs,
        'queued_jobs': queued_jobs,
        'active_jobs': active_jobs,
        'recent_jobs': recent_jobs,
        'worker_count': worker_count,
        'stale_workers': stale_workers,
    }


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
        SESSION_COOKIE_SECURE=True if SECRET_KEY != 'development-only-change-this-secret' else False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
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

    @app.get('/admin/login')
    def admin_login():
        if session.get('is_admin'):
            return redirect(url_for('admin_dashboard'))
        return render_template('admin/login.html')

    @app.post('/admin/login')
    def admin_sign_in():
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session.clear()
            session['is_admin'] = True
            session['admin_username'] = username
            session['admin_role'] = 'super_admin'
            session.permanent = True
            app.permanent_session_lifetime = ADMIN_SESSION_TTL
            record_admin_action(username, 'login', 'admin',
                                username, {'status': 'success'})
            return redirect(url_for('admin_dashboard'))
        flash('Invalid admin username or password.', 'error')
        return redirect(url_for('admin_login'))

    @app.get('/admin/logout')
    @admin_required
    def admin_logout():
        actor = session.get('admin_username', 'system')
        record_admin_action(actor, 'logout', 'admin',
                            actor, {'status': 'success'})
        session.clear()
        return redirect(url_for('admin_login'))

    @app.get('/admin')
    @admin_required
    def admin_dashboard():
        with SessionLocal() as session:
            summary = admin_job_summary(session)
            metrics = session.execute(
                select(Job.status, func.count()).group_by(Job.status)
            ).all()
            recent_jobs = summary['recent_jobs']
        return render_template(
            'admin/dashboard.html',
            summary=summary,
            metrics=dict(metrics),
            recent_jobs=recent_jobs,
        )

    @app.get('/admin/jobs')
    @admin_required
    def admin_jobs():
        page = max(1, int(request.args.get('page', '1')))
        status_filter = request.args.get('status', 'all')
        search_term = request.args.get('q', '').strip()
        with SessionLocal() as session:
            query = select(Job)
            if status_filter != 'all':
                query = query.where(Job.status == status_filter)
            if search_term:
                pattern = f'%{search_term}%'
                query = query.where(
                    or_(
                        Job.id.like(pattern),
                        Job.source_url.like(pattern),
                        Job.platform.like(pattern),
                        Job.filename.like(pattern),
                    )
                )
            total = session.scalar(
                select(func.count()).select_from(query.subquery())) or 0
            jobs = session.scalars(
                query.order_by(Job.created_at.desc()).offset(
                    (page - 1) * 25).limit(25)
            ).all()
            status_counts = dict(session.execute(
                select(Job.status, func.count()).group_by(Job.status)
            ).all())
        return render_template(
            'admin/jobs.html',
            jobs=jobs,
            page=page,
            total=total,
            status_filter=status_filter,
            search_term=search_term,
            status_counts=status_counts,
        )

    @app.get('/admin/jobs/<job_id>')
    @admin_required
    def admin_job_detail(job_id):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if not job:
                abort(404)
            actions = {
                'retry': job.status in {'failed', 'queued'} or job.attempts < 1,
                'cancel': job.status != 'failed',
            }
        return render_template('admin/job_detail.html', job=job, actions=actions)

    @app.post('/admin/jobs/<job_id>/retry')
    @require_admin_access('super_admin')
    def admin_job_retry(job_id):
        with SessionLocal.begin() as session:
            job = session.get(Job, job_id)
            if job:
                job.status = 'queued'
                job.error_message = None
                job.progress = 0
                job.updated_at = datetime.now(timezone.utc)
        record_admin_action(session.get('admin_username', 'system'),
                            'retry_job', 'job', job_id, {'status': 'queued'})
        flash('The job was reset for retry.', 'success')
        return redirect(url_for('admin_job_detail', job_id=job_id))

    @app.post('/admin/jobs/<job_id>/cancel')
    @require_admin_access('super_admin')
    def admin_job_cancel(job_id):
        with SessionLocal.begin() as session:
            job = session.get(Job, job_id)
            if job:
                job.status = 'failed'
                job.error_message = 'Cancelled by admin.'
                job.updated_at = datetime.now(timezone.utc)
        record_admin_action(session.get('admin_username', 'system'),
                            'cancel_job', 'job', job_id, {'status': 'failed'})
        flash('The job was marked failed by admin action.', 'warning')
        return redirect(url_for('admin_job_detail', job_id=job_id))

    @app.post('/admin/jobs/<job_id>/delete')
    @require_admin_access('super_admin')
    def admin_job_delete(job_id):
        with SessionLocal.begin() as session:
            job = session.get(Job, job_id)
            if job:
                session.delete(job)
        record_admin_action(session.get('admin_username', 'system'),
                            'delete_job', 'job', job_id, {'action': 'delete'})
        flash('The job was deleted from the admin list.', 'success')
        return redirect(url_for('admin_jobs'))

    @app.get('/admin/workers')
    @admin_required
    def admin_workers():
        with SessionLocal() as session:
            workers = session.scalars(
                select(WorkerHeartbeat).order_by(
                    WorkerHeartbeat.heartbeat_at.desc())
            ).all()
            now = datetime.now(timezone.utc)
            health = []
            for worker in workers:
                age = (now - worker.heartbeat_at.replace(tzinfo=timezone.utc)
                       if worker.heartbeat_at.tzinfo is None else now - worker.heartbeat_at).total_seconds()
                health.append({
                    'worker': worker,
                    'age_seconds': max(0, int(age)),
                    'is_stale': age > 90,
                })
        return render_template('admin/workers.html', workers=health)

    @app.get('/admin/settings')
    @admin_required
    def admin_settings():
        settings = {
            'MAX_ACTIVE_JOBS': MAX_ACTIVE_JOBS,
            'RATE_LIMIT_CAPACITY': RATE_LIMIT_CAPACITY,
            'RATE_LIMIT_REFILL_SECONDS': RATE_LIMIT_REFILL_SECONDS,
            'JOB_RETENTION_SECONDS': 1800,
            'WORKER_CONCURRENCY': 1,
            'MAX_CONCURRENT_DOWNLOADS': 2,
        }
        return render_template('admin/settings.html', settings=settings)

    @app.get('/admin/alerts')
    @admin_required
    def admin_alerts():
        with SessionLocal() as session:
            jobs = session.scalars(
                select(Job).where(Job.status.in_({'failed', 'queued'})).order_by(
                    Job.updated_at.desc()).limit(20)
            ).all()
        return render_template('admin/alerts.html', jobs=jobs)

    @app.get('/admin/audit')
    @admin_required
    def admin_audit():
        with SessionLocal() as session:
            audit_entries = session.scalars(
                select(AdminAuditLog).order_by(
                    AdminAuditLog.created_at.desc()).limit(50)
            ).all()
        return render_template('admin/audit.html', audit_entries=audit_entries)

    @app.get('/admin/reports')
    @admin_required
    def admin_reports():
        with SessionLocal() as session:
            state_rows = session.execute(
                select(Job.status, func.count()).group_by(Job.status)
            ).all()
            recent = session.scalars(
                select(Job).order_by(Job.created_at.desc()).limit(10)
            ).all()
        return render_template(
            'admin/reports.html',
            state_rows=state_rows,
            recent=recent,
            total_jobs=sum(count for _, count in state_rows),
        )

    @app.post('/admin/cleanup')
    @admin_required
    def admin_cleanup():
        with SessionLocal.begin() as session:
            cutoff = datetime.now(timezone.utc)
            old_jobs = session.execute(
                select(Job).where(Job.updated_at < cutoff)
            ).scalars().all()
            for job in old_jobs:
                if job.status in {'failed', 'ready'}:
                    session.delete(job)
            stale_heartbeats = session.scalars(
                select(WorkerHeartbeat).where(
                    WorkerHeartbeat.heartbeat_at < cutoff)
            ).all()
            for heartbeat in stale_heartbeats:
                session.delete(heartbeat)
        flash('Cleanup completed for stale records.', 'success')
        return redirect(url_for('admin_dashboard'))

    @app.post('/admin/clear-rate-limits')
    @admin_required
    def admin_clear_rate_limits():
        from rate_limit import RateLimitBucket
        with SessionLocal.begin() as session:
            session.execute(text('DELETE FROM rate_limit_buckets'))
        flash('Rate-limit buckets were reset.', 'success')
        return redirect(url_for('admin_dashboard'))

    @app.post('/admin/refresh')
    @admin_required
    def admin_refresh():
        flash('Admin dashboard refreshed.', 'success')
        return redirect(url_for('admin_dashboard'))

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
