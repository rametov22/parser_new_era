from .. import models
from ..kinopoisk_scrap_utils import save_image_from_url_to_award


def _first_or_create(model, **lookup):
    obj = model.objects.filter(**lookup).order_by("id").first()
    if obj:
        return obj, False
    return model.objects.create(**lookup), True


def _get_award_year_nomination(award_year, name, **extra_lookup):
    return _first_or_create(
        models.AwardYearNomination,
        award_year=award_year,
        name=name,
        **extra_lookup,
    )


def save_awards(content_obj, award_list):
    for award_data in award_list:
        award, created = _first_or_create(models.Award, slug=award_data["slug"])
        if created:
            award.name = award_data["name"]
            if award_data["image"]:
                save_image_from_url_to_award(award, award_data["image"])
            award.save()

        award_year, _ = _first_or_create(
            models.AwardYear,
            award=award,
            year=award_data["award_year"],
        )

        winners = award_data["winner_content"]
        for winner in winners:
            _get_award_year_nomination(
                award_year=award_year,
                name=winner,
                winner_content=content_obj,
            )

        winners_part = award_data["winner_participant"]
        for winner_part in winners_part:
            for winner_id in winner_part["winner_id"]:
                participant, _ = models.Participant.objects.get_or_create(
                    participant_id=winner_id
                )
                _get_award_year_nomination(
                    award_year=award_year,
                    name=winner_part["name"],
                    winner_participant=participant,
                )

        nominations = award_data["nomination_content"]
        for nomination in nominations:
            year_nomination, _ = _get_award_year_nomination(
                award_year=award_year, name=nomination
            )
            year_nomination.nomination_content.add(content_obj)

        nominations_part = award_data["nomination_participant"]
        for nomination_part in nominations_part:
            for nomination_id in nomination_part["nomination_id"]:
                participant, _ = models.Participant.objects.get_or_create(
                    participant_id=nomination_id
                )
                year_nomination_part, _ = _get_award_year_nomination(
                    award_year=award_year, name=nomination_part["name"]
                )
                year_nomination_part.nomination_participant.add(participant)
