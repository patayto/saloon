from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('analysis', '0001_initial'),
        ('spotify', '0009_playlist_staleness_and_playlisttrack_position_index'),
    ]

    operations = [
        migrations.CreateModel(
            name='MashupPair',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('score', models.IntegerField()),
                ('saved_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('track1', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='spotify.track')),
                ('track2', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='spotify.track')),
            ],
            options={
                'ordering': ['-saved_at'],
                'unique_together': {('track1', 'track2')},
            },
        ),
    ]
