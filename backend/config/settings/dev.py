from .base import *  # noqa: F403

DEBUG = True

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# No local Redis on the dev machine — use in-process cache instead.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
}
