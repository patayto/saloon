from django.db import migrations, models


def backfill_playlist_created_at(apps, schema_editor):
    Playlist = apps.get_model("spotify", "Playlist")
    PlaylistTrack = apps.get_model("spotify", "PlaylistTrack")

    for playlist in Playlist.objects.all():
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


class Migration(migrations.Migration):

    dependencies = [
        ("spotify", "0006_add_playlist_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="playlist",
            name="created_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="Estimated from the oldest added_at across all tracks in this playlist.",
            ),
        ),
        migrations.RunPython(backfill_playlist_created_at, migrations.RunPython.noop),
    ]
