#!/usr/bin/env bash
# Резервное копирование LabBooking: PostgreSQL (pg_dump) + media.
#
# Запускается ежечасно через systemd timer (deploy/systemd/labbooking-backup.timer).
# Раз в сутки (в 03:xx) дополнительно создаёт суточную/недельную/месячную копию
# по схеме GFS и синхронизирует media.
#
# Ручной запуск:
#   deploy/backup.sh --auto          # как из таймера
#   deploy/backup.sh --skip-media    # только БД
#
# Настройки берутся из /etc/labbooking/backup.env (можно переопределить
# переменными окружения). Пример — deploy/backup.env.example.

set -euo pipefail

# Дампы содержат персональные данные: только владелец читает каталог и файлы.
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_COMPOSE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG_FILE="${BACKUP_CONFIG:-/etc/labbooking/backup.env}"
if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

COMPOSE_DIR="${COMPOSE_DIR:-$DEFAULT_COMPOSE_DIR}"
BACKUP_DIR="${BACKUP_DIR:-$COMPOSE_DIR/backups}"
DATA_DIR="${DATA_DIR:-$COMPOSE_DIR/data}"
HOURLY_KEEP="${HOURLY_KEEP:-48}"
DAILY_KEEP="${DAILY_KEEP:-14}"
WEEKLY_KEEP="${WEEKLY_KEEP:-8}"
MONTHLY_KEEP="${MONTHLY_KEEP:-12}"
MEDIA_KEEP="${MEDIA_KEEP:-8}"
BACKUP_REMOTE="${BACKUP_REMOTE:-}"
MIN_DUMP_BYTES="${MIN_DUMP_BYTES:-2048}"
LOCK_FILE="${LOCK_FILE:-/run/lock/labbooking-backup.lock}"

SKIP_MEDIA=0
for arg in "$@"; do
  case "$arg" in
    --auto) ;;
    --skip-media) SKIP_MEDIA=1 ;;
    -h|--help)
      sed -n '2,16p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "Неизвестный аргумент: $arg" >&2
      exit 2
      ;;
  esac
done

HOURLY_DIR="$BACKUP_DIR/db/hourly"
DAILY_DIR="$BACKUP_DIR/db/daily"
WEEKLY_DIR="$BACKUP_DIR/db/weekly"
MONTHLY_DIR="$BACKUP_DIR/db/monthly"
GLOBALS_DIR="$BACKUP_DIR/db/globals"
MEDIA_MIRROR_DIR="$BACKUP_DIR/media"
MEDIA_WEEKLY_DIR="$BACKUP_DIR/media-weekly"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die() { log "ОШИБКА: $*" >&2; exit 1; }

mkdir -p "$HOURLY_DIR" "$DAILY_DIR" "$WEEKLY_DIR" "$MONTHLY_DIR" \
         "$GLOBALS_DIR" "$MEDIA_MIRROR_DIR" "$MEDIA_WEEKLY_DIR"

# Не даём двум запускам пересечься (таймер + ручной запуск).
LOCK_FILE="${LOCK_FILE:-/run/lock/labbooking-backup.lock}"
if [[ ! -d "$(dirname "$LOCK_FILE")" || ! -w "$(dirname "$LOCK_FILE")" ]]; then
  LOCK_FILE="$BACKUP_DIR/.backup.lock"
fi
if command -v flock >/dev/null; then
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "Предыдущий бэкап ещё выполняется — выход"
    exit 0
  fi
else
  log "flock не найден — блокировка от параллельного запуска отключена"
fi

cd "$COMPOSE_DIR"
command -v docker >/dev/null || die "docker не найден в PATH"
docker compose ps --status running --services | grep -qx db \
  || die "Контейнер db не запущен"

# Удаляет старые файлы, оставляя $keep самых свежих.
prune() {
  local dir="$1" keep="$2" pattern="$3"
  local count
  count=$(find "$dir" -maxdepth 1 -type f -name "$pattern" | wc -l)
  (( count > keep )) || return 0
  find "$dir" -maxdepth 1 -type f -name "$pattern" -printf '%T@ %p\n' \
    | sort -n \
    | head -n $(( count - keep )) \
    | cut -d' ' -f2- \
    | while IFS= read -r old; do
        log "Удаляю старый бэкап: $(basename "$old")"
        rm -f -- "$old"
      done
}

dump_db() {
  local target="$1" tmp="${target}.tmp" size
  log "pg_dump -> $target"
  if ! docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' >"$tmp"; then
    rm -f "$tmp"
    die "pg_dump завершился с ошибкой"
  fi

  size=$(stat -c%s "$tmp")
  if (( size < MIN_DUMP_BYTES )); then
    rm -f "$tmp"
    die "Дамп подозрительно мал: $size байт"
  fi

  # Читаемость архива: pg_restore --list разбирает заголовок и оглавление.
  if ! docker compose exec -T db pg_restore --list <"$tmp" >/dev/null; then
    rm -f "$tmp"
    die "pg_restore не может прочитать дамп"
  fi

  mv "$tmp" "$target"
  log "OK: $(basename "$target"), $size байт"
}

backup_globals() {
  local target="$1" tmp="${target}.tmp"
  log "pg_dumpall --globals-only -> $target"
  if ! docker compose exec -T db sh -c 'pg_dumpall -U "$POSTGRES_USER" --globals-only' >"$tmp"; then
    rm -f "$tmp"
    die "pg_dumpall завершился с ошибкой"
  fi
  mv "$tmp" "$target"
}

backup_media() {
  local weekly_archive
  if [[ ! -d "$DATA_DIR/media" ]]; then
    log "Каталог $DATA_DIR/media не найден — синхронизация media пропущена"
    return 0
  fi
  command -v rsync >/dev/null || die "rsync не установлен (нужен для media)"

  log "rsync media -> $MEDIA_MIRROR_DIR"
  rsync -a --delete "$DATA_DIR/media/" "$MEDIA_MIRROR_DIR/"

  # Недельный архивный снимок media (на случай удаления файлов из зеркала).
  if [[ "$(date '+%u')" == "7" ]]; then
    weekly_archive="$MEDIA_WEEKLY_DIR/media_${TS}.tar.gz"
    log "tar media -> $weekly_archive"
    tar -C "$DATA_DIR" -czf "${weekly_archive}.tmp" media
    mv "${weekly_archive}.tmp" "$weekly_archive"
    prune "$MEDIA_WEEKLY_DIR" "$MEDIA_KEEP" '*.tar.gz'
  fi
}

TS="$(date '+%Y-%m-%d_%H%M')"
HOUR="$(date '+%H')"
DOW="$(date '+%u')"   # 7 = воскресенье
DOM="$(date '+%d')"

log "Старт бэкапа: TS=$TS, BACKUP_DIR=$BACKUP_DIR"
dump_db "$HOURLY_DIR/labbooking2_${TS}.dump"
prune "$HOURLY_DIR" "$HOURLY_KEEP" '*.dump'

if [[ "$HOUR" == "03" ]]; then
  cp -p "$HOURLY_DIR/labbooking2_${TS}.dump" "$DAILY_DIR/"
  backup_globals "$GLOBALS_DIR/globals_${TS}.sql"
  prune "$DAILY_DIR" "$DAILY_KEEP" '*.dump'
  prune "$GLOBALS_DIR" "$DAILY_KEEP" '*.sql'

  if [[ "$DOW" == "7" ]]; then
    cp -p "$HOURLY_DIR/labbooking2_${TS}.dump" "$WEEKLY_DIR/"
    prune "$WEEKLY_DIR" "$WEEKLY_KEEP" '*.dump'
  fi

  if [[ "$DOM" == "01" ]]; then
    cp -p "$HOURLY_DIR/labbooking2_${TS}.dump" "$MONTHLY_DIR/"
    prune "$MONTHLY_DIR" "$MONTHLY_KEEP" '*.dump'
  fi

  if [[ "$SKIP_MEDIA" == "0" ]]; then
    backup_media
  fi
fi

# Офсайт-копия. Включается через BACKUP_REMOTE=user@host:/path в backup.env.
if [[ -n "$BACKUP_REMOTE" ]]; then
  local_dumps=$(find "$BACKUP_DIR/db" -type f -name '*.dump' | wc -l)
  if (( local_dumps == 0 )); then
    die "Локальных дампов нет — офсайт-копирование отменено"
  fi
  log "Офсайт: rsync -> $BACKUP_REMOTE"
  rsync -a --delete "$BACKUP_DIR/" "$BACKUP_REMOTE/"
fi

log "Бэкап завершён"
