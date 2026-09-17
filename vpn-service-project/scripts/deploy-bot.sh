#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  Управление ботом на VPS одной короткой командой.
#
#  Сделан специально без флагов в вызове: длинные docker-команды часто
#  ломаются при копировании из браузера (обычный дефис "-" превращается
#  в типографский минус "−", и docker ругается "unknown shorthand flag").
#
#      bash scripts/deploy-bot.sh          # собрать и запустить
#      bash scripts/deploy-bot.sh logs     # смотреть логи
#      bash scripts/deploy-bot.sh restart  # перезапустить
#      bash scripts/deploy-bot.sh update   # обновить код с GitHub и пересобрать
#      bash scripts/deploy-bot.sh status   # что сейчас запущено
#      bash scripts/deploy-bot.sh stop     # остановить
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker/docker-compose.bot-only.yml"
ENV_FILE="$PROJECT_DIR/.env"
ACTION="${1:-up}"

cd "$PROJECT_DIR"

log()  { echo -e "\033[1;32m[+]\033[0m $*"; }
fail() { echo -e "\033[1;31m[✗]\033[0m $*"; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker не установлен. Поставьте: curl -fsSL https://get.docker.com | sh"

# Ищем .env: сначала рядом с проектом, потом в домашней папке развёртывания
if [[ ! -f "$ENV_FILE" ]]; then
    for candidate in "$HOME/salt-bot/.env" "$HOME/.env"; do
        if [[ -f "$candidate" ]]; then
            log "Нашёл настройки в $candidate — копирую в проект"
            cp "$candidate" "$ENV_FILE"
            break
        fi
    done
fi
[[ -f "$ENV_FILE" ]] || fail "Нет файла .env. Создайте его: cp .env.example .env && nano .env"

grep -q '^BOT_TOKEN=.\+' "$ENV_FILE" || fail "В .env не заполнен BOT_TOKEN"

dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

case "$ACTION" in
    up|start|"")
        log "Собираю образ и запускаю бота (первый раз это 2–4 минуты)…"
        dc up -d --build
        echo
        log "Готово. Последние строки лога:"
        sleep 4
        dc logs --tail 20
        echo
        log "Смотреть логи дальше:  bash scripts/deploy-bot.sh logs"
        ;;
    logs)
        log "Логи (выход — Ctrl+C, бот продолжит работать)"
        dc logs -f --tail 100
        ;;
    restart)
        log "Перезапускаю бота (подхватит правки в config/*.yml и .env)…"
        dc restart
        dc ps
        ;;
    update)
        log "Забираю свежий код с GitHub…"
        git pull
        log "Пересобираю и перезапускаю…"
        dc up -d --build
        dc ps
        ;;
    status|ps)
        dc ps
        ;;
    stop|down)
        log "Останавливаю бота (база в томе botdata остаётся)…"
        dc down
        ;;
    *)
        fail "Неизвестная команда: $ACTION. Доступны: up, logs, restart, update, status, stop"
        ;;
esac
