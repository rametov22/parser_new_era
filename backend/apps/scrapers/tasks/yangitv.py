"""
Парсер yangi.tv (Yangi TV) — узбекский видео-сервис.

Логика:
  1. collect_all_ids       — собирает все content_id с API yangi.tv
                              в YtConnectContent (статус not_parsed).
  2. spawn_yt_connect      — диспетчер: батчем берёт not_parsed,
                              для каждой запускает parse_yt_connect.
  3. parse_yt_connect      — для одного content_id берёт детали
                              (name/name_ru/orig_name/year), ищет совпадение в Content,
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
from itertools import zip_longest

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
    default="414307|9GZiGExoNwcwAQkL7inXsWetcbpX0svd4Ygw93QNc995ccb1",
)
# Интеграция с основным backend Kmax: отдаём готовые id_uz new_releases,
# если backend сам не может достучаться до Yangi.tv API (гео-блок).
KMAX_INTERNAL_URL = config("KMAX_INTERNAL_URL", default="")
KMAX_INTERNAL_TOKEN = config("KMAX_INTERNAL_TOKEN", default="")
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

# Обновление сериалов: перепарс уже спарсенных сериалов, чтобы подхватить
# новые серии. Окно — не чаще раза в N дней.
SERIAL_REFRESH_DAYS = 2
SERIAL_REFRESH_BATCH = 100

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
        **{count_field: F(count_field) + 1}, updated_at=timezone.now()
    )
    rec = YtConnectContent.objects.filter(content_id=content_id).only(count_field).first()
    fail_count = getattr(rec, count_field, 0) if rec else 0

    if fail_count >= MAX_FAIL_ATTEMPTS:
        YtConnectContent.objects.filter(content_id=content_id).update(
            **{status_field: "failed"}, updated_at=timezone.now()
        )
        logger.error(
            f"☠️ {phase}: {content_id} помечен failed после {fail_count} попыток"
        )
    else:
        YtConnectContent.objects.filter(content_id=content_id).update(
            **{status_field: "not_parsed"}, updated_at=timezone.now()
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


def _normalize_name(s: str) -> str:
    """
    Жёсткая нормализация: lower, ё→е, выкидываем все знаки препинания
    (кавычки «»""'', запятые, двоеточия, тире и т.д.), сжимаем пробелы.

    'Загадай «свою» смерть' → 'загадай свою смерть'
    '«Монарх»: Наследие монстров' → 'монарх наследие монстров'
    """
    if not s:
        return ""
    s = s.replace("ё", "е").replace("Ё", "Е")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def _normalize_name_soft(s: str) -> str:
    """
    Та же нормализация + дополнительно удаляем содержимое скобок (...) и
    всё после первого ':' / '.' / '—' (часто это субтитр который у
    yangi.tv есть, а у kinopoisk нет).

    'Загадай свою смерть (Если бы желания могли убивать)' → 'загадай свою смерть'
    'Операция «Панда». Дикая миссия' → 'операция панда'
    """
    if not s:
        return ""
    # Убираем содержимое скобок
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\[[^\]]*\]", "", s)
    # Режем хвост после первого разделителя сабтайтла
    s = re.split(r"[:.—–]", s, maxsplit=1)[0]
    return _normalize_name(s)


def _match_content(yt_name: str, yt_year, content_id: int, yt_name_original: str = ""):
    """
    Многоступенчатый матч YtConnectContent → Content.

    Возвращает (content, strategy) либо (None, reason_str).
    Стратегии (по убыванию строгости):
      1. exact_norm_year             — нормализованное ru-имя + год
      2. exact_original_norm_year    — orig_name + Content.name_original + год
      3. exact_*_unique_any_year     — имя уникально во всей базе,
                                      если год yangi.tv неверный
      4. exact_norm                  — нормализованное имя без года (если в yt нет year)
      5. soft_norm_year              — мягкая нормализация + год
    Если на любом шаге найдено >1 совпадение — НЕ привязываем (ambiguous).
    """
    if not yt_name and not yt_name_original:
        return None, "no_yt_name"

    yt_strict = _normalize_name(yt_name)
    yt_soft = _normalize_name_soft(yt_name)
    yt_original_strict = _normalize_name(yt_name_original)
    yt_original_soft = _normalize_name_soft(yt_name_original)

    # Строим набор кандидатов через Python (Content в main_db, JOIN не делаем).
    # Сужаем по году для строгих стратегий, но ниже умеем безопасный fallback
    # на уникальное совпадение во всей базе, когда год на yangi.tv ошибочный.
    qs = Content.objects.only("id", "name_ru", "name_original", "year_production")
    if yt_year:
        qs = qs.filter(year_production=yt_year)
    candidates = list(qs)
    no_candidates_in_year = bool(yt_year and not candidates)

    # Стратегия 1: exact normalized + year
    if candidates and yt_strict:
        exact_year = [
            c for c in candidates
            if _normalize_name(c.name_ru) == yt_strict
        ]
        if len(exact_year) == 1:
            return exact_year[0], "exact_norm_year"
        if len(exact_year) > 1:
            return None, f"ambiguous_exact_{len(exact_year)}"

    # Fallback: original title from Yangi → Content.name_original
    if candidates and yt_original_strict:
        exact_original_year = [
            c for c in candidates
            if _normalize_name(c.name_original) == yt_original_strict
        ]
        if len(exact_original_year) == 1:
            return exact_original_year[0], "exact_original_norm_year"
        if len(exact_original_year) > 1:
            return None, f"ambiguous_original_exact_{len(exact_original_year)}"

    all_qs = None

    # Если год отличается, допускаем матч без года только когда название уникально.
    if yt_year and (yt_strict or yt_original_strict):
        all_qs = list(
            Content.objects.only(
                "id", "name_ru", "name_original", "year_production"
            )
        )
        if yt_strict:
            exact_any_year = [
                c for c in all_qs
                if _normalize_name(c.name_ru) == yt_strict
            ]
            if len(exact_any_year) == 1:
                return exact_any_year[0], "exact_norm_unique_any_year"
            if len(exact_any_year) > 1:
                return None, f"ambiguous_exact_any_year_{len(exact_any_year)}"

        if yt_original_strict:
            exact_original_any_year = [
                c for c in all_qs
                if _normalize_name(c.name_original) == yt_original_strict
            ]
            if len(exact_original_any_year) == 1:
                return exact_original_any_year[0], "exact_original_norm_unique_any_year"
            if len(exact_original_any_year) > 1:
                return None, (
                    f"ambiguous_original_exact_any_year_{len(exact_original_any_year)}"
                )

    # Стратегия 2: exact normalized (без года, если в yt нет года)
    if not yt_year:
        if all_qs is None:
            all_qs = list(
                Content.objects.only(
                    "id", "name_ru", "name_original", "year_production"
                )
            )
        if yt_strict:
            exact_any = [
                c for c in all_qs
                if _normalize_name(c.name_ru) == yt_strict
            ]
            if len(exact_any) == 1:
                return exact_any[0], "exact_norm_noyear"
            if len(exact_any) > 1:
                return None, f"ambiguous_exact_noyear_{len(exact_any)}"

        if yt_original_strict:
            exact_original_any = [
                c for c in all_qs
                if _normalize_name(c.name_original) == yt_original_strict
            ]
            if len(exact_original_any) == 1:
                return exact_original_any[0], "exact_original_norm_noyear"
            if len(exact_original_any) > 1:
                return None, (
                    f"ambiguous_original_exact_noyear_{len(exact_original_any)}"
                )

    # Стратегия 3: soft normalize (выкинуть скобки/сабтайтл) + year
    if candidates and yt_year and yt_soft:
        soft_matches = [
            c for c in candidates
            if _normalize_name_soft(c.name_ru) == yt_soft
        ]
        if len(soft_matches) == 1:
            return soft_matches[0], "soft_norm_year"
        if len(soft_matches) > 1:
            return None, f"ambiguous_soft_{len(soft_matches)}"

    if candidates and yt_year and yt_original_soft:
        soft_original_matches = [
            c for c in candidates
            if _normalize_name_soft(c.name_original) == yt_original_soft
        ]
        if len(soft_original_matches) == 1:
            return soft_original_matches[0], "soft_original_norm_year"
        if len(soft_original_matches) > 1:
            return None, f"ambiguous_original_soft_{len(soft_original_matches)}"

    if no_candidates_in_year:
        return None, "no_kp_in_year"

    return None, "no_match"


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
        parsing_status="in_progress", updated_at=timezone.now()
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
      - тянет детали (name, name_ru, orig_name, year, description, poster, age, ...)
      - ищет совпадение в Content по (name_ru, year_production),
        затем по (orig_name, year_production)
      - заполняет uz-поля и помечает parsed.

    Если фильм уже связан и metadata yangi.tv уже сохранена,
    пропускаем API-запрос.
    """
    task_name = f"YT connect {content_id}"

    yt_meta = (
        YtConnectContent.objects.filter(content_id=content_id)
        .only("yt_name", "yt_name_uz", "yt_name_original", "yt_year")
        .first()
    )

    # Быстрый skip — если фильм уже связан и metadata yangi.tv уже заполнена.
    already_linked = (
        Content.objects.filter(id_uz=content_id)
        .exclude(name_uz="")
        .exclude(name_uz__isnull=True)
        .exists()
    )
    metadata_cached = bool(
        yt_meta
        and yt_meta.yt_name
        and yt_meta.yt_name_uz
        and yt_meta.yt_name_original
        and yt_meta.yt_year is not None
    )
    if already_linked and metadata_cached:
        YtConnectContent.objects.filter(content_id=content_id).update(
            parsing_status="parsed", updated_at=timezone.now()
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

        name_uz = data.get("name") or ""
        name_ru = data.get("name_ru") or ""
        name_original = data.get("orig_name") or ""
        year = data.get("year")

        content_original, strategy = _match_content(
            name_ru, year, content_id, name_original
        )
        logger.info(
            f"[yt-match] {content_id} | yt={name_ru!r}/{year} → "
            f"{strategy} → {f'kp_id={content_original.kino_poisk_id}' if content_original else 'NONE'}"
        )

        if content_original:
            content_original.name_uz = name_uz
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
                Content.objects.filter(
                    pk=content_original.pk, add_content_date_uz__isnull=True
                ).update(add_content_date_uz=timezone.now().date())

        YtConnectContent.objects.filter(content_id=content_id).update(
            parsing_status="parsed",
            yt_name=name_ru,
            yt_name_uz=name_uz,
            yt_name_original=name_original,
            yt_year=year,
            updated_at=timezone.now(),
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
        parsing_status_player="in_progress", updated_at=timezone.now()
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

        # Сохраняем в YtConnectContent (в main_db через router)
        YtConnectContent.objects.filter(content_id=content_id).update(
            content_url=urls,
            parsing_status_player="parsed",
            is_serial=is_serial,
            updated_at=timezone.now(),
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
        # Дату ставим только при ПЕРВОМ появлении плеера, чтобы перепарс
        # сериалов (refresh каждые N дней) её не сбрасывал на сегодня.
        Content.objects.filter(
            id_uz=content_id, add_content_date_uz__isnull=True
        ).update(add_content_date_uz=timezone.now().date())

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
# 3b. SERIAL REFRESH — обновление уже спарсенных сериалов
# ============================================================
@shared_task(queue="default")
def spawn_yt_serial_refresh():
    """
    Диспетчер обновления сериалов yangi.tv.

    Отдельный парс не нужен: parse_yt_movie_url заново тянет getMovieUrl со
    всеми сезонами/сериями и обновляет content_url / film_content_uz /
    last_season_uz / last_episode_uz. Здесь только периодически возвращаем в
    очередь уже спарсенные сериалы, давно не обновлявшиеся.

    Кандидаты: is_serial=True, parsing_status_player='parsed',
    updated_at старше SERIAL_REFRESH_DAYS. updated_at двигаем атомарно,
    чтобы повторный тик не схватил те же.
    """
    cutoff = timezone.now() - timedelta(days=SERIAL_REFRESH_DAYS)
    candidates = list(
        YtConnectContent.objects.filter(
            is_serial=True,
            parsing_status_player="parsed",
            updated_at__lt=cutoff,
        )
        .order_by("updated_at")
        .values_list("content_id", flat=True)[:SERIAL_REFRESH_BATCH]
    )
    if not candidates:
        logger.info("[yt-serial-refresh] нет кандидатов")
        return 0

    # Двигаем updated_at, чтобы следующий тик не подхватил эти же сериалы
    # (parse_yt_movie_url по успеху снова обновит updated_at на now).
    YtConnectContent.objects.filter(content_id__in=candidates).update(
        updated_at=timezone.now()
    )
    for content_id in candidates:
        parse_yt_movie_url.delay(content_id)

    logger.info(f"[yt-serial-refresh] поставлено в очередь: {len(candidates)}")
    return len(candidates)


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
    ).update(parsing_status="not_parsed", updated_at=timezone.now())

    stuck_player = YtConnectContent.objects.filter(
        parsing_status_player="in_progress",
        updated_at__lt=threshold,
    ).update(parsing_status_player="not_parsed", updated_at=timezone.now())

    logger.info(
        f"[yt-expire] стак-connect: {stuck_connect}, стак-player: {stuck_player}"
    )
    return {"stuck_connect": stuck_connect, "stuck_player": stuck_player}


# ============================================================
# 5. RETRY — возвращаем в работу failed и «player есть, но не связано»
# ============================================================
@shared_task(queue="default")
def retry_yt_failed():
    """
    Периодический recovery (рекомендуется раз в ~6 часов):

      1. failed connect/player → not_parsed + обнуление счётчика попыток.
         Даём свежие MAX_FAIL_ATTEMPTS попыток — часть сбоев временные
         (моргнул API, "no urls decoded" в тот момент и т.п.).

      2. player='parsed', но не попавшие в Content → матчим ЛОКАЛЬНО по кэшу
         yt_name/yt_year (без запроса к API) и сбрасываем connect в not_parsed
         только тем, у кого матч уже нашёлся (KP-сторона дозаполнила name_ru).
         Тогда connect один раз сходит в API и свяжет, скопировав content_url
         в film_content_uz. Несвязанные без матча API не дёргают.

    Темп задают существующие диспетчеры с их rate_limit=6/m — здесь только
    переводим статусы, фактический перепарс идёт штатным путём.
    """
    now = timezone.now()

    failed_connect = YtConnectContent.objects.filter(
        parsing_status="failed"
    ).update(parsing_status="not_parsed", connect_fail_count=0, updated_at=now)

    failed_player = YtConnectContent.objects.filter(
        parsing_status_player="failed"
    ).update(parsing_status_player="not_parsed", player_fail_count=0, updated_at=now)

    # «player есть, но не связано»: player спарсен, но в Content не попал.
    # Чтобы не дёргать API для всех каждый цикл — матчим ЛОКАЛЬНО по кэшу
    # yt_name/yt_name_original/yt_year и переводим в not_parsed (→ перепарс через API) только:
    #   - у кого матч НАШЁЛСЯ локально (KP-сторона дозаполнилась) — connect свяжет;
    #   - у кого кэша имени ещё нет (старые записи) — один раз сходить за деталями.
    # Несвязанные без локального матча API не трогают вообще.
    linked_filled = set(
        Content.objects.exclude(id_uz__isnull=True)
        .exclude(film_content_uz={})
        .exclude(film_content_uz__isnull=True)
        .values_list("id_uz", flat=True)
    )
    unlinked = (
        YtConnectContent.objects.filter(parsing_status_player="parsed")
        .exclude(content_id__in=linked_filled)
        .only("content_id", "yt_name", "yt_name_original", "yt_year")
    )
    relink_ids = []
    for y in unlinked.iterator():
        if not y.yt_name and not y.yt_name_original:
            relink_ids.append(y.content_id)  # кэша ещё нет → один раз сходить в API
            continue
        if y.yt_year is None:
            continue  # без года матч ненадёжен — API не дёргаем
        content, _strategy = _match_content(
            y.yt_name, y.yt_year, y.content_id, y.yt_name_original
        )
        if content:
            relink_ids.append(y.content_id)  # локально найден матч → connect свяжет
    relink = YtConnectContent.objects.filter(content_id__in=relink_ids).update(
        parsing_status="not_parsed", updated_at=now
    )

    logger.info(
        f"[yt-retry] failed-connect→{failed_connect}, "
        f"failed-player→{failed_player}, relink→{relink}"
    )
    return {
        "failed_connect": failed_connect,
        "failed_player": failed_player,
        "relink": relink,
    }


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
        parsing_status="in_progress", updated_at=timezone.now()
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
        parsing_status_player="in_progress", updated_at=timezone.now()
    )
    return parse_yt_movie_url.run(candidate.content_id)


# ============================================================
# 6. KMAX INTEGRATION — обновление кеша uz new_releases в основном backend
# ============================================================
def _fetch_category_ids(category_id: int, page: int = 1) -> list[int]:
    """Вспомогательный запрос getCategoryDetail для Kmax-интеграции."""
    url = f"{YT_API_BASE}/getCategoryDetail"
    try:
        response = requests.get(
            url,
            params={"category_id": category_id, "page": page},
            headers=_headers(),
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 200:
            logger.warning(
                "[kmax-yangi] category %s returned code %s: %s",
                category_id,
                payload.get("code"),
                payload.get("message"),
            )
            return []
        items = payload.get("data", {}).get("list", [])
        return [int(item["id"]) for item in items if item.get("id")]
    except Exception:
        logger.exception("[kmax-yangi] failed to fetch category %s", category_id)
        return []


@shared_task(queue="default")
def refresh_kmax_yangi_cache():
    """
    Запрашивает категории 1,2,4,5 Yangi.tv API (доступен из UZ), делает
    round-robin и отправляет готовые списки id_uz в основной backend Kmax.

    Актуально, когда backend Kmax стоит за гео-блоком (например, в KZ) и не
    может сам обратиться к admin.yangi.tv.
    """
    if not KMAX_INTERNAL_URL or not KMAX_INTERNAL_TOKEN:
        logger.info(
            "[kmax-yangi-refresh] KMAX_INTERNAL_URL or KMAX_INTERNAL_TOKEN not configured, skipping"
        )
        return "skipped"

    category_ids = (1, 2, 4, 5)
    categories = {cid: _fetch_category_ids(cid) for cid in category_ids}

    # Round-robin: по одному из каждой категории.
    all_ids = []
    for bucket in zip_longest(*categories.values()):
        for value in bucket:
            if value is not None:
                all_ids.append(value)
    home_ids = all_ids[:20]

    if not all_ids:
        logger.warning(
            "[kmax-yangi-refresh] all categories are empty, skipping POST"
        )
        return {"home": 0, "all": 0, "skipped": True}

    url = f"{KMAX_INTERNAL_URL.rstrip('/')}/ru/api/v1/home/internal/yangi/refresh/"
    try:
        response = requests.post(
            url,
            json={"home": home_ids, "all": all_ids},
            headers={
                "Authorization": f"Bearer {KMAX_INTERNAL_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        logger.info(
            "[kmax-yangi-refresh] sent home=%s all=%s response=%s",
            len(home_ids),
            len(all_ids),
            response.json(),
        )
        return response.json()
    except Exception:
        logger.exception("[kmax-yangi-refresh] failed to post to Kmax")
        raise
