#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  Полный мастер-сервер: панель Marzban + бот + сайт + воркеры.
#
#      bash scripts/deploy-master.sh          # развернуть всё
#      bash scripts/deploy-master.sh logs     # логи
#      bash scripts/deploy-master.sh status   # что запущено
#      bash scripts/deploy-master.sh keys     # показать ключи Reality
#      bash scripts/deploy-master.sh stop     # остановить
#
#  Перед первым запуском в .env должны быть заполнены:
#      PUBLIC_BASE_URL   — https://vpn.ваш-домен
#      PANEL_BASE_URL    — https://panel.ваш-домен
#      MARZBAN_USERNAME, MARZBAN_PASSWORD — вход в панель
#  Обе A-записи должны указывать на этот сервер.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker/docker-compose.master.yml"
ENV_FILE="$PROJECT_DIR/.env"
GEN_DIR="$PROJECT_DIR/config/generated"
KEYS_FILE="$GEN_DIR/reality-keys.json"
XRAY_FILE="$GEN_DIR/xray_config.json"
TEMPLATE="$PROJECT_DIR/config/xray-vless/master-xray-config.template.json"

NODE_CODE="${NODE_CODE:-nl-1}"
NODE_LOCATION="${NODE_LOCATION:-nl}"
XRAY_PORT="${XRAY_PORT:-8443}"
XRAY_SNI="${XRAY_SNI:-www.nvidia.com}"

ACTION="${1:-up}"
cd "$PROJECT_DIR"

log()  { echo -e "\033[1;32m[+]\033[0m $*"; }
warn() { echo -e "\033[1;33m[!]\033[0m $*"; }
fail() { echo -e "\033[1;31m[✗]\033[0m $*"; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker не установлен"

if docker compose version >/dev/null 2>&1; then
    dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
    dc() { docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }
else
    fail "Нужен docker compose: apt-get install -y docker-compose-v2"
fi

[[ -f "$ENV_FILE" ]] || fail "Нет файла .env"

read_env() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true; }
host_of()  { local u="${1#https://}"; u="${u#http://}"; echo "${u%%/*}"; }

PUBLIC_URL="$(read_env PUBLIC_BASE_URL)"
PANEL_URL="$(read_env PANEL_BASE_URL)"
MZ_USER="$(read_env MARZBAN_USERNAME)"
MZ_PASS="$(read_env MARZBAN_PASSWORD)"

[[ -n "$PUBLIC_URL" ]] || fail "В .env нет PUBLIC_BASE_URL"
[[ -n "$PANEL_URL"  ]] || fail "В .env нет PANEL_BASE_URL. Добавьте:
    echo 'PANEL_BASE_URL=https://panel.ваш-домен' >> .env"
[[ -n "$MZ_USER" && -n "$MZ_PASS" ]] || fail "В .env нет MARZBAN_USERNAME или MARZBAN_PASSWORD"

PUBLIC_HOST="$(host_of "$PUBLIC_URL")"
PANEL_HOST="$(host_of "$PANEL_URL")"
SERVER_IP="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo '')"

check_dns() {
    local name="$1" resolved
    resolved="$(getent hosts "$name" 2>/dev/null | awk '{print $1}' | head -1)"
    if [[ -z "$resolved" ]]; then
        warn "Домен $name не резолвится — сертификат не выпустится, пока не разойдётся DNS"
    elif [[ -n "$SERVER_IP" && "$resolved" != "$SERVER_IP" ]]; then
        warn "Домен $name ведёт на $resolved, а сервер — $SERVER_IP. Исправьте A-запись."
    else
        log "Домен $name указывает на этот сервер"
    fi
}

generate_keys() {
    mkdir -p "$GEN_DIR"
    if [[ -f "$KEYS_FILE" ]]; then
        log "Ключи Reality уже есть, переиспользую (перевыпуск разорвал бы работающие ключи)"
        return
    fi
    log "Генерирую ключи Reality…"
    dc run --rm --no-deps -T api python -m app.cli reality-keys > "$KEYS_FILE"
    chmod 600 "$KEYS_FILE"
}

render_xray_config() {
    local priv sid
    priv="$(python3 -c "import json;print(json.load(open('$KEYS_FILE'))['private_key'])")"
    sid="$(python3 -c "import json;print(json.load(open('$KEYS_FILE'))['short_id'])")"

    sed -e "s|{{NODE_CODE}}|$NODE_CODE|g" \
        -e "s|{{PORT}}|$XRAY_PORT|g" \
        -e "s|{{SNI}}|$XRAY_SNI|g" \
        -e "s|{{PRIVATE_KEY}}|$priv|g" \
        -e "s|{{SHORT_ID}}|$sid|g" \
        "$TEMPLATE" > "$XRAY_FILE"

    python3 -c "import json;json.load(open('$XRAY_FILE'))" \
        || fail "Сгенерированный конфиг Xray невалиден"
    log "Конфиг Xray собран: inbound VLESS_REALITY_$NODE_CODE на порту $XRAY_PORT, SNI $XRAY_SNI"
}

wait_for_panel() {
    log "Жду, пока панель поднимется…"
    for _ in $(seq 1 30); do
        if dc exec -T api python -c "
import asyncio, sys
from app.marzban import MarzbanClient, MarzbanError
async def main():
    try:
        async with MarzbanClient() as mz:
            await mz.list_inbounds()
    except MarzbanError:
        sys.exit(1)
asyncio.run(main())
" >/dev/null 2>&1; then
            log "Панель отвечает"
            return 0
        fi
        sleep 4
    done
    warn "Панель не ответила за 2 минуты. Логи: bash scripts/deploy-master.sh logs"
    return 1
}

case "$ACTION" in
    up|start|"")
        check_dns "$PUBLIC_HOST"
        check_dns "$PANEL_HOST"

        log "Собираю образы…"
        dc build

        generate_keys
        render_xray_config

        # Конфиг кладём в том ДО первого старта: если панель не найдёт файл,
        # на который указывает XRAY_JSON, ядро Xray не поднимется вовсе
        log "Кладу конфиг Xray в том панели…"
        docker volume inspect salt_marzban_data >/dev/null 2>&1 \
            || docker volume create salt_marzban_data >/dev/null
        docker run --rm \
            -v salt_marzban_data:/dst \
            -v "$GEN_DIR":/src:ro \
            alpine:3 sh -c 'cp /src/xray_config.json /dst/xray_config.json' >/dev/null

        log "Запускаю панель, бота, API, воркеры и Caddy…"
        dc up -d
        sleep 15

        if wait_for_panel; then
            log "Прописываю ноду в панели и в базе сервиса…"
            dc exec -T api python -m app.cli setup-marzban \
                --node-code "$NODE_CODE" \
                --location "$NODE_LOCATION" \
                --host "${SERVER_IP:-$PUBLIC_HOST}" \
                --port "$XRAY_PORT" \
                --sni "$XRAY_SNI" || warn "Не удалось настроить ноду автоматически"
        fi

        echo
        dc ps
        echo
        log "Панель: $PANEL_URL   логин: $MZ_USER"
        log "Проверка сайта: curl $PUBLIC_URL/health"
        log "Ключи Reality лежат в $KEYS_FILE (в git не попадают)"
        ;;

    logs)    dc logs -f --tail 100 ;;
    status|ps) dc ps ;;
    restart) dc restart ;;
    keys)
        [[ -f "$KEYS_FILE" ]] || fail "Ключи ещё не сгенерированы"
        cat "$KEYS_FILE"
        ;;
    update)
        git pull
        dc up -d --build
        ;;
    stop|down) dc down ;;
    *) fail "Неизвестная команда: $ACTION. Доступны: up, logs, status, restart, keys, update, stop" ;;
esac
