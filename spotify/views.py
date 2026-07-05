import logging
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid

logger = logging.getLogger(__name__)

import requests as http_requests
from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import F, Q
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from analysis.management.commands.compute_track_tags import ALLOWED as TAG_ALLOWED
from analysis.models import TrackTags
from spotify.audio_features.reccobeats import ReccoBeatsProvider
from spotify.auth import exchange_code
from spotify.deezer import search_track as deezer_search_track
from spotify.models import (
    AudioFeatures,
    Playlist,
    PlaylistTrack,
    SavedTrack,
    SpotifyToken,
    Track,
    TrackLyrics,
)

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
SCOPE = "user-library-read user-library-modify user-read-private playlist-read-private playlist-read-collaborative"

# In-memory job store for background playlist sync tasks.
# Single-user dev tool; no cleanup needed.
_sync_jobs: dict[str, dict] = {}

# Maps URL param name → ORM field for ordering (library)
SORT_FIELDS = {
    "added_at": "added_at",
    "title": "track__name",
    "album": "track__album__name",
    "duration": "track__duration_ms",
}

# Default sort direction when first clicking a column
DEFAULT_DIR = {
    "added_at": "desc",
    "title": "asc",
    "album": "asc",
    "duration": "desc",
}


# Maps URL param name → ORM field for ordering (audio features)
AF_SORT_FIELDS = {
    "title": "track__name",
    "tempo": "tempo",
    "energy": "energy",
    "danceability": "danceability",
    "valence": "valence",
    "acousticness": "acousticness",
    "instrumentalness": "instrumentalness",
    "liveness": "liveness",
    "speechiness": "speechiness",
    "loudness": "loudness",
    "key": "key",
    "mode": "mode",
}

AF_DEFAULT_DIR = {
    "title": "asc",
    "tempo": "desc",
    "energy": "desc",
    "danceability": "desc",
    "valence": "desc",
    "acousticness": "desc",
    "instrumentalness": "desc",
    "liveness": "desc",
    "speechiness": "desc",
    "loudness": "desc",
    "key": "asc",
    "mode": "asc",
}


def spotify_login(request: HttpRequest) -> HttpResponse:
    """Redirect the user to Spotify's OAuth authorization page."""
    params = urllib.parse.urlencode(
        {
            "client_id": settings.SPOTIFY_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
            "scope": SCOPE,
        }
    )
    return redirect(f"{AUTHORIZE_URL}?{params}")


def spotify_callback(request: HttpRequest) -> HttpResponse:
    """Handle the OAuth callback from Spotify and store tokens."""
    error = request.GET.get("error")
    if error:
        return HttpResponse(f"Authorization denied: {error}", status=400)

    code = request.GET.get("code")
    if not code:
        return HttpResponse("Missing authorization code.", status=400)

    token = exchange_code(code)
    # Fetch the user's Spotify profile and store it on the token
    try:
        resp = http_requests.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {token.access_token}"},
            timeout=10,
        )
        if resp.ok:
            profile = resp.json()
            token.display_name = profile.get("display_name") or profile.get("id", "")
            token.profile_url = profile.get("external_urls", {}).get("spotify", "")
            token.save(update_fields=["display_name", "profile_url"])
    except Exception:
        pass  # profile info is non-critical
    return render(request, "spotify/oauth_complete.html")


def library(request: HttpRequest) -> HttpResponse:
    ctx = _table_context(request)
    user_token = SpotifyToken.objects.filter(
        token_type=SpotifyToken.TOKEN_TYPE_USER
    ).first()
    ctx["spotify_user"] = (
        user_token if (user_token and user_token.display_name) else None
    )
    ctx["af_total_count"] = AudioFeatures.objects.count()
    ctx["playlists_count"] = Playlist.objects.count()
    ctx["tag_options"] = _tag_options()
    return render(request, "spotify/library.html", ctx)


def tracks_table(request: HttpRequest) -> HttpResponse:
    ctx = _table_context(request)
    return render(request, "spotify/partials/tracks_table.html", ctx)


def audio_features_table(request: HttpRequest) -> HttpResponse:
    ctx = _af_table_context(request)
    return render(request, "spotify/partials/audio_features_table.html", ctx)


def track_detail(request: HttpRequest, track_id: str) -> HttpResponse:
    track = get_object_or_404(
        Track.objects.select_related("album").prefetch_related(
            "artists", "album__artists"
        ),
        id=track_id,
    )
    af = AudioFeatures.objects.filter(track_id=track_id).first()
    saved = SavedTrack.objects.filter(track_id=track_id).first()
    lyrics = TrackLyrics.objects.filter(track_id=track_id).first()
    tags = TrackTags.objects.filter(track_id=track_id).first()
    return render(
        request,
        "spotify/partials/track_detail_modal.html",
        {
            "track": track,
            "saved": saved,
            "af": af,
            "lyrics": lyrics,
            "tags": tags,
        },
    )


def fetch_track_audio_features(request: HttpRequest, track_id: str) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    track = get_object_or_404(
        Track.objects.prefetch_related("artists"),
        id=track_id,
    )

    # Already exists — just return it
    af = AudioFeatures.objects.filter(track_id=track_id).first()
    if af:
        return render(
            request,
            "spotify/partials/track_audio_features.html",
            {"af": af, "track": track},
        )

    provider = ReccoBeatsProvider()
    af_result = None
    error = None

    # Phase 1: ReccoBeats batch lookup
    results = provider.fetch([track_id])
    if track_id in results:
        af_result = results[track_id]

    # Phase 2: Deezer preview + ReccoBeats analysis fallback
    if af_result is None:
        first_artist = track.artists.first()
        artist_name = first_artist.name if first_artist else ""
        deezer_result = deezer_search_track(title=track.name, artist=artist_name)
        if deezer_result:
            try:
                resp = http_requests.get(deezer_result.preview, timeout=30)
                resp.raise_for_status()
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=True) as tmp:
                    tmp.write(resp.content)
                    tmp.flush()
                    af_result = provider.fetch_from_file(tmp.name, track_id)
            except Exception as exc:
                error = str(exc)

    if af_result is None:
        return render(
            request,
            "spotify/partials/track_audio_features.html",
            {
                "af": None,
                "track": track,
                "error": error or "No audio features found for this track.",
            },
        )

    af_obj, _ = AudioFeatures.objects.get_or_create(
        track_id=track_id,
        defaults={
            "acousticness": af_result.acousticness,
            "danceability": af_result.danceability,
            "energy": af_result.energy,
            "instrumentalness": af_result.instrumentalness,
            "key": af_result.key,
            "liveness": af_result.liveness,
            "loudness": af_result.loudness,
            "mode": af_result.mode,
            "speechiness": af_result.speechiness,
            "tempo": af_result.tempo,
            "time_signature": af_result.time_signature,
            "valence": af_result.valence,
            "analysis_url": "",
        },
    )
    return render(
        request,
        "spotify/partials/track_audio_features.html",
        {"af": af_obj, "track": track},
    )


def fetch_track_lyrics(request: HttpRequest, track_id: str) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    track = get_object_or_404(
        Track.objects.prefetch_related("artists").select_related("album"), id=track_id
    )

    # Already exists — return it
    lyrics = TrackLyrics.objects.filter(track_id=track_id).first()
    if lyrics:
        return render(
            request,
            "spotify/partials/track_lyrics.html",
            {"lyrics": lyrics, "track": track},
        )

    from spotify.genius import get_lyrics as genius_get_lyrics
    from spotify.lrclib import get_lyrics as lrclib_get_lyrics

    first_artist = track.artists.first()
    artist_name = first_artist.name if first_artist else ""

    lrclib_result = lrclib_get_lyrics(
        track_name=track.name,
        artist_name=artist_name,
        album_name=track.album.name,
        duration_seconds=track.duration_ms // 1000,
    )

    if lrclib_result is not None:
        lyrics, _ = TrackLyrics.objects.get_or_create(
            track_id=track_id,
            defaults={
                "instrumental": lrclib_result.instrumental,
                "plain_lyrics": lrclib_result.plain_lyrics,
                "synced_lyrics": lrclib_result.synced_lyrics,
            },
        )
    else:
        genius_result = genius_get_lyrics(
            track_name=track.name, artist_name=artist_name
        )
        if genius_result is None:
            return render(
                request,
                "spotify/partials/track_lyrics.html",
                {
                    "lyrics": None,
                    "track": track,
                    "error": "No lyrics found for this track.",
                },
            )
        lyrics, _ = TrackLyrics.objects.get_or_create(
            track_id=track_id,
            defaults={
                "instrumental": False,
                "plain_lyrics": genius_result.plain_lyrics,
                "synced_lyrics": "",
            },
        )

    if not lyrics.instrumental and lyrics.plain_lyrics:
        try:
            from analysis.management.commands.compute_lyric_embeddings import (
                run_sync as _embed,
            )

            _embed(track_ids=[track_id])
        except Exception:
            logger.warning(
                "Lyric embedding skipped for %s (Ollama unavailable?)", track_id
            )

    return render(
        request,
        "spotify/partials/track_lyrics.html",
        {"lyrics": lyrics, "track": track},
    )


def _model_short(model: str) -> str:
    return model.split("/")[-1].split(":")[0]


def fetch_track_tags(request: HttpRequest, track_id: str) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from analysis.management.commands.compute_track_tags import (
        DEFAULT_MODEL as _TAG_MODEL,
    )
    from analysis.management.commands.compute_track_tags import (
        FREE_MODELS,
    )
    from analysis.management.commands.compute_track_tags import (
        run_sync as _tag_sync,
    )

    models_list = [_TAG_MODEL] + [m for m in FREE_MODELS if m != _TAG_MODEL]
    job_id = str(uuid.uuid4())
    _sync_jobs[job_id] = {
        "status": "running",
        "model": _model_short(_TAG_MODEL),
        "model_idx": 1,
        "total_models": len(models_list),
        "attempt": 1,
    }

    def _attempt_cb(
        model: str, model_idx: int, total_models: int, attempt: int
    ) -> None:
        _sync_jobs[job_id].update(
            {
                "model": _model_short(model),
                "model_idx": model_idx,
                "total_models": total_models,
                "attempt": attempt,
            }
        )

    def _run():
        try:
            TrackTags.objects.filter(track_id=track_id, model_name=_TAG_MODEL).delete()
            _tag_sync(
                model=_TAG_MODEL,
                track_ids=[track_id],
                fallback_models=FREE_MODELS,
                attempt_cb=_attempt_cb,
            )
            _sync_jobs[job_id]["status"] = "complete"
        except Exception:
            logger.exception("fetch_track_tags failed for track %s", track_id)
            _sync_jobs[job_id].update(
                {"status": "error", "error": traceback.format_exc()}
            )

    threading.Thread(target=_run, daemon=True).start()

    return render(
        request,
        "spotify/partials/track_tags.html",
        {
            "track_id": track_id,
            "job_id": job_id,
            "status_label": f"Trying {_model_short(_TAG_MODEL)} · 1/{len(models_list)}",
        },
    )


def fetch_track_tags_status(
    request: HttpRequest, track_id: str, job_id: str
) -> HttpResponse:
    job = _sync_jobs.get(job_id)
    if job is None or job["status"] == "error":
        error = (job or {}).get("error", "Job not found (server may have restarted).")
        return render(
            request,
            "spotify/partials/track_tags.html",
            {"track_id": track_id, "error": error},
        )
    if job["status"] == "running":
        label = f"Trying {job['model']} · {job['model_idx']}/{job['total_models']} · attempt {job['attempt']}"
        return render(
            request,
            "spotify/partials/track_tags.html",
            {"track_id": track_id, "job_id": job_id, "status_label": label},
        )
    tags = TrackTags.objects.filter(track_id=track_id).first()
    return render(
        request,
        "spotify/partials/track_tags.html",
        {"tags": tags, "track_id": track_id},
    )


def save_track_to_library(request: HttpRequest, track_id: str) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    from spotify import client

    track = get_object_or_404(Track, id=track_id)

    try:
        client.save_track(track_id)
    except http_requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (401, 403):
            error = (
                "Re-authenticate with Spotify to enable saving (needs library-modify)."
            )
        else:
            error = f"Spotify rejected the save request (HTTP {status})."
        return render(
            request,
            "spotify/partials/track_save.html",
            {"track_id": track_id, "error": error},
        )

    job_id = str(uuid.uuid4())
    _sync_jobs[job_id] = {"status": "running", "stage": "Saved — enriching…"}

    def _stage_cb(stage: str) -> None:
        _sync_jobs[job_id]["stage"] = stage

    def _run():
        try:
            from django.utils import timezone

            from spotify.pipeline import enrich_tracks

            SavedTrack.objects.get_or_create(
                track_id=track.id, defaults={"added_at": timezone.now()}
            )
            enrich_tracks([track.id], compute_tags=True, stage_cb=_stage_cb)
            _sync_jobs[job_id]["status"] = "complete"
        except Exception:
            logger.exception("save_track_to_library enrichment failed for %s", track_id)
            _sync_jobs[job_id].update(
                {"status": "error", "error": traceback.format_exc()}
            )

    threading.Thread(target=_run, daemon=True).start()

    return render(
        request,
        "spotify/partials/track_save.html",
        {
            "track_id": track_id,
            "job_id": job_id,
            "status_label": _sync_jobs[job_id]["stage"],
        },
    )


def save_track_status(request: HttpRequest, track_id: str, job_id: str) -> HttpResponse:
    job = _sync_jobs.get(job_id)
    if job is None or job["status"] == "error":
        error = (job or {}).get(
            "error", "Save job not found (server may have restarted)."
        )
        return render(
            request,
            "spotify/partials/track_save.html",
            {"track_id": track_id, "error": error},
        )
    if job["status"] == "running":
        return render(
            request,
            "spotify/partials/track_save.html",
            {
                "track_id": track_id,
                "job_id": job_id,
                "status_label": job.get("stage", "Saving to library…"),
            },
        )
    # complete → tell the partial to refresh the whole modal
    return render(
        request,
        "spotify/partials/track_save.html",
        {"track_id": track_id, "done": True},
    )


def track_mashup_candidates(request: HttpRequest, track_id: str) -> HttpResponse:
    from analysis.mashup import get_candidates, lyric_similarity

    track = get_object_or_404(Track, id=track_id)
    if not AudioFeatures.objects.filter(track_id=track_id).exists():
        return render(
            request,
            "spotify/partials/track_mashup_candidates.html",
            {
                "candidates": [],
                "track": track,
                "no_af": True,
                "key_compat": "",
                "bpm_max": "",
                "sort_by": "",
            },
        )

    key_compat = request.GET.get("key_compat", "")
    bpm_max = request.GET.get("bpm_max", "")
    sort_by = request.GET.get("sort_by", "")

    candidates = get_candidates(track_id, n=50)

    if key_compat == "1":
        candidates = [
            c for c in candidates if c["key_compat"] in ("perfect", "compatible")
        ]
    if bpm_max.isdigit():
        candidates = [c for c in candidates if c["bpm_diff_pct"] <= int(bpm_max)]

    lyric_sims = lyric_similarity(track_id, [c["track"].id for c in candidates])
    for c in candidates:
        c["lyric_sim"] = lyric_sims.get(c["track"].id)

    query_has_lyric = bool(lyric_sims)
    if sort_by == "lyric" and query_has_lyric:
        candidates.sort(
            key=lambda c: c["lyric_sim"] if c["lyric_sim"] is not None else -1,
            reverse=True,
        )

    return render(
        request,
        "spotify/partials/track_mashup_candidates.html",
        {
            "candidates": candidates,
            "track": track,
            "key_compat": key_compat,
            "bpm_max": bpm_max,
            "sort_by": sort_by,
            "query_has_lyric": query_has_lyric,
        },
    )


def sync_lyrics_view(request: HttpRequest) -> HttpResponse:
    """Start a background lyrics sync and return the job ID immediately."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    job_id = str(uuid.uuid4())
    _sync_jobs[job_id] = {
        "status": "running",
        "label": "Lyrics Sync",
        "started_at": time.time(),
        "progress": {"done": 0, "total": 0},
    }

    def _progress_cb(done: int, total: int) -> None:
        _sync_jobs[job_id]["progress"] = {"done": done, "total": total}

    def _run():
        try:
            from spotify.management.commands.fetch_lyrics import run_sync

            stats = run_sync(progress_cb=_progress_cb)
            _sync_jobs[job_id].update({"status": "complete", "stats": stats})
        except Exception:
            _sync_jobs[job_id].update(
                {"status": "error", "error": traceback.format_exc()}
            )
            logger.error("Background fetch_lyrics failed:\n%s", traceback.format_exc())

    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({"job_id": job_id})


def sync_lyrics_status(request: HttpRequest, job_id: str) -> HttpResponse:
    """Poll the status of a background lyrics sync job."""
    job = _sync_jobs.get(job_id)
    if job is None:
        return JsonResponse({"status": "not_found"}, status=404)
    return JsonResponse(job)


PLAYLIST_SORT_FIELDS = {
    "name": "name",
    "tracks": "total_tracks",
    "owner": "owner_display_name",
    "created": "created_at",
}

PLAYLIST_DEFAULT_DIR = {
    "name": "asc",
    "tracks": "desc",
    "owner": "asc",
    "created": "desc",
}


def playlists_grid(request: HttpRequest) -> HttpResponse:
    q = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "created")
    direction = request.GET.get("dir", "desc")
    year = request.GET.get("year", "").strip()
    stale = request.GET.get("stale", "").strip()

    if sort not in PLAYLIST_SORT_FIELDS:
        sort = "created"
    if direction not in ("asc", "desc"):
        direction = "asc"
    # Reject anything that isn't a plain 4-digit number
    if not (year.isdigit() and len(year) == 4):
        year = ""
    if stale not in ("1", ""):
        stale = ""

    qs = Playlist.objects.all()
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(description__icontains=q)
            | Q(owner_display_name__icontains=q)
        )
    if year:
        qs = qs.filter(created_at__year=int(year))
    if stale == "1":
        qs = qs.filter(is_stale=True)

    order_field = PLAYLIST_SORT_FIELDS[sort]
    f = F(order_field)
    qs = qs.order_by(
        f.asc(nulls_last=True) if direction == "asc" else f.desc(nulls_last=True)
    )

    sort_dirs = {
        col: ("asc" if direction == "desc" else "desc")
        if sort == col
        else PLAYLIST_DEFAULT_DIR[col]
        for col in PLAYLIST_SORT_FIELDS
    }

    available_years = [
        d.year
        for d in Playlist.objects.filter(created_at__isnull=False).dates(
            "created_at", "year", order="DESC"
        )
    ]

    stale_count = Playlist.objects.filter(is_stale=True).count()

    return render(
        request,
        "spotify/partials/playlists_grid.html",
        {
            "playlists": qs,
            "q": q,
            "sort": sort,
            "dir": direction,
            "sort_dirs": sort_dirs,
            "year": year,
            "stale": stale,
            "available_years": available_years,
            "total_count": qs.count(),
            "stale_count": stale_count,
        },
    )


def playlist_detail(request: HttpRequest, playlist_id: str) -> HttpResponse:
    playlist = get_object_or_404(Playlist, id=playlist_id)
    playlist_tracks = list(
        PlaylistTrack.objects.filter(playlist=playlist)
        .select_related("track", "track__album")
        .prefetch_related("track__artists")
        .order_by("position")
    )
    track_ids = [pt.spotify_track_id for pt in playlist_tracks]
    saved_ids = set(
        SavedTrack.objects.filter(track_id__in=track_ids).values_list(
            "track_id", flat=True
        )
    )
    af_ids = set(
        AudioFeatures.objects.filter(track_id__in=track_ids).values_list(
            "track_id", flat=True
        )
    )
    lyrics_ids = set(
        TrackLyrics.objects.filter(track_id__in=track_ids).values_list(
            "track_id", flat=True
        )
    )
    return render(
        request,
        "spotify/playlist_detail.html",
        {
            "playlist": playlist,
            "playlist_tracks": playlist_tracks,
            "saved_ids": saved_ids,
            "af_ids": af_ids,
            "lyrics_ids": lyrics_ids,
        },
    )


def sync_playlists_view(request: HttpRequest) -> HttpResponse:
    """Start a background playlist sync and return the job ID immediately."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    job_id = str(uuid.uuid4())
    _sync_jobs[job_id] = {
        "status": "running",
        "label": "Playlist Sync",
        "started_at": time.time(),
        "progress": {"done": 0, "total": 0},
    }

    def _progress_cb(done: int, total: int) -> None:
        _sync_jobs[job_id]["progress"] = {"done": done, "total": total}

    def _run():
        try:
            from spotify.management.commands.sync_playlists import run_sync

            # Metadata-only for new playlists — the user picks which ones to
            # track-sync from the preview modal.
            stats = run_sync(progress_cb=_progress_cb, sync_new_tracks=False)
            _sync_jobs[job_id].update({"status": "complete", "stats": stats})
        except Exception:
            _sync_jobs[job_id].update(
                {"status": "error", "error": traceback.format_exc()}
            )
            logger.error(
                "Background sync_playlists failed:\n%s", traceback.format_exc()
            )

    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({"job_id": job_id})


def sync_playlists_status(request: HttpRequest, job_id: str) -> HttpResponse:
    """Poll the status of a background playlist sync job."""
    job = _sync_jobs.get(job_id)
    if job is None:
        return JsonResponse({"status": "not_found"}, status=404)
    return JsonResponse(job)


def playlist_sync_preview(request: HttpRequest) -> HttpResponse:
    """Playlists that genuinely need a track sync (and are not ignored).

    Two cases:
    - No PlaylistTrack rows at all  → brand-new playlist, never synced
    - is_stale=True                 → snapshot changed since last sync

    Returns 204 when there is nothing pending so the frontend skips the modal.
    """
    from django.db.models import Count, Q

    pending_qs = (
        Playlist.objects.exclude(ignored=True)
        .filter(Q(playlist_tracks__isnull=True) | Q(is_stale=True))
        .distinct()
        .order_by("name")
    )
    if not pending_qs.exists():
        return HttpResponse(status=204)

    ids = list(pending_qs.values_list("id", flat=True))
    local_counts = dict(
        PlaylistTrack.objects.filter(playlist_id__in=ids)
        .values("playlist_id")
        .annotate(n=Count("id"))
        .values_list("playlist_id", "n")
    )
    rows = [
        {
            "playlist": p,
            "local": local_counts.get(p.id, 0),
            "spotify": p.total_tracks,
            "net": p.total_tracks - local_counts.get(p.id, 0),
        }
        for p in pending_qs
    ]
    total_net = sum(r["net"] for r in rows)
    return render(
        request,
        "spotify/partials/playlist_sync_preview.html",
        {"rows": rows, "total_net": total_net},
    )


def ignore_playlist(request: HttpRequest, playlist_id: str) -> HttpResponse:
    """Permanently exclude a playlist from future sync previews."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    playlist = get_object_or_404(Playlist, id=playlist_id)
    playlist.ignored = True
    playlist.save(update_fields=["ignored"])
    return HttpResponse(status=204)


def sync_selected_playlists_view(request: HttpRequest) -> HttpResponse:
    """Start a background track sync for the playlists selected in the preview modal."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    playlist_ids = request.POST.getlist("playlist_ids")
    if not playlist_ids:
        return JsonResponse({"error": "no playlists selected"}, status=400)

    job_id = str(uuid.uuid4())
    _sync_jobs[job_id] = {
        "status": "running",
        "label": f"Playlist Tracks Sync ({len(playlist_ids)})",
        "started_at": time.time(),
        "progress": {"done": 0, "total": len(playlist_ids)},
    }

    def _run():
        from spotify.management.commands.sync_playlist_tracks import run_sync

        names = {
            p.id: p.name
            for p in Playlist.objects.filter(id__in=playlist_ids).only("id", "name")
        }
        stats = {"synced": 0, "added": 0, "errors": []}
        for i, pid in enumerate(playlist_ids):
            name = names.get(pid, pid)
            _sync_jobs[job_id]["current"] = {
                "playlist_name": name,
                "playlist_index": i + 1,
                "stage": "Starting…",
                "track_progress": {"done": 0, "total": 0},
            }

            def _stage_cb(stage, _jid=job_id):
                _sync_jobs[_jid]["current"]["stage"] = stage

            def _track_progress_cb(done, total, _jid=job_id):
                _sync_jobs[_jid]["current"]["track_progress"] = {"done": done, "total": total}

            try:
                result = run_sync(pid, progress_cb=_track_progress_cb, stage_cb=_stage_cb)
                stats["synced"] += 1
                stats["added"] += result.get("added", 0)
                stats["errors"].extend(result.get("errors", []))
            except Exception as exc:
                stats["errors"].append(
                    {"type": "playlist", "id": pid, "name": name, "error": str(exc)}
                )
                logger.error(
                    "Background sync_playlist_tracks failed for %s:\n%s",
                    pid,
                    traceback.format_exc(),
                )
            _sync_jobs[job_id]["progress"] = {"done": i + 1, "total": len(playlist_ids)}
        _sync_jobs[job_id].pop("current", None)
        _sync_jobs[job_id].update({"status": "complete", "stats": stats})

    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({"job_id": job_id})


def sync_single_playlist_view(request: HttpRequest, playlist_id: str) -> HttpResponse:
    """Start a background delta track sync for a single playlist."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    playlist = get_object_or_404(Playlist, id=playlist_id)

    job_id = str(uuid.uuid4())
    _sync_jobs[job_id] = {
        "status": "running",
        "label": f"Sync: {playlist.name}",
        "started_at": time.time(),
        "progress": {"done": 0, "total": 0},
    }

    def _progress_cb(done: int, total: int) -> None:
        _sync_jobs[job_id]["progress"] = {"done": done, "total": total}

    def _run():
        try:
            from spotify.management.commands.sync_playlist_tracks import run_sync

            stats = run_sync(playlist_id, progress_cb=_progress_cb)
            _sync_jobs[job_id].update({"status": "complete", "stats": stats})
        except Exception:
            _sync_jobs[job_id].update(
                {"status": "error", "error": traceback.format_exc()}
            )
            logger.error(
                "Background sync_playlist_tracks failed for %s:\n%s",
                playlist_id,
                traceback.format_exc(),
            )

    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({"job_id": job_id})


def sync_single_playlist_status(
    request: HttpRequest, playlist_id: str, job_id: str
) -> HttpResponse:
    """Poll the status of a per-playlist background sync job."""
    job = _sync_jobs.get(job_id)
    if job is None:
        return JsonResponse({"status": "not_found"}, status=404)
    return JsonResponse(job)


def sync_playlist_audio_features_view(
    request: HttpRequest, playlist_id: str
) -> HttpResponse:
    """Start a background audio features enrichment job for a single playlist."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    playlist = get_object_or_404(Playlist, id=playlist_id)
    track_ids = list(
        PlaylistTrack.objects.filter(playlist=playlist).values_list(
            "spotify_track_id", flat=True
        )
    )

    job_id = str(uuid.uuid4())
    _sync_jobs[job_id] = {
        "status": "running",
        "label": f"Audio Features: {playlist.name}",
        "started_at": time.time(),
        "progress": {"done": 0, "total": len(track_ids)},
    }

    def _progress_cb_af(done: int, total: int) -> None:
        _sync_jobs[job_id]["progress"] = {"done": done, "total": total}

    def _run():
        try:
            from spotify.pipeline import _enrich_audio_features

            stats = _enrich_audio_features(track_ids, progress_cb=_progress_cb_af)
            _sync_jobs[job_id].update({"status": "complete", "stats": stats})
        except Exception:
            _sync_jobs[job_id].update(
                {"status": "error", "error": traceback.format_exc()}
            )
            logger.error(
                "Background playlist audio features failed for %s:\n%s",
                playlist_id,
                traceback.format_exc(),
            )

    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({"job_id": job_id})


def sync_playlist_audio_features_status(
    request: HttpRequest, playlist_id: str, job_id: str
) -> HttpResponse:
    """Poll the status of a per-playlist audio features job."""
    job = _sync_jobs.get(job_id)
    if job is None:
        return JsonResponse({"status": "not_found"}, status=404)
    return JsonResponse(job)


def sync_playlist_lyrics_view(request: HttpRequest, playlist_id: str) -> HttpResponse:
    """Start a background lyrics enrichment job for a single playlist."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    playlist = get_object_or_404(Playlist, id=playlist_id)
    track_ids = list(
        PlaylistTrack.objects.filter(playlist=playlist).values_list(
            "spotify_track_id", flat=True
        )
    )

    job_id = str(uuid.uuid4())
    _sync_jobs[job_id] = {
        "status": "running",
        "label": f"Lyrics: {playlist.name}",
        "started_at": time.time(),
        "progress": {"done": 0, "total": len(track_ids)},
    }

    def _progress_cb_lyrics(done: int, total: int) -> None:
        _sync_jobs[job_id]["progress"] = {"done": done, "total": total}

    def _run():
        try:
            from spotify.pipeline import _enrich_lyrics

            stats = _enrich_lyrics(track_ids, progress_cb=_progress_cb_lyrics)
            _sync_jobs[job_id].update({"status": "complete", "stats": stats})
        except Exception:
            _sync_jobs[job_id].update(
                {"status": "error", "error": traceback.format_exc()}
            )
            logger.error(
                "Background playlist lyrics failed for %s:\n%s",
                playlist_id,
                traceback.format_exc(),
            )

    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({"job_id": job_id})


def sync_playlist_lyrics_status(
    request: HttpRequest, playlist_id: str, job_id: str
) -> HttpResponse:
    """Poll the status of a per-playlist lyrics job."""
    job = _sync_jobs.get(job_id)
    if job is None:
        return JsonResponse({"status": "not_found"}, status=404)
    return JsonResponse(job)


def sync_audio_features_view(request: HttpRequest) -> HttpResponse:
    """Start a background audio features sync and return the job ID immediately."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    job_id = str(uuid.uuid4())
    _sync_jobs[job_id] = {
        "status": "running",
        "label": "Audio Features Sync",
        "started_at": time.time(),
        "progress": {"done": 0, "total": 0},
    }

    def _progress_cb(done: int, total: int) -> None:
        _sync_jobs[job_id]["progress"] = {"done": done, "total": total}

    def _run():
        try:
            from spotify.management.commands.fetch_audio_features import run_sync

            stats = run_sync(progress_cb=_progress_cb)
            _sync_jobs[job_id].update({"status": "complete", "stats": stats})
        except Exception:
            _sync_jobs[job_id].update(
                {"status": "error", "error": traceback.format_exc()}
            )
            logger.error(
                "Background fetch_audio_features failed:\n%s", traceback.format_exc()
            )

    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({"job_id": job_id})


def sync_audio_features_status(request: HttpRequest, job_id: str) -> HttpResponse:
    """Poll the status of a background audio features sync job."""
    job = _sync_jobs.get(job_id)
    if job is None:
        return JsonResponse({"status": "not_found"}, status=404)
    return JsonResponse(job)


def sync_library(request: HttpRequest) -> HttpResponse:
    """Start a background library sync and return the job ID immediately."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    job_id = str(uuid.uuid4())
    _sync_jobs[job_id] = {
        "status": "running",
        "label": "Library Sync",
        "started_at": time.time(),
        "progress": {"done": 0, "total": 0},
    }

    def _progress_cb(done: int, total: int) -> None:
        _sync_jobs[job_id]["progress"] = {"done": done, "total": total}

    def _run():
        try:
            from spotify.management.commands.sync_saved_tracks import run_sync

            stats = run_sync(progress_cb=_progress_cb)
            _sync_jobs[job_id].update({"status": "complete", "stats": stats})
        except Exception:
            _sync_jobs[job_id].update(
                {"status": "error", "error": traceback.format_exc()}
            )
            logger.error(
                "Background sync_saved_tracks failed:\n%s", traceback.format_exc()
            )

    threading.Thread(target=_run, daemon=True).start()
    return JsonResponse({"job_id": job_id})


def sync_library_status(request: HttpRequest, job_id: str) -> HttpResponse:
    """Poll the status of a background library sync job."""
    job = _sync_jobs.get(job_id)
    if job is None:
        return JsonResponse({"status": "not_found"}, status=404)
    return JsonResponse(job)


def _table_context(request: HttpRequest) -> dict:
    q = request.GET.get("q", "").strip()
    tag = request.GET.get("tag", "").strip()
    sort = request.GET.get("sort", "added_at")
    direction = request.GET.get("dir", "desc")
    page_num = request.GET.get("page", 1)

    if sort not in SORT_FIELDS:
        sort = "added_at"
    if direction not in ("asc", "desc"):
        direction = "desc"

    qs = SavedTrack.objects.select_related("track", "track__album").prefetch_related(
        "track__artists"
    )

    if q:
        qs = qs.filter(
            Q(track__name__icontains=q)
            | Q(track__artists__name__icontains=q)
            | Q(track__album__name__icontains=q)
        ).distinct()

    if tag:
        axis, _, value = tag.partition(":")
        # ponytail: JSONField containment is unsupported on SQLite — scan rows in Python (~3.6k)
        tag_ids = [
            tid
            for tid, tags in TrackTags.objects.values_list("track_id", "tags")
            if value in (tags or {}).get(axis, {})
        ]
        qs = qs.filter(track_id__in=tag_ids)

    order_field = SORT_FIELDS[sort]
    qs = qs.order_by(order_field if direction == "asc" else f"-{order_field}")

    paginator = Paginator(qs, 50)
    page = paginator.get_page(page_num)

    # Compute next direction for each sortable column
    sort_dirs = {}
    for col in SORT_FIELDS:
        if sort == col:
            sort_dirs[col] = "asc" if direction == "desc" else "desc"
        else:
            sort_dirs[col] = DEFAULT_DIR.get(col, "desc")

    af_ids = set(AudioFeatures.objects.values_list("track_id", flat=True))
    lyrics_ids = set(TrackLyrics.objects.values_list("track_id", flat=True))

    return {
        "page": page,
        "q": q,
        "tag": tag,
        "sort": sort,
        "dir": direction,
        "sort_dirs": sort_dirs,
        "total_count": paginator.count,
        "af_ids": af_ids,
        "lyrics_ids": lyrics_ids,
    }


def _tag_options() -> dict:
    """Axis → sorted tags actually present in the DB, in canonical axis order."""
    present: dict[str, set] = {}
    for tags in TrackTags.objects.values_list("tags", flat=True):
        for axis, vals in (tags or {}).items():
            present.setdefault(axis, set()).update(vals)
    return {axis: sorted(present[axis]) for axis in TAG_ALLOWED if present.get(axis)}


def _af_table_context(request: HttpRequest) -> dict:
    q = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "title")
    direction = request.GET.get("dir", "asc")
    page_num = request.GET.get("page", 1)

    if sort not in AF_SORT_FIELDS:
        sort = "title"
    if direction not in ("asc", "desc"):
        direction = "asc"

    qs = AudioFeatures.objects.select_related("track").prefetch_related(
        "track__artists"
    )

    if q:
        qs = qs.filter(
            Q(track__name__icontains=q) | Q(track__artists__name__icontains=q)
        ).distinct()

    order_field = AF_SORT_FIELDS[sort]
    qs = qs.order_by(order_field if direction == "asc" else f"-{order_field}")

    paginator = Paginator(qs, 50)
    page = paginator.get_page(page_num)

    sort_dirs = {}
    for col in AF_SORT_FIELDS:
        if sort == col:
            sort_dirs[col] = "asc" if direction == "desc" else "desc"
        else:
            sort_dirs[col] = AF_DEFAULT_DIR.get(col, "desc")

    return {
        "af_page": page,
        "af_q": q,
        "af_sort": sort,
        "af_dir": direction,
        "af_sort_dirs": sort_dirs,
        "af_total_count": paginator.count,
    }


# ── Mashup tab ────────────────────────────────────────────────────────────────


def mashup_page(request: HttpRequest) -> HttpResponse:
    return render(request, "spotify/mashup.html")


def mashup_search(request: HttpRequest) -> HttpResponse:
    q = request.GET.get("q", "").strip()
    slot = request.GET.get("slot", "1")
    tracks = []
    if q:
        tracks = list(
            Track.objects.filter(saved__isnull=False, name__icontains=q)
            .prefetch_related("artists")
            .order_by("name")[:10]
        )
    return render(
        request,
        "spotify/partials/mashup_search_results.html",
        {
            "tracks": tracks,
            "slot": slot,
            "q": q,
        },
    )


def mashup_track_detail(request: HttpRequest, track_id: str) -> HttpResponse:
    track = get_object_or_404(
        Track.objects.select_related("album").prefetch_related("artists"),
        id=track_id,
    )
    af = AudioFeatures.objects.filter(track_id=track_id).first()
    saved = SavedTrack.objects.filter(track_id=track_id).first()
    lyrics = TrackLyrics.objects.filter(track_id=track_id).first()
    return render(
        request,
        "spotify/partials/mashup_track_detail.html",
        {
            "track": track,
            "saved": saved,
            "af": af,
            "lyrics": lyrics,
        },
    )


def mashup_compat(request: HttpRequest) -> HttpResponse:
    from analysis.mashup import compute_pairwise_compat
    from analysis.models import MashupPair

    t1 = request.GET.get("t1", "")
    t2 = request.GET.get("t2", "")
    af1 = AudioFeatures.objects.filter(track_id=t1).first() if t1 else None
    af2 = AudioFeatures.objects.filter(track_id=t2).first() if t2 else None
    compat = compute_pairwise_compat(af1, af2) if (af1 and af2) else None
    score_color = "#374151"
    if compat:
        s = compat["score"]
        score_color = (
            "#22c55e"
            if s >= 75
            else "#eab308"
            if s >= 50
            else "#f97316"
            if s >= 25
            else "#ef4444"
        )
    id1, id2 = sorted([t1, t2]) if t1 and t2 else (t1, t2)
    already_saved = (
        MashupPair.objects.filter(track1_id=id1, track2_id=id2).exists()
        if (id1 and id2)
        else False
    )
    shared_tags = []
    if t1 and t2:
        rows = {
            tt.track_id: tt.tags
            for tt in TrackTags.objects.filter(track_id__in=[t1, t2])
        }
        tags1, tags2 = rows.get(t1) or {}, rows.get(t2) or {}
        for axis in TAG_ALLOWED:
            for tag_val in tags1.get(axis, {}):
                if tag_val in tags2.get(axis, {}):
                    shared_tags.append({"axis": axis, "tag": tag_val})
    return render(
        request,
        "spotify/partials/mashup_compat.html",
        {
            "compat": compat,
            "af1": af1,
            "af2": af2,
            "score_color": score_color,
            "t1": t1,
            "t2": t2,
            "already_saved": already_saved,
            "shared_tags": shared_tags,
        },
    )


def mashup_pairs(request: HttpRequest) -> HttpResponse:
    from analysis.models import MashupPair

    pairs = list(
        MashupPair.objects.select_related(
            "track1__album",
            "track2__album",
            "track1__audio_features",
            "track2__audio_features",
        )
        .prefetch_related("track1__artists", "track2__artists")
        .all()
    )
    return render(request, "spotify/partials/mashup_pairs.html", {"pairs": pairs})


def mashup_save_pair(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    from analysis.mashup import compute_pairwise_compat
    from analysis.models import MashupPair

    t1 = request.POST.get("t1", "")
    t2 = request.POST.get("t2", "")
    if not (t1 and t2):
        return HttpResponse(status=400)
    id1, id2 = sorted([t1, t2])
    af1 = AudioFeatures.objects.filter(track_id=id1).first()
    af2 = AudioFeatures.objects.filter(track_id=id2).first()
    score = compute_pairwise_compat(af1, af2)["score"] if (af1 and af2) else 0
    MashupPair.objects.get_or_create(
        track1_id=id1, track2_id=id2, defaults={"score": score}
    )
    return render(
        request,
        "spotify/partials/mashup_save_btn.html",
        {"saved": True, "t1": t1, "t2": t2},
    )


def mashup_delete_pair(request: HttpRequest, pair_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    from analysis.models import MashupPair

    MashupPair.objects.filter(id=pair_id).delete()
    pairs = list(
        MashupPair.objects.select_related(
            "track1__album",
            "track2__album",
            "track1__audio_features",
            "track2__audio_features",
        )
        .prefetch_related("track1__artists", "track2__artists")
        .all()
    )
    return render(request, "spotify/partials/mashup_pairs.html", {"pairs": pairs})
