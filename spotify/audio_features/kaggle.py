"""
KaggleDatasetProvider — resolves audio features from the
rodolfofigueroa/spotify-12m-songs Kaggle dataset (~1.2 M tracks).

The dataset uses Spotify track IDs as the primary key (`id` column), so no
ID translation step is required.

kagglehub caches the downloaded CSV under ~/.cache/kagglehub/ — the network
hit only happens once.  Subsequent runs load from disk (a pandas CSV read of
~100 MB, typically 2–4 s).

Dataset columns used
---------------------
id, danceability, energy, key, loudness, mode, speechiness, acousticness,
instrumentalness, liveness, valence, tempo, time_signature

Note: `time_signature` is stored as float64 in the CSV; we cast to int.
      `analysis_url` is absent from the dataset; AudioFeatures.analysis_url
      will be left as an empty string for Kaggle-sourced rows.
"""

from __future__ import annotations

import logging

from .base import AudioFeaturesResult

logger = logging.getLogger(__name__)

_DATASET = "rodolfofigueroa/spotify-12m-songs"
_FILE = "tracks_features.csv"
_USECOLS = [
    "id",
    "acousticness",
    "danceability",
    "energy",
    "instrumentalness",
    "key",
    "liveness",
    "loudness",
    "mode",
    "speechiness",
    "tempo",
    "time_signature",
    "valence",
]


class KaggleDatasetProvider:
    """Audio features from the Kaggle spotify-12m-songs dataset.

    Requires kagglehub and Kaggle API credentials (~/.kaggle/kaggle.json or
    KAGGLE_USERNAME / KAGGLE_KEY environment variables).
    """

    name = "kaggle:rodolfofigueroa/spotify-12m-songs"

    def fetch(self, track_ids: list[str]) -> dict[str, AudioFeaturesResult]:
        try:
            from kagglehub import KaggleDatasetAdapter, dataset_load
        except ImportError as exc:
            raise ImportError(
                "kagglehub is required for KaggleDatasetProvider. "
                "Install it with: uv add 'kagglehub[pandas-datasets]'"
            ) from exc

        logger.info("Loading Kaggle dataset '%s' (cached after first download)…", _DATASET)
        df = dataset_load(
            KaggleDatasetAdapter.PANDAS,
            _DATASET,
            _FILE,
            pandas_kwargs={"usecols": _USECOLS},
        )

        id_set = set(track_ids)
        matched = df[df["id"].isin(id_set)]
        logger.info(
            "Kaggle dataset: matched %d / %d requested tracks",
            len(matched),
            len(track_ids),
        )

        results: dict[str, AudioFeaturesResult] = {}
        for row in matched.itertuples(index=False):
            results[row.id] = AudioFeaturesResult(
                track_id=row.id,
                acousticness=float(row.acousticness),
                danceability=float(row.danceability),
                energy=float(row.energy),
                instrumentalness=float(row.instrumentalness),
                key=int(row.key),
                liveness=float(row.liveness),
                loudness=float(row.loudness),
                mode=int(row.mode),
                speechiness=float(row.speechiness),
                tempo=float(row.tempo),
                time_signature=int(row.time_signature),
                valence=float(row.valence),
            )
        return results
