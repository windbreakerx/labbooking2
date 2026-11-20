# План на следующую сессию: фронтенд labbooking2

**Дата:** 19.11.2026
**Для продолжения:** 20.11.2026
**Проект:** `labbooking2-main`
**Статус:** аудит бэкенда и эксплуатации завершён, код зелёный, можно переходить к фронтенду.

> **Обновление 20.11.2026:** этап 1 закрыт, этап 2 начат. Актуальный план
> и открытые вопросы — `docs/PLAN_NEXT_SESSION_2026-11-21.md`.

## 1. Где остановились

За 18–19.11.2026 сделано:

- методички: доступ только авторизованным, просмотр/скачивание у студентов,
  загрузка у завлаба, PDF-валидация, `X-Accel-Redirect`, аудит;
- админка: `is_staff` только у сисадмина, `sync_admin_access`,
  rate limit на логин по реальному IP;
- HTTPS: `SECURE_SSL_REDIRECT=True` по умолчанию в prod;
- Chart.js перенесён с CDN в `backend/static/js/chart.umd.min.js`;
- бэкапы: `deploy/backup.sh`, `restore-check.sh`, systemd, `umask 077`,
  документация `docs/BACKUP.md`; политика и восстановление описаны;
- групповые брони: группа допускается только если изучает ЛР (ЛР или её
  дисциплина в плане); `group_part` — вся/первая/вторая половина;
  переполнение аудитории разрешается с предупреждением и аудитом;
- преподаватель видит ЛР/слоты/брони кафедр своих дисциплин
  (`UserProfile.department`);
- аудитория слота должна быть закреплена за лабораторией; комнаты без
  лаборатории — рудимент под будущие консультации;
- серии и копирование сохраняют `occupant_group` и `group_part`;
- последний полный прогон: **359 passed**, `ruff` и `manage.py check` чистые,
  `makemigrations --check` чистый.

## 2. Что лежит вне основного репозитория

Сервисные файлы перенесены в архивный репозиторий `labbooking-main`:

- `labbooking-main/archive/labbooking2/concepts/` — прототипы редизайна;
- `labbooking-main/archive/labbooking2/.agents/skills/` — локальные скилы.

Локальная копия `.agents` в `labbooking2-main` оставлена, потому что каталог
в `.gitignore` и нужен для работы. Если её удалить — восстановить из архива:

```bash
cp -r ../labbooking-main/archive/labbooking2/.agents .
```

Перед началом следующей сессии проверить, что скилы видны, и загрузить
`labbooking2-ui` + `labbooking2-frontend-design`.

## 3. Что показал аудит фронтенда

Отчёт параллельной сессии («Аудит фронтенда без изменений кода») и проверка:

### P0 — CSS разошёлся с шаблонами

В шаблонах используются, но в `main.css` отсутствуют:

`table--schedule`, `pair-cell`, `session-card__title`, `session-card__teacher`,
`form-row`, `form-row__label`, `form-row--release`, `form-stacked`,
`mode-group`, `form-grid__full`, `form-actions--compact`, `grid`, `grid--2`,
`grid--4`, `kpi-card`, `kpi-card__value`, `kpi-card__label`, `chart-wrap`,
`chart-wrap--small`, `legend-list`, `progress-bar`, `progress-bar__fill`,
`progress-bar__label`, `empty-cell`, `methodics-list`, `slot-card--group`.

Плюс:

- в глобальном селекторе полей нет `input[type=time]`, `datetime-local`,
  `file`, `url`, `tel`;
- мёртвый CSS старого редактора (`.entry-chip`, `.schedule-free`,
  `.schedule-grid*`);
- медиа-запросы в середине файла, новые компоненты после них — каскад
  непредсказуем.

### P0 — контраст и фокус

- `--color-muted #7d8f97` на белом ≈ 3.36 (нужно ≥ 4.5);
- `--color-accent #b07328` на `--color-accent-soft` ≈ 3.37;
- `--color-warning #9a6700` на `--color-warning-soft` ≈ 4.31;
- `:focus-visible` использует `outline: none` + `box-shadow` — в режиме
  высокой контрастности фокус пропадает;
- `.ticket-row--history { opacity:.75 }` дополнительно роняет контраст.

### P1 — кэш, статика, UX

- `base.html` версионирует только CSS; JS — нет;
- nginx не включает gzip и `expires` для `/static/`;
- инлайн-стили/скрипты и `confirm()` в `schedule.html` — мешают CSP;
- нет `hx-indicator`/`hx-disabled-elt` — двойные клики при медленном ответе;
- расписание: шесть форм под календарём, `type=time` вместо выбора допустимых
  стартов, `select multiple` для групп, жаргон «группа-оккупант»;
- на мобильном таблица «пара × 7 дней» нечитаема.

## 4. План работ

### Этап 1. Стабилизация фронтенда (первый заход)

1. Привести CSS в соответствие с шаблонами:
   - добавить недостающие классы (список выше) или удалить неиспользуемые;
   - убрать мёртвый CSS старого редактора;
   - добавить стили `input[type=time|datetime-local|file|url|tel]`;
   - привести порядок медиа-запросов к концу файла (или ввести `@layer`).
2. Доступность:
   - поправить контраст токенов (`--color-muted`, `--color-accent`,
     `--color-warning`), проверить AA;
   - `:focus-visible` — `outline: 2px solid` + `outline-offset`,
     не убирать `border-radius`;
   - `accent-color` для чекбоксов/радио;
   - убрать снижение контраста через `opacity` у истории тикетов.
3. Статика:
   - версионировать JS тем же `?v=` или ввести `ManifestStaticFilesStorage`
     (второй вариант — отдельным шагом, с проверкой `collectstatic`);
   - nginx: `gzip on` для CSS/JS/SVG/JSON и `expires 1y` для `/static/`.
4. Расписание:
   - вынести инлайн-стили и скрипты в `static/js/schedule.js`;
   - заменить `input[type=time]` на `<select>` допустимых стартов;
   - добавить `hx-indicator` и блокировку повторной отправки.
5. Мелочи:
   - `methodics-list`, `slot-card--group`, высота `chart-wrap`;
   - вынести инициализацию Chart.js в `dashboard.js` (данные уже в `json_script`).

### Этап 2. Дизайн-система v2

Направление: «графит + медь» (светлая) и «графитовая ночь» (тёмная),
крупная типографика, 1280 контейнер, шаг 8 px, один акцент.

- токены света/тьмы в `main.css` (или отдельных `tokens.css` + `components.css`);
- шрифт Golos Text локально (`font-display: swap`, preload одного веса);
- компоненты: кнопки, поля, карточки, таблицы, бейджи, диалоги, пустые
  состояния, скелетоны;
- тёмная тема: `prefers-color-scheme` + ручной переключатель + сохранение
  выбора, без вспышки;
- пилотные экраны: студенческий каталог/запись и расписание завлаба;
- проверка 360/768/1280, клавиатура, контраст, `prefers-reduced-motion`.

### Этап 3. Раскатка

- страницы по одной, после каждой — полный `pytest` и визуальная проверка;
- не смешивать редизайн с изменением бизнес-логики;
- после выбора направления перенести нужное из `concepts/` в реальные
  шаблоны/токены, прототипы оставить в архиве.

## 5. Проверки перед коммитом/деплоем

```bash
cd backend
python manage.py check
python manage.py makemigrations --check --dry-run
ruff check .
pytest -q
```

Перед рискованными операциями — `deploy/backup.sh` (см. `docs/BACKUP.md`).

## 6. Открытые решения

- утвердить гибрид «графит + медь» / «графитовая ночь»;
- Golos Text: подключать сейчас или оставить системные шрифты;
- `ManifestStaticFilesStorage` сейчас или после стабилизации;
- тёмная тема: следовать системе или по умолчанию светлая;
- оставить `concepts/` только в архиве или перенести в `docs/design/`.

## 7. Первый шаг следующей сессии

Загрузить `labbooking2-ui` и `labbooking2-frontend-design`, открыть
`backend/static/css/main.css`, `backend/templates/scheduling/portal/schedule.html`,
`backend/templates/bookings/student/book.html` и начать с этапа 1:
недостающие классы + контраст + фокус. После — два пилотных экрана.
