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
        "Последовательно обработать новые фильмы обычным Vavada-парсером "
        "и вывести озвучки и is_pirated. Команда обновляет записи."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--delay", type=float, default=5.0)
        parser.add_argument(
            "--kp-id",
            action="append",
            type=int,
            dest="kp_ids",
            help="Обработать конкретный Kinopoisk ID; можно повторять.",
        )

    def handle(self, *args, **options):
        if settings.VAVADA_PROXY_ENABLED:
            raise CommandError(
                "Для проверки одного IP передайте VAVADA_PROXY_ENABLED=false."
            )
        if not settings.VAVADA_WD_TRUST_COOKIE:
            raise CommandError("Не передан VAVADA_WD_TRUST_COOKIE.")
        if not settings.VAVADA_WD_APPROVAL_COOKIE:
            raise CommandError("Не передан VAVADA_WD_APPROVAL_COOKIE.")

        limit = max(1, min(options["limit"], 200))
        delay = max(0.0, options["delay"])
        requested_ids = options.get("kp_ids") or []

        queryset = Content.objects.exclude(kino_poisk_id__isnull=True)
        if requested_ids:
            queryset = queryset.filter(kino_poisk_id__in=requested_ids)
            films_by_id = {
                film.kino_poisk_id: film
                for film in queryset.only(
                    "kino_poisk_id",
                    "name_ru",
                    "is_parsed_ru",
                    "audio_tracks",
                    "is_pirated",
                )
            }
            films = [
                films_by_id[kp_id]
                for kp_id in requested_ids
                if kp_id in films_by_id
            ][:limit]
        else:
            now = timezone.now()
            today = now.date()
            start_date = today - timedelta(days=settings.PREMIERE)
            retry_after = now - timedelta(hours=4)
            premiere_filter = Q(premiere__range=(start_date, today)) | Q(
                premiere_ru__range=(start_date, today)
            )
            films = list(
                queryset.filter(is_parsed_ru="not_parsed")
                .filter(
                    Q(parsed_at_ru__isnull=True) | Q(parsed_at_ru__lte=retry_after)
                )
                .filter(premiere_filter)
                .order_by("parsed_at_ru", "last_update", "id")
                .only(
                    "kino_poisk_id",
                    "name_ru",
                    "is_parsed_ru",
                    "audio_tracks",
                    "is_pirated",
                )[:limit]
            )

        if not films:
            raise CommandError("Новые фильмы для обработки не найдены.")

        self.stdout.write(
            self.style.WARNING(
                f"Vavada films: direct IP, sequential requests={len(films)}, "
                f"delay={delay:g}s"
            )
        )

        succeeded = 0
        failed = 0
        started_at = time.monotonic()
        for index, film in enumerate(films, start=1):
            request_started = time.monotonic()
            self.stdout.write(
                f"\n[{index}/{len(films)}] {film.kino_poisk_id} | "
                f"{film.name_ru or '-'} | status={film.is_parsed_ru}"
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
                    f"player={bool(film.film_content)} | "
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
