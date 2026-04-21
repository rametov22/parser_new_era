from .. import models
from ..kinopoisk_scrap_utils import save_image_from_url_to_award


def save_awards(content_obj, award_list):
    for award_data in award_list:
        award, created = models.Award.objects.get_or_create(slug=award_data["slug"])
        if created:
            award.name = award_data["name"]
            if award_data["image"]:
                save_image_from_url_to_award(award, award_data["image"])
            award.save()

        award_year, _ = models.AwardYear.objects.get_or_create(
            award=award, year=award_data["award_year"]
        )

        winners = award_data["winner_content"]
        for winner in winners:
            year_winner, _ = models.AwardYearNomination.objects.get_or_create(
                award_year=award_year, name=winner
            )
            year_winner.winner_content = content_obj
            year_winner.save()

        winners_part = award_data["winner_participant"]
        for winner_part in winners_part:
            for winner_id in winner_part["winner_id"]:
                participant, _ = models.Participant.objects.get_or_create(
                    participant_id=winner_id
                )
                year_winner_part, _ = models.AwardYearNomination.objects.get_or_create(
                    award_year=award_year, name=winner_part["name"]
                )
                year_winner_part.winner_participant = participant
                year_winner_part.save()

        nominations = award_data["nomination_content"]
        for nomination in nominations:
            year_nomination, _ = models.AwardYearNomination.objects.get_or_create(
                award_year=award_year, name=nomination
            )
            year_nomination.nomination_content.add(content_obj)
            year_nomination.save()

        nominations_part = award_data["nomination_participant"]
        for nomination_part in nominations_part:
            for nomination_id in nomination_part["nomination_id"]:
                participant, _ = models.Participant.objects.get_or_create(
                    participant_id=nomination_id
                )
                year_nomination_part, _ = (
                    models.AwardYearNomination.objects.get_or_create(
                        award_year=award_year, name=nomination_part["name"]
                    )
                )
                year_nomination_part.nomination_participant.add(participant)
                year_nomination_part.save()
