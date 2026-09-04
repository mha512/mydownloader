"""Validation for URLs accepted by the external download worker."""

import re
import ipaddress
import socket
from urllib.parse import urlparse


SUPPORTED_HOSTS = {
    'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be',
    'instagram.com', 'www.instagram.com',
    'facebook.com', 'www.facebook.com', 'fb.watch',
    'tiktok.com', 'www.tiktok.com', 'vm.tiktok.com',
}
FORMAT_ID_PATTERN = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


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


def detect_platform(url):
    parsed = urlparse(url.strip())
    if parsed.scheme != 'https' or not parsed.hostname:
        return 'unknown'

    hostname = parsed.hostname.lower().rstrip('.')
    if parsed.username or parsed.password or parsed.port or parsed.fragment:
        return 'unknown'
    if hostname not in SUPPORTED_HOSTS:
        return 'unknown'
    if hostname in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'}:
        return 'youtube'
    if hostname in {'instagram.com', 'www.instagram.com'}:
        return 'instagram'
    if hostname in {'facebook.com', 'www.facebook.com', 'fb.watch'}:
        return 'facebook'
    if hostname in {'tiktok.com', 'www.tiktok.com', 'vm.tiktok.com'}:
        return 'tiktok'
    return 'unknown'
