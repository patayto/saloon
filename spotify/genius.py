"""
Genius lyrics client via the lyricsgenius library.

Requires GENIUS_ACCESS_TOKEN env var (from genius.com/api-clients).
Falls back gracefully if the token is not configured or the package is missing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class GeniusResult:
    plain_lyrics: str


def get_lyrics(track_name: str, artist_name: str) -> GeniusResult | None:
    """Fetch lyrics from Genius for a track.

    Returns a GeniusResult with plain lyrics, or None if not found or
    GENIUS_ACCESS_TOKEN is not set.
    """
    token = os.environ.get("GENIUS_ACCESS_TOKEN", "")
    if not token:
        logger.debug("GENIUS_ACCESS_TOKEN not set; skipping Genius lookup")
        return None

    try:
        import lyricsgenius
    except ImportError:
        logger.warning("lyricsgenius not installed; skipping Genius lookup")
        return None

    genius = lyricsgenius.Genius(
        token,
        remove_section_headers=True,
        skip_non_songs=True,
        excluded_terms=["(Remix)", "(Live)"],
    )

    logger.info("Genius lookup: %r by %r", track_name, artist_name)
    try:
        song = genius.search_song(track_name, artist=artist_name)
    except Exception as exc:
        logger.warning("Genius request failed for %r: %s", track_name, exc)
        return None

    if song is None or not song.lyrics:
        logger.info("Genius: not found — %r by %r", track_name, artist_name)
        return None

    return GeniusResult(plain_lyrics=song.lyrics.strip())
