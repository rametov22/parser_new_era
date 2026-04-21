import re
from urllib.parse import urljoin

from ..kinopoisk_scrap_utils import (
    parse_date,
    parse_people_block,
    safe_float,
    is_placeholder,
)


# header-search
def parse_header_info(soup):
    name_ru = None
    name_orig = None
    age = None
    short_description = None
    try:
        head = soup.find("div", class_=re.compile(r"^styles_header__"))
        if head:
            h1 = head.find("h1", itemprop="name")
            if h1:
                span = h1.find("span")
                if span:
                    name_ru = span.text.split("(")[0].strip()

            name_original_span = head.find(
                "span", class_=re.compile(r"^styles_originalTitle__")
            )
            if name_original_span:
                name_orig = name_original_span.text

            age_span = head.find("span", class_=re.compile(r"^styles_ageRate__"))
            if age_span:
                text = age_span.get_text(strip=True)
                match = re.search(r"(\d+)", text)
                age = int(match.group(1)) if match else None

            topText = head.find("div", class_=re.compile(r"^styles_topText__"))
            if topText:
                short_data = topText.find("p")
                if short_data:
                    short_description = short_data.get_text(strip=True)
    except Exception as ex:
        print(f"Ошибка в head: {ex}")

    if name_orig is None:
        name_orig = name_ru
    if short_description is None:
        short_description = ""
    return name_ru, name_orig, short_description, age


# description-search
def get_description(soup):
    description = None
    try:
        description_p = soup.find("p", class_=re.compile(r"^styles_paragraph__"))
        if description_p:
            description = description_p.get_text(strip=True)
    except Exception as ex:
        print(f"Ошибка в description: {ex}")
    if description is None:
        description = ""
    return description


# trailer-search
def get_trailer(soup):
    trailer = None
    try:
        film_trailer_div = soup.find("div", class_="film-trailer")
        if film_trailer_div:
            trailer_a = film_trailer_div.find("a")
            if trailer_a:
                trailer = "https://www.kinopoisk.ru" + trailer_a.get("href")
        if trailer is None:
            trailer = ""
    except Exception as ex:
        print(f"Ошибка в root: {ex}")
    return trailer


# IS_SERIAL
def get_is_serial(soup):
    is_serial = False
    try:
        table_header = soup.find("h3", class_=re.compile(r"^styles_tableHeader__"))
        if table_header:
            type_film = table_header.text
            if type_film:
                if "фильм" in type_film.lower():
                    is_serial = False
                if "сериал" in type_film.lower():
                    is_serial = True
    except Exception as ex:
        print(f"Ошибка в table_header: {ex}")
    return is_serial


# premiere-search
def get_premiere(soup, kino_poisk_id):
    premiere = None
    premiere_ru = None

    months = {
        "января": "01",
        "февраля": "02",
        "марта": "03",
        "апреля": "04",
        "мая": "05",
        "июня": "06",
        "июля": "07",
        "августа": "08",
        "сентября": "09",
        "октября": "10",
        "ноября": "11",
        "декабря": "12",
    }

    try:
        about_film = soup.find("div", attrs={"data-test-id": "encyclopedic-table"})
        if about_film:
            values_a = about_film.find_all("a")
            for value_a in values_a:
                value_href = value_a.get("href")
                raw_text = value_a.text.strip()
                if value_href and raw_text and raw_text != "...":
                    if "/premiere/ru/" in value_href:
                        premiere_ru = parse_date(raw_text, months)
                    elif f"/film/{kino_poisk_id}/dates/" in value_href:
                        premiere = parse_date(raw_text, months)
    except Exception as ex:
        print(f"Ошибка в премьере: {ex}")

    return premiere, premiere_ru


# year-production-search
def parse_year_production(soup):
    year_production = None
    try:
        about_film = soup.find("div", attrs={"data-test-id": "encyclopedic-table"})
        if about_film:
            values_a = about_film.find_all("a")
            for value_a in values_a:
                value_href = value_a.get("href")
                if value_href:
                    if "/lists/movies/year" in value_href:
                        year_production = int(value_a.text)
    except Exception as ex:
        print(f"Ошибка в year_production: {ex}")
    return year_production


# slogan-search
def parse_slogan(soup):
    slogan = None
    try:
        about_film = soup.find("div", attrs={"data-test-id": "encyclopedic-table"})
        tagline = about_film.find("div", attrs={"data-test-id": "tagline"})
        if tagline:
            value_div = tagline.find("div", class_=re.compile(r"^styles_value"))
            if value_div:
                slogan = value_div.get_text(strip=True)
    except Exception as ex:
        print(f"Ошибка в slogan: {ex}")
    if slogan is None:
        slogan = ""
    return slogan


# details-search
def get_film_details(soup):
    platform_id, platform_name = (
        None,
        None,
    )
    country_list, genre_list = [], []
    directors_list = []
    screenwriters_list = []
    producers_list = []
    operators_list = []
    composers_list = []
    editors_list = []
    try:
        about_film = soup.find("div", attrs={"data-test-id": "encyclopedic-table"})
        if about_film:
            values_a = about_film.find_all("a")
            for value_a in values_a:
                value_href = value_a.get("href")
                if value_href:
                    if "lists/movies/company-originals" in value_href:
                        platform_name = value_a.text
                        platform_id = value_href.split("-")[2].split("/")[0]
                    elif "lists/movies/country" in value_href:
                        country = value_a.text
                        country_id = value_href.split("--")[-1].split("/")[0]
                        country_list.append({"id": country_id, "name": country})
                    elif "lists/movies/genre" in value_href:
                        genre = value_a.text
                        genre_slug = value_href.split("--")[-1].split("/")[0]
                        genre_list.append({"slug": genre_slug, "name": genre})

            directors_list = parse_people_block(about_film, "directors")
            screenwriters_list = parse_people_block(about_film, "writers")
            producers_list = parse_people_block(about_film, "producers")
            operators_list = parse_people_block(about_film, "operators")
            composers_list = parse_people_block(about_film, "composers")
            editors_list = parse_people_block(about_film, "designers")

    except Exception as ex:
        print(f"Ошибка в about_film: {ex}")

    return (
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
    )


# ratings-search
def get_ratings_and_sequels(soup):
    kino_poisk_rating = None
    imdb_rating = None
    sequel_list = []

    try:
        kp_link = soup.find("a", attrs={"data-tid": "kp-movie-rating.rating-value"})
        if kp_link:
            kp_span = kp_link.find("span")
            if kp_span:
                text = kp_span.get_text(strip=True)
                if text:
                    kino_poisk_rating = safe_float(text)

        imdb_block = soup.find("div", attrs={"data-tid": "3d4f49c8"})
        if imdb_block:
            text = imdb_block.get_text(strip=True)
            match = re.search(r"IMDb:\s*([\d.]+)", text)
            if match:
                imdb_rating = safe_float(match.group(1))

        items_container = soup.find(
            "div", class_=re.compile(r"^styles_itemsContainer__")
        )
        if items_container:
            item_containers = items_container.find_all(
                "div", class_=re.compile(r"^styles_carouselItem__")
            )
            if item_containers:
                for item_sequel in item_containers:
                    sequel_a = item_sequel.find("a")
                    if sequel_a and sequel_a.get("href"):
                        sequel_href = sequel_a.get("href")
                        sequel_id = list(filter(None, sequel_href.split("/")))[-1]
                        sequel_list.append({"kino_poisk_ids": sequel_id})

    except Exception as ex:
        print(f"Ошибка в рейтингах или сиквел: {ex}")

    imdb_rating_formatted = f"{imdb_rating:.2f}" if imdb_rating is not None else None

    return kino_poisk_rating, imdb_rating_formatted, sequel_list


# poster-search
def parse_poster(soup):
    try:
        img = soup.select_one("div[class^='styles_posterContainer__'] img")
        if not img:
            return ""

        src = img.get("src")
        if not src:
            return ""

        full_url = urljoin("https://www.kinopoisk.ru", src)

        if is_placeholder(full_url):
            return ""

        return full_url

    except Exception as ex:
        print(f"Ошибка в постере: {ex}")
        return ""
