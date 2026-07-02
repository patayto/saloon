from django.contrib import admin
from django.urls import include, path

from spotify import views as spotify_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("analysis/", include("analysis.urls")),
    path("spotify/", include("spotify.urls")),  # OAuth: /spotify/login/, /spotify/callback/
    path("", spotify_views.library, name="library"),
    path("tracks/", spotify_views.tracks_table, name="tracks_table"),
    path("tracks/<str:track_id>/detail/", spotify_views.track_detail, name="track_detail"),
    path("tracks/<str:track_id>/fetch-audio-features/", spotify_views.fetch_track_audio_features, name="fetch_track_audio_features"),
    path("tracks/<str:track_id>/mashup-candidates/", spotify_views.track_mashup_candidates, name="track_mashup_candidates"),
    path("tracks/<str:track_id>/fetch-lyrics/", spotify_views.fetch_track_lyrics, name="fetch_track_lyrics"),
    path("lyrics/sync/", spotify_views.sync_lyrics_view, name="sync_lyrics"),
    path("lyrics/sync/status/<str:job_id>/", spotify_views.sync_lyrics_status, name="sync_lyrics_status"),
    path("sync/", spotify_views.sync_library, name="sync_library"),
    path("sync/status/<str:job_id>/", spotify_views.sync_library_status, name="sync_library_status"),
    path("audio-features/", spotify_views.audio_features_table, name="audio_features_table"),
    path("audio-features/sync/", spotify_views.sync_audio_features_view, name="sync_audio_features"),
    path("audio-features/sync/status/<str:job_id>/", spotify_views.sync_audio_features_status, name="sync_audio_features_status"),
    path("playlists/", spotify_views.playlists_grid, name="playlists_grid"),
    path("playlists/sync/", spotify_views.sync_playlists_view, name="sync_playlists"),
    path("playlists/sync/status/<str:job_id>/", spotify_views.sync_playlists_status, name="sync_playlists_status"),
    path("playlists/<str:playlist_id>/", spotify_views.playlist_detail, name="playlist_detail"),
    path("playlists/<str:playlist_id>/sync/", spotify_views.sync_single_playlist_view, name="sync_single_playlist"),
    path("playlists/<str:playlist_id>/sync/status/<str:job_id>/", spotify_views.sync_single_playlist_status, name="sync_single_playlist_status"),
    path("playlists/<str:playlist_id>/sync-audio-features/", spotify_views.sync_playlist_audio_features_view, name="sync_playlist_audio_features"),
    path("playlists/<str:playlist_id>/sync-audio-features/status/<str:job_id>/", spotify_views.sync_playlist_audio_features_status, name="sync_playlist_audio_features_status"),
    path("playlists/<str:playlist_id>/sync-lyrics/", spotify_views.sync_playlist_lyrics_view, name="sync_playlist_lyrics"),
    path("playlists/<str:playlist_id>/sync-lyrics/status/<str:job_id>/", spotify_views.sync_playlist_lyrics_status, name="sync_playlist_lyrics_status"),
    path("mashup/", spotify_views.mashup_page, name="mashup_page"),
    path("mashup/search/", spotify_views.mashup_search, name="mashup_search"),
    path("mashup/track/<str:track_id>/", spotify_views.mashup_track_detail, name="mashup_track_detail"),
    path("mashup/compat/", spotify_views.mashup_compat, name="mashup_compat"),
    path("mashup/pairs/", spotify_views.mashup_pairs, name="mashup_pairs"),
    path("mashup/pairs/save/", spotify_views.mashup_save_pair, name="mashup_save_pair"),
    path("mashup/pairs/<int:pair_id>/delete/", spotify_views.mashup_delete_pair, name="mashup_delete_pair"),
]
