from django.contrib import admin

from spotify.models import Album, Artist, AudioFeatures, Playlist, PlaylistTrack, SavedTrack, Track

admin.site.register(Artist)
admin.site.register(Album)
admin.site.register(Track)
admin.site.register(SavedTrack)
admin.site.register(AudioFeatures)
admin.site.register(Playlist)
admin.site.register(PlaylistTrack)
