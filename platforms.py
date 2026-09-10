"""Single source of truth for supported platform routing and page content."""

PLATFORM_CONFIG = {
    'youtube': {
        'name': 'YouTube',
        'hosts': ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'),
        'url_example': 'https://www.youtube.com/watch?v=VIDEO_ID',
        'not_supported_example': 'A private video URL that requires YouTube sign-in',
        'limitation': 'The smoke test returned a thumbnail and 28 available formats for the sampled public video. Other videos can expose a different list, and private, login-gated, age-restricted, DRM-protected, or otherwise unavailable videos may fail where supported by the current extractor.',
        'description': 'Preview and download a public YouTube video where supported by the current extractor, then choose from the formats returned for that video.',
        'faqs': (
            ('Can I use a YouTube Shorts URL?',
             'Public Shorts URLs can be submitted where supported by the current extractor. Availability and returned formats depend on the source response.'),
            ('Will a private YouTube video work?',
             'No. Private or login-gated videos are not supported, and the service does not bypass access controls.'),
            ('Why are formats different between videos?',
             'YouTube returns formats per video. The downloader only offers formats returned by the current extraction job.'),
        ),
    },
    'instagram': {
        'name': 'Instagram',
        'hosts': ('instagram.com', 'www.instagram.com'),
        'url_example': 'https://www.instagram.com/reel/POST_ID/',
        'not_supported_example': 'A private post URL or a post that requires Instagram login',
        'limitation': 'The smoke test completed extraction for a public Instagram URL. Format counts were not recorded as a stable platform-wide property, so each post is evaluated independently; private accounts, login-gated posts, Stories with restricted access, and DRM-protected media are not supported.',
        'description': 'Preview and download a public Instagram post or Reel where supported by the current extractor, using only formats returned for that post.',
        'faqs': (
            ('Can I download an Instagram Reel?',
             'A public Reel can be submitted where supported by the current extractor. Private or login-gated Reels are not supported.'),
            ('Do Instagram private posts work?',
             'No. The service does not use credentials or bypass private-account permissions.'),
            ('Why might Instagram offer fewer choices?',
             'Some Instagram posts expose a limited set of source formats. The page only shows formats returned by the extraction job.'),
        ),
    },
    'tiktok': {
        'name': 'TikTok',
        'hosts': ('tiktok.com', 'www.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'),
        'url_example': 'https://www.tiktok.com/@creator/video/VIDEO_ID',
        'not_supported_example': 'A private TikTok video or a link that requires account access',
        'limitation': 'The smoke test completed extraction for a public TikTok URL. TikTok can occasionally require additional source-side verification that interrupts automated extraction, so public, login-free availability may be more sensitive to extractor or source changes; private, region-restricted, DRM-protected, or removed videos may fail where supported by the current extractor.',
        'description': 'Preview and download a public TikTok video where supported by the current extractor, with quality choices based on the formats returned.',
        'faqs': (
            ('Can I submit a TikTok share link?',
             'A public TikTok URL or supported short link can be submitted where supported by the current extractor.'),
            ('Will private or friends-only videos work?',
             'No. The service does not access account-only TikTok content or bypass visibility settings.'),
            ('Why are there sometimes few quality choices?',
             'TikTok may return a limited format set for a video. Only those returned formats are offered.'),
        ),
    },
    'facebook': {
        'name': 'Facebook',
        'hosts': ('facebook.com', 'www.facebook.com', 'fb.watch'),
        'url_example': 'https://www.facebook.com/watch/?v=VIDEO_ID',
        'not_supported_example': 'A Facebook video that requires login or belongs to a private audience',
        'limitation': 'The smoke test completed extraction for a public Facebook URL. Available formats were not recorded as a stable platform-wide property and can vary by post; login-gated, audience-restricted, removed, DRM-protected, or region-limited media may fail where supported by the current extractor.',
        'description': 'Preview and download public Facebook video content where supported by the current extractor, based on the formats returned for that post.',
        'faqs': (
            ('Can I use an fb.watch link?',
             'Supported public fb.watch links can be submitted where supported by the current extractor.'),
            ('Will a private Facebook post work?',
             'No. The service does not use Facebook credentials or bypass audience restrictions.'),
            ('Why does one Facebook post have different formats?',
             'Facebook can expose different source formats per post. The downloader only lists formats returned for that job.'),
        ),
    },
}

PLATFORM_ORDER = ('youtube', 'instagram', 'tiktok', 'facebook')
PLATFORM_HOSTS = {
    host: platform
    for platform, details in PLATFORM_CONFIG.items()
    for host in details['hosts']
}
