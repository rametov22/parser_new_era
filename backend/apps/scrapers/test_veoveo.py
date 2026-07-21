from datetime import datetime, timezone as datetime_timezone
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.scrapers.models import VeoVeoSyncState
from apps.scrapers.tasks.veoveo import (
    SYNC_STATE_KEY,
    run_veoveo_incremental_sync,
)
from apps.scrapers.veoveo_catalog import (
    VeoVeoCatalogClient,
    VeoVeoCatalogPage,
    derive_last_season_episode,
    normalize_veoveo_content,
)


class VeoVeoCatalogTests(SimpleTestCase):
    def test_client_sends_incremental_window(self):
        session = Mock()
        session.headers = {}
        response = Mock()
        response.json.return_value = {
            "data": [],
            "meta": {
                "page": 1,
                "pageSize": 100,
                "total": 0,
                "pages": 0,
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
        window_start = datetime(
            2026, 7, 20, 10, 0, tzinfo=datetime_timezone.utc
        )
        window_end = datetime(
            2026, 7, 20, 10, 5, tzinfo=datetime_timezone.utc
        )

        client.get_details_page(
            page=1,
            page_size=100,
            from_updated_at=window_start,
            to_updated_at=window_end,
        )

        session.post.assert_called_once_with(
            "https://catalog.example/v1/contents/details",
            json={
                "fromUpdatedAt": "2026-07-20T10:00:00.000Z",
                "toUpdatedAt": "2026-07-20T10:05:00.000Z",
                "pagination": {
                    "page": 1,
                    "pageSize": 100,
                    "type": "page",
                    "order": "ASC",
                    "sortBy": "id",
                },
            },
            timeout=12,
        )

    def test_normalizes_serial_metadata(self):
        seen_at = timezone.now()

        normalized = normalize_veoveo_content(
            {
                "id": 77,
                "kinopoiskId": 464963,
                "contentType": {"slug": "series"},
                "episodesBySeason": {"8": 6},
                "episodesByVoiceAuthors": [
                    {
                        "seasons": [
                            {
                                "seasonOrdering": 8,
                                "episodes": [1, 2, 3, 4, 5, 6],
                            }
                        ]
                    }
                ],
                "updatedAt": "2026-07-20T10:00:00Z",
                "ageRestriction": "AGE_18",
                "duration": 57,
            },
            seen_at=seen_at,
        )

        self.assertEqual(normalized["veoveo_id"], 77)
        self.assertEqual(normalized["kinopoisk_id"], 464963)
        self.assertEqual(normalized["content_type"], "series")
        self.assertEqual(normalized["last_season"], 8)
        self.assertEqual(normalized["last_episode"], 6)
        self.assertEqual(normalized["age_restriction"], 18)
        self.assertEqual(normalized["duration"], 57)

    def test_latest_episode_uses_all_voice_authors(self):
        result = derive_last_season_episode(
            episodes_by_season={"9": 3},
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


@override_settings(
    VEOVEO_API_TOKEN="website-token",
    VEOVEO_CATALOG_API_URL="https://catalog.example",
    VEOVEO_REQUEST_TIMEOUT_SECONDS=12,
    VEOVEO_INCREMENTAL_PAGE_SIZE=100,
    VEOVEO_SYNC_OVERLAP_SECONDS=300,
    VEOVEO_INITIAL_LOOKBACK_HOURS=24,
    VEOVEO_SYNC_LOCK_TIMEOUT_SECONDS=1800,
)
class VeoVeoIncrementalSyncTests(TestCase):
    databases = {"default"}

    @patch("apps.scrapers.tasks.veoveo._upsert_rows", return_value=(1, 0))
    @patch("apps.scrapers.tasks.veoveo._bootstrap_cursor")
    @patch("apps.scrapers.tasks.veoveo.VeoVeoCatalogClient")
    def test_success_advances_cursor_after_upsert(
        self,
        client_class,
        bootstrap_cursor,
        upsert_rows,
    ):
        window_start = timezone.now().replace(microsecond=0)
        bootstrap_cursor.return_value = window_start
        client_class.return_value.get_details_page.return_value = (
            VeoVeoCatalogPage(
                items=[{"id": 99, "updatedAt": window_start.isoformat()}],
                page=1,
                page_size=100,
                total=1,
                pages=1,
                has_next_page=False,
            )
        )

        result = run_veoveo_incremental_sync()

        state = VeoVeoSyncState.objects.get(key=SYNC_STATE_KEY)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["created"], 1)
        self.assertEqual(state.status, VeoVeoSyncState.STATUS_SUCCESS)
        self.assertEqual(state.cursor_at, state.last_to_updated_at)
        self.assertIsNone(state.run_token)
        upsert_rows.assert_called_once()

    @patch(
        "apps.scrapers.tasks.veoveo._bootstrap_cursor",
        return_value=timezone.now(),
    )
    @patch("apps.scrapers.tasks.veoveo.VeoVeoCatalogClient")
    def test_failure_keeps_previous_cursor(
        self,
        client_class,
        bootstrap_cursor,
    ):
        previous_cursor = timezone.now().replace(microsecond=0)
        VeoVeoSyncState.objects.create(
            key=SYNC_STATE_KEY,
            cursor_at=previous_cursor,
        )
        client_class.return_value.get_details_page.side_effect = RuntimeError(
            "temporary API failure"
        )

        with self.assertRaisesMessage(RuntimeError, "temporary API failure"):
            run_veoveo_incremental_sync()

        state = VeoVeoSyncState.objects.get(key=SYNC_STATE_KEY)
        self.assertEqual(state.cursor_at, previous_cursor)
        self.assertEqual(state.status, VeoVeoSyncState.STATUS_ERROR)
        self.assertIsNone(state.run_token)
        self.assertIn("temporary API failure", state.last_error)

    @patch(
        "apps.scrapers.tasks.veoveo._bootstrap_cursor",
        side_effect=RuntimeError("main database unavailable"),
    )
    def test_bootstrap_failure_releases_lock(self, bootstrap_cursor):
        with self.assertRaisesMessage(
            RuntimeError,
            "main database unavailable",
        ):
            run_veoveo_incremental_sync()

        state = VeoVeoSyncState.objects.get(key=SYNC_STATE_KEY)
        self.assertEqual(state.status, VeoVeoSyncState.STATUS_ERROR)
        self.assertIsNone(state.run_token)
        self.assertIsNone(state.running_since)
