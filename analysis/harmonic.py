from typing import Callable

import librosa

_HOP_LENGTH = 512


def score_harmonic_similarity(
    path1: str, path2: str, stage_cb: Callable[[str], None] | None = None
) -> dict:
    """Chromagram + DTW harmonic-rhythm similarity between two local audio files.

    Best used on instrumental stems — chroma extracted from a solo vocal is thin
    and doesn't represent the underlying chord progression.
    """

    def stage(label: str) -> None:
        if stage_cb:
            stage_cb(label)

    stage("Loading audio")
    y1, sr1 = librosa.load(path1, sr=22050)
    y2, sr2 = librosa.load(path2, sr=22050)

    stage("Isolating harmonic content")
    y1_harmonic, _ = librosa.effects.hpss(y1)
    y2_harmonic, _ = librosa.effects.hpss(y2)

    stage("Extracting chromagrams")
    chroma1 = librosa.feature.chroma_cqt(y=y1_harmonic, sr=sr1, hop_length=_HOP_LENGTH)
    chroma2 = librosa.feature.chroma_cqt(y=y2_harmonic, sr=sr2, hop_length=_HOP_LENGTH)

    stage("Aligning with DTW")
    D, wp = librosa.sequence.dtw(X=chroma1, Y=chroma2, metric="cosine")

    normalized_distance = D[-1, -1] / len(wp)
    score = max(0.0, 100.0 - normalized_distance * 100)
    return {"score": round(float(score), 1)}
