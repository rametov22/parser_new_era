import logging
import uuid
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from ..models import VeoVeoContent, VeoVeoSyncState
from ..veoveo_catalog import (
    VeoVeoCatalogClient,
    normalize_veoveo_content,
)


logger = logging.getLogger(__name__)

SYNC_STATE_KEY = "catalog_updates"
MAIN_DB_ALIAS = "main_db"
UPDATE_FIELDS = [
    field.name
    for field in VeoVeoContent._meta.concrete_fields
    if not field.primary_key
]


def _claim_sync():
    VeoVeoSyncState.objects.get_or_create(key=SYNC_STATE_KEY)

    now = timezone.now()
    stale_before = now - timedelta(
        seconds=settings.VEOVEO_SYNC_LOCK_TIMEOUT_SECONDS
    )
    run_token = uuid.uuid4()
    claimed = (
        VeoVeoSyncState.objects.filter(key=SYNC_STATE_KEY)
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
    if not claimed:
        return None, None
    return run_token, VeoVeoSyncState.objects.get(key=SYNC_STATE_KEY)


def _bootstrap_cursor(window_end):
    latest_full_sync = VeoVeoContent.objects.aggregate(
        latest=Max("synced_at")
    )["latest"]
    if latest_full_sync is not None:
        return min(latest_full_sync, window_end)
    return window_end - timedelta(
        hours=settings.VEOVEO_INITIAL_LOOKBACK_HOURS
    )


def _upsert_rows(rows, *, batch_size):
    if not rows:
        return 0, 0

    ids = [row["veoveo_id"] for row in rows]
    existing_ids = set(
        VeoVeoContent.objects.filter(veoveo_id__in=ids).values_list(
            "veoveo_id",
            flat=True,
        )
    )
    objects = [VeoVeoContent(**row) for row in rows]
    with transaction.atomic(using=MAIN_DB_ALIAS):
        VeoVeoContent.objects.bulk_create(
            objects,
            batch_size=batch_size,
            update_conflicts=True,
            unique_fields=["veoveo_id"],
            update_fields=UPDATE_FIELDS,
        )
    created = len(ids) - len(existing_ids)
    return created, len(existing_ids)


def _finish_success(
    run_token,
    *,
    window_start,
    window_end,
    pages,
    received,
    created,
    updated,
):
    VeoVeoSyncState.objects.filter(
        key=SYNC_STATE_KEY,
        run_token=run_token,
    ).update(
        cursor_at=window_end,
        run_token=None,
        running_since=None,
        status=VeoVeoSyncState.STATUS_SUCCESS,
        last_finished_at=timezone.now(),
        last_from_updated_at=window_start,
        last_to_updated_at=window_end,
        last_pages=pages,
        last_received=received,
        last_created=created,
        last_updated=updated,
        last_error="",
    )


def _finish_error(run_token, exc):
    VeoVeoSyncState.objects.filter(
        key=SYNC_STATE_KEY,
        run_token=run_token,
    ).update(
        run_token=None,
        running_since=None,
        status=VeoVeoSyncState.STATUS_ERROR,
        last_finished_at=timezone.now(),
        last_error=f"{type(exc).__name__}: {exc}"[:2000],
    )


def run_veoveo_incremental_sync():
    token = settings.VEOVEO_API_TOKEN.strip()
    if not token:
        raise RuntimeError(
            "VEOVEO_API_TOKEN is empty. Add the website token to .env."
        )

    page_size = settings.VEOVEO_INCREMENTAL_PAGE_SIZE
    if not 1 <= page_size <= 100:
        raise RuntimeError(
            "VEOVEO_INCREMENTAL_PAGE_SIZE must be between 1 and 100."
        )
    if settings.VEOVEO_SYNC_OVERLAP_SECONDS < 0:
        raise RuntimeError("VEOVEO_SYNC_OVERLAP_SECONDS cannot be negative.")
    if settings.VEOVEO_INITIAL_LOOKBACK_HOURS <= 0:
        raise RuntimeError("VEOVEO_INITIAL_LOOKBACK_HOURS must be positive.")
    if settings.VEOVEO_SYNC_LOCK_TIMEOUT_SECONDS <= 0:
        raise RuntimeError(
            "VEOVEO_SYNC_LOCK_TIMEOUT_SECONDS must be positive."
        )

    run_token, state = _claim_sync()
    if run_token is None:
        logger.info("[veoveo] sync is already running, skipping this tick")
        return {"status": "skipped", "reason": "already_running"}

    page_number = 1
    pages = 0
    received = 0
    created = 0
    updated = 0

    try:
        window_end = timezone.now()
        base_cursor = state.cursor_at or _bootstrap_cursor(window_end)
        window_start = base_cursor - timedelta(
            seconds=settings.VEOVEO_SYNC_OVERLAP_SECONDS
        )
        VeoVeoSyncState.objects.filter(
            key=SYNC_STATE_KEY,
            run_token=run_token,
        ).update(
            last_from_updated_at=window_start,
            last_to_updated_at=window_end,
        )
        client = VeoVeoCatalogClient(
            base_url=settings.VEOVEO_CATALOG_API_URL,
            token=token,
            timeout=settings.VEOVEO_REQUEST_TIMEOUT_SECONDS,
        )
        logger.info(
            "[veoveo] incremental sync started: from=%s to=%s page_size=%s",
            window_start.isoformat(),
            window_end.isoformat(),
            page_size,
        )

        while True:
            page = client.get_details_page(
                page=page_number,
                page_size=page_size,
                from_updated_at=window_start,
                to_updated_at=window_end,
            )
            rows_by_id = {}
            for item in page.items:
                row = normalize_veoveo_content(item, seen_at=window_end)
                rows_by_id[row["veoveo_id"]] = row
            rows = list(rows_by_id.values())

            page_created, page_updated = _upsert_rows(
                rows,
                batch_size=page_size,
            )
            pages += 1
            received += len(rows)
            created += page_created
            updated += page_updated
            logger.info(
                "[veoveo] page=%s/%s rows=%s created=%s updated=%s total=%s",
                page.page,
                page.pages or "?",
                len(rows),
                page_created,
                page_updated,
                page.total,
            )

            if not page.has_next_page:
                break
            page_number += 1
    except Exception as exc:
        _finish_error(run_token, exc)
        logger.exception(
            "[veoveo] incremental sync failed on page %s",
            page_number,
        )
        raise

    _finish_success(
        run_token,
        window_start=window_start,
        window_end=window_end,
        pages=pages,
        received=received,
        created=created,
        updated=updated,
    )
    logger.info(
        "[veoveo] incremental sync finished: pages=%s rows=%s "
        "created=%s updated=%s cursor=%s",
        pages,
        received,
        created,
        updated,
        window_end.isoformat(),
    )
    return {
        "status": "success",
        "pages": pages,
        "received": received,
        "created": created,
        "updated": updated,
        "from_updated_at": window_start.isoformat(),
        "to_updated_at": window_end.isoformat(),
    }


@shared_task(queue="default")
def sync_veoveo_updates():
    """Upsert only VeoVeo rows changed inside the persisted time window."""

    return run_veoveo_incremental_sync()
