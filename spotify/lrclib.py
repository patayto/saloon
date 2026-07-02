"""
LRCLib API client.

No authentication required. LRCLib is a community-contributed lyrics database
with ~3M tracks. Lookup is by track name, artist name, and optionally album
name + duration (in seconds).

API: https://lrclib.net/api/get
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://lrclib.net/api"
_HEADERS = {"User-Agent": "Saloon/1.0 (https://github.com/saloon; personal Spotify library analyser)"}


@dataclass
class LRCLibResult:
    track_name: str
    artist_name: str
    album_name: str
    duration: int           # seconds
    instrumental: bool
    plain_lyrics: str       # empty string if not available
    synced_lyrics: str      # LRC format; empty string if not available


def get_lyrics(
    track_name: str,
    artist_name: str,
    album_name: str = "",
    duration_seconds: int | None = None,
) -> LRCLibResult | None:
    """Fetch lyrics from LRCLib for a track.

    Returns an LRCLibResult, or None if the track is not found (404) or the
    request fails.

    The ``duration_seconds`` parameter improves match accuracy when provided.
    """
    params: dict[str, str | int] = {
        "track_name": track_name,
        "artist_name": artist_name,
    }
    if album_name:
        params["album_name"] = album_name
    if duration_seconds is not None:
        params["duration"] = duration_seconds

    logger.info("LRCLib lookup: %r by %r", track_name, artist_name)

    try:
        resp = requests.get(
            f"{_BASE_URL}/get",
            params=params,
            headers=_HEADERS,
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning("LRCLib request failed for %r: %s", track_name, exc)
        return None

    if resp.status_code == 404:
        logger.info("LRCLib: not found — %r by %r", track_name, artist_name)
        return None

    if not resp.ok:
        logger.warning("LRCLib returned %s for %r: %s", resp.status_code, track_name, resp.text[:200])
        return None

    data = resp.json()
    return LRCLibResult(
        track_name=data.get("trackName", ""),
        artist_name=data.get("artistName", ""),
        album_name=data.get("albumName", ""),
        duration=data.get("duration") or 0,
        instrumental=bool(data.get("instrumental")),
        plain_lyrics=data.get("plainLyrics") or "",
        synced_lyrics=data.get("syncedLyrics") or "",
    )
