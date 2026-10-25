import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")

app.config_from_object("django.conf:settings", namespace="CELERY")

# Имя записи соответствует фактическому интервалу (в исходнике имя
# «add-every-5-min» не совпадало с schedule 10 с — рассогласование устранено).
app.conf.beat_schedule = {
    "update-prices-every-10-seconds": {
        "task": "dashboard.tasks.update_last_price",
        "schedule": 10.0,
    },
}

app.autodiscover_tasks()
