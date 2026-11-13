# Редизайн подсистемы расписания — «Календарь слотов» (вариант A)

> Дата: 13.11.2026. Статус: дизайн-документ, утверждается перед реализацией.
> Контекст: цель проекта — сопровождение людьми без программирования;
> деплой отложен, локальная БД — одноразовые демо/CSV-данные (разрушительная
> миграция допустима). Базлайн: Django 5.2 LTS, 320 тестов, HEAD после
> «upgrade: Django 5.2 LTS».

## 1. Контекст и цель

Сегодня расписание хранится как **правила повтора** (`ScheduleEntry`: день
недели, чётность, выборки дисциплин/ЛР), а конкретные слоты (`LabSession`)
рождаются генератором. Из-за производности слотов существуют три
механизма-посредника:

1. **Whitelist** — слот видим студенту, только если его покрывает живая
   запись расписания (`ScheduleWhitelistIndex`, 4 неявных условия в
   `entry_match.py`).
2. **Reconcile** (`bookings/services/schedule_sync.py`) — после правки
   правила: отмена осиротевших слотов (брони → SLOT_CANCELLED, сброс
   waitlist), докатка преподавателя/мест/ends_at. Вызовы размазаны по 4
   местам в views; пропущенный вызов «замуровывает» слоты навсегда
   (insert-only, CANCELLED не воскрешается).
3. **Самодельный календарь чётности** (`academic_week_parity`) — счёт недель
   от 1 сентября сквозь каникулы; из данных не выводится.

Проблема: расписание существует в **двух представлениях**, которые нужно
сверять; правки шаблона молча меняют бронируемость уже созданных слотов.
Вокруг этого ~3,5 тыс. строк кода и тестов; 4 из 10 топ-хотспотов проекта.

**Цель:** одно представление — сами слоты. Правка видна и объяснима без
чтения кода: «что в календаре — то и есть».

## 2. Почему A, а не B

B (уплощённые шаблоны) сохранял бы пару «правило → генератор → сверка»,
убирая только поля. Двойное представление — источник класса ошибок
«правило и слоты разошлись» — осталось бы. A убирает класс целиком.

| Критерий | A (выбран) | B (отклонён) |
|---|---|---|
| Сущности для сопровождения | 1 (слот) | 2 (правило + слот) |
| Ошибки рассинхрона | невозможны | возможны, реже |
| Праздники задним числом | кнопка «отменить день» | автоматически |
| Правка паттерна семестра | пересоздать серию (2 клика) | правка одного правила |
| Выигрыш LOC (код+тесты) | ~−1,7–1,9 тыс. | ~−0,9–1,1 тыс. |
| Объём UI-переделки | больше (новый календарь) | меньше (форма короче) |

Компенсаторы минусов A: массовые операции двухфазные (preview → force),
`create_series` пропускает праздники и дубли, `copy_week` переносит неделю.

## 3. Схема данных

### 3.1. Добавляется

| Поле | Модель | Тип / правила | Назначение |
|---|---|---|---|
| `student_group` | `scheduling.LabSession` | FK `academics.StudentGroup`, null, blank, `SET_NULL`, related_name `lab_sessions` | слот только для группы; NULL = для всех. Заменяет `TEACHER_DUTY` + `load_group_label` (строку) |
| `booking_opens_at` | `academics.LabWork` | DateTimeField, null, blank | постепенное открытие записи; NULL = запись закрыта; будущее = «откроется D»; прошлое/сейчас = открыта |

Семантика `booking_opens_at` выбрана fail-closed: забытая ЛР не бронируется
(заметно, лечится кнопкой), а не открывается молча в первый день.

### 3.2. Удаляется (модели)

- `ScheduleEntry` + `ScheduleEntryDisciplineSelection` (шаблоны и выборки);
- `WeekParity` (ODD/EVEN/BOTH — чётность стала параметром серии «через
  неделю от даты»);
- `ScheduleDutyRole` (LAB_STAFF_DUTY/TEACHER_DUTY/OTHER — остались поля
  `teacher` и `student_group`).

### 3.3. Не меняется

`TrainingCenter`, `Laboratory`, `LaboratoryType`, `Room` (включая
`is_blocked`, `is_excluded_from_autogen` — второе поле становится
неиспользуемым, удаляем вместе с фильтром), `LabDisciplineBinding`,
`LabStand`, `Holiday`, `LaboratoryBookingSettings`, `AutoVisitedMode`,
`LabSessionStatus` (DRAFT/OPEN/CLOSED/CANCELLED), констрейнт
`scheduling_labsession_unique_slot` (защита от дублей при массовом
создании), сетка пар `UNIVERSITY_PAIR_SLOTS` + `pair_start_times_for_duration`
(остаётся валидатором и UI-подсказкой), вся математика мест
(`seat_capacity`), привязки, стенды, каскад блокировки аудитории.

## 4. Новые сервисы (`apps/scheduling/services/`)

### 4.1. `session_admin.py` (~150 строк)

CRUD одного слота + валидация. `clean_session_payload`:

- комната той же лаборатории/ТЦ и не `is_blocked`;
- ЛР привязана к лаборатории (`binding`) и `is_published`;
- `start_time` — начало одной из пар; слот целиком внутри пары
  (`pair_start_times_for_duration` — существующая функция);
- `ends_at = starts_at + lab_work.duration_minutes` (вычисляется, не вводится);
- `capacity ≥ 1`, default `lab_work.capacity` (вместимость аудитории в формулу
  не входит — динамический лимит пересекающихся броней, как сегодня);
- `starts_at` в будущем; семестр — активный (авто);
- `student_group` — опционально; список групп для формы — группы с нагрузкой
  (`GroupDisciplineLoad`) по дисциплинам лаборатории.

Правила правки слота:

| Поле | Без активных броней | С активными бронями |
|---|---|---|
| `teacher`, `student_group` | да | да |
| `capacity` | да | да, но ≥ `booked_count` |
| `starts_at`, `room`, `lab_work` | да | **нет** — отменить слот и создать новый (снимает рассинхрон со снапшотами `Booking.scheduled_at/room`) |

- `cancel_session` → `BookingService.cancel_session_bookings(session, note)`
  (уже написан и протестирован) + `status=CANCELLED`.
- Удаление слота запрещено при наличии любых броней (у `Booking.lab_session`
  CASCADE — молча сожрало бы историю; сначала отмена).
- `update_future_after_lab_work_edit(lab_work)` — после правки ЛР: будущие
  OPEN-слоты **без активных броней** получают `capacity=lab_work.capacity`,
  `ends_at=starts_at+duration` (одна функция вместо if/elif-пары
  `sync_open_session_capacities/durations` — фикс известного бага «изменил
  оба поля — синхронизировалось только первое»).

### 4.2. `session_bulk.py` (~160 строк)

- `create_series(...)` — параметры: `lab_work`, `room`, `teacher` (опц.),
  `student_group` (опц.), `capacity` (default из ЛР), `start_date`,
  `start_time` (старт пары), `repeat ∈ {NONE, WEEKLY, BIWEEKLY}`,
  `occurrences ≤ 30`, `skip_holidays=True`. Валидация как в 4.1; даты =
  стартовая + 0/7/14… дней; праздники и дубли (unique_slot) пропускаются
  через `bulk_create(ignore_conflicts=True)`; возвращает
  `(created, skipped_holidays, skipped_duplicates)`.
  **BIWEEKLY — это и есть чёт/нечёт**, но от конкретной даты: арифметика
  дат, объяснимая без календаря от 1 сентября.
- `copy_day(room, source_date, target_date)` / `copy_week(room, source_week,
  target_week)` — копирование слотов комнаты (кроме CANCELLED) с пропуском
  дублей; возвращает `(created, skipped)`.
- `cancel_series(room=None, lab_work=None, from_date=None, to_date=None)` —
  **двухфазно**: preview `(sessions, booked_bookings, waitlist_entries)` →
  `force=True` → по каждому `cancel_session_bookings(note="Серия отменена")`.
- `cancel_day(room, date)` — preview + force для одного дня (праздник
  задним числом).

Крон не нужен: серийное создание — действие завлаба, слоты на весь семестр
можно создать за один запуск (бронируемость дальше 14 дней всё равно
ограничена горизонтом записи; слоты вне горизонта видны персоналу).

## 5. `booking_opens_at` — точки проверки

| Точка | Поведение |
|---|---|
| Каталог студента (`session_catalog`) | слоты закрытой ЛР показываются, кнопка «Записаться» задизейблена: «Запись закрыта» / «Запись откроется 15.10, 09:00». Состояние вычисляется один раз на ЛР (bulk) |
| `BookingService.create` | guard до транзакции → `BookingError("Запись на лабораторную работу «X» ещё не открыта")` |
| `WaitlistService` (create) | тот же guard |
| Ручная бронь персонала (`staff`) | **bypass** — режим персонала уже отдельный (окна не действуют) |
| Портал завлаба (`catalog.py` view + `lab_works.html`) | колонка «Запись»: закрыта / откроется D / открыта. Действия: `open-now` (ставит now), `open-at` (форма дата+время), массовое «открыть запись» по чекбоксам. URL: `/lab-head/lab-works/<pk>/booking-open/` + bulk-эндпоинт |

Хелпер на модели: `LabWork.booking_open(now=None) -> bool` (тривиальное
сравнение) + человекочитаемая дата для шаблонов. Каталог студента не
показывает причину закрытия дальше, чем подпись у кнопки.

Ограничение: уведомления «запись открылась» не существуют (нотификации —
заглушка). Для пилота достаточно: студент видит дату открытия заранее.

## 6. Слоты для группы — проверка

- Каталог: `session.student_group_id is None or session.student_group_id ==
  profile.student_group_id` (группа студента — `UserProfile.student_group`,
  существующий accessor в `academics/scope.py`).
- `BookingService.create`: тот же guard + `BookingError` (защита от прямого
  POST мимо каталога). Waitlist — аналогично.

## 7. Изменения по приложениям

### `apps/scheduling`
- **Удалить:** `services/slot_generation.py` (142), `services/entry_match.py`
  (61), `services/schedule_admin.py` (367), `management/commands/generate_sessions.py`
  (27), `views/schedule.py` (273 — переписать), модели из §3.2, из
  `services/slot_grid.py` — `academic_week_parity` (остаётся сетка пар),
  `Room.is_excluded_from_autogen` (поле + фильтры + hint).
- **Добавить:** `services/session_admin.py`, `services/session_bulk.py`,
  новые `views/schedule.py` (сетка недели × пары по комнате: просмотр,
  создание с блоком «Повторять», правка, отмена, копирование, отмена серии,
  XLSX-экспорт слотов недели).
- **Поправить:** `urls.py` (маршруты), `admin.py` (снять ScheduleEntry/
  WeekParity/ScheduleDutyRole, добавить `student_group` на LabSession),
  `reports.py` (экспорт слотов вместо правил, стиль тот же: лист на
  аудиторию, freeze B2), `models.py` (лабораторные свойства `booked_count`/
  `available_seats` остаются).

### `apps/bookings`
- **Удалить:** `services/schedule_sync.py` (128) целиком; из
  `services/session_availability.py` — блок whitelist (`ScheduleWhitelistIndex`,
  импорты `entry_match`/`ScheduleEntry`/`ScheduleDutyRole`, групповое
  правило через `load_group_label`) ~60 строк.
- **Поправить:** `services/session_catalog.py` (whitelist → проверки
  `student_group` + `booking_open` из §5–6), `services/booking.py` (+2
  guard'а), `services/waitlist.py` (+2 guard'а), `views/catalog.py`
  (блокировка аудитории остаётся; вызовы sync → `update_future_after_lab_work_edit`).
- Ручное окно персонала (`staff_manual_slots`, сетка пар) — без изменений.

### `apps/academics`
- `models.py`: +`LabWork.booking_opens_at`; `admin.py`: показать поле.
- `management/commands/seed_demo.py`: вместо записей расписания — демо-слоты
  напрямую (серия WEEKLY на 3 недели, BIWEEKLY, один слот с `student_group`);
  ЛР получают примеры `booking_opens_at` (открыта ×2, закрыта, будущая дата).
- `import_dekanat_csv.py` ScheduleEntry не касается — без изменений.

### `config`, шаблоны
- `config/urls.py` не меняется. Шаблоны: `portal/schedule.html` (сетка
  слотов) и `_session_form.html` — новые; `portal/lab_works.html` — блок
  «Запись»; `rooms.html`, `bindings.html`, `home.html` — без изменений.

## 8. Сценарии

**Завлаб, ввод семестра:** Портал → Расписание → создать слот (ЛР, аудитория,
пара, «повторять каждую неделю × 15», пропуская праздники) → 15 строк-слотов
созданы. Вторая ЛР — «через неделю × 8». Ошибся — «отменить серию» (preview:
сколько слотов/броней/очередей) → force → пересоздать.

**Завлаб, в течение семестра:** поменялся преподаватель — правка слота.
Аудитория занята — «отменить слот» (брони → SLOT_CANCELLED, письма нет,
очередь сброшена) + создать слот в другой аудитории. Изменилась вместимость
ЛР — правка ЛР, будущие слоты без броней подтянутся.

**Открытие записи:** в списке ЛР — «Запись: закрыта». Дважды в месяц завлаб
выделяет очередную пару ЛР → «открыть сейчас» (или заранее проставил даты —
откроются сами). Студенты видят «Запись откроется 15.10» в каталоге.

**Студент:** без изменений в UX — каталог, фильтры, wizard. Слот «для
группы» видит только студент этой группы.

## 9. Миграции (разрушительные, порядок)

1. `academics/0003_*`: AddField `LabWork.booking_opens_at` (additive).
2. `scheduling/0005_session_calendar`: AddField `LabSession.student_group`;
   RemoveField `Room.is_excluded_from_autogen`;
   DeleteModel `ScheduleEntryDisciplineSelection`, `ScheduleEntry`,
   `WeekParity`, `ScheduleDutyRole`.
3. Data-миграций нет — данные одноразовые. После миграций: `seed_demo`
   (пересев), CSV-импорт при необходимости.
4. `makemigrations --check` в гейтах; ожидаемое число миграций — 2.

## 10. Тест-план

| Файл | Действие | Оценка строк |
|---|---|---|
| `test_slot_generation.py` (204) | удалить | −204 |
| `test_schedule_sync.py` (136) | удалить (инварианты переезжают в session_admin) | −136 |
| `test_web_schedule.py` (769) | переписать: сетка, создание+повтор, правила правки, отмена-каскад, copy, cancel_series, XLSX | ~−350 |
| `test_slot_grid.py` (185) | убрать parity-тесты, оставить сетку пар | ~−60 |
| `conftest.py` | фабрика записей → фабрика слотов | ~0 |
| новые `test_session_admin.py` | валидации, правила правки, guard удаления | +~120 |
| новые `test_session_bulk.py` | серии (WEEKLY/BIWEEKLY/праздники/дубли), copy, cancel_series preview/force | +~150 |
| `test_booking.py` | +booking_opens_at (4: NULL/будущее/прошлое/staff-bypass), +группа (2) | +~90 |
| `test_session_catalog.py` | состояние кнопки, видимость группы | +~60 |
| `test_web_portal.py` | действия open-now/open-at/bulk | +~70 |

Прогноз: тесты 5 371 → **~4 400–4 600**; prod-python 6 871 → **~5 900–6 100**
(удаляется ~1 100 строк кода, добавляется ~700).

Гейты этапа: pytest (ожидание ~300–320 после перегона), ruff, bandit,
pip-audit, `makemigrations --check`, LOC-контроль ≤ 14 000.

## 11. Риски и правила-сторожи

1. **UI-календарь — крупнейший кусок работ.** Митигация: переиспользовать
   сетку «пара × день» и стили текущего `schedule.html`; форма создания —
   эволюция `_entry_form.html`.
2. **Массовые операции** — всюду двухфазность (preview → force), unique_slot
   страхует от дублей; `occurrences ≤ 30` — защита от опечатки «300 недель».
3. **Перенос слота с бронями запрещён** (правило из §4.1) — снапшоты
   `Booking` (room, scheduled_at) никогда не протухают.
4. **ЛР без `booking_opens_at` не бронируется** — fail-closed; seed_demo
   обязан проставлять значения; в форме ЛР поле видно рядом с «Опубликовано».
5. **Праздники задним числом** — не автоматизируются; кнопка `cancel_day`
   в портале (двухфазная). Отказ от автоматизации — осознанный (простота
   важнее удобства редкого сценария).
6. **Ручное окно персонала** — не зависело от whitelist; проверить при
   имплементации, что удаление блока его не задело.

## 12. Out of scope / открытые вопросы

- qcluster/scheduler-контейнер и автопосещение (мёртвая инфраструктура) —
  отдельное решение в бэклоге.
- `LaboratoryBookingSettings.booking_day_opens_at/closes_at` — не
  используются, кандидат на удаление позже.
- Уведомления студентов об открытии записи — нет (заглушки нотификаций).
- Семантика DRAFT (скрыт от студентов) и CLOSED (запись закрыта вручную) —
  сохраняется, пересмотр позже.
- Экспорт XLSX: форма отчёта по слотам (неделя × аудитория) — утвердить
  макет при имплементации.
