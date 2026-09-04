from django.core.validators import FileExtensionValidator
from django.db import models

ALLOWED_LAB_DURATIONS = (30, 45, 60, 90)
ALLOWED_LAB_DURATIONS_CHOICES = tuple((value, f"{value} мин") for value in ALLOWED_LAB_DURATIONS)


class Semester(models.Model):
    name = models.CharField("Название", max_length=128)
    start_date = models.DateField("Дата начала")
    end_date = models.DateField("Дата окончания")
    is_active = models.BooleanField("Активный", default=False)

    class Meta:
        verbose_name = "Семестр"
        verbose_name_plural = "Семестры"
        ordering = ["-start_date"]

    def __str__(self):
        return self.name


class Faculty(models.Model):
    code = models.CharField("Код", max_length=16, unique=True)
    title = models.CharField("Название", max_length=256)
    ordering = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "Факультет"
        verbose_name_plural = "Факультеты"
        ordering = ["ordering", "title"]

    def __str__(self):
        return self.title


class StudentGroup(models.Model):
    name = models.CharField("Группа", max_length=64, unique=True)
    faculty = models.CharField("Факультет", max_length=128, blank=True)
    department = models.ForeignKey("Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="student_groups", verbose_name="Кафедра")
    dekanat_id = models.CharField("ID в Деканате", max_length=64, blank=True)
    disciplines = models.ManyToManyField("Discipline", blank=True, related_name="student_groups", verbose_name="Дисциплины учебного плана")
    lab_works = models.ManyToManyField("LabWork", blank=True, related_name="student_groups", verbose_name="Лабораторные работы учебного плана")

    class Meta:
        verbose_name = "Учебная группа"
        verbose_name_plural = "Учебные группы"
        ordering = ["name"]

    def __str__(self):
        return self.name


class GroupDisciplineLoadSource(models.TextChoices):
    IMPORTED_WORKLOAD = "IMPORTED_WORKLOAD", "Импорт нагрузки"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT", "Ручная корректировка"


class GroupDisciplineLoad(models.Model):
    group = models.ForeignKey(StudentGroup, on_delete=models.CASCADE, related_name="discipline_loads", verbose_name="Учебная группа")
    semester = models.ForeignKey("Semester", on_delete=models.CASCADE, related_name="group_discipline_loads", verbose_name="Семестр")
    discipline = models.ForeignKey("Discipline", on_delete=models.CASCADE, related_name="group_loads", verbose_name="Дисциплина")
    source = models.CharField("Источник", max_length=32, choices=GroupDisciplineLoadSource.choices, default=GroupDisciplineLoadSource.IMPORTED_WORKLOAD)
    is_active = models.BooleanField("Активно", default=True)
    import_batch_id = models.CharField("Пакет импорта", max_length=64, blank=True)
    note = models.CharField("Комментарий", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Нагрузка группы по дисциплине"
        verbose_name_plural = "Нагрузка групп по дисциплинам"
        ordering = ["group__name", "discipline__title"]
        constraints = [models.UniqueConstraint(fields=["group", "semester", "discipline"], name="academics_group_discipline_load_unique")]
        indexes = [
            models.Index(fields=["semester", "is_active"]), models.Index(fields=["group", "semester"]),
            models.Index(fields=["discipline", "semester"]),
        ]

    def __str__(self):
        return f"{self.group.name} / {self.semester.name} / {self.discipline.title}"


class Department(models.Model):
    title = models.CharField("Название", max_length=256, unique=True)
    short_code = models.CharField("Короткий код", max_length=16, blank=True)
    faculty = models.ForeignKey(Faculty, on_delete=models.SET_NULL, null=True, blank=True, related_name="departments", verbose_name="Факультет")
    ordering = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "Кафедра"
        verbose_name_plural = "Кафедры"
        ordering = ["ordering", "title"]

    def __str__(self):
        return self.title


class Discipline(models.Model):
    code = models.CharField("Код", max_length=32, blank=True)
    short_code = models.CharField("Короткий код", max_length=16, blank=True)
    title = models.CharField("Название", max_length=256)
    description = models.TextField("Описание", blank=True)
    is_published = models.BooleanField("Опубликовано", default=True)
    dekanat_id = models.CharField("ID в Деканате", max_length=64, blank=True)
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True, related_name="disciplines", verbose_name="Кафедра")
    semester = models.ForeignKey(Semester, on_delete=models.CASCADE, related_name="disciplines", null=True, blank=True)
    # through=LabDisciplineBinding: аудит кто/когда/почему привязал (override требует reason).
    # Заменяет голый M2M и legacy Discipline.training_centers — учебный центр дисциплины
    # теперь выводится через laboratory.training_center.
    laboratories = models.ManyToManyField("scheduling.Laboratory", through="scheduling.LabDisciplineBinding", blank=True, related_name="disciplines", verbose_name="Лаборатории")

    class Meta:
        verbose_name = "Дисциплина"
        verbose_name_plural = "Дисциплины"
        ordering = ["title"]

    def __str__(self):
        return self.title


class LabWork(models.Model):
    disciplines = models.ManyToManyField(Discipline, related_name="lab_works", verbose_name="Дисциплины")
    number = models.PositiveIntegerField("Номер", default=1)
    code = models.CharField("Код ЛР", max_length=64, blank=True, null=True)
    title = models.CharField("Название", max_length=256)
    description = models.TextField("Описание", blank=True)
    duration_minutes = models.PositiveIntegerField("Длительность (мин)", default=90, choices=ALLOWED_LAB_DURATIONS_CHOICES)
    capacity = models.PositiveIntegerField("Макс. мест", default=30)
    is_published = models.BooleanField("Опубликовано", default=True)
    training_centers = models.ManyToManyField("scheduling.TrainingCenter", blank=True, related_name="lab_works", verbose_name="Учебные центры")
    laboratories = models.ManyToManyField("scheduling.Laboratory", blank=True, related_name="lab_works", verbose_name="Лаборатории")
    default_room = models.ForeignKey("scheduling.Room", on_delete=models.SET_NULL, null=True, blank=True, related_name="default_lab_works", verbose_name="Аудитория по умолчанию")
    primary_stand = models.ForeignKey("scheduling.LabStand", on_delete=models.SET_NULL, null=True, blank=True, related_name="primary_for_lab_works", verbose_name="Основной стенд")

    class Meta:
        verbose_name = "Лабораторная работа"
        verbose_name_plural = "Лабораторные работы"
        ordering = ["number", "title"]
        constraints = [
            models.CheckConstraint(condition=models.Q(duration_minutes__in=ALLOWED_LAB_DURATIONS), name="academics_labwork_allowed_duration"),
            models.UniqueConstraint(fields=["code"], condition=models.Q(code__isnull=False), name="academics_labwork_code_unique_not_null"),
        ]

    def __str__(self):
        titles = ", ".join(self.disciplines.values_list("title", flat=True)[:3])
        return f"ЛР {self.number}: {self.title} ({titles})" if titles else f"ЛР {self.number}: {self.title}"


class GroupLabWorkOverrideMode(models.TextChoices):
    OPEN = "OPEN", "Открыть"
    HIDDEN = "HIDDEN", "Скрыть"


class GroupLabWorkOverride(models.Model):
    group = models.ForeignKey(StudentGroup, on_delete=models.CASCADE, related_name="lab_work_overrides", verbose_name="Учебная группа")
    semester = models.ForeignKey("Semester", on_delete=models.CASCADE, related_name="group_lab_work_overrides", verbose_name="Семестр")
    lab_work = models.ForeignKey("LabWork", on_delete=models.CASCADE, related_name="group_overrides", verbose_name="Лабораторная работа")
    mode = models.CharField("Режим", max_length=16, choices=GroupLabWorkOverrideMode.choices)
    is_active = models.BooleanField("Активно", default=True)
    reason = models.CharField("Причина", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Исключение по ЛР для группы"
        verbose_name_plural = "Исключения по ЛР для групп"
        constraints = [models.UniqueConstraint(fields=["group", "semester", "lab_work"], name="academics_group_lab_work_override_unique")]
        indexes = [models.Index(fields=["semester", "is_active"]), models.Index(fields=["group", "semester"])]

    def __str__(self):
        return f"{self.group.name} / {self.semester.name} / {self.lab_work} / {self.mode}"


class LabWorkMethodics(models.Model):
    lab_work = models.ForeignKey(LabWork, on_delete=models.CASCADE, related_name="methodics_files", verbose_name="Лабораторная работа")
    file = models.FileField("PDF", upload_to="methodics/", validators=[FileExtensionValidator(allowed_extensions=["pdf"])])
    title = models.CharField("Название", max_length=256, blank=True)
    uploaded_at = models.DateTimeField("Загружено", auto_now_add=True)

    class Meta:
        verbose_name = "Методичка"
        verbose_name_plural = "Методички"
        ordering = ["uploaded_at", "pk"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self) -> str:
        return self.title or self.file.name.rsplit("/", 1)[-1]
