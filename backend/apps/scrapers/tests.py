from contextlib import nullcontext
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from selenium.common.exceptions import WebDriverException

from config.router import ScraperRouter

from .chrome_utils import apply_vavada_trust_cookie
from .models import ScraperLog, VeoVeoContent
from .release_quality import has_pirated_release
from .tasks.vavada_serials import _episode_number
from .veoveo_catalog import (
    VeoVeoCatalogClient,
    VeoVeoCatalogDataError,
    VeoVeoCatalogPage,
    derive_last_season_episode,
    normalize_veoveo_content,
)


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


class VavadaTrustCookieTests(SimpleTestCase):
    @override_settings(
        VAVADA_WD_TRUST_COOKIE="",
        VAVADA_WD_APPROVAL_COOKIE="",
    )
    def test_skips_cookie_when_value_is_empty(self):
        driver = Mock()

        self.assertFalse(apply_vavada_trust_cookie(driver))
        driver.execute_cdp_cmd.assert_not_called()

    @override_settings(
        VAVADA_WD_TRUST_COOKIE="test-trust-token",
        VAVADA_WD_APPROVAL_COOKIE="test-approval-token",
    )
    def test_installs_cookie_for_obrut_domain(self):
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {"success": True}

        self.assertTrue(apply_vavada_trust_cookie(driver))
        self.assertEqual(driver.execute_cdp_cmd.call_count, 2)
        driver.execute_cdp_cmd.assert_any_call(
            "Network.setCookie",
            {
                "name": "wd_trust",
                "value": "test-trust-token",
                "domain": ".obrut.show",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
                "priority": "High",
            },
        )
        driver.execute_cdp_cmd.assert_any_call(
            "Network.setCookie",
            {
                "name": "wd_approval",
                "value": "test-approval-token",
                "domain": ".obrut.show",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
                "priority": "High",
                "partitionKey": {
                    "topLevelSite": "https://iframe.cloud",
                    "hasCrossSiteAncestor": True,
                },
            },
        )

    @override_settings(
        VAVADA_WD_TRUST_COOKIE="test-trust-token",
        VAVADA_WD_APPROVAL_COOKIE="test-approval-token",
    )
    def test_raises_when_chrome_rejects_cookie(self):
        driver = Mock()
        driver.execute_cdp_cmd.side_effect = [
            {"success": True},
            {"success": False},
        ]

        with self.assertRaises(WebDriverException):
            apply_vavada_trust_cookie(driver)

    @override_settings(
        VAVADA_WD_TRUST_COOKIE="test-trust-token",
        VAVADA_WD_APPROVAL_COOKIE="",
    )
    def test_requires_both_verification_cookies(self):
        driver = Mock()

        with self.assertRaisesMessage(
            WebDriverException,
            "Both VAVADA_WD_TRUST_COOKIE and VAVADA_WD_APPROVAL_COOKIE are required",
        ):
            apply_vavada_trust_cookie(driver)
        driver.execute_cdp_cmd.assert_not_called()


class VeoVeoCatalogTests(SimpleTestCase):
    def test_normalizes_availability_and_latest_episode(self):
        seen_at = timezone.now()
        payload = {
            "id": 77,
            "kinopoiskId": 464963,
            "imdbId": "tt0944947",
            "title": "Game of Thrones",
            "originalTitle": "Game of Thrones",
            "year": 2011,
            "contentType": {"id": 2, "name": "Series", "slug": "series"},
            "videoQuality": "FULLHD",
            "audioTracks": "DUB, MVO",
            "voiceAuthorsV2": [
                {"id": 10, "name": "LostFilm"},
                {"id": 11, "name": "HDrezka Studio"},
            ],
            "languages": [{"id": 1, "name": "Russian", "slug": "ru"}],
            "seasonsCount": 8,
            "episodesCount": 73,
            "episodesBySeason": {"7": 7, "8": 6},
            "episodesByVoiceAuthors": [
                {
                    "voiceAuthorId": 10,
                    "name": "LostFilm",
                    "seasons": [
                        {"seasonOrdering": 8, "episodes": [1, 2, 3, 4, 5, 6]}
                    ],
                }
            ],
            "createdAt": "2024-01-01T10:00:00Z",
            "updatedAt": "2026-07-18T10:00:00Z",
            "playerUrl": "https://example.invalid/temporary",
        }

        normalized = normalize_veoveo_content(payload, seen_at=seen_at)

        self.assertEqual(normalized["veoveo_id"], 77)
        self.assertEqual(normalized["kinopoisk_id"], 464963)
        self.assertEqual(normalized["content_type"], "series")
        self.assertTrue(normalized["is_available"])
        self.assertEqual(normalized["last_season"], 8)
        self.assertEqual(normalized["last_episode"], 6)
        self.assertEqual(len(normalized["voice_authors"]), 2)
        self.assertEqual(normalized["last_seen_at"], seen_at)
        self.assertNotIn("player_url", normalized)

    def test_latest_episode_uses_all_voice_authors(self):
        result = derive_last_season_episode(
            episodes_by_season={"8": 6},
            episodes_by_voice_authors=[
                {
                    "seasons": [
                        {"seasonOrdering": 9, "episodes": [1, 2, 3]},
                    ]
                },
                {
                    "seasons": [
                        {"seasonOrdering": 9, "episodes": [1, 2, 4]},
                    ]
                },
            ],
            seasons_count=9,
            episodes_count=77,
        )

        self.assertEqual(result, (9, 4))

    def test_latest_episode_falls_back_to_season_counts(self):
        result = derive_last_season_episode(
            episodes_by_season={"1": 10, "2": 7},
            episodes_by_voice_authors=[],
            seasons_count=2,
            episodes_count=17,
        )

        self.assertEqual(result, (2, 7))

    def test_client_sends_bearer_token_and_stable_pagination(self):
        session = Mock()
        session.headers = {}
        response = Mock()
        response.json.return_value = {
            "data": [{"id": 1}],
            "meta": {
                "page": 1,
                "pageSize": 100,
                "total": 1,
                "pages": 1,
                "hasNextPage": False,
            },
        }
        session.post.return_value = response
        client = VeoVeoCatalogClient(
            base_url="https://catalog.example/",
            token="website-token",
            timeout=12,
            session=session,
        )

        page = client.get_details_page(page=1, page_size=100)

        self.assertEqual(page.total, 1)
        self.assertEqual(
            session.headers["Authorization"],
            "Bearer website-token",
        )
        session.post.assert_called_once_with(
            "https://catalog.example/v1/contents/details",
            json={
                "pagination": {
                    "page": 1,
                    "pageSize": 100,
                    "type": "page",
                    "order": "ASC",
                    "sortBy": "id",
                }
            },
            timeout=12,
        )

    def test_client_rejects_inconsistent_page(self):
        session = Mock()
        session.headers = {}
        response = Mock()
        response.json.return_value = {
            "data": [{"id": 1}],
            "meta": {
                "page": 2,
                "total": 1,
                "pages": 1,
                "hasNextPage": False,
            },
        }
        session.post.return_value = response
        client = VeoVeoCatalogClient(
            base_url="https://catalog.example",
            token="website-token",
            session=session,
        )

        with self.assertRaisesMessage(
            VeoVeoCatalogDataError,
            "Requested page 1",
        ):
            client.get_details_page(page=1, page_size=100)


class VeoVeoRouterTests(SimpleTestCase):
    def test_veoveo_model_uses_main_database(self):
        router = ScraperRouter()

        self.assertEqual(router.db_for_read(VeoVeoContent), "main_db")
        self.assertEqual(router.db_for_write(VeoVeoContent), "main_db")
        self.assertTrue(
            router.allow_migrate(
                "main_db",
                "scrapers",
                model_name="veoveocontent",
            )
        )
        self.assertFalse(
            router.allow_migrate(
                "default",
                "scrapers",
                model_name="veoveocontent",
            )
        )

    def test_technical_model_stays_in_default_database(self):
        router = ScraperRouter()

        self.assertEqual(router.db_for_read(ScraperLog), "default")


class VeoVeoSyncCommandTests(SimpleTestCase):
    @override_settings(
        VEOVEO_API_TOKEN="website-token",
        VEOVEO_CATALOG_API_URL="https://catalog.example",
        VEOVEO_REQUEST_TIMEOUT_SECONDS=12,
    )
    @patch(
        "apps.scrapers.management.commands.sync_veoveo_catalog."
        "VeoVeoCatalogClient"
    )
    def test_dry_run_fetches_without_database_writes(self, client_class):
        client_class.return_value.get_details_page.return_value = VeoVeoCatalogPage(
            items=[
                {
                    "id": 1,
                    "title": "Movie",
                    "originalTitle": "Movie",
                    "year": 2026,
                    "contentType": {
                        "id": 1,
                        "name": "Movie",
                        "slug": "movie",
                    },
                }
            ],
            page=1,
            page_size=100,
            total=1,
            pages=1,
            has_next_page=False,
        )
        stdout = StringIO()

        call_command(
            "sync_veoveo_catalog",
            dry_run=True,
            stdout=stdout,
        )

        self.assertIn("rows=1", stdout.getvalue())
        self.assertIn("dry_run=True", stdout.getvalue())

    @override_settings(
        VEOVEO_API_TOKEN="website-token",
        VEOVEO_CATALOG_API_URL="https://catalog.example",
        VEOVEO_REQUEST_TIMEOUT_SECONDS=12,
    )
    @patch(
        "apps.scrapers.management.commands.sync_veoveo_catalog."
        "transaction.atomic",
        return_value=nullcontext(),
    )
    @patch(
        "apps.scrapers.management.commands.sync_veoveo_catalog."
        "VeoVeoCatalogClient"
    )
    def test_full_sync_upserts_pages_and_deactivates_missing_rows(
        self,
        client_class,
        atomic,
    ):
        client_class.return_value.get_details_page.side_effect = [
            VeoVeoCatalogPage(
                items=[
                    {
                        "id": 1,
                        "kinopoiskId": 101,
                        "title": "First",
                        "originalTitle": "First",
                        "year": 2025,
                        "contentType": {"slug": "movie"},
                    }
                ],
                page=1,
                page_size=1,
                total=2,
                pages=2,
                has_next_page=True,
            ),
            VeoVeoCatalogPage(
                items=[
                    {
                        "id": 2,
                        "kinopoiskId": 102,
                        "title": "Second",
                        "originalTitle": "Second",
                        "year": 2026,
                        "contentType": {"slug": "series"},
                        "episodesBySeason": {"1": 5},
                    }
                ],
                page=2,
                page_size=1,
                total=2,
                pages=2,
                has_next_page=False,
            ),
        ]
        manager = Mock()
        manager.using.return_value = manager
        stale_rows = Mock()
        stale_rows.update.return_value = 3
        manager.filter.return_value = stale_rows
        stdout = StringIO()

        with patch.object(VeoVeoContent, "objects", manager):
            call_command(
                "sync_veoveo_catalog",
                page_size=1,
                stdout=stdout,
            )

        self.assertEqual(manager.bulk_create.call_count, 2)
        self.assertEqual(atomic.call_count, 2)
        manager.filter.assert_called_once()
        stale_rows.update.assert_called_once()
        self.assertIn("deactivated=3", stdout.getvalue())

    @override_settings(VEOVEO_API_TOKEN="")
    def test_command_requires_website_token(self):
        with self.assertRaisesMessage(CommandError, "VEOVEO_API_TOKEN is empty"):
            call_command("sync_veoveo_catalog", dry_run=True)
