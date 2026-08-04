import json
from pathlib import Path

from apps.scrapers.imdb_charts import sync_imdb_top_10_week_chart
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Synchronize IMDb chart collections into the KMAX main database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and parse IMDb, but do not write rows.",
        )
        parser.add_argument(
            "--html-file",
            help="Parse saved IMDb HTML instead of fetching imdb.com.",
        )
        parser.add_argument(
            "--ids",
            nargs="+",
            help="Use explicit IMDb title ids instead of fetching imdb.com.",
        )
        parser.add_argument(
            "--no-kmax-push",
            action="store_true",
            help="Do not POST parsed ids to the Kmax internal cache endpoint.",
        )

    def handle(self, *args, **options):
        try:
            html = None
            if options["html_file"]:
                html = Path(options["html_file"]).read_text(encoding="utf-8")
            result = sync_imdb_top_10_week_chart(
                dry_run=options["dry_run"],
                html=html,
                imdb_ids=options["ids"],
                push_to_kmax=not options["no_kmax_push"],
            )
        except Exception as exc:
            raise CommandError(f"IMDb chart sync failed: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(json.dumps(result, ensure_ascii=False, sort_keys=True))
        )
