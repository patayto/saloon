"""
Deezer API client.

No authentication required for catalog/search endpoints.

Search endpoint: GET https://api.deezer.com/search/track?q=<query>
Returns a list of track objects, each with a `preview` field containing
a direct CDN URL to a 30-second MP3 clip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.deezer.com"


@dataclass
class DeezerTrack:
    id: int
    title: str
    artist: str
    duration: int       # seconds
    preview: str        # 30-second MP3 URL


def search_track(title: str, artist: str) -> DeezerTrack | None:
    """Search Deezer for a track by title and artist name.

    Returns the first result, or None if no match is found.
    The search query is ``{artist} {title}``; Deezer ranks results by
    relevance so the first hit is typically the correct track.
    """
    query = f"{artist} {title}"
    logger.info("Deezer search: %r", query)

    try:
        resp = requests.get(
            f"{_BASE_URL}/search/track",
            params={"q": query},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as exc:
        logger.warning("Deezer search failed for %r: %s", query, exc)
        return None

    items = data.get("data", [])
    if not items:
        logger.info("Deezer: no results for %r", query)
        return None

    item = items[0]
    return DeezerTrack(
        id=item["id"],
        title=item["title"],
        artist=item["artist"]["name"],
        duration=item["duration"],
        preview=item["preview"],
    )
