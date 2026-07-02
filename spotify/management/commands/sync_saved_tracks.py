import requests
from django.core.management.base import BaseCommand
from django.db.models import Max
from django.utils.dateparse import parse_datetime

from spotify.auth import get_access_token
from spotify.importers import persist_track_entry
from spotify.pipeline import enrich_tracks

API_URL = "https://api.spotify.com/v1/me/tracks"
PAGE_SIZE = 50


def run_sync(progress_cb=None) -> dict:
    """Sync saved tracks from the Spotify API.

    Returns {"synced": int, "enrichment": {"audio_features": {...}, "lyrics": {...}}}.

    progress_cb(done: int, total: int) is called after each track persisted.
    total is 0 until the first API page is fetched (unknown upfront).
    """
    latest_added_at = SavedTrack_latest()

    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    created_count = 0
    new_track_ids: list[str] = []
    page = 0
    stop = False

    while not stop:
        resp = requests.get(
            API_URL,
            headers=headers,
            params={"limit": PAGE_SIZE, "offset": page * PAGE_SIZE},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])

        if not items:
            break

        for entry in items:
            if not entry or not entry.get("track"):
                continue

            added_at = parse_datetime(entry["added_at"])

            if latest_added_at and added_at <= latest_added_at:
                stop = True
                break

            saved_track, created = persist_track_entry(entry)
            if created:
                created_count += 1
                new_track_ids.append(saved_track.track_id)
                if progress_cb:
                    progress_cb(created_count, 0)

        if data.get("next") is None:
            break

        page += 1

    enrichment = enrich_tracks(new_track_ids)
    return {"synced": created_count, "enrichment": enrichment}


class Command(BaseCommand):
    help = "Sync saved tracks from the Spotify API (delta: only fetches tracks newer than the latest in DB)"

    def handle(self, *args, **options):
        latest_added_at = SavedTrack_latest()
        if latest_added_at:
            self.stdout.write(f"Latest saved track in DB: {latest_added_at.isoformat()}")
        else:
            self.stdout.write("No tracks in DB — will fetch everything.")

        result = run_sync()
        self.stdout.write(
            self.style.SUCCESS(f"Done. New tracks synced: {result['synced']}")
        )


def SavedTrack_latest():
    from spotify.models import SavedTrack
    result = SavedTrack.objects.aggregate(latest=Max("added_at"))
    return result["latest"]
