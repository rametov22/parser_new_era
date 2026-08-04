from celery import shared_task

from ..imdb_charts import sync_imdb_top_10_week_chart


@shared_task(queue="default")
def sync_imdb_top_10_week():
    """Refresh IMDb weekly top-10 collection for Kmax."""

    return sync_imdb_top_10_week_chart()
