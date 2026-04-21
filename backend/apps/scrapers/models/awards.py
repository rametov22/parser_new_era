from django.db import models
from apps.stdimage.models import StdImageField

from .content import Content
from .participants import Participant


class Award(models.Model):
    slug = models.SlugField(null=True, blank=True)
    name = models.CharField(max_length=512, null=True, blank=True)
    image = StdImageField(null=True, blank=True)

    def __str__(self):
        return self.name

    class Meta:
        managed = False
        db_table = "content_app_award"


class AwardYear(models.Model):
    award = models.ForeignKey(Award, on_delete=models.PROTECT, null=True, blank=True)
    year = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.award.name} - {self.year}"

    class Meta:
        managed = False
        db_table = "content_app_awardyear"


class AwardYearNomination(models.Model):
    award_year = models.ForeignKey(
        AwardYear, on_delete=models.PROTECT, null=True, blank=True
    )
    name = models.CharField(max_length=255, null=True, blank=True)
    winner_content = models.ForeignKey(
        Content,
        related_name="award_winner",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    winner_participant = models.ForeignKey(
        Participant,
        related_name="award_winner",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    nomination_content = models.ManyToManyField(
        Content, related_name="award_nomination", blank=True
    )
    nomination_participant = models.ManyToManyField(
        Participant, related_name="award_nomination", blank=True
    )

    class Meta:
        managed = False
        db_table = "content_app_awardyearnomination"

    # def __str__(self):
    #     return f"{self.award_year.award.name} - {self.award_year.year} - {self.name}"
