import csv
from datetime import datetime

import requests
from decouple import config
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.scrapers.models import Content, ScraperLog, YtConnectContent
from apps.scrapers.tasks.yangitv import _decrypt_movie_urls


YT_API_BASE = "https://admin.yangi.tv/api/v1"


class Command(BaseCommand):
    help = (
        "Inspect linked Yangi rows without player and player failed rows. "
        "Read-only; optionally checks getMovieUrl live."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            choices=("both", "linked-missing-player", "failed-player"),
            default="both",
            help="Which issue group to inspect.",
        )
        parser.add_argument(
            "--ids",
            nargs="*",
            type=int,
            default=None,
            help="Only these Yangi content_id/id_uz values.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Limit output rows. 0 = no limit.",
        )
        parser.add_argument(
            "--fetch-api",
            action="store_true",
            help="Call getMovieUrl now and try decrypting response without writing DB.",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=60,
            help="HTTP timeout in seconds for --fetch-api.",
        )
        parser.add_argument(
            "--token",
            default="",
            help="Override YT_BEARER_TOKEN.",
        )
        parser.add_argument(
            "--output",
            default="",
            help="CSV path. Defaults to /tmp/yangi_player_issues_<timestamp>.csv",
        )

    def handle(self, *args, **options):
        token = options["token"] or config("YT_BEARER_TOKEN", default="")
        if options["fetch_api"] and not token:
            raise CommandError("YT_BEARER_TOKEN is empty. Pass --token or set env.")

        rows = self._build_rows(options)
        if options["limit"]:
            rows = rows[: options["limit"]]

        headers = {
            "User-Agent": "okhttp/5.1.0",
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        session = requests.Session()

        for row in rows:
            self._attach_logs(row)
            if options["fetch_api"]:
                self._attach_api_probe(session, headers, row, options["timeout"])

        output_path = options["output"] or (
            f"/tmp/yangi_player_issues_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        self._write_csv(output_path, rows)

        self.stdout.write(
            self.style.SUCCESS(
                f"rows={len(rows)} fetch_api={options['fetch_api']} output={output_path}"
            )
        )
        for row in rows[: min(len(rows), 20)]:
            self.stdout.write(self._format_row(row))

    def _build_rows(self, options):
        rows_by_id = {}
        mode = options["mode"]

        if mode in ("both", "linked-missing-player"):
            for row in self._linked_missing_player_rows(options):
                rows_by_id[row["yt_content_id"]] = row

        if mode in ("both", "failed-player"):
            for row in self._failed_player_rows(options):
                existing = rows_by_id.get(row["yt_content_id"])
                if existing:
                    existing["issue"] = f"{existing['issue']}+failed_player"
                    existing.update(
                        {
                            key: value
                            for key, value in row.items()
                            if value not in ("", None, False)
                        }
                    )
                else:
                    rows_by_id[row["yt_content_id"]] = row

        return sorted(rows_by_id.values(), key=lambda item: item["yt_content_id"])

    def _linked_missing_player_rows(self, options):
        content_qs = (
            Content.objects.exclude(id_uz__isnull=True)
            .filter(Q(film_content_uz__isnull=True) | Q(film_content_uz={}))
            .order_by("id_uz")
        )
        if options["ids"]:
            content_qs = content_qs.filter(id_uz__in=options["ids"])

        content_rows = list(
            content_qs.values(
                "id",
                "kino_poisk_id",
                "id_uz",
                "name_ru",
                "is_serial",
                "film_content_uz",
            )
        )
        yt_ids = [row["id_uz"] for row in content_rows]
        yt_rows = self._yt_rows_map(yt_ids)

        return [
            self._row_from_content(
                content_row,
                yt_rows.get(content_row["id_uz"]),
                "linked_missing_player",
            )
            for content_row in content_rows
        ]

    def _failed_player_rows(self, options):
        yt_qs = YtConnectContent.objects.filter(parsing_status_player="failed").order_by(
            "content_id"
        )
        if options["ids"]:
            yt_qs = yt_qs.filter(content_id__in=options["ids"])

        yt_rows = list(yt_qs.values(*self._yt_value_fields()))
        content_rows = self._content_rows_map([row["content_id"] for row in yt_rows])

        return [
            self._row_from_content(
                content_rows.get(yt_row["content_id"]),
                yt_row,
                "failed_player",
            )
            for yt_row in yt_rows
        ]

    def _row_from_content(self, content_row, yt_row, issue):
        yt_id = (
            content_row["id_uz"]
            if content_row and content_row.get("id_uz") is not None
            else yt_row["content_id"]
        )
        film_content_uz = content_row.get("film_content_uz") if content_row else None
        content_url = yt_row.get("content_url") if yt_row else None
        return {
            "issue": issue,
            "yt_content_id": yt_id,
            "content_pk": content_row.get("id", "") if content_row else "",
            "kino_poisk_id": content_row.get("kino_poisk_id", "") if content_row else "",
            "content_name_ru": content_row.get("name_ru", "") if content_row else "",
            "content_is_serial": content_row.get("is_serial", "") if content_row else "",
            "content_film_content_uz_empty": self._json_empty(film_content_uz),
            "yt_exists": bool(yt_row),
            "yt_name": yt_row.get("yt_name", "") if yt_row else "",
            "yt_name_original": yt_row.get("yt_name_original", "") if yt_row else "",
            "yt_year": yt_row.get("yt_year", "") if yt_row else "",
            "yt_is_serial": yt_row.get("is_serial", "") if yt_row else "",
            "parsing_status": yt_row.get("parsing_status", "") if yt_row else "",
            "parsing_status_player": (
                yt_row.get("parsing_status_player", "") if yt_row else ""
            ),
            "player_fail_count": yt_row.get("player_fail_count", "") if yt_row else "",
            "yt_content_url_empty": self._json_empty(content_url),
            "last_player_error_at": "",
            "last_player_error": "",
            "last_player_success_at": "",
            "api_code": "",
            "api_message": "",
            "api_data_type": "",
            "decrypted_kind": "",
            "decrypted_summary": "",
        }

    def _attach_logs(self, row):
        error_log = (
            ScraperLog.objects.filter(
                task_name=f"YT player {row['yt_content_id']}",
                status="error",
            )
            .order_by("-created_at")
            .values("created_at", "message")
            .first()
        )
        success_log = (
            ScraperLog.objects.filter(
                task_name=f"YT movie url {row['yt_content_id']}",
                status="success",
            )
            .order_by("-created_at")
            .values("created_at")
            .first()
        )
        if error_log:
            row["last_player_error_at"] = error_log["created_at"]
            row["last_player_error"] = error_log["message"]
        if success_log:
            row["last_player_success_at"] = success_log["created_at"]

    def _attach_api_probe(self, session, headers, row, timeout):
        try:
            response = session.get(
                f"{YT_API_BASE}/getMovieUrl",
                params={"content_id": row["yt_content_id"]},
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data")
            urls = _decrypt_movie_urls(data)
            row.update(
                {
                    "api_code": payload.get("code", ""),
                    "api_message": payload.get("message", ""),
                    "api_data_type": type(data).__name__,
                    "decrypted_kind": self._decrypted_kind(urls),
                    "decrypted_summary": self._decrypted_summary(urls),
                }
            )
        except Exception as exc:  # noqa: BLE001
            row.update(
                {
                    "api_code": "error",
                    "api_message": f"{type(exc).__name__}: {exc}",
                }
            )

    def _write_csv(self, output_path, rows):
        fieldnames = [
            "issue",
            "yt_content_id",
            "content_pk",
            "kino_poisk_id",
            "content_name_ru",
            "content_is_serial",
            "content_film_content_uz_empty",
            "yt_exists",
            "yt_name",
            "yt_name_original",
            "yt_year",
            "yt_is_serial",
            "parsing_status",
            "parsing_status_player",
            "player_fail_count",
            "yt_content_url_empty",
            "last_player_error_at",
            "last_player_error",
            "last_player_success_at",
            "api_code",
            "api_message",
            "api_data_type",
            "decrypted_kind",
            "decrypted_summary",
        ]
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _format_row(self, row):
        parts = [
            f"{row['issue']} id_uz={row['yt_content_id']}",
            f"kp={row['kino_poisk_id'] or '-'}",
            f"name={row['content_name_ru'] or row['yt_name'] or '-'}",
            f"player={row['parsing_status_player'] or '-'}",
            f"fails={row['player_fail_count'] or 0}",
        ]
        if row["last_player_error"]:
            parts.append(f"last_error={row['last_player_error']}")
        if row["api_code"]:
            parts.append(
                f"api={row['api_code']} decrypted={row['decrypted_summary'] or '-'}"
            )
        return " | ".join(parts)

    def _yt_rows_map(self, yt_ids):
        if not yt_ids:
            return {}
        return {
            row["content_id"]: row
            for row in YtConnectContent.objects.filter(content_id__in=yt_ids).values(
                *self._yt_value_fields()
            )
        }

    def _content_rows_map(self, yt_ids):
        if not yt_ids:
            return {}
        return {
            row["id_uz"]: row
            for row in Content.objects.filter(id_uz__in=yt_ids).values(
                "id",
                "kino_poisk_id",
                "id_uz",
                "name_ru",
                "is_serial",
                "film_content_uz",
            )
        }

    def _yt_value_fields(self):
        return (
            "content_id",
            "content_url",
            "is_serial",
            "parsing_status",
            "parsing_status_player",
            "player_fail_count",
            "yt_name",
            "yt_name_original",
            "yt_year",
        )

    def _json_empty(self, value):
        return value in (None, {}, [], "")

    def _decrypted_kind(self, urls):
        if not urls:
            return "empty"
        first_value = next(iter(urls.values()))
        return "serial" if isinstance(first_value, dict) else "movie"

    def _decrypted_summary(self, urls):
        if not urls:
            return ""
        if self._decrypted_kind(urls) == "serial":
            episode_count = sum(len(episodes) for episodes in urls.values())
            return f"seasons={len(urls)} episodes={episode_count}"
        return f"qualities={','.join(urls.keys())}"
