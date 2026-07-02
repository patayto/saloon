from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('spotify', '0004_token_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='spotifytoken',
            name='display_name',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='spotifytoken',
            name='profile_url',
            field=models.CharField(blank=True, max_length=512),
        ),
    ]
