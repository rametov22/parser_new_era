import re
import time
import random
import json
import datetime as dt
from selenium import webdriver
from fake_useragent import UserAgent
from django.core.cache import cache
from bs4 import BeautifulSoup
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from celery import shared_task
from .. import models
from ..kinopoisk_scrap_codes import *
from ..kinopoisk_scrap_saves import *
from ..kinopoisk_scrap_utils import download_and_save_poster


additional_path_keywords = "keywords/"
additional_path_actors = "cast/"
additional_path_studio = "studio/"
additional_path_like = "like/"
additional_path_awards = "awards/"
additional_path_episodes = "episodes/"


def create_driver():
    """Создает драйвер, подключаясь к удаленному браузеру или локальному"""

    # ua = UserAgent()
    # random_user_agent = ua.random

    options = Options()
    options.binary_location = "/usr/bin/chromium"

    # options.add_argument(f"user-agent={random_user_agent}")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = Service(executable_path="/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)

    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        },
    )

    return driver


def inject_cookies(driver, cookies):
    driver.get("https://www.kinopoisk.ru/robots.txt")

    for cookie in cookies:
        cookie.pop("expiry", None)
        cookie.pop("domain", None)
        try:
            driver.add_cookie(cookie)
        except Exception as e:
            print(f"Не удалось добавить куку: {e}")


def start_global_parsing():
    with open("/app/kinopoisk_cookies.json", "r") as f:
        cookies = json.load(f)
    # cache.set("kp_cookies", cookies, 86400)
    driver = create_driver()
    try:
        inject_cookies(driver, cookies)
        last_page = get_last_page_number(driver)

        print(f"Найдено страниц: {last_page}. Начинаю постановку задач в очередь...")

        for page in range(1, last_page + 1):
            parse_page_list_task.delay(page, cookies)

        print("Все задачи на страницы успешно добавлены в Redis.")
    finally:
        driver.quit()


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


@shared_task(bind=True)
def parse_page_list_task(self, page_number, cookies):
    # cookies = cache.get("kp_cookies")
    # if not cookies:
    #     print("Куки истекли или не найдены в кэше!")
    #     return

    print(f">>> Обработка страницы №{page_number}")
    driver = create_driver()

    try:
        inject_cookies(driver, cookies)
        driver.get(f"https://www.kinopoisk.ru/lists/movies/?page={page_number}")

        time.sleep(random.uniform(1, 3))

        if "showcaptcha" in driver.current_url:
            print(f"!!! КАПЧА на странице {page_number}")
            return

        soup = BeautifulSoup(driver.page_source, "lxml")

        items = soup.find_all("div", attrs={"data-tid": "679d3e26"})

        if not items:
            print(
                f"Предупреждение: На странице {page_number} не найдено ни одного фильма."
            )
            return

        for item in items:
            link = item.find("a", href=re.compile(r"/film/\d+/"))
            if link:
                href = link.get("href")
                kp_id = re.search(r"/film/(\d+)/", href).group(1)

                exists = models.Content.objects.filter(
                    kino_poisk_id=kp_id, is_parsed_kp="parsed"
                ).exists()

                if not exists:
                    print(f"ID {kp_id} готов к парсингу")
                    parse_single_film_task.delay(kp_id, href, cookies)
    except Exception as e:
        print(f"Ошибка на странице {page_number}: {e}")
    finally:
        driver.quit()


@shared_task(bind=True, max_retries=None)
def parse_single_film_task(self, kp_id, href, cookies=None):
    if cookies is None:
        with open("/app/kinopoisk_cookies.json", "r") as f:
            cookies = json.load(f)
    film_href = f"https://www.kinopoisk.ru{href}"
    driver = create_driver()
    try:
        inject_cookies(driver, cookies)
        driver.get(film_href)
        print(f">>> Обработка ID {kp_id}")

        if "showcaptcha" in driver.current_url:
            print(f"Капча на ID {kp_id}")
            return

        time.sleep(2)

        soup = BeautifulSoup(driver.page_source, "lxml")
        # print(soup)
        current_year = dt.datetime.now().year
        print(current_year)

        content_obj = models.Content.objects.filter(kino_poisk_id=kp_id).first()
        print(content_obj)

        is_new_record = content_obj is None
        print(is_new_record)

        name_ru, name_original, short_desc, age = parse_header_info(soup)
        print(name_ru, name_original)
        description = get_description(soup)
        print(description)
        trailer_link = get_trailer(soup)
        print(trailer_link)
        is_serial = get_is_serial(soup)
        print(is_serial)
        premiere, premiere_ru = get_premiere(soup, kp_id)
        print(premiere, premiere_ru)
        year_production = parse_year_production(soup)
        print(year_production)
        slogan = parse_slogan(soup)
        print(slogan)
        kp_rating, imdb_rating, sequel_list = get_ratings_and_sequels(soup)
        print(kp_rating, imdb_rating)
        poster_url = parse_poster(soup)
        print(poster_url)

        print(4)

        if is_new_record:
            print(6)
            content_obj = models.Content(
                kino_poisk_id=kp_id,
                is_serial=is_serial,
                name_ru=name_ru,
                name_original=name_original,
                is_parsed_kp="in_progress",
            )
            content_obj.save()
        else:
            content_obj.is_parsed_kp = "in_progress"
            content_obj.save(update_fields=("is_parsed_kp",))

        if is_new_record or (content_obj.year_production or 0) >= current_year - 8:
            print(5)
            if content_obj.is_serial:
                seasons_dict = parse_serial_seasons(
                    driver,
                    film_href,
                    additional_path_episodes,
                )
                print("seasons:", seasons_dict)
                award_list = parse_awards(driver, film_href, additional_path_awards)
                print("awards:", award_list)
                save_serial_seasons(content_obj, seasons_dict, content_obj.is_serial)
                save_awards(content_obj, award_list)

        if is_new_record or (content_obj.year_production or 0) >= current_year - 1:
            print(6)
            # parse
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
            print("platform_id, platform_name:", platform_id, platform_name)
            print("countries:", country_list)
            print("genres:", genre_list)
            print("directors:", directors_list)
            print("screenwriters:", screenwriters_list)
            print("producers:", producers_list)
            print("operators:", operators_list)
            print("composers:", composers_list)
            print("editors:", editors_list)
            collection_list = parse_collections(driver)
            print("collections:", collection_list)
            actors_list = parse_actors(driver, film_href, additional_path_actors)
            print("actors:", actors_list)
            keyword_list = parse_keywords(driver, film_href, additional_path_keywords)
            print("keywords:", keyword_list)
            studio_list = parse_studios(driver, film_href, additional_path_studio)
            print("studios:", studio_list)
            like_list = parse_like_films(driver, film_href, additional_path_like)
            print("like_films:", like_list)
            platform = attach_film_to_platform(platform_id, kp_id)
            print("platform:", platform)
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
        print("end")

    except Exception as e:
        print(f"Ошибка в фильме {kp_id}, {e}")
    finally:
        driver.quit()
