from datetime import time

from django.db import models

from apps.academics.models import LabWork, Semester, StudentGroup
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
    """Привязка дисциплины к лаборатории с аудитом кто/когда/почему (заменяет голый M2M)."""

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
        constraints = [
            models.CheckConstraint(condition=models.Q(capacity__gte=1), name="scheduling_room_capacity_positive"),
        ]

    def __str__(self):
        if self.name:
            return f"ауд. {self.number} — {self.name}"
        return f"УЦ №{self.training_center.number}, ауд. {self.number}"


class LabSessionBookingMode(models.TextChoices):
    INDIVIDUAL = "INDIVIDUAL", "Индивидуальная запись"
    GROUP = "GROUP", "Групповая бронь"


class LabSessionGroupPart(models.TextChoices):
    ALL = "ALL", "Вся группа"
    FIRST = "FIRST", "Первая половина"
    SECOND = "SECOND", "Вторая половина"


class LabSessionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Черновик"
    OPEN = "OPEN", "Открыта"
    CLOSED = "CLOSED", "Закрыта"
    CANCELLED = "CANCELLED", "Отменена"


class LabSession(models.Model):
    lab_work = models.ForeignKey(LabWork, on_delete=models.PROTECT, related_name="sessions")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="sessions")
    semester = models.ForeignKey(Semester, on_delete=models.PROTECT, related_name="sessions")
    teacher = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="taught_sessions")
    starts_at = models.DateTimeField("Начало")
    ends_at = models.DateTimeField("Окончание")
    status = models.CharField(max_length=16, choices=LabSessionStatus.choices, default=LabSessionStatus.OPEN)
    booking_mode = models.CharField(
        "Режим бронирования",
        max_length=16,
        choices=LabSessionBookingMode.choices,
        default=LabSessionBookingMode.INDIVIDUAL,
    )
    group_part = models.CharField(
        "Часть группы",
        max_length=8,
        choices=LabSessionGroupPart.choices,
        default=LabSessionGroupPart.ALL,
    )
    allowed_student_groups = models.ManyToManyField(
        StudentGroup,
        blank=True,
        related_name="allowed_lab_sessions",
        verbose_name="Доступно группам",
    )
    occupant_group = models.ForeignKey(
        StudentGroup,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="occupied_lab_sessions",
        verbose_name="Группа-оккупант",
    )
    class Meta:
        verbose_name = "Слот лабораторной"
        verbose_name_plural = "Слоты лабораторных"
        ordering = ["starts_at"]
        indexes = [
            models.Index(fields=["starts_at", "status"]),
            models.Index(fields=["lab_work", "starts_at"]),
            models.Index(fields=["room", "starts_at", "status"]),
            models.Index(fields=["semester", "status", "starts_at"]),
            models.Index(fields=["teacher", "starts_at"]),
            models.Index(fields=["status", "starts_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["lab_work", "room", "semester", "starts_at"],
                name="scheduling_labsession_unique_slot",
                condition=~models.Q(status=LabSessionStatus.CANCELLED),
            ),
        ]

    def __str__(self):
        return f"{self.lab_work} — {self.starts_at:%d.%m.%Y %H:%M}"

    @property
    def capacity(self) -> int:
        """Мест на пару: для групповой брони — выбранная часть группы, иначе вместимость ЛР."""
        if self.booking_mode == LabSessionBookingMode.GROUP and self.occupant_group_id:
            return len(self.occupant_group.student_ids_for_part(self.group_part))
        return self.lab_work.capacity

    @property
    def booked_count(self):
        from apps.bookings.models import BookingStatus

        return self.bookings.filter(current_status=BookingStatus.BOOKED).count()

    @property
    def available_seats(self) -> int:
        """Свободные места с учётом лимитов аудитории/ЛР/стенда (seat_capacity)."""
        from apps.bookings.services.seat_capacity import session_available_seats

        return session_available_seats(self)

    def is_stand_blocked(self) -> bool:
        """Занят ли стенд: чужая ЛР использует тот же стенд или в комнате кончились стенды этой ЛР."""
        stand_ids = list(self.lab_work.stands.values_list("id", flat=True))
        if not stand_ids:
            return False
        overlapping = (
            LabSession.objects.filter(
                starts_at__lt=self.ends_at,
                ends_at__gt=self.starts_at,
            )
            .exclude(pk=self.pk)
            .exclude(status=LabSessionStatus.CANCELLED)
        )
        if (
            overlapping.exclude(lab_work_id=self.lab_work_id)
            .filter(lab_work__stands__id__in=stand_ids)
            .exists()
        ):
            return True
        stands_in_room = self.lab_work.stands.filter(room_id=self.room_id).count()
        if stands_in_room == 0:
            return True
        parallel = overlapping.filter(lab_work_id=self.lab_work_id, room_id=self.room_id).count()
        return parallel >= stands_in_room


class Holiday(models.Model):
    date = models.DateField("Дата", unique=True)
    name = models.CharField("Название", max_length=128, blank=True)

    class Meta:
        verbose_name = "Праздничный день"
        verbose_name_plural = "Праздничные дни"
        ordering = ["date"]

    def __str__(self):
        return f"{self.date} {self.name}".strip()


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
    capacity = models.PositiveIntegerField("Вместимость", default=3)
    is_published = models.BooleanField("Опубликован", default=True)

    class Meta:
        verbose_name = "Лабораторный стенд"
        verbose_name_plural = "Лабораторные стенды"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=models.Q(capacity__gte=1), name="scheduling_labstand_capacity_positive"),
        ]

    def __str__(self):
        return f"{self.name} ({self.inventory_number})"



