const $ = (id) => document.getElementById(id);
const STATUS_COPY = {
  preview: 'Preparing preview',
  queued: 'Preparing preview',
  processing: 'Reading available formats',
  extracting: 'Reading available formats',
  awaiting_format: 'Choose a download quality',
  downloading: 'Downloading selected quality',
  converting: 'Preparing the MP4 file',
  checking: 'Verifying the file',
  uploading: 'Finalizing download link',
  ready: 'Download ready',
  failed: 'Could not complete',
};
const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 5 * 60 * 1000;
let currentJobToken = null;
let lastSubmittedUrl = '';
let pollStartedAt = 0;
let pollTimer = null;
let fetchInFlight = false;
let downloadInFlight = false;
let downloadStarted = false;
let lastAnnouncedState = '';

function escapeHtml(value) {
  const d = document.createElement('div');
  d.textContent = String(value ?? '');
  return d.innerHTML;
}

function showStatus(message, type, state = message) {
  const element = $('status');
  if (!element || state === lastAnnouncedState) return;
  lastAnnouncedState = state;
  element.innerHTML = `<div class="status-box ${type}">${escapeHtml(message)}</div>`;
}

function statusCopy(status) {
  return STATUS_COPY[status] || 'Preparing preview';
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  const unitIndex = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** unitIndex)).toFixed(unitIndex ? 1 : 0)} ${units[unitIndex]}`;
}

function updateActionState() {
  const fetchButton = $('fetch-button');
  const downloadButton = $('quality-download');
  const hasFile = Boolean(downloadButton?.dataset.fileUrl);
  const hasQuality = Boolean(selectedQuality());
  if (fetchButton) fetchButton.disabled = fetchInFlight || Boolean(currentJobToken);
  if (downloadButton) {
    downloadButton.disabled = downloadInFlight || (!hasFile && (!hasQuality || downloadStarted));
    downloadButton.textContent = hasFile ? 'Download Ready File' : 'Download Selected Format';
  }
}

function clearPollTimer() {
  if (pollTimer) window.clearTimeout(pollTimer);
  pollTimer = null;
}

function showResult(title, message) {
  const result = $('download-result');
  if (result) result.classList.add('show');
  if ($('download-result-title')) $('download-result-title').textContent = title;
  if ($('download-result-text')) $('download-result-text').textContent = message;
}

function hideRecovery() {
  const actions = $('recovery-actions');
  if (actions) actions.classList.add('is-hidden');
}

function showRecovery(mode) {
  const actions = $('recovery-actions');
  const retryButton = $('retry-button');
  if (!actions || !retryButton) return;
  retryButton.dataset.mode = mode;
  const retryLabel = mode === 'failed' ? 'Retry job' : 'Retry status check';
  retryButton.textContent = retryLabel;
  retryButton.setAttribute('aria-label', retryLabel);
  $('cancel-button')?.setAttribute('aria-label', 'Cancel current job');
  actions.classList.remove('is-hidden');
}

function renderProgress(result) {
  const status = result.status;
  const progress = Number(result.progress);
  const totalBytes = formatBytes(result.total_bytes);
  const isDownloadPhase = ['downloading', 'converting', 'checking', 'uploading', 'ready'].includes(status);
  const hasProgress = isDownloadPhase && Number.isFinite(progress)
    && (progress > 0 || Number(result.total_bytes) > 0 || status === 'ready');
  const progressElement = $('download-progress');
  const progressBar = $('download-progress-bar');
  const progressLabel = $('download-progress-label');
  const progressPercent = $('download-progress-percent');
  if (!progressElement || !progressBar || !progressLabel || !progressPercent) return;

  if (!isDownloadPhase || status === 'ready') {
    progressElement.classList.remove('show', 'indeterminate');
    progressElement.removeAttribute('aria-valuenow');
    return;
  }

  progressElement.classList.add('show');
  if (hasProgress) {
    const safeProgress = Math.min(Math.max(progress, 0), 100);
    progressElement.classList.remove('indeterminate');
    progressBar.style.width = `${safeProgress}%`;
    progressElement.setAttribute('aria-valuenow', String(safeProgress));
    progressElement.setAttribute('aria-valuetext', `${safeProgress}% complete`);
    progressLabel.textContent = totalBytes
      ? `Preparing a ${totalBytes} file...`
      : 'Preparing your file...';
    progressPercent.textContent = `${safeProgress}%`;
  } else {
    progressElement.classList.add('indeterminate');
    progressBar.style.width = '35%';
    progressElement.removeAttribute('aria-valuenow');
    progressElement.setAttribute('aria-valuetext', 'Progress is not available yet');
    progressLabel.textContent = 'Progress is not available yet...';
    progressPercent.textContent = '--';
  }
}

function renderThumbnail(result) {
  if (!result.thumbnail) return;
  const thumb = $('preview-thumb');
  const placeholder = $('thumb-placeholder');
  if (thumb) {
    thumb.src = result.thumbnail;
    thumb.alt = `${result.title || 'Video'} thumbnail`;
    thumb.classList.remove('is-hidden');
  }
  if (placeholder) placeholder.style.display = 'none';
}

function renderFormats(formats) {
  const qualities = $('qualities');
  if (!qualities || !Array.isArray(formats) || !formats.length) return;
  qualities.innerHTML = formats.map((item) => {
    const label = item.label || item.id;
    const detail = item.detail || 'Available format';
    return `
        <button class="quality" type="button" data-quality="${escapeHtml(item.id)}">
          <strong>${escapeHtml(label)}</strong>
          <small>${escapeHtml(detail)}</small>
        </button>
      `;
  }).join('');
  document.querySelectorAll('.quality').forEach((button) => {
    button.addEventListener('click', () => selectQuality(button));
  });
}

function resetPreviewOnly() {
  clearPollTimer();
  const preview = $('preview');
  const thumb = $('preview-thumb');
  const placeholder = $('thumb-placeholder');
  if (preview) preview.classList.remove('show');
  if (thumb) { thumb.classList.add('is-hidden'); thumb.src = ''; }
  if (placeholder) placeholder.style.display = 'grid';
  if ($('qualities')) $('qualities').innerHTML = '';
  if ($('download-result')) $('download-result').classList.remove('show');
  if ($('download-progress')) {
    $('download-progress').classList.remove('show', 'indeterminate');
    $('download-progress').removeAttribute('aria-valuenow');
    $('download-progress').setAttribute('aria-valuetext', 'Progress is not available yet');
  }
  if ($('download-progress-bar')) $('download-progress-bar').style.width = '0%';
  if ($('download-progress-percent')) $('download-progress-percent').textContent = '--';
  if ($('download-progress-label')) $('download-progress-label').textContent = 'Preparing...';
  if ($('qualities')) $('qualities').innerHTML = '';
  if ($('quality-download')) {
    $('quality-download').removeAttribute('data-file-url');
    $('quality-download').textContent = 'Download Selected Format';
  }
  hideRecovery();
  fetchInFlight = false;
  downloadInFlight = false;
  downloadStarted = false;
  updateActionState();
}

async function readResponse(response) {
  const text = await response.text();
  try { return text ? JSON.parse(text) : {}; }
  catch (_) { return { message: 'The service returned an unexpected response.' }; }
}

function selectQuality(button) {
  document.querySelectorAll('.quality').forEach((item) => item.classList.remove('active'));
  button.classList.add('active');
  if (!downloadStarted && !downloadInFlight) updateActionState();
}

function selectedQuality() {
  const active = document.querySelector('.quality.active');
  return active ? active.dataset.quality : null;
}

async function fetchMedia() {
  if (fetchInFlight || currentJobToken) return;
  const input = $('single-url');
  const url = input?.value.trim();
  if (!url || !input?.checkValidity()) {
    input?.setAttribute('aria-invalid', 'true');
    showStatus('Please enter a valid URL.', 'error', 'invalid-url');
    input?.focus();
    return;
  }
  input.setAttribute('aria-invalid', 'false');

  lastSubmittedUrl = url;
  showStatus('Sending your job to the download service...', 'info', 'job-started');
  resetPreviewOnly();
  fetchInFlight = true;
  updateActionState();
  const button = $('fetch-button');
  const spinner = $('fetch-spinner');
  if (spinner) spinner.classList.remove('is-hidden');

  try {
    const response = await fetch('/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });

    const result = await readResponse(response);
    if (!response.ok || !result.job_token) {
      throw new Error(result.message || 'Could not start the preview.');
    }

    currentJobToken = result.job_token;
    fetchInFlight = false;
    if ($('preview')) $('preview').classList.add('show');
    if ($('preview-title')) $('preview-title').textContent = statusCopy(result.status || 'preview');
    if ($('preview-meta')) $('preview-meta').textContent = statusCopy(result.status || 'preview');
    if ($('qualities')) $('qualities').innerHTML = '<div class="status-box info">Preparing preview...</div>';
    updateActionState();
    startPolling(result.job_token);
  } catch (error) {
    showStatus(error.message || 'The request could not be processed.', 'error');
    currentJobToken = null;
  } finally {
    fetchInFlight = false;
    updateActionState();
    if (spinner) spinner.classList.add('is-hidden');
  }
}

function startPolling(token) {
  clearPollTimer();
  pollStartedAt = Date.now();
  hideRecovery();
  pollJob(token);
}

function schedulePoll(token) {
  clearPollTimer();
  pollTimer = window.setTimeout(() => pollJob(token), POLL_INTERVAL_MS);
}

function showTimeout() {
  if ($('quality-title')) $('quality-title').textContent = 'Download status';
  showResult('This is taking longer than usual', 'The job is still not complete. Retry the status check or cancel and start again.');
  showRecovery('polling');
  showStatus('This is taking longer than usual. You can retry checking or cancel this job.', 'info', 'timeout');
}

function showFailure(reason) {
  clearPollTimer();
  const message = reason || 'The service did not provide a failure reason.';
  if ($('quality-title')) $('quality-title').textContent = 'Download status';
  showResult('Could not complete', message);
  showRecovery('failed');
  showStatus(`Could not complete: ${message}`, 'error', 'failed');
  if ($('preview-meta')) $('preview-meta').textContent = 'Could not complete';
  updateActionState();
}

function updateJob(result) {
  const label = statusCopy(result.status);
  if ($('preview-title') && result.title) $('preview-title').textContent = result.title;
  if ($('preview-meta')) $('preview-meta').textContent = label;
  renderThumbnail(result);
  renderProgress(result);
  showStatus(statusCopy(result.status), 'info', result.status);
  if (result.formats && result.formats.length && result.status === 'awaiting_format') {
    renderFormats(result.formats);
  }
}

async function pollJob(token) {
  if (token !== currentJobToken) return;
  if (Date.now() - pollStartedAt >= POLL_TIMEOUT_MS) {
    showTimeout();
    return;
  }

  try {
    const response = await fetch(`/jobs/${encodeURIComponent(token)}`, { cache: 'no-store' });
    const result = await readResponse(response);

    if (!response.ok) {
      throw new Error(result.message || 'The job status could not be read.');
    }

    if (result.status === 'failed') {
      showFailure(result.message);
      return;
    }

    updateJob(result);

    if (result.status === 'ready' && result.file_url) {
      const button = $('quality-download');
      if (button) {
        button.dataset.fileUrl = result.file_url;
      }
      downloadStarted = false;
      showResult('Download ready', result.filename
        ? `Your file is ready: ${result.filename}`
        : 'Your selected file is ready to download.');
      if ($('qualities')) $('qualities').innerHTML = '';
      if ($('quality-title')) $('quality-title').textContent = 'Your file is ready';
      showStatus('Your file is ready to download.', 'success', 'ready');
      hideRecovery();
      updateActionState();
      return;
    }

    if (result.status === 'downloading' || result.status === 'converting'
      || result.status === 'checking' || result.status === 'uploading') {
      showResult(statusCopy(result.status), 'Your selected file is being prepared.');
    }
    updateActionState();
    if (result.status !== 'awaiting_format') {
      schedulePoll(token);
    }
  } catch (error) {
    showResult('This is taking longer than usual', 'The status check could not be completed. Retry the status check or cancel and start again.');
    showRecovery('polling');
    showStatus(error.message || 'The preview could not be completed.', 'error');
  }
}

function handleDownloadClick() {
  const fileUrl = $('quality-download')?.dataset.fileUrl;
  if (fileUrl) {
    window.open(fileUrl, '_blank', 'noopener');
    return;
  }
  downloadSelected();
}

async function downloadSelected() {
  const quality = selectedQuality();
  if (!currentJobToken || !quality) {
    showStatus('Select an available format first.', 'error');
    return;
  }

  if (downloadInFlight || downloadStarted) return;
  downloadInFlight = true;
  updateActionState();
  try {
    const response = await fetch('/download-quality', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_token: currentJobToken, quality }),
    });
    const result = await readResponse(response);
    if (!response.ok) throw new Error(result.message || 'Could not start the download.');
    downloadStarted = true;
    showStatus('Your download is being prepared...', 'info', 'download-started');
    showResult('Preparing your download', 'Your selected quality is being prepared.');
    updateActionState();
    startPolling(currentJobToken);
  } catch (error) {
    showStatus(error.message || 'The download could not be started.', 'error');
    downloadStarted = false;
  } finally {
    downloadInFlight = false;
    updateActionState();
  }
}

function retryJob() {
  const mode = $('retry-button')?.dataset.mode;
  if (mode === 'failed') {
    const input = $('single-url');
    if (input) input.value = lastSubmittedUrl;
    currentJobToken = null;
    resetPreviewOnly();
    fetchMedia();
    return;
  }
  if (currentJobToken) startPolling(currentJobToken);
}

function resetDownloader() {
  currentJobToken = null;
  lastSubmittedUrl = '';
  const input = $('single-url');
  if (input) {
    input.value = '';
    input.setAttribute('aria-invalid', 'false');
  }
  resetPreviewOnly();
  updateActionState();
  showStatus('Ready for another URL.', 'success');
  input?.focus();
}

document.addEventListener('DOMContentLoaded', () => {
  const form = $('downloader-form');
  if (form) form.addEventListener('submit', (event) => {
    event.preventDefault();
    fetchMedia();
  });

  const anotherButton = $('another-button');
  if (anotherButton) anotherButton.addEventListener('click', resetDownloader);

  const downloadButton = $('quality-download');
  if (downloadButton) downloadButton.addEventListener('click', handleDownloadClick);

  $('retry-button')?.addEventListener('click', retryJob);
  $('cancel-button')?.addEventListener('click', resetDownloader);

  const input = $('single-url');
  if (input) {
    input.addEventListener('input', () => input.setAttribute('aria-invalid', 'false'));
  }
  updateActionState();
});
