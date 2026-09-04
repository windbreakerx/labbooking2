from datetime import time

from django.db import models

from apps.academics.models import (
    ALLOWED_LAB_DURATIONS,
    ALLOWED_LAB_DURATIONS_CHOICES,
    LabWork,
    Semester,
)
from apps.users.models import User


class TrainingCenter(models.Model):
    number = models.PositiveIntegerField("УЦ №", unique=True)
    name = models.CharField("Название", max_length=128, blank=True)

    class Meta:
        verbose_name = "Учебный центр"
        verbose_name_plural = "Учебные центры"

    def __str__(self):
        return f"УЦ №{self.number} — {self.name}" if self.name else f"УЦ №{self.number}"


class LaboratoryType(models.TextChoices):
    REGULAR = "REGULAR", "Кафедральная"
    INTERDEPT = "INTERDEPT", "Межкафедральная"
    COMPLEX = "COMPLEX", "Комплексная"


class Laboratory(models.Model):
    training_center = models.ForeignKey(TrainingCenter, on_delete=models.CASCADE, related_name="laboratories", verbose_name="Учебный центр")
    faculty = models.ForeignKey("academics.Faculty", on_delete=models.SET_NULL, null=True, blank=True, related_name="laboratories", verbose_name="Факультет")
    name = models.CharField("Название", max_length=256)
    short_name = models.CharField("Краткое название", max_length=64, blank=True)
    lab_type = models.CharField("Тип лаборатории", max_length=16, choices=LaboratoryType.choices, default=LaboratoryType.REGULAR)

    class Meta:
        verbose_name = "Лаборатория"
        verbose_name_plural = "Лаборатории"
        ordering = ["training_center", "name"]
        unique_together = [("training_center", "name")]

    def __str__(self):
        return self.name


class LabDisciplineBinding(models.Model):
    """Привязка дисциплины к лаборатории — с аудитом кто/когда/почему (заменяет голый M2M).

    Завлаб бинда к дисциплине своего факультета; ``is_override=True`` — вне факультета,
    только SYS_ADMIN, требует ``reason`` (BindingService, День 4).
    """

    laboratory = models.ForeignKey(Laboratory, on_delete=models.CASCADE, related_name="discipline_bindings", verbose_name="Лаборатория")
    discipline = models.ForeignKey("academics.Discipline", on_delete=models.CASCADE, related_name="laboratory_bindings", verbose_name="Дисциплина")
    bound_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="lab_discipline_bindings", verbose_name="Кто привязал")
    is_override = models.BooleanField("Несоответствующая привязка", default=False)
    reason = models.CharField("Причина", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        verbose_name = "Привязка дисциплины к лаборатории"
        verbose_name_plural = "Привязки дисциплин к лабораториям"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["laboratory", "discipline"], name="scheduling_labdisciplinebinding_unique"),
            models.CheckConstraint(condition=~models.Q(is_override=True) | ~models.Q(reason=""), name="scheduling_labdisciplinebinding_override_requires_reason"),
        ]

    def __str__(self):
        return f"{self.laboratory} ↔ {self.discipline}"


class Room(models.Model):
    training_center = models.ForeignKey(TrainingCenter, on_delete=models.CASCADE, related_name="rooms")
    number = models.CharField("Аудитория №", max_length=32)
    name = models.CharField("Название", max_length=256, blank=True)
    photo = models.ImageField("Фотография", upload_to="rooms/", blank=True)
    capacity = models.PositiveIntegerField("Вместимость", default=30)
    is_blocked = models.BooleanField("Заблокирована", default=False)
    laboratory = models.ForeignKey(Laboratory, on_delete=models.SET_NULL, null=True, blank=True, related_name="rooms", verbose_name="Лаборатория")
    disciplines = models.ManyToManyField("academics.Discipline", blank=True, related_name="rooms", verbose_name="Дисциплины")
    default_lab_staff = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="default_staff_rooms",
        verbose_name="Сотрудник лаборатории по умолчанию", limit_choices_to={"role__in": ["LAB_ADMIN", "LAB_HEAD"]},
    )

    class Meta:
        verbose_name = "Аудитория"
        verbose_name_plural = "Аудитории"
        unique_together = [("training_center", "number")]

    def __str__(self):
        if self.name:
            return f"ауд. {self.number} — {self.name}"
        return f"УЦ №{self.training_center.number}, ауд. {self.number}"


class LabSessionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Черновик"
    OPEN = "OPEN", "Открыта"
    CLOSED = "CLOSED", "Закрыта"
    CANCELLED = "CANCELLED", "Отменена"


class LabSession(models.Model):
    lab_work = models.ForeignKey(LabWork, on_delete=models.CASCADE, related_name="sessions")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="sessions")
    semester = models.ForeignKey(Semester, on_delete=models.PROTECT, related_name="sessions")
    teacher = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="taught_sessions")
    starts_at = models.DateTimeField("Начало")
    ends_at = models.DateTimeField("Окончание")
    capacity = models.PositiveIntegerField("Мест")
    status = models.CharField(max_length=16, choices=LabSessionStatus.choices, default=LabSessionStatus.OPEN)

    class Meta:
        verbose_name = "Слот лабораторной"
        verbose_name_plural = "Слоты лабораторных"
        ordering = ["starts_at"]
        indexes = [models.Index(fields=["starts_at", "status"]), models.Index(fields=["lab_work", "starts_at"])]

    def __str__(self):
        return f"{self.lab_work} — {self.starts_at:%d.%m.%Y %H:%M}"

    @property
    def booked_count(self):
        # available_seats (учитывает лимиты стенда/waitlist) добавляется в День 3 вместе с SessionAvailabilityService.
        from apps.bookings.models import BookingStatus

        return self.bookings.filter(current_status=BookingStatus.BOOKED).count()

    def is_stand_blocked_by_other_lab_work(self) -> bool:
        stand_id = self.lab_work.primary_stand_id
        if not stand_id:
            return False
        from apps.bookings.models import Booking, BookingStatus

        return Booking.objects.filter(
            current_status=BookingStatus.BOOKED, lab_session__lab_work__primary_stand_id=stand_id,
            lab_session__starts_at__lt=self.ends_at, lab_session__ends_at__gt=self.starts_at,
        ).exclude(lab_session__lab_work_id=self.lab_work_id).exists()


class Holiday(models.Model):
    date = models.DateField("Дата", unique=True)
    name = models.CharField("Название", max_length=128, blank=True)

    class Meta:
        verbose_name = "Праздничный день"
        verbose_name_plural = "Праздничные дни"
        ordering = ["date"]

    def __str__(self):
        return f"{self.date} {self.name}".strip()


class WeekParity(models.TextChoices):
    ODD = "ODD", "Нечётная"
    EVEN = "EVEN", "Чётная"
    BOTH = "BOTH", "Каждую неделю"


class ScheduleDutyRole(models.TextChoices):
    LAB_STAFF_DUTY = "LAB_STAFF_DUTY", "Дежурство сотрудника лаборатории"
    TEACHER_DUTY = "TEACHER_DUTY", "Дежурство преподавателя"
    OTHER = "OTHER", "Другое"


SCHEDULE_SLOT_DURATION_MINUTES = 90


class AutoVisitedMode(models.TextChoices):
    MANUAL = "MANUAL", "отсутствует"
    AT_22_00 = "AT_22_00", "В 22:00 текущего дня"
    AT_09_00_NEXT = "AT_09_00_NEXT", "В 9:00 следующего дня"


class LaboratoryBookingSettings(models.Model):
    laboratory = models.OneToOneField(Laboratory, on_delete=models.CASCADE, related_name="booking_settings")
    booking_horizon_days = models.PositiveSmallIntegerField("Горизонт записи (дни)", null=True, blank=True)
    booking_day_opens_at = models.TimeField("Открытие нового дня", null=True, blank=True)
    booking_day_closes_at = models.TimeField("Закрытие ближнего дня", null=True, blank=True)
    booking_cancel_hours = models.PositiveSmallIntegerField("Отмена за N часов", null=True, blank=True)
    restriction_base_time = models.TimeField("Базовое время ограничения", default=time(9, 0))
    restriction_hours_before_base = models.PositiveSmallIntegerField("Ограничение записи за N часов до базового времени", null=True, blank=True, help_text="NULL = ограничение отсутствует")
    auto_visited_mode = models.CharField("Автопроставление «Посетил»", max_length=16, choices=AutoVisitedMode.choices, default=AutoVisitedMode.MANUAL)

    class Meta:
        verbose_name = "Настройки записи лаборатории"
        verbose_name_plural = "Настройки записи лабораторий"

    def __str__(self):
        return f"Настройки записи: {self.laboratory}"


class LabStand(models.Model):
    name = models.CharField("Наименование", max_length=256)
    inventory_number = models.CharField("Инвентарный номер", max_length=64)
    training_center = models.ForeignKey(TrainingCenter, on_delete=models.CASCADE, related_name="stands")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="stands")
    description = models.TextField("Описание", blank=True)
    photo = models.ImageField("Фотография", upload_to="stands/", blank=True)
    is_published = models.BooleanField("Опубликован", default=True)

    class Meta:
        verbose_name = "Лабораторный стенд"
        verbose_name_plural = "Лабораторные стенды"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.inventory_number})"


class ScheduleEntry(models.Model):
    """Еженедельный шаблон слота расписания.

    Что читается в слоте определяется через ``disciplines`` (M2M-through
    ``ScheduleEntryDisciplineSelection``) — единая точка правды вместо параллельных
    singular lab_work/discipline + голого M2M lab_works, как было в v1.
    """

    duty_role = models.CharField("Тип дежурства", max_length=32, choices=ScheduleDutyRole.choices, default=ScheduleDutyRole.LAB_STAFF_DUTY)
    disciplines = models.ManyToManyField("academics.Discipline", through="ScheduleEntryDisciplineSelection", related_name="schedule_entries", blank=True, verbose_name="Дисциплины")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="schedule_entries")
    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name="schedule_entries")
    week_parity = models.CharField(max_length=8, choices=WeekParity.choices, default=WeekParity.BOTH)
    weekday = models.PositiveSmallIntegerField("День недели (0=Пн)")
    start_time = models.TimeField("Время начала")
    duration_minutes = models.PositiveIntegerField("Длительность (мин)", default=90, choices=ALLOWED_LAB_DURATIONS_CHOICES)
    capacity = models.PositiveIntegerField("Мест", default=30)
    load_group_label = models.CharField("Группа по нагрузке", max_length=128, blank=True)
    manual_title = models.CharField("Ручное описание", max_length=256, blank=True)
    duty_person = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="duty_schedule_entries")
    teacher = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="schedule_entries")
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Запись расписания"
        verbose_name_plural = "Расписание"
        ordering = ["weekday", "start_time"]
        constraints = [models.CheckConstraint(condition=models.Q(duration_minutes__in=ALLOWED_LAB_DURATIONS), name="scheduling_scheduleentry_allowed_duration")]

    def __str__(self):
        first_discipline = self.disciplines.order_by("pk").first()
        label = first_discipline.title if first_discipline else self.manual_title
        return f"{label or 'Слот'} — день {self.weekday} {self.start_time}"


class ScheduleEntryDisciplineSelection(models.Model):
    """Дисциплина слота + свой набор ЛР (для комплексных слотов с несколькими дисциплинами)."""

    schedule_entry = models.ForeignKey(ScheduleEntry, on_delete=models.CASCADE, related_name="discipline_selections")
    discipline = models.ForeignKey("academics.Discipline", on_delete=models.CASCADE, related_name="schedule_entry_selections")
    lab_works = models.ManyToManyField(LabWork, blank=True, related_name="schedule_entry_discipline_selections", verbose_name="Лабораторные работы дисциплины в слоте")

    class Meta:
        verbose_name = "Дисциплина слота расписания"
        verbose_name_plural = "Дисциплины слотов расписания"
        constraints = [models.UniqueConstraint(fields=["schedule_entry", "discipline"], name="scheduling_unique_schedule_entry_discipline_selection")]

    def __str__(self):
        return f"{self.schedule_entry_id}: {self.discipline.title}"
