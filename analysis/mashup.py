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


_BAR_RULES: dict[str, list[tuple]] = {
    "energy":           [(0.1, "green", "Matched energy — smooth transition"), (0.2, "amber", "Slight energy shift — works for buildup/breakdown"), (1.0, "red", "Energy clash — jarring without EQing")],
    "danceability":     [(0.1, "green", "Matched groove — floor stays full"), (0.2, "amber", "Slight groove difference — minor disruption"), (1.0, "red", "Groove mismatch — crowd energy may break")],
    "valence":          [(0.15, "green", "Similar mood — consistent vibe"), (0.3, "amber", "Moderate mood shift — works as contrast"), (1.0, "red", "Strong mood contrast — intentional or jarring")],
    "acousticness":     [(0.2, "green", "Similar acoustic texture"), (0.4, "amber", "Noticeable texture shift"), (1.0, "red", "Very different textures — stark contrast")],
    "instrumentalness": [(0.2, "green", "Similar vocal/instrumental balance"), (0.4, "amber", "Different vocal presence — may clash"), (1.0, "red", "One is vocal-heavy, one is not")],
    "liveness":         [(0.2, "green", "Similar live/studio feel"), (0.4, "amber", "Noticeable difference in live feel"), (1.0, "red", "One sounds live, one is studio — texture clash")],
    "speechiness":      [(0.1, "green", "Similar speech content"), (0.3, "amber", "Different speech density"), (1.0, "red", "Very different vocal/speech texture")],
}
_LOUDNESS_RULES = [(2.0, "green", "Matched loudness — no gain riding needed"), (5.0, "amber", "Moderate gap — adjust gain"), (99.0, "red", "Large loudness gap — significant gain adjustment needed")]
_BPM_RULES = [(5.0, "green", "Perfect BPM match — will blend seamlessly"), (10.0, "amber", "Close BPM — minor pitch-shifting may help"), (20.0, "amber", "Noticeable BPM gap — beatmatching needed"), (999.0, "red", "Large BPM gap — consider half-time or double-time")]


def _classify(diff: float, rules: list[tuple]) -> tuple[str, str]:
    for threshold, color, tip in rules:
        if diff <= threshold:
            return color, tip
    return rules[-1][1], rules[-1][2]


def compute_pairwise_compat(af1: "AudioFeatures", af2: "AudioFeatures") -> dict:
    bpm_diff_pct = _bpm_diff_pct(af1.tempo, af2.tempo)
    kc = _key_compat(af1.key, af1.mode, af2.key, af2.mode)
    c1_num, c1_let = _camelot(af1.key, af1.mode)
    c2_num, c2_let = _camelot(af2.key, af2.mode)

    energy_diff = abs(af1.energy - af2.energy)
    dance_diff = abs(af1.danceability - af2.danceability)
    valence_diff = abs(af1.valence - af2.valence)
    bpm_score = max(0.0, 40.0 - bpm_diff_pct * 2)
    key_score = {"perfect": 35, "compatible": 25, "incompatible": 0}[kc]
    score = round(bpm_score + key_score + (1 - energy_diff) * 10 + (1 - dance_diff) * 8 + (1 - valence_diff) * 7)

    key_tips = {
        "perfect": f"Perfect key match ({c1_num}{c1_let} = {c2_num}{c2_let}) — fully harmonic",
        "compatible": f"Compatible keys ({c1_num}{c1_let} / {c2_num}{c2_let}) — Camelot-adjacent, minor key tension",
        "incompatible": f"Incompatible keys ({c1_num}{c1_let} / {c2_num}{c2_let}) — will clash harmonically",
    }

    bar_features = []
    for field in ["energy", "danceability", "valence", "acousticness", "instrumentalness", "liveness", "speechiness"]:
        v1, v2 = getattr(af1, field), getattr(af2, field)
        diff = abs(v1 - v2)
        color, tip = _classify(diff, _BAR_RULES[field])
        sign = "+" if v2 > v1 else ("-" if v1 > v2 else "±")
        bar_features.append({
            "key": field,
            "label": field.capitalize(),
            "val1": round(v1, 2),
            "val2": round(v2, 2),
            "diff_display": f"{sign}{diff:.2f}" if diff else "±0.00",
            "color": color,
            "tooltip": tip,
        })

    loudness_diff = abs(af1.loudness - af2.loudness)
    ld_color, ld_tip = _classify(loudness_diff, _LOUDNESS_RULES)
    bpm_color, bpm_tip = _classify(bpm_diff_pct, _BPM_RULES)
    ts_same = af1.time_signature == af2.time_signature

    numeric_features = [
        {
            "key": "tempo", "label": "BPM",
            "val1": f"{af1.tempo:.1f}", "val2": f"{af2.tempo:.1f}",
            "diff_display": f"{bpm_diff_pct:.1f}%",
            "color": bpm_color, "tooltip": bpm_tip,
        },
        {
            "key": "loudness", "label": "Loudness",
            "val1": f"{af1.loudness:.1f} dB", "val2": f"{af2.loudness:.1f} dB",
            "diff_display": f"{loudness_diff:.1f} dB",
            "color": ld_color, "tooltip": ld_tip,
        },
        {
            "key": "key", "label": "Key",
            "val1": f"{c1_num}{c1_let}", "val2": f"{c2_num}{c2_let}",
            "diff_display": kc.capitalize(),
            "color": {"perfect": "green", "compatible": "amber", "incompatible": "red"}[kc],
            "tooltip": key_tips[kc],
        },
        {
            "key": "time_signature", "label": "Time Sig",
            "val1": f"{af1.time_signature}/4", "val2": f"{af2.time_signature}/4",
            "diff_display": "Match" if ts_same else f"{af1.time_signature} vs {af2.time_signature}",
            "color": "green" if ts_same else "red",
            "tooltip": "Same time signature — rhythmic compatibility" if ts_same else "Different time signatures — complex polyrhythm, difficult mix",
        },
    ]

    return {
        "score": max(0, min(100, score)),
        "key_compat": kc,
        "camelot_1": f"{c1_num}{c1_let}",
        "camelot_2": f"{c2_num}{c2_let}",
        "bar_features": bar_features,
        "numeric_features": numeric_features,
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
