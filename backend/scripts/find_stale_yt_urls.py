"""
Ищет YtConnectContent с устаревшим форматом URL и (опционально) сбрасывает
их статус для перепарсинга.

Старый формат (надо перепарсить):
  - https://cdn-1.yangi.tv/Kinolar/...
  - https://yangi.tv/generator.php?link=...

Новый формат (корректный):
  - https://sN.yangi.tv/kinolar/MM.YYYY/ID/...

Запуск:
    # просто посмотреть сколько таких:
    docker compose exec backend python manage.py shell < backend/scripts/find_stale_yt_urls.py

    # или с RESET=1 чтобы сбросить parsing_status_player='not_parsed':
    docker compose exec -e RESET=1 backend python manage.py shell < backend/scripts/find_stale_yt_urls.py
"""
import os
import re

from apps.scrapers.models import YtConnectContent


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


qs = YtConnectContent.objects.filter(
    parsing_status_player="parsed",
    content_url__isnull=False,
).only("content_id", "content_url", "is_serial")

stale_ids = []
total = 0
for rec in qs.iterator(chunk_size=500):
    total += 1
    if has_stale(rec.content_url):
        stale_ids.append(rec.content_id)

print(f"Всего записей с parsed: {total}")
print(f"Из них со stale URL:    {len(stale_ids)}")
if stale_ids[:20]:
    print(f"Первые 20 ID:           {stale_ids[:20]}")

if RESET and stale_ids:
    n = YtConnectContent.objects.filter(content_id__in=stale_ids).update(
        parsing_status_player="not_parsed",
        player_fail_count=0,
    )
    print(f"\n✅ Сброшено в not_parsed: {n}")
    print("Диспетчер spawn_yt_movie_urls подберёт их пачками по 20.")
elif stale_ids:
    print(f"\n[dry-run] Чтобы сбросить статус — запусти с RESET=1")
