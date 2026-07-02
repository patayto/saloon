import tempfile

import requests
from django.core.management.base import BaseCommand

from spotify.audio_features.reccobeats import ReccoBeatsProvider
from spotify.deezer import search_track
from spotify.models import AudioFeatures, Track


def run_sync(progress_cb=None) -> dict:
    """Fetch and store audio features for all saved tracks not yet in DB.

    Returns {"saved": int, "not_found": int}.

    progress_cb(done: int, total: int) is called after each batch/track saved.
    """
    existing_ids = set(AudioFeatures.objects.values_list("track_id", flat=True))
    pending_ids = list(
        Track.objects.filter(saved__isnull=False)
        .exclude(id__in=existing_ids)
        .values_list("id", flat=True)
    )

    if not pending_ids:
        return {"saved": 0, "not_found": 0}

    total = len(pending_ids)
    provider = ReccoBeatsProvider()
    resolved_ids: set[str] = set()
    total_saved = 0

    # Phase 1: ReccoBeats batch endpoint
    for batch in provider.fetch_stream(pending_ids):
        to_create = [_make_audio_features(af) for af in batch.values()]
        AudioFeatures.objects.bulk_create(to_create, ignore_conflicts=True)
        resolved_ids.update(batch.keys())
        total_saved += len(to_create)
        if progress_cb:
            progress_cb(total_saved, total)

    # Phase 2: Deezer preview → ReccoBeats analysis for remaining tracks
    fallback_ids = [tid for tid in pending_ids if tid not in resolved_ids]
    if fallback_ids:
        fallback_saved = _run_fallback(fallback_ids, provider, total_saved, total, progress_cb)
        total_saved += fallback_saved

    not_found = len(pending_ids) - total_saved
    return {"saved": total_saved, "not_found": not_found}


def _run_fallback(
    track_ids: list[str],
    provider: ReccoBeatsProvider,
    done_so_far: int = 0,
    total: int = 0,
    progress_cb=None,
) -> int:
    tracks = (
        Track.objects.filter(id__in=track_ids)
        .prefetch_related("artists")
    )
    track_map = {t.id: t for t in tracks}

    saved = 0
    for tid in track_ids:
        track = track_map.get(tid)
        if track is None:
            continue

        artist_name = track.artists.first()
        artist_name = artist_name.name if artist_name else ""

        deezer_result = search_track(title=track.name, artist=artist_name)
        if deezer_result is None:
            continue

        try:
            resp = requests.get(deezer_result.preview, timeout=30)
            resp.raise_for_status()
        except requests.exceptions.RequestException:
            continue

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=True) as tmp:
            tmp.write(resp.content)
            tmp.flush()
            af = provider.fetch_from_file(tmp.name, tid)

        if af is None:
            continue

        AudioFeatures.objects.get_or_create(
            track_id=tid,
            defaults={
                "acousticness": af.acousticness,
                "danceability": af.danceability,
                "energy": af.energy,
                "instrumentalness": af.instrumentalness,
                "key": af.key,
                "liveness": af.liveness,
                "loudness": af.loudness,
                "mode": af.mode,
                "speechiness": af.speechiness,
                "tempo": af.tempo,
                "time_signature": af.time_signature,
                "valence": af.valence,
                "analysis_url": "",
            },
        )
        saved += 1
        if progress_cb:
            progress_cb(done_so_far + saved, total)

    return saved


class Command(BaseCommand):
    help = "Fetch and store audio features for all saved tracks not yet in DB."

    def handle(self, *args, **options):
        existing_ids = set(AudioFeatures.objects.values_list("track_id", flat=True))
        pending_count = (
            Track.objects.filter(saved__isnull=False)
            .exclude(id__in=existing_ids)
            .count()
        )

        if not pending_count:
            self.stdout.write("No pending tracks — audio features are up to date.")
            return

        self.stdout.write(f"{pending_count} tracks need audio features.")
        stats = run_sync()
        if stats["saved"] == 0:
            self.stdout.write(self.style.WARNING("No audio features found."))
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Done. Saved {stats['saved']} records.") +
                (f" {stats['not_found']} tracks not found." if stats["not_found"] else "")
            )


def _make_audio_features(af) -> AudioFeatures:
    return AudioFeatures(
        track_id=af.track_id,
        acousticness=af.acousticness,
        danceability=af.danceability,
        energy=af.energy,
        instrumentalness=af.instrumentalness,
        key=af.key,
        liveness=af.liveness,
        loudness=af.loudness,
        mode=af.mode,
        speechiness=af.speechiness,
        tempo=af.tempo,
        time_signature=af.time_signature,
        valence=af.valence,
        analysis_url="",
    )
