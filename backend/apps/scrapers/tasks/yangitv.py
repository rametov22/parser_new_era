"""
Парсер yangi.tv (Yangi TV) — узбекский видео-сервис.

Логика:
  1. collect_all_ids       — собирает все content_id с API yangi.tv
                              в YtConnectContent (статус not_parsed).
  2. spawn_yt_connect      — диспетчер: батчем берёт not_parsed,
                              для каждой запускает parse_yt_connect.
  3. parse_yt_connect      — для одного content_id берёт детали
                              (name_ru/year), ищет совпадение в Content,
                              заполняет name_uz/description_uz/poster_uz/
                              film_content_uz/age_restriction.
  4. spawn_yt_movie_urls   — диспетчер: берёт parsed, у которых
                              parsing_status_player='not_parsed', ставит
                              в очередь parse_yt_movie_url.
  5. parse_yt_movie_url    — для одного content_id запрашивает API
                              getMovieUrl, расшифровывает AES-CBC, кладёт
                              ссылки в YtConnectContent.content_url
                              и в Content.film_content_uz.
"""
import base64
import logging
import re
import time
from datetime import timedelta

import requests
from celery import shared_task
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from decouple import config
from django.db.models import F
from django.db.models.functions import Lower, Trim
from django.utils import timezone

from ..models import YtConnectContent, ScraperLog, Content
from ..utils import parse_age, download_image_to_field


logger = logging.getLogger("yangitv_parser")
logger.setLevel(logging.INFO)


# === Конфиг (вынесено в .env) ===
YT_API_BASE = "https://admin.yangi.tv/api/v1"
YT_BEARER_TOKEN = config(
    "YT_BEARER_TOKEN",
    default="177270|r1Sy3xfJltnuQmSX8HGnoxbYsJ3BlKvzHbSiGJxK16f9df4a",
)
# AES-ключи извлечены из официального приложения yangi.tv (Frida)
YT_AES_KEY = config(
    "YT_AES_KEY", default="op1PU19Y2JoWcj0CwKwgYTtKh8OlrR3O"
).encode("utf-8")
YT_AES_IV = bytes.fromhex(
    config("YT_AES_IV_HEX", default="596633736a567a6d694c674157383361")
)

# Размер батча для диспетчеров — небольшой, чтобы не давить API
CONNECT_BATCH = 20
MOVIE_URL_BATCH = 20

# Тайминги — щадящий режим, не торопимся
HTTP_TIMEOUT = 60  # на медленные ответы API
PAGE_SLEEP = 5  # пауза между страницами в collect_all_ids

# После N неудачных попыток фильм помечается failed и пропускается.
MAX_FAIL_ATTEMPTS = 5

# Recovery — сбрасывать застрявший in_progress старше N минут.
IN_PROGRESS_STUCK_MINUTES = 30


def _headers():
    return {
        "User-Agent": "okhttp/5.1.0",
        "Authorization": f"Bearer {YT_BEARER_TOKEN}",
        "Accept": "application/json",
    }


def _record_yt_failure(content_id, phase, message):
    """
    Инкрементирует счётчик ошибок и помечает failed при превышении лимита.
    phase: "connect" | "player"
    """
    if phase == "connect":
        count_field = "connect_fail_count"
        status_field = "parsing_status"
    else:
        count_field = "player_fail_count"
        status_field = "parsing_status_player"

    YtConnectContent.objects.filter(content_id=content_id).update(
        **{count_field: F(count_field) + 1}
    )
    rec = YtConnectContent.objects.filter(content_id=content_id).only(count_field).first()
    fail_count = getattr(rec, count_field, 0) if rec else 0

    if fail_count >= MAX_FAIL_ATTEMPTS:
        YtConnectContent.objects.filter(content_id=content_id).update(
            **{status_field: "failed"}
        )
        logger.error(
            f"☠️ {phase}: {content_id} помечен failed после {fail_count} попыток"
        )
    else:
        YtConnectContent.objects.filter(content_id=content_id).update(
            **{status_field: "not_parsed"}
        )

    ScraperLog.objects.create(
        task_name=f"YT {phase} {content_id}",
        status="error",
        message=f"[attempt {fail_count}] {message[:480]}",
    )


def _manual_unpad(data: bytes) -> bytes:
    """Снимает PKCS7-паддинг, не падает если паддинга нет."""
    if not data:
        return b""
    padding_len = data[-1]
    if padding_len == 0 or padding_len > len(data):
        return data
    if data[-padding_len:] != bytes([padding_len]) * padding_len:
        return data
    return data[:-padding_len]


def _decrypt_chunks(parts) -> str | None:
    """
    Дешифрует массив base64-зашифрованных кусков AES-CBC.
    Каждый кусок имеет свой PKCS7-паддинг (снимаем до склейки).
    Возвращает строку URL или None.
    """
    if not isinstance(parts, list) or not parts:
        return None
    decrypted_parts = []
    for part in parts:
        encrypted_chunk = base64.b64decode(part)
        cipher = Cipher(algorithms.AES(YT_AES_KEY), modes.CBC(YT_AES_IV))
        decryptor = cipher.decryptor()
        chunk = decryptor.update(encrypted_chunk) + decryptor.finalize()
        chunk = _manual_unpad(chunk)
        decrypted_parts.append(chunk)
    full = b"".join(decrypted_parts)
    url = full.decode("utf-8", errors="ignore").strip()
    return url or None


def _parse_episode_name(name: str):
    """
    Из 'N-qism Mp' → (episode_number, quality_str).
    Примеры:
      '8-qism 1080p' → (8, '1080p'); '7-qism 720p' → (7, '720p');
      '8-qism (oxirgi)' → (8, None);  '5-qism' → (5, None).
    Если качества нет в имени — вернём None, потом определим из URL.
    """
    if not name:
        return None, None
    ep_match = re.search(r"(\d+)\s*-\s*qism", name, re.IGNORECASE)
    q_match = re.search(r"(\d+)\s*p", name, re.IGNORECASE)
    ep = int(ep_match.group(1)) if ep_match else None
    quality = f"{q_match.group(1)}p" if q_match else None
    return ep, quality


def _detect_quality_from_url(url: str) -> str:
    """Если в URL встречается '480p'/'720p'/'1080p' — вернёт; иначе 'default'."""
    if not url:
        return "default"
    m = re.search(r"(2160|1440|1080|720|480|360)p", url, re.IGNORECASE)
    return f"{m.group(1)}p" if m else "default"


def _parse_season_name(name: str):
    """Из 'N-fasl' извлекает номер сезона."""
    if not name:
        return None
    match = re.search(r"(\d+)\s*-\s*fasl", name, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _decrypt_film_urls(api_data) -> dict:
    """
    Фильм: api_data = {'480A': [chunks], '720A': [chunks], '1080A': [chunks], ...}.
    Возвращает {'480p': 'url', '720p': 'url', '1080p': 'url'}.
    """
    if not isinstance(api_data, dict):
        return {}
    result = {}
    for quality, encrypted_parts in api_data.items():
        try:
            url = _decrypt_chunks(encrypted_parts)
            if url:
                result[quality.replace("A", "p")] = url
        except Exception as e:
            logger.warning(f"[yt-decrypt] {quality}: {type(e).__name__}: {e}")
    return result


def _decrypt_serial_urls(api_data) -> dict:
    """
    Сериал: api_data = [
        {'id': ..., 'name': '1-fasl', 'series': [
            {'id': ..., 'name': '1-qism 1080p', 'fileA': [chunks]},
            {'id': ..., 'name': '1-qism 720p', 'fileA': [chunks]},
            ...
        ]},
        ...
    ]
    Возвращает {
        '1': {  # сезон
            '1': {  # эпизод
                '1080p': 'url',
                '720p': 'url',
                ...
            },
            ...
        },
        ...
    }
    """
    if not isinstance(api_data, list):
        return {}
    result = {}
    for season_obj in api_data:
        if not isinstance(season_obj, dict):
            continue
        season_num = _parse_season_name(season_obj.get("name", ""))
        if season_num is None:
            continue
        season_key = str(season_num)
        for episode_obj in season_obj.get("series", []) or []:
            if not isinstance(episode_obj, dict):
                continue
            ep_num, quality = _parse_episode_name(episode_obj.get("name", ""))
            if ep_num is None:
                continue
            try:
                url = _decrypt_chunks(episode_obj.get("fileA"))
                if not url:
                    continue
                # Старые сезоны не содержат качество в имени эпизода —
                # вытаскиваем из самого URL (там бывает '480p'/'1080p').
                if quality is None:
                    quality = _detect_quality_from_url(url)
                ep_key = str(ep_num)
                result.setdefault(season_key, {}).setdefault(ep_key, {})[quality] = url
            except Exception as e:
                logger.warning(
                    f"[yt-decrypt-serial] s{season_num}e{ep_num} {quality}: "
                    f"{type(e).__name__}: {e}"
                )
    return result


def _decrypt_movie_urls(api_data) -> dict:
    """
    Универсальная точка входа. Определяет — фильм (dict) или сериал (list)
    — и возвращает соответствующую структуру.

    Для фильма:  {'480p': 'url', '720p': 'url', ...}
    Для сериала: {'1': {'1': {'480p': 'url', ...}, ...}, ...}
    Сам сериал-формат можно отличить по наличию вложенных dict'ов.
    """
    if isinstance(api_data, list):
        return _decrypt_serial_urls(api_data)
    if isinstance(api_data, dict):
        return _decrypt_film_urls(api_data)
    return {}


# ============================================================
# 1. COLLECT — собрать все content_id с yangi.tv
# ============================================================
@shared_task(bind=True, max_retries=3, queue="default")
def collect_all_ids(self):
    """Сбор всех content_id с yangi.tv API в YtConnectContent."""
    task_name = "YT collect_all_ids"
    url = f"{YT_API_BASE}/search"
    current_page = 1
    total_pages = 1
    new_ids_count = 0

    ScraperLog.objects.create(task_name=task_name, status="started", message="—")

    try:
        while current_page <= total_pages:
            params = {"page": current_page}
            response = requests.get(
                url, params=params, headers=_headers(), timeout=HTTP_TIMEOUT
            )

            if response.status_code != 200:
                logger.warning(
                    f"[collect] page {current_page} -> {response.status_code}, retry через 60s"
                )
                time.sleep(60)
                continue

            data = response.json()
            if current_page == 1:
                total_pages = data["data"]["lastPage"]

            for item in data["data"]["list"]:
                _, created = YtConnectContent.objects.get_or_create(
                    content_id=item["id"],
                    defaults={"parsing_status": "not_parsed"},
                )
                if created:
                    new_ids_count += 1

            current_page += 1
            time.sleep(PAGE_SLEEP)  # вежливая пауза между страницами

        ScraperLog.objects.create(
            task_name=task_name,
            status="success",
            message=f"страниц: {current_page - 1}, новых ID: {new_ids_count}",
        )
        logger.info(f"[collect] страниц: {current_page - 1}, новых ID: {new_ids_count}")
        return new_ids_count

    except Exception as exc:
        ScraperLog.objects.create(
            task_name=task_name, status="error", message=f"page {current_page}: {exc}"
        )
        raise self.retry(exc=exc, countdown=300)


# ============================================================
# 2. CONNECT — связать с Content по name_ru + year
# ============================================================
@shared_task(queue="default")
def spawn_yt_connect():
    """
    Диспетчер: берёт батч not_parsed YtConnectContent и кидает в очередь
    parse_yt_connect для каждого.
    """
    candidates = list(
        YtConnectContent.objects.filter(parsing_status="not_parsed")
        .order_by("id")
        .values_list("content_id", flat=True)[:CONNECT_BATCH]
    )
    if not candidates:
        return 0

    YtConnectContent.objects.filter(content_id__in=candidates).update(
        parsing_status="in_progress"
    )

    for content_id in candidates:
        parse_yt_connect.delay(content_id)

    logger.info(f"[connect-dispatcher] поставлено: {len(candidates)}")
    return len(candidates)


@shared_task(
    bind=True,
    max_retries=3,
    queue="default",
    rate_limit="6/m",
    soft_time_limit=120,
    time_limit=150,
)
def parse_yt_connect(self, content_id):
    """
    Для одного yangi.tv content_id:
      - тянет детали (name_ru, year, description, poster, age, ...)
      - ищет совпадение в Content по (name_ru, year_production)
      - заполняет uz-поля и помечает parsed.

    Если фильм уже связан (Content с id_uz=content_id и непустым name_uz),
    пропускаем API-запрос — повторно ничего не получим, только трафик зря.
    """
    task_name = f"YT connect {content_id}"

    # Быстрый skip — если фильм уже связан и заполнен.
    already_linked = (
        Content.objects.filter(id_uz=content_id)
        .exclude(name_uz="")
        .exclude(name_uz__isnull=True)
        .exists()
    )
    if already_linked:
        YtConnectContent.objects.filter(content_id=content_id).update(
            parsing_status="parsed"
        )
        return f"already linked {content_id}"

    url = f"{YT_API_BASE}/getContentDetail"

    try:
        response = requests.get(
            url,
            params={"content_id": content_id},
            headers=_headers(),
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json().get("data", {})

        if not data:
            _record_yt_failure(content_id, "connect", "empty data")
            return f"empty data {content_id}"

        name_ru = data.get("name_ru")
        year = data.get("year")

        # Нормализуем имя: trim + lower на стороне БД (Content.name_ru иногда
        # содержит trailing space из KP-парсера → exact-match не срабатывает).
        content_original = None
        if name_ru and year:
            yt_name_norm = name_ru.strip().lower()
            content_original = (
                Content.objects.annotate(
                    _name_norm=Lower(Trim("name_ru"))
                )
                .filter(_name_norm=yt_name_norm, year_production=year)
                .first()
            )

        if content_original:
            content_original.name_uz = data.get("name") or ""
            content_original.description_uz = data.get("description") or ""
            content_original.id_uz = content_id
            if content_original.age_restriction is None:
                content_original.age_restriction = parse_age(data.get("age"))
            content_original.save(
                update_fields=[
                    "name_uz",
                    "description_uz",
                    "id_uz",
                    "age_restriction",
                ]
            )

            # Постер качаем и сохраняем в MinIO через FieldFile.save —
            # это вызовет content_original.save() автоматически.
            poster_url = data.get("poster")
            if poster_url and not content_original.poster_uz:
                download_image_to_field(
                    content_original.poster_uz,
                    poster_url,
                    name_base=f"yt_{content_id}",
                )

            # Если у этого content_id уже есть готовые URL'ы (фаза 3 отработала
            # ДО того как мы нашли матч) — копируем их в film_content_uz сейчас.
            existing_yt = (
                YtConnectContent.objects.filter(content_id=content_id)
                .only("content_url", "is_serial")
                .first()
            )
            if (
                existing_yt
                and existing_yt.content_url
                and existing_yt.content_url != {}
                and (
                    not content_original.film_content_uz
                    or content_original.film_content_uz == {}
                )
            ):
                extra = {"film_content_uz": existing_yt.content_url}
                if existing_yt.is_serial and isinstance(existing_yt.content_url, dict):
                    try:
                        seasons = sorted(existing_yt.content_url.keys(), key=int)
                        if seasons:
                            last_s = int(seasons[-1])
                            extra["last_season_uz"] = last_s
                            ep_keys = list(
                                (existing_yt.content_url.get(str(last_s)) or {}).keys()
                            )
                            if ep_keys:
                                extra["last_episode_uz"] = max(int(e) for e in ep_keys)
                    except (ValueError, TypeError):
                        pass
                Content.objects.filter(pk=content_original.pk).update(**extra)

        YtConnectContent.objects.filter(content_id=content_id).update(
            parsing_status="parsed"
        )
        ScraperLog.objects.create(
            task_name=task_name,
            status="success",
            message=f"matched: {bool(content_original)}",
        )
        return f"ok {content_id} (matched: {bool(content_original)})"

    except Exception as exc:
        _record_yt_failure(content_id, "connect", str(exc))
        try:
            raise self.retry(exc=exc, countdown=120)
        except self.MaxRetriesExceededError:
            logger.error(f"☠️ connect retries исчерпаны для {content_id}")
            raise


# ============================================================
# 3. MOVIE URLS — зашифрованные ссылки на видео
# ============================================================
@shared_task(queue="default")
def spawn_yt_movie_urls():
    """
    Диспетчер: берёт батч YtConnectContent с
    parsing_status='parsed' AND parsing_status_player='not_parsed'
    и кидает в очередь parse_yt_movie_url.
    """
    candidates = list(
        YtConnectContent.objects.filter(
            parsing_status="parsed",
            parsing_status_player="not_parsed",
        )
        .order_by("id")
        .values_list("content_id", flat=True)[:MOVIE_URL_BATCH]
    )
    if not candidates:
        return 0

    YtConnectContent.objects.filter(content_id__in=candidates).update(
        parsing_status_player="in_progress"
    )

    for content_id in candidates:
        parse_yt_movie_url.delay(content_id)

    logger.info(f"[movie-url-dispatcher] поставлено: {len(candidates)}")
    return len(candidates)


@shared_task(
    bind=True,
    max_retries=3,
    queue="default",
    rate_limit="6/m",
    soft_time_limit=120,
    time_limit=150,
)
def parse_yt_movie_url(self, content_id):
    """
    Запрашивает getMovieUrl?content_id=..., расшифровывает AES-CBC,
    сохраняет dict {qualityname: url} в YtConnectContent.content_url
    и Content.film_content_uz.
    """
    task_name = f"YT movie url {content_id}"
    url = f"{YT_API_BASE}/getMovieUrl"

    try:
        response = requests.get(
            url,
            params={"content_id": content_id},
            headers=_headers(),
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        api_response = response.json()

        if api_response.get("code") != 200:
            _record_yt_failure(
                content_id,
                "player",
                f"API code {api_response.get('code')}: {api_response.get('message')}",
            )
            return f"api error {content_id}"

        data = api_response.get("data", {})
        urls = _decrypt_movie_urls(data)
        if not urls:
            _record_yt_failure(content_id, "player", "no urls decoded")
            return f"no urls {content_id}"

        is_serial = isinstance(data, list)

        # Сохраняем в YtConnectContent (локальная техническая БД)
        YtConnectContent.objects.filter(content_id=content_id).update(
            content_url=urls,
            parsing_status_player="parsed",
            is_serial=is_serial,
        )

        # Копируем в Content. Для сериала ещё last_season_uz / last_episode_uz.
        content_update = {"film_content_uz": urls}
        if is_serial and urls:
            try:
                seasons = sorted(urls.keys(), key=int)
                last_s = int(seasons[-1])
                content_update["last_season_uz"] = last_s
                ep_keys = list((urls.get(str(last_s)) or {}).keys())
                if ep_keys:
                    last_ep = max(int(e) for e in ep_keys)
                    content_update["last_episode_uz"] = last_ep
            except (ValueError, TypeError):
                pass

        Content.objects.filter(id_uz=content_id).update(**content_update)

        summary = (
            f"серий: {sum(len(v) for v in urls.values())}, сезонов: {len(urls)}"
            if is_serial
            else f"качества: {list(urls.keys())}"
        )
        logger.info(f"✅ {content_id} | {'сериал' if is_serial else 'фильм'} | {summary}")
        ScraperLog.objects.create(
            task_name=task_name,
            status="success",
            message=summary,
        )
        return f"ok {content_id}"

    except Exception as exc:
        _record_yt_failure(content_id, "player", str(exc))
        try:
            raise self.retry(exc=exc, countdown=120)
        except self.MaxRetriesExceededError:
            logger.error(f"☠️ movie-url retries исчерпаны для {content_id}")
            raise


# ============================================================
# 4. EXPIRE — recovery застрявших in_progress
# ============================================================
@shared_task(queue="default")
def expire_yt_stuck():
    """
    Сбрасывает в not_parsed записи, зависшие в in_progress дольше
    IN_PROGRESS_STUCK_MINUTES минут. Используется аналог updated_at —
    если статус in_progress, но запись давно не обновлялась, значит
    воркер умер и не закончил работу.
    """
    threshold = timezone.now() - timedelta(minutes=IN_PROGRESS_STUCK_MINUTES)

    stuck_connect = YtConnectContent.objects.filter(
        parsing_status="in_progress",
        updated_at__lt=threshold,
    ).update(parsing_status="not_parsed")

    stuck_player = YtConnectContent.objects.filter(
        parsing_status_player="in_progress",
        updated_at__lt=threshold,
    ).update(parsing_status_player="not_parsed")

    logger.info(
        f"[yt-expire] стак-connect: {stuck_connect}, стак-player: {stuck_player}"
    )
    return {"stuck_connect": stuck_connect, "stuck_player": stuck_player}


# ============================================================
# Backward compatibility — старое имя
# ============================================================
@shared_task(queue="default")
def connect_yt_content():
    """Backward-compat: один связь за вызов. Лучше использовать spawn_yt_connect."""
    candidate = (
        YtConnectContent.objects.filter(parsing_status="not_parsed")
        .order_by("id")
        .first()
    )
    if not candidate:
        return "no candidates"
    YtConnectContent.objects.filter(pk=candidate.pk).update(
        parsing_status="in_progress"
    )
    return parse_yt_connect.run(candidate.content_id)


@shared_task(queue="default")
def get_movie_url():
    """Backward-compat: один URL за вызов. Лучше использовать spawn_yt_movie_urls."""
    candidate = (
        YtConnectContent.objects.filter(
            parsing_status="parsed", parsing_status_player="not_parsed"
        )
        .order_by("id")
        .first()
    )
    if not candidate:
        return "no candidates"
    YtConnectContent.objects.filter(pk=candidate.pk).update(
        parsing_status_player="in_progress"
    )
    return parse_yt_movie_url.run(candidate.content_id)
