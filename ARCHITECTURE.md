# LabBooking — архитектура

Система электронной записи студентов на лабораторные работы: расписание,
брони, обращения в поддержку, порталы лабораторий.

## Стек

- Python 3.12, Django 5.1 — серверный рендеринг шаблонов + HTMX, без DRF.
- PostgreSQL 16 (psycopg 3), Redis — кэш; django-q2 (ORM-брокер) — фоновые задачи.
- gunicorn + nginx (docker compose: `db`, `redis`, `web`, `nginx`, `scheduler`).
- Аутентификация — сессии, логин по email (`User.email` = USERNAME_FIELD);
  `django-ratelimit` на POST входа (10 запросов/мин на IP).

## Приложения

```
config/            settings (base/dev/prod/test), urls, middleware (health-исключение
                   из SSL-redirect), health-endpoint.
apps/users         User (roles: STUDENT/TEACHER/LAB_HEAD/LAB_ADMIN/SYS_ADMIN),
                   UserProfile (лаборатория/УЦ), формы входа, RoleRequiredMixin'ы,
                   предикаты ролей (roles.py), context processor флагов ролей.
apps/academics     Faculty, Department, StudentGroup, Semester, Discipline,
                   LabWork, GroupDisciplineLoad, GroupLabWorkOverride;
                   scope.py — видимость каталога (студент → группа → дисциплины
                   → лабы), resolve_staff_*; команды seed_demo, import_dekanat_csv.
apps/scheduling    TrainingCenter, Laboratory, Room, LabStand, Holiday,
                   LabSession, ScheduleEntry (+ selection), LaboratoryBookingSettings,
                   LabDisciplineBinding (through: laboratory+discipline, bound_by,
                   is_override+reason); сервисы: binding (правило факультета,
                   override только SYS_ADMIN), slot_generation/slot_grid
                   (уникальный слот, автогенерация по недельным шаблонам),
                   catalog_admin, schedule_admin, entry_match; portal-views.
apps/bookings      Booking, BookingStatusHistory, StudentLabAttendance,
                   WaitlistEntry, AuditLog, SupportTicket, SupportMessage;
                   services: booking (транзакции, select_for_update,
                   retry_on_deadlock на PG 40P01), session_availability
                   (whitelist-семантика, окна записи), support (статусы,
                   overdue за 24 рабочих часа, авто-реопен), waitlist,
                   attendance (no-show, автопосещение), schedule_sync,
                   seat_capacity, notifications (заглушка-хуки);
                   views: student (каталог, wizard, мои брони), staff
                   (таблица броней, фильтры, ручная бронь), support
                   (мессенджер: пузыри, HTMX-поллинг 4 с); reports (XLSX).
```

## Поток брони

```
студент → view (StudentRequiredMixin) → BookingService.create()
→ проверка окна записи и лимитов (SessionAvailabilityService)
→ транзакция: select_for_update(слоты/сессии/брони) → бронь BOOKED
→ история статусов + AuditLog → notify-хук (заглушка).
Отмена: за ≥24 ч → FREE, waitlist-поднимает следующего.
```

## Права и скоупы

- Роли проверяются только в `apps/users` (mixins/roles); views не содержат
  инлайн-проверок.
- Объектный доступ — через именованные скоуп-queryset'ы
  (`student_disciplines_qs`, `staff_bookings_qs`, `staff_support_tickets_qs`):
  сотрудник видит брони/тикет/людей своей лаборатории или УЦ; SYS_ADMIN — всё;
  сисадмин ≠ `is_superuser` (admin-доступ отдельный, по `is_staff`).

## Деплой

- docker compose: `web` = migrate + collectstatic + gunicorn; `scheduler` =
  qcluster; `nginx` — статика/медиа, заголовки безопасности, proxy к web.
  Порты db/redis/web публикованы только на 127.0.0.1.
- `/api/health/` — проверка БД для оркестратора.
- prod-настройки: SECRET_KEY обязателен, HSTS/secure-cookies при
  SECURE_SSL_REDIRECT=1, CSRF_TRUSTED_ORIGINS из ALLOWED_HOSTS.
- Начальные данные: `seed_demo` (демо-аккаунты) и Dekanat CSV-импорт (пилот).

## Зависимости брони от расписания

LabSession создаётся только в слоте (`unique_slot`: lab_work+room+semester+
starts_at); автогенерация по ScheduleEntry (недельный шаблон, чётность
академической недели); capacity синхронизируется с бронями (seat_capacity).
