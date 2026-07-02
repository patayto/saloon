import itertools
from collections import Counter

import numpy as np
from sklearn.preprocessing import StandardScaler

from analysis.mashup import af_row
from analysis.models import LyricEmbedding
from spotify.models import AudioFeatures, PlaylistTrack, SavedTrack, Track

LYRIC_K, LYRIC_MIN_SIM = 5, 0.75
AUDIO_K, AUDIO_MIN_SIM = 5, 0.80
PLAYLIST_MAX_SIZE, PLAYLIST_MIN_SHARED = 100, 2
ARTIST_CLIQUE_CAP = 20
EDGE_TYPES = ["lyric", "audio", "playlist", "artist"]  # link type = index into this

# ponytail: module-level cache keyed by row counts; rebuilds on restart or when data changes
_cache: dict = {}


def build_graph() -> dict:
    key = (
        SavedTrack.objects.count(),
        AudioFeatures.objects.count(),
        LyricEmbedding.objects.count(),
        PlaylistTrack.objects.count(),
    )
    if _cache.get("key") != key:
        _cache.update({"key": key, "graph": _build()})
    return _cache["graph"]


def _build() -> dict:
    saved = list(
        SavedTrack.objects
        .select_related("track", "track__audio_features", "track__sentiment")
        .prefetch_related("track__artists")
    )
    nodes = []
    idx: dict[str, int] = {}
    for i, st in enumerate(saved):
        t = st.track
        af = getattr(t, "audio_features", None)
        sent = getattr(t, "sentiment", None)
        idx[t.id] = i
        nodes.append({
            "id": i,
            "sid": t.id,
            "name": t.name,
            "artist": ", ".join(a.name for a in t.artists.all()),
            "valence": af.valence if af else None,
            "energy": af.energy if af else None,
            "sentiment": sent.vader_compound if sent else None,
        })

    links = (
        _lyric_edges(idx)
        + _audio_edges(idx)
        + _playlist_edges(idx)
        + _artist_edges(idx)
    )
    return {"nodes": nodes, "links": links, "types": EDGE_TYPES}


def _top_k_sim_edges(
    ids: list[str], X: np.ndarray, k: int, min_sim: float, type_idx: int, idx: dict[str, int]
) -> list[list]:
    """Row-normalise X, take top-k cosine neighbours per row above min_sim, dedup pairs."""
    if len(ids) < 2:
        return []
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    S = (X @ X.T).astype(np.float32)
    np.fill_diagonal(S, -1.0)
    k = min(k, len(ids) - 1)
    top = np.argpartition(-S, k, axis=1)[:, :k]
    pairs = set()
    for i in range(len(ids)):
        for j in top[i]:
            if S[i, j] >= min_sim:
                pairs.add((min(i, int(j)), max(i, int(j))))
    return [
        [idx[ids[a]], idx[ids[b]], type_idx, round(float(S[a, b]), 3)]
        for a, b in pairs
    ]


def _lyric_edges(idx: dict[str, int]) -> list[list]:
    rows = LyricEmbedding.objects.filter(
        model_name="nomic-embed-text", track_id__in=idx.keys()
    ).values_list("track_id", "embedding")
    ids, embs = [], []
    for tid, blob in rows:
        ids.append(tid)
        embs.append(np.frombuffer(bytes(blob), dtype=np.float32))
    if len(ids) < 2:
        return []
    return _top_k_sim_edges(ids, np.vstack(embs), LYRIC_K, LYRIC_MIN_SIM, 0, idx)


def _audio_edges(idx: dict[str, int]) -> list[list]:
    afs = list(AudioFeatures.objects.filter(track_id__in=idx.keys()))
    if len(afs) < 2:
        return []
    X = StandardScaler().fit_transform(np.array([af_row(af) for af in afs], dtype=np.float64))
    return _top_k_sim_edges([af.track_id for af in afs], X, AUDIO_K, AUDIO_MIN_SIM, 1, idx)


def _playlist_edges(idx: dict[str, int]) -> list[list]:
    by_playlist: dict[str, set[int]] = {}
    rows = PlaylistTrack.objects.filter(spotify_track_id__in=idx.keys()).values_list(
        "playlist_id", "spotify_track_id"
    )
    for pid, tid in rows:
        by_playlist.setdefault(pid, set()).add(idx[tid])
    counts: Counter = Counter()
    for members in by_playlist.values():
        # ponytail: giant playlists blow up pair counts and carry little co-membership signal
        if len(members) > PLAYLIST_MAX_SIZE:
            continue
        counts.update(itertools.combinations(sorted(members), 2))
    return [[a, b, 2, w] for (a, b), w in counts.items() if w >= PLAYLIST_MIN_SHARED]


def _artist_edges(idx: dict[str, int]) -> list[list]:
    by_artist: dict[str, set[int]] = {}
    rows = Track.artists.through.objects.filter(track_id__in=idx.keys()).values_list(
        "artist_id", "track_id"
    )
    for aid, tid in rows:
        by_artist.setdefault(aid, set()).add(idx[tid])
    pairs = set()
    for members_set in by_artist.values():
        members = sorted(members_set)
        if len(members) < 2:
            continue
        if len(members) <= ARTIST_CLIQUE_CAP:
            pairs.update(itertools.combinations(members, 2))
        else:
            # ponytail: star topology for prolific artists; a 159-track clique alone is 12.5k edges
            hub = members[0]
            pairs.update((hub, m) for m in members[1:])
    return [[a, b, 3, 1] for a, b in pairs]
