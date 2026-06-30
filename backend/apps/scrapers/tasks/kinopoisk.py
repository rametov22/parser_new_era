import re
import time
import random
import json
import datetime as dt
from bs4 import BeautifulSoup
from celery import shared_task
from django.utils import timezone
from .. import models
from ..models import ScraperLog
from ..chrome_utils import (
    create_chrome_driver,
    quit_driver,
)
from ..kinopoisk_scrap_codes import *
from ..kinopoisk_scrap_saves import *
from ..kinopoisk_scrap_utils import download_and_save_poster

additional_path_keywords = "keywords/"
additional_path_actors = "cast/"
additional_path_studio = "studio/"
additional_path_like = "like/"
additional_path_awards = "awards/"
additional_path_episodes = "episodes/"
additional_path_other = "other/"


def extract_kp_items_from_list(soup):
    """
    Возвращает [(kp_id, href), ...] со страницы списка KP.

    Не используем data-tid: он меняется. Берём и фильмы, и сериалы.
    """
    items = []
    seen = set()
    for link in soup.find_all("a", href=re.compile(r"/(?:film|series)/\d+/")):
        href = link.get("href") or ""
        match = re.search(r"/(film|series)/(\d+)/", href)
        if not match:
            continue
        kind, kp_id = match.groups()
        if kp_id in seen:
            continue
        seen.add(kp_id)
        items.append((kp_id, f"/{kind}/{kp_id}/"))
    return items


def inject_cookies(driver, cookies):
    driver.get("https://www.kinopoisk.ru/robots.txt")

    for cookie in cookies:
        cookie.pop("expiry", None)
        cookie.pop("domain", None)
        try:
            driver.add_cookie(cookie)
        except Exception as e:
            print(f"Не удалось добавить куку: {e}")


def get_last_page_number(driver):
    driver.get("https://www.kinopoisk.ru/lists/movies/")
    time.sleep(random.uniform(2, 4))

    soup = BeautifulSoup(driver.page_source, "lxml")

    try:
        pagination = soup.find_all("a", class_=re.compile(r"styles_page__"))
        if pagination:
            page_numbers = [int(p.text) for p in pagination if p.text.isdigit()]
            return max(page_numbers) if page_numbers else 1
    except Exception as e:
        print(f"Ошибка при поиске пагинации: {e}")
    return 1


def start_global_parsing():
    with open("/app/kinopoisk_cookies.json", "r") as f:
        cookies = json.load(f)
    # cache.set("kp_cookies", cookies, 86400)
    driver = create_chrome_driver(stealth=False)
    try:
        inject_cookies(driver, cookies)
        last_page = get_last_page_number(driver)

        for page in range(1, last_page + 1):
            parse_page_list_task.delay(page)
    finally:
        quit_driver(driver)


@shared_task(bind=True, queue="kp_pages_queue")
def parse_page_list_task(self, page_number):
    with open("/app/kinopoisk_cookies.json", "r") as f:
        cookies = json.load(f)

    driver = create_chrome_driver(stealth=False)

    try:
        inject_cookies(driver, cookies)
        driver.get(f"https://www.kinopoisk.ru/lists/movies/?page={page_number}")

        time.sleep(random.uniform(1, 3))

        if "showcaptcha" in driver.current_url:
            print(f"КАПЧА на странице {page_number}")
            return

        soup = BeautifulSoup(driver.page_source, "lxml")

        kp_items = extract_kp_items_from_list(soup)
        if not kp_items:
            return

        for kp_id, href in kp_items:
            exists = models.Content.objects.filter(
                kino_poisk_id=kp_id, is_parsed_kp="parsed"
            ).exists()

            if not exists:
                parse_single_film_task.delay(kp_id, href)
    except Exception as e:
        print(f"Ошибка на странице {page_number}: {e}")
        ScraperLog.objects.create(
            task_name=f"KP page {page_number}",
            status="error",
            message=str(e)[:500],
        )
    finally:
        quit_driver(driver)


@shared_task(
    bind=True,
    max_retries=None,
    queue="kp_films_queue",
    soft_time_limit=180,
    time_limit=210,
)
def parse_single_film_task(self, kp_id, href, cookies=None):
    if cookies is None:
        with open("/app/kinopoisk_cookies.json", "r") as f:
            cookies = json.load(f)
    film_href = f"https://www.kinopoisk.ru{href}"

    # Идемпотентность: если задача доставлена повторно (acks_late + рестарт
    # воркера), статус будет уже "parsed" — пропускаем, чтобы не делать
    # двойной парсинг и не накручивать parse_count_kp.
    existing = models.Content.objects.filter(kino_poisk_id=kp_id).only(
        "is_parsed_kp", "parsed_at_kp"
    ).first()
    if (
        existing
        and existing.is_parsed_kp == "parsed"
        and existing.parsed_at_kp
        and (timezone.now() - existing.parsed_at_kp) < dt.timedelta(minutes=5)
    ):
        return

    driver = None
    try:
        try:
            driver = create_chrome_driver(stealth=False)
        except Exception as e:
            error_msg = f"Chrome не стартанул для {kp_id}: {type(e).__name__}: {e}"
            print(error_msg)
            ScraperLog.objects.create(
                task_name=f"KP film {kp_id}",
                status="error",
                message=str(e)[:500],
            )
            models.Content.objects.filter(kino_poisk_id=kp_id).update(
                is_parsed_kp="not_parsed"
            )
            return
        inject_cookies(driver, cookies)
        driver.get(film_href)

        if "showcaptcha" in driver.current_url:
            print(f"Капча на ID {kp_id}")
            models.Content.objects.filter(kino_poisk_id=kp_id).update(
                is_parsed_kp="not_parsed"
            )
            return

        time.sleep(2)

        soup = BeautifulSoup(driver.page_source, "lxml")
        current_year = dt.datetime.now().year

        content_obj = models.Content.objects.filter(kino_poisk_id=kp_id).first()
        is_new_record = content_obj is None

        name_ru, name_original, short_desc, age = parse_header_info(soup)
        description = get_description(soup)
        trailer_link = get_trailer(soup)
        is_serial = get_is_serial(soup)
        premiere, premiere_ru = get_premiere(soup, kp_id)
        year_production = parse_year_production(soup)
        slogan = parse_slogan(soup)
        kp_rating, imdb_rating, sequel_list = get_ratings_and_sequels(soup)
        poster_url = parse_poster(soup)

        # Связи со страницы /other/ — парсим для всех фильмов независимо от года.
        # На главной и в /other/ часто одни и те же фильмы, но у некоторых одни
        # есть только на одной странице → объединяем без дублей по kino_poisk_ids.
        other_relations = parse_other_relations(
            driver, film_href, additional_path_other
        )
        if other_relations:
            seen_ids = {s.get("kino_poisk_ids") for s in sequel_list}
            sequel_list = sequel_list + [
                r for r in other_relations if r["kino_poisk_ids"] not in seen_ids
            ]

        if is_new_record:
            content_obj = models.Content(
                kino_poisk_id=kp_id,
                is_serial=is_serial,
                name_ru=name_ru or "",
                name_original=name_original or "",
                description=description or "",
                is_parsed_kp="in_progress",
            )
            content_obj.save()
        else:
            content_obj.is_parsed_kp = "in_progress"
            content_obj.save(update_fields=("is_parsed_kp",))

        if is_new_record or (content_obj.year_production or 0) >= current_year - 8:
            if content_obj.is_serial:
                seasons_dict = parse_serial_seasons(
                    driver,
                    film_href,
                    additional_path_episodes,
                )
                award_list = parse_awards(driver, film_href, additional_path_awards)
                save_serial_seasons(content_obj, seasons_dict, content_obj.is_serial)
                save_awards(content_obj, award_list)

        if is_new_record or (content_obj.year_production or 0) >= current_year - 1:
            # Нужно заменить collection и film details потому-что в пред запросе он остается на другой странице
            driver.get(film_href)
            time.sleep(2)
            (
                platform_id,
                platform_name,
                country_list,
                genre_list,
                directors_list,
                screenwriters_list,
                producers_list,
                operators_list,
                composers_list,
                editors_list,
            ) = get_film_details(soup)
            collection_list = parse_collections(driver)
            actors_list = parse_actors(driver, film_href, additional_path_actors)
            keyword_list = parse_keywords(driver, film_href, additional_path_keywords)
            studio_list = parse_studios(driver, film_href, additional_path_studio)
            like_list = parse_like_films(driver, film_href, additional_path_like)
            platform = attach_film_to_platform(platform_id, kp_id)
            # save
            save_country(content_obj, country_list)
            save_genre(content_obj, genre_list)
            save_participants(
                content_obj,
                directors_list,
                screenwriters_list,
                producers_list,
                operators_list,
                composers_list,
                editors_list,
            )
            save_collections(content_obj, collection_list)
            save_actors(content_obj, actors_list)
            save_keywords(content_obj, keyword_list)
            save_studio(content_obj, studio_list)
            save_like(content_obj, like_list)
            save_platform(content_obj, platform)

        if not content_obj.poster or content_obj.poster_link != poster_url:
            download_and_save_poster(content_obj, poster_url)

        update_mains(
            content_obj,
            kp_rating,
            imdb_rating,
            age,
            sequel_list,
            short_desc,
            trailer_link,
            is_serial,
            poster_url,
            premiere,
            premiere_ru,
            year_production,
            slogan,
            description,
            name_ru,
            name_original,
        )

        from django.db.models import F

        # Атомарный финал: инкрементируем счётчик ТОЛЬКО если статус всё
        # ещё in_progress. Если другой воркер уже завершил (redelivery)
        # — статус будет "parsed", UPDATE затронет 0 строк, дубля не будет.
        models.Content.objects.filter(
            pk=content_obj.pk, is_parsed_kp="in_progress"
        ).update(
            is_parsed_kp="parsed",
            parsed_at_kp=timezone.now(),
            parse_count_kp=F("parse_count_kp") + 1,
        )

    except Exception as e:
        print(f"Ошибка в фильме {kp_id}, {e}")
        ScraperLog.objects.create(
            task_name=f"KP film {kp_id}",
            status="error",
            message=str(e)[:500],
        )
        models.Content.objects.filter(kino_poisk_id=kp_id).update(
            is_parsed_kp="not_parsed"
        )
    finally:
        if driver:
            quit_driver(driver)
        from .vavada import report_chrome_heartbeat

        report_chrome_heartbeat("kp_films")
