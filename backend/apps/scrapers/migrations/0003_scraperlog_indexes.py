from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scrapers", "0002_award_awardyear_awardyearnomination_collection_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="scraperlog",
            index=models.Index(
                fields=["created_at"], name="scr_log_created_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="scraperlog",
            index=models.Index(
                fields=["status", "created_at"], name="scr_log_status_created_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="scraperlog",
            index=models.Index(
                fields=["task_name", "status", "created_at"],
                name="scr_log_task_status_created_idx",
            ),
        ),
    ]
