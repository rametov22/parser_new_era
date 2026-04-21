from django.db import models
from apps.stdimage.models import StdImageField


class Participant(models.Model):
    participant_id = models.PositiveIntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    img = StdImageField(null=True, blank=True)

    date_of_birth = models.DateField(null=True, blank=True)
    year_of_birth = models.IntegerField(null=True, blank=True)

    date_of_death = models.DateField(null=True, blank=True)
    year_of_death = models.IntegerField(null=True, blank=True)

    place_of_birth = models.CharField(null=True, blank=True)

    genres = models.ManyToManyField("Genre", related_name="participants")
    total_movies = models.IntegerField(null=True, blank=True)

    is_completed = models.BooleanField(default=False)

    photo1 = StdImageField(null=True, blank=True)
    photo2 = StdImageField(null=True, blank=True)
    photo3 = StdImageField(null=True, blank=True)
    photo4 = StdImageField(null=True, blank=True)
    photo5 = StdImageField(null=True, blank=True)
    photo6 = StdImageField(null=True, blank=True)
    photo7 = StdImageField(null=True, blank=True)
    photo8 = StdImageField(null=True, blank=True)

    def __str__(self):
        return self.name

    class Meta:
        managed = False
        db_table = "content_app_participant"
