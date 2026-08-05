import logging
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import timedelta
from threading import Lock, local

from celery import shared_task
from django.conf import settings
from django.core.files.storage import default_storage
from django.db.models import F, Q
from django.utils import timezone

from ..models import VeoVeoContent, VeoVeoSyncState
from ..tmdb_episode_previews import (
    TMDbEpisodePreviewClient,
    TMDbEpisodePreviewError,
    add_episode_placeholders,
)
from ..veoveo_episode_preview_storage import (
    STORAGE_FIELDS,
    EpisodePreviewStorageClient,
    has_pending_episode_preview_downloads,
    list_episode_preview_storage_keys,
    merge_episode_preview_storage,
)
from ..veoveo_episode_previews import (
    VeoVeoEpisodePreviewClient,
    VeoVeoEpisodePreviewNotFound,
)

logger = logging.getLogger(__name__)

MAIN_DB_ALIAS = "main_db"
PREVIEW_SYNC_STATE_KEY = "episode_previews"
SERIAL_CONTENT_TYPES = ("serial", "docserial", "multserial", "tvshow")
MAX_ERROR_LENGTH = 2000
PROGRESS_EVERY = 100


def _validate_preview_settings():
    for name in (
        "VEOVEO_EPISODE_PREVIEW_WORKERS",
        "VEOVEO_EPISODE_PREVIEW_DOWNLOAD_WORKERS",
    ):
        value = getattr(settings, name)
        if not 1 <= value <= 32:
            raise RuntimeError(f"{name} must be between 1 and 32")
    if settings.VEOVEO_EPISODE_PREVIEW_MAX_BYTES <= 0:
        raise RuntimeError("VEOVEO_EPISODE_PREVIEW_MAX_BYTES must be positive")
    if not settings.VEOVEO_EPISODE_PREVIEW_ALLOWED_HOSTS:
        raise RuntimeError("VEOVEO_EPISODE_PREVIEW_ALLOWED_HOSTS cannot be empty")
    if settings.VEOVEO_EPISODE_PIPELINE_LOCK_TIMEOUT_SECONDS <= 0:
        raise RuntimeError(
            "VEOVEO_EPISODE_PIPELINE_LOCK_TIMEOUT_SECONDS must be positive"
        )


def _claim_preview_pipeline():
    VeoVeoSyncState.objects.get_or_create(key=PREVIEW_SYNC_STATE_KEY)
    now = timezone.now()
    stale_before = now - timedelta(
        seconds=settings.VEOVEO_EPISODE_PIPELINE_LOCK_TIMEOUT_SECONDS
    )
    run_token = uuid.uuid4()
    claimed = (
        VeoVeoSyncState.objects.filter(key=PREVIEW_SYNC_STATE_KEY)
        .filter(
            Q(run_token__isnull=True)
            | Q(running_since__isnull=True)
            | Q(running_since__lt=stale_before)
        )
        .update(
            run_token=run_token,
            running_since=now,
            status=VeoVeoSyncState.STATUS_RUNNING,
            last_started_at=now,
            last_error="",
        )
    )
    return run_token if claimed else None


def _finish_preview_pipeline(run_token, metadata, downloads):
    error_count = metadata["errors"] + downloads["errors"]
    unavailable = metadata["unavailable"]
    status = (
        VeoVeoSyncState.STATUS_ERROR if error_count else VeoVeoSyncState.STATUS_SUCCESS
    )
    message = ""
    if error_count:
        message = (
            f"metadata_errors={metadata['errors']}; "
            f"download_errors={downloads['errors']}"
        )
    VeoVeoSyncState.objects.filter(
        key=PREVIEW_SYNC_STATE_KEY,
        run_token=run_token,
    ).update(
        run_token=None,
        running_since=None,
        status=status,
        last_finished_at=timezone.now(),
        last_received=metadata["processed"],
        last_created=downloads["downloaded"],
        last_updated=downloads["completed"],
        last_pages=unavailable,
        last_error=message,
    )


def _finish_preview_pipeline_error(run_token, exc):
    VeoVeoSyncState.objects.filter(
        key=PREVIEW_SYNC_STATE_KEY,
        run_token=run_token,
    ).update(
        run_token=None,
        running_since=None,
        status=VeoVeoSyncState.STATUS_ERROR,
        last_finished_at=timezone.now(),
        last_error=f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH],
    )


def _apply_target_filter(queryset, *, kp_id=None, veoveo_id=None):
    if kp_id is not None:
        return queryset.filter(kinopoisk_id=kp_id)
    if veoveo_id is not None:
        return queryset.filter(veoveo_id=veoveo_id)
    return queryset


def run_veoveo_episode_preview_sync(
    *,
    force=False,
    limit=0,
    kp_id=None,
    veoveo_id=None,
):
    queryset = (
        VeoVeoContent.objects.using(MAIN_DB_ALIAS)
        .filter(is_available=True)
        .exclude(player_url="")
        .filter(
            Q(episodes_count__gt=1)
            | Q(seasons_count__gt=1)
            | Q(content_type__in=SERIAL_CONTENT_TYPES)
        )
        .order_by("veoveo_id")
    )
    queryset = _apply_target_filter(queryset, kp_id=kp_id, veoveo_id=veoveo_id)
    if not force:
        queryset = queryset.filter(
            Q(episode_previews_synced_at__isnull=True)
            | Q(provider_updated_at__gt=F("episode_previews_synced_at"))
        )
    rows = queryset.values(
        "veoveo_id",
        "imdb_id",
        "player_url",
        "episode_previews",
        "episode_previews_downloaded_at",
        "episodes_by_season",
    )
    if limit:
        rows = rows[:limit]
    total = rows.count()
    if not total:
        return {
            "processed": 0,
            "updated": 0,
            "episodes": 0,
            "previews": 0,
            "tmdb_previews": 0,
            "unavailable": 0,
            "errors": 0,
        }

    thread_state = local()

    def fetch(row):
        client = getattr(thread_state, "client", None)
        if client is None:
            client = VeoVeoEpisodePreviewClient(
                timeout=settings.VEOVEO_REQUEST_TIMEOUT_SECONDS,
            )
            thread_state.client = client
        previews = client.get_episode_previews(
            veoveo_id=row["veoveo_id"],
            player_url=row["player_url"],
        )
        previews = add_episode_placeholders(
            previews,
            row["episodes_by_season"],
            max_episodes=settings.TMDB_EPISODE_PREVIEW_MAX_MISSING_PER_CONTENT,
        )
        tmdb_filled = 0
        if settings.TMDB_EPISODE_PREVIEW_ENABLED:
            tmdb_client = getattr(thread_state, "tmdb_client", None)
            if tmdb_client is None:
                tmdb_client = TMDbEpisodePreviewClient(
                    api_key=settings.TMDB_API_KEY,
                    read_access_token=settings.TMDB_READ_ACCESS_TOKEN,
                    api_base_url=settings.TMDB_API_BASE_URL,
                    image_base_url=settings.TMDB_IMAGE_BASE_URL,
                    language=settings.TMDB_LANGUAGE,
                    timeout=settings.VEOVEO_REQUEST_TIMEOUT_SECONDS,
                )
                thread_state.tmdb_client = tmdb_client
            try:
                previews, tmdb_filled = tmdb_client.fill_missing_episode_previews(
                    imdb_id=row["imdb_id"],
                    previews=previews,
                    max_missing=settings.TMDB_EPISODE_PREVIEW_MAX_MISSING_PER_CONTENT,
                )
            except TMDbEpisodePreviewError as exc:
                logger.info(
                    "[veoveo-previews] TMDb fallback skipped id=%s imdb=%s: %s",
                    row["veoveo_id"],
                    row["imdb_id"],
                    exc,
                )
        return previews, tmdb_filled

    processed = 0
    updated = 0
    episodes = 0
    previews_found = 0
    tmdb_previews_found = 0
    unavailable = 0
    errors = 0
    row_iterator = rows.iterator(chunk_size=100)
    workers = settings.VEOVEO_EPISODE_PREVIEW_WORKERS
    in_flight_cap = max(workers * 2, 16)

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="veoveo-preview-sync",
    ) as executor:
        in_flight = {}
        for _ in range(in_flight_cap):
            try:
                row = next(row_iterator)
            except StopIteration:
                break
            in_flight[executor.submit(fetch, row)] = row

        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                row = in_flight.pop(future)
                veoveo_id = row["veoveo_id"]
                processed += 1
                try:
                    previews, tmdb_filled = future.result()
                except VeoVeoEpisodePreviewNotFound as exc:
                    unavailable += 1
                    VeoVeoContent.objects.using(MAIN_DB_ALIAS).filter(
                        pk=veoveo_id
                    ).update(
                        episode_previews_synced_at=timezone.now(),
                        episode_previews_error=str(exc)[:MAX_ERROR_LENGTH],
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one serial
                    errors += 1
                    VeoVeoContent.objects.using(MAIN_DB_ALIAS).filter(
                        pk=veoveo_id
                    ).update(
                        episode_previews_error=str(exc)[:MAX_ERROR_LENGTH],
                    )
                    if errors <= 20:
                        logger.warning(
                            "[veoveo-previews] metadata id=%s failed: %s",
                            veoveo_id,
                            exc,
                        )
                else:
                    previews = merge_episode_preview_storage(
                        row["episode_previews"],
                        previews,
                    )
                    pending = has_pending_episode_preview_downloads(previews)
                    VeoVeoContent.objects.using(MAIN_DB_ALIAS).filter(
                        pk=veoveo_id
                    ).update(
                        episode_previews=previews,
                        episode_previews_synced_at=timezone.now(),
                        episode_previews_error="",
                        episode_previews_downloaded_at=(
                            None
                            if pending
                            else row["episode_previews_downloaded_at"] or timezone.now()
                        ),
                        episode_previews_download_error="",
                    )
                    updated += 1
                    episodes += len(previews)
                    previews_found += sum(
                        bool(item.get("preview_url")) for item in previews
                    )
                    tmdb_previews_found += tmdb_filled

                if processed % PROGRESS_EVERY == 0 or processed == total:
                    logger.info(
                        "[veoveo-previews] metadata %s/%s updated=%s "
                        "episodes=%s previews=%s tmdb=%s unavailable=%s errors=%s",
                        processed,
                        total,
                        updated,
                        episodes,
                        previews_found,
                        tmdb_previews_found,
                        unavailable,
                        errors,
                    )

                try:
                    row = next(row_iterator)
                except StopIteration:
                    continue
                in_flight[executor.submit(fetch, row)] = row

    return {
        "processed": processed,
        "updated": updated,
        "episodes": episodes,
        "previews": previews_found,
        "tmdb_previews": tmdb_previews_found,
        "unavailable": unavailable,
        "errors": errors,
    }


def _download_preview_row(row, client, known_keys):
    previews = []
    downloaded = 0
    reused = 0
    skipped = 0
    error_messages = []

    for original in row["episode_previews"]:
        if not isinstance(original, dict):
            continue
        item = dict(original)
        source_url = item.get("preview_url")
        if not source_url:
            previews.append(item)
            continue

        current_key = item.get("preview_storage_key")
        if current_key and current_key in known_keys:
            skipped += 1
            previews.append(item)
            continue
        for field in STORAGE_FIELDS:
            item.pop(field, None)

        try:
            stored = client.download_and_store(source_url)
        except Exception as exc:  # noqa: BLE001 - keep other episodes
            error_messages.append(f"s{item.get('season')}e{item.get('episode')}: {exc}")
        else:
            item.update(
                {
                    "preview_storage_key": stored.key,
                    "preview_storage_bytes": stored.size,
                    "preview_storage_format": stored.image_format,
                }
            )
            downloaded += int(stored.created)
            reused += int(not stored.created)
        previews.append(item)

    return {
        "previews": previews,
        "downloaded": downloaded,
        "reused": reused,
        "skipped": skipped,
        "errors": len(error_messages),
        "error": "; ".join(error_messages)[:MAX_ERROR_LENGTH],
    }


def run_veoveo_episode_preview_download(
    *,
    force=False,
    limit=0,
    kp_id=None,
    veoveo_id=None,
):
    queryset = (
        VeoVeoContent.objects.using(MAIN_DB_ALIAS)
        .filter(
            is_available=True,
            episode_previews_synced_at__isnull=False,
        )
        .exclude(episode_previews=[])
        .order_by("veoveo_id")
    )
    queryset = _apply_target_filter(queryset, kp_id=kp_id, veoveo_id=veoveo_id)
    if not force:
        queryset = queryset.filter(
            Q(episode_previews_downloaded_at__isnull=True)
            | Q(episode_previews_synced_at__gt=F("episode_previews_downloaded_at"))
        )
    rows = queryset.values("veoveo_id", "episode_previews")
    if limit:
        rows = rows[:limit]
    total = rows.count()
    if not total:
        return {
            "processed": 0,
            "completed": 0,
            "downloaded": 0,
            "reused": 0,
            "skipped": 0,
            "errors": 0,
        }

    logger.info("[veoveo-previews] listing existing S3 objects")
    known_keys = list_episode_preview_storage_keys(default_storage)
    known_keys_lock = Lock()
    in_flight_keys = {}
    thread_state = local()

    def process(row):
        client = getattr(thread_state, "client", None)
        if client is None:
            client = EpisodePreviewStorageClient(
                allowed_hosts=settings.VEOVEO_EPISODE_PREVIEW_ALLOWED_HOSTS,
                max_bytes=settings.VEOVEO_EPISODE_PREVIEW_MAX_BYTES,
                timeout=settings.VEOVEO_REQUEST_TIMEOUT_SECONDS,
                storage=default_storage,
                known_keys=known_keys,
                known_keys_lock=known_keys_lock,
                in_flight_keys=in_flight_keys,
            )
            thread_state.client = client
        return _download_preview_row(row, client, known_keys)

    processed = 0
    completed = 0
    downloaded = 0
    reused = 0
    skipped = 0
    errors = 0
    row_iterator = rows.iterator(chunk_size=100)
    workers = settings.VEOVEO_EPISODE_PREVIEW_DOWNLOAD_WORKERS
    in_flight_cap = max(workers * 2, 16)

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="veoveo-preview-download",
    ) as executor:
        in_flight = {}
        for _ in range(in_flight_cap):
            try:
                row = next(row_iterator)
            except StopIteration:
                break
            in_flight[executor.submit(process, row)] = row["veoveo_id"]

        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                veoveo_id = in_flight.pop(future)
                processed += 1
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - isolate one serial
                    result = {
                        "previews": None,
                        "downloaded": 0,
                        "reused": 0,
                        "skipped": 0,
                        "errors": 1,
                        "error": str(exc)[:MAX_ERROR_LENGTH],
                    }

                downloaded += result["downloaded"]
                reused += result["reused"]
                skipped += result["skipped"]
                errors += result["errors"]
                if result["previews"] is not None:
                    is_complete = not result["errors"] and not (
                        has_pending_episode_preview_downloads(result["previews"])
                    )
                    VeoVeoContent.objects.using(MAIN_DB_ALIAS).filter(
                        pk=veoveo_id
                    ).update(
                        episode_previews=result["previews"],
                        episode_previews_downloaded_at=(
                            timezone.now() if is_complete else None
                        ),
                        episode_previews_download_error=result["error"],
                    )
                    completed += int(is_complete)
                else:
                    VeoVeoContent.objects.using(MAIN_DB_ALIAS).filter(
                        pk=veoveo_id
                    ).update(
                        episode_previews_downloaded_at=None,
                        episode_previews_download_error=result["error"],
                    )
                if result["error"] and errors <= 20:
                    logger.warning(
                        "[veoveo-previews] download id=%s failed: %s",
                        veoveo_id,
                        result["error"],
                    )

                if processed % PROGRESS_EVERY == 0 or processed == total:
                    logger.info(
                        "[veoveo-previews] download %s/%s completed=%s "
                        "downloaded=%s reused=%s skipped=%s errors=%s",
                        processed,
                        total,
                        completed,
                        downloaded,
                        reused,
                        skipped,
                        errors,
                    )

                try:
                    row = next(row_iterator)
                except StopIteration:
                    continue
                in_flight[executor.submit(process, row)] = row["veoveo_id"]

    return {
        "processed": processed,
        "completed": completed,
        "downloaded": downloaded,
        "reused": reused,
        "skipped": skipped,
        "errors": errors,
    }


def run_veoveo_preview_pipeline(
    *,
    force=False,
    limit=0,
    kp_id=None,
    veoveo_id=None,
):
    _validate_preview_settings()
    if limit < 0:
        raise ValueError("limit cannot be negative")
    if kp_id is not None and veoveo_id is not None:
        raise ValueError("kp_id and veoveo_id cannot be used together")
    run_token = _claim_preview_pipeline()
    if run_token is None:
        logger.info("[veoveo-previews] pipeline already running; skipping")
        return {"status": "skipped", "reason": "already_running"}

    try:
        metadata = run_veoveo_episode_preview_sync(
            force=force,
            limit=limit,
            kp_id=kp_id,
            veoveo_id=veoveo_id,
        )
        downloads = run_veoveo_episode_preview_download(
            force=force,
            limit=limit,
            kp_id=kp_id,
            veoveo_id=veoveo_id,
        )
    except Exception as exc:
        _finish_preview_pipeline_error(run_token, exc)
        logger.exception("[veoveo-previews] pipeline failed")
        raise

    _finish_preview_pipeline(run_token, metadata, downloads)
    status = "partial" if metadata["errors"] or downloads["errors"] else "success"
    result = {
        "status": status,
        "metadata": metadata,
        "downloads": downloads,
    }
    logger.info("[veoveo-previews] pipeline finished: %s", result)
    return result


@shared_task(queue="default")
def sync_veoveo_previews():
    """Update changed episode metadata, then mirror missing previews to S3."""

    return run_veoveo_preview_pipeline()
