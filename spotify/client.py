"""
Spotify Web API client helpers.

Thin wrappers around the REST API that handle auth via get_access_token().
"""

from __future__ import annotations

import logging

import requests

from spotify.auth import get_access_token

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.spotify.com/v1"


def get_track(track_id: str) -> dict:
    """Fetch a single track object from the Spotify API."""
    resp = requests.get(
        f"{_BASE_URL}/tracks/{track_id}",
        headers={"Authorization": f"Bearer {get_access_token()}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_user_playlists(page_size: int = 50):
    """Yield simplified playlist objects for the current user (paginated)."""
    url = f"{_BASE_URL}/me/playlists"
    headers = {"Authorization": f"Bearer {get_access_token()}"}
    offset = 0
    while True:
        resp = requests.get(
            url,
            headers=headers,
            params={"limit": page_size, "offset": offset},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            if item:
                yield item
        if data.get("next") is None:
            break
        offset += page_size


def get_playlist_tracks(playlist_id: str, page_size: int = 50):
    """Yield playlist track items for a playlist (paginated).

    Each item has shape: {added_at, added_by: {id}, track: {id, name, uri, ...}}.
    """
    url = f"{_BASE_URL}/playlists/{playlist_id}/items"
    headers = {"Authorization": f"Bearer {get_access_token()}"}
    offset = 0
    while True:
        resp = requests.get(
            url,
            headers=headers,
            params={
                "limit": page_size,
                "offset": offset,
                "fields": (
            "next,items(added_at,added_by(id),track("
            "id,name,uri,href,duration_ms,explicit,popularity,"
            "track_number,disc_number,is_local,external_urls,"
            "album(id,name,album_type,uri,href,release_date,"
            "release_date_precision,total_tracks,external_urls,"
            "artists(id,name,uri,href,external_urls)),"
            "artists(id,name,uri,href,external_urls)))"
        ),
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            if item:
                yield item
        if data.get("next") is None:
            break
        offset += page_size


def get_playlist_track_ids(playlist_id: str, page_size: int = 50):
    """Yield lightweight track entries for a playlist (paginated).

    Requests only the fields needed for delta comparison (no full track metadata).
    Each yielded dict has shape: {track_id, added_at, added_by}.
    Position is the sequential index in the yielded stream (0-based).
    Null items and local files are skipped.
    """
    url = f"{_BASE_URL}/playlists/{playlist_id}/items"
    headers = {"Authorization": f"Bearer {get_access_token()}"}
    offset = 0
    while True:
        resp = requests.get(
            url,
            headers=headers,
            params={
                "limit": page_size,
                "offset": offset,
                "fields": "next,items(added_at,added_by(id),track(id,is_local))",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("items", []):
            if not item:
                continue
            track = item.get("track")
            if not track or not track.get("id") or track.get("is_local"):
                continue
            yield {
                "track_id": track["id"],
                "added_at": item.get("added_at"),
                "added_by": (item.get("added_by") or {}).get("id", ""),
            }
        if data.get("next") is None:
            break
        offset += page_size


def get_tracks_batch(track_ids: list[str], page_size: int = 50) -> list[dict]:
    """Fetch full track objects for a list of IDs (max 50 per request).

    Returns a flat list of track dicts (same shape as a single track object).
    Unknown IDs are omitted from the response by Spotify.
    """
    headers = {"Authorization": f"Bearer {get_access_token()}"}
    results: list[dict] = []
    for i in range(0, len(track_ids), page_size):
        batch = track_ids[i : i + page_size]
        resp = requests.get(
            f"{_BASE_URL}/tracks",
            headers=headers,
            params={"ids": ",".join(batch)},
            timeout=15,
        )
        resp.raise_for_status()
        for track in resp.json().get("tracks", []):
            if track:
                results.append(track)
    return results


