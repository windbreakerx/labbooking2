# LabBooking

Система бронирования лабораторных занятий: расписание, брони, обращения в поддержку.

## Стек

- Python 3.12, Django 5.2 (LTS)
- PostgreSQL 16
- Redis (кэш) + django-q2 (фоновые задачи, ORM-брокер)
- gunicorn + nginx
- HTMX на фронтенде, серверный рендеринг шаблонов

## Локальная разработка

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements-dev.txt
copy ..\.env.example ..\.env  # и заполнить значения
python manage.py migrate
python manage.py runserver
```

Требуется работающий PostgreSQL 16 (см. `DATABASE_URL` в `.env`).

### PostgreSQL на Windows (портативный сервер)

Docker на рабочей машине нет, поэтому локальная БД — портативный PostgreSQL 16
в `D:\Users\Mayorov_IV\pg16`, порт **5433** (5432 занят корпоративным сервисом,
его не трогаем). Суперпользователь `postgres`, trust, только localhost.

**Запуск — только через `pg-start.bat` из `cmd`**, не голым `pg_ctl` из bash:
иначе fork-бэкенды падают с `0xC0000142`.

```bat
D:\Users\Mayorov_IV\pg16\pg-start.bat
D:\Users\Mayorov_IV\pg16\bin\pg_isready.exe -h 127.0.0.1 -p 5433
```

Ожидаемый ответ: `127.0.0.1:5433 - accepting connections`. После перезагрузки
машины PostgreSQL нужно запустить заново.

Если `pg-start.bat` пишет `another server might be running`, сначала проверьте
`pg-status.bat`, а не запускайте второй экземпляр. Остановка — `pg-stop.bat`.

Без запущенной БД `psycopg` ждёт соединение до 130 секунд на попытку, поэтому
`pytest` и `manage.py` выглядят как зависшие. Чтобы падать за секунды, укажите
таймаут в `DATABASE_URL`:

```dotenv
DATABASE_URL=postgres://postgres@localhost:5433/labbooking2?connect_timeout=5
```

## Оформление

Три темы, переключаются в шапке, выбор запоминается:

| Тема | Что это |
|---|---|
| **Стекло** | по умолчанию: пастельная шкала, стеклянные поверхности (`backdrop-filter`), фон-меш |
| **Графит** | тёплый графит с медным акцентом |
| **Ночь** | тёмный графит со стальным акцентом |

Тема следует системной, пока не выбрана вручную; `?theme=glass|graphite|night`
в адресе — быстрый предпросмотр без сохранения.

Шрифты локальные: Golos Text (текст) и Manrope (заголовки), оба под OFL —
`backend/static/fonts/`; внешних CDN нет.

## Проверка здоровья

`GET /api/health/` — JSON-статус приложения и подключения к БД.

## Тесты и линт

```bash
cd backend
pytest
ruff check .
```

## Docker

```bash
docker compose up --build
```

Поднимает `db` (PostgreSQL), `redis`, `web` (gunicorn), `nginx`, `scheduler` (django-q2 воркер).

Бэкапы и восстановление — `docs/BACKUP.md` (скрипты в `deploy/`).
Методички и права на них — `docs/METHODICS.md`.

Прод: `SECURE_SSL_REDIRECT=1` включён по умолчанию в `config.settings.prod`.
Для HTTP-стенда без TLS задайте `SECURE_SSL_REDIRECT=0` явно и не открывайте
порт 80 наружу.
