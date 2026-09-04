from .dev import *  # noqa: F403

DEBUG = True
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
TASK_QUEUE_ALWAYS_EAGER = True
