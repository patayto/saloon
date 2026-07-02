import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand

from spotify.genius import get_lyrics as genius_get_lyrics
from spotify.lrclib import get_lyrics as lrclib_get_lyrics
from spotify.models import Track, TrackLyrics

_WORKERS = 8


def _fetch_one(track) -> str:
    """Fetch lyrics for a single track. Returns 'saved' or 'not_found'."""
    first_artist = track.artists.first()
    artist_name = first_artist.name if first_artist else ""
    duration_s = track.duration_ms // 1000

    lrclib_result = lrclib_get_lyrics(
        track_name=track.name,
        artist_name=artist_name,
        album_name=track.album.name,
        duration_seconds=duration_s,
    )

    if lrclib_result is not None:
        TrackLyrics.objects.get_or_create(
            track_id=track.id,
            defaults={
                "instrumental": lrclib_result.instrumental,
                "plain_lyrics": lrclib_result.plain_lyrics,
                "synced_lyrics": lrclib_result.synced_lyrics,
            },
        )
        return "saved"

    genius_result = genius_get_lyrics(
        track_name=track.name,
        artist_name=artist_name,
    )
    if genius_result is not None:
        TrackLyrics.objects.get_or_create(
            track_id=track.id,
            defaults={
                "instrumental": False,
                "plain_lyrics": genius_result.plain_lyrics,
                "synced_lyrics": "",
            },
        )
        return "saved"

    return "not_found"


def run_sync(progress_cb=None) -> dict:
    """Fetch and store lyrics for all saved tracks not yet in DB.

    Tries LRCLib first; falls back to Genius if LRCLib returns nothing.
    Returns {"saved": int, "not_found": int}.

    progress_cb(done: int, total: int) is called after each track processed.
    """
    existing_ids = set(TrackLyrics.objects.values_list("track_id", flat=True))
    tracks = list(
        Track.objects.filter(saved__isnull=False)
        .exclude(id__in=existing_ids)
        .select_related("album")
        .prefetch_related("artists")
    )

    if not tracks:
        return {"saved": 0, "not_found": 0}

    total = len(tracks)
    saved = 0
    not_found = 0
    done = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=_WORKERS) as executor:
        futures = {executor.submit(_fetch_one, t): t for t in tracks}
        for future in as_completed(futures):
            result = future.result()
            with lock:
                if result == "saved":
                    saved += 1
                else:
                    not_found += 1
                done += 1
                if progress_cb:
                    progress_cb(done, total)

    return {"saved": saved, "not_found": not_found}


class Command(BaseCommand):
    help = "Fetch and store lyrics for all saved tracks not yet in DB."

    def handle(self, *args, **options):
        existing_ids = set(TrackLyrics.objects.values_list("track_id", flat=True))
        pending_count = (
            Track.objects.filter(saved__isnull=False)
            .exclude(id__in=existing_ids)
            .count()
        )

        if not pending_count:
            self.stdout.write("No pending tracks — lyrics are up to date.")
            return

        self.stdout.write(f"{pending_count} tracks need lyrics.")

        def progress_cb(done, total):
            if done % 50 == 0 or done == total:
                self.stdout.write(f"  [{done}/{total}]")

        stats = run_sync(progress_cb=progress_cb)
        self.stdout.write(
            self.style.SUCCESS(f"Done. Saved {stats['saved']} records.") +
            (f" {stats['not_found']} tracks not found." if stats["not_found"] else "")
        )
