"""Render background worker for yt-dlp downloads and S3-compatible storage."""

import logging
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import boto3
import yt_dlp
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import func, select, text
from yt_dlp.utils import DownloadError

from config import (
    S3_ACCESS_KEY_ID,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_REGION,
    S3_SECRET_ACCESS_KEY,
    S3_PRESIGNED_URL_SECONDS,
    JOB_RETENTION_SECONDS,
    CLEANUP_INTERVAL_SECONDS,
    WORKER_JOB_TIMEOUT_SECONDS,
    MAX_DOWNLOAD_BYTES,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_TEMP_DISK_BYTES,
    MIN_FREE_DISK_BYTES,
    FFMPEG_PATH,
    WORKER_POLL_SECONDS,
    WORKER_CONCURRENCY,
    WORKER_MAX_ATTEMPTS,
    WORKER_RETRY_BACKOFF_SECONDS,
    WORKER_HEARTBEAT_SECONDS,
)
from database import SessionLocal, init_db
from models import ACTIVE_JOB_STATUSES, Job, ResourceLease, WorkerHeartbeat
from url_validation import has_public_host, validate_redirect_chain

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)
RESOURCE_LOCK_KEY = 917345
WORKER_ID = os.environ.get('WORKER_INSTANCE_ID', uuid.uuid4().hex)


def safe_error_text(error, limit=240):
    text = str(error)
    parts = text.split()
    redacted = []
    for part in parts:
        if part.startswith(('http://', 'https://')):
            parsed = urlsplit(part.rstrip('.,);'))
            part = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, '', ''))
        redacted.append(part)
    return ' '.join(redacted)[:limit]


def is_transient_error(error):
    if isinstance(error, (DownloadSizeExceeded, ValueError, TypeError)):
        return False
    if isinstance(error, ClientError):
        status = error.response.get(
            'ResponseMetadata', {}).get('HTTPStatusCode')
        return status in {403, 429, 500, 502, 503, 504}
    if isinstance(error, (BotoCoreError, ConnectionError, TimeoutError)):
        return True
    if isinstance(error, DownloadError):
        message = str(error).lower()
        return any(marker in message for marker in (
            'http error 403', 'http error 429', 'http error 500',
            'http error 502', 'http error 503', 'http error 504',
            'timed out', 'temporarily unavailable', 'connection reset',
            'unable to extract universal data',
            'unexpected response from webpage request',
        ))
    return False


def record_job_failure(job_id, error):
    should_retry = False
    attempts = 0
    with SessionLocal.begin() as session:
        job = session.get(Job, job_id)
        if not job:
            return None
        attempts = job.attempts
        should_retry = is_transient_error(error) and (
            attempts < WORKER_MAX_ATTEMPTS
        )
        if should_retry:
            job.status = 'queued'
            job.progress = 0
            job.error_message = 'Temporary problem. Retrying automatically.'
        else:
            job.status = 'failed'
            job.error_message = (
                str(error) if isinstance(error, DownloadSizeExceeded)
                else 'The worker could not process this download.'
            )
    if should_retry:
        return min(
            60, WORKER_RETRY_BACKOFF_SECONDS * 2 ** max(0, attempts - 1)
        )
    return None


def release_resource_lease(job_id):
    with SessionLocal.begin() as session:
        session.query(ResourceLease).filter_by(job_id=job_id).delete()


def write_heartbeat():
    with SessionLocal.begin() as session:
        heartbeat = session.get(WorkerHeartbeat, WORKER_ID)
        if heartbeat:
            heartbeat.heartbeat_at = datetime.now(timezone.utc)
        else:
            session.add(WorkerHeartbeat(
                worker_id=WORKER_ID,
                heartbeat_at=datetime.now(timezone.utc),
            ))


class DownloadSizeExceeded(RuntimeError):
    """Raised before downloading when media cannot fit the configured limit."""


def extractor_args_for_platform(platform):
    if platform == 'tiktok':
        return {
            'tiktok': {
                'app_info': ['musical_ly/35.1.3/2023501030/0'],
            },
        }
    return {}


def yt_dlp_options_for_platform(platform):
    if platform == 'tiktok':
        return {
            'extractor_args': extractor_args_for_platform(platform),
            'extractor_retries': 3,
        }
    return {}


def format_bytes(byte_count):
    value = float(byte_count)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{int(value)} B'
        value /= 1024


def format_size(item):
    return item.get('filesize') or item.get('filesize_approx')


def estimated_download_size(info, selected_format):
    formats = info.get('formats', [])
    if selected_format == 'best':
        candidates = [item for item in formats if item.get(
            'vcodec') not in (None, 'none')]
        if not candidates:
            return None
        selected = max(candidates, key=lambda item: item.get('quality') or -1)
    else:
        selected = next(
            (item for item in formats if str(
                item.get('format_id')) == str(selected_format)),
            None,
        )
        if not selected:
            return None

    video_size = format_size(selected)
    if selected.get('acodec') not in (None, 'none'):
        return video_size
    audio_formats = [
        item for item in formats
        if item.get('vcodec') == 'none' and item.get('acodec') not in (None, 'none')
    ]
    audio_size = max(
        (format_size(item) or 0 for item in audio_formats), default=0)
    if video_size is None:
        return None
    return video_size + audio_size


def ensure_download_size(info, selected_format):
    if selected_format != 'best' and not any(
        str(item.get('format_id')) == str(selected_format)
        for item in info.get('formats', [])
    ):
        raise RuntimeError('The selected format is no longer available.')
    estimated_size = estimated_download_size(info, selected_format)
    if estimated_size and estimated_size > MAX_DOWNLOAD_BYTES:
        raise DownloadSizeExceeded(
            f'This video is estimated at {format_bytes(estimated_size)}, '
            f'which is larger than the {format_bytes(MAX_DOWNLOAD_BYTES)} download limit.'
        )


def format_display_key(item):
    return (
        item.get('height'), item.get('ext'), item.get('resolution'),
        item.get('fps'),
    )


def format_quality_key(item):
    return (
        item.get('quality') or -1,
        item.get('tbr') or -1,
        format_size(item) or -1,
    )


def media_tool_directory():
    configured = Path(FFMPEG_PATH) if FFMPEG_PATH else None
    if configured:
        if configured.is_file():
            return configured.parent
        if (configured / 'ffmpeg.exe').is_file() or (configured / 'ffmpeg').is_file():
            return configured

    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        return Path(ffmpeg).parent

    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        packages = Path(local_app_data) / 'Microsoft' / 'WinGet' / 'Packages'
        for candidate in packages.glob('Gyan.FFmpeg*'):
            for executable in candidate.rglob('ffmpeg.exe'):
                return executable.parent
    return None


def require_media_tools():
    directory = media_tool_directory()
    if not directory:
        raise RuntimeError(
            'FFmpeg and FFprobe are required. Install FFmpeg or set FFMPEG_PATH.'
        )
    ffmpeg_name = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    ffprobe_name = 'ffprobe.exe' if os.name == 'nt' else 'ffprobe'
    ffmpeg = directory / ffmpeg_name
    ffprobe = directory / ffprobe_name
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise RuntimeError(
            'Both ffmpeg and ffprobe must be available in FFMPEG_PATH.'
        )
    return directory, ffmpeg, ffprobe


def storage_client():
    if not all((S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY)):
        raise RuntimeError(
            'S3_BUCKET, S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY are required')
    if not S3_ENDPOINT_URL and (
        S3_REGION == 'auto' or S3_REGION.startswith('your-')
    ):
        raise RuntimeError(
            'Set S3_REGION to your real AWS region, such as us-east-1')
    return boto3.client(
        's3',
        endpoint_url=S3_ENDPOINT_URL or None,
        region_name=S3_REGION or None,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
    )


def claim_job():
    with SessionLocal.begin() as session:
        stale_jobs = session.scalars(
            select(Job).where(
                Job.status.in_(ACTIVE_JOB_STATUSES),
                Job.updated_at < datetime.now(timezone.utc) - timedelta(
                    seconds=WORKER_JOB_TIMEOUT_SECONDS
                )
            )
        ).all()
        for stale_job in stale_jobs:
            stale_job.status = 'failed'
            stale_job.error_message = (
                'The worker timed out. Please start the download again.'
            )
            session.query(ResourceLease).filter_by(
                job_id=stale_job.id).delete()

        if session.get_bind().dialect.name == 'postgresql':
            session.execute(text(
                f'SELECT pg_advisory_xact_lock({RESOURCE_LOCK_KEY})'
            ))
        reserved_bytes = session.scalar(
            select(func.coalesce(func.sum(ResourceLease.reserved_bytes), 0))
        ) or 0
        active_leases = session.scalar(
            select(func.count()).select_from(ResourceLease)
        ) or 0
        reservation = MAX_DOWNLOAD_BYTES * 2
        free_bytes = shutil.disk_usage(tempfile.gettempdir()).free
        if (active_leases >= MAX_CONCURRENT_DOWNLOADS
            or reserved_bytes + reservation > MAX_TEMP_DISK_BYTES
                or free_bytes < reservation + MIN_FREE_DISK_BYTES):
            return None

        job = session.scalar(
            select(Job)
            .where(Job.status.in_(['queued', 'preview']))
            .order_by(Job.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return None
        session.add(ResourceLease(
            job_id=job.id,
            reserved_bytes=reservation,
        ))
        job.status = 'processing'
        job.attempts += 1
        return job.id


def cleanup_expired_jobs():
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=JOB_RETENTION_SECONDS
    )
    client = None
    with SessionLocal() as session:
        jobs = session.scalars(
            select(Job).where(
                Job.status.in_([
                    'ready',
                    'failed',
                    'preview',
                    'awaiting_format',
                    'queued',
                ]),
                Job.updated_at < cutoff,
            ).with_for_update(skip_locked=True).limit(50)
        ).all()
        for job in jobs:
            if job.status == 'ready' and job.filename:
                if client is None:
                    try:
                        client = storage_client()
                    except Exception:
                        logger.exception(
                            'Could not connect to storage for job %s', job.id)
                        continue
                object_key = f'jobs/{job.id}/{job.filename}'
                try:
                    client.delete_object(Bucket=S3_BUCKET, Key=object_key)
                except Exception:
                    logger.exception(
                        'Could not delete object for job %s', job.id)
                    continue
            try:
                session.delete(job)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception(
                    'Could not delete database record for job %s', job.id)


def cleanup_orphan_objects():
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=JOB_RETENTION_SECONDS
    )
    client = storage_client()
    paginator = client.get_paginator('list_objects_v2')
    with SessionLocal() as session:
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix='jobs/'):
            for item in page.get('Contents', []):
                object_key = item.get('Key', '')
                modified_at = item.get('LastModified')
                parts = object_key.split('/', 2)
                if len(parts) != 3 or not modified_at or modified_at >= cutoff:
                    continue
                job = session.get(Job, parts[1])
                if job and job.status == 'ready' and job.filename == parts[2]:
                    continue
                try:
                    client.delete_object(Bucket=S3_BUCKET, Key=object_key)
                except Exception:
                    logger.exception(
                        'Could not delete orphan object %s', object_key)


def process_job(job_id):
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if not job:
            return
        source_url = job.source_url
        platform = job.platform
        selected_format = job.selected_format

    work_dir = Path(tempfile.mkdtemp(prefix=f'vidzflow-{job_id}-'))
    job_stage = 'initializing'
    try:
        if not has_public_host(source_url):
            raise RuntimeError('The source host is not publicly reachable.')
        if not validate_redirect_chain(source_url):
            raise RuntimeError(
                'The source redirect chain is not publicly reachable.')
        media_tools, _, ffprobe = require_media_tools()
        output_template = str(work_dir / '%(title).120s-%(id)s.%(ext)s')
        last_progress_update = [0.0]
        downloaded_by_file = {}

        def update_job(status=None, progress=None, total_bytes=None):
            with SessionLocal.begin() as session:
                job = session.get(Job, job_id)
                if not job or job.status == 'failed':
                    return
                if status:
                    job.status = status
                if progress is not None:
                    job.progress = min(max(int(progress), 0), 99)
                if total_bytes is not None:
                    job.total_bytes = total_bytes

        job_stage = 'extracting metadata'
        update_job('extracting', 1)

        def progress_hook(event):
            if event.get('status') != 'downloading':
                return
            downloaded = event.get('downloaded_bytes', 0)
            file_key = event.get('filename') or event.get(
                'format_id') or 'unknown'
            downloaded_by_file[file_key] = max(
                downloaded_by_file.get(file_key, 0), downloaded)
            accumulated = sum(downloaded_by_file.values())
            if accumulated > MAX_DOWNLOAD_BYTES:
                raise DownloadSizeExceeded(
                    f'Download stopped after exceeding the '
                    f'{format_bytes(MAX_DOWNLOAD_BYTES)} limit.'
                )
            now = time.monotonic()
            if now - last_progress_update[0] < 1:
                return
            last_progress_update[0] = now
            total = event.get('total_bytes') or event.get(
                'total_bytes_estimate')
            progress = int(downloaded * 100 / total) if total else 0
            update_job('downloading', min(progress * 7 // 10, 70), total)

        with yt_dlp.YoutubeDL({
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'socket_timeout': WORKER_JOB_TIMEOUT_SECONDS,
            'ffmpeg_location': str(media_tools),
            **yt_dlp_options_for_platform(platform),
        }) as downloader:
            info = downloader.extract_info(source_url, download=False)

        if selected_format:
            ensure_download_size(info, selected_format)
        else:
            formats_by_display = {}
            for item in info.get('formats', []):
                if not item.get('format_id') or not item.get('vcodec') or item.get('vcodec') == 'none':
                    continue
                estimated_size = estimated_download_size(
                    info, item['format_id'])
                if estimated_size and estimated_size > MAX_DOWNLOAD_BYTES:
                    continue
                display_format = {
                    'id': str(item['format_id']),
                    'label': f"{item.get('height') or '?'}p {item.get('ext', '').upper()}",
                    'detail': (
                        f"{item.get('resolution') or 'video'} / "
                        f"{item.get('fps') or '?'} fps"
                        + (f" / {format_bytes(estimated_size)}" if estimated_size else '')
                    ),
                    '_source': item,
                }
                display_key = format_display_key(item)
                current = formats_by_display.get(display_key)
                if not current or format_quality_key(item) > format_quality_key(current['_source']):
                    formats_by_display[display_key] = display_format
            formats = sorted(
                formats_by_display.values(),
                key=lambda item: (
                    item['_source'].get('height') or -1,
                    item['_source'].get('fps') or -1,
                    item['_source'].get('quality') or -1,
                ),
                reverse=True,
            )
            for item in formats:
                item.pop('_source', None)
            if not formats:
                ensure_download_size(info, 'best')
                formats.append({'id': 'best', 'label': 'Best available',
                                'detail': 'Automatic playable MP4'})
            with SessionLocal.begin() as session:
                job = session.get(Job, job_id)
                if job:
                    job.title = info.get('title') or 'Video'
                    job.thumbnail_url = info.get('thumbnail')
                    job.available_formats = json.dumps(
                        formats)
                    job.status = 'awaiting_format'
                    job.progress = 0
            return

        selected_item = next(
            (item for item in info.get('formats', [])
             if str(item.get('format_id')) == str(selected_format)),
            None,
        )
        if selected_format == 'best':
            format_selector = 'bestvideo*+bestaudio'
        elif selected_item and selected_item.get('acodec') not in (None, 'none'):
            format_selector = selected_format
        else:
            format_selector = f'{selected_format}+bestaudio'
        options = {
            'format': format_selector,
            'merge_output_format': 'mp4',
            'outtmpl': output_template,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'retries': 2,
            'fragment_retries': 2,
            'max_filesize': MAX_DOWNLOAD_BYTES,
            'socket_timeout': WORKER_JOB_TIMEOUT_SECONDS,
            'ffmpeg_location': str(media_tools),
            **yt_dlp_options_for_platform(platform),
            'progress_hooks': [progress_hook],
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
        }
        job_stage = 'downloading selected format'
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(source_url, download=True)

        files = [
            path for path in work_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {'.mp4', '.webm', '.mkv'}
        ]
        if not files:
            raise RuntimeError('The worker did not produce a file')
        source_file = max(files, key=lambda path: path.stat().st_size)
        if source_file.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise RuntimeError('The downloaded file exceeds the size limit')
        normalized_file = work_dir / 'vidzflow-normalized.mp4'
        update_job('converting', 75)

        job_stage = 'converting with ffmpeg'
        normalize = subprocess.run(
            [
                str(media_tools / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')),
                '-y', '-hide_banner', '-loglevel', 'error',
                '-i', str(source_file), '-map', '0:v:0', '-map', '0:a:0?',
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
                '-movflags', '+faststart', str(normalized_file),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if normalize.returncode != 0 or not normalized_file.is_file():
            raise RuntimeError('FFmpeg could not create a playable MP4 file')
        source_file = normalized_file
        if source_file.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise RuntimeError('The converted file exceeds the size limit')
        update_job('checking', 90)
        job_stage = 'checking converted file'
        probe = subprocess.run(
            [
                str(ffprobe), '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name',
                '-of', 'default=nw=1:nk=1', str(source_file),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode != 0 or probe.stdout.strip() != 'h264':
            raise RuntimeError('The worker produced an unreadable video')
        filename = f'{uuid.uuid4().hex}-{source_file.name}'
        object_key = f'jobs/{job_id}/{filename}'
        job_stage = 'uploading to storage'
        client = storage_client()
        content_type = mimetypes.guess_type(source_file.name)[
            0] or 'application/octet-stream'
        update_job('uploading', 96)
        client.upload_file(
            str(source_file),
            S3_BUCKET,
            object_key,
            ExtraArgs={'ContentType': content_type},
        )
        try:
            file_url = client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': S3_BUCKET,
                    'Key': object_key,
                    'ResponseContentType': 'video/mp4',
                    'ResponseContentDisposition': (
                        f'attachment; filename="{filename}"'
                    ),
                },
                ExpiresIn=min(S3_PRESIGNED_URL_SECONDS, JOB_RETENTION_SECONDS),
            )
            with SessionLocal.begin() as session:
                job = session.get(Job, job_id)
                if not job or job.status == 'failed':
                    raise RuntimeError('The job record no longer exists')
                job.status = 'ready'
                job.progress = 100
                job.title = info.get('title') or job.title
                job.thumbnail_url = info.get('thumbnail') or job.thumbnail_url
                job.filename = filename
                job.file_url = file_url
                job.error_message = None
        except Exception:
            try:
                client.delete_object(Bucket=S3_BUCKET, Key=object_key)
            except Exception:
                logger.exception(
                    'Could not roll back object for job %s', job_id)
            raise
        logger.info('Completed job %s (%s)', job_id, platform)
    except Exception as error:
        logger.exception(
            'Job %s failed at stage %s (%s): %s',
            job_id, job_stage, platform, safe_error_text(error),
        )
        retry_delay = record_job_failure(job_id, error)
        if retry_delay is not None:
            release_resource_lease(job_id)
            logger.warning(
                'Job %s will retry after %.1f seconds (%s): %s',
                job_id, retry_delay, type(error).__name__,
                safe_error_text(error))
            time.sleep(retry_delay)
        else:
            logger.error(
                'Job %s marked failed after stage %s (%s).',
                job_id, job_stage, platform,
            )
    finally:
        with SessionLocal.begin() as session:
            session.query(ResourceLease).filter_by(job_id=job_id).delete()
        shutil.rmtree(work_dir, ignore_errors=True)


def run():
    init_db()
    next_cleanup = 0.0
    next_heartbeat = 0.0
    with ThreadPoolExecutor(max_workers=WORKER_CONCURRENCY) as executor:
        active_futures = set()
        while True:
            active_futures = {
                future for future in active_futures if not future.done()
            }
            now = time.monotonic()
            if now >= next_heartbeat:
                try:
                    write_heartbeat()
                except Exception:
                    logger.exception('Could not write worker heartbeat')
                next_heartbeat = now + WORKER_HEARTBEAT_SECONDS
            if now >= next_cleanup:
                try:
                    cleanup_expired_jobs()
                    cleanup_orphan_objects()
                except Exception:
                    logger.exception(
                        'Storage cleanup cycle failed; continuing worker loop')
                next_cleanup = now + CLEANUP_INTERVAL_SECONDS
            if len(active_futures) >= WORKER_CONCURRENCY:
                time.sleep(WORKER_POLL_SECONDS)
                continue
            job_id = claim_job()
            if job_id:
                active_futures.add(executor.submit(process_job, job_id))
            else:
                time.sleep(WORKER_POLL_SECONDS)


if __name__ == '__main__':
    run()
