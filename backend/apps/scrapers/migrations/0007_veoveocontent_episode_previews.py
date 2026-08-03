from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0006_veoveocontent_veoveosyncstate"),
    ]

    operations = [
        migrations.AddField(
            model_name="veoveocontent",
            name="episode_previews",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="veoveocontent",
            name="episode_previews_download_error",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="veoveocontent",
            name="episode_previews_downloaded_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="veoveocontent",
            name="episode_previews_error",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="veoveocontent",
            name="episode_previews_synced_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
