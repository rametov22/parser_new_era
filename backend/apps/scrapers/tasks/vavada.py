import re
import logging
import time
from django.conf import settings
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db.models import Q
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup

from ..models import Content, ScraperLog
from ..chrome_utils import (
    create_chrome_driver,
    quit_driver,
    get_chrome_count,
)
from ..release_quality import has_pirated_release
from ..vavada_proxy import acquire_vavada_proxy


logging.getLogger("selenium").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger("vavada_parser")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)


def report_chrome_heartbeat(label):
    """
    Пишет в Redis число живых Chrome-процессов воркера + метку времени —
    для операционного дашборда. Дашборд (backend-контейнер) не видит процессы
    воркеров через psutil, поэтому каждый воркер отчитывается сам.

    Полностью защищено: любые ошибки глотаются, парсинг не роняется.
    Ключ chrome_health:<label> живёт 1ч; устаревший ключ = воркер встал.
    """
    try:
        import json
        import redis

        r = redis.Redis(
            host=settings.REDIS_HOST,
            port=int(settings.REDIS_PORT),
            password=settings.REDIS_PASSWORD,
            decode_responses=True,
        )
        r.set(
            f"chrome_health:{label}",
            json.dumps(
                {"chrome": get_chrome_count(), "ts": timezone.now().isoformat()}
            ),
            ex=3600,
        )
    except Exception:
        pass


VAVADA_QUEUE_NAME = "vavada_queue"
VAVADA_QUEUE_THRESHOLD = 500
VAVADA_IN_PROGRESS_STUCK_MINUTES = 30
VAVADA_NO_PLAYER_RETRY_HOURS = 4
VAVADA_PIRATED_RECHECK_HOURS = 24


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
        не чаще раза в VAVADA_NO_PLAYER_RETRY_HOURS, если уже проверяли;
      - ИЛИ is_parsed_ru="parsed" + is_serial=True (сериалам нужно обновлять
        last_episode/last_season/audio_tracks по мере выхода новых серий)
        и last_update <= today - 4 дня.
    Плюс окно премьеры PREMIERE дней.
    """
    queue_len = _vavada_queue_length()
    if queue_len >= VAVADA_QUEUE_THRESHOLD:
        logger.info(
            f"[vavada-dispatcher] очередь {VAVADA_QUEUE_NAME}: {queue_len} задач "
            f">= {VAVADA_QUEUE_THRESHOLD}, пропускаем тик"
        )
        return 0

    now = timezone.now()
    today = now.date()
    start_date = today - timedelta(days=settings.PREMIERE)
    cut_date = today - timedelta(days=4)
    retry_after = now - timedelta(hours=VAVADA_NO_PLAYER_RETRY_HOURS)

    date_filter = Q(premiere__range=(start_date, today)) | Q(
        premiere_ru__range=(start_date, today)
    )

    not_parsed_ready = Q(is_parsed_ru="not_parsed") & (
        Q(parsed_at_ru__isnull=True) | Q(parsed_at_ru__lte=retry_after)
    )
    serial_refresh_ready = (
        Q(is_parsed_ru="parsed") & Q(is_serial=True) & Q(last_update__lte=cut_date)
    )
    status_filter = not_parsed_ready | serial_refresh_ready
    batch_size = max(VAVADA_QUEUE_THRESHOLD - queue_len, 0)

    kp_ids = list(
        Content.objects.filter(status_filter)
        .filter(date_filter)
        .order_by("parsed_at_ru", "last_update", "id")
        .values_list("kino_poisk_id", flat=True)[:batch_size]
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

    logger.info(
        f"[vavada-dispatcher] поставлено в очередь: {len(kp_ids)} "
        f"(queue_before={queue_len}, batch_size={batch_size})"
    )
    return len(kp_ids)


@shared_task(queue="default")
def spawn_pirated_rechecks(limit=100, min_age_hours=VAVADA_PIRATED_RECHECK_HOURS):
    """Перекинуть TS/CAMRip/HDCAM релизы на повторный Vavada-парсинг.

    Когда Vavada заменит театральный релиз на нормальное качество,
    parse_single_iframe пересчитает is_pirated=False.
    """
    queue_len = _vavada_queue_length()
    if queue_len >= VAVADA_QUEUE_THRESHOLD:
        logger.info(
            f"[vavada-pirated-recheck] очередь {VAVADA_QUEUE_NAME}: {queue_len} задач "
            f">= {VAVADA_QUEUE_THRESHOLD}, пропускаем тик"
        )
        return 0

    batch_size = min(max(int(limit or 0), 0), VAVADA_QUEUE_THRESHOLD - queue_len)
    if batch_size <= 0:
        return 0

    retry_after = timezone.now() - timedelta(hours=int(min_age_hours or 0))
    kp_ids = list(
        Content.objects.filter(is_pirated=True)
        .exclude(is_parsed_ru="in_progress")
        .filter(Q(parsed_at_ru__isnull=True) | Q(parsed_at_ru__lte=retry_after))
        .order_by("parsed_at_ru", "id")
        .values_list("kino_poisk_id", flat=True)[:batch_size]
    )
    if not kp_ids:
        logger.info("[vavada-pirated-recheck] нет кандидатов")
        return 0

    Content.objects.filter(kino_poisk_id__in=kp_ids).update(
        is_parsed_ru="in_progress",
        parsed_at_ru=timezone.now(),
    )
    for kp_id in kp_ids:
        parse_single_iframe.delay(kp_id)

    logger.info(
        f"[vavada-pirated-recheck] поставлено в очередь: {len(kp_ids)} "
        f"(queue_before={queue_len})"
    )
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
    rate_limit=settings.VAVADA_TASK_RATE_LIMIT,
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
    proxy_lease = None
    start_time = timezone.now()
    try:
        film = Content.objects.get(kino_poisk_id=kp_id)
        old_last_season = film.last_season
        old_last_episode = film.last_episode

        # Перезахватываем in_progress на момент РЕАЛЬНОГО старта обработки
        # (а не постановки в очередь). При длинной очереди фильм ждёт дольше
        # IN_PROGRESS_STUCK_MINUTES, expire успевает сбросить его в not_parsed,
        # и финальный атомарный UPDATE filter(is_parsed_ru="in_progress") не
        # находит строку → parsed/parse_count не фиксируются, фильм парсится
        # повторно. Свежий parsed_at_ru рестартит таймер expire от старта.
        Content.objects.filter(pk=film.pk).update(
            is_parsed_ru="in_progress", parsed_at_ru=timezone.now()
        )

        proxy_lease = acquire_vavada_proxy(f"vavada:{kp_id}")
        proxy_url = proxy_lease.url if proxy_lease else None
        driver = create_chrome_driver(
            stealth=True,
            proxy_url=proxy_url,
        )

        # Логика из check_and_pars_iframe
        url = f"https://iframe.cloud/iframe/{kp_id}"
        try:
            driver.get(url)
        except TimeoutException:
            # Renderer завис при загрузке страницы. Пересоздаём драйвер
            # и пробуем один раз — часто помогает при временной нагрузке.
            logger.warning(
                f"[vavada] {kp_id} | timeout загрузки страницы, пересоздаём драйвер"
            )
            quit_driver(driver)
            if proxy_lease:
                proxy_lease.release(failed=True)
            proxy_lease = acquire_vavada_proxy(f"vavada-retry:{kp_id}")
            proxy_url = proxy_lease.url if proxy_lease else None
            driver = create_chrome_driver(
                stealth=True,
                proxy_url=proxy_url,
            )
            try:
                driver.get(url)
            except TimeoutException:
                logger.warning(
                    f"[vavada] {kp_id} | повторный timeout загрузки, пропускаем"
                )
                film.is_parsed_ru = "not_parsed"
                film.parsed_at_ru = timezone.now()
                film.save(update_fields=["is_parsed_ru", "parsed_at_ru"])
                if proxy_lease:
                    proxy_lease.release(failed=True)
                ScraperLog.objects.create(
                    task_name=f"Vavada parser {kp_id}",
                    status="success",
                    message="Плеер пока не найден: page timeout",
                )
                return f"No player found for {kp_id} (page timeout)"

        # Ожидание фрейма
        def _ready_player_frame(current_driver):
            frame = current_driver.find_element(By.ID, "playerFrame")
            src = frame.get_attribute("src") or ""
            return frame if src and not src.startswith("https://iframe") else False

        try:
            player_frame = WebDriverWait(driver, 10).until(_ready_player_frame)
        except Exception:
            # Плеер ещё не появился. Сбрасываем статус и фиксируем время
            # проверки в parsed_at_ru: диспетчер вернёт фильм в работу
            # не раньше чем через VAVADA_NO_PLAYER_RETRY_HOURS.
            checked_at = timezone.now()
            film.is_parsed_ru = "not_parsed"
            film.parsed_at_ru = checked_at
            film.save(update_fields=["is_parsed_ru", "parsed_at_ru"])
            ScraperLog.objects.create(
                task_name=f"Vavada parser {kp_id}",
                status="success",
                message="Плеер пока не найден",
            )
            return f"No player found for {kp_id}"

        # Сохраняем основные данные
        film.film_content = f"https://iframe.cloud/iframe/{kp_id}"
        # https://vavada.video/iframe/
        film.add_content_date = timezone.now().date()

        # Переключаемся во фрейм для аудиодорожек
        logger.info(
            f"[vavada-debug] {kp_id} | switching to playerFrame: "
            f"{player_frame.get_attribute('src')}"
        )
        driver.switch_to.frame(player_frame)
        try:
            WebDriverWait(driver, 60).until(
                lambda current_driver: current_driver.find_elements(
                    By.CSS_SELECTOR, "pjsdiv"
                )
            )
        except TimeoutException:
            source_lower = driver.page_source.lower()
            waf_challenge = any(
                marker in source_lower
                for marker in (
                    "cb-container",
                    "wsdk.js",
                    "verification browser",
                    "верификация браузера",
                )
            )
            logger.warning(
                f"[vavada] {kp_id} | player UI timeout "
                f"(waf_challenge={waf_challenge})"
            )
            checked_at = timezone.now()
            film.is_parsed_ru = "not_parsed"
            film.parsed_at_ru = checked_at
            film.save(update_fields=["is_parsed_ru", "parsed_at_ru"])
            ScraperLog.objects.create(
                task_name=f"Vavada parser {kp_id}",
                status="success",
                message=f"Player UI unavailable; waf_challenge={waf_challenge}",
            )
            return f"No player UI for {kp_id}"

        time.sleep(3)
        soup = BeautifulSoup(driver.page_source, "lxml")

        # логика поиска дорожек
        filtered_audio_tracks = []
        parsed_last_episode = None
        parsed_last_season = None
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
                    parsed_last_episode = max(episode_numbers)

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
                    parsed_last_season = max(season_numbers)

                new_last_season = (
                    parsed_last_season
                    if parsed_last_season is not None
                    else old_last_season
                )
                new_last_episode = (
                    parsed_last_episode
                    if parsed_last_episode is not None
                    else old_last_episode
                )
                season_changed = (
                    new_last_season != old_last_season
                    or new_last_episode != old_last_episode
                )
                film.last_season = new_last_season
                film.last_episode = new_last_episode
                if season_changed:
                    film.last_update_season = timezone.now().date()
            else:
                season_changed = False
        else:
            season_changed = False

        # Сохраняем audio_tracks только если плеер вернул данные.
        # Если бот-чекер заблокировал страницу — не затираем существующие треки.
        if filtered_audio_tracks:
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

        # Сначала сохраняем все парсенные поля БЕЗ смены статуса,
        # чтобы атомарный финал проверил исходный статус "in_progress".
        film.player_id = int(variyt_player_id) if variyt_player_id else None
        film.player_variables = player_list
        film.is_pirated = has_pirated_release(
            filtered_audio_tracks or film.audio_tracks,
        )
        film.last_update = timezone.now()
        film.save(
            update_fields=[
                "film_content",
                "add_content_date",
                "audio_tracks",
                "player_id",
                "player_variables",
                "is_pirated",
                "last_season",
                "last_episode",
                "last_update",
            ]
            + (["last_update_season"] if season_changed else [])
        )

        # Атомарный финал: переводим в parsed + инкрементируем счётчик
        # ТОЛЬКО если статус ещё in_progress. При redelivery второй воркер
        # увидит уже "parsed" → UPDATE затронет 0 строк, дубля не будет.
        from django.db.models import F as _F
        Content.objects.filter(pk=film.pk, is_parsed_ru="in_progress").update(
            is_parsed_ru="parsed",
            parsed_at_ru=timezone.now(),
            parse_count_ru=_F("parse_count_ru") + 1,
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
            quit_driver(driver)
        if proxy_lease:
            proxy_lease.release(failed=True)
        ScraperLog.objects.create(
            task_name=f"Vavada parser {kp_id}", status="error", message=str(exc)
        )
        try:
            raise self.retry(exc=exc, countdown=60)
        except self.MaxRetriesExceededError:
            checked_at = timezone.now()
            Content.objects.filter(kino_poisk_id=kp_id).update(
                is_parsed_ru="not_parsed",
                parsed_at_ru=checked_at,
            )
            logger.error(f"☠️  {kp_id} | retries исчерпаны, статус сброшен в not_parsed")
            raise
    finally:
        if driver:
            quit_driver(driver)
        if proxy_lease:
            proxy_lease.release()
        report_chrome_heartbeat("vavada")
