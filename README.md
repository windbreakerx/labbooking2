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
