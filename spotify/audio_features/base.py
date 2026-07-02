"""
Base types for audio features providers.

AudioFeaturesResult — plain dataclass; the common currency passed between
                      providers, the pipeline, and the management command.

AudioFeaturesProvider — Protocol (structural subtyping).  Any object that
                        exposes `name: str` and `fetch(...)` satisfies it;
                        no inheritance required.  This makes it trivial to
                        add or swap providers without touching shared code.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class AudioFeaturesResult:
    track_id: str
    acousticness: float
    danceability: float
    energy: float
    instrumentalness: float
    key: int
    liveness: float
    loudness: float
    mode: int
    speechiness: float
    tempo: float
    time_signature: int
    valence: float


@runtime_checkable
class AudioFeaturesProvider(Protocol):
    """Structural protocol for audio-features providers.

    Implement `name` and `fetch` — no base class needed.
    """

    name: str

    def fetch(self, track_ids: list[str]) -> dict[str, AudioFeaturesResult]:
        """Return features for as many of the given Spotify track IDs as possible.

        Keys of the returned dict are a subset of `track_ids`.
        IDs that could not be resolved are simply absent from the result.
        """
        ...
