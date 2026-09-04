import os
from itertools import cycle

import gevent
from locust import HttpUser, between, task


SUCCESS_URL = os.environ.get('LOAD_TEST_SUCCESS_URL')
FAILURE_URL = os.environ.get('LOAD_TEST_FAILURE_URL')
POLL_SECONDS = float(os.environ.get('LOAD_TEST_POLL_SECONDS', '2'))
MAX_POLLS = int(os.environ.get('LOAD_TEST_MAX_POLLS', '90'))


class VidzFlowUser(HttpUser):
    wait_time = between(3, 8)

    def on_start(self):
        urls = [url for url in (SUCCESS_URL, FAILURE_URL) if url]
        if not urls:
            raise RuntimeError(
                'Set LOAD_TEST_SUCCESS_URL and optionally LOAD_TEST_FAILURE_URL'
            )
        self.urls = cycle(urls)

    @task
    def download_flow(self):
        source_url = next(self.urls)
        preview = self.client.post(
            '/preview',
            json={'url': source_url},
            name='POST /preview',
        )
        if preview.status_code != 202:
            return
        payload = preview.json()
        token = payload.get('job_token')
        if not token:
            return

        formats = self.poll_until(token, {'awaiting_format', 'failed'})
        if not formats or formats.get('status') == 'failed':
            return
        available = formats.get('formats') or []
        if not available:
            return

        selected = self.client.post(
            '/download-quality',
            json={'job_token': token, 'quality': available[0]['id']},
            name='POST /download-quality',
        )
        if selected.status_code != 202:
            return
        self.poll_until(token, {'ready', 'failed'})

    def poll_until(self, token, terminal_states):
        for _ in range(MAX_POLLS):
            response = self.client.get(
                f'/jobs/{token}',
                name='GET /jobs/<token>',
            )
            if response.status_code != 200:
                return None
            payload = response.json()
            if payload.get('status') in terminal_states:
                return payload
            gevent.sleep(POLL_SECONDS)
        return None
