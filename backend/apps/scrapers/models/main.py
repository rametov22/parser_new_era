from django.db import models


class ScraperLog(models.Model):
    """Логи парсинга — пойдут в техническую базу (default)"""

    task_name = models.CharField(max_length=255)
    status = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)
    message = models.TextField()


class YtConnectContent(models.Model):
    PARSING_STATUS_CHOICES = [
        ("not_parsed", "Not parsed"),
        ("in_progress", "In Progress"),
        ("parsed", "Parsed"),
        ("failed", "Failed (too many attempts)"),
    ]

    content_id = models.PositiveIntegerField(unique=True)
    content_url = models.JSONField(null=True, blank=True, default=dict)
    is_serial = models.BooleanField(default=False)
    parsing_status = models.CharField(
        max_length=20, choices=PARSING_STATUS_CHOICES, default="not_parsed"
    )
    parsing_status_player = models.CharField(
        max_length=20, choices=PARSING_STATUS_CHOICES, default="not_parsed"
    )
    connect_fail_count = models.PositiveSmallIntegerField(default=0)
    player_fail_count = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
