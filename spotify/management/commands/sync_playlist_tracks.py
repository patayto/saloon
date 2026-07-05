"""
Per-playlist delta track sync.

Fetches the current track list for a single playlist from the Spotify API,
diffs it against the local DB, adds new tracks (with enrichment), removes
deleted tracks, and updates positions. Marks the playlist as fresh afterwards.

Usage:
    python manage.py sync_playlist_tracks <playlist_id>
"""

import logging

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from spotify.client import get_playlist_track_ids, get_tracks_batch
from spotify.importers import update_playlist_created_at, upsert_track
from spotify.models import Playlist, PlaylistTrack
from spotify.pipeline import enrich_tracks

logger = logging.getLogger(__name__)


def run_sync(playlist_id: str, progress_cb=None, stage_cb=None) -> dict:
    """
    Delta-sync tracks for one playlist. Returns::

        {
            "added": int,
            "removed": int,
            "reordered": int,
            "errors": [...],
            "enrichment": {"audio_features": {...}, "lyrics": {...}},
        }

    progress_cb(done: int, total: int) is called during enrichment of added tracks.
    stage_cb(stage: str) is called before each major step for UI progress labels.
    """
    playlist = Playlist.objects.get(id=playlist_id)
    errors: list[dict] = []

    def _stage(label):
        if stage_cb:
            stage_cb(label)

    # ------------------------------------------------------------------
    # Step 1: Fetch lightweight track list from Spotify (IDs + positions).
    # We enumerate to assign positions because the API returns tracks in
    # order and the lightweight fields don't include a position field.
    # ------------------------------------------------------------------
    _stage("Fetching tracks from Spotify")
    api_tracks: dict[str, dict] = {}  # spotify_track_id → {position, added_at, added_by}
    position = 0
    for item in get_playlist_track_ids(playlist_id):
        api_tracks[item["track_id"]] = {
            "position": position,
            "added_at": parse_datetime(item["added_at"]) if item.get("added_at") else None,
            "added_by": item.get("added_by", ""),
        }
        position += 1

    logger.info("Playlist %s (%s): %d tracks from API", playlist_id, playlist.name, len(api_tracks))

    # ------------------------------------------------------------------
    # Step 2: Get existing PlaylistTrack rows from DB.
    # ------------------------------------------------------------------
    existing_pts = {
        pt.spotify_track_id: pt
        for pt in PlaylistTrack.objects.filter(playlist=playlist)
    }

    api_ids = set(api_tracks)
    db_ids = set(existing_pts)
    added_ids = api_ids - db_ids
    removed_ids = db_ids - api_ids

    logger.info(
        "Playlist %s: +%d added, -%d removed",
        playlist_id, len(added_ids), len(removed_ids),
    )

    # ------------------------------------------------------------------
    # Step 3: Remove tracks that are no longer in the playlist.
    # ------------------------------------------------------------------
    if removed_ids:
        _stage(f"Removing {len(removed_ids)} track{'s' if len(removed_ids) != 1 else ''}")
        PlaylistTrack.objects.filter(playlist=playlist, spotify_track_id__in=removed_ids).delete()

    # ------------------------------------------------------------------
    # Step 4: Fetch full metadata for new tracks and create PlaylistTrack rows.
    # ------------------------------------------------------------------
    if added_ids:
        _stage(f"Adding {len(added_ids)} track{'s' if len(added_ids) != 1 else ''}")
        track_objects = get_tracks_batch(list(added_ids))
        for track_data in track_objects:
            tid = track_data.get("id")
            if not tid:
                continue
            try:
                track = upsert_track(track_data)
            except Exception as exc:
                errors.append({"id": tid, "error": str(exc)})
                logger.warning("Failed to upsert track %s: %s", tid, exc)
                added_ids.discard(tid)
                continue

            info = api_tracks[tid]
            PlaylistTrack.objects.create(
                playlist=playlist,
                track=track,
                spotify_track_id=tid,
                added_at=info["added_at"],
                added_by=info["added_by"],
                position=info["position"],
            )

    # ------------------------------------------------------------------
    # Step 5: Update positions for tracks that moved.
    # ------------------------------------------------------------------
    _stage("Updating positions")
    to_update = []
    for pt in PlaylistTrack.objects.filter(playlist=playlist).exclude(
        spotify_track_id__in=added_ids  # newly created — already at correct position
    ):
        new_pos = api_tracks.get(pt.spotify_track_id, {}).get("position")
        if new_pos is not None and pt.position != new_pos:
            pt.position = new_pos
            to_update.append(pt)
    if to_update:
        PlaylistTrack.objects.bulk_update(to_update, ["position"])

    reordered = len(to_update)
    logger.info("Playlist %s: %d tracks reordered", playlist_id, reordered)

    # ------------------------------------------------------------------
    # Step 6: Enrich new tracks only.
    # ------------------------------------------------------------------
    enrichment: dict = {}
    if added_ids:
        enrichment = enrich_tracks(list(added_ids), progress_cb=progress_cb, stage_cb=stage_cb)

    # ------------------------------------------------------------------
    # Step 7: Mark playlist as fresh and update created_at estimate.
    # ------------------------------------------------------------------
    update_playlist_created_at(playlist)
    playlist.is_stale = False
    playlist.tracks_synced_at = timezone.now()
    playlist.save(update_fields=["is_stale", "tracks_synced_at"])

    return {
        "added": len(added_ids),
        "removed": len(removed_ids),
        "reordered": reordered,
        "errors": errors,
        "enrichment": enrichment,
    }


class Command(BaseCommand):
    help = "Delta-sync tracks for a single playlist"

    def add_arguments(self, parser):
        parser.add_argument("playlist_id", help="Spotify playlist ID")

    def handle(self, *args, **options):
        playlist_id = options["playlist_id"]
        try:
            stats = run_sync(playlist_id)
        except Playlist.DoesNotExist:
            raise CommandError(f"Playlist {playlist_id!r} not found in DB. Run sync_playlists first.")
        error_count = len(stats["errors"])
        suffix = f" {error_count} error(s)." if error_count else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. +{stats['added']} added, -{stats['removed']} removed, "
                f"{stats['reordered']} reordered.{suffix}"
            )
        )
