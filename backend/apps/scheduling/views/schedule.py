"""Портал расписания завлаба/сисадмина: календарь слотов и массовые операции.

Календарь строится по сетке «день недели × пара» для выбранной аудитории.
Все операции со слотами — через session_admin / session_bulk.
"""

from datetime import date, datetime, time

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from apps.academics.models import ALLOWED_LAB_DURATIONS, Semester
from apps.academics.scope import (
    laboratory_people_qs,
    laboratory_study_groups_qs,
    laboratory_teacher_users_qs,
)
from apps.scheduling.models import (
    LabSession,
    LabSessionBookingMode,
    LabSessionGroupPart,
    LabSessionStatus,
)
from apps.scheduling.services.session_admin import (
    SessionError,
    cancel_session,
    create_session,
    update_session,
)
from apps.scheduling.services.session_bulk import (
    BulkError,
    cancel_day,
    cancel_series,
    copy_day,
    copy_week,
    create_series,
)
from apps.scheduling.services.slot_grid import (
    UNIVERSITY_PAIR_SLOTS,
    pair_start_times_for_duration,
    time_to_minutes,
)
from apps.scheduling.views.portal import PortalLaboratoryMixin
from apps.users.models import UserRole

WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _pair_number_for_time(moment: time) -> int | None:
    minute_value = time_to_minutes(moment)
    for number, pair_start, pair_end in UNIVERSITY_PAIR_SLOTS:
        if time_to_minutes(pair_start) <= minute_value < time_to_minutes(pair_end):
            return number
    return None


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_time(value) -> time | None:
    if not value:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except (ValueError, TypeError):
            continue
    return None


def _default_room(laboratory):
    return laboratory.rooms.filter(is_blocked=False).order_by("number").first()


def _default_semester() -> Semester | None:
    active = Semester.objects.filter(is_active=True)
    if active.count() == 1:
        return active.first()
    return None


def _warn_group_overflow(request, session) -> None:
    if (
        session.booking_mode == LabSessionBookingMode.GROUP
        and session.capacity > session.room.capacity
    ):
        messages.warning(
            request,
            f"Группа ({session.capacity}) больше вместимости аудитории "
            f"({session.room.capacity}). Записи созданы, факт записан в аудит.",
        )


def _session_grid(sessions: list[LabSession]):
    """Раскладывает слоты по сетке [пара][день недели]."""
    grid = {
        pair_number: {
            weekday: [] for weekday in range(7)
        }
        for pair_number, _pair_start, _pair_end in UNIVERSITY_PAIR_SLOTS
    }
    for session in sessions:
        local_start = timezone.localtime(session.starts_at)
        pair = _pair_number_for_time(local_start.time())
        if pair is None:
            continue
        grid[pair][local_start.weekday()].append(session)
    return [
        {
            "pair": pair_number,
            "start": pair_start,
            "end": pair_end,
            "days": [grid[pair_number][weekday] for weekday in range(7)],
        }
        for pair_number, pair_start, pair_end in UNIVERSITY_PAIR_SLOTS
    ]


def _start_time_groups() -> list[dict]:
    """Допустимые старты для всех длительностей, сгруппированные по парам."""
    starts = sorted(
        {
            start
            for duration in ALLOWED_LAB_DURATIONS
            for start in pair_start_times_for_duration(duration)
        }
    )
    groups = []
    for number, pair_start, pair_end in UNIVERSITY_PAIR_SLOTS:
        times = [
            start
            for start in starts
            if time_to_minutes(pair_start) <= time_to_minutes(start) < time_to_minutes(pair_end)
        ]
        if times:
            groups.append(
                {
                    "number": number,
                    "start": pair_start,
                    "end": pair_end,
                    "times": times,
                }
            )
    return groups


def _form_context(laboratory, room, semester) -> dict:
    lab_works = list(laboratory.lab_works.filter(is_published=True).order_by("number", "title"))
    rooms = list(laboratory.rooms.order_by("number"))
    semesters = list(Semester.objects.order_by("-start_date"))
    teacher_map = {
        user.pk: user
        for user in [
            *laboratory_teacher_users_qs(laboratory),
            *laboratory_people_qs(laboratory).filter(
                role__in=[UserRole.LAB_ADMIN, UserRole.LAB_HEAD]
            ),
        ]
    }
    teachers = sorted(
        teacher_map.values(), key=lambda user: (user.last_name, user.first_name)
    )
    groups = list(
        laboratory_study_groups_qs(laboratory, semester.pk if semester else None)
    )
    return {
        "lab_works": lab_works,
        "rooms": rooms,
        "semesters": semesters,
        "teachers": teachers,
        "student_groups": groups,
        "booking_modes": LabSessionBookingMode.choices,
        "group_parts": LabSessionGroupPart.choices,
        "start_time_groups": _start_time_groups(),
        "weekday_labels": WEEKDAY_LABELS,
        "selected_room": room,
        "selected_semester": semester,
    }


class LabHeadScheduleView(PortalLaboratoryMixin, View):
    """Календарь слотов выбранной аудитории + формы управления."""

    template_name = "scheduling/portal/schedule.html"

    def _resolve_selection(self, request):
        room = None
        semester = None
        room_pk = request.GET.get("room") or request.POST.get("room")
        semester_pk = request.GET.get("semester") or request.POST.get("semester")
        if room_pk:
            room = self.laboratory.rooms.filter(pk=room_pk).first()
        if semester_pk:
            semester = Semester.objects.filter(pk=semester_pk).first()
        if room is None:
            room = _default_room(self.laboratory)
        if semester is None:
            semester = _default_semester()
        return room, semester

    def get(self, request):
        room, semester = self._resolve_selection(request)
        sessions = self._sessions(room, semester)
        context = {
            "laboratory": self.laboratory,
            "room": room,
            "semester": semester,
            "grid": _session_grid(sessions),
            "sessions": sessions,
        }
        if room and semester:
            context.update(_form_context(self.laboratory, room, semester))
        return render(request, self.template_name, context)

    def post(self, request):
        action = request.POST.get("action", "create")
        handler = getattr(self, f"_handle_{action}", None)
        if handler is None:
            messages.error(request, "Неизвестное действие.")
            return redirect("lab-head-schedule")
        return handler(request)

    def _sessions(self, room, semester):
        if room is None or semester is None:
            return []
        sessions = list(
            LabSession.objects.filter(
                room=room,
                semester=semester,
            )
            .exclude(status=LabSessionStatus.CANCELLED)
            .select_related("lab_work", "room", "room__training_center", "teacher", "occupant_group")
            .prefetch_related("allowed_student_groups")
            .order_by("starts_at")
        )
        for session in sessions:
            session.allowed_group_ids = set(
                session.allowed_student_groups.values_list("pk", flat=True)
            )
        return sessions

    def _redirect_back(self, request, room, semester):
        params = {}
        if room:
            params["room"] = room.pk
        if semester:
            params["semester"] = semester.pk
        url = "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
        return redirect(f"lab-head-schedule{url}")

    def _handle_create(self, request):
        room, semester = self._resolve_selection(request)
        if room is None or semester is None:
            messages.error(request, "Выберите аудиторию и семестр.")
            return redirect("lab-head-schedule")
        try:
            session = create_session(self.laboratory, request.POST, actor=request.user)
        except SessionError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Слот создан.")
            _warn_group_overflow(request, session)
        return self._redirect_back(request, room, semester)

    def _handle_update(self, request):
        room, semester = self._resolve_selection(request)
        session_pk = request.POST.get("session_pk")
        session = LabSession.objects.filter(pk=session_pk, room__laboratory=self.laboratory).first()
        if session is None:
            messages.error(request, "Слот не найден.")
            return redirect("lab-head-schedule")
        try:
            session = update_session(self.laboratory, session, request.POST, actor=request.user)
        except SessionError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Слот обновлён.")
            _warn_group_overflow(request, session)
        return self._redirect_back(request, room, semester)

    def _handle_delete(self, request):
        room, semester = self._resolve_selection(request)
        session_pk = request.POST.get("session_pk")
        session = LabSession.objects.filter(pk=session_pk, room__laboratory=self.laboratory).first()
        if session is None:
            messages.error(request, "Слот не найден.")
            return redirect("lab-head-schedule")
        try:
            cancel_session(self.laboratory, session, actor=request.user, note="Отмена в портале")
        except SessionError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Слот отменён.")
        return self._redirect_back(request, room, semester)

    def _handle_create_series(self, request):
        room, semester = self._resolve_selection(request)
        if room is None or semester is None:
            messages.error(request, "Выберите аудиторию и семестр.")
            return redirect("lab-head-schedule")
        data = {
            **request.POST,
            "room_id": room.pk,
            "semester_id": semester.pk,
        }
        try:
            result = create_series(self.laboratory, data, actor=request.user)
        except (SessionError, BulkError) as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Создано слотов: {result['created']} "
                f"(пропущено праздников: {result['skipped_holidays']}, "
                f"дублей: {result['skipped_duplicates']}).",
            )
            if result.get("overflows"):
                messages.warning(
                    request,
                    f"В {result['overflows']} слотах группа больше вместимости аудитории — "
                    "записи созданы, факт записан в аудит.",
                )
        return self._redirect_back(request, room, semester)

    def _handle_copy_day(self, request):
        room, semester = self._resolve_selection(request)
        source_date = _parse_date(request.POST.get("source_date"))
        target_date = _parse_date(request.POST.get("target_date"))
        if not all([room, semester, source_date, target_date]):
            messages.error(request, "Укажите аудиторию, семестр и обе даты.")
            return redirect("lab-head-schedule")
        try:
            result = copy_day(self.laboratory, semester, room, source_date, target_date, actor=request.user)
        except BulkError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Скопировано слотов: {result['created']} "
                f"(праздники: {result['skipped_holidays']}, дубли: {result['skipped_duplicates']}).",
            )
        return self._redirect_back(request, room, semester)

    def _handle_copy_week(self, request):
        room, semester = self._resolve_selection(request)
        source_week = _parse_date(request.POST.get("source_week"))
        target_week = _parse_date(request.POST.get("target_week"))
        if not all([room, semester, source_week, target_week]):
            messages.error(request, "Укажите аудиторию, семестр и обе недели.")
            return redirect("lab-head-schedule")
        try:
            result = copy_week(self.laboratory, semester, room, source_week, target_week, actor=request.user)
        except BulkError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Скопировано слотов: {result['created']} "
                f"(праздники: {result['skipped_holidays']}, дубли: {result['skipped_duplicates']}).",
            )
        return self._redirect_back(request, room, semester)

    def _handle_cancel_series(self, request):
        room, semester = self._resolve_selection(request)
        from_date = _parse_date(request.POST.get("from_date"))
        to_date = _parse_date(request.POST.get("to_date"))
        if semester is None or from_date is None or to_date is None:
            messages.error(request, "Укажите семестр и период.")
            return redirect("lab-head-schedule")
        lab_work = None
        lab_work_pk = request.POST.get("lab_work") or None
        if lab_work_pk:
            lab_work = self.laboratory.lab_works.filter(pk=int(lab_work_pk)).first()
        if room is None and lab_work is None:
            messages.error(request, "Укажите аудиторию или лабораторную работу.")
            return redirect("lab-head-schedule")
        force = request.POST.get("force") == "1"
        try:
            result = cancel_series(
                self.laboratory,
                semester,
                lab_work=lab_work,
                room=room,
                from_date=from_date,
                to_date=to_date,
                actor=request.user,
                force=force,
            )
        except BulkError as exc:
            messages.error(request, str(exc))
        else:
            if force:
                messages.success(
                    request,
                    f"Отменено слотов: {result['cancelled']} "
                    f"(записей: {result['booked_bookings']}, "
                    f"очередь: {result['waitlist_entries']}).",
                )
            else:
                messages.warning(
                    request,
                    f"Будет отменено слотов: {result['sessions']}, "
                    f"записей: {result['booked_bookings']}, "
                    f"очередь: {result['waitlist_entries']}. "
                    f"Подтвердите повторной отправкой.",
                )
                # Показываем форму отмены снова с флагом force
                sessions = self._sessions(room, semester)
                context = {
                    "laboratory": self.laboratory,
                    "room": room,
                    "semester": semester,
                    "grid": _session_grid(sessions),
                    "sessions": sessions,
                    "cancel_preview": result,
                    "cancel_form_data": request.POST,
                }
                if room and semester:
                    context.update(_form_context(self.laboratory, room, semester))
                return render(request, self.template_name, context)
        return self._redirect_back(request, room, semester)

    def _handle_cancel_day(self, request):
        room, semester = self._resolve_selection(request)
        day = _parse_date(request.POST.get("day"))
        if not all([room, semester, day]):
            messages.error(request, "Укажите аудиторию, семестр и день.")
            return redirect("lab-head-schedule")
        force = request.POST.get("force") == "1"
        try:
            result = cancel_day(
                self.laboratory, semester, room, day, actor=request.user, force=force
            )
        except BulkError as exc:
            messages.error(request, str(exc))
        else:
            if force:
                messages.success(
                    request,
                    f"Отменено слотов: {result['cancelled']} "
                    f"(записей: {result['booked_bookings']}, "
                    f"очередь: {result['waitlist_entries']}).",
                )
            else:
                messages.warning(
                    request,
                    f"Будет отменено слотов: {result['sessions']}, "
                    f"записей: {result['booked_bookings']}, "
                    f"очередь: {result['waitlist_entries']}. "
                    f"Подтвердите повторной отправкой.",
                )
                sessions = self._sessions(room, semester)
                context = {
                    "laboratory": self.laboratory,
                    "room": room,
                    "semester": semester,
                    "grid": _session_grid(sessions),
                    "sessions": sessions,
                    "cancel_preview": result,
                    "cancel_form_data": request.POST,
                }
                if room and semester:
                    context.update(_form_context(self.laboratory, room, semester))
                return render(request, self.template_name, context)
        return self._redirect_back(request, room, semester)
