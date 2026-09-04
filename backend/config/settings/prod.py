from .base import *  # noqa: F403

DEBUG = False

# No insecure default in production — misconfigured deploy must fail at startup.
SECRET_KEY = env("SECRET_KEY")  # noqa: F405

SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)  # noqa: F405
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

_default_csrf_origins = [
    f"https://{host}"
    for host in ALLOWED_HOSTS  # noqa: F405
    if host not in ("localhost", "127.0.0.1")
]
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=_default_csrf_origins)  # noqa: F405

if SECURE_SSL_REDIRECT:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
