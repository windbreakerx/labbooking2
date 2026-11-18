# План реализации редизайна LabBooking

> Документ зафиксирован после обсуждения архитектуры. Базовый дизайн — `docs/DESIGN_SCHEDULING.md`.
>
> **Обновление 18.11.2026.** Вместимость слота больше не хранится: мест на пару = `LabWork.capacity` (для групповой брони — размер группы). Стенды задаются у ЛР набором (`LabWork.stands`), а не у слота. Актуальные решения и инварианты — `docs/PLAN_BOOKING_CORE_2026-11-18.md`; состояние работ — `docs/WORKLOG_BOOKING_CORE_2026-11-18.md`. Разделы ниже, где упоминаются `used_stands` и `LabSession.capacity`, считаются историческими.

---

## 1. Цель

Перейти от двойного представления расписания (`ScheduleEntry` + `LabSession`) к **одному источнику правды** — прямому календарю слотов (`LabSession`).

Дополнительно:
- поддержка стендов с разной вместимостью;
- групповые брони, полностью занимающие аудиторию;
- лабораторные работы, требующие нескольких посещений;
- автопосещение через `django-q2`;
- отчётность и аналитика.

---

## 2. Итоговая модель данных

### `LabWork` (`apps/academics`)

```python
capacity = models.PositiveIntegerField("Макс. мест", default=3)  # мест на одну пару
required_sessions = models.PositiveIntegerField("Требуется занятий", default=1)
stands = models.ManyToManyField("scheduling.LabStand", blank=True)  # одинаковые стенды ЛР
is_archived = models.BooleanField(default=False)
```

### `LabStand` (`apps/scheduling`)

```python
capacity = models.PositiveIntegerField("Вместимость", default=3)
```

### `LabSession` (`apps/scheduling`)

```python
class LabSessionBookingMode(models.TextChoices):
    INDIVIDUAL = "INDIVIDUAL", "Индивидуальная запись"
    GROUP = "GROUP", "Групповая бронь"

lab_work = models.ForeignKey(LabWork, ...)
room = models.ForeignKey(Room, ...)
semester = models.ForeignKey(Semester, ...)
teacher = models.ForeignKey(User, null=True, blank=True, ...)

# Режим бронирования
booking_mode = models.CharField(choices=LabSessionBookingMode.choices, default=INDIVIDUAL)

# Для INDIVIDUAL: если пусто — слот для всех; иначе только для этих групп
allowed_student_groups = models.ManyToManyField(StudentGroup, blank=True)

# Для GROUP: группа, которая полностью занимает слот
occupant_group = models.ForeignKey(
    StudentGroup, on_delete=models.PROTECT, null=True, blank=True
)

starts_at = models.DateTimeField()
ends_at = models.DateTimeField()
status = models.CharField(choices=LabSessionStatus.choices, default=OPEN)
```

**Вместимость слота:**
- в базе не хранится;
- групповая бронь — `occupant_group.students.count()`;
- обычная — `lab_work.capacity`;
- аудитория ограничивает всегда, стенды проверяются по `LabWork.stands`.

### `Booking` (`apps/bookings`)

```python
class RegistrationType(models.TextChoices):
    AUTO = "AUTO", "Автоматическая"
    MANUAL = "MANUAL", "Ручная"
    GROUP = "GROUP", "Групповая бронь"
```

### `LabWorkBookingRelease` — новая модель в `apps/academics`

```python
lab_work = models.ForeignKey(LabWork, ...)
semester = models.ForeignKey(Semester, ...)
booking_opens_at = models.DateTimeField(null=True, blank=True)

class Meta:
    constraints = [
        models.UniqueConstraint(fields=["lab_work", "semester"], name="...")
    ]
```

- отсутствие строки = запись закрыта;
- `NULL` внутри строки = закрыта;
- дата в будущем = откроется автоматически;
- дата в прошлом/сейчас = открыта.

### Удаляемые сущности

- `ScheduleEntry`
- `ScheduleEntryDisciplineSelection`
- `WeekParity`
- `ScheduleDutyRole`
- `Room.is_excluded_from_autogen`
- `schedule_sync.py`, `slot_generation.py`, `entry_match.py`, `schedule_admin.py`

---

## 3. Итоговые бизнес-правила

| Правило | Решение |
|---|---|
| **Стенды** | Набор одинаковых стендов у ЛР (`LabWork.stands`); в комнате параллельных слотов одной ЛР не больше, чем её стендов здесь |
| **Студент выбирает стенд** | Нет, записывается на слот |
| **Конфликт стендов** | Проверка в сервисе + `select_for_update` на конфликтующие слоты |
| **Групповой слот** | Занимает всю аудиторию; capacity = размер группы; auto-book всех студентов группы |
| **Броней на одну ЛР** | До `LabWork.required_sessions` активных (`BOOKED`) |
| **Броней на одну дисциплину** | Одна активная |
| **Выполнение ЛР** | `VISITED` ≥ `required_sessions` |
| **No-show** | Отслеживается отдельно; `REACCESS` — отдельная бронь |
| **Порядок сессий** | Не важен, общий счётчик |
| **Ручная запись staff** | Обходит release, групповые ограничения и окно записи |
| **Автопосещение** | Ежедневно в 20:00 МСК через `django-q2`; для `BOOKED` и `REACCESS` |
| **Архивация** | В начале осеннего семестра архивируем `CANCELLED`-слоты и старый `AuditLog` |

### Групповая бронь

- Создаётся завлабом через портал.
- `booking_mode = GROUP`, `occupant_group` обязателен.
- `capacity = occupant_group.students.count()`.
- При сохранении создаются `Booking` для всех студентов группы с `registration_type=GROUP`.
- Другие слоты в это время в этой аудитории ограничены её вместимостью, а не запрещены целиком.
- Посещение проставляется: автоматически, преподавателем или лаборантом (для групповых — обычно преподаватель/авто).

### Многосессионные ЛР

- `LabWork.required_sessions` — сколько раз студент должен посетить ЛР.
- Студент может иметь до `required_sessions` активных броней на эту ЛР одновременно.
- Групповой слот — это одна из сессий; для ЛР с `required_sessions=3` нужно создать 3 групповых слота.

---

## 4. План реализации

### Фаза 1. Модели и миграции

- [x] Изменить `LabWork.capacity` default → 3.
- [x] Добавить `LabWork.required_sessions`.
- [x] Добавить `LabStand.capacity`.
- [x] Добавить в `LabSession`:
  - `booking_mode`,
  - `allowed_student_groups` (M2M),
  - `occupant_group` (FK),
  - `used_stands` (M2M).
- [x] Добавить `Booking.registration_type.GROUP`.
- [x] Создать `LabWorkBookingRelease` в `apps/academics`.
- [x] Удалить `ScheduleEntry`, `ScheduleEntryDisciplineSelection`, `WeekParity`, `ScheduleDutyRole`.
- [x] Убрать `Room.is_excluded_from_autogen`.
- [x] Обновить constraints `LabSession`:
  - partial unique без `CANCELLED`;
  - добавить индексы.
- [x] Создать разрушительные миграции.

### Фаза 2. Сервисы

- [x] Создать `apps/scheduling/services/session_admin.py`:
  - `create_session`, `update_session`, `cancel_session`;
  - валидация: ЛР ↔ лаборатория, семестр, сетка пар, стенды, группы, capacity;
  - правила редактирования при активных бронях.
- [x] Создать `apps/scheduling/services/session_bulk.py`:
  - `create_series` (`NONE`/`WEEKLY`/`BIWEEKLY`, preview, пропуск праздников/дублей);
  - `copy_day`, `copy_week`;
  - `cancel_series`, `cancel_day` (preview + `force`).
- [x] Обновить `apps/bookings/services/booking.py`:
  - сузить блокировки;
  - проверка `LabWorkBookingRelease`;
  - проверка группы слота;
  - лимит `required_sessions`;
  - автосоздание групповых броней;
  - обновить ручную запись staff.
- [x] Обновить `apps/bookings/services/waitlist.py`:
  - проверка `status=OPEN`, release, группа, окно записи.
- [x] Обновить `apps/bookings/services/attendance.py`:
  - автопосещение для `BOOKED` и `REACCESS`.
- [x] Удалить:
  - `slot_generation.py`,
  - `entry_match.py`,
  - `schedule_admin.py`,
  - `schedule_sync.py`.

### Фаза 3. Каталог и портал расписания

- [x] Обновить `apps/bookings/services/session_catalog.py`:
  - убрать whitelist;
  - добавить release и групповые ограничения.
- [x] Переписать `apps/scheduling/views/schedule.py`:
  - календарь «день недели × пара» для выбранной аудитории;
  - форма создания/редактирования слота;
  - форма серийного создания с preview;
  - копирование дня/недели;
  - отмена серии/дня с preview.
- [x] Обновить шаблоны портала завлаба.
- [x] Обновить студенский каталог:
  - видимость групповых слотов;
  - отображение даты открытия записи;
  - поддержка `required_sessions`.

### Фаза 4. Автопосещение и qcluster

- [x] Добавить `apps/bookings/tasks.py` с `mark_visited_task`.
- [x] Настроить `django-q2 Schedule` на запуск в 20:00 МСК.
- [x] Убедиться, что контейнер `scheduler` запускает `qcluster`.

### Фаза 5. Отчётность и аналитика

- [x] Расширить `apps/bookings/reports.py`.
- [x] Реализовать отчёты:
  - загруженность аудиторий (УЦ → лаборатория → аудитория);
  - загруженность преподавателей;
  - посещаемость (XLSX, строгий формат);
  - выполнение учебного плана с учётом `required_sessions`;
  - dashboard завлаба с KPI и графиками (Chart.js).
- [x] Права доступа:
  - завлаб/staff — свой УЦ/лаборатория;
  - преподаватель — только свои занятия;
  - сисадмин — всё.

### Фаза 6. Тесты, CI, инфраструктура

- [x] Переписать/удалить старые тесты расписания (устаревших `ScheduleEntry`-тестов не осталось).
- [x] Добавить тесты:
  - `session_admin` / `session_bulk`;
  - групповые брони;
  - `required_sessions`;
  - release / групповые ограничения;
  - отчёты.
- [x] Создать `.github/workflows/ci.yml`:
  - `ruff check .`
  - `pytest`
  - `python manage.py makemigrations --check --dry-run`
  - `bandit -r apps config`
  - `pip-audit`
- [x] Добавить индексы на `LabSession`, `AuditLog`, `SupportTicket`, `BookingStatusHistory`.
- [x] Поправить `nginx.conf`:
  - убрать дублирующие security-заголовки (отдаёт Django);
  - добавить `server_tokens off;`;
  - корректно пробрасывать `X-Forwarded-Proto`.
- [x] Создать management command для архивации старых `CANCELLED`-слотов и `AuditLog`.

---

## 5. Открытые технические нюансы

- **Стенды ЛР и комнаты.** Стенд принадлежит комнате; при создании слота проверяется, что в комнате есть стенд ЛР, что чужая ЛР не заняла тот же стенд и что параллельных слотов этой ЛР в комнате не больше, чем её стендов здесь.
- **Вместимость группового слота.** Если состав группы изменится после создания слота, `session.capacity` пересчитается сам: это свойство, а не столбец.
- **Вместимость при изменении стендов.** Вместимость стенда справочная; на запись она не влияет. При сохранении ЛР показывается предупреждение, если мест в ЛР больше, чем вмещает стенд.

---

