"""
Library-level playlist scan.

Fetches all playlists from the Spotify API and ensures each has a local DB entry.
For existing playlists, compares ``snapshot_id`` to detect changes and marks the
playlist as stale (``is_stale=True``) when the snapshot differs — without fetching
tracks. For new playlists, performs a full track delta sync immediately.

Usage:
    python manage.py sync_playlists
"""

import logging

from django.core.management.base import BaseCommand

from spotify.client import get_user_playlists
from spotify.importers import update_playlist_metadata, upsert_playlist
from spotify.models import Playlist
from spotify.management.commands.sync_playlist_tracks import run_sync as run_track_sync

logger = logging.getLogger(__name__)


def run_sync(progress_cb=None, sync_new_tracks=True) -> dict:
    """
    Scan all Spotify playlists and sync metadata / staleness state.

    Returns::

        {
            "scanned": int,
            "new": int,          # playlists created (tracks synced immediately)
            "stale": int,        # existing playlists whose snapshot changed
            "unchanged": int,
            "errors": [...],
        }

    progress_cb(done: int, total: int) is called after each playlist is processed.
    total is 0 (unknown) until all playlists are fetched.

    When ``sync_new_tracks`` is False, new playlists get a metadata-only row
    (``tracks_synced_at`` stays null) so the user can pick which ones to
    track-sync from the preview modal.
    """
    scanned = 0
    new_count = 0
    stale_count = 0
    unchanged_count = 0
    errors: list[dict] = []

    for playlist_data in get_user_playlists():
        playlist_id = playlist_data.get("id", "?")
        playlist_name = playlist_data.get("name", "?")
        try:
            existing = Playlist.objects.filter(id=playlist_id).first()

            if existing is None:
                # New playlist — create it and sync tracks straight away.
                playlist = upsert_playlist(playlist_data)
                if sync_new_tracks:
                    logger.info("New playlist %s (%s) — syncing tracks", playlist_id, playlist_name)
                    try:
                        run_track_sync(playlist_id)
                    except Exception as exc:
                        logger.warning(
                            "Track sync failed for new playlist %s: %s", playlist_id, exc
                        )
                        errors.append({
                            "type": "track_sync",
                            "id": playlist_id,
                            "name": playlist_name,
                            "error": str(exc),
                        })
                else:
                    logger.info(
                        "New playlist %s (%s) — pending track sync", playlist_id, playlist_name
                    )
                new_count += 1

            elif existing.snapshot_id != (playlist_data.get("snapshot_id") or ""):
                # Snapshot changed — update metadata and mark as stale.
                logger.info(
                    "Playlist %s (%s) is stale (snapshot changed)", playlist_id, playlist_name
                )
                update_playlist_metadata(existing, playlist_data)
                stale_count += 1

            else:
                # No change.
                unchanged_count += 1

        except Exception as exc:
            errors.append({
                "type": "playlist",
                "id": playlist_id,
                "name": playlist_name,
                "error": str(exc),
            })
            logger.exception("Failed to process playlist %s", playlist_id)

        scanned += 1
        if progress_cb:
            progress_cb(scanned, 0)

    return {
        "scanned": scanned,
        "new": new_count,
        "stale": stale_count,
        "unchanged": unchanged_count,
        "errors": errors,
    }


class Command(BaseCommand):
    help = "Scan Spotify playlists and update metadata / staleness state (no full track sync)"

    def handle(self, *args, **options):
        stats = run_sync()
        error_count = len(stats["errors"])
        suffix = f" {error_count} error(s)." if error_count else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Scanned {stats['scanned']} playlists: "
                f"{stats['new']} new, {stats['stale']} stale, "
                f"{stats['unchanged']} unchanged.{suffix}"
            )
        )
