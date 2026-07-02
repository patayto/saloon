from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from django.core.management.base import BaseCommand

from analysis.models import TrackSentiment
from spotify.models import TrackLyrics


def run_sync(track_ids: list[str] | None = None, progress_cb=None) -> dict:
    """Compute and upsert VADER sentiment for tracks with lyrics.

    If track_ids is given, restricts to that set; otherwise processes all
    saved tracks with lyrics that don't already have a TrackSentiment row.

    Skips instrumental tracks and tracks with empty lyrics.

    Returns:
        {
            "saved": int,
            "skipped_no_lyrics": int,
            "skipped_instrumental": int,
        }
    """
    qs = TrackLyrics.objects.select_related("track")
    if track_ids is not None:
        qs = qs.filter(track_id__in=track_ids)
    else:
        existing_ids = set(TrackSentiment.objects.values_list("track_id", flat=True))
        qs = qs.filter(track__saved__isnull=False).exclude(track_id__in=existing_ids)

    lyrics_rows = list(qs)
    total = len(lyrics_rows)
    saved = 0
    skipped_no_lyrics = 0
    skipped_instrumental = 0

    analyzer = SentimentIntensityAnalyzer()

    for i, row in enumerate(lyrics_rows, 1):
        if row.instrumental:
            skipped_instrumental += 1
        elif not row.plain_lyrics.strip():
            skipped_no_lyrics += 1
        else:
            scores = analyzer.polarity_scores(row.plain_lyrics)
            TrackSentiment.objects.update_or_create(
                track_id=row.track_id,
                defaults={
                    "vader_positive": scores["pos"],
                    "vader_negative": scores["neg"],
                    "vader_neutral": scores["neu"],
                    "vader_compound": scores["compound"],
                    "classifier_label": "",
                    "classifier_score": None,
                    "classifier_model": "",
                },
            )
            saved += 1

        if progress_cb:
            progress_cb(i, total)

    return {
        "saved": saved,
        "skipped_no_lyrics": skipped_no_lyrics,
        "skipped_instrumental": skipped_instrumental,
    }


class Command(BaseCommand):
    help = "Compute VADER sentiment for all saved tracks with lyrics not yet analysed."

    def handle(self, *args, **options):
        existing_ids = set(TrackSentiment.objects.values_list("track_id", flat=True))
        pending_count = (
            TrackLyrics.objects
            .filter(track__saved__isnull=False)
            .exclude(track_id__in=existing_ids)
            .count()
        )

        if not pending_count:
            self.stdout.write("No pending tracks — sentiment is up to date.")
            return

        self.stdout.write(f"{pending_count} tracks need sentiment analysis.")
        stats = run_sync()
        self.stdout.write(
            self.style.SUCCESS(f"Done. Saved {stats['saved']} records.") +
            (f" {stats['skipped_instrumental']} instrumental." if stats["skipped_instrumental"] else "") +
            (f" {stats['skipped_no_lyrics']} empty lyrics." if stats["skipped_no_lyrics"] else "")
        )
