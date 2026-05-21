"""
Копирует все YtConnectContent из default → main_db.

В default таблица называется `scrapers_ytconnectcontent` (managed=True,
старая).
В main_db — `parser_yt_connect_content` (managed=False, новая, создана
проектом Kmax).

Использует raw SQL чтобы не зависеть от текущего состояния модели.
ON CONFLICT (content_id) DO UPDATE — идемпотентен, можно прогонять
несколько раз.

Запуск:
    docker compose exec -T backend python manage.py shell \\
        < backend/scripts/copy_yt_to_main_db.py
"""
from django.db import connections
from psycopg2.extras import Json


SRC_TABLE = "scrapers_ytconnectcontent"
DST_TABLE = "parser_yt_connect_content"
BATCH = 500


with connections["default"].cursor() as c:
    c.execute(f"SELECT COUNT(*) FROM {SRC_TABLE}")
    total = c.fetchone()[0]
    print(f"[src=default.{SRC_TABLE}] записей: {total}")

if total == 0:
    print("нечего копировать")
    raise SystemExit(0)

# Проверим что есть в main_db до копирования
with connections["main_db"].cursor() as c:
    c.execute(f"SELECT COUNT(*) FROM {DST_TABLE}")
    pre = c.fetchone()[0]
    print(f"[dst=main_db.{DST_TABLE}] до копирования: {pre}")

# Чтение с курсором (server-side), чтобы не съесть память
read_cursor = connections["default"].cursor()
read_cursor.execute(f"""
    SELECT content_id, content_url, is_serial, parsing_status,
           parsing_status_player, connect_fail_count, player_fail_count,
           created_at, updated_at
    FROM {SRC_TABLE}
    ORDER BY id
""")

write_cursor = connections["main_db"].cursor()
insert_sql = f"""
    INSERT INTO {DST_TABLE}
    (content_id, content_url, is_serial, parsing_status,
     parsing_status_player, connect_fail_count, player_fail_count,
     created_at, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (content_id) DO UPDATE SET
        content_url = EXCLUDED.content_url,
        is_serial = EXCLUDED.is_serial,
        parsing_status = EXCLUDED.parsing_status,
        parsing_status_player = EXCLUDED.parsing_status_player,
        connect_fail_count = EXCLUDED.connect_fail_count,
        player_fail_count = EXCLUDED.player_fail_count,
        created_at = EXCLUDED.created_at,
        updated_at = EXCLUDED.updated_at
"""

copied = 0
buf = []
for row in read_cursor:
    (
        content_id,
        content_url,
        is_serial,
        parsing_status,
        parsing_status_player,
        connect_fail_count,
        player_fail_count,
        created_at,
        updated_at,
    ) = row
    buf.append(
        (
            content_id,
            Json(content_url) if content_url is not None else None,
            is_serial,
            parsing_status,
            parsing_status_player,
            connect_fail_count,
            player_fail_count,
            created_at,
            updated_at,
        )
    )
    if len(buf) >= BATCH:
        write_cursor.executemany(insert_sql, buf)
        copied += len(buf)
        print(f"  скопировано: {copied}/{total}")
        buf = []

if buf:
    write_cursor.executemany(insert_sql, buf)
    copied += len(buf)

print(f"✅ скопировано всего: {copied}")

with connections["main_db"].cursor() as c:
    c.execute(f"SELECT COUNT(*) FROM {DST_TABLE}")
    post = c.fetchone()[0]
    print(f"[dst=main_db.{DST_TABLE}] после копирования: {post}")
