from django.db import models
from django.utils import timezone


class ImdbChartEntry(models.Model):
    """Unmanaged mirror for IMDb-powered Kmax collections."""

    chart_key = models.CharField(max_length=64, db_index=True)
    imdb_id = models.CharField(max_length=16, db_index=True)
    position = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=255, blank=True)
    year = models.IntegerField(null=True, blank=True)
    imdb_rating = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        null=True,
        blank=True,
    )
    vote_count = models.PositiveIntegerField(null=True, blank=True)
    source_url = models.TextField(blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    fetched_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "content_app_imdb_chart_entry"
        constraints = [
            models.UniqueConstraint(
                fields=["chart_key", "imdb_id"],
                name="uniq_imdb_chart_entry_chart_imdb",
            )
        ]
        indexes = [
            models.Index(fields=["chart_key", "is_active", "position"]),
            models.Index(fields=["chart_key", "fetched_at"]),
        ]

    def __str__(self):
        return f"{self.chart_key} #{self.position}: {self.imdb_id}"
