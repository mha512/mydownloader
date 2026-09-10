from platforms import PLATFORM_HOSTS
from url_validation import detect_platform


def test_tiktok_share_host_is_supported():
    assert PLATFORM_HOSTS['vt.tiktok.com'] == 'tiktok'
    assert detect_platform('https://vt.tiktok.com/ZSFakeShareCode/') == 'tiktok'