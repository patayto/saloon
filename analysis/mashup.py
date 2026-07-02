import math
import numpy as np
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

from spotify.models import AudioFeatures

# ponytail: module-level cache keyed by af count; rebuilds on restart or when count changes
_cache: dict = {}

# Camelot wheel positions indexed by Spotify key (0=C … 11=B)
CAMELOT_MAJOR = [8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1]
CAMELOT_MINOR = [5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10]


def _camelot(key: int, mode: int) -> tuple[int, str]:
    table = CAMELOT_MAJOR if mode == 1 else CAMELOT_MINOR
    return table[key % 12], ("B" if mode == 1 else "A")


def _key_compat(ak: int, am: int, bk: int, bm: int) -> str:
    a_num, a_letter = _camelot(ak, am)
    b_num, b_letter = _camelot(bk, bm)
    diff = abs(a_num - b_num)
    adjacent = min(diff, 12 - diff) <= 1
    if a_num == b_num and a_letter == b_letter:
        return "perfect"
    if a_letter == b_letter and adjacent:
        return "compatible"
    if a_num == b_num:  # relative major/minor pair
        return "compatible"
    return "incompatible"


def _bpm_diff_pct(a: float, b: float) -> float:
    return round(abs(a - b) / a * 100, 1) if a > 0 else 0.0


def _build() -> dict | None:
    afs = list(
        AudioFeatures.objects
        .select_related("track", "track__album")
        .prefetch_related("track__artists")
        .all()
    )
    if len(afs) < 2:
        return None

    # Circular key encoding so C (0) and B (11) stay adjacent on the circle of fifths
    X = np.array([[
        af.energy, af.danceability, af.valence, af.acousticness,
        af.instrumentalness, af.loudness, af.speechiness, af.liveness, af.tempo,
        math.sin(2 * math.pi * af.key / 12),
        math.cos(2 * math.pi * af.key / 12),
        float(af.mode),
    ] for af in afs], dtype=np.float64)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    nn = NearestNeighbors(n_neighbors=min(51, len(afs)), metric="cosine", algorithm="brute")
    nn.fit(X_scaled)

    return {
        "nn": nn,
        "X_scaled": X_scaled,
        "afs": afs,
        "idx_map": {af.track_id: i for i, af in enumerate(afs)},
    }


def lyric_similarity(track_id: str, candidate_ids: list[str], model_name: str = "nomic-embed-text") -> dict[str, float]:
    from analysis.models import LyricEmbedding
    rows = LyricEmbedding.objects.filter(
        track_id__in=[track_id, *candidate_ids], model_name=model_name
    )
    embeds = {r.track_id: np.frombuffer(bytes(r.embedding), dtype=np.float32) for r in rows}
    if track_id not in embeds:
        return {}
    q = embeds.pop(track_id)
    q = q / (np.linalg.norm(q) + 1e-9)
    candidate_set = set(candidate_ids)
    return {
        cid: round(float(np.dot(q, v / (np.linalg.norm(v) + 1e-9))), 3)
        for cid, v in embeds.items() if cid in candidate_set
    }


def get_candidates(track_id: str, n: int = 50) -> list[dict]:
    current_count = AudioFeatures.objects.count()
    if _cache.get("af_count") != current_count:
        _cache.update({"data": _build(), "af_count": current_count})

    data = _cache.get("data")
    if not data:
        return []

    idx = data["idx_map"].get(track_id)
    if idx is None:
        return []

    target_af = data["afs"][idx]
    distances, indices = data["nn"].kneighbors(
        data["X_scaled"][idx : idx + 1],
        n_neighbors=min(n + 1, len(data["afs"])),
    )

    results = []
    for dist, i in zip(distances[0], indices[0]):
        af = data["afs"][i]
        if af.track_id == track_id:
            continue
        num, letter = _camelot(af.key, af.mode)
        results.append({
            "track": af.track,
            "af": af,
            "similarity": round(1 - float(dist), 3),
            "bpm_diff_pct": _bpm_diff_pct(target_af.tempo, af.tempo),
            "key_compat": _key_compat(target_af.key, target_af.mode, af.key, af.mode),
            "camelot": f"{num}{letter}",
        })
        if len(results) >= n:
            break

    return results
