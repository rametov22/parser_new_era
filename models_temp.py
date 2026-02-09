# This is an auto-generated Django model module.
# You'll have to do the following manually to clean this up:
#   * Rearrange models' order
#   * Make sure each model has one field with primary_key=True
#   * Make sure each ForeignKey and OneToOneField has `on_delete` set to the desired behavior
#   * Remove `managed = False` lines if you wish to allow Django to create, modify, and delete the table
# Feel free to rename the models, but don't rename db_table values or field names.
from django.db import models


class ContentAppContent(models.Model):
    id = models.BigAutoField(primary_key=True)
    kino_poisk_id = models.IntegerField(unique=True)
    name_ru = models.CharField(max_length=255)
    name_original = models.CharField(max_length=255)
    short_description = models.TextField()
    poster = models.CharField(max_length=100, blank=True, null=True)
    trailer_link = models.CharField(max_length=200, blank=True, null=True)
    is_serial = models.BooleanField()
    year_production = models.IntegerField(blank=True, null=True)
    slogan = models.TextField()
    age_restriction = models.IntegerField(blank=True, null=True)
    description = models.TextField()
    kino_poisk_rating = models.DecimalField(max_digits=4, decimal_places=1, blank=True, null=True)
    imdb_rating = models.DecimalField(max_digits=4, decimal_places=2, blank=True, null=True)
    additional = models.JSONField()
    seasons = models.JSONField()
    platform = models.ForeignKey('ContentAppPlatform', models.DO_NOTHING, blank=True, null=True)
    language = models.CharField()
    film_content = models.CharField(max_length=200, blank=True, null=True)
    audio_tracks = models.JSONField(blank=True, null=True)
    have_trailer_player = models.BooleanField(blank=True, null=True)
    last_episode = models.IntegerField(blank=True, null=True)
    last_season = models.IntegerField(blank=True, null=True)
    player_id = models.IntegerField(unique=True, blank=True, null=True)
    premiere = models.DateField(blank=True, null=True)
    premiere_ru = models.DateField(blank=True, null=True)
    add_content_date = models.DateField(blank=True, null=True)
    poster_link = models.CharField(max_length=512, blank=True, null=True)
    last_update = models.DateField(blank=True, null=True)
    player_variables = models.JSONField(blank=True, null=True)
    description_en = models.TextField(blank=True, null=True)
    description_ru = models.TextField(blank=True, null=True)
    description_uz = models.TextField(blank=True, null=True)
    short_description_en = models.TextField(blank=True, null=True)
    short_description_ru = models.TextField(blank=True, null=True)
    short_description_uz = models.TextField(blank=True, null=True)
    film_content_uz = models.JSONField(blank=True, null=True)
    id_uz = models.IntegerField(unique=True, blank=True, null=True)
    kmax_rating = models.FloatField()
    name_uz = models.CharField(max_length=255, blank=True, null=True)
    is_parsed_kp = models.CharField(max_length=20)
    is_parsed_ru = models.CharField(max_length=20)
    is_parsed_uz = models.CharField(max_length=20)
    poster_uz = models.CharField(max_length=250, blank=True, null=True)
    last_episode_uz = models.IntegerField(blank=True, null=True)
    last_season_uz = models.IntegerField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'content_app_content'
