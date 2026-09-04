from django.db import models

from apps.academics.models import Discipline, LabWork
from apps.scheduling.models import Laboratory, LabSession, Room, TrainingCenter
from apps.users.models import User

# Sticky no-shows within one laboratory before an explanatory note is required.
NO_SHOW_EXPLANATION_THRESHOLD = 3


class BookingStatus(models.TextChoices):
    BOOKED = "BOOKED", "Записан"
    NO_SHOW = "NO_SHOW", "Неявка"
    CANCELLED = "CANCELLED", "Отменил запись"
    SLOT_CANCELLED = "SLOT_CANCELLED", "Слот отменён"
    REACCESS = "REACCESS", "Повторный доступ"
    VISITED = "VISITED", "Посетил"


class CancelSource(models.TextChoices):
    STUDENT = "STUDENT", "Студент"
    STAFF = "STAFF", "Сотрудник"


class RegistrationType(models.TextChoices):
    AUTO = "AUTO", "Автоматическая"
    MANUAL = "MANUAL", "Не автоматическая"


class Booking(models.Model):
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name="bookings")
    lab_session = models.ForeignKey(LabSession, on_delete=models.CASCADE, related_name="bookings")
    lab_work = models.ForeignKey(LabWork, on_delete=models.CASCADE, related_name="bookings")
    discipline = models.ForeignKey(Discipline, on_delete=models.CASCADE, related_name="bookings")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="bookings")
    scheduled_at = models.DateTimeField("Дата и время ЛР")
    current_status = models.CharField(max_length=16, choices=BookingStatus.choices, default=BookingStatus.BOOKED, help_text="Текущее состояние строки записи.")
    had_no_show = models.BooleanField(
        "Была неявка", default=False,
        help_text="Факт засчитанной неявки (слой 2): ставится при первом NO_SHOW, сохраняется после "
        "REACCESS, снимается только исправлением NO_SHOW → VISITED. Нужен для отчётов и аналитики.",
    )
    cancel_source = models.CharField("Кто отменил запись", max_length=16, choices=CancelSource.choices, null=True, blank=True, help_text="Заполняется только для статуса CANCELLED: студент или сотрудник.")
    registration_type = models.CharField(max_length=16, choices=RegistrationType.choices, default=RegistrationType.AUTO)
    registered_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="registered_bookings")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Запись"
        verbose_name_plural = "Записи"
        ordering = ["-scheduled_at"]
        indexes = [
            models.Index(fields=["student", "discipline", "current_status"]),
            models.Index(fields=["scheduled_at", "current_status"]),
        ]

    def __str__(self):
        return f"{self.student.email} — {self.lab_work} ({self.current_status})"

    @property
    def is_repeat_after_no_show(self) -> bool:
        """True when the booking is active and the student has a past no-show for the same lab work."""
        if self.current_status != BookingStatus.BOOKED or not self.student_id or not self.lab_work_id:
            return False
        qs = Booking.objects.filter(student_id=self.student_id, lab_work_id=self.lab_work_id, had_no_show=True)
        return qs.exclude(pk=self.pk).exists()


class BookingStatusHistory(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="status_history")
    status = models.CharField(max_length=16, choices=BookingStatus.choices)
    changed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)
    note = models.TextField(blank=True)
    acknowledged_at = models.DateTimeField("Просмотрено студентом", null=True, blank=True, help_text="Когда студент просмотрел уведомление об изменении статуса.")

    class Meta:
        verbose_name = "История статуса"
        verbose_name_plural = "История статусов"
        ordering = ["changed_at"]
        indexes = [models.Index(fields=["booking", "acknowledged_at"], name="bookings_bo_booking_4a8f21_idx")]

    def __str__(self):
        return f"{self.booking_id} → {self.status}"


class StudentLabAttendance(models.Model):
    """Per-laboratory no-show counter and explanation flag for a student.

    Счётчик неявок в лаборатории и флаг объяснительной (≥3). Сам по себе запись не
    блокирует — только информирует сотрудников и показывает студенту уведомление.
    """

    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name="lab_attendance")
    laboratory = models.ForeignKey(Laboratory, on_delete=models.CASCADE, related_name="student_attendance")
    no_show_count = models.PositiveIntegerField("Количество неявок", default=0, help_text="Сколько раз студенту ставили неявку в этой лаборатории (сохраняется для аналитики).")
    explanation_required = models.BooleanField(
        "Требуется объяснительная", default=False,
        help_text=f"True при {NO_SHOW_EXPLANATION_THRESHOLD}+ неявках в этой лаборатории. Снимается сотрудником "
        "после получения объяснительной; счётчик неявок при этом не сбрасывается.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Посещаемость по лаборатории"
        verbose_name_plural = "Посещаемость по лабораториям"
        constraints = [models.UniqueConstraint(fields=["student", "laboratory"], name="uniq_student_lab_attendance")]
        indexes = [models.Index(fields=["laboratory", "explanation_required"])]

    def __str__(self):
        return f"{self.student_id} @ {self.laboratory_id}: {self.no_show_count}"


class WaitlistEntry(models.Model):
    lab_session = models.ForeignKey(LabSession, on_delete=models.CASCADE, related_name="waitlist")
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name="waitlist_entries")
    position = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Очередь"
        verbose_name_plural = "Очередь"
        unique_together = [("lab_session", "student")]
        ordering = ["position"]

    def __str__(self):
        return f"#{self.position} {self.student.email}"


class AuditLog(models.Model):
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=64)
    entity_type = models.CharField(max_length=64)
    entity_id = models.PositiveIntegerField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Аудит"
        verbose_name_plural = "Аудит"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.entity_type}:{self.entity_id}"


class SupportTicket(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Открыт"
        IN_PROGRESS = "IN_PROGRESS", "В работе"
        RESOLVED = "RESOLVED", "Отвечено"
        CLOSED = "CLOSED", "Закрыт"

    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name="support_tickets")
    training_center = models.ForeignKey(TrainingCenter, on_delete=models.PROTECT, related_name="support_tickets", null=True, blank=True)
    subject = models.CharField(max_length=256)
    body = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Обращение"
        verbose_name_plural = "Обращения"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.subject} ({self.get_status_display()})"


class SupportMessage(models.Model):
    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
