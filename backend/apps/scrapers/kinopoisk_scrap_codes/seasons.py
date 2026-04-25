import re
from urllib.parse import urljoin

from ..kinopoisk_scrap_utils import (
    normalize_film_href,
    load_page_and_soup,
    extract_int,
    parse_ru_date,
)


# seasons-search
def parse_serial_seasons(drivers, film_hrefs, additional_path):
    seasons = {}

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
        film_hrefs = normalize_film_href(film_hrefs)
        href = urljoin(film_hrefs, additional_path)

        soup = load_page_and_soup(drivers, href)

        container = soup.find("td", colspan="2", style="padding-left:20px")
        if not container:
            return seasons

        tables = container.find_all("table", width="100%")
        if not tables:
            return seasons

        current_season = None

        for table in tables:
            for tr in table.find_all("tr"):
                info_td = tr.find("td", class_="news")
                date_td = tr.find(
                    "td",
                    style="border-bottom:1px dotted #ccc;padding:15px 0px;font-size:12px",
                )

                if not info_td:
                    continue

                season_text = info_td.find(
                    "h1", style="font-size:21px;padding:0px;margin:0px;color:#f60"
                )
                if season_text:
                    season_id = extract_int(season_text.text)
                    if season_id is not None:
                        current_season = str(season_id)
                        seasons.setdefault(current_season, {})
                    continue

                if current_season is None:
                    continue

                episode_text = info_td.find("span", style=re.compile(r"color:#777"))
                episode_id = extract_int(episode_text.text if episode_text else None)
                if episode_id is None:
                    continue
                episode_id = str(episode_id)

                ru_name = info_td.find("h1", style=re.compile(r"font-size:16px"))
                ru_name = ru_name.text.strip() if ru_name else None

                original_name = info_td.find("span", class_="episodesOriginalName")
                original_name = original_name.text.strip() if original_name else None

                release_date = (
                    parse_ru_date(date_td.text.strip(), months_dict)
                    if date_td
                    else None
                )

                seasons[current_season][episode_id] = {
                    "name": ru_name,
                    "original_name": original_name,
                    "date": release_date,
                }

    except Exception as ex:
        print("Ошибка в parse_serial_seasons:", ex)

    return seasons
