from django.db import models
from django.utils import timezone
from apps.stdimage.models import StdImageField

# from ..managers import ContentQuerySet


class ContentActor(models.Model):
    content = models.ForeignKey(
        "Content", related_name="content_actors", on_delete=models.CASCADE
    )
    participant = models.ForeignKey("Participant", on_delete=models.CASCADE)
    role = models.CharField(max_length=512, blank=True)
    ordering = models.PositiveSmallIntegerField(default=10)

    class Meta:
        ordering = ("ordering",)
        unique_together = (
            "content",
            "participant",
        )
        managed = False
        db_table = "content_app_contentactor"


class ContentDirector(models.Model):
    content = models.ForeignKey(
        "Content", related_name="content_directors", on_delete=models.CASCADE
    )
    participant = models.ForeignKey("Participant", on_delete=models.CASCADE)
    ordering = models.PositiveSmallIntegerField(default=10)

    class Meta:
        ordering = ("ordering",)
        unique_together = (
            "content",
            "participant",
        )
        managed = False
        db_table = "content_app_contentdirector"


class ContentScreenwriter(models.Model):
    content = models.ForeignKey(
        "Content", related_name="content_screenwriters", on_delete=models.CASCADE
    )
    participant = models.ForeignKey("Participant", on_delete=models.CASCADE)
    ordering = models.PositiveSmallIntegerField(default=10)

    class Meta:
        ordering = ("ordering",)
        unique_together = (
            "content",
            "participant",
        )
        managed = False
        db_table = "content_app_contentscreenwriter"


class ContentProducer(models.Model):
    content = models.ForeignKey(
        "Content", related_name="content_producers", on_delete=models.CASCADE
    )
    participant = models.ForeignKey("Participant", on_delete=models.CASCADE)
    ordering = models.PositiveSmallIntegerField(default=10)

    class Meta:
        ordering = ("ordering",)
        unique_together = (
            "content",
            "participant",
        )
        managed = False
        db_table = "content_app_contentproducer"


class ContentOperator(models.Model):
    content = models.ForeignKey(
        "Content", related_name="content_operators", on_delete=models.CASCADE
    )
    participant = models.ForeignKey("Participant", on_delete=models.CASCADE)
    ordering = models.PositiveSmallIntegerField(default=10)

    class Meta:
        ordering = ("ordering",)
        unique_together = (
            "content",
            "participant",
        )
        managed = False
        db_table = "content_app_contentoperator"


class ContentComposer(models.Model):
    content = models.ForeignKey(
        "Content", related_name="content_composers", on_delete=models.CASCADE
    )
    participant = models.ForeignKey("Participant", on_delete=models.CASCADE)
    ordering = models.PositiveSmallIntegerField(default=10)

    class Meta:
        ordering = ("ordering",)
        unique_together = (
            "content",
            "participant",
        )
        managed = False
        db_table = "content_app_contentcomposer"


class ContentEditor(models.Model):
    content = models.ForeignKey(
        "Content", related_name="content_editors", on_delete=models.CASCADE
    )
    participant = models.ForeignKey("Participant", on_delete=models.CASCADE)
    ordering = models.PositiveSmallIntegerField(default=10)

    class Meta:
        ordering = ("ordering",)
        unique_together = (
            "content",
            "participant",
        )
        managed = False
        db_table = "content_app_contenteditor"


class Content(models.Model):
    PARSING_STATUS_CHOICES = [
        ("not_parsed", "Not parsed"),
        ("in_progress", "In Progress"),
        ("parsed", "Parsed"),
    ]

    kino_poisk_id = models.PositiveIntegerField(unique=True)
    name_ru = models.CharField(max_length=255)
    name_original = models.CharField(max_length=255)
    short_description = models.TextField(blank=True)
    short_description_uz = models.TextField(blank=True)
    short_description_ru = models.TextField(blank=True)
    short_description_en = models.TextField(blank=True)
    poster = StdImageField(
        upload_to="content_media/",
        null=True,
        blank=True,
    )
    poster_link = models.CharField(max_length=512, null=True, blank=True)
    trailer_link = models.URLField(null=True, blank=True)
    is_serial = models.BooleanField()

    year_production = models.IntegerField(null=True, blank=True)
    countries = models.ManyToManyField("Country", related_name="contents")
    genres = models.ManyToManyField("Genre", related_name="contents")
    slogan = models.TextField(blank=True)
    age_restriction = models.IntegerField(null=True, blank=True)

    keywords = models.ManyToManyField("Keyword", related_name="contents")

    actors = models.ManyToManyField(
        "Participant",
        verbose_name="Актеры",
        related_name="contents_actors",
        through=ContentActor,
        blank=True,
    )
    directors = models.ManyToManyField(
        "Participant",
        verbose_name="Режиссеры",
        related_name="contents_director",
        through=ContentDirector,
        blank=True,
    )
    screenwriters = models.ManyToManyField(
        "Participant",
        verbose_name="Сценаристы",
        related_name="contents_screenwriters",
        through=ContentScreenwriter,
        blank=True,
    )
    producers = models.ManyToManyField(
        "Participant",
        verbose_name="Продюсеры",
        related_name="contents_producers",
        through=ContentProducer,
        blank=True,
    )
    operators = models.ManyToManyField(
        "Participant",
        verbose_name="Операторы",
        related_name="contents_operators",
        through=ContentOperator,
        blank=True,
    )
    composers = models.ManyToManyField(
        "Participant",
        verbose_name="Композитор",
        related_name="contents_composers",
        through=ContentComposer,
        blank=True,
    )
    editors = models.ManyToManyField(
        "Participant",
        verbose_name="Художники",
        related_name="contents_editors",
        through=ContentEditor,
        blank=True,
    )

    description = models.TextField()
    description_ru = models.TextField(blank=True, null=True)
    description_en = models.TextField(blank=True, null=True)
    description_uz = models.TextField(blank=True, null=True)
    platform = models.ForeignKey(
        "Platform",
        on_delete=models.PROTECT,
        related_name="contents",
        null=True,
        blank=True,
    )

    kino_poisk_rating = models.DecimalField(
        max_digits=4, decimal_places=1, default=5.0, null=True, blank=True
    )
    imdb_rating = models.DecimalField(
        max_digits=4, decimal_places=2, default=1.00, null=True, blank=True
    )
    kmax_rating = models.FloatField(default=10.0)

    studios = models.ManyToManyField("Studio", blank=True, related_name="contents")
    collections = models.ManyToManyField(
        "Collection", blank=True, related_name="contents"
    )

    additional = models.JSONField(blank=True, default=dict)
    seasons = models.JSONField(blank=True, default=dict)

    premiere = models.DateField(null=True, blank=True)
    premiere_ru = models.DateField(null=True, blank=True)

    film_content = models.URLField(null=True, blank=True)
    add_content_date = models.DateField(null=True, blank=True)
    audio_tracks = models.JSONField(null=True, blank=True, default=list)
    player_variables = models.JSONField(null=True, blank=True, default=list)
    have_trailer_player = models.BooleanField(default=False, null=True)
    player_id = models.PositiveIntegerField(unique=True, null=True)
    last_season = models.IntegerField(null=True, blank=True)
    last_episode = models.IntegerField(null=True, blank=True)
    last_update = models.DateField(null=True, blank=True, default=timezone.now)

    film_content_uz = models.JSONField(null=True, blank=True, default=dict)
    name_uz = models.CharField(max_length=255, null=True)
    id_uz = models.PositiveIntegerField(unique=True, null=True)
    poster_uz = StdImageField(upload_to="content_media_uz/", null=True, blank=True)
    last_season_uz = models.IntegerField(null=True, blank=True)
    last_episode_uz = models.IntegerField(null=True, blank=True)

    language = models.CharField(default="Русский")

    is_parsed_kp = models.CharField(
        max_length=20, choices=PARSING_STATUS_CHOICES, default="not_parsed"
    )
    is_parsed_uz = models.CharField(
        max_length=20, choices=PARSING_STATUS_CHOICES, default="not_parsed"
    )
    is_parsed_ru = models.CharField(
        max_length=20, choices=PARSING_STATUS_CHOICES, default="not_parsed"
    )

    parsed_at_kp = models.DateTimeField(null=True, blank=True)
    parsed_at_uz = models.DateTimeField(null=True, blank=True)
    parsed_at_ru = models.DateTimeField(null=True, blank=True)

    parse_count_kp = models.PositiveIntegerField(default=0)
    parse_count_uz = models.PositiveIntegerField(default=0)
    parse_count_ru = models.PositiveIntegerField(default=0)

    # objects = ContentQuerySet.as_manager()

    class Meta:
        managed = False
        db_table = "content_app_content"
        indexes = [
            models.Index(fields=["premiere"]),
            models.Index(fields=["premiere_ru"]),
            models.Index(fields=["year_production"]),
            models.Index(fields=["kino_poisk_rating"]),
            models.Index(fields=["is_parsed_kp", "parsed_at_kp"]),
            models.Index(fields=["is_parsed_kp"]),
            models.Index(fields=["parse_count_kp"]),
        ]

    def __str__(self):
        return self.name_ru
