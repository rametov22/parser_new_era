import time
import re
import random
import datetime as dt
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.common.by import By

from . import models


def click_more_button(drivers):
    while True:
        try:
            show_more_button = WebDriverWait(drivers, 5).until(
                ec.element_to_be_clickable(
                    (By.CSS_SELECTOR, "button.styles_showMoreButton___rYMb")
                )
            )
            show_more_button.click()

            time.sleep(1)
        except Exception:
            break


class ElementNotFoundException(Exception):
    pass


def scroll_until_find(drivers, scroll_height=1000, max_height=3000, timeout=30):
    end_time = time.time() + timeout
    current_height = 0

    while time.time() < end_time:
        drivers.execute_script(f"window.scrollBy(0, {scroll_height});")
        time.sleep(2)
        current_height += scroll_height

        try:
            element = drivers.find_element(By.CSS_SELECTOR, "div[data-tid='ea81b24f']")
            if element:
                return element
        except:
            pass

        if current_height >= max_height:
            raise ElementNotFoundException("Collection not found")

    raise ElementNotFoundException("Collection not found within timeout period")


def click_button_square(drivers):
    try:
        square_button = WebDriverWait(drivers, 10).until(
            ec.element_to_be_clickable((By.ID, "js-button"))
        )
        square_button.click()
        drivers.switch_to.default_content()
    except Exception as cbs:
        print("Ошибка при нажатии кнопки", cbs)


# NAME RU NAME ORIG DESCRIPTION
def get_name_and_description(soup):
    name_ru = None
    name_orig = None
    short_description = None
    try:
        h1 = soup.find("h1", itemprop="name")
        if h1:
            span = h1.find("span")
            if span:
                name_ru = span.text.split("(")[0].strip()
    except Exception as ex:
        print(f"Ошибка в name_ru: {ex}")

    try:
        head = soup.find("div", class_=["styles_header__mzj3d", "styles_header__br783"])
        if head:
            name_orig = head.find("span", class_="styles_originalTitle__JaNKM")
            short_description = head.find("div", class_="styles_topText__p__5L")
            if name_orig:
                name_orig = name_orig.text
            if short_description:
                short_description = short_description.text

    except Exception as head_ex:
        print(f"Ошибка в head: {head_ex}")
    if name_orig is None:
        name_orig = name_ru
    if short_description is None:
        short_description = ""
    return name_ru, name_orig, short_description


def get_description(soup):
    description = None
    try:
        description_p = soup.find("p", class_="styles_paragraph__wEGPz")
        if description_p:
            description = description_p.text
    except Exception as description_ex:
        print(f"Ошибка в description: {description_ex}")
    if description is None:
        description = ""
    return description


# TRAILER
def get_trailer(soup):
    trailer = None
    try:
        root = soup.find("div", class_="styles_root__JykRA")
        if root:
            trailer_root = root.find("div", class_="film-trailer")
            if trailer_root:
                trailer_a = trailer_root.find("a")
                if trailer_a:
                    trailer = "https://www.kinopoisk.ru" + trailer_a.get("href")
        if trailer is None:
            trailer = ""
    except Exception as root_ex:
        print(f"Ошибка в root: {root_ex}")
    return trailer


# IS_SERIAL
def get_is_serial(soup):
    is_serial = False
    try:
        table_header = soup.find(
            "h3", class_=["styles_tableHeader__HdxpN", "styles_tableHeader__R2ZOO"]
        )
        if table_header:
            type_film = table_header.text
            if type_film:
                if type_film == "О фильме":
                    is_serial = False
                if type_film == "О сериале":
                    is_serial = True
    except Exception as table_header_ex:
        print(f"Ошибка в table_header: {table_header_ex}")
    return is_serial


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
                        premiere_ru = parse_russian_date(raw_text, months)
                    elif f"/film/{kino_poisk_id}/dates/" in value_href:
                        premiere = parse_russian_date(raw_text, months)
    except Exception as prm:
        print(f"Ошибка в премьере: {prm}")

    return premiere, premiere_ru


def parse_russian_date(date_str, months):
    try:
        date_str = date_str.strip().lower()

        match = re.match(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", date_str)

        if not match:
            raise ValueError(f"Некорректный формат даты: {date_str}")

        day, month_name, year = match.groups()

        month = months.get(month_name)
        if not month:
            raise ValueError(f"Неизвестный месяц: {month_name}")

        formatted_date = f"{year}-{month}-{day.zfill(2)}"

        return dt.datetime.strptime(formatted_date, "%Y-%m-%d").date()

    except Exception as e:
        print(f"Ошибка преобразования даты: {date_str}, {e}")
        return None


# film details
def get_film_details(soup):
    platform_id, platform_name, year_production, slogan, age_restriction = (
        None,
        None,
        None,
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
                    if "/lists/movies/year" in value_href:
                        year_production = value_a.text
                    elif "lists/movies/company-originals" in value_href:
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

            slogan_value = about_film.find("div", attrs={"data-tid": "e1e37c21"})
            if slogan_value:
                slogan = slogan_value.text

            about_participants = about_film.find_all(
                "div", class_=["styles_rowLight__P8Y_1", "styles_rowDark__ucbcz"]
            )
            for about_participant in about_participants:
                if about_participant:
                    title_class = about_participant.find(
                        "div",
                        class_=["styles_titleLight__HIbfT", "styles_titleDark___tfMR"],
                    )
                    value_classes = about_participant.find_all(
                        "a",
                        class_=["styles_linkLight__cha3C", "styles_linkDark__7m929"],
                    )
                    value_age = about_participant.find(
                        "span", class_="styles_rootHighContrast__Bevle"
                    )
                    if title_class:
                        for value_class in value_classes:
                            if title_class.text == "Режиссер":
                                if "/name" in value_class.get("href"):
                                    director_id = value_class.get("href").split("/")[-2]
                                    director_name = value_class.text
                                    directors_list.append(
                                        {"id": director_id, "name": director_name}
                                    )
                            if title_class.text == "Сценарий":
                                if "/name" in value_class.get("href"):
                                    screenwriter_id = value_class.get("href").split(
                                        "/"
                                    )[-2]
                                    screenwriter_name = value_class.text
                                    screenwriters_list.append(
                                        {
                                            "id": screenwriter_id,
                                            "name": screenwriter_name,
                                        }
                                    )
                            if title_class.text == "Продюсер":
                                if "/name" in value_class.get("href"):
                                    producer_id = value_class.get("href").split("/")[-2]
                                    producer_name = value_class.text
                                    producers_list.append(
                                        {"id": producer_id, "name": producer_name}
                                    )
                            if title_class.text == "Оператор":
                                if "/name" in value_class.get("href"):
                                    operator_id = value_class.get("href").split("/")[-2]
                                    operator_name = value_class.text
                                    operators_list.append(
                                        {"id": operator_id, "name": operator_name}
                                    )
                            if title_class.text == "Композитор":
                                if "/name" in value_class.get("href"):
                                    composer_id = value_class.get("href").split("/")[-2]
                                    composer_name = value_class.text
                                    composers_list.append(
                                        {"id": composer_id, "name": composer_name}
                                    )
                            if title_class.text == "Художник":
                                if "/name" in value_class.get("href"):
                                    editor_id = value_class.get("href").split("/")[-2]
                                    editor_name = value_class.text
                                    editors_list.append(
                                        {"id": editor_id, "name": editor_name}
                                    )
                        if title_class.text == "Возраст":
                            age_restriction = value_age.text
                            if age_restriction:
                                age_restriction_match = re.match(
                                    r"(\d+)", age_restriction
                                )
                                if age_restriction_match:
                                    age_restriction = int(
                                        age_restriction_match.group(1)
                                    )

    except Exception as about_film_ex:
        print(f"Ошибка в about_film: {about_film_ex}")

    if slogan is None:
        slogan = ""
    return (
        platform_id,
        platform_name,
        year_production,
        slogan,
        age_restriction,
        country_list,
        genre_list,
        directors_list,
        screenwriters_list,
        producers_list,
        operators_list,
        composers_list,
        editors_list,
    )


# RATINGS AND SEQUELS PARS
def get_ratings_and_sequels(soup):
    kino_poisk_rating = None
    imdb_rating = None
    sequel_list = []

    try:
        styles_film = soup.find("div", class_="styles_rootLight___QD_Q")
        if styles_film:
            film_rating = styles_film.find("div", class_="film-rating")
            if film_rating:
                kino_poisk_rating_elem = film_rating.find(
                    "span",
                    class_=[
                        "styles_ratingKpTop__84afd",
                        "styles_ratingPositive__dzFSI",
                        "styles_ratingNeutral__meu3w",
                    ],
                )
                if kino_poisk_rating_elem:
                    kino_poisk_rating = kino_poisk_rating_elem.text.strip()

                imdb_rating_elem = soup.find(
                    "span", class_="styles_valueSection__0Tcsy"
                )
                if imdb_rating_elem:
                    imdb_rating = imdb_rating_elem.text.split(": ")[1]

        items_container = soup.find("div", class_="styles_itemsContainer__tPx8D")
        if items_container:
            item_container = items_container.find_all(
                "div", class_="styles_carouselItem__4Q2kR"
            )
            if item_container:
                for item_sequel in item_container:
                    sequel_a = item_sequel.find("a")
                    if sequel_a:
                        sequel_href = sequel_a.get("href")
                        if sequel_href:
                            sequel_id = sequel_href.split("/")[-2]
                            sequel_list.append({"kino_poisk_ids": sequel_id})

    except Exception as ex:
        print(f"Ошибка в рейтингах или сиквел: {ex}")

    if kino_poisk_rating is None:
        kino_poisk_rating = 5.0
    if imdb_rating is None:
        imdb_rating = 1.00

    imdb_rating = f"{float(imdb_rating):.2f}"
    return kino_poisk_rating, imdb_rating, sequel_list


# POSTER PARS
def get_poster(soup):
    poster = None
    try:
        root = soup.find("div", class_="styles_root__JykRA")
        if root:
            style_root = root.find("div", class_="styles_root__0qoat")
            if style_root:
                poster_a = style_root.find("a")
                if poster_a:
                    img = poster_a.find("img")
                    if img:
                        src = img.get("src")
                        if src.startswith("//"):
                            poster = "https:" + src
                        elif src.startswith("http://") or src.startswith("https://"):
                            poster = src
                        else:
                            poster = "https://" + src
        if poster is None:
            poster = ""
    except Exception as ex:
        print(f"Ошибка в постере: {ex}")

    return poster


def get_poster_link(soup):
    poster_link = None
    try:
        root = soup.find("div", class_="styles_root__JykRA")
        if root:
            style_root = root.find("div", class_="styles_root__0qoat")
            if style_root:
                poster_a = style_root.find("a")
                if poster_a:
                    img = poster_a.find("img")
                    if img:
                        src = img.get("src")
                        src_set = img.get("srcset")

                        if src_set:
                            src_set_parts = src_set.split(",")
                            for part in src_set_parts:
                                url, size = part.strip().split(" ")
                                if "2x" in size:
                                    if url.startswith("//"):
                                        poster_link = "https:" + url
                                    elif url.startswith("http://") or url.startswith(
                                        "https://"
                                    ):
                                        poster_link = url
                                    else:
                                        poster_link = "https://" + url
        if poster_link is None:
            poster_link = ""
    except Exception as ex:
        print(f"Ошибка в постере: {ex}")

    return poster_link


# AWARD PARS
def get_awards(drivers, film_url, additional_path_awards):
    award_list = []
    try:
        if "series/" in film_url:
            film_url = film_url.replace("series/", "film/")
            award_href = urljoin(film_url, additional_path_awards)
        if "film/" in film_url:
            award_href = urljoin(film_url, additional_path_awards)
        drivers.get(award_href)
        if "showcaptcha" in drivers.current_url:
            print("captcha")
            click_button_square(drivers)
        time.sleep(random.randrange(1, 2))

        WebDriverWait(drivers, 10).until(ec.url_to_be(award_href))
        src = drivers.page_source
        soup = BeautifulSoup(src, "lxml")

        award_td = soup.find("td", style="padding-left: 20px")
        if award_td:
            awards_table = award_td.find_all("table", cellpadding="0")
            if awards_table:
                for award_table in awards_table:
                    awards_image_content = award_table.find("td", align="center")
                    image = None
                    if awards_image_content:
                        awards_image_content_a = awards_image_content.find("a")
                        if awards_image_content_a:
                            awards_img = awards_image_content_a.find("img")
                            if awards_img:
                                src = awards_img.get("src")
                                if src.startswith("//"):
                                    image = "https:" + src
                                elif src.startswith("http://") or src.startswith(
                                    "https://"
                                ):
                                    image = src
                                else:
                                    image = "https://" + src
                    #
                    awards_info = award_table.find("b")
                    winner_content = []
                    winner_participant = []
                    nomination_content = []
                    nomination_participant = []
                    if awards_info:
                        awards_info2 = awards_info.find("a")
                        if awards_info2:
                            award_name = awards_info2.text.split(",")[0]
                            award_slug = awards_info2.get("href").split("/")[-3]
                            award_year = awards_info2.get("href").split("/")[-2]
                            winner_labels = award_table.find_all(
                                "font", color="#ff6600"
                            )
                            nomination_labels = award_table.find("b", text="Номинации")
                            for label in winner_labels:
                                row = label.find_parent("tr")
                                next_row = row.find_next_sibling("tr")
                                td = next_row.find("td")
                                if td:
                                    ul = td.find("ul")
                                    if ul:
                                        li_trivia = ul.find_all("li")
                                        for li in li_trivia:
                                            li_text = li.get_text(strip=True)
                                            if "(" in li_text and ")" in li_text:
                                                winners_a = li.find_all("a")
                                                winners_id = []
                                                if winners_a:
                                                    for winner_a in winners_a:
                                                        winner_href = winner_a.get(
                                                            "href"
                                                        )
                                                        if "name/" in winner_href:
                                                            winner_id = (
                                                                winner_href.split("/")[
                                                                    2
                                                                ]
                                                            )
                                                            winners_id.append(winner_id)
                                                li_name = li_text.split("(")[0]
                                                try:
                                                    winner_participant.append(
                                                        {
                                                            "name": li_name,
                                                            "winner_id": winners_id,
                                                        }
                                                    )
                                                except Exception as winners_ex:
                                                    print(
                                                        f"Ошибка в выигрышах участника - {li_text}, "
                                                        f"{winners_ex}"
                                                    )
                                            else:
                                                winner_content.append(li_text)
                            #
                            if nomination_labels:
                                next_nom_row = nomination_labels.find_parent(
                                    "tr"
                                ).find_next_sibling("tr")
                                td = next_nom_row.find("td")
                                if td:
                                    ul = td.find("ul")
                                    if ul:
                                        li_trivia = ul.find_all("li")
                                        for li in li_trivia:
                                            li_text = li.get_text(strip=True)
                                            if "(" in li_text and ")" in li_text:
                                                nominations_a = li.find_all("a")
                                                nominations_id = []
                                                if nominations_a:
                                                    for nomination_a in nominations_a:
                                                        nomination_href = (
                                                            nomination_a.get("href")
                                                        )
                                                        if "name/" in nomination_href:
                                                            nomination_id = (
                                                                nomination_href.split(
                                                                    "/"
                                                                )[2]
                                                            )
                                                            nominations_id.append(
                                                                nomination_id
                                                            )
                                                li_name = li_text.split("(")[0].strip()
                                                try:
                                                    nomination_participant.append(
                                                        {
                                                            "name": li_name,
                                                            "nomination_id": nominations_id,
                                                        }
                                                    )
                                                except Exception as nomination_ex:
                                                    print(
                                                        f"Ошибка в номинациях участника - {li_text},"
                                                        f"{nomination_ex}"
                                                    )
                                            else:
                                                nomination_content.append(li_text)
                            #
                            award_list.append(
                                {
                                    "name": award_name,
                                    "slug": award_slug,
                                    "image": image,
                                    "award_year": award_year,
                                    "winner_content": winner_content,
                                    "winner_participant": winner_participant,
                                    "nomination_participant": nomination_participant,
                                    "nomination_content": nomination_content,
                                }
                            )

    except Exception as award_except:
        print("ошибка в наградах", award_except)
    return award_list


# SEASON PARS
def parse_serial_seasons(film_hrefs, additional_path_episodes, drivers, is_serial):
    seasons_dict = {}
    months_dict = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
    }
    try:
        if is_serial:
            if "series/" in film_hrefs:
                film_hrefs = film_hrefs.replace("series/", "film/")
                episodes_href = urljoin(film_hrefs, additional_path_episodes)
            if "film/" in film_hrefs:
                episodes_href = urljoin(film_hrefs, additional_path_episodes)

            drivers.get(episodes_href)

            if "showcaptcha" in drivers.current_url:
                print("captcha")
                click_button_square(drivers)

            time.sleep(random.randrange(1, 2))

            WebDriverWait(drivers, 10).until(ec.url_to_be(episodes_href))
            src = drivers.page_source
            soup = BeautifulSoup(src, "lxml")

            serial_td = soup.find("td", colspan="2", style="padding-left:20px")
            if serial_td:
                serial_tables = serial_td.find_all("table", width="100%")
                if serial_tables:
                    current_season_id = None
                    for serial_table in serial_tables:
                        serial_trs = serial_table.find_all("tr")
                        if serial_trs:
                            for serial_tr in serial_trs:
                                season = episode = ru_name = original_name = (
                                    release_date
                                ) = None

                                serial_tds = serial_tr.find("td", class_="news")
                                release_tds = serial_tr.find(
                                    "td",
                                    style="border-bottom:1px dotted #ccc;padding:15px 0px;font-size:12px",
                                )

                                if serial_tds:
                                    season_test = serial_tds.find(
                                        "h1",
                                        style="font-size:21px;padding:0px;margin:0px;color:#f60",
                                    )
                                    if season_test:
                                        season = season_test.text
                                    episode_test = serial_tds.find(
                                        "span", style="color:#777"
                                    )
                                    if episode_test:
                                        episode = episode_test.text
                                    ru_name_test = serial_tds.find(
                                        "h1",
                                        style="font-size:16px;padding:0px;color:#444",
                                    )
                                    if ru_name_test:
                                        ru_name = ru_name_test.text
                                    original_name_test = serial_tds.find(
                                        "span", class_="episodesOriginalName"
                                    )
                                    if original_name_test:
                                        original_name = original_name_test.text

                                if release_tds:
                                    release_date = release_tds.text.strip()

                                # Обработка сезонов и эпизодов
                                if season:
                                    season_match = re.search(r"\d+", season)
                                    if season_match:
                                        current_season_id = int(season_match.group())
                                        seasons_dict[current_season_id] = dict()
                                else:
                                    if isinstance(episode, str):
                                        episode_id = int(episode.split(" ")[-1])

                                        # Проверка на корректность формата release_date
                                        if (
                                            release_date
                                            and len(release_date.split(" ")) == 3
                                        ):
                                            try:
                                                day, month, year = release_date.split(
                                                    " "
                                                )
                                                month = months_dict.get(month, None)
                                                if month:
                                                    date = str(
                                                        dt.date(
                                                            year=int(year),
                                                            month=month,
                                                            day=int(day),
                                                        )
                                                    )
                                                else:
                                                    date = None
                                            except ValueError:
                                                date = None
                                        else:
                                            date = None

                                        seasons_dict[current_season_id][episode_id] = {
                                            "name": ru_name,
                                            "original_name": original_name,
                                            "date": date,
                                        }
    except Exception as series_ex:
        print("Ошибка в серии:", series_ex)
    return seasons_dict


def parse_film_details(
    drivers,
    film_hrefs,
    additional_path,
    additional_path_keywords,
    additional_path_studio,
    additional_path_like,
    platform_id,
    kino_poisk_id,
):
    collections_list = []
    try:
        scroll_until_find(drivers)
        WebDriverWait(drivers, 20).until(
            ec.presence_of_element_located((By.CSS_SELECTOR, '[data-tid="ea81b24f"]'))
        )
        click_more_button(drivers)
        soup = BeautifulSoup(drivers.page_source, "html.parser")

        movie_list = soup.find("div", attrs={"data-tid": "ea81b24f"})
        if movie_list:
            movies_a = movie_list.find_all("a")
            for movie_a in movies_a:
                if movie_a:
                    movie_href = movie_a.get("href")
                    movie_name = movie_a.find("h3", class_="styles_title__4qO8I")
                    collection_slug = ""
                    collection_name = ""
                    if movie_href:
                        collection_slug = movie_href.split("/")[3]
                    if movie_name:
                        collection_name = movie_name.text
                    collections_list.append(
                        {"slug": collection_slug, "name": collection_name}
                    )
    except Exception as movie_list_ex:
        print(f"Ошибка в styles_film: {movie_list_ex}")

    actors_list = []
    try:
        if "series/" in film_hrefs:
            film_hrefs = film_hrefs.replace("series/", "film/")
            actors_href = urljoin(film_hrefs, additional_path)
        if "film/" in film_hrefs:
            actors_href = urljoin(film_hrefs, additional_path)
        drivers.get(actors_href)
        if "showcaptcha" in drivers.current_url:
            print("captcha")
            click_button_square(drivers)
        time.sleep(random.randrange(1, 2))

        WebDriverWait(drivers, 10).until(ec.url_to_be(actors_href))

        src = drivers.page_source
        soup = BeautifulSoup(src, "lxml")

        actors_header = soup.find("div", text="Актеры")
        if actors_header:
            next_element = actors_header.find_next_sibling("div")
            while next_element:
                if (
                    next_element.get("style")
                    == "padding-left: 20px; border-bottom: 2px solid #f60; font-size: 16px"
                ):
                    break
                class_name = next_element.get("class", [])
                if "dub" in class_name:
                    name_div = next_element.find("div", class_="name")
                    if name_div:
                        actor_href = name_div.find("a")
                        if actor_href:
                            id_actor = actor_href.get("href").split("/")[-2]
                            ru_name = actor_href.text
                    role_div = next_element.find("div", class_="role")
                    if role_div:
                        role_text = role_div.text
                        if role_text:
                            role = role_text.split("... ")[-1]
                        actors_list.append(
                            {"id": id_actor, "name": ru_name, "role": role}
                        )
                next_element = next_element.find_next_sibling("div")
    except Exception as actor_header_ex:
        print(f"Ошибка при выполнении кода: {actor_header_ex}")

    keyword_list = []
    try:
        if "series/" in film_hrefs:
            film_hrefs = film_hrefs.replace("series/", "film/")
            keywords_href = urljoin(film_hrefs, additional_path_keywords)
        if "film/" in film_hrefs:
            keywords_href = urljoin(film_hrefs, additional_path_keywords)
        drivers.get(keywords_href)
        if "showcaptcha" in drivers.current_url:
            print("captcha")
            click_button_square(drivers)
        time.sleep(random.randrange(1, 2))

        WebDriverWait(drivers, 10).until(ec.url_to_be(keywords_href))
        src = drivers.page_source
        soup = BeautifulSoup(src, "lxml")

        block_left = soup.find("div", class_="block_left")
        if block_left:
            keywords_list = block_left.find_all("ul", class_="keywordsList")
            if keywords_list:
                for list_ in keywords_list:
                    keywords_list_li = list_.find_all("li")
                    if keywords_list_li:
                        for li in keywords_list_li:
                            keyword_a = li.find("a")
                            if keyword_a:
                                keyword_name = keyword_a.text
                                keyword_id = keyword_a.get("href").split("/")[-2]
                                keyword_list.append(
                                    {"id": keyword_id, "name": keyword_name}
                                )
    except Exception as keywords_href_ex:
        print(f"Ошибка при выполнении кода: {keywords_href_ex}")

    studio_list = []
    try:
        if "series/" in film_hrefs:
            film_hrefs = film_hrefs.replace("series/", "film/")
            studio_href = urljoin(film_hrefs, additional_path_studio)
        if "film/" in film_hrefs:
            studio_href = urljoin(film_hrefs, additional_path_studio)
        drivers.get(studio_href)
        if "showcaptcha" in drivers.current_url:
            print("captcha")
            click_button_square(drivers)
        time.sleep(random.randrange(1, 2))

        WebDriverWait(drivers, 10).until(ec.url_to_be(studio_href))
        src = drivers.page_source
        soup = BeautifulSoup(src, "lxml")

        margin_left = soup.find("div", style="margin-left: 64px; text-align: left")
        if margin_left:
            studio_table = margin_left.find("table")
            if studio_table:
                studio_body = studio_table.find("tbody")
                if studio_body:
                    studio_tr = studio_table.find_all("tr")
                    if studio_tr:
                        for tr_ in studio_tr:
                            studio_td = tr_.find_all("td")
                            if studio_td:
                                for td in studio_td:
                                    studio_a = td.find("a")
                                    if studio_a:
                                        studio_name = studio_a.text
                                        studio_href = studio_a.get("href")
                                        if studio_href:
                                            studio_id = studio_href.split("/")[-2]
                                        studio_list.append(
                                            {"id": studio_id, "name": studio_name}
                                        )
    except Exception as studio_href_ex:
        print(f"Ошибка при выполнении кода: {studio_href_ex}")

    like_list = []
    try:
        if "series/" in film_hrefs:
            film_hrefs = film_hrefs.replace("series/", "film/")
            like_href = urljoin(film_hrefs, additional_path_like)
        if "film/" in film_hrefs:
            like_href = urljoin(film_hrefs, additional_path_like)
        drivers.get(like_href)
        if "showcaptcha" in drivers.current_url:
            print("captcha")
            click_button_square(drivers)
        time.sleep(random.randrange(1, 2))

        WebDriverWait(drivers, 10).until(ec.url_to_be(like_href))
        src = drivers.page_source
        soup = BeautifulSoup(src, "lxml")

        like_table = soup.find("table", class_="ten_items")
        if like_table:
            like_trs = like_table.find_all("tr")
            for like_tr in like_trs:
                like_tr_id = like_tr.get("id")
                if like_tr_id and like_tr_id.startswith("tr_"):
                    like_id = like_tr_id.split("_")[1]
                    like_list.append({"kino_poisk_ids": like_id})
    except Exception as like_href_ex:
        print(f"Ошибка при выполнении кода: {like_href_ex}")

    platform_obj = None
    try:
        platform_obj = models.Platform.objects.filter(platform_id=platform_id).first()

        if platform_obj:
            platform_films = platform_obj.films

            if not platform_films:
                platform_films = {}

            if "platform_films_ids" in platform_films.keys():
                if kino_poisk_id not in platform_films["platform_films_ids"]:
                    print(kino_poisk_id)
                    platform_films["platform_films_ids"].append(kino_poisk_id)
            else:
                platform_films["platform_films_ids"] = []
            platform_obj.films = platform_films
            platform_obj.save()
    except Exception as platform_id_ex:
        print(f"Ошибка при сохранении платформы: {platform_id_ex}")

    return (
        collections_list,
        actors_list,
        keyword_list,
        studio_list,
        like_list,
        platform_obj,
    )
