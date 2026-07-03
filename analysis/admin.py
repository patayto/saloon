from django.contrib import admin
from django.db.models import Avg, Count

from .models import (
    LyricEmbedding,
    MashupPair,
    TagSuggestion,
    TrackFeatureVector,
    TrackSentiment,
    TrackTags,
)

admin.site.register(TrackSentiment)
admin.site.register(LyricEmbedding)
admin.site.register(TrackFeatureVector)
admin.site.register(MashupPair)
admin.site.register(TrackTags)


@admin.register(TagSuggestion)
class TagSuggestionAdmin(admin.ModelAdmin):
    list_display = ("tag", "axis", "score", "track", "model_name", "created_at")
    list_filter = ("axis", "model_name")
    search_fields = ("tag", "track__name")
    ordering = ("-created_at",)

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context)
        try:
            qs = response.context_data["cl"].queryset
        except (AttributeError, KeyError):
            return response
        response.context_data["tag_summary"] = (
            qs.values("axis", "tag")
            .annotate(count=Count("id"), avg_score=Avg("score"))
            .order_by("-count")[:40]
        )
        return response
