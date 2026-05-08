import re
import logging
import psutil
from django.conf import settings
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

from ..models import Content, ScraperLog


logging.getLogger("selenium").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger("vavada_parser")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)


def get_chrome_count():
    """Считает активные процессы Chrome для контроля ресурсов"""
    return sum(
        1
        for proc in psutil.process_iter(["name"])
        if proc.info["name"] and "chrome" in proc.info["name"].lower()
    )


def _kill_zombie_chrome():
    """Убивает осиротевшие chromedriver/chrome процессы (PPID=1 = zombie)."""
    for proc in psutil.process_iter(["pid", "ppid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "chromedriver" in name or "chrome" in name:
                if proc.info.get("ppid") == 1:
                    proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def create_driver():
    """Создает драйвер, подключаясь к удаленному браузеру или локальному"""

    _kill_zombie_chrome()

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

    driver.set_page_load_timeout(45)
    driver.set_script_timeout(30)

    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        },
    )

    return driver


VAVADA_QUEUE_NAME = "vavada_queue"
VAVADA_QUEUE_THRESHOLD = 500
VAVADA_IN_PROGRESS_STUCK_MINUTES = 30


def _vavada_queue_length():
    import redis

    r = redis.Redis(
        host=settings.REDIS_HOST,
        port=int(settings.REDIS_PORT),
        password=settings.REDIS_PASSWORD,
        decode_responses=True,
    )
    return r.llen(VAVADA_QUEUE_NAME)


@shared_task()
def spawn_iframe_parsers():
    """
    Диспетчер: находит премьерные фильмы для парсинга vavada
    и кидает их в очередь.

    Берём:
      - is_parsed_ru="not_parsed" (новые / неудачные)
      - ИЛИ is_parsed_ru="parsed" + is_serial=True (сериалам нужно обновлять
        last_episode/last_season/audio_tracks по мере выхода новых серий)
    Плюс окно last_update <= today - 4 дня (не дёргаем чаще 4 дней).
    Плюс окно премьеры PREMIERE дней.
    """
    queue_len = _vavada_queue_length()
    if queue_len >= VAVADA_QUEUE_THRESHOLD:
        logger.info(
            f"[vavada-dispatcher] очередь {VAVADA_QUEUE_NAME}: {queue_len} задач "
            f">= {VAVADA_QUEUE_THRESHOLD}, пропускаем тик"
        )
        return 0

    today = timezone.now().date()
    start_date = today - timedelta(days=settings.PREMIERE)
    cut_date = today - timedelta(days=4)

    date_filter = Q(premiere__range=(start_date, today)) | Q(
        premiere_ru__range=(start_date, today)
    )

    status_filter = Q(is_parsed_ru="not_parsed") | (
        Q(is_parsed_ru="parsed") & Q(is_serial=True)
    )

    kp_ids = list(
        Content.objects.filter(status_filter)
        .filter(last_update__lte=cut_date)
        .filter(date_filter)
        .values_list("kino_poisk_id", flat=True)
    )

    if not kp_ids:
        logger.info("[vavada-dispatcher] нет кандидатов")
        return 0

    # Атомарный захват: помечаем in_progress + запоминаем время захвата
    # в parsed_at_ru — чтобы expire_stuck_vavada_task мог отличить
    # «давно зависшие» от «только что взятых».
    Content.objects.filter(kino_poisk_id__in=kp_ids).update(
        is_parsed_ru="in_progress",
        parsed_at_ru=timezone.now(),
    )

    for kp_id in kp_ids:
        parse_single_iframe.delay(kp_id)

    logger.info(f"[vavada-dispatcher] поставлено в очередь: {len(kp_ids)}")
    return len(kp_ids)


@shared_task(queue="default")
def expire_stuck_vavada_task():
    """
    Сбрасывает в not_parsed записи vavada, зависшие в in_progress дольше
    VAVADA_IN_PROGRESS_STUCK_MINUTES минут (например, при OOM-killer
    или жёсткой смерти воркера, когда except не успел отработать).
    """
    threshold = timezone.now() - timedelta(minutes=VAVADA_IN_PROGRESS_STUCK_MINUTES)
    stuck = Content.objects.filter(
        Q(is_parsed_ru="in_progress")
        & (Q(parsed_at_ru__lt=threshold) | Q(parsed_at_ru__isnull=True))
    ).update(is_parsed_ru="not_parsed")
    logger.info(f"[vavada-expire] зависших in_progress сброшено: {stuck}")
    return stuck


# concurrency 3
@shared_task(
    bind=True,
    queue="vavada_queue",
    max_retries=3,
    rate_limit="10/m",
    acks_late=True,
    soft_time_limit=180,
    time_limit=210,
)
def parse_single_iframe(self, kp_id):
    """Парсинг одного конкретного фильма через Selenium"""
    # Идемпотентность: если задача доставлена повторно (acks_late + рестарт
    # воркера), пропускаем, чтобы не накручивать parse_count_ru.
    existing = Content.objects.filter(kino_poisk_id=kp_id).only(
        "is_parsed_ru", "parsed_at_ru"
    ).first()
    if (
        existing
        and existing.is_parsed_ru == "parsed"
        and existing.parsed_at_ru
        and (timezone.now() - existing.parsed_at_ru) < timedelta(minutes=5)
    ):
        return f"Skipped {kp_id} (recently parsed)"

    driver = None
    start_time = timezone.now()
    try:
        film = Content.objects.get(kino_poisk_id=kp_id)
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
            # Плеер ещё не появился. Сбрасываем статус и ставим last_update
            # на ~4 часа назад, чтобы фильм снова попал в окно через 4 часа,
            # а не через 4 дня.
            film.is_parsed_ru = "not_parsed"
            film.last_update = (timezone.now() - timedelta(days=3, hours=20)).date()
            film.save(update_fields=["is_parsed_ru", "last_update"])
            return f"No player found for {kp_id}"

        # Сохраняем основные данные
        film.film_content = f"https://iframe.cloud/iframe/{kp_id}"
        # https://vavada.video/iframe/
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
        film.parsed_at_ru = timezone.now()

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
                "parsed_at_ru",
            ]
        )

        # Инкрементируем счётчик циклов парсинга vavada атомарно
        from django.db.models import F as _F
        Content.objects.filter(pk=film.pk).update(
            parse_count_ru=_F("parse_count_ru") + 1
        )

        exec_time = (timezone.now() - start_time).total_seconds()
        logger.info(
            f"✅ {kp_id} | {exec_time}s | Tracks: {len(filtered_audio_tracks)} | "
            f"S:{film.last_season or 0} E:{film.last_episode or 0} | IDs: {film.player_id} | Ch:{get_chrome_count()}"
        )

        ScraperLog.objects.create(
            task_name=f"Vavada parser {kp_id}",
            status="success",
            message="Плеер и дорожки обновлены",
        )
        return kp_id

    except Exception as exc:
        error_msg = str(exc).split("\n")[0][:50]
        logger.error(f"❌ {kp_id} | FAILED | Error: {error_msg}")

        if driver:
            driver.quit()
        ScraperLog.objects.create(
            task_name=f"Vavada parser {kp_id}", status="error", message=str(exc)
        )
        try:
            raise self.retry(exc=exc, countdown=60)
        except self.MaxRetriesExceededError:
            # Все ретраи исчерпаны — сбрасываем статус, чтобы диспетчер
            # подхватил фильм снова при следующем запуске.
            Content.objects.filter(kino_poisk_id=kp_id).update(
                is_parsed_ru="not_parsed",
                last_update=(timezone.now() - timedelta(days=3, hours=20)).date(),
            )
            logger.error(f"☠️  {kp_id} | retries исчерпаны, статус сброшен в not_parsed")
            raise
    finally:
        if driver:
            driver.quit()
