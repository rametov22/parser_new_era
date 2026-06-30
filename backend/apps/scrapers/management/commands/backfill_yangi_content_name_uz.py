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
        "Fill empty Content.name_uz for already linked Content.id_uz rows "
        "and cache Yangi metadata in YtConnectContent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--ids",
            nargs="*",
            type=int,
            default=None,
            help="Only these Yangi id_uz/content_id values.",
        )
        parser.add_argument(
            "--kp-ids",
            nargs="*",
            type=int,
            default=None,
            help="Only these kino_poisk_id values.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Process at most N rows after filtering. 0 = no limit.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite Content.name_uz even if it is already filled.",
        )
        parser.add_argument(
            "--fill-description-uz",
            action="store_true",
            help="Also fill empty Content.description_uz from yangi.tv description.",
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
            help="CSV log path. Defaults to /tmp/yangi_content_name_uz_backfill_<timestamp>.csv",
        )

    def handle(self, *args, **options):
        token = options["token"] or config("YT_BEARER_TOKEN", default="")
        if not token:
            raise CommandError("YT_BEARER_TOKEN is empty. Pass --token or set env.")

        output_path = options["output"] or (
            "/tmp/yangi_content_name_uz_backfill_"
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
            "content_id",
            "kino_poisk_id",
            "content_name_ru",
            "old_content_name_uz",
            "new_content_name_uz",
            "yt_name_ru",
            "yt_name_uz",
            "yt_name_original",
            "yt_year",
            "wrote_yt_metadata",
            "wrote_content_name_uz",
            "wrote_description_uz",
        ]
        stats = {
            "processed": 0,
            "updated_yt": 0,
            "updated_content_name_uz": 0,
            "updated_description_uz": 0,
            "errors": 0,
        }

        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()

            for row in target_rows:
                stats["processed"] += 1
                yt_id = row["id_uz"]
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
                    description = data.get("description") or ""
                    year = data.get("year")

                    journal.update(
                        {
                            "new_content_name_uz": name_uz,
                            "yt_name_ru": name_ru,
                            "yt_name_uz": name_uz,
                            "yt_name_original": name_original,
                            "yt_year": year or "",
                        }
                    )

                    if not dry_run:
                        _yt_obj, _created = YtConnectContent.objects.update_or_create(
                            content_id=yt_id,
                            defaults={
                                "yt_name": name_ru,
                                "yt_name_uz": name_uz,
                                "yt_name_original": name_original,
                                "yt_year": year,
                                "updated_at": timezone.now(),
                            },
                        )
                        yt_updated = 1
                    else:
                        yt_updated = 1

                    content_updates = {}
                    if name_uz and (options["force"] or not row["name_uz"]):
                        content_updates["name_uz"] = name_uz
                    if (
                        options["fill_description_uz"]
                        and description
                        and not row["description_uz"]
                    ):
                        content_updates["description_uz"] = description

                    if content_updates and not dry_run:
                        Content.objects.filter(pk=row["id"]).update(**content_updates)

                    stats["updated_yt"] += int(bool(yt_updated))
                    stats["updated_content_name_uz"] += int("name_uz" in content_updates)
                    stats["updated_description_uz"] += int(
                        "description_uz" in content_updates
                    )
                    journal.update(
                        {
                            "status": "dry_run" if dry_run else "updated",
                            "reason": ",".join(
                                ["yt_metadata", *sorted(content_updates.keys())]
                            ),
                            "wrote_yt_metadata": bool(yt_updated),
                            "wrote_content_name_uz": "name_uz" in content_updates,
                            "wrote_description_uz": "description_uz" in content_updates,
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
        qs = Content.objects.exclude(id_uz__isnull=True).only(
            "id",
            "kino_poisk_id",
            "id_uz",
            "name_ru",
            "name_uz",
            "description_uz",
        )

        if options["ids"]:
            qs = qs.filter(id_uz__in=options["ids"])
        if options["kp_ids"]:
            qs = qs.filter(kino_poisk_id__in=options["kp_ids"])
        if not options["force"]:
            qs = qs.filter(Q(name_uz__isnull=True) | Q(name_uz=""))

        qs = qs.order_by("id_uz")
        if options["limit"]:
            qs = qs[: options["limit"]]

        return list(
            qs.values(
                "id",
                "kino_poisk_id",
                "id_uz",
                "name_ru",
                "name_uz",
                "description_uz",
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
            "yt_content_id": row["id_uz"],
            "content_id": row["id"],
            "kino_poisk_id": row["kino_poisk_id"],
            "content_name_ru": row["name_ru"],
            "old_content_name_uz": row["name_uz"] or "",
            "new_content_name_uz": "",
            "yt_name_ru": "",
            "yt_name_uz": "",
            "yt_name_original": "",
            "yt_year": "",
            "wrote_yt_metadata": False,
            "wrote_content_name_uz": False,
            "wrote_description_uz": False,
        }
