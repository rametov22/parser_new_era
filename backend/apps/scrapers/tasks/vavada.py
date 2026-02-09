import re
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db.models import Q
from selenium.webdriver.chrome.service import Service
from fake_useragent import UserAgent
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from bs4 import BeautifulSoup

from ..models import ContentAppContent, ScraperLog

PROXIES = [
    "91.243.188.143:7951:ingp3040902:xB4pki06bZ",
]

# def create_driver():
#     proxy_str = random.choice(PROXIES)
#     p_ip, p_port, p_user, p_pass = proxy_str.split(':')

#     wire_options = {
#         'proxy': {
#             'http': f'http://{p_user}:{p_pass}@{p_ip}:{p_port}',
#             'https': f'https://{p_user}:{p_pass}@{p_ip}:{p_port}',
#             'no_proxy': 'localhost,127.0.0.1'
#         }
#     }

#     options = Options()
#     options.add_argument(f"user-agent={ua.random}")
#     options.add_argument("--headless=new") # 'new' — более современный режим
#     options.add_argument("--no-sandbox")
#     options.add_argument("--disable-dev-shm-usage")
#     options.add_argument("--disable-blink-features=AutomationControlled")
#     options.add_experimental_option("excludeSwitches", ["enable-automation"])
#     options.add_experimental_option("useAutomationExtension", False)

#     driver = webdriver.Chrome(options=options, seleniumwire_options=wire_options)

#     # Сверхважная правка для скрытности:
#     driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
#         "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
#     })

#     return driver


def create_driver():
    """Создает драйвер, подключаясь к удаленному браузеру или локальному"""

    ua = UserAgent()
    random_user_agent = ua.random

    options = Options()
    options.binary_location = "/usr/bin/chromium"

    options.add_argument("--lang=ru-RU")
    options.add_experimental_option(
        "prefs", {"intl.accept_languages": "ru,ru-RU,en-US,en"}
    )

    options.add_argument(f"user-agent={random_user_agent}")
    options.add_argument("--headless=new")  # В докере только headless
    options.add_argument("--no-sandbox")

    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = Service(executable_path="/usr/bin/chromedriver")

    driver = webdriver.Chrome(service=service, options=options)
    return driver


@shared_task()
def spawn_iframe_parsers():
    """Диспетчер: находит фильмы без контента и ставит их в очередь Celery"""
    today = timezone.now().date()
    start_date = today - timedelta(days=180)
    cut_date = today - timedelta(days=4)

    date_filter = Q(premiere__range=(start_date, today)) | Q(
        premiere_ru__range=(start_date, today)
    )

    films_to_update = (
        ContentAppContent.objects.filter(
            film_content__isnull=True, last_update__lte=cut_date
        )
        .filter(date_filter)
        .values_list("kino_poisk_id", flat=True)
    )

    for kp_id in films_to_update:
        parse_single_iframe.delay(kp_id)


# concurrency 3
@shared_task(
    bind=True,
    queue="vavada_queue",
    max_retries=3,
    rate_limit="10/m",
    acks_late=True,
)
def parse_single_iframe(self, kp_id):
    """Парсинг одного конкретного фильма через Selenium"""
    driver = None
    try:
        film = ContentAppContent.objects.get(kino_poisk_id=kp_id)
        driver = create_driver()

        # Логика из check_and_pars_iframe
        url = f"https://iframe.cloud/iframe/{kp_id}"
        driver.get(url)

        # Ожидание фрейма
        wait = WebDriverWait(driver, 10)
        try:
            wait.until(
                lambda d: (
                    (iframe := d.find_element(By.ID, "playerFrame"))
                    and iframe.get_attribute("src")
                    and not iframe.get_attribute("src").startswith("https://iframe")
                )
            )
        except Exception:
            film.last_update = timezone.now()
            film.save(update_fields=["last_update"])
            return f"No player found for {kp_id}"

        # Сохраняем основные данные
        film.film_content = f"https://vavada.video/iframe/{kp_id}"
        film.add_content_date = timezone.now().date()

        # Переключаемся во фрейм для аудиодорожек
        driver.switch_to.frame(0)
        soup = BeautifulSoup(driver.page_source, "lxml")

        # логика поиска дорожек
        filtered_audio_tracks = []
        track_div = soup.find("div", id="player")
        if track_div:
            playlist = track_div.find("pjsdiv", id="player_playlist1")
            if playlist:
                playlist_scroll = playlist.find("pjsdiv", class_="pjsplplayerscroll")
                if playlist_scroll:
                    items = playlist_scroll.find_all("pjsdiv", attrs={"me": True})
                    for i in items:
                        text = i.get_text(strip=True)
                        if text:
                            filtered_audio_tracks.append(text)

            if film.is_serial:
                episode_numbers = []
                episode_wrapper = track_div.find("pjsdiv", id="player_playlist2")
                if episode_wrapper:
                    episode_scroll = episode_wrapper.find(
                        "pjsdiv", class_="pjsplplayerscroll"
                    )
                    if episode_scroll:
                        episode_items = episode_scroll.find_all("pjsdiv")
                        for item in episode_items:
                            text = item.get_text(strip=True)
                            if text:
                                match = re.search(r"(\d+)", text)
                                if match:
                                    episode_numbers.append(int(match.group(1)))

                if episode_numbers:
                    film.last_episode = str(max(episode_numbers))
                else:
                    film.last_episode = None

                season_numbers = []
                season_wrapper = track_div.find("pjsdiv", id="player_playlist3")
                if season_wrapper:
                    season_scroll = season_wrapper.find(
                        "pjsdiv", class_="pjsplplayerscroll"
                    )
                    if season_scroll:
                        season_items = season_scroll.find_all("pjsdiv")
                        for item in season_items:
                            text = item.get_text(strip=True)
                            if text:
                                match = re.search(r"(\d+)", text)
                                if match:
                                    season_numbers.append(int(match.group(1)))

                if season_numbers:
                    film.last_season = str(max(season_numbers))
                else:
                    film.last_season = None

        film.audio_tracks = filtered_audio_tracks

        # Выходим из фрейма для переменных плеера
        driver.switch_to.default_content()
        soup = BeautifulSoup(driver.page_source, "lxml")

        # логика плееров
        player_list = []
        variyt_player_id = None
        player_dropdown = soup.find("div", class_="cinemaplayer-items")
        if player_dropdown:
            items = player_dropdown.find_all("div", class_="cinemaplayer-item-select")
            for item in items:
                raw_url = item.get("data-value", "").strip()
                label = item.get_text(strip=True)

                player_list.append(
                    {
                        "label": label,
                        "url": raw_url,
                    }
                )

                if "api.variyt.ws" in raw_url:
                    try:
                        variyt_player_id = raw_url.rstrip("/").split("/")[-1]
                    except Exception:
                        pass

        film.player_id = int(variyt_player_id) if variyt_player_id else None
        film.player_variables = player_list
        film.last_update = timezone.now()
        film.is_parsed_ru = "parsed"

        # Сохраняем всё в основную базу (managed=False модель это позволяет)
        film.save(
            update_fields=[
                "film_content",
                "add_content_date",
                "audio_tracks",
                "player_id",
                "player_variables",
                "last_season",
                "last_episode",
                "last_update",
                "is_parsed_ru",
            ]
        )

        ScraperLog.objects.create(
            task_name=f"Vavada parser {kp_id}",
            status="success",
            message="Плеер и дорожки обновлены",
        )
        return f"Success: {kp_id}"

    except Exception as exc:
        if driver:
            driver.quit()
        ScraperLog.objects.create(
            task_name=f"Vavada parser {kp_id}", status="error", message=str(exc)
        )
        raise self.retry(exc=exc, countdown=60)
    finally:
        if driver:
            driver.quit()
