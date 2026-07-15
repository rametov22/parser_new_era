import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.scrapers.models import Content
from apps.scrapers.tasks.vavada import parse_single_iframe


class Command(BaseCommand):
    help = (
        "Однократно перепарсить фильмы с film_content и недавней премьерой, "
        "чтобы заполнить озвучки и пересчитать is_pirated."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=90,
            help="Глубина премьеры в днях (по умолчанию: 90).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Максимум записей; 0 означает все найденные.",
        )
        parser.add_argument("--delay", type=float, default=5.0)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        days = max(1, options["days"])
        limit = max(0, options["limit"])
        delay = max(0.0, options["delay"])
        today = timezone.now().date()
        start_date = today - timedelta(days=days)
        premiere_filter = Q(premiere__range=(start_date, today)) | Q(
            premiere_ru__range=(start_date, today)
        )
        queryset = (
            Content.objects.filter(
                is_serial=False,
                film_content__isnull=False,
            )
            .exclude(film_content="")
            .filter(premiere_filter)
            .exclude(kino_poisk_id__isnull=True)
            .order_by("last_update", "id")
        )

        total = queryset.count()
        if options["dry_run"]:
            sample = list(
                queryset.values_list("kino_poisk_id", "name_ru")[:20]
            )
            self.stdout.write(
                {
                    "date_from": start_date.isoformat(),
                    "date_to": today.isoformat(),
                    "candidates": total,
                    "requested_limit": limit or "all",
                    "sample": sample,
                }.__repr__()
            )
            return

        if settings.VAVADA_PROXY_ENABLED:
            raise CommandError(
                "Для проверки одного IP передайте VAVADA_PROXY_ENABLED=false."
            )
        if not settings.VAVADA_WD_TRUST_COOKIE:
            raise CommandError("Не передан VAVADA_WD_TRUST_COOKIE.")
        if not settings.VAVADA_WD_APPROVAL_COOKIE:
            raise CommandError("Не передан VAVADA_WD_APPROVAL_COOKIE.")
        if total == 0:
            raise CommandError("Подходящие фильмы не найдены.")

        selected_queryset = queryset[:limit] if limit else queryset
        films = list(
            selected_queryset.only(
                "kino_poisk_id",
                "name_ru",
                "is_parsed_ru",
                "audio_tracks",
                "is_pirated",
            )
        )
        self.stdout.write(
            self.style.WARNING(
                f"Vavada recent films backfill: date={start_date}..{today}, "
                f"candidates={total}, selected={len(films)}, "
                f"direct IP, delay={delay:g}s"
            )
        )

        succeeded = 0
        failed = 0
        started_at = time.monotonic()
        for index, film in enumerate(films, start=1):
            request_started = time.monotonic()
            old_tracks = list(film.audio_tracks or [])
            old_is_pirated = film.is_pirated
            self.stdout.write(
                f"\n[{index}/{len(films)}] {film.kino_poisk_id} | "
                f"{film.name_ru or '-'} | old tracks={len(old_tracks)} "
                f"is_pirated={old_is_pirated}"
            )

            try:
                result = parse_single_iframe.run(film.kino_poisk_id)
                film.refresh_from_db(
                    fields=[
                        "is_parsed_ru",
                        "audio_tracks",
                        "is_pirated",
                        "film_content",
                    ]
                )
                elapsed = time.monotonic() - request_started
                tracks = list(film.audio_tracks or [])
                ok = str(result) == str(film.kino_poisk_id)
                succeeded += int(ok)
                failed += int(not ok)
                line = (
                    f"[{index}/{len(films)}] result={result!r} "
                    f"time={elapsed:.1f}s | status={film.is_parsed_ru} "
                    f"is_pirated={film.is_pirated} | "
                    f"tracks={len(tracks)} {tracks!r}"
                )
                self.stdout.write(
                    self.style.SUCCESS(line) if ok else self.style.ERROR(line)
                )
            except Exception as exc:
                failed += 1
                elapsed = time.monotonic() - request_started
                self.stdout.write(
                    self.style.ERROR(
                        f"[{index}/{len(films)}] exception after {elapsed:.1f}s: "
                        f"{type(exc).__name__}: {exc}"
                    )
                )

            if index < len(films) and delay:
                time.sleep(delay)

        total_elapsed = time.monotonic() - started_at
        self.stdout.write(
            self.style.SUCCESS(
                f"\nSUMMARY total={len(films)} ok={succeeded} failed={failed} "
                f"time={total_elapsed:.1f}s"
            )
        )
