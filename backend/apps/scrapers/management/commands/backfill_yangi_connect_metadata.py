import csv
import time
from datetime import datetime

import requests
from decouple import config
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.scrapers.models import Content, YtConnectContent


YT_API_BASE = "https://admin.yangi.tv/api/v1"


class Command(BaseCommand):
    help = (
        "Fetch yangi.tv name/name_ru/orig_name/year into YtConnectContent. "
        "By default targets only unlinked rows with missing metadata."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--ids",
            nargs="*",
            type=int,
            default=None,
            help="Only these Yangi content_id values.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Process at most N rows after filtering. 0 = no limit.",
        )
        parser.add_argument(
            "--include-linked",
            action="store_true",
            help="Also process rows already linked through Content.id_uz.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Fetch even if yt_name/yt_name_uz/yt_name_original/yt_year are already filled.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and log, but do not write database changes.",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.1,
            help="Seconds to sleep between API requests.",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=60,
            help="HTTP timeout in seconds.",
        )
        parser.add_argument(
            "--token",
            default="",
            help="Override YT_BEARER_TOKEN.",
        )
        parser.add_argument(
            "--output",
            default="",
            help="CSV log path. Defaults to /tmp/yangi_connect_metadata_backfill_<timestamp>.csv",
        )

    def handle(self, *args, **options):
        token = options["token"] or config("YT_BEARER_TOKEN", default="")
        if not token:
            raise CommandError("YT_BEARER_TOKEN is empty. Pass --token or set env.")

        output_path = options["output"] or (
            "/tmp/yangi_connect_metadata_backfill_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        dry_run = bool(options["dry_run"])
        target_rows = self._target_rows(options)

        self.stdout.write(
            f"target={len(target_rows)} dry_run={dry_run} output={output_path}"
        )

        headers = {
            "User-Agent": "okhttp/5.1.0",
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        session = requests.Session()

        fieldnames = [
            "status",
            "reason",
            "yt_content_id",
            "old_yt_name",
            "old_yt_name_uz",
            "old_yt_name_original",
            "old_yt_year",
            "new_yt_name",
            "new_yt_name_uz",
            "new_yt_name_original",
            "new_yt_year",
            "wrote_yt_metadata",
        ]
        stats = {
            "processed": 0,
            "updated_yt": 0,
            "errors": 0,
        }

        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()

            for row in target_rows:
                stats["processed"] += 1
                yt_id = row["content_id"]
                journal = self._blank_journal(row)

                try:
                    data = self._fetch_detail(
                        session,
                        headers,
                        yt_id,
                        options["timeout"],
                    )
                    name_ru = data.get("name_ru") or ""
                    name_uz = data.get("name") or ""
                    name_original = data.get("orig_name") or ""
                    year = data.get("year")

                    journal.update(
                        {
                            "new_yt_name": name_ru,
                            "new_yt_name_uz": name_uz,
                            "new_yt_name_original": name_original,
                            "new_yt_year": year or "",
                        }
                    )

                    if not dry_run:
                        updated = YtConnectContent.objects.filter(
                            content_id=yt_id
                        ).update(
                            yt_name=name_ru,
                            yt_name_uz=name_uz,
                            yt_name_original=name_original,
                            yt_year=year,
                            updated_at=timezone.now(),
                        )
                    else:
                        updated = 1

                    stats["updated_yt"] += int(bool(updated))
                    journal.update(
                        {
                            "status": "dry_run" if dry_run else "updated",
                            "reason": "yt_metadata",
                            "wrote_yt_metadata": bool(updated),
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    stats["errors"] += 1
                    journal.update(
                        {
                            "status": "error",
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                    )

                writer.writerow(journal)

                if options["sleep"]:
                    time.sleep(options["sleep"])

        self.stdout.write(self.style.SUCCESS(str(stats)))
        self.stdout.write(f"log={output_path}")

    def _target_rows(self, options):
        qs = YtConnectContent.objects.only(
            "content_id",
            "yt_name",
            "yt_name_uz",
            "yt_name_original",
            "yt_year",
        )

        if options["ids"]:
            qs = qs.filter(content_id__in=options["ids"])

        if not options["include_linked"]:
            linked_ids = set(
                Content.objects.exclude(id_uz__isnull=True).values_list(
                    "id_uz", flat=True
                )
            )
            qs = qs.exclude(content_id__in=linked_ids)

        if not options["force"]:
            qs = qs.filter(
                Q(yt_name__isnull=True)
                | Q(yt_name="")
                | Q(yt_name_uz__isnull=True)
                | Q(yt_name_uz="")
                | Q(yt_name_original__isnull=True)
                | Q(yt_name_original="")
                | Q(yt_year__isnull=True)
            )

        qs = qs.order_by("content_id")
        if options["limit"]:
            qs = qs[: options["limit"]]

        return list(
            qs.values(
                "content_id",
                "yt_name",
                "yt_name_uz",
                "yt_name_original",
                "yt_year",
            )
        )

    def _fetch_detail(self, session, headers, yt_id, timeout):
        response = session.get(
            f"{YT_API_BASE}/getContentDetail",
            params={"content_id": yt_id},
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json().get("data") or {}

    def _blank_journal(self, row):
        return {
            "status": "pending",
            "reason": "",
            "yt_content_id": row["content_id"],
            "old_yt_name": row["yt_name"] or "",
            "old_yt_name_uz": row["yt_name_uz"] or "",
            "old_yt_name_original": row["yt_name_original"] or "",
            "old_yt_year": row["yt_year"] or "",
            "new_yt_name": "",
            "new_yt_name_uz": "",
            "new_yt_name_original": "",
            "new_yt_year": "",
            "wrote_yt_metadata": False,
        }
