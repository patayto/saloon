import json
import logging
import os

import requests
from django.core.management.base import BaseCommand, CommandError

from analysis.models import TrackTags
from spotify.models import AudioFeatures, TrackLyrics

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
DEFAULT_API_URL = "https://openrouter.ai/api/v1"
_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

ALLOWED = {
    "mood": {
        "melancholic",
        "euphoric",
        "nostalgic",
        "defiant",
        "peaceful",
        "anxious",
        "romantic",
        "angry",
        "hopeful",
        "bittersweet",
        "playful",
        "dark",
        "uplifting",
    },
    "theme": {
        "love",
        "loss",
        "identity",
        "social_commentary",
        "relationships",
        "nature",
        "party",
        "spirituality",
        "family",
        "escapism",
        "resilience",
        "loneliness",
        "political",
    },
    "scene": {
        "late_night",
        "road_trip",
        "workout",
        "heartbreak",
        "celebration",
        "morning",
        "introspection",
        "summer",
    },
}

_SYSTEM_PROMPT = (
    "You are a music analyst. Given a track's metadata and lyrics, return a JSON object "
    "with exactly three keys: mood, theme, scene. Each value is a list of 1–3 tags chosen "
    "strictly from the allowed values below. Return only the JSON object, nothing else.\n\n"
    "mood: melancholic, euphoric, nostalgic, defiant, peaceful, anxious, romantic, angry, "
    "hopeful, bittersweet, playful, dark, uplifting\n"
    "theme: love, loss, identity, social_commentary, relationships, nature, party, "
    "spirituality, family, escapism, resilience, loneliness, political\n"
    "scene: late_night, road_trip, workout, heartbreak, celebration, morning, introspection, summer"
)

_LYRICS_LIMIT = 2000


def _energy_label(v: float) -> str:
    if v >= 0.7:
        return "high"
    if v >= 0.4:
        return "medium"
    return "low"


def _valence_label(v: float) -> str:
    if v >= 0.6:
        return "positive"
    if v >= 0.35:
        return "neutral"
    return "negative"


def _build_user_message(lyrics_row, af: AudioFeatures | None) -> str:
    track = lyrics_row.track
    artists = ", ".join(a.name for a in track.artists.all())
    genres = []
    for a in track.artists.all():
        genres.extend(a.genres or [])
    genres = list(dict.fromkeys(genres))[:5]  # dedupe, cap at 5

    parts = [f"Track: '{track.name}' by {artists}"]
    if genres:
        parts.append(f"Genres: {', '.join(genres)}")
    if af:
        parts.append(
            f"Energy: {af.energy:.2f} ({_energy_label(af.energy)}), "
            f"Valence: {af.valence:.2f} ({_valence_label(af.valence)}), "
            f"Tempo: {af.tempo:.0f} BPM, "
            f"Danceability: {af.danceability:.2f}"
        )
    lyrics = (lyrics_row.plain_lyrics or "").strip()
    if lyrics:
        parts.append(f"Lyrics:\n{lyrics[:_LYRICS_LIMIT]}")
    return "\n".join(parts)


def _call_api(user_message: str, model: str, api_url: str) -> dict:
    url = f"{api_url}/chat/completions"
    payload = {
        "model": model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    }
    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
    }
    logger.debug("API request → model=%s url=%s", model, url)
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
    except requests.ConnectionError:
        raise CommandError(f"Cannot reach API at {api_url}.")
    if resp.status_code == 401:
        raise CommandError("API key missing or invalid (OPENROUTER_API_KEY).")
    if resp.status_code == 404:
        raise CommandError(f"Model '{model}' not found.")
    resp.raise_for_status()
    try:
        content = resp.json()["choices"][0]["message"]["content"]
        logger.debug("API raw response: %s", content)
        return json.loads(content)
    except KeyError as ke:
        logger.error(f"Invalid response, failed to find '{str(ke)}' key: {resp.json()}")
        raise ke


def _validate(raw: dict) -> dict:
    """Keep only known tag values; return empty list for missing/invalid axes."""
    return {
        axis: [v for v in raw.get(axis, []) if v in allowed]
        for axis, allowed in ALLOWED.items()
    }


def _ping(model: str, base_url: str) -> None:
    """Verify Ollama is reachable and model is available before bulk run."""
    _call_api("ping — respond with empty JSON {}", model, base_url)


def run_sync(
    model: str = DEFAULT_MODEL,
    api_url: str = DEFAULT_API_URL,
    track_ids: list[str] | None = None,
    progress_cb=None,
) -> dict:
    """Tag tracks via Ollama using lyrics + audio features.

    Skips tracks already tagged for this model_name, instrumental tracks,
    and tracks with no lyrics. On per-track parse failures, increments errors
    and continues.

    Returns: {"model", "saved", "skipped_existing", "skipped_no_lyrics", "errors"}
    """
    existing_ids = set(
        TrackTags.objects.filter(model_name=model).values_list("track_id", flat=True)
    )
    qs = (
        TrackLyrics.objects.select_related("track__album")
        .prefetch_related("track__artists")
        .filter(instrumental=False)
        .exclude(track_id__in=existing_ids)
    )
    if track_ids is not None:
        qs = qs.filter(track_id__in=track_ids)
    else:
        qs = qs.filter(track__saved__isnull=False)

    rows = list(qs)
    empty = [r for r in rows if not (r.plain_lyrics or "").strip()]
    rows = [r for r in rows if (r.plain_lyrics or "").strip()]

    skipped_existing = (
        len(existing_ids)
        if track_ids is None
        else sum(1 for tid in (track_ids or []) if tid in existing_ids)
    )
    skipped_no_lyrics = len(empty)
    saved = 0
    errors = 0
    total = len(rows) + len(empty)
    done = len(empty)

    logger.info(
        "run_sync: %d tracks to tag, %d skipped (existing), %d skipped (no lyrics), model=%s",
        len(rows),
        skipped_existing,
        skipped_no_lyrics,
        model,
    )

    if not rows:
        return {
            "model": model,
            "saved": 0,
            "skipped_existing": skipped_existing,
            "skipped_no_lyrics": skipped_no_lyrics,
            "errors": 0,
        }

    # Pre-fetch audio features keyed by track_id
    af_map = {
        af.track_id: af
        for af in AudioFeatures.objects.filter(track_id__in=[r.track_id for r in rows])
    }

    logger.info("Pinging Ollama at %s with model %s", api_url, model)
    _ping(model, api_url)
    logger.info("Ollama ping OK")

    for row in rows:
        track_label = f"{row.track.name!r} ({row.track_id})"
        logger.info("Tagging %s", track_label)
        try:
            user_msg = _build_user_message(row, af_map.get(row.track_id))
            raw = _call_api(user_msg, model, api_url)
            tags = _validate(raw)
            logger.info(
                "Tagged %s → mood=%s theme=%s scene=%s",
                track_label,
                tags.get("mood"),
                tags.get("theme"),
                tags.get("scene"),
            )
            TrackTags.objects.update_or_create(
                track_id=row.track_id,
                model_name=model,
                defaults={"tags": tags},
            )
            saved += 1
        except Exception:
            logger.exception("Failed to tag %s", track_label)
            errors += 1
        done += 1
        if progress_cb:
            progress_cb(done, total)

    logger.info("run_sync complete: saved=%d errors=%d", saved, errors)

    return {
        "model": model,
        "saved": saved,
        "skipped_existing": skipped_existing,
        "skipped_no_lyrics": skipped_no_lyrics,
        "errors": errors,
    }


class Command(BaseCommand):
    help = "Tag saved tracks via a local Ollama model using lyrics + audio features."

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            default=DEFAULT_MODEL,
            help=f"Ollama model to use (default: {DEFAULT_MODEL})",
        )
        parser.add_argument(
            "--api-url",
            default=DEFAULT_API_URL,
            help=f"API base URL (default: {DEFAULT_API_URL})",
        )
        parser.add_argument(
            "--track-id",
            dest="track_id",
            default=None,
            help="Tag a single track by Spotify ID (skips the rest of the library).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-tag even if a tag row already exists for this model.",
        )

    def handle(self, *args, **options):
        model = options["model"]
        api_url = options["api_url"]
        track_id = options["track_id"]
        force = options["force"]

        track_ids = [track_id] if track_id else None

        if force and track_ids:
            deleted, _ = TrackTags.objects.filter(
                track_id__in=track_ids, model_name=model
            ).delete()
            if deleted:
                self.stdout.write(f"Cleared {deleted} existing tag row(s) (--force).")

        if track_ids:
            self.stdout.write(
                f"Tagging track {track_id!r} with '{model}' via {api_url}."
            )
        else:
            existing_ids = set(
                TrackTags.objects.filter(model_name=model).values_list(
                    "track_id", flat=True
                )
            )
            pending = (
                TrackLyrics.objects.filter(
                    instrumental=False, track__saved__isnull=False
                )
                .exclude(track_id__in=existing_ids)
                .exclude(plain_lyrics="")
                .count()
            )
            if not pending:
                self.stdout.write(f"No pending tracks — tags up to date for '{model}'.")
                return
            self.stdout.write(f"{pending} tracks to tag with '{model}' via {api_url}.")

        def _progress(done, total):
            pct = int(done / total * 100) if total else 100
            bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
            print(f"\r  [{bar}] {pct:3d}%  {done}/{total}", end="", flush=True)

        stats = run_sync(
            model=model, api_url=api_url, track_ids=track_ids, progress_cb=_progress
        )
        print()

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Saved {stats['saved']} tag sets."
                + (
                    f" {stats['skipped_no_lyrics']} skipped (no lyrics)."
                    if stats["skipped_no_lyrics"]
                    else ""
                )
                + (f" {stats['errors']} errors." if stats["errors"] else "")
            )
        )
