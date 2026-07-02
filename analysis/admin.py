from django.contrib import admin

from .models import LyricEmbedding, MashupPair, TrackFeatureVector, TrackSentiment

admin.site.register(TrackSentiment)
admin.site.register(LyricEmbedding)
admin.site.register(TrackFeatureVector)
admin.site.register(MashupPair)
