"""
Track enrichment pipeline.

Called automatically after any track ingest (library sync, playlist sync) to
fetch audio features and lyrics for newly encountered tracks.
"""
import logging
import tempfile

import requests

from spotify.audio_features.reccobeats import ReccoBeatsProvider
from spotify.deezer import search_track
from spotify.genius import get_lyrics as genius_get_lyrics
from spotify.lrclib import get_lyrics as lrclib_get_lyrics
from spotify.models import AudioFeatures, Track, TrackLyrics

logger = logging.getLogger(__name__)


def enrich_tracks(track_ids: list[str], compute_sentiment: bool = False) -> dict:
    """Fetch audio features and lyrics for the given track IDs.

    Skips tracks that already have audio features / lyrics.

    Args:
        track_ids: Spotify track IDs to process.
        compute_sentiment: If True, also run VADER sentiment analysis on any
            newly available lyrics (phase 3). Off by default.

    Returns:
        {
            "audio_features": {"saved": int, "not_found": int},
            "lyrics": {"saved": int, "not_found": int},
            # only present when compute_sentiment=True:
            "sentiment": {"saved": int, "skipped_no_lyrics": int, "skipped_instrumental": int},
        }
    """
    if not track_ids:
        result: dict = {
            "audio_features": {"saved": 0, "not_found": 0},
            "lyrics": {"saved": 0, "not_found": 0},
        }
        if compute_sentiment:
            result["sentiment"] = {"saved": 0, "skipped_no_lyrics": 0, "skipped_instrumental": 0}
        return result

    result = {
        "audio_features": _enrich_audio_features(track_ids),
        "lyrics": _enrich_lyrics(track_ids),
    }

    try:
        from analysis.management.commands.compute_lyric_embeddings import run_sync as _embed_lyrics
        _embed_lyrics(track_ids=track_ids)
    except Exception:
        logger.warning("Lyric embedding skipped during enrichment (Ollama unavailable?)")

    if compute_sentiment:
        from analysis.management.commands.compute_sentiment import run_sync as compute_sentiment_sync
        result["sentiment"] = compute_sentiment_sync(track_ids=track_ids)

    return result


def _enrich_audio_features(track_ids: list[str], progress_cb=None) -> dict:
    existing_ids = set(
        AudioFeatures.objects.filter(track_id__in=track_ids).values_list("track_id", flat=True)
    )
    pending_ids = [tid for tid in track_ids if tid not in existing_ids]

    total = len(track_ids)
    done = len(existing_ids)
    if progress_cb:
        progress_cb(done, total)

    if not pending_ids:
        return {"saved": 0, "not_found": 0}

    provider = ReccoBeatsProvider()
    resolved_ids: set[str] = set()
    total_saved = 0

    # Phase 1: ReccoBeats batch lookup
    for batch in provider.fetch_stream(pending_ids):
        to_create = [
            AudioFeatures(
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
            for af in batch.values()
        ]
        AudioFeatures.objects.bulk_create(to_create, ignore_conflicts=True)
        resolved_ids.update(batch.keys())
        total_saved += len(to_create)
        done = len(existing_ids) + len(resolved_ids)
        if progress_cb:
            progress_cb(done, total)

    # Phase 2: Deezer preview → ReccoBeats analysis fallback
    fallback_ids = [tid for tid in pending_ids if tid not in resolved_ids]
    phase2_done = 0
    if fallback_ids:
        tracks = {
            t.id: t
            for t in Track.objects.filter(id__in=fallback_ids).prefetch_related("artists")
        }
        for tid in fallback_ids:
            track = tracks.get(tid)
            if not track:
                phase2_done += 1
                if progress_cb:
                    progress_cb(len(existing_ids) + len(resolved_ids) + phase2_done, total)
                continue
            first_artist = track.artists.first()
            artist_name = first_artist.name if first_artist else ""
            deezer_result = search_track(title=track.name, artist=artist_name)
            if not deezer_result:
                phase2_done += 1
                if progress_cb:
                    progress_cb(len(existing_ids) + len(resolved_ids) + phase2_done, total)
                continue
            try:
                resp = requests.get(deezer_result.preview, timeout=30)
                resp.raise_for_status()
            except requests.RequestException:
                phase2_done += 1
                if progress_cb:
                    progress_cb(len(existing_ids) + len(resolved_ids) + phase2_done, total)
                continue
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=True) as tmp:
                tmp.write(resp.content)
                tmp.flush()
                af = provider.fetch_from_file(tmp.name, tid)
            phase2_done += 1
            if af is None:
                if progress_cb:
                    progress_cb(len(existing_ids) + len(resolved_ids) + phase2_done, total)
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
            total_saved += 1
            if progress_cb:
                progress_cb(len(existing_ids) + len(resolved_ids) + phase2_done, total)

    return {"saved": total_saved, "not_found": len(pending_ids) - total_saved}


def _enrich_lyrics(track_ids: list[str], progress_cb=None) -> dict:
    existing_ids = set(
        TrackLyrics.objects.filter(track_id__in=track_ids).values_list("track_id", flat=True)
    )
    pending = list(
        Track.objects.filter(id__in=track_ids)
        .exclude(id__in=existing_ids)
        .select_related("album")
        .prefetch_related("artists")
    )

    total = len(track_ids)
    done = len(existing_ids)
    if progress_cb:
        progress_cb(done, total)

    if not pending:
        return {"saved": 0, "not_found": 0}

    saved = 0
    not_found = 0

    for track in pending:
        first_artist = track.artists.first()
        artist_name = first_artist.name if first_artist else ""

        lrclib_result = lrclib_get_lyrics(
            track_name=track.name,
            artist_name=artist_name,
            album_name=track.album.name,
            duration_seconds=track.duration_ms // 1000,
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
            saved += 1
        else:
            genius_result = genius_get_lyrics(track_name=track.name, artist_name=artist_name)
            if genius_result is not None:
                TrackLyrics.objects.get_or_create(
                    track_id=track.id,
                    defaults={
                        "instrumental": False,
                        "plain_lyrics": genius_result.plain_lyrics,
                        "synced_lyrics": "",
                    },
                )
                saved += 1
            else:
                not_found += 1

        done += 1
        if progress_cb:
            progress_cb(done, total)

    return {"saved": saved, "not_found": not_found}
