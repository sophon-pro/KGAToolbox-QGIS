# -*- coding: utf-8 -*-
"""Pure logic for the Check for Update window: no Qt, no network.

The window fetches the latest GitHub release of the plugin's repository; this
module turns that answer into something to show. Keeping it free of Qt lets it
be tested without QGIS.
"""

import json
import re

REPO = 'sophon-pro/KGAToolbox-QGIS'
LATEST_RELEASE_API = 'https://api.github.com/repos/{}/releases/latest'.format(REPO)
RELEASES_PAGE = 'https://github.com/{}/releases'.format(REPO)


def parse_version(text):
    """'v1.2.3-beta' -> (1, 2, 3). None if no digits are found."""
    numbers = re.findall(r'\d+', (text or '').split('-')[0])
    return tuple(int(n) for n in numbers) or None


def compare_versions(current, latest):
    """-1 if `latest` is newer, 0 if the same, 1 if `current` is ahead.

    None when either side cannot be read as a version.
    """
    a, b = parse_version(current), parse_version(latest)
    if a is None or b is None:
        return None
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return (a > b) - (a < b)


def parse_release(raw):
    """(version, notes, url) from a GitHub 'latest release' response body.

    Raises ValueError if it is not a release.
    """
    try:
        data = json.loads(bytes(raw).decode('utf-8'))
    except (ValueError, TypeError) as exc:
        raise ValueError('The reply was not valid JSON.') from exc
    if not isinstance(data, dict) or not data.get('tag_name'):
        raise ValueError('The reply did not describe a release.')
    version = str(data['tag_name']).lstrip('vV')
    notes = (data.get('body') or '').strip()
    url = data.get('html_url') or RELEASES_PAGE
    return version, notes, url
