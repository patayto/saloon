import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from django.core.management.base import BaseCommand, CommandError

from analysis.models import TagSuggestion, TrackTags
from spotify.models import AudioFeatures, TrackLyrics
from spotify.templatetags.spotify_extras import duration_ms, key_name

logger = logging.getLogger(__name__)

FREE_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-120b:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "poolside/laguna-xs-2.1:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "poolside/laguna-m.1:free",
    "openai/gpt-oss-20b:free",
]
DEFAULT_MODEL = FREE_MODELS[0]
DEFAULT_API_URL = "https://openrouter.ai/api/v1"
_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# Insertion order = display order (mood → tempo_feel) throughout the app.
ALLOWED = {
    "mood": (
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
        "tender",
        "brooding",
        "yearning",
        "triumphant",
        "sensual",
        "wistful",
        "menacing",
        "carefree",
        "desperate",
        "dreamy",
        "cathartic",
    ),
    "theme": (
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
        "ambition",
        "fame",
        "freedom",
        "growing_up",
        "memory",
        "death",
        "faith_and_doubt",
        "betrayal",
        "desire",
        "home",
        "self_destruction",
        "empowerment",
        "jealousy",
        "forgiveness",
    ),
    "scene": (
        "late_night",
        "road_trip",
        "workout",
        "heartbreak",
        "celebration",
        "morning",
        "introspection",
        "summer",
        "rainy_day",
        "club",
        "house_party",
        "study_focus",
        "winter",
        "slow_dance",
        "breakup_recovery",
        "sunset",
    ),
    "style": (
        "storytelling",
        "confessional",
        "anthemic",
        "poetic",
        "witty",
        "raw",
        "abstract",
        "conversational",
        "cinematic",
        "minimalist",
        "wordplay",
        "spoken_word",
    ),
    "tempo_feel": (
        "driving",
        "floaty",
        "swaying",
        "head_nod",
        "frantic",
        "laid_back",
        "pulsing",
        "bouncy",
        "hypnotic",
        "explosive",
    ),
}

# Fingerprint of the vocabulary; changes automatically whenever ALLOWED is edited.
# TrackTags rows stamped with an older hash are "stale" (see --refresh-stale).
VOCAB_HASH = hashlib.sha256(
    ";".join(f"{a}:{t}" for a in sorted(ALLOWED) for t in sorted(ALLOWED[a])).encode()
).hexdigest()[:12]

TAG_SCORE_THRESHOLD = 0.6
MAX_TAGS_PER_AXIS = 6

# Out-of-vocabulary tags the model proposes: recorded as TagSuggestion rows for audit.
SUGGESTION_MIN_SCORE = 0.5
MAX_SUGGESTIONS_PER_AXIS = 3
_SUGGESTION_RE = re.compile(
    r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){0,2}$"
)  # ≤3 words, no sentences

# Throttled/erroring models are skipped by every caller (all threads) until
# their cooldown expires. 429 cooldown = Retry-After when given, else default.
_COOLDOWN_429 = 15
_COOLDOWN_5XX = 10
_cooldowns: dict[
    str, float
] = {}  # model → unix ts; ponytail: unlocked dict, atomic under GIL


class TransientAPIError(Exception):
    """2xx response whose body is an error payload instead of a completion."""

_SYSTEM_PROMPT = (
    "You are a music analyst. Given a track's metadata, audio features, and lyrics, "
    "return a JSON object with exactly five keys: mood, theme, scene, style, tempo_feel. "
    "Each value is an object mapping tags to a confidence score between 0 and 1. Prefer "
    "tags from the allowed values below; if a clearly applicable tag is missing from an "
    "axis, you may add up to 2 extra tags for that axis — a short lowercase snake_case "
    "concept (e.g. vulnerability, slow_burn), never a sentence. Include only tags that "
    "clearly apply (confidence 0.5 or higher) — most tracks warrant 2–4 tags per axis. "
    "Score honestly; do not pad. Return only the JSON object, nothing else.\n\n"
    + "\n".join(f"{axis}: {', '.join(vals)}" for axis, vals in ALLOWED.items())
)

_LYRICS_LIMIT = 2000


def _build_user_message(lyrics_row, af: AudioFeatures | None) -> str:
    track = lyrics_row.track
    artists = ", ".join(a.name for a in track.artists.all())
    genres = []
    for a in track.artists.all():
        genres.extend(a.genres or [])
    genres = list(dict.fromkeys(genres))[:5]  # dedupe, cap at 5

    parts = [f"Track: '{track.name}' by {artists}"]
    if track.album:
        year = (track.album.release_date or "")[:4]
        parts.append(f"Album: '{track.album.name}'" + (f" ({year})" if year else ""))
    parts.append(f"Duration: {duration_ms(track.duration_ms)}")
    if genres:
        parts.append(f"Genres: {', '.join(genres)}")
    if af:
        mode = "major" if af.mode else "minor"
        parts.append(
            "Audio features (0–1 unless noted): "
            f"energy {af.energy:.2f}, valence {af.valence:.2f}, "
            f"danceability {af.danceability:.2f}, acousticness {af.acousticness:.2f}, "
            f"instrumentalness {af.instrumentalness:.2f}, speechiness {af.speechiness:.2f}, "
            f"liveness {af.liveness:.2f}, tempo {af.tempo:.0f} BPM, "
            f"loudness {af.loudness:.1f} dB, key {key_name(af.key)} {mode}, "
            f"time signature {af.time_signature}/4"
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
    body = resp.json()
    if "choices" not in body:
        # OpenRouter (esp. :free models) can return 200 with {"error": {...}}
        raise TransientAPIError(str(body.get("error") or body))
    content = body["choices"][0]["message"]["content"]
    logger.debug("API raw response: %s", content)
    return json.loads(content)


def _call_api_with_retry(
    user_message: str,
    models: list[str],
    api_url: str,
    max_rounds: int = 3,
    attempt_cb=None,
) -> dict:
    """Try each model in order, skipping any still on cooldown.

    A 429/5xx puts the model on cooldown (shared across threads) and moves
    straight to the next model — the model list IS the retry mechanism.
    If a whole round yields nothing, sleep until the earliest cooldown
    expires and go again. Other 4xx errors are re-raised immediately.
    """
    for round_ in range(1, max_rounds + 1):
        for model_idx, model in enumerate(models):
            if _cooldowns.get(model, 0) > time.time():
                continue
            if attempt_cb:
                attempt_cb(model, model_idx + 1, len(models), round_)
            try:
                return _call_api(user_message, model, api_url)
            except requests.exceptions.HTTPError as e:
                code = e.response.status_code
                if code == 429:
                    retry_after = (
                        int(e.response.headers.get("Retry-After", 0)) or _COOLDOWN_429
                    )
                    _cooldowns[model] = time.time() + retry_after
                    logger.warning(
                        "Model %s: 429 (cooldown %ds) — next model", model, retry_after
                    )
                elif 500 <= code < 600:
                    _cooldowns[model] = time.time() + _COOLDOWN_5XX
                    logger.warning(
                        "Model %s: %d (cooldown %ds) — next model",
                        model,
                        code,
                        _COOLDOWN_5XX,
                    )
                else:
                    raise
            except TransientAPIError as e:
                _cooldowns[model] = time.time() + _COOLDOWN_5XX
                logger.warning(
                    "Model %s: error body in 200 (%s) — cooldown %ds, next model",
                    model,
                    e,
                    _COOLDOWN_5XX,
                )
        if round_ < max_rounds:
            wait = min(_cooldowns.get(m, 0) for m in models) - time.time()
            if wait > 120:
                # ponytail: likely a daily-limit Retry-After; fail fast instead
                # of crawling — rerun the command once the limit resets.
                break
            wait = max(wait, 1)
            logger.warning(
                "All models cooling down — sleeping %.0fs (round %d/%d)",
                wait,
                round_,
                max_rounds,
            )
            time.sleep(wait)
    raise CommandError(f"All {len(models)} model(s) exhausted.")


def _normalize(tag) -> str:
    return re.sub(r"[\s\-]+", "_", str(tag).strip().lower())


def _validate(raw: dict) -> tuple[dict, dict]:
    """Split the model response into (tags, strays), each {axis: {tag: score}}.

    tags: known tags scoring ≥ threshold, top MAX_TAGS_PER_AXIS by score per axis.
    strays: out-of-vocabulary suggestions passing minimal hygiene, top
    MAX_SUGGESTIONS_PER_AXIS by score per axis.
    """
    out = {}
    strays = {}
    for axis, allowed in ALLOWED.items():
        vals = raw.get(axis)
        if not isinstance(vals, dict):
            out[axis] = {}
            continue
        known = []
        unknown = []
        for tag, score in vals.items():
            if not isinstance(score, (int, float)):
                continue
            tag = _normalize(tag)
            score = round(float(score), 2)
            if tag in allowed:
                if score >= TAG_SCORE_THRESHOLD:
                    known.append((tag, score))
            elif (
                score >= SUGGESTION_MIN_SCORE
                and len(tag) <= 30
                and _SUGGESTION_RE.fullmatch(tag)
            ):
                unknown.append((tag, score))
        out[axis] = dict(sorted(known, key=lambda ts: -ts[1])[:MAX_TAGS_PER_AXIS])
        if unknown:
            strays[axis] = dict(
                sorted(unknown, key=lambda ts: -ts[1])[:MAX_SUGGESTIONS_PER_AXIS]
            )
    return out, strays


def _ping(model: str, base_url: str) -> None:
    """Verify Ollama is reachable and model is available before bulk run."""
    _call_api("ping — respond with empty JSON {}", model, base_url)


def run_sync(
    model: str = DEFAULT_MODEL,
    api_url: str = DEFAULT_API_URL,
    track_ids: list[str] | None = None,
    fallback_models: list[str] | None = None,
    progress_cb=None,
    attempt_cb=None,
    workers: int = 1,
    limit: int | None = None,
) -> dict:
    """Tag tracks via OpenRouter using lyrics + audio features.

    Skips tracks already tagged for this model_name, instrumental tracks,
    and tracks with no lyrics. On per-track failures, increments errors and
    continues. With fallback_models, cycles through them per-track on 429/5xx.
    With workers > 1, tags that many tracks concurrently (threads share the
    model cooldown map, so one throttled model is skipped by all).

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
        qs = qs.filter(track__saved__isnull=False).order_by("-track__saved__added_at")
        if limit:
            qs = qs[:limit]

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

    logger.info("Skipping Openrouter ping")
    # logger.info("Pinging Openrouter at %s with model %s", api_url, model)
    # _ping(model, api_url)
    # logger.info("Openrouter ping OK")

    models = [model] + [m for m in (fallback_models or []) if m != model]
    lock = threading.Lock()

    def _tag_one(row):
        nonlocal saved, errors, done
        track_label = f"{row.track.name!r} ({row.track_id})"
        logger.info("Tagging %s", track_label)
        try:
            user_msg = _build_user_message(row, af_map.get(row.track_id))
            raw = _call_api_with_retry(user_msg, models, api_url, attempt_cb=attempt_cb)
            tags, strays = _validate(raw)
            logger.info("Tagged %s → %s", track_label, tags)
            if strays:
                logger.info("Suggestions from %s → %s", track_label, strays)
            TrackTags.objects.update_or_create(
                track_id=row.track_id,
                model_name=model,
                defaults={"tags": tags, "vocab_hash": VOCAB_HASH},
            )
            for axis, sugg in strays.items():
                for tag, score in sugg.items():
                    TagSuggestion.objects.update_or_create(
                        track_id=row.track_id,
                        axis=axis,
                        tag=tag,
                        defaults={"score": score, "model_name": model},
                    )
            with lock:
                saved += 1
        except Exception:
            logger.exception("Failed to tag %s", track_label)
            with lock:
                errors += 1
        with lock:
            done += 1
            if progress_cb:
                progress_cb(done, total)

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_tag_one, rows))
    else:
        for row in rows:
            _tag_one(row)

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
            help=(
                f"Model to use via OpenRouter (default: {DEFAULT_MODEL}). "
                "Suggested free models: "
                f"{', '.join(FREE_MODELS)}"
                "Any OpenRouter model slug is accepted."
            ),
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
            help="Re-tag even if tag rows already exist for this model "
            "(library-wide unless --track-id is given).",
        )
        parser.add_argument(
            "--refresh-stale",
            action="store_true",
            help="Re-tag tracks whose tags were computed under an older vocabulary "
            "(vocab_hash mismatch). Useful after editing ALLOWED.",
        )
        parser.add_argument(
            "--retry",
            action="store_true",
            default=False,
            help=(
                "On 429/5xx, cycle through FREE_MODELS as per-track fallbacks. "
                "A throttled model goes on cooldown (Retry-After) and is "
                "skipped until it expires."
            ),
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=5,
            help="Number of tracks to tag concurrently (default: 5; 1 = serial).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            metavar="N",
            help="Process at most N tracks (the most recently saved without tags).",
        )

    def handle(self, *args, **options):
        model = options["model"]
        api_url = options["api_url"]
        track_id = options["track_id"]
        force = options["force"]
        retry = options["retry"]
        refresh_stale = options["refresh_stale"]

        track_ids = [track_id] if track_id else None

        if force:
            stale = TrackTags.objects.filter(model_name=model)
            suggestions = TagSuggestion.objects.filter(model_name=model)
            if track_ids:
                stale = stale.filter(track_id__in=track_ids)
                suggestions = suggestions.filter(track_id__in=track_ids)
            deleted, _ = stale.delete()
            suggestions.delete()
            if deleted:
                self.stdout.write(f"Cleared {deleted} existing tag row(s) (--force).")
        elif refresh_stale:
            stale = TrackTags.objects.filter(model_name=model).exclude(
                vocab_hash=VOCAB_HASH
            )
            if track_ids:
                stale = stale.filter(track_id__in=track_ids)
            deleted, _ = stale.delete()
            self.stdout.write(
                f"Cleared {deleted} stale tag row(s) (--refresh-stale, vocab {VOCAB_HASH})."
            )

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
            model=model,
            api_url=api_url,
            track_ids=track_ids,
            fallback_models=FREE_MODELS if retry else None,
            progress_cb=_progress,
            workers=options["workers"],
            limit=options["limit"],
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
