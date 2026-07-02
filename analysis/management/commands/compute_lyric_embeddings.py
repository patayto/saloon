import os

import numpy as np
import requests
from django.core.management.base import BaseCommand, CommandError

from analysis.models import LyricEmbedding
from spotify.models import TrackLyrics

DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
BATCH_SIZE = 64
# Approximate character threshold for verse-chunking (~ 512 tokens at ~4 chars/token)
LONG_LYRICS_CHARS = 2048


def _chunk_by_verse(text: str) -> list[str]:
    """Split lyrics on blank lines; return non-empty verse strings."""
    return [v.strip() for v in text.split("\n\n") if v.strip()]


def _embed_batch(texts: list[str], model: str, base_url: str) -> list[list[float]]:
    """Call Ollama /api/embed for a batch of texts. Raises on HTTP errors."""
    url = f"{base_url}/api/embed"
    try:
        resp = requests.post(url, json={"model": model, "input": texts}, timeout=120)
    except requests.ConnectionError:
        raise CommandError(
            f"Cannot reach Ollama at {base_url}. Is it running? (ollama serve)"
        )
    if resp.status_code == 404:
        raise CommandError(
            f"Model '{model}' not found in Ollama. Pull it first: ollama pull {model}"
        )
    resp.raise_for_status()
    return resp.json()["embeddings"]


def _embed_lyrics(text: str, model: str, base_url: str) -> np.ndarray:
    """Embed one track's lyrics, chunking by verse if text is long, mean-pooling."""
    if len(text) > LONG_LYRICS_CHARS:
        verses = _chunk_by_verse(text)
        if not verses:
            verses = [text[:LONG_LYRICS_CHARS]]
        embeddings = _embed_batch(verses, model, base_url)
        vec = np.mean(np.array(embeddings, dtype="float32"), axis=0)
    else:
        vec = np.array(_embed_batch([text], model, base_url)[0], dtype="float32")
    return vec


def run_sync(
    model: str = DEFAULT_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    track_ids: list[str] | None = None,
    progress_cb=None,
) -> dict:
    """Compute and upsert LyricEmbedding rows via Ollama for tracks with lyrics.

    If track_ids is given, restricts to that set; otherwise processes all
    saved tracks with lyrics that don't already have an embedding for this model.

    Short lyrics (<=LONG_LYRICS_CHARS) are batched BATCH_SIZE at a time into a
    single Ollama call. Long lyrics are verse-chunked and sent individually.

    Returns:
        {
            "model": str,
            "dimensions": int | None,
            "saved": int,
            "skipped_existing": int,
            "skipped_no_lyrics": int,
        }
    """
    existing_ids = set(
        LyricEmbedding.objects.filter(model_name=model).values_list(
            "track_id", flat=True
        )
    )
    qs = (
        TrackLyrics.objects.select_related("track")
        .filter(instrumental=False)
        .exclude(track_id__in=existing_ids)
    )
    if track_ids is not None:
        qs = qs.filter(track_id__in=track_ids)
    else:
        qs = qs.filter(track__saved__isnull=False)

    rows = list(qs)
    if not rows:
        return {
            "model": model,
            "dimensions": None,
            "saved": 0,
            "skipped_existing": 0,
            "skipped_no_lyrics": 0,
        }

    total = len(rows)
    saved = 0
    skipped_no_lyrics = 0
    skipped_existing = 0
    dimensions = None
    done = 0

    # Verify Ollama is reachable before starting (cheap HEAD-style check)
    _embed_batch(["ping"], model, ollama_url)

    # Separate short and long lyrics rows (skip empty)
    short_rows = []
    long_rows = []
    empty_rows = []
    for row in rows:
        text = (row.plain_lyrics or "").strip()
        if not text:
            empty_rows.append(row)
        elif len(text) > LONG_LYRICS_CHARS:
            long_rows.append((row, text))
        else:
            short_rows.append((row, text))

    skipped_no_lyrics = len(empty_rows)
    done += len(empty_rows)
    if progress_cb and empty_rows:
        progress_cb(done, total)

    # Process short lyrics in batches
    for batch_start in range(0, len(short_rows), BATCH_SIZE):
        batch = short_rows[batch_start : batch_start + BATCH_SIZE]
        texts = [text for _, text in batch]
        embeddings = _embed_batch(texts, model, ollama_url)
        for (row, _), emb in zip(batch, embeddings):
            vec = np.array(emb, dtype="float32")
            if dimensions is None:
                dimensions = vec.shape[0]
            LyricEmbedding.objects.update_or_create(
                track_id=row.track_id,
                model_name=model,
                defaults={"dimensions": vec.shape[0], "embedding": vec.tobytes()},
            )
            saved += 1
            done += 1
        if progress_cb:
            progress_cb(done, total)

    # Process long lyrics individually (verse-chunked)
    for row, text in long_rows:
        vec = _embed_lyrics(text, model, ollama_url)
        if dimensions is None:
            dimensions = vec.shape[0]
        LyricEmbedding.objects.update_or_create(
            track_id=row.track_id,
            model_name=model,
            defaults={"dimensions": vec.shape[0], "embedding": vec.tobytes()},
        )
        saved += 1
        done += 1
        if progress_cb:
            progress_cb(done, total)

    return {
        "model": model,
        "dimensions": dimensions,
        "saved": saved,
        "skipped_existing": skipped_existing,
        "skipped_no_lyrics": skipped_no_lyrics,
    }


class Command(BaseCommand):
    help = "Compute lyric embeddings via Ollama for saved tracks not yet embedded."

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            default=DEFAULT_MODEL,
            help=f"Ollama model to use for embeddings (default: {DEFAULT_MODEL})",
        )
        parser.add_argument(
            "--ollama-url",
            default=DEFAULT_OLLAMA_URL,
            help=f"Ollama base URL (default: {DEFAULT_OLLAMA_URL})",
        )

    def handle(self, *args, **options):
        model = options["model"]
        ollama_url = options["ollama_url"]

        existing_ids = set(
            LyricEmbedding.objects.filter(model_name=model).values_list(
                "track_id", flat=True
            )
        )
        pending_count = (
            TrackLyrics.objects.filter(instrumental=False, track__saved__isnull=False)
            .exclude(track_id__in=existing_ids)
            .count()
        )

        if not pending_count:
            self.stdout.write(
                f"No pending tracks — embeddings up to date for '{model}'."
            )
            return

        self.stdout.write(
            f"{pending_count} tracks need embedding with '{model}' via {ollama_url}."
        )

        def _progress(done, total):
            pct = int(done / total * 100) if total else 100
            bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
            print(f"\r  [{bar}] {pct:3d}%  {done}/{total}", end="", flush=True)

        stats = run_sync(model=model, ollama_url=ollama_url, progress_cb=_progress)
        print()  # newline after progress bar

        msg = self.style.SUCCESS(
            f"Done. Saved {stats['saved']} embeddings ({stats['dimensions']} dims)."
        )
        if stats["skipped_no_lyrics"]:
            msg += f" {stats['skipped_no_lyrics']} skipped (empty lyrics)."
        self.stdout.write(msg)
