"""Base settings shared by dev/prod/test."""

import importlib.util
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR.parent / ".env")

SECRET_KEY = env("SECRET_KEY", default="insecure-dev-key-change-in-production")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.users",
    "apps.academics",
    "apps.scheduling",
    "apps.bookings",
]

AUTH_USER_MODEL = "users.User"

# django-q2 is a hard dependency, but keep the defensive check from v1 so the
# task queue can be disabled in constrained environments without editing code.
TASK_QUEUE_BACKEND = env("TASK_QUEUE_BACKEND", default="django_q2")
DJANGO_Q_AVAILABLE = importlib.util.find_spec("django_q") is not None
if TASK_QUEUE_BACKEND == "django_q2" and DJANGO_Q_AVAILABLE:
    INSTALLED_APPS.append("django_q")

MIDDLEWARE = [
    "config.middleware.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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

# PostgreSQL only — no SQLite fallback (dev and test require a running
# PostgreSQL 16 instance; local dev uses a portable instance on port 5433).
DATABASES = {
    "default": env.db("DATABASE_URL"),
}

# Booking window defaults; per-laboratory overrides live in LaboratoryBookingSettings.
BOOKING_HORIZON_DAYS = env.int("BOOKING_HORIZON_DAYS", default=14)
BOOKING_CANCEL_HOURS = env.int("BOOKING_CANCEL_HOURS", default=24)
MANUAL_BOOKING_WORKING_WEEKS = env.int("MANUAL_BOOKING_WORKING_WEEKS", default=2)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Redis cache (dev overrides to LocMemCache — see settings/dev.py).
REDIS_URL = env("REDIS_URL", default="redis://127.0.0.1:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    },
}

# django-q2: ORM broker (PostgreSQL) — no Redis dependency for the queue itself.
TASK_QUEUE_ALWAYS_EAGER = env.bool("TASK_QUEUE_ALWAYS_EAGER", default=False)
Q_CLUSTER = {
    "name": env("TASK_QUEUE_NAME", default="labbooking"),
    "workers": env.int("TASK_QUEUE_WORKERS", default=4),
    "timeout": env.int("TASK_QUEUE_TIMEOUT", default=90),
    "retry": env.int("TASK_QUEUE_RETRY", default=600),
    "bulk": env.int("TASK_QUEUE_BULK", default=10),
    "orm": "default",
    "catch_up": False,
    "sync": TASK_QUEUE_ALWAYS_EAGER,
}

# Security headers (HTTPS-specific hardening lives in settings/prod.py).
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {
        "handlers": ["console"],
        "level": env("DJANGO_LOG_LEVEL", default="INFO"),
    },
}
