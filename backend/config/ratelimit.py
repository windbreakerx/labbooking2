"""Ключ rate limit по реальному IP клиента.

За nginx `REMOTE_ADDR` — это адрес контейнера nginx, поэтому лимит на
`key="ip"` становится общим для всех пользователей. nginx выставляет
`X-Real-IP: $remote_addr` (перезаписывая клиентское значение), его и берём.
Если заголовка нет (dev, тесты, прямой доступ) — падаем на X-Forwarded-For
и затем на REMOTE_ADDR.
"""


def client_ip(group, request) -> str:
    real_ip = request.META.get("HTTP_X_REAL_IP")
    if real_ip:
        return real_ip.strip()

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()

    return request.META.get("REMOTE_ADDR", "")
