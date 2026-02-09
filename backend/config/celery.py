import os
from celery import Celery
from django.apps import apps

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")

app.conf.broker_connection_max_retries = None  # Бесконечное количество попыток
app.conf.broker_connection_retry = True  # Включение повторных попыток
app.conf.broker_connection_timeout = 1.0  # Таймаут ожидания подключения (в секундах)
app.conf.broker_heartbeat = (
    10  # Интервал отправки heartbeat для проверки соединения с redis
)

app.conf.worker_prefetch_multiplier = 1  # Чтобы воркер не хватал лишние задачи
app.conf.task_acks_late = (
    True  # Чтобы задача возвращалась в очередь при падении воркера
)

app.autodiscover_tasks(lambda: [n.name for n in apps.get_app_configs()])
