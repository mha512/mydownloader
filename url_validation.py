"""Validation for URLs accepted by the external download worker."""

import re
import ipaddress
import socket
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from platforms import PLATFORM_HOSTS

SUPPORTED_HOSTS = frozenset(PLATFORM_HOSTS)
FORMAT_ID_PATTERN = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
MAX_REDIRECTS = 5


def is_valid_format_id(value):
    return not isinstance(value, bool) and isinstance(value, (str, int)) and bool(
        FORMAT_ID_PATTERN.fullmatch(str(value).strip())
    )


def _coerce_ip_address(hostname):
    try:
        return {
            result[4][0]
            for result in socket.getaddrinfo(
                hostname, 443, type=socket.SOCK_STREAM
            )
        }
    except socket.gaierror:
        return set()


def _is_public_ip_address(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def has_public_host(url):
    parsed = urlparse(url.strip())
    if not parsed.hostname:
        return False
    addresses = _coerce_ip_address(parsed.hostname)
    return bool(addresses) and all(
        _is_public_ip_address(address) for address in addresses
    )


def validate_redirect_target(url):
    """Reject redirect targets that resolve to local-only or private network addresses."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower().rstrip('.')
    if hostname in {'localhost', '127.0.0.1', '::1'}:
        return False
    if hostname.endswith('.localhost'):
        return False
    addresses = _coerce_ip_address(hostname)
    if not addresses:
        return False
    return all(_is_public_ip_address(address) for address in addresses)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, msg, headers, newurl):
        return None


def validate_redirect_chain(url, max_redirects=MAX_REDIRECTS, opener=None):
    """Validate every HTTP redirect target before a downloader follows it."""
    current_url = url.strip()
    if not has_public_host(current_url):
        return False
    if max_redirects < 0:
        return False

    redirect_opener = opener or build_opener(_NoRedirectHandler())
    for redirect_count in range(max_redirects + 1):
        try:
            if opener:
                response = redirect_opener(current_url)
            else:
                response = redirect_opener.open(
                    Request(current_url, headers={'Range': 'bytes=0-0'}),
                    timeout=10,
                )
        except HTTPError as error:
            response = error
        except (OSError, ValueError):
            return False

        try:
            status = getattr(response, 'status', None)
            if status is None:
                status = response.getcode()
            location = response.headers.get('Location')
        finally:
            response.close()

        if status not in {301, 302, 303, 307, 308}:
            return True
        if not location or redirect_count == max_redirects:
            return False
        current_url = urljoin(current_url, location)
        if not validate_redirect_target(current_url):
            return False


def detect_platform(url):
    parsed = urlparse(url.strip())
    if parsed.scheme != 'https' or not parsed.hostname:
        return 'unknown'

    hostname = parsed.hostname.lower().rstrip('.')
    if parsed.username or parsed.password or parsed.port or parsed.fragment:
        return 'unknown'
    return PLATFORM_HOSTS.get(hostname, 'unknown')
