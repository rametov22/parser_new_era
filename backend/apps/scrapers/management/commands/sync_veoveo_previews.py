import json

from apps.scrapers.tasks.veoveo_previews import run_veoveo_preview_pipeline
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Update VeoVeo episode previews and mirror missing images to S3."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Check every serial; existing S3 files are still reused.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Process at most N serials in each stage; 0 means all.",
        )
        parser.add_argument(
            "--kp-id",
            type=int,
            default=None,
            help="Process only one content by Kinopoisk ID.",
        )
        parser.add_argument(
            "--veoveo-id",
            type=int,
            default=None,
            help="Process only one VeoVeo content ID.",
        )

    def handle(self, *args, **options):
        if options["limit"] < 0:
            raise CommandError("--limit cannot be negative")
        if options["kp_id"] is not None and options["kp_id"] <= 0:
            raise CommandError("--kp-id must be positive")
        if options["veoveo_id"] is not None and options["veoveo_id"] <= 0:
            raise CommandError("--veoveo-id must be positive")
        if options["kp_id"] is not None and options["veoveo_id"] is not None:
            raise CommandError("--kp-id and --veoveo-id cannot be used together")
        try:
            result = run_veoveo_preview_pipeline(
                force=options["force"],
                limit=options["limit"],
                kp_id=options["kp_id"],
                veoveo_id=options["veoveo_id"],
            )
        except Exception as exc:
            raise CommandError(f"VeoVeo preview sync failed: {exc}") from exc
        self.stdout.write(
            self.style.SUCCESS(json.dumps(result, ensure_ascii=False, sort_keys=True))
        )
