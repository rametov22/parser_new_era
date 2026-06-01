from datetime import timedelta
from django.utils import timezone
from django.db.models import Q
from django.conf import settings
from apps.scrapers.models import Content

today = timezone.now().date()
start_date = today - timedelta(days=settings.PREMIERE)
cut_date = today - timedelta(days=4)

total_not_parsed = Content.objects.filter(is_parsed_ru='not_parsed').count()
in_premiere = Content.objects.filter(
    Q(premiere__range=(start_date, today)) | Q(premiere_ru__range=(start_date, today)),
    is_parsed_ru='not_parsed'
).count()
with_old_update = Content.objects.filter(
    Q(premiere__range=(start_date, today)) | Q(premiere_ru__range=(start_date, today)),
    is_parsed_ru='not_parsed',
    last_update__lte=cut_date
).count()

print(f'PREMIERE={settings.PREMIERE}, today={today}, cut_date={cut_date}')
print(f'Всего not_parsed: {total_not_parsed}')
print(f'В окне премьеры ({settings.PREMIERE} дн): {in_premiere}')
print(f'Eligible (last_update <= {cut_date}): {with_old_update}')

# Проверяем spawn_iframe_parsers напрямую
import redis
r = redis.Redis(host=settings.REDIS_HOST, port=int(settings.REDIS_PORT), password=settings.REDIS_PASSWORD)
print(f'vavada_queue len: {r.llen("vavada_queue")}')

from apps.scrapers.tasks.vavada import spawn_iframe_parsers
result = spawn_iframe_parsers()
print(f'spawn_iframe_parsers result: {result}')
