"""Отдача файлов методичек: только после входа и проверки зоны доступа.

В проде файл отдаёт nginx по ``X-Accel-Redirect`` (внутренний
``/protected-media/``), поэтому публичного ``/media/`` нет. В dev и тестах —
``FileResponse``, чтобы не требовать nginx.
"""

from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.academics.models import LabWorkMethodics
from apps.academics.scope import user_can_view_methodics

X_ACCEL_PREFIX = "/protected-media/"


def _content_disposition(filename: str, *, inline: bool) -> str:
    kind = "inline" if inline else "attachment"
    ascii_name = filename.encode("ascii", "ignore").decode().replace('"', "") or "methodic.pdf"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


class MethodicFileView(LoginRequiredMixin, View):
    """Просмотр (inline) и скачивание (``?download=1``) PDF-методички."""

    def get(self, request, pk):
        methodic = get_object_or_404(
            LabWorkMethodics.objects.select_related("lab_work"), pk=pk
        )
        if not user_can_view_methodics(request.user, methodic.lab_work):
            raise PermissionDenied

        name = methodic.file.name or ""
        if not name.startswith("methodics/") or ".." in name.split("/"):
            raise Http404("Файл не найден.")

        filename = name.rsplit("/", 1)[-1]
        disposition = _content_disposition(filename, inline=request.GET.get("download") != "1")

        if getattr(settings, "USE_X_ACCEL_REDIRECT", False):
            response = HttpResponse(content_type="application/pdf")
            response["X-Accel-Redirect"] = X_ACCEL_PREFIX + quote(name)
        else:
            try:
                response = FileResponse(methodic.file.open("rb"), content_type="application/pdf")
            except FileNotFoundError as exc:
                raise Http404("Файл не найден.") from exc

        response["Content-Disposition"] = disposition
        response["X-Content-Type-Options"] = "nosniff"
        return response
