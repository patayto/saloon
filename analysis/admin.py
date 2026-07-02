from django.contrib import admin

from .models import LyricEmbedding, TrackFeatureVector, TrackSentiment

admin.site.register(TrackSentiment)
admin.site.register(LyricEmbedding)
admin.site.register(TrackFeatureVector)
