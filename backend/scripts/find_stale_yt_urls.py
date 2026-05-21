"""
Ищет stale URL в:
  1. YtConnectContent.content_url
  2. Content.film_content_uz  (туда копируется из YtConnectContent)

Старый формат:
  - cdn-N.yangi.tv/...
  - yangi.tv/generator.php?link=...

Запуск dry-run:
    docker compose exec -T backend python manage.py shell \\
      < backend/scripts/find_stale_yt_urls.py

С RESET=1 — сбросит parsing_status_player='not_parsed' для перепарсинга:
    docker compose exec -T -e RESET=1 backend python manage.py shell \\
      < backend/scripts/find_stale_yt_urls.py
"""
import os
import re

from apps.scrapers.models import YtConnectContent, Content


STALE_PATTERNS = [
    re.compile(r"cdn-\d+\.yangi\.tv", re.IGNORECASE),
    re.compile(r"yangi\.tv/generator\.php", re.IGNORECASE),
]

RESET = os.environ.get("RESET") == "1"


def is_stale_url(url):
    if not isinstance(url, str):
        return False
    return any(p.search(url) for p in STALE_PATTERNS)


def has_stale(content_url):
    """Рекурсивно ищет stale-URL в dict/list/str."""
    if isinstance(content_url, str):
        return is_stale_url(content_url)
    if isinstance(content_url, dict):
        return any(has_stale(v) for v in content_url.values())
    if isinstance(content_url, list):
        return any(has_stale(v) for v in content_url)
    return False


# ========== 1. YtConnectContent ==========
print("=" * 60)
print("1. YtConnectContent")
print("=" * 60)

# Распределение по статусам
print("По parsing_status_player:")
from django.db.models import Count

for row in (
    YtConnectContent.objects.values("parsing_status_player")
    .annotate(n=Count("id"))
    .order_by("parsing_status_player")
):
    print(f"  {row['parsing_status_player']:15} : {row['n']}")

yt_qs = YtConnectContent.objects.filter(content_url__isnull=False).only(
    "content_id", "content_url"
)

yt_stale_ids = []
yt_total = 0
for rec in yt_qs.iterator(chunk_size=500):
    yt_total += 1
    if has_stale(rec.content_url):
        yt_stale_ids.append(rec.content_id)

print(f"\nЗаписей с непустым content_url: {yt_total}")
print(f"Из них stale:                    {len(yt_stale_ids)}")
if yt_stale_ids[:20]:
    print(f"Первые 20:                       {yt_stale_ids[:20]}")


# ========== 2. Content.film_content_uz ==========
print("\n" + "=" * 60)
print("2. Content.film_content_uz")
print("=" * 60)

content_qs = Content.objects.filter(film_content_uz__isnull=False).only(
    "id", "id_uz", "kino_poisk_id", "name_ru", "film_content_uz"
)

content_stale_ids = []
content_stale_id_uz = []
content_total = 0
for rec in content_qs.iterator(chunk_size=500):
    content_total += 1
    if has_stale(rec.film_content_uz):
        content_stale_ids.append(rec.id)
        if rec.id_uz:
            content_stale_id_uz.append(rec.id_uz)

print(f"\nContent с непустым film_content_uz: {content_total}")
print(f"Из них stale:                       {len(content_stale_ids)}")
if content_stale_ids[:20]:
    print(f"Первые 20 (Content.id):             {content_stale_ids[:20]}")


# ========== RESET ==========
print("\n" + "=" * 60)
all_stale_yt_ids = set(yt_stale_ids) | set(content_stale_id_uz)
print(f"Итого уникальных yangi content_id со stale URL: {len(all_stale_yt_ids)}")

if RESET and all_stale_yt_ids:
    # Если запись в YtConnectContent отсутствует — создаём для перепарсинга.
    existing = set(
        YtConnectContent.objects.filter(content_id__in=all_stale_yt_ids).values_list(
            "content_id", flat=True
        )
    )
    missing = all_stale_yt_ids - existing
    if missing:
        YtConnectContent.objects.bulk_create(
            [
                YtConnectContent(
                    content_id=cid,
                    parsing_status="parsed",
                    parsing_status_player="not_parsed",
                )
                for cid in missing
            ],
            ignore_conflicts=True,
        )
        print(f"Создано отсутствующих записей: {len(missing)}")

    n = YtConnectContent.objects.filter(content_id__in=all_stale_yt_ids).update(
        parsing_status_player="not_parsed",
        player_fail_count=0,
    )
    print(f"✅ Сброшено в not_parsed: {n}")
    print("Дальше — spawn_yt_movie_urls (beat) или вручную раскидать в очередь.")
elif all_stale_yt_ids:
    print("[dry-run] чтобы сбросить — запусти с -e RESET=1")
