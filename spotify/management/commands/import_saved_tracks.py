import json
from pathlib import Path

from django.core.management.base import BaseCommand

from spotify.importers import persist_track_entry


DATA_PATH = Path(__file__).resolve().parents[3] / "spotify" / "data" / "my_saved_tracks.json"


class Command(BaseCommand):
    help = "Import saved tracks from spotify/data/my_saved_tracks.json into the database"

    def handle(self, *args, **options):
        with open(DATA_PATH) as f:
            payload = json.load(f)

        tracks = payload["tracks"]
        total = len(tracks)
        created_count = 0
        skipped_count = 0

        for i, entry in enumerate(tracks, 1):
            _, created = persist_track_entry(entry)
            if created:
                created_count += 1
            else:
                skipped_count += 1

            if i % 500 == 0:
                self.stdout.write(f"  Processed {i}/{total}...")

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created: {created_count}, Skipped (already exists): {skipped_count}"
            )
        )
