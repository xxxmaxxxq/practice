#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  Запуск связки «бот + сайт подписки» (нужен домен).
#
#      bash scripts/deploy-web.sh          # собрать и запустить
#      bash scripts/deploy-web.sh logs     # логи всех контейнеров
#      bash scripts/deploy-web.sh check    # проверить домен и сертификат
#      bash scripts/deploy-web.sh stop     # остановить
#
#  Перед первым запуском:
#    1. Купить домен и направить A-запись на IP этого сервера
#    2. В .env указать PUBLIC_BASE_URL=https://ваш-домен
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker/docker-compose.bot-web.yml"
ENV_FILE="$PROJECT_DIR/.env"
ACTION="${1:-up}"

cd "$PROJECT_DIR"

log()  { echo -e "\033[1;32m[+]\033[0m $*"; }
warn() { echo -e "\033[1;33m[!]\033[0m $*"; }
fail() { echo -e "\033[1;31m[✗]\033[0m $*"; exit 1; }

command -v docker >/dev/null 2>&1 \
    || fail "Docker не установлен: curl -fsSL https://get.docker.com | sh"

# Здесь compose обязателен: контейнеров три и между ними есть зависимости
if docker compose version >/dev/null 2>&1; then
    dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
    dc() { docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }
else
    fail "Нужен docker compose. Поставьте: apt-get install -y docker-compose-plugin"
fi

[[ -f "$ENV_FILE" ]] || fail "Нет файла .env"

DOMAIN_URL="$(grep -E '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")"
DOMAIN="${DOMAIN_URL#https://}"
DOMAIN="${DOMAIN#http://}"
DOMAIN="${DOMAIN%%/*}"

check_domain() {
    [[ -n "$DOMAIN" && "$DOMAIN" != "localhost:8000" && "$DOMAIN" != localhost* ]] \
        || fail "В .env не указан домен. Впишите PUBLIC_BASE_URL=https://ваш-домен"

    [[ "$DOMAIN_URL" == https://* ]] \
        || fail "PUBLIC_BASE_URL должен начинаться с https:// — иначе Telegram не примет кнопку"

    log "Домен: $DOMAIN"

    local server_ip domain_ip
    server_ip="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo '')"
    domain_ip="$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -1)"

    if [[ -z "$domain_ip" ]]; then
        warn "Домен $DOMAIN пока не резолвится. DNS-запись обновляется до 15 минут."
        warn "Запуск продолжу, но сертификат выпустится только после появления записи."
    elif [[ -n "$server_ip" && "$domain_ip" != "$server_ip" ]]; then
        warn "A-запись домена ведёт на $domain_ip, а IP сервера — $server_ip."
        warn "Исправьте A-запись у регистратора, иначе сертификат не выпустится."
    else
        log "A-запись домена указывает на этот сервер ($domain_ip) — верно"
    fi

    if command -v ss >/dev/null 2>&1 && ss -lntp 2>/dev/null | grep -qE ':(80|443)\s'; then
        warn "Порты 80/443 уже кем-то заняты (nginx, apache?). Caddy не сможет их взять."
    fi
}

case "$ACTION" in
    up|start|"")
        check_domain
        log "Собираю и запускаю бота, API и Caddy…"
        dc up -d --build
        log "Жду выпуск сертификата (до минуты)…"
        sleep 25
        dc ps
        echo
        if curl -fsS --max-time 15 "https://$DOMAIN/health" >/dev/null 2>&1; then
            log "Сайт отвечает по https — кнопка «Подключить в 1 клик» заработает"
        else
            warn "https пока не отвечает. Посмотрите логи: bash scripts/deploy-web.sh logs"
            warn "Частые причины: A-запись не обновилась, порты 80/443 заняты."
        fi
        ;;
    logs)
        dc logs -f --tail 100
        ;;
    check)
        check_domain
        echo
        log "Проверяю https…"
        curl -fsS --max-time 15 "https://$DOMAIN/health" && echo || warn "https не отвечает"
        ;;
    restart)
        dc restart
        ;;
    update)
        git pull
        dc up -d --build
        ;;
    status|ps)
        dc ps
        ;;
    stop|down)
        dc down
        ;;
    *)
        fail "Неизвестная команда: $ACTION. Доступны: up, logs, check, restart, update, status, stop"
        ;;
esac
