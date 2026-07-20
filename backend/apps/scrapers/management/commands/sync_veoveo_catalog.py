import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.scrapers.models import VeoVeoContent
from apps.scrapers.veoveo_catalog import (
    VeoVeoCatalogClient,
    VeoVeoCatalogError,
    normalize_veoveo_content,
)


UPDATE_FIELDS = [
    field.name
    for field in VeoVeoContent._meta.concrete_fields
    if not field.primary_key
]


class Command(BaseCommand):
    help = (
        "Синхронизировать локальную таблицу доступного контента с "
        "VeoVeo Catalog Sync API."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--page-size",
            type=int,
            default=100,
            help="Количество элементов в одном запросе (по умолчанию: 100).",
        )
        parser.add_argument(
            "--start-page",
            type=int,
            default=1,
            help="Начальная страница (по умолчанию: 1).",
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=0,
            help="Остановиться после N страниц; 0 означает весь каталог.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Получить и проверить данные, но не изменять базу.",
        )

    def handle(self, *args, **options):
        token = settings.VEOVEO_API_TOKEN.strip()
        if not token:
            raise CommandError(
                "VEOVEO_API_TOKEN is empty. Add the website token to .env."
            )

        page_size = options["page_size"]
        start_page = options["start_page"]
        max_pages = options["max_pages"]
        dry_run = options["dry_run"]
        if not 1 <= page_size <= 1000:
            raise CommandError("--page-size must be between 1 and 1000.")
        if start_page < 1:
            raise CommandError("--start-page must be at least 1.")
        if max_pages < 0:
            raise CommandError("--max-pages cannot be negative.")

        client = VeoVeoCatalogClient(
            base_url=settings.VEOVEO_CATALOG_API_URL,
            token=token,
            timeout=settings.VEOVEO_REQUEST_TIMEOUT_SECONDS,
        )
        database = "main_db"
        sync_marker = timezone.now()
        started = time.monotonic()
        page_number = start_page
        processed_pages = 0
        received = 0
        with_kinopoisk_id = 0
        naturally_completed = False

        self.stdout.write(
            self.style.WARNING(
                "VeoVeo sync started: "
                f"page={start_page}, page_size={page_size}, "
                f"max_pages={max_pages or 'all'}, dry_run={dry_run}"
            )
        )

        try:
            while True:
                page = client.get_details_page(
                    page=page_number,
                    page_size=page_size,
                )
                rows_by_id = {}
                for item in page.items:
                    row = normalize_veoveo_content(item, seen_at=sync_marker)
                    rows_by_id[row["veoveo_id"]] = row
                rows = list(rows_by_id.values())

                if not dry_run and rows:
                    objects = [VeoVeoContent(**row) for row in rows]
                    with transaction.atomic(using=database):
                        VeoVeoContent.objects.using(database).bulk_create(
                            objects,
                            batch_size=page_size,
                            update_conflicts=True,
                            unique_fields=["veoveo_id"],
                            update_fields=UPDATE_FIELDS,
                        )

                processed_pages += 1
                received += len(rows)
                with_kinopoisk_id += sum(
                    row["kinopoisk_id"] is not None for row in rows
                )
                self.stdout.write(
                    f"page={page.page}/{page.pages or '?'} "
                    f"rows={len(rows)} total={page.total} "
                    f"saved={0 if dry_run else len(rows)}"
                )

                if not page.has_next_page:
                    naturally_completed = True
                    break
                if max_pages and processed_pages >= max_pages:
                    break
                page_number += 1
        except VeoVeoCatalogError as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:
            raise CommandError(
                f"VeoVeo sync stopped on page {page_number}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        full_sync = start_page == 1 and naturally_completed
        deactivated = 0
        if not dry_run and full_sync:
            if received:
                deactivated = (
                    VeoVeoContent.objects.using(database)
                    .filter(
                        is_available=True,
                        last_seen_at__lt=sync_marker,
                    )
                    .update(is_available=False, synced_at=timezone.now())
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        "The API returned an empty catalog; missing rows were "
                        "not deactivated as a safety measure."
                    )
                )

        elapsed = time.monotonic() - started
        mode = "full" if full_sync else "partial"
        self.stdout.write(
            self.style.SUCCESS(
                "VeoVeo sync finished: "
                f"mode={mode}, pages={processed_pages}, rows={received}, "
                f"with_kp_id={with_kinopoisk_id}, deactivated={deactivated}, "
                f"dry_run={dry_run}, time={elapsed:.1f}s"
            )
        )
