import csv
from collections import defaultdict
from datetime import datetime

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.scrapers.models import Content, YtConnectContent
from apps.scrapers.tasks.yangitv import _normalize_name, _normalize_name_soft


class ContentMatchIndex:
    """Small in-memory index for the same matching order as yangitv._match_content."""

    def __init__(self, yt_rows):
        self.year_counts = defaultdict(int)
        self.strict_ru_year = defaultdict(list)
        self.strict_original_year = defaultdict(list)
        self.strict_ru_any = defaultdict(list)
        self.strict_original_any = defaultdict(list)
        self.soft_ru_year = defaultdict(list)
        self.soft_original_year = defaultdict(list)

        self.target_years = set()
        self.target_strict_names = set()
        self.target_soft_names = set()
        for row in yt_rows:
            yt_name = row["yt_name"] or ""
            yt_original = row["yt_name_original"] or ""
            if row["yt_year"]:
                self.target_years.add(row["yt_year"])
            for name in (yt_name, yt_original):
                strict = _normalize_name(name)
                soft = _normalize_name_soft(name)
                if strict:
                    self.target_strict_names.add(strict)
                if soft:
                    self.target_soft_names.add(soft)

        self.content_rows = 0
        self.indexed_rows = 0
        if not self.target_strict_names and not self.target_soft_names:
            return
        self._build()

    def _build(self):
        qs = Content.objects.values(
            "id",
            "kino_poisk_id",
            "name_ru",
            "name_original",
            "year_production",
            "id_uz",
        ).iterator(chunk_size=2000)

        for row in qs:
            self.content_rows += 1
            year = row["year_production"]
            if year in self.target_years:
                self.year_counts[year] += 1

            name_ru = row["name_ru"] or ""
            name_original = row["name_original"] or ""
            ru_strict = _normalize_name(name_ru)
            original_strict = _normalize_name(name_original)
            was_indexed = False

            if ru_strict in self.target_strict_names:
                self.strict_ru_any[ru_strict].append(row)
                was_indexed = True
                if year in self.target_years:
                    self.strict_ru_year[(year, ru_strict)].append(row)
            if original_strict in self.target_strict_names:
                self.strict_original_any[original_strict].append(row)
                was_indexed = True
                if year in self.target_years:
                    self.strict_original_year[(year, original_strict)].append(row)

            if year in self.target_years:
                ru_soft = _normalize_name_soft(name_ru)
                original_soft = _normalize_name_soft(name_original)
                if ru_soft in self.target_soft_names:
                    self.soft_ru_year[(year, ru_soft)].append(row)
                    was_indexed = True
                if original_soft in self.target_soft_names:
                    self.soft_original_year[(year, original_soft)].append(row)
                    was_indexed = True

            if was_indexed:
                self.indexed_rows += 1

    def match(self, yt_name, yt_year, yt_name_original):
        if not yt_name and not yt_name_original:
            return None, "no_yt_name"

        yt_strict = _normalize_name(yt_name)
        yt_soft = _normalize_name_soft(yt_name)
        yt_original_strict = _normalize_name(yt_name_original)
        yt_original_soft = _normalize_name_soft(yt_name_original)

        if yt_year and yt_strict:
            content, strategy = self._unique(
                self.strict_ru_year[(yt_year, yt_strict)],
                "exact_norm_year",
                "ambiguous_exact",
            )
            if content or strategy.startswith("ambiguous_"):
                return content, strategy

        if yt_year and yt_original_strict:
            content, strategy = self._unique(
                self.strict_original_year[(yt_year, yt_original_strict)],
                "exact_original_norm_year",
                "ambiguous_original_exact",
            )
            if content or strategy.startswith("ambiguous_"):
                return content, strategy

        if yt_year and (yt_strict or yt_original_strict):
            if yt_strict:
                content, strategy = self._unique(
                    self.strict_ru_any[yt_strict],
                    "exact_norm_unique_any_year",
                    "ambiguous_exact_any_year",
                )
                if content or strategy.startswith("ambiguous_"):
                    return content, strategy

            if yt_original_strict:
                content, strategy = self._unique(
                    self.strict_original_any[yt_original_strict],
                    "exact_original_norm_unique_any_year",
                    "ambiguous_original_exact_any_year",
                )
                if content or strategy.startswith("ambiguous_"):
                    return content, strategy

        if not yt_year:
            if yt_strict:
                content, strategy = self._unique(
                    self.strict_ru_any[yt_strict],
                    "exact_norm_noyear",
                    "ambiguous_exact_noyear",
                )
                if content or strategy.startswith("ambiguous_"):
                    return content, strategy

            if yt_original_strict:
                content, strategy = self._unique(
                    self.strict_original_any[yt_original_strict],
                    "exact_original_norm_noyear",
                    "ambiguous_original_exact_noyear",
                )
                if content or strategy.startswith("ambiguous_"):
                    return content, strategy

        if yt_year and yt_soft:
            content, strategy = self._unique(
                self.soft_ru_year[(yt_year, yt_soft)],
                "soft_norm_year",
                "ambiguous_soft",
            )
            if content or strategy.startswith("ambiguous_"):
                return content, strategy

        if yt_year and yt_original_soft:
            content, strategy = self._unique(
                self.soft_original_year[(yt_year, yt_original_soft)],
                "soft_original_norm_year",
                "ambiguous_original_soft",
            )
            if content or strategy.startswith("ambiguous_"):
                return content, strategy

        if yt_year and self.year_counts[yt_year] == 0:
            return None, "no_kp_in_year"
        return None, "no_match"

    def _unique(self, rows, ok_strategy, ambiguous_prefix):
        if len(rows) == 1:
            return rows[0], ok_strategy
        if len(rows) > 1:
            return None, f"{ambiguous_prefix}_{len(rows)}"
        return None, ""


class Command(BaseCommand):
    help = (
        "Find unlinked YtConnectContent rows that now match Content by cached "
        "yt_name / yt_name_original / yt_year and optionally return them to "
        "the connect queue."
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
            help="Inspect at most N YtConnectContent rows. 0 = no limit.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write changes: set matched YtConnectContent.parsing_status to not_parsed.",
        )
        parser.add_argument(
            "--include-linked",
            action="store_true",
            help="Inspect already linked Yangi rows too. Mostly useful for debugging.",
        )
        parser.add_argument(
            "--output",
            default="",
            help="CSV path. Defaults to /tmp/yangi_relink_by_metadata_<timestamp>.csv",
        )

    def handle(self, *args, **options):
        rows = self._target_rows(options)
        output_path = options["output"] or (
            "/tmp/yangi_relink_by_metadata_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )

        stats = {
            "inspected": 0,
            "matched": 0,
            "applied": 0,
            "conflicts": 0,
            "already_linked": 0,
            "ambiguous": 0,
            "no_match": 0,
        }
        journal_rows = []

        if not rows:
            self._write_csv(output_path, journal_rows)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{stats} apply={options['apply']} output={output_path}"
                )
            )
            return

        index = ContentMatchIndex(rows)

        for row in rows:
            stats["inspected"] += 1
            journal = self._blank_journal(row)

            content, strategy = index.match(
                row["yt_name"] or "",
                row["yt_year"],
                row["yt_name_original"] or "",
            )
            journal["match_strategy"] = strategy

            if not content:
                if strategy.startswith("ambiguous_"):
                    stats["ambiguous"] += 1
                    journal["status"] = "ambiguous"
                else:
                    stats["no_match"] += 1
                    journal["status"] = "no_match"
                journal_rows.append(journal)
                continue

            journal.update(
                {
                    "content_pk": content["id"],
                    "kino_poisk_id": content["kino_poisk_id"],
                    "content_name_ru": content["name_ru"],
                    "content_name_original": content["name_original"],
                    "content_year": content["year_production"] or "",
                    "content_existing_id_uz": content["id_uz"] or "",
                }
            )

            if content["id_uz"] == row["content_id"]:
                stats["already_linked"] += 1
                journal["status"] = "already_linked"
                journal_rows.append(journal)
                continue

            if content["id_uz"] and content["id_uz"] != row["content_id"]:
                stats["conflicts"] += 1
                journal["status"] = "conflict_content_already_linked"
                journal_rows.append(journal)
                continue

            stats["matched"] += 1
            if options["apply"]:
                updated = YtConnectContent.objects.filter(
                    content_id=row["content_id"]
                ).update(
                    parsing_status="not_parsed",
                    connect_fail_count=0,
                    updated_at=timezone.now(),
                )
                stats["applied"] += int(bool(updated))
                journal["status"] = "queued_for_connect" if updated else "not_updated"
            else:
                journal["status"] = "dry_run_match"

            journal_rows.append(journal)

        self._write_csv(output_path, journal_rows)
        self.stdout.write(
            self.style.SUCCESS(
                f"{stats} apply={options['apply']} output={output_path}"
            )
        )
        self.stdout.write(
            f"content_index scanned={index.content_rows} indexed={index.indexed_rows}"
        )
        for row in journal_rows[: min(len(journal_rows), 30)]:
            self.stdout.write(self._format_row(row))

    def _target_rows(self, options):
        blank_yt_name = Q(yt_name__isnull=True) | Q(yt_name="")
        blank_original_name = Q(yt_name_original__isnull=True) | Q(
            yt_name_original=""
        )
        qs = YtConnectContent.objects.exclude(blank_yt_name & blank_original_name)

        if options["ids"]:
            qs = qs.filter(content_id__in=options["ids"])

        if not options["include_linked"]:
            linked_ids = Content.objects.exclude(id_uz__isnull=True).values_list(
                "id_uz", flat=True
            )
            qs = qs.exclude(content_id__in=linked_ids)

        qs = qs.order_by("content_id")
        if options["limit"]:
            qs = qs[: options["limit"]]

        return list(
            qs.values(
                "content_id",
                "parsing_status",
                "parsing_status_player",
                "yt_name",
                "yt_name_original",
                "yt_year",
            )
        )

    def _blank_journal(self, row):
        return {
            "status": "pending",
            "yt_content_id": row["content_id"],
            "yt_name": row["yt_name"] or "",
            "yt_name_original": row["yt_name_original"] or "",
            "yt_year": row["yt_year"] or "",
            "old_parsing_status": row["parsing_status"],
            "old_parsing_status_player": row["parsing_status_player"],
            "match_strategy": "",
            "content_pk": "",
            "kino_poisk_id": "",
            "content_name_ru": "",
            "content_name_original": "",
            "content_year": "",
            "content_existing_id_uz": "",
        }

    def _write_csv(self, output_path, rows):
        fieldnames = [
            "status",
            "yt_content_id",
            "yt_name",
            "yt_name_original",
            "yt_year",
            "old_parsing_status",
            "old_parsing_status_player",
            "match_strategy",
            "content_pk",
            "kino_poisk_id",
            "content_name_ru",
            "content_name_original",
            "content_year",
            "content_existing_id_uz",
        ]
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _format_row(self, row):
        return (
            f"{row['status']} id_uz={row['yt_content_id']} "
            f"yt={row['yt_name'] or row['yt_name_original']!r}/{row['yt_year'] or '-'} "
            f"strategy={row['match_strategy']} kp={row['kino_poisk_id'] or '-'} "
            f"content={row['content_name_ru'] or '-'}"
        )
