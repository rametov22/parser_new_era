from django.db import models


class Country(models.Model):
    country_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name

    class Meta:
        managed = False
        db_table = "content_app_country"


class Genre(models.Model):
    slug = models.SlugField(max_length=255)
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name

    class Meta:
        managed = False
        db_table = "content_app_genre"


class Studio(models.Model):
    studio_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name

    class Meta:
        managed = False
        db_table = "content_app_studio"


class Collection(models.Model):
    slug = models.SlugField(max_length=255)
    name = models.CharField(max_length=255)

    poster_1 = models.CharField(max_length=255, null=True, blank=True)
    poster_2 = models.CharField(max_length=255, null=True, blank=True)
    poster_3 = models.CharField(max_length=255, null=True, blank=True)

    def __str__(self):
        return self.name

    class Meta:
        managed = False
        db_table = "content_app_collection"


class Platform(models.Model):
    platform_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    films = models.JSONField(blank=True, default=dict)

    def __str__(self):
        return self.name

    class Meta:
        managed = False
        db_table = "content_app_platform"


class Keyword(models.Model):
    keyword_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name

    class Meta:
        managed = False
        db_table = "content_app_keyword"
