from django.db import models


class TrackSentiment(models.Model):
    track = models.OneToOneField(
        "spotify.Track", on_delete=models.CASCADE, related_name="sentiment"
    )
    vader_positive = models.FloatField()
    vader_negative = models.FloatField()
    vader_neutral = models.FloatField()
    vader_compound = models.FloatField()
    classifier_label = models.CharField(max_length=64, blank=True)
    classifier_score = models.FloatField(null=True)
    classifier_model = models.CharField(max_length=128, blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"TrackSentiment({self.track_id})"


class LyricEmbedding(models.Model):
    track = models.ForeignKey(
        "spotify.Track", on_delete=models.CASCADE, related_name="lyric_embeddings"
    )
    model_name = models.CharField(max_length=128)
    dimensions = models.IntegerField()
    embedding = models.BinaryField()
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("track", "model_name")]

    def __str__(self):
        return f"LyricEmbedding({self.track_id}, {self.model_name})"


class TrackFeatureVector(models.Model):
    track = models.ForeignKey(
        "spotify.Track", on_delete=models.CASCADE, related_name="feature_vectors"
    )
    version = models.CharField(max_length=64)
    feature_spec = models.JSONField()
    dimensions = models.IntegerField()
    vector = models.BinaryField()
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("track", "version")]

    def __str__(self):
        return f"TrackFeatureVector({self.track_id}, {self.version})"


class TrackTags(models.Model):
    track = models.ForeignKey(
        "spotify.Track", on_delete=models.CASCADE, related_name="track_tags"
    )
    model_name = models.CharField(max_length=128)
    tags = models.JSONField()  # {"mood": [...], "theme": [...], "scene": [...]}
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("track", "model_name")]

    def __str__(self):
        return f"TrackTags({self.track_id}, {self.model_name})"


class MashupPair(models.Model):
    track1 = models.ForeignKey("spotify.Track", on_delete=models.CASCADE, related_name="+")
    track2 = models.ForeignKey("spotify.Track", on_delete=models.CASCADE, related_name="+")
    score = models.IntegerField()
    saved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("track1", "track2")]
        ordering = ["-saved_at"]

    def __str__(self):
        return f"MashupPair({self.track1_id} / {self.track2_id}, score={self.score})"
