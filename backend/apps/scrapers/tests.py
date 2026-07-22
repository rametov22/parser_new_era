from datetime import datetime, timezone as datetime_timezone
from unittest.mock import Mock, patch

from django.db.models import F
from django.test import SimpleTestCase

from .release_quality import has_pirated_release
from .tasks.vavada import (
    _finish_vavada_parse,
    _save_vavada_player_presence,
)
from .tasks.vavada_serials import _episode_number


class VavadaReleaseQualityTests(SimpleTestCase):
    def test_detects_theatrical_release_labels(self):
        labels = (
            "TS",
            "HDTS",
            "CAMRip",
            "HDCAM",
            "TeleSync",
            "DVD Screener",
            "экранка",
            "камрип",
        )

        for label in labels:
            with self.subTest(label=label):
                self.assertTrue(has_pirated_release(label))

    def test_ignores_ts_inside_names_and_urls(self):
        self.assertFalse(has_pirated_release("MVO | TVShows"))
        self.assertFalse(has_pirated_release("DTS-HD MA"))
        self.assertFalse(
            has_pirated_release(
                {
                    "label": "WEB-DL",
                    "url": "https://cdn.example/video/segment.ts",
                }
            )
        )

    def test_normal_track_makes_release_non_pirated(self):
        self.assertFalse(
            has_pirated_release(
                [
                    "DUB | Звук с TS",
                    "MVO | TVShows",
                    "(EN) Оригинал",
                ]
            )
        )

    def test_all_pirated_tracks_make_release_pirated(self):
        self.assertTrue(
            has_pirated_release(
                [
                    "DUB | Звук с TS",
                    "DUB | Официальный (TC)",
                ]
            )
        )


class VavadaEpisodeNumberTests(SimpleTestCase):
    def test_reads_episode_label(self):
        self.assertEqual(_episode_number("9 эпизод"), 9)
        self.assertEqual(_episode_number("12 серия"), 12)

    def test_ignores_age_rating_in_audio_track(self):
        self.assertIsNone(_episode_number("MVO | HDrezka Studio (18+)"))


class VavadaPlayerPersistenceTests(SimpleTestCase):
    def test_saves_player_before_optional_metadata(self):
        film = Mock()
        found_at = datetime(
            2026,
            7,
            22,
            9,
            12,
            tzinfo=datetime_timezone.utc,
        )

        _save_vavada_player_presence(film, 12300691, found_at=found_at)

        self.assertEqual(
            film.film_content,
            "https://iframe.cloud/iframe/12300691",
        )
        self.assertEqual(film.add_content_date, found_at.date())
        self.assertEqual(film.last_update, found_at)
        film.save.assert_called_once_with(
            update_fields=[
                "film_content",
                "add_content_date",
                "last_update",
            ]
        )

    @patch("apps.scrapers.tasks.vavada.Content.objects.filter")
    def test_finishes_player_parse_without_metadata(self, filter_mock):
        film = Mock(pk=77)
        parsed_at = datetime(
            2026,
            7,
            22,
            9,
            13,
            tzinfo=datetime_timezone.utc,
        )

        _finish_vavada_parse(film, parsed_at=parsed_at)

        filter_mock.assert_called_once_with(
            pk=77,
            is_parsed_ru="in_progress",
        )
        update_kwargs = filter_mock.return_value.update.call_args.kwargs
        self.assertEqual(update_kwargs["is_parsed_ru"], "parsed")
        self.assertEqual(update_kwargs["parsed_at_ru"], parsed_at)
        self.assertEqual(
            str(update_kwargs["parse_count_ru"]),
            str(F("parse_count_ru") + 1),
        )
