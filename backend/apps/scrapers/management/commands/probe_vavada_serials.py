import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.scrapers.models import Content
from apps.scrapers.tasks.vavada_serials import parse_vavada_serial


class Command(BaseCommand):
    help = (
        "Последовательно проверить Vavada-сериалы с одного IP и вывести "
        "сезоны, серии и озвучки. Команда обновляет проверенные записи."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--delay", type=float, default=5.0)
        parser.add_argument(
            "--order",
            choices=("oldest", "newest"),
            default="oldest",
            help="Порядок по last_update (по умолчанию: oldest).",
        )
        parser.add_argument(
            "--kp-id",
            action="append",
            type=int,
            dest="kp_ids",
            help="Проверить конкретный Kinopoisk ID; можно повторять.",
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

        queryset = Content.objects.filter(
            is_serial=True,
            film_content__isnull=False,
        ).exclude(kino_poisk_id__isnull=True)
        if requested_ids:
            queryset = queryset.filter(kino_poisk_id__in=requested_ids)
            films_by_id = {
                film.kino_poisk_id: film
                for film in queryset.only(
                    "kino_poisk_id",
                    "name_ru",
                    "last_season",
                    "last_episode",
                    "audio_tracks",
                )
            }
            films = [
                films_by_id[kp_id]
                for kp_id in requested_ids
                if kp_id in films_by_id
            ][:limit]
        else:
            ordering = (
                ("last_update", "kino_poisk_id")
                if options["order"] == "oldest"
                else ("-last_update", "kino_poisk_id")
            )
            films = list(
                queryset.order_by(*ordering).only(
                    "kino_poisk_id",
                    "name_ru",
                    "last_season",
                    "last_episode",
                    "audio_tracks",
                )[:limit]
            )

        if not films:
            raise CommandError("Подходящие сериалы не найдены.")

        self.stdout.write(
            self.style.WARNING(
                f"Vavada probe: direct IP, sequential requests={len(films)}, "
                f"delay={delay:g}s"
            )
        )

        succeeded = 0
        failed = 0
        started_at = time.monotonic()
        for index, film in enumerate(films, start=1):
            old_season = film.last_season
            old_episode = film.last_episode
            request_started = time.monotonic()
            self.stdout.write(
                f"\n[{index}/{len(films)}] {film.kino_poisk_id} | "
                f"{film.name_ru or '-'} | old S:{old_season} E:{old_episode}"
            )

            try:
                result = parse_vavada_serial.run(film.kino_poisk_id)
                film.refresh_from_db(
                    fields=[
                        "last_season",
                        "last_episode",
                        "audio_tracks",
                    ]
                )
                elapsed = time.monotonic() - request_started
                tracks = list(film.audio_tracks or [])
                ok = str(result) == str(film.kino_poisk_id)
                succeeded += int(ok)
                failed += int(not ok)
                line = (
                    f"[{index}/{len(films)}] result={result!r} "
                    f"time={elapsed:.1f}s | S:{film.last_season} "
                    f"E:{film.last_episode} | tracks={len(tracks)} {tracks!r}"
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
