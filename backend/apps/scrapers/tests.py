from django.test import SimpleTestCase

from .release_quality import has_pirated_release
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
