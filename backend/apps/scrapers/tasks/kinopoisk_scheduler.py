"""
Планировщик парсинга Кинопоиска.

Три независимые задачи, работающие по расписанию celery-beat:

1. discover_task  — каждые 3 мин, обходит 20 страниц по курсору.
                    Находит новые фильмы и добавляет их в БД как not_parsed.
                    Полный цикл ~2 дня на 19370 страницах.

2. expire_task    — каждые 10 мин, помечает давно спаршенные записи как not_parsed.
                    TTL = 5 дней.

3. refill_task    — каждую минуту, если очередь kp_films_queue мелкая (< 100),
                    добирает 100 not_parsed записей и кидает в очередь.
"""
import re
import json
import time
import random
from datetime import timedelta

import redis
from bs4 import BeautifulSoup
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .. import models
from .kinopoisk import (
    create_driver,
    inject_cookies,
    get_last_page_number,
    parse_single_film_task,
)


DISCOVER_CURSOR_KEY = "kp:discover:cursor"
LAST_PAGE_CACHE_KEY = "kp:last_page"
LAST_PAGE_TTL = 86400  # 1 день

DISCOVER_PAGES_PER_TICK = 20

QUEUE_NAME = "kp_films_queue"
QUEUE_THRESHOLD = 100
REFILL_BATCH = 100

REPARSE_TTL_DAYS = 5
IN_PROGRESS_STUCK_MINUTES = 30

COOKIES_PATH = "/app/kinopoisk_cookies.json"


def _redis_client():
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=int(settings.REDIS_PORT),
        password=settings.REDIS_PASSWORD,
        decode_responses=True,
    )


def _extract_kp_ids(soup):
    kp_ids = []
    items = soup.find_all("div", attrs={"data-tid": "679d3e26"})
    for item in items:
        link = item.find("a", href=re.compile(r"/film/\d+/"))
        if link:
            match = re.search(r"/film/(\d+)/", link.get("href") or "")
            if match:
                kp_ids.append(match.group(1))
    return kp_ids


@shared_task(bind=True, queue="kp_pages_queue")
def discover_task(self):
    """
    Проходит по страницам Кинопоиска, добавляя новые фильмы в БД.
    Курсор хранится в Redis, инкрементируется каждый тик.
    При достижении последней страницы — откат на 1.
    """
    r = _redis_client()

    with open(COOKIES_PATH, "r") as f:
        cookies = json.load(f)

    driver = create_driver()
    try:
        inject_cookies(driver, cookies)

        cached_last_page = r.get(LAST_PAGE_CACHE_KEY)
        if cached_last_page:
            last_page = int(cached_last_page)
        else:
            last_page = get_last_page_number(driver)
            r.setex(LAST_PAGE_CACHE_KEY, LAST_PAGE_TTL, last_page)

        cursor = int(r.get(DISCOVER_CURSOR_KEY) or 1)
        print(f"[discover] last_page={last_page}, cursor={cursor}")

        pages_done = 0
        new_films = 0

        for offset in range(DISCOVER_PAGES_PER_TICK):
            page = cursor + offset
            if page > last_page:
                page = ((page - 1) % last_page) + 1

            driver.get(f"https://www.kinopoisk.ru/lists/movies/?page={page}")
            time.sleep(random.uniform(1, 2))

            if "showcaptcha" in driver.current_url:
                print(f"[discover] КАПЧА на странице {page}, прерываемся")
                break

            soup = BeautifulSoup(driver.page_source, "lxml")
            kp_ids = _extract_kp_ids(soup)

            if not kp_ids:
                print(f"[discover] Страница {page}: фильмов не найдено")
                pages_done += 1
                continue

            for kp_id in kp_ids:
                _, created = models.Content.objects.get_or_create(
                    kino_poisk_id=kp_id,
                    defaults={
                        "name_ru": "",
                        "name_original": "",
                        "is_serial": False,
                        "is_parsed_kp": "not_parsed",
                    },
                )
                if created:
                    new_films += 1
                    print(f"[discover] +фильм {kp_id}")

            pages_done += 1

        new_cursor = cursor + pages_done
        if new_cursor > last_page:
            new_cursor = 1
            print(f"[discover] Полный цикл пройден, курсор сброшен на 1")

        r.set(DISCOVER_CURSOR_KEY, new_cursor)
        print(
            f"[discover] Готово. Страниц обработано: {pages_done}, "
            f"новых фильмов: {new_films}, следующий курсор: {new_cursor}"
        )

    finally:
        driver.quit()


@shared_task(bind=True, queue="default")
def expire_task(self):
    """
    Сбрасывает в not_parsed:
      - kp parsed старше REPARSE_TTL_DAYS дней (для регулярного обновления)
      - kp in_progress старше IN_PROGRESS_STUCK_MINUTES минут (зависшие)
      - vavada in_progress старше IN_PROGRESS_STUCK_MINUTES минут (зависшие)
    """
    from django.db.models import Q

    now = timezone.now()
    stale_threshold = now - timedelta(days=REPARSE_TTL_DAYS)
    stuck_threshold = now - timedelta(minutes=IN_PROGRESS_STUCK_MINUTES)

    kp_stale = models.Content.objects.filter(
        is_parsed_kp="parsed",
        parsed_at_kp__lt=stale_threshold,
    ).update(is_parsed_kp="not_parsed")

    kp_stuck = models.Content.objects.filter(
        Q(is_parsed_kp="in_progress")
        & (Q(parsed_at_kp__lt=stuck_threshold) | Q(parsed_at_kp__isnull=True))
    ).update(is_parsed_kp="not_parsed")

    vavada_stuck = models.Content.objects.filter(
        Q(is_parsed_ru="in_progress")
        & (Q(parsed_at_ru__lt=stuck_threshold) | Q(parsed_at_ru__isnull=True))
    ).update(is_parsed_ru="not_parsed")

    print(
        f"[expire] kp устаревших parsed: {kp_stale}, "
        f"kp зависших in_progress: {kp_stuck}, "
        f"vavada зависших in_progress: {vavada_stuck}"
    )
    return {
        "kp_stale": kp_stale,
        "kp_stuck": kp_stuck,
        "vavada_stuck": vavada_stuck,
    }


@shared_task(bind=True, queue="default")
def refill_task(self):
    """
    Следит за длиной kp_films_queue. Если меньше порога —
    берёт из БД батч not_parsed и кидает в очередь.
    """
    r = _redis_client()
    length = r.llen(QUEUE_NAME)

    if length >= QUEUE_THRESHOLD:
        print(f"[refill] Очередь {QUEUE_NAME}: {length} задач, порог {QUEUE_THRESHOLD} — пропуск")
        return 0

    # Приоритет:
    #   1) parse_count_kp ASC — никогда успешно не спаршенные (0) первыми,
    #      потом 1-цикловые, и т.д. Не зависит от прошлых неудачных попыток
    #      (refill ставит parsed_at_kp на выдаче, но parse_count_kp
    #      инкрементится только при успехе).
    #   2) -year_production NULLS LAST — внутри одного кол-ва циклов: сначала
    #      свежие года (2026, 2025), потом старые. На перепарсе это даёт
    #      современным фильмам ходить первыми.
    #   3) parsed_at_kp ASC NULLS FIRST — true-NULL первыми, потом давние.
    #   4) -id — стабильный tiebreak.
    from django.db.models import F

    kp_ids = list(
        models.Content.objects.filter(is_parsed_kp="not_parsed")
        .order_by(
            "parse_count_kp",
            F("year_production").desc(nulls_last=True),
            F("parsed_at_kp").asc(nulls_first=True),
            "-id",
        )
        .values_list("kino_poisk_id", flat=True)[:REFILL_BATCH]
    )

    if not kp_ids:
        print(f"[refill] Нет not_parsed записей")
        return 0

    models.Content.objects.filter(kino_poisk_id__in=kp_ids).update(
        is_parsed_kp="in_progress",
        parsed_at_kp=timezone.now(),
    )

    for kp_id in kp_ids:
        parse_single_film_task.delay(kp_id, f"/film/{kp_id}/")

    print(f"[refill] Очередь была {length}, поставлено в очередь: {len(kp_ids)}")
    return len(kp_ids)
