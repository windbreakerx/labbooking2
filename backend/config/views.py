from django.db import connection
from django.http import JsonResponse


def health(request):
    """Liveness/readiness probe for Docker healthchecks.

    Returns 200 with {"status": "ok"} when the database is reachable,
    503 with the error otherwise.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 — any DB failure must fail the probe
        return JsonResponse({"status": "error", "database": str(exc)}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})
