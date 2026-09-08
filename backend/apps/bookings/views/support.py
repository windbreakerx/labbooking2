"""Мессенджер обращений (день 6): чат-пузыри, поллинг 4с, статусы, overdue.

Студент: список обращений, создание (тема/текст/УЦ из доступных группе),
чат на тикет. Сотрудник: split-view — треды слева (скоуп УЦ, unread/overdue
бейджи), активный чат справа; клик по треду подгружает чат-фрагмент без
перезагрузки, прочтение отмечается при открытии чата.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from apps.academics.scope import student_support_training_centers_qs
from apps.bookings.forms import TicketCreateForm
from apps.bookings.models import SupportTicket
from apps.bookings.scope import staff_support_tickets_qs
from apps.bookings.services.support import (
    SupportError,
    create_ticket,
    is_support_ticket_overdue,
    mark_staff_read,
    staff_reply,
    staff_set_status,
    student_reply,
)
from apps.users.mixins import StaffRequiredMixin, StudentRequiredMixin
from apps.users.models import UserRole


def _last_message(messages):
    return max(messages, key=lambda item: item.created_at, default=None)


def _chat_items(ticket: SupportTicket, messages):
    """Плоская лента чата: разделители дат + пузыри (первый пузырь — тело тикета)."""
    entries = [(ticket.created_at, ticket.student, ticket.body)]
    entries += [(m.created_at, m.author, m.body) for m in messages]
    items = []
    prev_date = None
    for created_at, author, body in entries:
        if created_at.date() != prev_date:
            items.append({"kind": "day", "created_at": created_at})
            prev_date = created_at.date()
        items.append(
            {"kind": "msg", "author": author, "body": body, "created_at": created_at}
        )
    return items


def _chat_context(ticket: SupportTicket, *, error: str = "") -> dict:
    context = {
        "ticket": ticket,
        "chat_items": _chat_items(ticket, ticket.messages.select_related("author")),
        "ticket_overdue": is_support_ticket_overdue(ticket),
    }
    if error:
        context["error"] = error
    return context


def _thread_rows(user):
    """Треды сотрудника: превью последнего сообщения, unread, overdue; по активности."""
    tickets = (
        staff_support_tickets_qs(user)
        .select_related("student")
        .prefetch_related("messages__author")
    )
    rows = []
    for ticket in tickets:
        msg_list = list(ticket.messages.all())
        last = _last_message(msg_list)
        last_student = max(
            (m.created_at for m in msg_list if m.author.role == UserRole.STUDENT),
            default=None,
        )
        rows.append(
            {
                "ticket": ticket,
                "last": last,
                "unread": ticket.staff_read_at is None
                or (last_student is not None and last_student > ticket.staff_read_at),
                "overdue": is_support_ticket_overdue(ticket),
                "last_activity": (last.created_at if last else None) or ticket.created_at,
            }
        )
    rows.sort(key=lambda row: row["last_activity"], reverse=True)
    return rows


def _hx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


# --- Студент ----------------------------------------------------------------------


class SupportListView(StudentRequiredMixin, View):
    def get(self, request):
        tickets = request.user.support_tickets.select_related("training_center").prefetch_related("messages")
        rows = sorted(
            (
                {"ticket": ticket, "last": _last_message(ticket.messages.all())}
                for ticket in tickets
            ),
            key=lambda row: (row["last"] or row["ticket"]).created_at,
            reverse=True,
        )
        return render(request, "bookings/support/student_list.html", {"rows": rows})


class SupportCreateView(StudentRequiredMixin, View):
    def get(self, request):
        if not student_support_training_centers_qs(request.user).exists():
            messages.info(
                request, "Дисциплины вашей группы не привязаны к лабораториям — написать нельзя."
            )
            return redirect("support")
        return render(
            request, "bookings/support/student_create.html", {"form": TicketCreateForm(user=request.user)}
        )

    def post(self, request):
        form = TicketCreateForm(request.POST, user=request.user)
        if not form.is_valid():
            return render(request, "bookings/support/student_create.html", {"form": form})
        try:
            ticket = create_ticket(
                request.user,
                subject=form.cleaned_data["subject"],
                body=form.cleaned_data["body"],
                training_center=form.cleaned_data["training_center"],
            )
        except SupportError as exc:
            messages.error(request, str(exc))
            return render(request, "bookings/support/student_create.html", {"form": form})
        messages.success(request, "Обращение создано.")
        return redirect("support-detail", ticket.pk)


class SupportChatView(StudentRequiredMixin, View):
    def get(self, request, pk):
        ticket = get_object_or_404(
            SupportTicket.objects.select_related("student", "training_center"),
            pk=pk,
            student=request.user,
        )
        return render(
            request, "bookings/support/student_chat.html", _chat_context(ticket)
        )


class SupportMessagesView(StudentRequiredMixin, View):
    """Поллинг ленты студента: только фрагмент, без оболочки."""

    def get(self, request, pk):
        ticket = get_object_or_404(
            SupportTicket.objects.select_related("student", "training_center"),
            pk=pk,
            student=request.user,
        )
        return render(request, "bookings/support/_messages.html", _chat_context(ticket))


class SupportReplyView(StudentRequiredMixin, View):
    def post(self, request, pk):
        ticket = get_object_or_404(SupportTicket, pk=pk, student=request.user)
        try:
            student_reply(ticket, request.user, request.POST.get("body", ""))
        except SupportError as exc:
            if _hx(request):
                return render(
                    request, "bookings/support/_messages.html", _chat_context(ticket, error=str(exc))
                )
            messages.error(request, str(exc))
            return redirect("support-detail", ticket.pk)
        if _hx(request):
            return render(request, "bookings/support/_messages.html", _chat_context(ticket))
        return redirect("support-detail", ticket.pk)


# --- Сотрудник --------------------------------------------------------------------


def _staff_ticket(request, pk) -> SupportTicket | None:
    return (
        staff_support_tickets_qs(request.user)
        .select_related("student", "training_center")
        .filter(pk=pk)
        .first()
    )


class StaffSupportView(StaffRequiredMixin, View):
    def get(self, request):
        rows = _thread_rows(request.user)
        ticket = None
        ticket_id = request.GET.get("ticket", "")
        if ticket_id.isdigit():
            ticket = next(
                (row["ticket"] for row in rows if row["ticket"].pk == int(ticket_id)), None
            )
        context = {"rows": rows}
        if ticket:
            mark_staff_read(ticket)
            for row in rows:
                if row["ticket"].pk == ticket.pk:
                    row["unread"] = False
            context.update(_chat_context(ticket))
        return render(request, "bookings/support/staff_support.html", context)


class StaffThreadsView(StaffRequiredMixin, View):
    """Поллинг списка тредов: активный тикет приходит в ?active=."""

    def get(self, request):
        active_id = request.GET.get("active", "")
        active_id = int(active_id) if active_id.isdigit() else None
        rows = _thread_rows(request.user)
        return render(
            request,
            "bookings/support/_thread_list.html",
            {"rows": rows, "active_ticket_id": active_id},
        )


class StaffChatView(StaffRequiredMixin, View):
    """Клик по треду: чат-фрагмент в правую панель + отметка о прочтении."""

    def get(self, request, pk):
        ticket = _staff_ticket(request, pk)
        if ticket is None:
            return render(request, "bookings/support/_chat_denied.html")
        mark_staff_read(ticket)
        return render(request, "bookings/support/_chat.html", _chat_context(ticket))


class StaffMessagesView(StaffRequiredMixin, View):
    def get(self, request, pk):
        ticket = _staff_ticket(request, pk)
        if ticket is None:
            return render(request, "bookings/support/_chat_denied.html")
        return render(request, "bookings/support/_messages.html", _chat_context(ticket))


class StaffReplyView(StaffRequiredMixin, View):
    def post(self, request, pk):
        ticket = _staff_ticket(request, pk)
        if ticket is None:
            messages.error(request, "Обращение недоступно в вашей зоне доступа.")
            return redirect("staff-support")
        try:
            staff_reply(ticket, request.user, request.POST.get("body", ""))
        except SupportError as exc:
            if _hx(request):
                return render(
                    request, "bookings/support/_messages.html", _chat_context(ticket, error=str(exc))
                )
            messages.error(request, str(exc))
            return redirect("staff-support")
        if _hx(request):
            return render(request, "bookings/support/_messages.html", _chat_context(ticket))
        return redirect("staff-support")


class StaffStatusView(StaffRequiredMixin, View):
    def post(self, request, pk):
        ticket = _staff_ticket(request, pk)
        if ticket is None:
            messages.error(request, "Обращение недоступно в вашей зоне доступа.")
            return redirect("staff-support")
        try:
            staff_set_status(ticket, request.user, request.POST.get("status", ""))
        except SupportError as exc:
            if _hx(request):
                return render(
                    request, "bookings/support/_chat.html", _chat_context(ticket, error=str(exc))
                )
            messages.error(request, str(exc))
            return redirect("staff-support")
        if _hx(request):
            return render(request, "bookings/support/_chat.html", _chat_context(ticket))
        return redirect("staff-support")
