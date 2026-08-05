import uuid
from io import BytesIO
from typing import ClassVar
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from apps.scrapers.models import VeoVeoSyncState
from apps.scrapers.tasks.veoveo import (
    UPDATE_FIELDS,
    sync_veoveo_full_catalog,
    sync_veoveo_updates,
)
from apps.scrapers.tasks.veoveo_previews import (
    PREVIEW_SYNC_STATE_KEY,
    _download_preview_row,
    run_veoveo_preview_pipeline,
)
from apps.scrapers.tmdb_episode_previews import (
    TMDbEpisodePreviewClient,
    add_episode_placeholders,
)
from apps.scrapers.veoveo_episode_preview_storage import (
    EpisodePreviewStorageClient,
    StoredEpisodePreview,
    has_pending_episode_preview_downloads,
    merge_episode_preview_storage,
)
from apps.scrapers.veoveo_episode_previews import (
    VeoVeoEpisodePreviewClient,
    VeoVeoEpisodePreviewNotFound,
    normalize_episode_previews,
)

PREVIEW_SETTINGS = {
    "VEOVEO_EPISODE_PREVIEW_WORKERS": 2,
    "VEOVEO_EPISODE_PREVIEW_DOWNLOAD_WORKERS": 2,
    "VEOVEO_EPISODE_PREVIEW_MAX_BYTES": 1024 * 1024,
    "VEOVEO_EPISODE_PREVIEW_ALLOWED_HOSTS": ("video.example",),
    "VEOVEO_EPISODE_PIPELINE_LOCK_TIMEOUT_SECONDS": 3600,
}


class FakeStorage:
    def __init__(self):
        self.files = {}

    def save(self, name, content):
        self.files[name] = content.read()
        return name


def jpeg_bytes():
    buffer = BytesIO()
    Image.new("RGB", (8, 6), color="red").save(buffer, format="JPEG")
    return buffer.getvalue()


class VeoVeoPreviewServiceTests(SimpleTestCase):
    def test_catalog_upsert_does_not_overwrite_preview_fields(self):
        self.assertNotIn("episode_previews", UPDATE_FIELDS)
        self.assertNotIn("episode_previews_synced_at", UPDATE_FIELDS)
        self.assertNotIn("episode_previews_error", UPDATE_FIELDS)
        self.assertNotIn("episode_previews_downloaded_at", UPDATE_FIELDS)
        self.assertNotIn("episode_previews_download_error", UPDATE_FIELDS)

    def test_episode_client_classifies_404_without_leaking_token(self):
        response = Mock(status_code=404)
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        session = Mock()
        session.get.return_value = response
        client = VeoVeoEpisodePreviewClient(timeout=10, session=session)

        with self.assertRaises(VeoVeoEpisodePreviewNotFound) as caught:
            client.get_episode_previews(
                veoveo_id=37398,
                player_url=(
                    "https://player.example/balancer-api/iframe"
                    "?movie_id=37398&token=secret-token"
                ),
            )

        self.assertNotIn("secret-token", str(caught.exception))

    def test_normalizer_ignores_empty_zero_episode_placeholder(self):
        result = normalize_episode_previews(
            [
                {
                    "id": 123,
                    "order": 0,
                    "season": {"id": 99, "order": 0},
                    "episodeVariants": [],
                }
            ]
        )

        self.assertEqual(result, [])

    def test_storage_client_validates_and_uploads_image(self):
        image = jpeg_bytes()
        response = Mock()
        response.url = "https://video.example/episode/15.jpg"
        response.headers = {"Content-Length": str(len(image))}
        response.iter_content.return_value = [image]
        session = Mock()
        session.get.return_value = response
        storage = FakeStorage()
        client = EpisodePreviewStorageClient(
            allowed_hosts=("video.example",),
            max_bytes=1024 * 1024,
            timeout=10,
            storage=storage,
            session=session,
        )

        result = client.download_and_store(response.url)

        self.assertTrue(result.created)
        self.assertEqual(result.image_format, "JPEG")
        self.assertEqual(storage.files[result.key], image)

    def test_metadata_refresh_keeps_storage_only_for_same_source(self):
        existing = [
            {
                "season": 1,
                "episode": 1,
                "episode_id": 10,
                "preview_url": "https://video.example/old.jpg",
                "preview_storage_key": "veoveo/episode-previews/old.jpg",
            }
        ]
        unchanged = merge_episode_preview_storage(
            existing,
            [
                {
                    "season": 1,
                    "episode": 1,
                    "episode_id": 10,
                    "preview_url": "https://video.example/old.jpg",
                }
            ],
        )
        changed = merge_episode_preview_storage(
            existing,
            [
                {
                    "season": 1,
                    "episode": 1,
                    "episode_id": 10,
                    "preview_url": "https://video.example/new.jpg",
                }
            ],
        )

        self.assertIn("preview_storage_key", unchanged[0])
        self.assertNotIn("preview_storage_key", changed[0])
        self.assertFalse(has_pending_episode_preview_downloads(unchanged))
        self.assertTrue(has_pending_episode_preview_downloads(changed))

    def test_download_row_adds_s3_metadata_and_keeps_source_url(self):
        client = Mock()
        client.download_and_store.return_value = StoredEpisodePreview(
            key="veoveo/episode-previews/aa/image.jpg",
            size=32000,
            image_format="JPEG",
            created=True,
        )
        row = {
            "episode_previews": [
                {
                    "season": 2,
                    "episode": 3,
                    "preview_url": "https://video.example/source.jpg",
                }
            ]
        }

        result = _download_preview_row(row, client, set())

        preview = result["previews"][0]
        self.assertEqual(
            preview["preview_url"], row["episode_previews"][0]["preview_url"]
        )
        self.assertEqual(
            preview["preview_storage_key"],
            "veoveo/episode-previews/aa/image.jpg",
        )
        self.assertEqual(result["downloaded"], 1)

    def test_download_row_keeps_successes_when_another_preview_fails(self):
        client = Mock()
        client.download_and_store.side_effect = [
            StoredEpisodePreview(
                key="veoveo/episode-previews/aa/image.jpg",
                size=32000,
                image_format="JPEG",
                created=True,
            ),
            RuntimeError("provider unavailable"),
        ]
        row = {
            "episode_previews": [
                {
                    "season": 1,
                    "episode": 1,
                    "preview_url": "https://video.example/one.jpg",
                },
                {
                    "season": 1,
                    "episode": 2,
                    "preview_url": "https://video.example/two.jpg",
                },
            ]
        }

        result = _download_preview_row(row, client, set())

        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertIn("preview_storage_key", result["previews"][0])
        self.assertNotIn("preview_storage_key", result["previews"][1])

    def test_tmdb_fill_missing_keeps_existing_veoveo_previews(self):
        find_response = Mock()
        find_response.json.return_value = {"tv_results": [{"id": 1399}]}
        first_episode_response = Mock()
        first_episode_response.json.return_value = {
            "stills": [{"file_path": "/got-s1e1.jpg", "vote_average": 7.0}]
        }
        third_episode_response = Mock()
        third_episode_response.json.return_value = {
            "stills": [{"file_path": "/got-s1e3.jpg", "vote_average": 8.0}]
        }
        session = Mock()
        session.get.side_effect = [
            find_response,
            first_episode_response,
            third_episode_response,
        ]
        client = TMDbEpisodePreviewClient(
            api_key="tmdb-key",
            timeout=10,
            session=session,
        )

        previews, filled = client.fill_missing_episode_previews(
            imdb_id="tt0944947",
            previews=[
                {"season": 1, "episode": 1, "preview_url": None},
                {
                    "season": 1,
                    "episode": 2,
                    "preview_url": "https://video.example/veoveo-s1e2.jpg",
                },
                {"season": 1, "episode": 3, "preview_url": None},
            ],
            max_missing=10,
        )

        self.assertEqual(filled, 2)
        self.assertEqual(
            previews[0]["preview_url"],
            "https://image.tmdb.org/t/p/original/got-s1e1.jpg",
        )
        self.assertEqual(
            previews[1]["preview_url"],
            "https://video.example/veoveo-s1e2.jpg",
        )
        self.assertNotIn("preview_source", previews[1])
        self.assertEqual(previews[2]["preview_source"], "tmdb")
        self.assertEqual(session.get.call_count, 3)

    def test_tmdb_fill_missing_ignores_missing_episode_and_continues(self):
        find_response = Mock()
        find_response.json.return_value = {"tv_results": [{"id": 1399}]}
        first_episode_response = Mock()
        first_episode_response.json.return_value = {
            "stills": [{"file_path": "/got-s1e1.jpg", "vote_average": 7.0}]
        }
        missing_episode_response = Mock(status_code=404)
        missing_episode_response.raise_for_status.side_effect = requests.HTTPError(
            response=missing_episode_response
        )
        third_episode_response = Mock()
        third_episode_response.json.return_value = {
            "stills": [{"file_path": "/got-s1e3.jpg", "vote_average": 8.0}]
        }
        session = Mock()
        session.get.side_effect = [
            find_response,
            first_episode_response,
            missing_episode_response,
            third_episode_response,
        ]
        client = TMDbEpisodePreviewClient(
            api_key="tmdb-key",
            timeout=10,
            session=session,
        )

        previews, filled = client.fill_missing_episode_previews(
            imdb_id="tt0944947",
            previews=[
                {"season": 1, "episode": 1, "preview_url": None},
                {"season": 8, "episode": 7, "preview_url": None},
                {"season": 1, "episode": 3, "preview_url": None},
            ],
            max_missing=10,
        )

        self.assertEqual(filled, 2)
        self.assertEqual(previews[0]["preview_source"], "tmdb")
        self.assertIsNone(previews[1].get("preview_url"))
        self.assertEqual(previews[2]["preview_source"], "tmdb")
        self.assertEqual(session.get.call_count, 4)

    def test_episode_placeholders_fill_missing_orders_from_veoveo_counts(self):
        result = add_episode_placeholders(
            [
                {
                    "season": 1,
                    "episode": 2,
                    "preview_url": "https://video.example/veoveo-s1e2.jpg",
                }
            ],
            {"1": 3, "2": 2},
            max_episodes=10,
        )

        self.assertEqual(
            [(item["season"], item["episode"]) for item in result],
            [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)],
        )
        self.assertEqual(
            result[1]["preview_url"],
            "https://video.example/veoveo-s1e2.jpg",
        )


@override_settings(**PREVIEW_SETTINGS)
class VeoVeoPreviewPipelineTests(TestCase):
    databases: ClassVar[set[str]] = {"default"}

    @patch("apps.scrapers.tasks.veoveo_previews.run_veoveo_episode_preview_download")
    @patch("apps.scrapers.tasks.veoveo_previews.run_veoveo_episode_preview_sync")
    def test_pipeline_runs_metadata_before_download_and_releases_lock(
        self,
        metadata_sync,
        download_sync,
    ):
        metadata_sync.return_value = {
            "processed": 2,
            "updated": 2,
            "episodes": 10,
            "previews": 8,
            "unavailable": 0,
            "errors": 0,
        }
        download_sync.return_value = {
            "processed": 2,
            "completed": 2,
            "downloaded": 8,
            "reused": 0,
            "skipped": 0,
            "errors": 0,
        }

        result = run_veoveo_preview_pipeline()

        state = VeoVeoSyncState.objects.get(key=PREVIEW_SYNC_STATE_KEY)
        self.assertEqual(result["status"], "success")
        self.assertEqual(state.status, VeoVeoSyncState.STATUS_SUCCESS)
        self.assertIsNone(state.run_token)
        self.assertEqual(state.last_created, 8)
        metadata_sync.assert_called_once_with(
            force=False,
            limit=0,
            kp_id=None,
            veoveo_id=None,
        )
        download_sync.assert_called_once_with(
            force=False,
            limit=0,
            kp_id=None,
            veoveo_id=None,
        )

    @patch("apps.scrapers.tasks.veoveo_previews.run_veoveo_episode_preview_download")
    @patch("apps.scrapers.tasks.veoveo_previews.run_veoveo_episode_preview_sync")
    def test_pipeline_passes_target_filter_to_both_stages(
        self,
        metadata_sync,
        download_sync,
    ):
        metadata_sync.return_value = {
            "processed": 1,
            "updated": 1,
            "episodes": 10,
            "previews": 10,
            "tmdb_previews": 8,
            "unavailable": 0,
            "errors": 0,
        }
        download_sync.return_value = {
            "processed": 1,
            "completed": 1,
            "downloaded": 8,
            "reused": 0,
            "skipped": 2,
            "errors": 0,
        }

        result = run_veoveo_preview_pipeline(force=True, kp_id=464963)

        self.assertEqual(result["status"], "success")
        metadata_sync.assert_called_once_with(
            force=True,
            limit=0,
            kp_id=464963,
            veoveo_id=None,
        )
        download_sync.assert_called_once_with(
            force=True,
            limit=0,
            kp_id=464963,
            veoveo_id=None,
        )

    @patch("apps.scrapers.tasks.veoveo_previews.run_veoveo_episode_preview_download")
    @patch("apps.scrapers.tasks.veoveo_previews.run_veoveo_episode_preview_sync")
    def test_overlapping_pipeline_is_skipped(self, metadata_sync, download_sync):
        VeoVeoSyncState.objects.create(
            key=PREVIEW_SYNC_STATE_KEY,
            run_token=uuid.uuid4(),
            running_since=timezone.now(),
            status=VeoVeoSyncState.STATUS_RUNNING,
        )

        result = run_veoveo_preview_pipeline()

        self.assertEqual(result["status"], "skipped")
        metadata_sync.assert_not_called()
        download_sync.assert_not_called()


class VeoVeoPreviewDispatchTests(SimpleTestCase):
    @patch("apps.scrapers.tasks.veoveo_previews.sync_veoveo_previews.delay")
    @patch("apps.scrapers.tasks.veoveo.run_veoveo_incremental_sync")
    def test_incremental_catalog_dispatches_preview_pipeline(
        self,
        catalog_sync,
        delay,
    ):
        catalog_sync.return_value = {"status": "success"}
        delay.return_value.id = "preview-task"

        result = sync_veoveo_updates.run()

        delay.assert_called_once_with()
        self.assertEqual(result["preview_task_id"], "preview-task")

    @patch("apps.scrapers.tasks.veoveo_previews.sync_veoveo_previews.delay")
    @patch("apps.scrapers.tasks.veoveo.run_veoveo_full_sync")
    def test_full_catalog_dispatches_preview_pipeline(self, catalog_sync, delay):
        catalog_sync.return_value = {"status": "success"}
        delay.return_value.id = "preview-task"

        result = sync_veoveo_full_catalog.run()

        delay.assert_called_once_with()
        self.assertEqual(result["preview_task_id"], "preview-task")
