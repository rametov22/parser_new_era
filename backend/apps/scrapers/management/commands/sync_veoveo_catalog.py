import json

from django.core.management.base import BaseCommand, CommandError

from apps.scrapers.tasks.veoveo import (
    run_veoveo_full_sync,
    run_veoveo_incremental_sync,
)


class Command(BaseCommand):
    help = "Synchronize the VeoVeo catalog into the KMAX main database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            choices=("full", "incremental"),
            default="incremental",
            help="full = complete snapshot; incremental = updatedAt window.",
        )

    def handle(self, *args, **options):
        runner = (
            run_veoveo_full_sync
            if options["mode"] == "full"
            else run_veoveo_incremental_sync
        )
        try:
            result = runner()
        except Exception as exc:
            raise CommandError(f"VeoVeo sync failed: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                json.dumps(result, ensure_ascii=False, sort_keys=True)
            )
        )
