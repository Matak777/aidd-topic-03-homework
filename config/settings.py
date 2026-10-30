import logging
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Конфигурация через переменные окружения и файл .env (см. .env.example).
# Файл .env не обязателен: переменные окружения имеют приоритет.
load_dotenv(BASE_DIR / ".env")

_env = os.environ.get
_bool = lambda v: str(v).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}  # noqa: E731


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name, ""))
    except (TypeError, ValueError):
        return default


# --- Core Django ---
# Для разработки допускается ключ по умолчанию; в production обязательно
# переопределяйте SECRET_KEY переменной окружения.
SECRET_KEY = _env("SECRET_KEY", "django-insecure-dev-only-key-change-me")
DEBUG = _bool(_env("DJANGO_DEBUG", "on"))
ALLOWED_HOSTS = [
    h.strip()
    for h in _env("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "crispy_forms",
    "crispy_bootstrap3",
    "dashboard",
    "user",
    "rest_framework",
    "django_filters",
]

CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap3"
CRISPY_TEMPLATE_PACK = "bootstrap3"

REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 100,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend"
    ],
    # Анонимно — только чтение; запись (POST/PUT/PATCH/DELETE) — только
    # аутентифицированным (исправление Д12 из ANALYSIS.md: полный
    # анонимный CRUD был дефектом, а не требованием).
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ],
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "main_format": {
            "format": "[{module} {asctime} {levelname}] {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "main_format",
        },
        "file": {
            "class": "logging.FileHandler",
            "formatter": "main_format",
            "filename": _env("LOG_FILE", "app.logs"),
        },
    },
    "loggers": {
        "main": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": True,
        }
    },
}

logger = logging.getLogger("main")

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

WSGI_APPLICATION = "config.wsgi.application"

# --- База данных ---
# По умолчанию SQLite (достаточно для локального запуска без Docker).
# Для PostgreSQL установите DATABASE=postgres и переменные DB_*.
DATABASE = _env("DATABASE", "sqlite").lower()

if DATABASE == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _env("DB_NAME", "investing"),
            "USER": _env("DB_USER", "postgres"),
            "PASSWORD": _env("DB_PASSWORD", ""),
            "HOST": _env("DB_HOST", "localhost"),
            "PORT": _env("DB_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / _env("DB_NAME", "db.sqlite3"),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "en-us"

TIME_ZONE = _env("TIME_ZONE", "UTC")

USE_I18N = True

USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Внешние сервисы ---
# Токен Tinkoff Invest API задаётся только через окружение (.env/TINKOFF_TOKEN).
# Пустое значение разрешено: приложение запускается, но интеграции с рынком
# явно сообщают об отсутствии токена (см. dashboard/services/market_data.py).
TINKOFF_TOKEN = _env("TINKOFF_TOKEN", "")
TINKOFF_APP_NAME = _env("TINKOFF_APP_NAME", "investing-demo")

# Ограничение времени реальных внешних запросов к Tinkoff API (сек).
# Валидация окончательно выполняется перед каждым RPC (validate_timeout).
_raw_tinkoff_timeout = _env("TINKOFF_TIMEOUT", "") or "30"
try:
    TINKOFF_TIMEOUT = float(_raw_tinkoff_timeout)
except ValueError:
    raise ImproperlyConfigured(
        f"TINKOFF_TIMEOUT должно быть конечным положительным числом "
        f"секунд, получено: {_raw_tinkoff_timeout!r}"
    )

# --- История цен (группа 3, FEATURES.md) ---
# Источник цен для накопления истории: "tinkoff" (реальный API, нужен
# TINKOFF_TOKEN) или "demo" (ЯВНО включаемый синтетический режим для
# демонстрации без токена; данные помечаются источником в БД и
# "демонстрационными" в интерфейсе). Неизвестное значение — ошибка
# конфигурации, а не молчаливый fallback.
PRICE_SOURCE = _env("PRICE_SOURCE", "tinkoff").lower()
if PRICE_SOURCE not in ("tinkoff", "demo"):
    raise ImproperlyConfigured(
        f"PRICE_SOURCE должно быть 'tinkoff' или 'demo', получено: "
        f"{PRICE_SOURCE!r}"
    )

# Интервал локального коллектора цен (management command collect_prices),
# секунд; допустимы только конечные положительные значения (пустое значение
# окружения считается не заданным — берётся умолчание).
_raw_price_interval = _env("PRICE_COLLECT_INTERVAL", "") or "10"
try:
    PRICE_COLLECT_INTERVAL = float(_raw_price_interval)
except ValueError:
    raise ImproperlyConfigured(
        f"PRICE_COLLECT_INTERVAL должно быть конечным положительным числом "
        f"секунд, получено: {_raw_price_interval!r}"
    ) from None
if (
    PRICE_COLLECT_INTERVAL <= 0
    or PRICE_COLLECT_INTERVAL != PRICE_COLLECT_INTERVAL
    or PRICE_COLLECT_INTERVAL in (float("inf"), float("-inf"))
):
    raise ImproperlyConfigured(
        f"PRICE_COLLECT_INTERVAL должно быть конечным положительным числом "
        f"секунд, получено: {_raw_price_interval!r}"
    )

# --- Celery ---
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = _bool(_env("CELERY_TASK_TRACK_STARTED", "on"))
CELERY_TASK_TIME_LIMIT = _int_env("CELERY_TASK_TIME_LIMIT", 1800)
# Брокер по умолчанию — Redis; позже его отсутствие приводит к явной ошибке
# соединения Celery, а не к скрытым потерям задач.
CELERY_BROKER_URL = _env("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_TASK_ALWAYS_EAGER = _bool(_env("CELERY_TASK_ALWAYS_EAGER", "off"))
