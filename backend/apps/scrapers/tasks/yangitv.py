import requests
import time
from celery import shared_task
from ..models import YtConnectContent, ScraperLog, ContentAppContent
import re
from ..utils import parse_age
from django.db import transaction

headers = {
    "User-Agent": "ExoPlayer",
    "Authorization": "Bearer 1659095|4h95ZGeIqyx1DMvLQqddI2KmoSBEizDp8tMRIohr",
    "Accept": "application/json",
}


@shared_task(bind=True, max_retries=3)
def collect_all_ids(self):
    task_name = "YT Id Collector"
    url = "https://admin.yangi.tv/api/v1/search"
    current_page = 1
    total_pages = 1
    new_ids_count = 0

    ScraperLog.objects.create(
        task_name=task_name, status="started", message="Начат сбор id с Yt"
    )

    try:
        while current_page <= total_pages:
            params = {"page": current_page}
            response = requests.get(url, params=params, headers=headers, timeout=10)

            if not response.status_code == 200:
                time.sleep(60)
                continue

            response.raise_for_status()
            data = response.json()

            if current_page == 1:
                total_pages = data["data"]["lastPage"]

            items = data["data"]["list"]

            for item in items:
                content_id = item["id"]
                _, created = YtConnectContent.objects.get_or_create(
                    content_id=content_id,
                    defaults={"parsing_status": "not_parsed"},
                )
                if created:
                    new_ids_count += 1

            current_page += 1
            time.sleep(10)

        ScraperLog.objects.create(
            task_name=task_name,
            status="success",
            message=f"Парсинг окончен. Обработано страниц: {current_page-1}. Новых ID добавлено: {new_ids_count}",
        )

    except Exception as exc:
        ScraperLog.objects.create(
            task_name=task_name,
            status="error",
            message=f"Ошибка на странице {current_page}: {str(exc)}",
        )
        raise self.retry(exc=exc, countdown=300)

    return f"Finished. Total pages processed: {current_page - 1}"


@shared_task(bind=True, max_retries=3)
def connect_yt_content(self):
    task_name = "Connect Yt TO Content"
    url = "https://admin.yangi.tv/api/v1/getContentDetail"

    ScraperLog.objects.create(
        task_name=task_name, status="started", message="Начат скрап детейлов"
    )

    try:
        with transaction.atomic():
            content = (
                YtConnectContent.objects.select_for_update(skip_locked=True)
                .filter(parsing_status="not_parsed")
                .first()
            )
            if not content:
                return

            content.parsing_status = "in_progress"
            content.save(update_fields=["parsing_status"])
            params = {"content_id": content.content_id}
            response = requests.get(url, params=params, headers=headers, timeout=10)

            if not response.status_code == 200:
                time.sleep(60)
                content.parsing_status = "not_parsed"
                content.save(update_fields=["parsing_status"])
                return

            response.raise_for_status()
            data = response.json()

            items = data["data"]

            name_ru = items["name_ru"]
            year = items["year"]
            content_original = ContentAppContent.objects.filter(
                name_ru=name_ru, year_production=year
            ).first()

            if content_original:
                content_original.name_uz = items["name"]
                content_original.description_uz = items["description"]
                content_original.id_uz = content.content_id
                content_original.film_content_uz = content.content_url
                content_original.poster_uz = items["poster"]
                if content_original.age_restriction is None:
                    content_original.age_restriction = parse_age(items.get("age"))

                content_original.save(
                    update_fields=[
                        "name_uz",
                        "description_uz",
                        "id_uz",
                        "film_content_uz",
                        "poster_uz",
                        "age_restriction",
                    ]
                )

                content.parsing_status = "parsed"
                content.save(update_fields=["parsing_status"])

    except Exception as exc:
        ScraperLog.objects.create(
            task_name=task_name,
            status="error",
            message=f"Ошибка {str(exc)}",
        )
        raise self.retry(exc=exc, countdown=300)
