"""
Temporal mood and genre analytics over the saved-track library.

All functions aggregate over SavedTrack.added_at as the temporal anchor.
Playlist-only tracks (no SavedTrack row) are excluded from all analyses.
"""

from collections import defaultdict

from django.db.models import Avg, Count
from django.db.models.functions import TruncMonth, TruncQuarter, TruncWeek

from spotify.models import SavedTrack

_TRUNC = {
    "week": TruncWeek,
    "month": TruncMonth,
    "quarter": TruncQuarter,
}


def _trunc_fn(granularity: str):
    fn = _TRUNC.get(granularity)
    if fn is None:
        raise ValueError(f"granularity must be one of {list(_TRUNC)}; got {granularity!r}")
    return fn


def _fmt_period(dt) -> str:
    """ISO date string for the start of a period bucket."""
    return dt.strftime("%Y-%m-%d")


def mood_timeline(granularity: str = "month") -> list[dict]:
    """
    Per-period mean energy, valence, and VADER compound score.

    Returns a list of dicts ordered by period:
      {period, mean_energy, mean_valence, mean_vader_compound, track_count}

    mean_vader_compound is None when no TrackSentiment rows exist for the period.
    Only tracks with AudioFeatures are included (tracks without are skipped).
    """
    trunc = _trunc_fn(granularity)
    qs = (
        SavedTrack.objects.filter(track__audio_features__isnull=False)
        .annotate(period=trunc("added_at"))
        .values("period")
        .annotate(
            mean_energy=Avg("track__audio_features__energy"),
            mean_valence=Avg("track__audio_features__valence"),
            mean_vader_compound=Avg("track__sentiment__vader_compound"),
            track_count=Count("id"),
        )
        .order_by("period")
    )
    return [
        {
            "period": _fmt_period(row["period"]),
            "mean_energy": row["mean_energy"],
            "mean_valence": row["mean_valence"],
            "mean_vader_compound": row["mean_vader_compound"],
            "track_count": row["track_count"],
        }
        for row in qs
        if row["period"] is not None
    ]


def russell_circumplex_by_period(granularity: str = "month") -> list[dict]:
    """
    Per-period mean energy and valence for the 2-D Russell circumplex plane.

    Returns a list of dicts ordered by period:
      {period, mean_energy, mean_valence, track_count}

    Only tracks with AudioFeatures are included.
    """
    trunc = _trunc_fn(granularity)
    qs = (
        SavedTrack.objects.filter(track__audio_features__isnull=False)
        .annotate(period=trunc("added_at"))
        .values("period")
        .annotate(
            mean_energy=Avg("track__audio_features__energy"),
            mean_valence=Avg("track__audio_features__valence"),
            track_count=Count("id"),
        )
        .order_by("period")
    )
    return [
        {
            "period": _fmt_period(row["period"]),
            "mean_energy": row["mean_energy"],
            "mean_valence": row["mean_valence"],
            "track_count": row["track_count"],
        }
        for row in qs
        if row["period"] is not None
    ]


def genre_timeline(granularity: str = "quarter") -> dict[str, dict[str, int]]:
    """
    Per-period genre counts, suitable for stacked area charts.

    Returns {period_str: {genre: count}} ordered by period.
    Genres are exploded from Artist.genres (JSON list) for each saved track.
    Each track contributes its unique genres once per period bucket
    (duplicates across multiple artists on the same track are collapsed).
    """
    trunc = _trunc_fn(granularity)

    # One row per (saved_track, artist); genres is the full JSON list for that artist.
    # We pull period + track id + genres together so we can deduplicate per track.
    qs = (
        SavedTrack.objects.annotate(period=trunc("added_at"))
        .values("id", "period", "track__artists__genres")
        .order_by("period")
    )

    # Collect genres per (period, saved_track_id) to avoid double-counting a genre
    # that appears on multiple artists for the same track.
    per_track: dict[tuple, set] = defaultdict(set)
    for row in qs:
        period_dt = row["period"]
        if period_dt is None:
            continue
        key = (_fmt_period(period_dt), row["id"])
        genres: list = row["track__artists__genres"] or []
        per_track[key].update(genres)

    # Aggregate into {period: {genre: count}}
    result: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (period_str, _track_id), genres in per_track.items():
        for genre in genres:
            result[period_str][genre] += 1

    return {p: dict(counts) for p, counts in sorted(result.items())}
