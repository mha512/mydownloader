from platforms import PLATFORM_HOSTS
from url_validation import detect_platform
from worker import extractor_args_for_platform, yt_dlp_options_for_platform


def test_tiktok_share_host_is_supported():
    assert PLATFORM_HOSTS['vt.tiktok.com'] == 'tiktok'
    assert detect_platform('https://vt.tiktok.com/ZSFakeShareCode/') == 'tiktok'


def test_tiktok_uses_only_tiktok_extractor_fallback():
    assert extractor_args_for_platform('tiktok') == {
        'tiktok': {'app_info': ['musical_ly/35.1.3/2023501030/0']},
    }
    assert extractor_args_for_platform('youtube') == {}


def test_tiktok_challenge_retries_do_not_change_other_platforms():
    assert yt_dlp_options_for_platform('tiktok')['extractor_retries'] == 3
    assert yt_dlp_options_for_platform('youtube') == {}
    assert yt_dlp_options_for_platform('facebook') == {}