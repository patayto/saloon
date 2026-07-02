"""
Shared upsert helpers used by both import_saved_tracks and sync_saved_tracks.
"""
from django.utils.dateparse import parse_datetime

from spotify.models import Album, Artist, Playlist, PlaylistTrack, SavedTrack, Track


def upsert_artist(data: dict) -> Artist:
    artist, _ = Artist.objects.update_or_create(
        id=data["id"],
        defaults={
            "name": data.get("name") or "",
            "uri": data.get("uri") or "",
            "href": data.get("href") or "",
            "spotify_url": (data.get("external_urls") or {}).get("spotify") or "",
            "popularity": data.get("popularity"),
            "genres": data.get("genres") or [],
            "followers": data.get("followers"),
        },
    )
    return artist


def upsert_album(data: dict) -> Album:
    album, _ = Album.objects.update_or_create(
        id=data["id"],
        defaults={
            "name": data.get("name") or "",
            "album_type": data.get("album_type") or "",
            "uri": data.get("uri") or "",
            "href": data.get("href") or "",
            "spotify_url": (data.get("external_urls") or {}).get("spotify") or "",
            "release_date": data.get("release_date") or "",
            "release_date_precision": data.get("release_date_precision") or "",
            "total_tracks": data.get("total_tracks") or 0,
        },
    )
    for artist_data in data.get("artists", []):
        artist = upsert_artist(artist_data)
        album.artists.add(artist)
    return album


def update_playlist_metadata(playlist: Playlist, data: dict) -> None:
    """Update a Playlist's metadata fields from a SimplifiedPlaylistObject.

    Called when the playlist already exists but ``snapshot_id`` has changed.
    Sets ``is_stale=True`` so the UI can prompt the user to run a track sync.
    Does not touch ``tracks_synced_at`` or the track list.
    """
    owner = data.get("owner") or {}
    images = data.get("images") or []
    image_url = images[0]["url"] if images else ""
    tracks_ref = data.get("tracks") or {}
    total_tracks = tracks_ref.get("total", 0)

    playlist.name = data.get("name", "") or ""
    playlist.description = data.get("description") or ""
    playlist.public = data.get("public")
    playlist.collaborative = data.get("collaborative", False)
    playlist.snapshot_id = data.get("snapshot_id", "") or ""
    playlist.uri = data.get("uri", "") or ""
    playlist.href = data.get("href", "") or ""
    playlist.spotify_url = (data.get("external_urls") or {}).get("spotify", "") or ""
    playlist.owner_id = owner.get("id", "") or ""
    playlist.owner_display_name = owner.get("display_name", "") or ""
    playlist.total_tracks = total_tracks
    playlist.image_url = image_url
    playlist.is_stale = True
    playlist.save(update_fields=[
        "name", "description", "public", "collaborative", "snapshot_id",
        "uri", "href", "spotify_url", "owner_id", "owner_display_name",
        "total_tracks", "image_url", "is_stale",
    ])


def upsert_playlist(data: dict) -> Playlist:
    """Upsert a playlist from a SimplifiedPlaylistObject (as returned by /me/playlists)."""
    owner = data.get("owner") or {}
    images = data.get("images") or []
    image_url = images[0]["url"] if images else ""
    # /me/playlists returns tracks as {href, total}; use total for count
    tracks_ref = data.get("tracks") or {}
    total_tracks = tracks_ref.get("total", 0)

    playlist, _ = Playlist.objects.update_or_create(
        id=data["id"],
        defaults={
            "name": data.get("name", ""),
            "description": data.get("description") or "",
            "public": data.get("public"),
            "collaborative": data.get("collaborative", False),
            "snapshot_id": data.get("snapshot_id", ""),
            "uri": data.get("uri", ""),
            "href": data.get("href", ""),
            "spotify_url": data.get("external_urls", {}).get("spotify", ""),
            "owner_id": owner.get("id", ""),
            "owner_display_name": owner.get("display_name", ""),
            "total_tracks": total_tracks,
            "image_url": image_url,
        },
    )
    return playlist


def update_playlist_created_at(playlist: Playlist) -> None:
    """Set playlist.created_at to the oldest PlaylistTrack.added_at for that playlist.

    Always recalculated (tracks are rebuilt from scratch on each sync), so this
    should be called after _sync_tracks completes.  No-op if no dated tracks exist.
    """
    oldest = (
        PlaylistTrack.objects
        .filter(playlist=playlist, added_at__isnull=False)
        .order_by("added_at")
        .values_list("added_at", flat=True)
        .first()
    )
    if oldest is not None:
        playlist.created_at = oldest
        playlist.save(update_fields=["created_at"])


def upsert_track(data: dict) -> Track:
    """Upsert a Track (and its Album + Artists) without creating a SavedTrack."""
    album = upsert_album(data["album"])
    track, _ = Track.objects.update_or_create(
        id=data["id"],
        defaults={
            "name": data.get("name") or "",
            "uri": data.get("uri") or "",
            "href": data.get("href") or "",
            "spotify_url": (data.get("external_urls") or {}).get("spotify") or "",
            "album": album,
            "duration_ms": data.get("duration_ms") or 0,
            "explicit": data.get("explicit") or False,
            "popularity": data.get("popularity"),
            "track_number": data.get("track_number") or 0,
            "disc_number": data.get("disc_number") or 1,
            "is_local": data.get("is_local") or False,
            "is_playable": data.get("is_playable"),
        },
    )
    for artist_data in data.get("artists", []):
        artist = upsert_artist(artist_data)
        track.artists.add(artist)
    return track


def persist_track_entry(entry: dict) -> tuple[SavedTrack, bool]:
    """
    Persist a single saved-track entry from the Spotify API.

    ``entry`` has the shape ``{"added_at": "<iso8601>", "track": {...}}``.

    Returns ``(SavedTrack, created)`` where ``created`` is False if the
    SavedTrack already existed.
    """
    added_at = parse_datetime(entry["added_at"])
    track_data = entry["track"]
    track_id = track_data["id"]

    if SavedTrack.objects.filter(track_id=track_id).exists():
        return SavedTrack.objects.get(track_id=track_id), False

    track = upsert_track(track_data)
    saved = SavedTrack.objects.create(track=track, added_at=added_at)
    return saved, True
