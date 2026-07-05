from django.db import models


class Artist(models.Model):
    id = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=255)
    uri = models.CharField(max_length=255)
    href = models.CharField(max_length=512)
    spotify_url = models.CharField(max_length=512, blank=True)
    popularity = models.IntegerField(null=True, blank=True)
    genres = models.JSONField(default=list, blank=True)
    followers = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return self.name


class Album(models.Model):
    id = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=255)
    album_type = models.CharField(max_length=32)
    uri = models.CharField(max_length=255)
    href = models.CharField(max_length=512)
    spotify_url = models.CharField(max_length=512, blank=True)
    release_date = models.CharField(max_length=32)
    release_date_precision = models.CharField(max_length=16)
    total_tracks = models.IntegerField()
    artists = models.ManyToManyField(Artist, related_name='albums', blank=True)

    def __str__(self):
        return self.name


class Track(models.Model):
    id = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=512)
    uri = models.CharField(max_length=255)
    href = models.CharField(max_length=512)
    spotify_url = models.CharField(max_length=512, blank=True)
    album = models.ForeignKey(Album, on_delete=models.CASCADE, related_name='tracks')
    artists = models.ManyToManyField(Artist, related_name='tracks', blank=True)
    duration_ms = models.IntegerField()
    explicit = models.BooleanField(default=False)
    popularity = models.IntegerField(null=True, blank=True)
    track_number = models.IntegerField()
    disc_number = models.IntegerField(default=1)
    is_local = models.BooleanField(default=False)
    is_playable = models.BooleanField(null=True, blank=True)

    def __str__(self):
        return self.name


class SavedTrack(models.Model):
    track = models.OneToOneField(Track, on_delete=models.CASCADE, related_name='saved')
    added_at = models.DateTimeField()

    def __str__(self):
        return f"{self.track} (saved {self.added_at})"


class AudioFeatures(models.Model):
    track = models.OneToOneField(Track, on_delete=models.CASCADE, related_name='audio_features')
    acousticness = models.FloatField()
    danceability = models.FloatField()
    energy = models.FloatField()
    instrumentalness = models.FloatField()
    key = models.IntegerField()
    liveness = models.FloatField()
    loudness = models.FloatField()
    mode = models.IntegerField()
    speechiness = models.FloatField()
    tempo = models.FloatField()
    time_signature = models.IntegerField()
    valence = models.FloatField()
    analysis_url = models.CharField(max_length=512, blank=True)

    def __str__(self):
        return f"AudioFeatures({self.track_id})"


class Playlist(models.Model):
    id = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=512)
    description = models.TextField(blank=True)
    public = models.BooleanField(null=True, blank=True)
    collaborative = models.BooleanField(default=False)
    snapshot_id = models.CharField(max_length=255, blank=True)
    uri = models.CharField(max_length=255, blank=True)
    href = models.CharField(max_length=512, blank=True)
    spotify_url = models.CharField(max_length=512, blank=True)
    owner_id = models.CharField(max_length=255, blank=True)
    owner_display_name = models.CharField(max_length=255, blank=True)
    total_tracks = models.IntegerField(default=0)
    image_url = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Estimated from the oldest added_at across all tracks in this playlist.",
    )
    is_stale = models.BooleanField(
        default=False,
        help_text="True when snapshot_id has changed but tracks have not yet been re-synced.",
    )
    tracks_synced_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When tracks were last successfully delta-synced for this playlist.",
    )
    ignored = models.BooleanField(
        null=True, blank=True, default=None,
        help_text="True when the user explicitly excluded this playlist from future syncs.",
    )

    def __str__(self):
        return self.name


class PlaylistTrack(models.Model):
    playlist = models.ForeignKey(Playlist, on_delete=models.CASCADE, related_name='playlist_tracks')
    track = models.ForeignKey(Track, on_delete=models.SET_NULL, null=True, blank=True, related_name='playlist_tracks')
    spotify_track_id = models.CharField(max_length=64)
    added_at = models.DateTimeField(null=True, blank=True)
    added_by = models.CharField(max_length=255, blank=True)
    position = models.IntegerField(db_index=True)

    class Meta:
        ordering = ['position']

    def __str__(self):
        return f"{self.playlist} — {self.spotify_track_id} (#{self.position})"


class TrackLyrics(models.Model):
    track = models.OneToOneField(Track, on_delete=models.CASCADE, related_name='lyrics')
    instrumental = models.BooleanField(default=False)
    plain_lyrics = models.TextField(blank=True)
    synced_lyrics = models.TextField(blank=True, help_text="LRC-format time-synced lyrics.")
    fetched_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"TrackLyrics({self.track_id})"


class SpotifyToken(models.Model):
    TOKEN_TYPE_USER = 'user'
    TOKEN_TYPE_CLIENT = 'client'
    TOKEN_TYPE_CHOICES = [
        (TOKEN_TYPE_USER, 'Authorization Code (user)'),
        (TOKEN_TYPE_CLIENT, 'Client Credentials (app-only)'),
    ]

    token_type = models.CharField(max_length=16, choices=TOKEN_TYPE_CHOICES, default=TOKEN_TYPE_USER, unique=True)
    access_token = models.TextField()
    refresh_token = models.TextField(blank=True)
    expires_at = models.DateTimeField()
    display_name = models.CharField(max_length=255, blank=True)
    profile_url = models.CharField(max_length=512, blank=True)

    class Meta:
        verbose_name = "Spotify Token"

    def __str__(self):
        return f"SpotifyToken({self.token_type})"
