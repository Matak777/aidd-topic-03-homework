import logging
import math
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() in {"1", "true", "yes", "on"}

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set when DEBUG is false."
        )
    SECRET_KEY = "django-insecure-local-development-only"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]"
    ).split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
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
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly"
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
        "console": {"class": "logging.StreamHandler", "formatter": "main_format"},
    },
    "loggers": {
        "main": {"handlers": ["console"], "level": "INFO", "propagate": True}
    },
}

logger = logging.getLogger("main")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
LOGIN_URL = "user:auth"

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

WSGI_APPLICATION = "config.wsgi.application"

DB_ENGINE = os.getenv("DB_ENGINE", "sqlite").lower()
if DB_ENGINE == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.getenv("SQLITE_NAME", str(BASE_DIR / "db.sqlite3")),
        }
    }
elif DB_ENGINE in {"postgres", "postgresql"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "investing"),
            "USER": os.getenv("POSTGRES_USER", "postgres"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
            "HOST": os.getenv("POSTGRES_HOST", "localhost"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    }
else:
    raise ImproperlyConfigured("DB_ENGINE must be either 'sqlite' or 'postgresql'.")

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

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN", "").strip()
PRICE_DATA_MODE = os.getenv("PRICE_DATA_MODE", "live").strip().lower()
if PRICE_DATA_MODE not in {"live", "demo"}:
    raise ImproperlyConfigured("PRICE_DATA_MODE must be either 'live' or 'demo'.")
try:
    PRICE_COLLECTION_INTERVAL_SECONDS = int(
        os.getenv("PRICE_COLLECTION_INTERVAL_SECONDS", "300")
    )
except ValueError as error:
    raise ImproperlyConfigured(
        "PRICE_COLLECTION_INTERVAL_SECONDS must be a positive integer."
    ) from error
if PRICE_COLLECTION_INTERVAL_SECONDS < 1:
    raise ImproperlyConfigured(
        "PRICE_COLLECTION_INTERVAL_SECONDS must be a positive integer."
    )
try:
    TINKOFF_RPC_TIMEOUT_SECONDS = float(
        os.getenv("TINKOFF_RPC_TIMEOUT_SECONDS", "10")
    )
except ValueError as error:
    raise ImproperlyConfigured(
        "TINKOFF_RPC_TIMEOUT_SECONDS must be a positive finite number."
    ) from error
if not math.isfinite(TINKOFF_RPC_TIMEOUT_SECONDS) or TINKOFF_RPC_TIMEOUT_SECONDS <= 0:
    raise ImproperlyConfigured(
        "TINKOFF_RPC_TIMEOUT_SECONDS must be a positive finite number."
    )

"""Celery Configuration Options"""
CELERY_TIMEZONE = "Europe/Moscow"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
# CELERY_BROKER_URL = 'amqp://guest:guest@localhost:5672'
CELERY_BROKER_URL = os.getenv(
    "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0"
)

"""Debug Celery"""
CELERY_TASK_ALWAYS_EAGER = (
    os.getenv("CELERY_TASK_ALWAYS_EAGER", "false").lower()
    in {"1", "true", "yes", "on"}
)
