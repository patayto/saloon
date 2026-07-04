"""
ReccoBeatsProvider — resolves audio features from the ReccoBeats API.

The API accepts Spotify track IDs directly via the `ids` query parameter
(comma-separated, up to 40 per request) and returns audio features with
Spotify hrefs embedded for ID cross-referencing.

No API key is required.  Rate limits apply.

API endpoint: GET https://api.reccobeats.com/v1/audio-features?ids=<ids>
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path

import requests

from .base import AudioFeaturesResult

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.reccobeats.com/v1"
_BATCH_SIZE = 40
_INTER_BATCH_DELAY = 2.0  # seconds between requests
_MAX_429_RETRIES = 3
_BACKOFF_BASE = 5.0  # seconds; doubles per attempt: 5, 10, 20
_SPOTIFY_HREF_RE = re.compile(r"https://open\.spotify\.com/track/([A-Za-z0-9]+)")


def _extract_spotify_id(href: str) -> str | None:
    m = _SPOTIFY_HREF_RE.search(href or "")
    return m.group(1) if m else None


def _parse_batch(data: object) -> list[dict]:
    features_list = data if isinstance(data, list) else data.get("content", [])  # type: ignore[union-attr]
    return [features_list] if isinstance(features_list, dict) else features_list


class ReccoBeatsProvider:
    """Audio features from the ReccoBeats API (no credentials required)."""

    name = "reccobeats"

    def __init__(self, timeout: int = 10):
        self._timeout = timeout

    def fetch_stream(self, track_ids: list[str]) -> Iterator[dict[str, AudioFeaturesResult]]:
        """Yield one result dict per batch as responses arrive.

        Each yielded dict maps Spotify track ID → AudioFeaturesResult for the
        tracks resolved in that batch.  Empty dicts are not yielded.
        Callers can persist each batch immediately rather than waiting for all
        requests to finish.
        """
        total_batches = (len(track_ids) + _BATCH_SIZE - 1) // _BATCH_SIZE
        total_resolved = 0

        for batch_num, i in enumerate(range(0, len(track_ids), _BATCH_SIZE), start=1):
            batch = track_ids[i : i + _BATCH_SIZE]
            logger.info(
                "ReccoBeats: batch %d/%d — requesting %d tracks",
                batch_num, total_batches, len(batch),
            )

            if batch_num > 1:
                time.sleep(_INTER_BATCH_DELAY)

            try:
                # Pass ids as a raw query string — requests encodes commas as
                # %2C which the ReccoBeats API rejects with a 400.
                url = f"{_BASE_URL}/audio-features?ids={','.join(batch)}"
                resp = requests.get(
                    url,
                    headers={"Accept": "application/json"},
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.exceptions.RequestException as exc:
                logger.warning("ReccoBeats: batch %d/%d failed: %s", batch_num, total_batches, exc)
                continue

            batch_results: dict[str, AudioFeaturesResult] = {}
            for item in _parse_batch(data):
                sid = _extract_spotify_id(item.get("href", ""))
                if not sid:
                    sid = item.get("trackId") or item.get("spotify_id")
                if not sid:
                    continue
                try:
                    batch_results[sid] = AudioFeaturesResult(
                        track_id=sid,
                        acousticness=float(item.get("acousticness", 0)),
                        danceability=float(item.get("danceability", 0)),
                        energy=float(item.get("energy", 0)),
                        instrumentalness=float(item.get("instrumentalness", 0)),
                        key=int(item.get("key", 0)),
                        liveness=float(item.get("liveness", 0)),
                        loudness=float(item.get("loudness", 0)),
                        mode=int(item.get("mode", 0)),
                        speechiness=float(item.get("speechiness", 0)),
                        tempo=float(item.get("tempo", 0)),
                        time_signature=int(item.get("time_signature", 4)),
                        valence=float(item.get("valence", 0)),
                    )
                except (TypeError, ValueError) as exc:
                    logger.warning("ReccoBeats: skipping track %s — bad data: %s", sid, exc)

            total_resolved += len(batch_results)
            logger.info(
                "ReccoBeats: batch %d/%d — resolved %d/%d (running total: %d)",
                batch_num, total_batches, len(batch_results), len(batch), total_resolved,
            )

            if batch_results:
                yield batch_results

        logger.info(
            "ReccoBeats: done — resolved %d / %d tracks total",
            total_resolved, len(track_ids),
        )

    def fetch(self, track_ids: list[str]) -> dict[str, AudioFeaturesResult]:
        """Return all resolved features as a single dict (collects fetch_stream)."""
        results: dict[str, AudioFeaturesResult] = {}
        for batch in self.fetch_stream(track_ids):
            results.update(batch)
        return results

    def fetch_from_file(self, file_path: str | Path, track_id: str) -> AudioFeaturesResult | None:
        """Extract audio features from a local audio file via the ReccoBeats analysis endpoint.

        POSTs the file as multipart/form-data to /v1/analysis/audio-features.
        The endpoint returns 9 features; key, mode, and time_signature are not
        provided and default to 0, 0, and 4 respectively.

        Retries with exponential backoff on 429 (honouring Retry-After when
        present); gives up on this track after _MAX_429_RETRIES so one
        rate-limited track never stalls the rest of a run for long.

        Returns None if the request fails or the response cannot be parsed.
        """
        path = Path(file_path)
        logger.info("ReccoBeats analysis: uploading %s for track %s", path.name, track_id)

        try:
            for attempt in range(_MAX_429_RETRIES + 1):
                with path.open("rb") as f:
                    resp = requests.post(
                        f"{_BASE_URL}/analysis/audio-features",
                        files={"audioFile": (path.name, f, "audio/mpeg")},
                        timeout=self._timeout,
                    )
                if resp.status_code == 429 and attempt < _MAX_429_RETRIES:
                    try:
                        delay = float(resp.headers.get("Retry-After", ""))
                    except ValueError:
                        delay = _BACKOFF_BASE * 2**attempt
                    logger.info(
                        "ReccoBeats analysis: 429 for track %s — retrying in %.0fs (attempt %d/%d)",
                        track_id, delay, attempt + 1, _MAX_429_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                item = resp.json()
                break
        except requests.exceptions.RequestException as exc:
            logger.warning("ReccoBeats analysis failed for track %s: %s", track_id, exc)
            return None

        try:
            return AudioFeaturesResult(
                track_id=track_id,
                acousticness=float(item.get("acousticness", 0)),
                danceability=float(item.get("danceability", 0)),
                energy=float(item.get("energy", 0)),
                instrumentalness=float(item.get("instrumentalness", 0)),
                key=int(item.get("key", 0)),
                liveness=float(item.get("liveness", 0)),
                loudness=float(item.get("loudness", 0)),
                mode=int(item.get("mode", 0)),
                speechiness=float(item.get("speechiness", 0)),
                tempo=float(item.get("tempo", 0)),
                time_signature=int(item.get("time_signature", 4)),
                valence=float(item.get("valence", 0)),
            )
        except (TypeError, ValueError) as exc:
            logger.warning("ReccoBeats analysis: bad response for track %s: %s", track_id, exc)
            return None
