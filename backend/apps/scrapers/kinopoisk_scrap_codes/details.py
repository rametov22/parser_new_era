import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from django.db import transaction

from ..kinopoisk_scrap_utils import (
    normalize_film_href,
    load_page_and_soup,
    click_more_button,
    scroll_until_find,
    clean_role,
)
from .. import models


# collections-search
def parse_collections(drivers):
    collections = []

    element = scroll_until_find(drivers)
    if not element:
        return collections

    click_more_button(drivers)

    soup = BeautifulSoup(drivers.page_source, "html.parser")
    movie_list = soup.find("div", attrs={"data-tid": "ea81b24f"})

    if not movie_list:
        return collections

    for a in movie_list.find_all("a"):
        href = a.get("href")
        title = a.find("h3", class_=re.compile(r"^styles_title__"))

        if not href or not title:
            continue

        collections.append(
            {"slug": href.split("/")[3], "name": title.get_text(strip=True)}
        )
    return collections


# actors-search
def parse_actors(drivers, film_hrefs, additional_path):
    actors = []

    film_hrefs = normalize_film_href(film_hrefs)
    href = urljoin(film_hrefs, additional_path)

    soup = load_page_and_soup(drivers, href)

    actors_header = soup.find("div", string="Актеры")
    if not actors_header:
        return actors

    el = actors_header.find_next_sibling("div")
    while el:
        if (
            el.get("style")
            == "padding-left: 20px; border-bottom: 2px solid #f60; font-size: 16px"
        ):
            break

        if "dub" in el.get("class", []):
            name_a = el.select_one("div.name a")
            role_div = el.select_one("div.role")

            if name_a and role_div:
                actors.append(
                    {
                        "id": name_a["href"].split("/")[-2],
                        "name": name_a.get_text(strip=True),
                        "role": clean_role(
                            role_div.get_text(strip=True).split("... ")[-1]
                        ),
                    }
                )
        el = el.find_next_sibling("div")

    return actors


# keywords-search
def parse_keywords(drivers, film_hrefs, additional_path):
    keywords = []

    film_hrefs = normalize_film_href(film_hrefs)
    href = urljoin(film_hrefs, additional_path)

    soup = load_page_and_soup(drivers, href)

    block_left = soup.find("div", class_="block_left")
    if not block_left:
        return keywords

    for li in block_left.select("ul.keywordsList li a"):
        keywords.append(
            {
                "id": li["href"].split("/")[-2],
                "name": li.get_text(strip=True),
            }
        )

    return keywords


# studio-search
def parse_studios(drivers, film_hrefs, additional_path):
    studios = []

    film_hrefs = normalize_film_href(film_hrefs)
    href = urljoin(film_hrefs, additional_path)

    soup = load_page_and_soup(drivers, href)

    for a in soup.select('div[style="margin-left: 64px; text-align: left"] table a'):
        studios.append(
            {
                "id": a["href"].split("/")[-2],
                "name": a.get_text(strip=True),
            }
        )

    return studios


# like-search
def parse_like_films(drivers, film_hrefs, additional_path):
    likes = []

    film_hrefs = normalize_film_href(film_hrefs)
    href = urljoin(film_hrefs, additional_path)

    soup = load_page_and_soup(drivers, href)

    for tr in soup.select("table.ten_items tr[id^='tr_']"):
        likes.append({"kino_poisk_ids": tr["id"].split("_")[1]})

    return likes


# Карта секций на странице /other/ → значение поля "relation"
# в additional["sequel"]. Названия секций со страницы Кинопоиска,
# приведённые к нижнему регистру.
OTHER_RELATIONS_SECTIONS = {
    "начало": "начало",
    "продолжение": "продолжение",
    "приквел": "приквел",
    "ремейк": "ремейк",
    "спин-офф": "спин-офф",
    "версия фильма": "версия фильма",
    "сиквел": "сиквел",
    "сиквелы и приквелы": "сиквел",
}


# other-relations-search
def parse_other_relations(drivers, film_hrefs, additional_path):
    """
    Парсит страницу /other/ — связи фильма: продолжение, приквел, ремейк,
    спин-офф, версия фильма, начало.
    Возвращает плоский список:
      [{"kino_poisk_ids": "12345"}, ...]
    """
    relations = []
    try:
        film_hrefs = normalize_film_href(film_hrefs)
        href = urljoin(film_hrefs, additional_path)

        soup = load_page_and_soup(drivers, href)

        # Каждая секция = inner <table> с <td class="main_line">Название</td>
        # и <div class="personPageItems"> со списком фильмов внутри той же таблицы.
        for section_td in soup.find_all("td", class_="main_line"):
            section_name = section_td.get_text(strip=True).lower()
            relation_key = OTHER_RELATIONS_SECTIONS.get(section_name)
            if not relation_key:
                continue

            section_table = section_td.find_parent("table")
            if not section_table:
                continue

            items_container = section_table.find("div", class_="personPageItems")
            if not items_container:
                continue

            for item_div in items_container.find_all("div", class_="item"):
                link = item_div.find(
                    "a", href=re.compile(r"/(?:film|series)/\d+/")
                )
                if not link:
                    continue
                match = re.search(
                    r"/(?:film|series)/(\d+)/", link.get("href", "")
                )
                if not match:
                    continue
                relations.append({"kino_poisk_ids": match.group(1)})
    except Exception as ex:
        print(f"Ошибка в other relations: {ex}")

    return relations


# platform-attach-search
def attach_film_to_platform(
    platform_id,
    kino_poisk_id,
):
    if platform_id is None:
        return None

    kp_id_str = str(kino_poisk_id)

    with transaction.atomic(using="main_db"):
        platform = (
            models.Platform.objects.select_for_update()
            .filter(platform_id=platform_id)
            .first()
        )
        if not platform:
            return None

        films = platform.films or {}
        ids = [str(x) for x in films.get("platform_films_ids", [])]
        if kp_id_str not in ids:
            ids.append(kp_id_str)

        films["platform_films_ids"] = ids
        platform.films = films
        platform.save(update_fields=["films"])

    return platform
