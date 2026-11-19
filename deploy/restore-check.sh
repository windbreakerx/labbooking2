#!/usr/bin/env bash
# Проверка восстановления: разворачивает дамп во временную БД и выполняет
# контрольные запросы. Продакшен-БД не затрагивается.
#
# Использование:
#   deploy/restore-check.sh                    # последний суточный (или часовой) дамп
#   deploy/restore-check.sh path/to/file.dump  # конкретный дамп
#
# Рекомендуется запускать раз в месяц (deploy/systemd/labbooking-restore-check.timer).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_COMPOSE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG_FILE="${BACKUP_CONFIG:-/etc/labbooking/backup.env}"
if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

COMPOSE_DIR="${COMPOSE_DIR:-$DEFAULT_COMPOSE_DIR}"
BACKUP_DIR="${BACKUP_DIR:-$COMPOSE_DIR/backups}"
RESTORE_DB="${RESTORE_DB:-labbooking2_restorecheck}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die() { log "ОШИБКА: $*" >&2; exit 1; }

cd "$COMPOSE_DIR"
docker compose ps --status running --services | grep -qx db \
  || die "Контейнер db не запущен"

DUMP="${1:-}"
if [[ -z "$DUMP" ]]; then
  DUMP="$(find "$BACKUP_DIR/db/daily" "$BACKUP_DIR/db/hourly" \
    -maxdepth 1 -type f -name '*.dump' -printf '%T@ %p\n' 2>/dev/null \
    | sort -n | tail -n 1 | cut -d' ' -f2-)"
fi
[[ -n "$DUMP" && -f "$DUMP" ]] || die "Дамп не найден (укажите путь аргументом)"

log "Проверяю дамп: $DUMP"

cleanup() {
  log "Удаляю временную БД $RESTORE_DB"
  docker compose exec -T db sh -c \
    'dropdb -U "$POSTGRES_USER" --if-exists "$1"' -- "$RESTORE_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cleanup
docker compose exec -T db sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists "$1" && createdb -U "$POSTGRES_USER" "$1"' \
  -- "$RESTORE_DB"

log "pg_restore -> $RESTORE_DB"
docker compose exec -T db sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$1" --no-owner --exit-on-error' \
  -- "$RESTORE_DB" <"$DUMP"

log "Контрольные запросы"
counts=$(docker compose exec -T db sh -c \
  'psql -U "$POSTGRES_USER" -d "$1" -At -F " " -c "
     SELECT
       (SELECT count(*) FROM django_migrations),
       (SELECT count(*) FROM auth_user),
       (SELECT count(*) FROM academics_labwork),
       (SELECT count(*) FROM scheduling_labsession),
       (SELECT count(*) FROM bookings_booking)
   "' -- "$RESTORE_DB")

read -r migrations users labworks sessions bookings <<<"$counts"
log "django_migrations=$migrations, auth_user=$users, labwork=$labworks, labsessions=$sessions, bookings=$bookings"

(( migrations > 0 )) || die "В восстановленной БД нет миграций"
(( users > 0 )) || die "В восстановленной БД нет пользователей"

log "Проверка восстановления пройдена"
