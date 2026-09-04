from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email обязателен")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", UserRole.SYS_ADMIN)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class UserRole(models.TextChoices):
    STUDENT = "STUDENT", "Студент"
    TEACHER = "TEACHER", "Преподаватель"
    LAB_HEAD = "LAB_HEAD", "Заведующий лабораторией"
    LAB_ADMIN = "LAB_ADMIN", "Сотрудник лаборатории"
    SYS_ADMIN = "SYS_ADMIN", "Системный администратор"


class User(AbstractUser):
    username = None
    email = models.EmailField("Email", unique=True)
    role = models.CharField(max_length=20, choices=UserRole.choices, default=UserRole.STUDENT)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    class Meta:
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"

    def __str__(self):
        return self.email

    @property
    def full_name(self):
        return f"{self.last_name} {self.first_name}".strip() or self.email

    @staticmethod
    def _given_name_parts(first_name: str) -> list[str]:
        return [part for part in (first_name or "").split() if part]

    @property
    def greeting_name(self):
        """Имя и отчество для приветствия на главной (без фамилии)."""
        parts = self._given_name_parts(self.first_name)
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1]}"
        return parts[0] if parts else self.email

    @property
    def header_name(self):
        """Фамилия и инициалы для user-box в шапке, напр. «Майоров И. В.»."""
        last = (self.last_name or "").strip()
        parts = self._given_name_parts(self.first_name)
        if not last and not parts:
            return self.email
        initials = "".join(f" {part[0]}." for part in parts[:2] if part)
        return f"{last}{initials}".strip() if last else (initials.strip() or self.email)


class UserProfile(models.Model):
    class Gender(models.TextChoices):
        MALE = "M", "Мужской"
        FEMALE = "F", "Женский"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    student_id = models.CharField("ID студента", max_length=64, blank=True)
    group_name = models.CharField("Группа", max_length=64, blank=True)
    student_group = models.ForeignKey("academics.StudentGroup", on_delete=models.SET_NULL, null=True, blank=True, related_name="students", verbose_name="Учебная группа")
    faculty = models.CharField("Факультет", max_length=128, blank=True)
    gender = models.CharField(max_length=1, choices=Gender.choices, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    dekanat_id = models.CharField("ID в Деканате", max_length=64, blank=True)
    training_center = models.ForeignKey("scheduling.TrainingCenter", on_delete=models.SET_NULL, null=True, blank=True, related_name="staff_profiles", verbose_name="Учебный центр")
    laboratory = models.ForeignKey("scheduling.Laboratory", on_delete=models.SET_NULL, null=True, blank=True, related_name="staff_profiles", verbose_name="Лаборатория")
    department = models.ForeignKey("academics.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="staff_profiles", verbose_name="Кафедра")
    position = models.CharField("Должность", max_length=256, blank=True)
    disciplines = models.ManyToManyField("academics.Discipline", blank=True, related_name="staff_profiles", verbose_name="Дисциплины сотрудника")

    class Meta:
        verbose_name = "Профиль"
        verbose_name_plural = "Профили"

    def __str__(self):
        return f"Профиль {self.user.email}"
