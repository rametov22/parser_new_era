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
import time

import requests
from celery import shared_task
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from decouple import config

from ..models import YtConnectContent, ScraperLog, Content
from ..utils import parse_age


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

# Размер батча для диспетчеров
CONNECT_BATCH = 50
MOVIE_URL_BATCH = 50


def _headers():
    return {
        "User-Agent": "okhttp/5.1.0",
        "Authorization": f"Bearer {YT_BEARER_TOKEN}",
        "Accept": "application/json",
    }


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


def _decrypt_movie_urls(api_data: dict) -> dict:
    """
    Расшифровывает URL'ы из ответа yangi.tv API getMovieUrl.
    Каждое качество — список base64-зашифрованных кусков, кодируются
    AES-CBC отдельно по куску, потом склеиваются.
    """
    result = {}
    for quality, encrypted_parts in (api_data or {}).items():
        if not isinstance(encrypted_parts, list) or not encrypted_parts:
            continue
        try:
            decrypted_parts = []
            for part in encrypted_parts:
                encrypted_chunk = base64.b64decode(part)
                cipher = Cipher(algorithms.AES(YT_AES_KEY), modes.CBC(YT_AES_IV))
                decryptor = cipher.decryptor()
                chunk = decryptor.update(encrypted_chunk) + decryptor.finalize()
                decrypted_parts.append(chunk)
            full = b"".join(decrypted_parts)
            clean = _manual_unpad(full)
            url = clean.decode("utf-8", errors="ignore").strip()
            if url:
                quality_name = quality.replace("A", "p")
                result[quality_name] = url
        except Exception as e:
            logger.warning(f"[yt-decrypt] {quality}: {type(e).__name__}: {e}")
            continue
    return result


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
                url, params=params, headers=_headers(), timeout=15
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
            time.sleep(2)  # вежливая пауза между страницами

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
    bind=True, max_retries=3, queue="default", soft_time_limit=30, time_limit=45
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
            url, params={"content_id": content_id}, headers=_headers(), timeout=15
        )
        response.raise_for_status()
        data = response.json().get("data", {})

        if not data:
            YtConnectContent.objects.filter(content_id=content_id).update(
                parsing_status="not_parsed"
            )
            return f"empty data {content_id}"

        name_ru = data.get("name_ru")
        year = data.get("year")

        content_original = (
            Content.objects.filter(name_ru=name_ru, year_production=year).first()
            if name_ru and year
            else None
        )

        if content_original:
            content_original.name_uz = data.get("name") or ""
            content_original.description_uz = data.get("description") or ""
            content_original.id_uz = content_id
            content_original.poster_uz = data.get("poster") or None
            if content_original.age_restriction is None:
                content_original.age_restriction = parse_age(data.get("age"))
            content_original.save(
                update_fields=[
                    "name_uz",
                    "description_uz",
                    "id_uz",
                    "poster_uz",
                    "age_restriction",
                ]
            )

        YtConnectContent.objects.filter(content_id=content_id).update(
            parsing_status="parsed"
        )
        return f"ok {content_id} (matched: {bool(content_original)})"

    except Exception as exc:
        YtConnectContent.objects.filter(content_id=content_id).update(
            parsing_status="not_parsed"
        )
        ScraperLog.objects.create(
            task_name=task_name, status="error", message=str(exc)[:500]
        )
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
    bind=True, max_retries=3, queue="default", soft_time_limit=30, time_limit=45
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
            url, params={"content_id": content_id}, headers=_headers(), timeout=15
        )
        response.raise_for_status()
        api_response = response.json()

        if api_response.get("code") != 200:
            ScraperLog.objects.create(
                task_name=task_name,
                status="error",
                message=f"API code {api_response.get('code')}: {api_response.get('message')}",
            )
            YtConnectContent.objects.filter(content_id=content_id).update(
                parsing_status_player="not_parsed"
            )
            return f"api error {content_id}"

        urls = _decrypt_movie_urls(api_response.get("data", {}))
        if not urls:
            ScraperLog.objects.create(
                task_name=task_name,
                status="error",
                message="no urls decoded",
            )
            YtConnectContent.objects.filter(content_id=content_id).update(
                parsing_status_player="not_parsed"
            )
            return f"no urls {content_id}"

        # Сохраняем в YtConnectContent (локальная техническая БД)
        YtConnectContent.objects.filter(content_id=content_id).update(
            content_url=urls,
            parsing_status_player="parsed",
        )

        # Копируем в Content.film_content_uz, если связь установлена.
        Content.objects.filter(id_uz=content_id).update(film_content_uz=urls)

        logger.info(f"✅ {content_id} | качества: {list(urls.keys())}")
        ScraperLog.objects.create(
            task_name=task_name,
            status="success",
            message=f"качества: {list(urls.keys())}",
        )
        return f"ok {content_id}"

    except Exception as exc:
        YtConnectContent.objects.filter(content_id=content_id).update(
            parsing_status_player="not_parsed"
        )
        ScraperLog.objects.create(
            task_name=task_name, status="error", message=str(exc)[:500]
        )
        try:
            raise self.retry(exc=exc, countdown=120)
        except self.MaxRetriesExceededError:
            logger.error(f"☠️ movie-url retries исчерпаны для {content_id}")
            raise


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
