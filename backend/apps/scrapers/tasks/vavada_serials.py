"""
Лёгкий парсер обновления уже спаршенных сериалов на iframe.cloud.

Назначение: для сериалов, у которых film_content уже установлен (полный
parse_single_iframe прошёл), периодически обновлять:
  - last_season
  - last_episode
  - audio_tracks
  - last_update_season (только когда last_season/last_episode реально изменились)

Чтобы видеть новые сезоны/эпизоды по мере их выхода. Полную переразметку
(player_id / player_variables) делает `parse_single_iframe`.

Фильтр кандидатов:
  - is_serial=True
  - film_content IS NOT NULL (плеер уже найден)
  - year_production в окне [current_year - 8, current_year]
  - last_update <= today - SERIALS_REFRESH_DAYS (давно не обновлялись)
  - is_parsed_ru != "in_progress" (не пересекается с активным parse_single_iframe)
"""
import re
import time
import logging
from datetime import timedelta

import redis
from bs4 import BeautifulSoup
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, WebDriverException

from ..models import Content, ScraperLog
from ..chrome_utils import (
    create_chrome_driver,
    quit_driver,
    get_chrome_count,
)
from .vavada import report_chrome_heartbeat
from ..release_quality import has_pirated_release
from ..vavada_proxy import acquire_vavada_proxy


logger = logging.getLogger("vavada_serials")
logger.setLevel(logging.INFO)
# Добавляем handler чтобы messages выводились в stdout (для логов celery
# воркера и для синхронного вызова через .run() из shell).
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
    )
    logger.addHandler(_h)
    logger.propagate = False


VAVADA_SERIALS_QUEUE_NAME = "vavada_serials_queue"
VAVADA_SERIALS_QUEUE_THRESHOLD = 1000
VAVADA_SERIALS_BATCH = 200
SERIALS_REFRESH_DAYS = 2
SERIALS_YEAR_WINDOW = 8
EPISODE_LABEL_RE = re.compile(
    r"^\s*(\d+)\s*(?:эпизод(?:а|ов)?|сери(?:я|и|й))\b",
    re.IGNORECASE,
)


def _episode_number(text):
    match = EPISODE_LABEL_RE.search(str(text or ""))
    return int(match.group(1)) if match else None


def _vavada_serials_queue_length():
    r = redis.Redis(
        host=settings.REDIS_HOST,
        port=int(settings.REDIS_PORT),
        password=settings.REDIS_PASSWORD,
        decode_responses=True,
    )
    return r.llen(VAVADA_SERIALS_QUEUE_NAME)


@shared_task(queue="default")
def spawn_vavada_serials():
    """
    Диспетчер: находит сериалы с уже найденным film_content,
    давно не обновлявшиеся, и кидает в очередь parse_vavada_serial.
    Атомарно обновляет last_update чтобы повторный запуск
    не подхватил те же фильмы.
    """
    queue_len = _vavada_serials_queue_length()
    if queue_len >= VAVADA_SERIALS_QUEUE_THRESHOLD:
        logger.info(
            f"[serials-dispatcher] очередь {VAVADA_SERIALS_QUEUE_NAME}: "
            f"{queue_len} >= {VAVADA_SERIALS_QUEUE_THRESHOLD}, пропуск"
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
            # year_production__range=(start_year, current_year),
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
    queue="vavada_serials_queue",
    max_retries=2,
    rate_limit=settings.VAVADA_SERIALS_TASK_RATE_LIMIT,
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
    proxy_lease = None
    start_time = timezone.now()
    try:
        film = Content.objects.get(kino_poisk_id=kp_id)
        proxy_lease = acquire_vavada_proxy(f"vavada-serial:{kp_id}")
        proxy_url = proxy_lease.url if proxy_lease else None
        driver = create_chrome_driver(
            stealth=True,
            proxy_url=proxy_url,
        )

        url = f"https://iframe.cloud/iframe/{kp_id}"
        try:
            driver.get(url)
        except TimeoutException:
            logger.warning(
                f"[serial] {kp_id} | timeout загрузки страницы, пересоздаём драйвер"
            )
            quit_driver(driver)
            if proxy_lease:
                proxy_lease.release(failed=True)
            proxy_lease = acquire_vavada_proxy(f"vavada-serial-retry:{kp_id}")
            proxy_url = proxy_lease.url if proxy_lease else None
            driver = create_chrome_driver(
                stealth=True,
                proxy_url=proxy_url,
            )
            try:
                driver.get(url)
            except TimeoutException:
                logger.warning(
                    f"[serial] {kp_id} | повторный timeout загрузки, пропускаем"
                )
                if proxy_lease:
                    proxy_lease.release(failed=True)
                return f"No player for {kp_id} (page timeout)"

        def _ready_player_frame(current_driver):
            frame = current_driver.find_element(By.ID, "playerFrame")
            src = frame.get_attribute("src") or ""
            return frame if src and not src.startswith("https://iframe") else False

        try:
            player_frame = WebDriverWait(driver, 10).until(_ready_player_frame)
        except Exception:
            logger.warning(f"[serial] {kp_id} | плеер не найден")
            return f"No player for {kp_id}"

        logger.info(
            f"[serial-debug] {kp_id} | switching to playerFrame: "
            f"{player_frame.get_attribute('src')}"
        )
        driver.switch_to.frame(player_frame)
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
        frame_details = [
            {
                "id": frame.get_attribute("id"),
                "src": frame.get_attribute("src"),
            }
            for frame in driver.find_elements(By.TAG_NAME, "iframe")
        ]
        logger.info(
            f"[serial-debug] {kp_id} | player document: "
            f"url={driver.current_url!r} title={driver.title!r} "
            f"html={len(driver.page_source)} waf_challenge={waf_challenge} "
            f"iframes={frame_details!r}"
        )
        player_loaded = False
        in_visual = False
        deadline = time.time() + 60
        last_log = 0
        while time.time() < deadline:
            try:
                pjs_top = len(driver.find_elements(By.CSS_SELECTOR, "pjsdiv"))
            except Exception:
                pjs_top = 0

            if pjs_top > 0:
                player_loaded = True
                in_visual = False
                break

            pjs_vis = 0
            try:
                visual = driver.find_element(By.ID, "visual")
                driver.switch_to.frame(visual)
                in_visual = True
                pjs_vis = len(driver.find_elements(By.CSS_SELECTOR, "pjsdiv"))
                if pjs_vis > 0:
                    player_loaded = True
                    break
                driver.switch_to.parent_frame()
                in_visual = False
            except Exception:
                pass

            if time.time() - last_log > 10:
                logger.info(
                    f"[serial-debug] {kp_id} | waiting player: "
                    f"pjs_top={pjs_top} pjs_visual={pjs_vis}"
                )
                last_log = time.time()
            time.sleep(1)

        if not player_loaded:
            logger.warning(
                f"[serial] {kp_id} | плеер не появился за 60 сек "
                f"(in_visual={in_visual})"
            )
            return f"No player UI for {kp_id}"

        logger.info(
            f"[serial-debug] {kp_id} | плеер найден в "
            f"{'visual iframe' if in_visual else 'playerFrame'}"
        )
        time.sleep(3)
        soup = BeautifulSoup(driver.page_source, "lxml")

        filtered_audio_tracks = []
        last_season = None
        last_episode = None

        track_div = soup.find("div", id="player")
        logger.info(
            f"[serial-debug] {kp_id} | page_source len={len(driver.page_source)} | "
            f"track_div={track_div is not None}"
        )
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

        def _read_items(playlist_id):
            try:
                return driver.execute_script(
                    """
                    const wrap = document.querySelector('#' + arguments[0] + ' .pjsplplayerscroll');
                    if (!wrap) return [];
                    return Array.from(wrap.children)
                        .filter(el => el.hasAttribute('me'))
                        .map(el => (el.innerText || el.textContent || '').trim());
                    """,
                    playlist_id,
                ) or []
            except Exception as e:
                logger.warning(f"[serial] {kp_id} | _read_items({playlist_id}): {e}")
                return []

        def _click_item(playlist_id, idx):
            try:
                return driver.execute_script(
                    """
                    const wrap = document.querySelector('#' + arguments[0] + ' .pjsplplayerscroll');
                    if (!wrap) return null;
                    const items = Array.from(wrap.children).filter(el => el.hasAttribute('me'));
                    const idx = arguments[1];
                    if (items.length <= idx) return null;
                    const el = items[idx];
                    ['mousedown', 'mouseup', 'click'].forEach(evt => {
                        el.dispatchEvent(new MouseEvent(evt, {bubbles: true, cancelable: true, view: window}));
                    });
                    return (el.innerText || el.textContent || '').trim();
                    """,
                    playlist_id,
                    idx,
                )
            except Exception as e:
                logger.warning(f"[serial] {kp_id} | _click({playlist_id}, {idx}): {e}")
                return None

        def _max_number(items):
            nums = []
            for idx, text in enumerate(items):
                if text:
                    m = re.search(r"(\d+)", text)
                    if m:
                        nums.append((idx, int(m.group(1))))
            if not nums:
                return None, None
            return max(nums, key=lambda x: x[1])

        audio_items_text = _read_items("player_playlist1")
        filtered_audio_tracks = [t for t in audio_items_text if t]
        print(
            f"[serial-print] {kp_id} raw audio_tracks={filtered_audio_tracks!r}",
            flush=True,
        )
        logger.info(
            f"[serial-debug] {kp_id} | audio tracks: {len(filtered_audio_tracks)}"
        )

        season_items = _read_items("player_playlist3")
        season_idx, max_season = _max_number(season_items)
        if max_season is not None:
            last_season = str(max_season)
        logger.info(
            f"[serial-debug] {kp_id} | seasons: {season_items} | "
            f"last_season={last_season}"
        )

        if season_idx is not None and season_idx > 0:
            clicked = _click_item("player_playlist3", season_idx)
            logger.info(f"[serial-debug] {kp_id} | clicked season: {clicked!r}")
            time.sleep(4)

        def _only_episodes(items):
            return [t for t in items if _episode_number(t) is not None]

        ep_items_1 = _only_episodes(_read_items("player_playlist1"))
        ep_items_2 = _only_episodes(_read_items("player_playlist2"))
        max_ep_1 = max((_episode_number(t) for t in ep_items_1), default=None)
        max_ep_2 = max((_episode_number(t) for t in ep_items_2), default=None)
        max_ep = max(
            (n for n in (max_ep_1, max_ep_2) if n is not None),
            default=None,
        )
        if max_ep is not None:
            last_episode = str(max_ep)
        logger.info(
            f"[serial-debug] {kp_id} | episodes pl1={ep_items_1} pl2={ep_items_2} | "
            f"last_episode={last_episode}"
        )

        old_last_season = film.last_season
        old_last_episode = film.last_episode
        new_last_season = int(last_season) if last_season is not None else old_last_season
        new_last_episode = int(last_episode) if last_episode is not None else old_last_episode
        print(
            f"[serial-print] {kp_id} raw last_season={last_season!r} "
            f"raw last_episode={last_episode!r}",
            flush=True,
        )
        print(
            f"[serial-print] {kp_id} db old S:{old_last_season} E:{old_last_episode} "
            f"-> new S:{new_last_season} E:{new_last_episode}",
            flush=True,
        )
        season_changed = (
            new_last_season != old_last_season
            or new_last_episode != old_last_episode
        )

        film.audio_tracks = filtered_audio_tracks
        film.is_pirated = has_pirated_release(
            filtered_audio_tracks,
        )
        film.last_season = new_last_season
        film.last_episode = new_last_episode
        film.last_update = timezone.now()
        update_fields = [
            "audio_tracks",
            "is_pirated",
            "last_season",
            "last_episode",
            "last_update",
        ]
        if season_changed:
            film.last_update_season = timezone.now().date()
            update_fields.append("last_update_season")
        film.save(
            update_fields=update_fields
        )

        exec_time = (timezone.now() - start_time).total_seconds()
        logger.info(
            f"🔄 {kp_id} | {exec_time:.1f}s | Tracks: {len(filtered_audio_tracks)} | "
            f"S:{last_season or 0} E:{last_episode or 0} | Ch:{get_chrome_count()}"
        )

        ScraperLog.objects.create(
            task_name=f"Vavada serial refresh {kp_id}",
            status="success",
            message=(
                f"S:{film.last_season or 0} E:{film.last_episode or 0} "
                f"changed={season_changed}"
            ),
        )
        return kp_id

    except Exception as exc:
        error_msg = str(exc).split("\n")[0][:80]
        logger.error(f"❌ {kp_id} | FAILED | {error_msg}")
        if driver:
            quit_driver(driver)
        if proxy_lease:
            proxy_lease.release(failed=True)
        ScraperLog.objects.create(
            task_name=f"Vavada serial refresh {kp_id}",
            status="error",
            message=str(exc)[:500],
        )
        try:
            raise self.retry(exc=exc, countdown=60)
        except self.MaxRetriesExceededError:
            logger.error(f"☠️ {kp_id} | retries исчерпаны")
            raise
    finally:
        if driver:
            quit_driver(driver)
        if proxy_lease:
            proxy_lease.release()
        report_chrome_heartbeat("vavada_serials")
