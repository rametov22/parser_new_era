from django.db import models


class ScraperLog(models.Model):
    """Логи парсинга — пойдут в техническую базу (default)"""

    task_name = models.CharField(max_length=255)
    status = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)
    message = models.TextField()

    class Meta:
        # Индексы под запросы дашборда (активность/ошибки по окнам времени).
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["task_name", "status", "created_at"]),
        ]


class VeoVeoSyncState(models.Model):
    """Persistent cursor and diagnostics for incremental VeoVeo sync."""

    STATUS_IDLE = "idle"
    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_ERROR = "error"
    STATUS_CHOICES = (
        (STATUS_IDLE, "Idle"),
        (STATUS_RUNNING, "Running"),
        (STATUS_SUCCESS, "Success"),
        (STATUS_ERROR, "Error"),
    )

    key = models.CharField(max_length=64, primary_key=True)
    cursor_at = models.DateTimeField(null=True, blank=True)
    run_token = models.UUIDField(null=True, blank=True)
    running_since = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_IDLE,
    )
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_finished_at = models.DateTimeField(null=True, blank=True)
    last_from_updated_at = models.DateTimeField(null=True, blank=True)
    last_to_updated_at = models.DateTimeField(null=True, blank=True)
    last_pages = models.PositiveIntegerField(default=0)
    last_received = models.PositiveIntegerField(default=0)
    last_created = models.PositiveIntegerField(default=0)
    last_updated = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "VeoVeo sync state"
        verbose_name_plural = "VeoVeo sync states"

    def __str__(self):
        return f"{self.key}: {self.status}"


class YtConnectContent(models.Model):
    """
    Состояние парсинга yangi.tv. Живёт в main_db (см. router),
    чтобы не зависеть от локального парсер-хоста.
    Таблица создаётся в Kmax-проекте (managed=False здесь).
    """

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

    # Кэш данных yangi.tv (getContentDetail), чтобы повторный матч с Content
    # делать локально, без лишних запросов к API при relink.
    yt_name = models.CharField(max_length=255, null=True, blank=True)
    yt_name_uz = models.CharField(max_length=255, null=True, blank=True)
    yt_name_original = models.CharField(max_length=255, null=True, blank=True)
    yt_year = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "parser_yt_connect_content"
