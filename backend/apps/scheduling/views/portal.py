"""Портал завлаба/сисадмина (день 5): главная, привязки дисциплин, селектор лаборатории.

Сисадмин работает с любой лабораторией: выбранная хранится в сессии
(селектор в шапке), завлабу лаборатория приходит из профиля.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView

from apps.academics.models import Discipline
from apps.academics.scope import resolve_staff_laboratory
from apps.scheduling.models import Laboratory
from apps.scheduling.services import (
    BindingError,
    bind,
    bindable_disciplines_qs,
    faculty_matches,
    unbind,
)
from apps.users.mixins import LabHeadRequiredMixin, SysAdminRequiredMixin
from apps.users.models import UserRole

SESSION_ADMIN_LAB = "admin_lab_id"


def resolve_portal_laboratory(request):
    """Лаборатория портала: завлабу — из профиля, сисадмину — из сессии."""
    if request.user.role == UserRole.SYS_ADMIN:
        lab_id = request.session.get(SESSION_ADMIN_LAB)
        if lab_id is None:
            return None
        return Laboratory.objects.filter(pk=lab_id).first()
    return resolve_staff_laboratory(request.user)


class PortalLaboratoryMixin(LabHeadRequiredMixin):
    """Страницы портала, которым нужна лаборатория: без неё — на главную портала.

    Проверка в ``dispatch`` (а не get/post-обёртками): у view'ов свои ``post``,
    которые перекрыли бы обёртку в MRO.
    """

    def dispatch(self, request, *args, **kwargs):
        if self.has_access(request.user):
            self.laboratory = resolve_portal_laboratory(request)
            if self.laboratory is None:
                messages.error(request, "Сначала выберите лабораторию.")
                return redirect("lab-head-home")
        return super().dispatch(request, *args, **kwargs)


class LabHeadHomeView(LabHeadRequiredMixin, TemplateView):
    """Главная портала: без лаборатории показывает подсказку вместо редиректа."""

    template_name = "scheduling/portal/home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        laboratory = resolve_portal_laboratory(self.request)
        context["laboratory"] = laboratory
        context["bindings_count"] = (
            laboratory.discipline_bindings.count() if laboratory else 0
        )
        return context


class LabHeadBindingsView(PortalLaboratoryMixin, TemplateView):
    template_name = "scheduling/portal/bindings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        laboratory = self.laboratory
        query = (self.request.GET.get("q") or "").strip()

        bindings = laboratory.discipline_bindings.select_related(
            "discipline__department__faculty", "discipline__semester", "bound_by"
        )
        bindable = bindable_disciplines_qs(self.request.user, laboratory).exclude(
            pk__in=laboratory.discipline_bindings.values("discipline_id")
        )
        if query:
            bindings = bindings.filter(discipline__title__icontains=query)
            bindable = bindable.filter(title__icontains=query)

        context.update(
            laboratory=laboratory,
            query=query,
            bindings=bindings,
            bindable_disciplines=[
                {
                    "discipline": discipline,
                    "needs_override": not faculty_matches(laboratory, discipline),
                }
                for discipline in bindable
            ],
        )
        return context


class LabDisciplineBindView(PortalLaboratoryMixin, View):
    def post(self, request, pk):
        discipline = get_object_or_404(Discipline, pk=pk)
        try:
            bind(
                request.user,
                self.laboratory,
                discipline,
                override=request.POST.get("override") == "on",
                reason=request.POST.get("reason", ""),
            )
        except BindingError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Дисциплина «{discipline.title}» привязана.")
        return redirect("lab-head-bindings")


class LabDisciplineUnbindView(PortalLaboratoryMixin, View):
    def post(self, request, pk):
        discipline = get_object_or_404(Discipline, pk=pk)
        try:
            unbind(request.user, self.laboratory, discipline)
        except BindingError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Дисциплина «{discipline.title}» отвязана.")
        return redirect("lab-head-bindings")


class LabSelectView(SysAdminRequiredMixin, View):
    """Селектор лаборатории сисадмина из шапки: выбор в сессию и возврат назад."""

    def post(self, request):
        lab_pk = request.POST.get("laboratory")
        if not lab_pk:
            request.session.pop(SESSION_ADMIN_LAB, None)
        else:
            laboratory = get_object_or_404(Laboratory, pk=lab_pk)
            request.session[SESSION_ADMIN_LAB] = laboratory.pk
            messages.success(request, f"Лаборатория: {laboratory.name}")
        return redirect(self._next_url(request))

    def _next_url(self, request):
        next_url = request.POST.get("next") or "lab-head-home"
        if not url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return "lab-head-home"
        return next_url
