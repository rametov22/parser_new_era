"""
Лёгкий парсер обновления уже спаршенных сериалов на iframe.cloud.

Назначение: для сериалов, у которых film_content уже установлен (полный
parse_single_iframe прошёл), периодически обновлять:
  - last_season
  - last_episode
  - audio_tracks

Чтобы видеть новые сезоны/эпизоды по мере их выхода. Полную переразметку
(player_id / player_variables) делает `parse_single_iframe`.

Фильтр кандидатов:
  - is_serial=True
  - film_content IS NOT NULL (плеер уже найден)
  - year_production в окне [current_year - 8, current_year]
  - last_update <= today - 7 дней (давно не обновлялись)
  - is_parsed_ru="parsed" (не пересекается с активным parse_single_iframe)
"""
import re
import logging
from datetime import timedelta

import redis
from bs4 import BeautifulSoup
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from ..models import Content, ScraperLog
from .vavada import create_driver, get_chrome_count


logger = logging.getLogger("vavada_serials")
logger.setLevel(logging.INFO)


VAVADA_QUEUE_NAME = "vavada_queue"
VAVADA_SERIALS_QUEUE_THRESHOLD = 500
VAVADA_SERIALS_BATCH = 200
SERIALS_REFRESH_DAYS = 7
SERIALS_YEAR_WINDOW = 8


def _vavada_queue_length():
    r = redis.Redis(
        host=settings.REDIS_HOST,
        port=int(settings.REDIS_PORT),
        password=settings.REDIS_PASSWORD,
        decode_responses=True,
    )
    return r.llen(VAVADA_QUEUE_NAME)


@shared_task(queue="default")
def spawn_vavada_serials():
    """
    Диспетчер: находит сериалы с уже найденным film_content,
    давно не обновлявшиеся, и кидает в очередь parse_vavada_serial.
    Атомарно обновляет last_update чтобы повторный запуск
    не подхватил те же фильмы.
    """
    queue_len = _vavada_queue_length()
    if queue_len >= VAVADA_SERIALS_QUEUE_THRESHOLD:
        logger.info(
            f"[serials-dispatcher] очередь {queue_len} >= {VAVADA_SERIALS_QUEUE_THRESHOLD}, пропуск"
        )
        return 0

    today = timezone.now().date()
    cut_date = today - timedelta(days=SERIALS_REFRESH_DAYS)
    current_year = today.year
    start_year = current_year - SERIALS_YEAR_WINDOW

    # Берём любой сериал с film_content и давним last_update, не зависим от
    # статуса главного vavada-парсера. Исключаем только in_progress, чтобы
    # не пересекаться с активно парсящимися сейчас.
    kp_ids = list(
        Content.objects.filter(
            is_serial=True,
            film_content__isnull=False,
            year_production__range=(start_year, current_year),
            last_update__lte=cut_date,
        )
        .exclude(is_parsed_ru="in_progress")
        .order_by("last_update")
        .values_list("kino_poisk_id", flat=True)[:VAVADA_SERIALS_BATCH]
    )

    if not kp_ids:
        logger.info("[serials-dispatcher] нет кандидатов")
        return 0

    # Атомарно обновляем last_update — чтобы повторный диспатч не схватил.
    Content.objects.filter(kino_poisk_id__in=kp_ids).update(last_update=timezone.now())

    for kp_id in kp_ids:
        parse_vavada_serial.delay(kp_id)

    logger.info(f"[serials-dispatcher] поставлено в очередь: {len(kp_ids)}")
    return len(kp_ids)


@shared_task(
    bind=True,
    queue="vavada_queue",
    max_retries=2,
    rate_limit="10/m",
    acks_late=True,
    soft_time_limit=120,
    time_limit=150,
)
def parse_vavada_serial(self, kp_id):
    """
    Лёгкий парсинг страницы плеера iframe.cloud для уже-спаршенного сериала.
    Обновляет audio_tracks, last_season, last_episode.
    Не трогает is_parsed_ru / film_content / player_*.
    """
    driver = None
    start_time = timezone.now()
    try:
        film = Content.objects.get(kino_poisk_id=kp_id)
        driver = create_driver()

        url = f"https://iframe.cloud/iframe/{kp_id}"
        driver.get(url)

        try:
            WebDriverWait(driver, 10).until(
                lambda d: (
                    (iframe := d.find_element(By.ID, "playerFrame"))
                    and iframe.get_attribute("src")
                    and not iframe.get_attribute("src").startswith("https://iframe")
                )
            )
        except Exception:
            # Плеер не появился — оставляем last_update свежим (диспатчер
            # уже его обновил), попробуем через 7 дней снова.
            logger.warning(f"[serial] {kp_id} | плеер не найден")
            return f"No player for {kp_id}"

        driver.switch_to.frame(0)
        soup = BeautifulSoup(driver.page_source, "lxml")

        filtered_audio_tracks = []
        last_season = None
        last_episode = None

        track_div = soup.find("div", id="player")
        if track_div:
            # Аудиодорожки
            playlist = track_div.find("pjsdiv", id="player_playlist1")
            if playlist:
                playlist_scroll = playlist.find("pjsdiv", class_="pjsplplayerscroll")
                if playlist_scroll:
                    items = playlist_scroll.find_all("pjsdiv", attrs={"me": True})
                    for i in items:
                        text = i.get_text(strip=True)
                        if text:
                            filtered_audio_tracks.append(text)

            # Эпизоды (последний номер)
            episode_wrapper = track_div.find("pjsdiv", id="player_playlist2")
            if episode_wrapper:
                episode_scroll = episode_wrapper.find(
                    "pjsdiv", class_="pjsplplayerscroll"
                )
                if episode_scroll:
                    episode_items = episode_scroll.find_all("pjsdiv")
                    episode_numbers = []
                    for item in episode_items:
                        text = item.get_text(strip=True)
                        if text:
                            match = re.search(r"(\d+)", text)
                            if match:
                                episode_numbers.append(int(match.group(1)))
                    if episode_numbers:
                        last_episode = str(max(episode_numbers))

            # Сезоны (последний номер)
            season_wrapper = track_div.find("pjsdiv", id="player_playlist3")
            if season_wrapper:
                season_scroll = season_wrapper.find(
                    "pjsdiv", class_="pjsplplayerscroll"
                )
                if season_scroll:
                    season_items = season_scroll.find_all("pjsdiv")
                    season_numbers = []
                    for item in season_items:
                        text = item.get_text(strip=True)
                        if text:
                            match = re.search(r"(\d+)", text)
                            if match:
                                season_numbers.append(int(match.group(1)))
                    if season_numbers:
                        last_season = str(max(season_numbers))

        film.audio_tracks = filtered_audio_tracks
        film.last_season = last_season
        film.last_episode = last_episode
        film.last_update = timezone.now()
        film.save(
            update_fields=[
                "audio_tracks",
                "last_season",
                "last_episode",
                "last_update",
            ]
        )

        exec_time = (timezone.now() - start_time).total_seconds()
        logger.info(
            f"🔄 {kp_id} | {exec_time:.1f}s | Tracks: {len(filtered_audio_tracks)} | "
            f"S:{last_season or 0} E:{last_episode or 0} | Ch:{get_chrome_count()}"
        )

        ScraperLog.objects.create(
            task_name=f"Vavada serial refresh {kp_id}",
            status="success",
            message=f"S:{last_season or 0} E:{last_episode or 0}",
        )
        return kp_id

    except Exception as exc:
        error_msg = str(exc).split("\n")[0][:80]
        logger.error(f"❌ {kp_id} | FAILED | {error_msg}")
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        ScraperLog.objects.create(
            task_name=f"Vavada serial refresh {kp_id}",
            status="error",
            message=str(exc)[:500],
        )
        try:
            raise self.retry(exc=exc, countdown=60)
        except self.MaxRetriesExceededError:
            # Все попытки исчерпаны — last_update уже обновлён диспатчером,
            # следующая попытка через SERIALS_REFRESH_DAYS дней.
            logger.error(f"☠️ {kp_id} | retries исчерпаны")
            raise
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
