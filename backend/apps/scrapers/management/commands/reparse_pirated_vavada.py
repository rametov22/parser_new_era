from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.scrapers.models import Content
from apps.scrapers.tasks.vavada import spawn_pirated_rechecks


class Command(BaseCommand):
    help = "Поставить is_pirated=True контент в очередь повторного Vavada-парсинга."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--min-age-hours", type=int, default=24)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        limit = opts["limit"]
        min_age_hours = opts["min_age_hours"]

        if opts["dry_run"]:
            retry_after = timezone.now() - timedelta(hours=min_age_hours)
            count = (
                Content.objects.filter(is_pirated=True)
                .exclude(is_parsed_ru="in_progress")
                .filter(Q(parsed_at_ru__isnull=True) | Q(parsed_at_ru__lte=retry_after))
                .count()
            )
            self.stdout.write(
                {
                    "candidates_total": count,
                    "limit": limit,
                    "min_age_hours": min_age_hours,
                }.__repr__()
            )
            return

        queued = spawn_pirated_rechecks(limit=limit, min_age_hours=min_age_hours)
        self.stdout.write(self.style.SUCCESS(f"queued={queued}"))
