import socket

import pytest

from url_validation import has_public_host, validate_redirect_target


def test_private_resolved_address_is_rejected(monkeypatch):
    monkeypatch.setattr(
        socket, 'getaddrinfo',
        lambda *args, **kwargs: [(None, None, None, None, ('10.0.0.5', 443))],
    )
    assert not has_public_host('https://www.youtube.com/watch?v=test')


def test_public_resolved_address_is_allowed(monkeypatch):
    monkeypatch.setattr(
        socket, 'getaddrinfo',
        lambda *args, **kwargs: [(None, None, None,
                                  None, ('93.184.216.34', 443))],
    )
    assert has_public_host('https://www.youtube.com/watch?v=test')


@pytest.mark.parametrize('redirect_url', [
    'http://127.0.0.1/',
    'http://localhost/',
    'http://169.254.169.254/latest/meta-data/',
    'http://10.0.0.5/',
])
def test_redirect_target_to_private_or_localhost_is_rejected(redirect_url):
    assert validate_redirect_target(redirect_url) is False
