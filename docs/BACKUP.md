# Резервное копирование и восстановление

**Проект:** labbooking2-main
**Дата:** 19.11.2026
**Область:** PostgreSQL (в контейнере `db`) и файлы `media/` (методички PDF).

---

## 1. Политика

| Что | Частота | Хранение |
|---|---|---|
| Дамп БД (`pg_dump -Fc`) | каждый час | 48 последних |
| Суточная копия | раз в сутки, ~03:00 | 14 |
| Недельная копия | по воскресеньям, ~03:00 | 8 |
| Месячная копия | 1-го числа, ~03:00 | 12 |
| Роли (`pg_dumpall --globals-only`) | раз в сутки | 14 |
| `media/` (зеркало `rsync`) | раз в сутки | актуальное состояние |
| `media/` (архив `tar.gz`) | по воскресеньям | 8 |
| Проверка восстановления | раз в месяц | — |

Смысл: RPO — не больше часа, при этом история глубже (до года) есть в
суточных/недельных/месячных копиях. `pg_dump` не блокирует запись: студенты и
сотрудники продолжают работать во время бэкапа.

Копии должны уезжать **за пределы VM** (`BACKUP_REMOTE` в `backup.env`).
Бэкап на том же диске защищает от ошибочного `DELETE`, но не от смерти сервера.

`static/` не бэкапится — пересобирается из кода (`collectstatic`).
Redis не бэкапится: это кэш; очередь django-q2 хранится в PostgreSQL и попадает
в дамп.

---

## 2. Установка на сервере

### 2.1. Bind mount для данных

В `.env` на проде:

```dotenv
POSTGRES_DATA_DIR=/srv/labbooking2/pgdata
MEDIA_DATA_DIR=/srv/labbooking2/media
```

Подготовить каталоги (владельцы важны: без них PostgreSQL не стартует, а
Django не сможет писать методички):

```bash
sudo mkdir -p /srv/labbooking2/{pgdata,media,backups}
sudo chown -R 70:70 /srv/labbooking2/pgdata        # postgres:16-alpine = uid 70
sudo chown -R 1000:1000 /srv/labbooking2/media     # appuser в backend/Dockerfile = uid 1000
sudo chmod 700 /srv/labbooking2/pgdata
```

Если `pgdata` уже содержит данные из именованного тома, перенесите их до
переключения (остановить compose, скопировать с сохранением владельца 70:70).

`restart: unless-stopped` и `stop_grace_period: 1m` для `db` уже заданы в
`docker-compose.yml`: после ребута VM база поднимется сама, а при остановке
PostgreSQL успеет корректно закрыться.

### 2.2. Конфигурация бэкапа

```bash
sudo mkdir -p /etc/labbooking
sudo cp deploy/backup.env.example /etc/labbooking/backup.env
sudo nano /etc/labbooking/backup.env
sudo chmod 600 /etc/labbooking/backup.env
```

Проверить путь `COMPOSE_DIR` (каталог с `docker-compose.yml`) и `BACKUP_DIR`.

### 2.3. Ручной прогон

```bash
sudo BACKUP_CONFIG=/etc/labbooking/backup.env deploy/backup.sh --auto
ls -lh /srv/labbooking2/backups/db/hourly/
```

Скрипт проверяет, что контейнер `db` запущен, дамп непустой и читается
`pg_restore --list`. Ненулевой код выхода означает ошибку.

### 2.4. Таймеры systemd

```bash
sudo cp deploy/systemd/labbooking-*.service deploy/systemd/labbooking-*.timer /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now labbooking-backup.timer
# Необязательно: ежемесячная проверка восстановления
sudo systemctl enable --now labbooking-restore-check.timer

systemctl list-timers 'labbooking-*'
journalctl -u labbooking-backup.service -n 50
```

Если хотите запускать бэкап не от root, создайте пользователя и добавьте его в
группу `docker`, затем поменяйте `User=` в unit-файле.

### 2.5. rsync

Для синхронизации `media/` и офсайт-копии нужен `rsync`:

```bash
sudo apt install rsync
```

Офсайт (пример, настройте беспарольный SSH-ключ):

```bash
# в /etc/labbooking/backup.env
BACKUP_REMOTE=backup@backup-host:/srv/labbooking2
```

---

## 3. Восстановление

### 3.1. Полное восстановление на пустой БД

```bash
cd /srv/labbooking2

# 1. Остановить приложение, оставить только БД
docker compose stop web scheduler nginx

# 2. Пересоздать пустую БД (роли восстанавливаются из globals)
docker compose exec -T db sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'

# 3. Залить дамп
docker compose exec -T db sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' \
  < backups/db/daily/labbooking2_ГГГГ-ММ-ДД_ЧЧММ.dump

# 4. Вернуть роли/пароли, если нужно
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d postgres' \
  < backups/db/globals/globals_ГГГГ-ММ-ДД_ЧЧММ.sql

# 5. Поднять приложение
docker compose up -d
```

Файлы `media/` восстанавливаются из `backups/media/`:

```bash
sudo rsync -a --delete /srv/labbooking2/backups/media/ /srv/labbooking2/media/
sudo chown -R 1000:1000 /srv/labbooking2/media
```

Недельные архивы — в `backups/media-weekly/`, если нужно состояние на дату.

### 3.2. Проверка восстановления без остановки прода

```bash
sudo BACKUP_CONFIG=/etc/labbooking/backup.env deploy/restore-check.sh
# или конкретный файл:
deploy/restore-check.sh backups/db/daily/labbooking2_2026-11-19_0300.dump
```

Скрипт создаёт временную БД `labbooking2_restorecheck`, разворачивает дамп,
считает `django_migrations`, `auth_user`, `academics_labwork`,
`scheduling_labsession`, `bookings_booking` и удаляет временную БД. Продакшен
не затрагивается.

Запускайте проверку **раз в месяц** (таймер `labbooking-restore-check.timer`)
или вручную после смены версии PostgreSQL.

---

## 4. Обновление PostgreSQL (мажорное)

Том/каталог данных несовместим между мажорными версиями. Порядок:

1. Сделать свежий дамп и офсайт-копию.
2. Остановить `web`, `scheduler`, `nginx`.
3. Поднять новый контейнер на новом каталоге данных.
4. `createdb` + `pg_restore` как в разделе 3.1.
5. Проверить `deploy/restore-check.sh` и прогнать приложение.

---

## 5. Что делать, если

- **`db` не стартует после перехода на bind mount** — почти всегда владелец
  каталога: `sudo chown -R 70:70 /srv/labbooking2/pgdata`, `chmod 700`.
- **Django не может записать методичку** — владелец `media`:
  `sudo chown -R 1000:1000 /srv/labbooking2/media`.
- **`pg_dump: error: connection ...`** — контейнер `db` не запущен либо
  `COMPOSE_DIR` в `backup.env` указывает не туда.
- **Дамп «подозрительно мал»** — скрипт намеренно падает, чтобы не перезаписать
  хорошие копии пустыми. Проверьте `docker compose logs db`.
- **Диск переполнен** — уменьшите `HOURLY_KEEP`/`DAILY_KEEP` в `backup.env`;
  старые копии удалятся при следующем запуске.
- **Нужно проверить, что таймер работал** —
  `journalctl -u labbooking-backup.service --since "24 hours ago"`.

---

## 6. Перед рискованными операциями

Всегда делайте отдельный дамп перед:

- `python manage.py archive_old_data --apply`;
- импортом CSV деканата (`import_dekanat_csv`);
- мажорными миграциями;
- ручными правками в админке, затрагивающими историю.

```bash
sudo BACKUP_CONFIG=/etc/labbooking/backup.env deploy/backup.sh --skip-media
```
