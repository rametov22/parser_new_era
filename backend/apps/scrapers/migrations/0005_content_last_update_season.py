from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0004_ytconnectcontent_yt_name_original_and_uz"),
    ]

    operations = [
        migrations.AddField(
            model_name="content",
            name="last_update_season",
            field=models.DateField(blank=True, null=True),
        ),
    ]
