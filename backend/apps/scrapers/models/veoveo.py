from django.db import models
from django.utils import timezone


class VeoVeoContent(models.Model):
    """Unmanaged mirror of Kmax's VeoVeo catalog table."""

    veoveo_id = models.PositiveIntegerField(primary_key=True)
    kinopoisk_id = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    imdb_id = models.CharField(max_length=32, blank=True)
    title = models.CharField(max_length=255, blank=True)
    original_title = models.CharField(max_length=255, blank=True)
    year = models.IntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=32, blank=True, db_index=True)

    is_available = models.BooleanField(default=True, db_index=True)
    player_url = models.TextField(blank=True)
    video_quality = models.CharField(max_length=32, blank=True)
    duration = models.PositiveIntegerField(null=True, blank=True)
    age_restriction = models.PositiveSmallIntegerField(null=True, blank=True)
    audio_tracks_raw = models.TextField(blank=True)
    voice_authors = models.JSONField(default=list, blank=True)
    languages = models.JSONField(default=list, blank=True)

    seasons_count = models.PositiveIntegerField(null=True, blank=True)
    episodes_count = models.PositiveIntegerField(null=True, blank=True)
    episodes_by_season = models.JSONField(default=dict, blank=True)
    episodes_by_voice_authors = models.JSONField(default=list, blank=True)
    last_season = models.PositiveIntegerField(null=True, blank=True)
    last_episode = models.PositiveIntegerField(null=True, blank=True)

    provider_created_at = models.DateTimeField(null=True, blank=True)
    provider_updated_at = models.DateTimeField(null=True, blank=True)
    premiere_at = models.DateTimeField(null=True, blank=True)
    last_season_premiere_at = models.DateTimeField(null=True, blank=True)
    exclusive_start_at = models.DateTimeField(null=True, blank=True)
    exclusive_end_at = models.DateTimeField(null=True, blank=True)
    is_lgbt = models.BooleanField(null=True, blank=True, db_index=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        managed = False
        db_table = "content_app_veoveo_content"

    def __str__(self):
        return f"{self.veoveo_id}: {self.title or self.kinopoisk_id or '-'}"
