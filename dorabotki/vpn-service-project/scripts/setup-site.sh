#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  Выкатить публичный сайт (витрина + оферта) и прописать реквизиты.
#
#  Зачем отдельный скрипт: сайт нужен, чтобы платёжная система включила
#  приём оплаты, а до этого на корне домена был 404. Здесь собрано всё,
#  что для этого требуется, — искать проект и править .env руками не нужно.
#
#  Запуск (реквизиты подставьте свои):
#
#    bash setup-site.sh \
#      --name "Иванов Иван Иванович" \
#      --inn 123456789012 \
#      --email mail@example.com \
#      --phone "+7 900 000-00-00"      # необязательно
#      --address "г. Москва"           # необязательно
#
#  Скрипт сам находит каталог проекта, подтягивает свежий код,
#  дописывает реквизиты в .env (не дублируя) и перезапускает сервис.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

CLONE_URL="https://github.com/xxxmaxxxq/practice.git"
BRANCH="dev"

log()  { echo -e "\033[1;34m[+]\033[0m $*"; }
warn() { echo -e "\033[1;33m[!]\033[0m $*"; }
fail() { echo -e "\033[1;31m[✗]\033[0m $*" >&2; exit 1; }

NAME="" INN="" EMAIL="" PHONE="" ADDRESS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)    NAME="${2:-}";    shift 2 ;;
        --inn)     INN="${2:-}";     shift 2 ;;
        --email)   EMAIL="${2:-}";   shift 2 ;;
        --phone)   PHONE="${2:-}";   shift 2 ;;
        --address) ADDRESS="${2:-}"; shift 2 ;;
        *) fail "Неизвестный параметр: $1" ;;
    esac
done

[[ -n "$NAME"  ]] || fail "Укажите --name «Фамилия Имя Отчество»"
[[ -n "$INN"   ]] || fail "Укажите --inn (ИНН самозанятого)"
[[ -n "$EMAIL" ]] || fail "Укажите --email (почта для связи и возвратов)"

# ── 1. Найти проект ────────────────────────────────────────────────────────
log "Ищу каталог проекта…"
FOUND="$(find / -name deploy-master.sh -path '*/scripts/*' \
          -not -path '/proc/*' -not -path '/sys/*' 2>/dev/null | head -1 || true)"

if [[ -z "$FOUND" ]]; then
    warn "Проект на сервере не найден — клонирую в /opt/salt-vpn"
    command -v git >/dev/null || fail "Не установлен git: apt update && apt install -y git"
    git clone --branch "$BRANCH" "$CLONE_URL" /opt/salt-vpn
    FOUND="/opt/salt-vpn/vpn-service-project/scripts/deploy-master.sh"
fi

PROJECT_DIR="$(cd "$(dirname "$FOUND")/.." && pwd)"   # …/vpn-service-project
REPO_DIR="$(cd "$PROJECT_DIR/.." && pwd)"             # корень репозитория
log "Проект: $PROJECT_DIR"

# ── 2. Свежий код ──────────────────────────────────────────────────────────
log "Подтягиваю свежий код из ветки $BRANCH…"
cd "$REPO_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
git pull origin "$BRANCH"

# Доработки лежат отдельной папкой и переносятся в основную
if [[ -d "$REPO_DIR/dorabotki/vpn-service-project" ]]; then
    cp -r "$REPO_DIR/dorabotki/vpn-service-project/." "$PROJECT_DIR/"
fi

# ── 3. Реквизиты в .env ────────────────────────────────────────────────────
ENV_FILE="$PROJECT_DIR/.env"
[[ -f "$ENV_FILE" ]] || fail "Нет файла $ENV_FILE — сервис ещё не настроен"

# Личные данные в репозиторий не попадают: только в .env на сервере
set_env() {
    local key="$1" value="$2"
    [[ -n "$value" ]] || return 0
    if grep -q "^${key}=" "$ENV_FILE"; then
        # Значение может содержать / и пробелы — разделитель для sed берём редкий
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
    log "  $key прописан"
}

log "Прописываю реквизиты в .env…"
set_env OWNER_FULL_NAME "$NAME"
set_env OWNER_INN       "$INN"
set_env OWNER_EMAIL     "$EMAIL"
set_env OWNER_PHONE     "$PHONE"
set_env OWNER_ADDRESS   "$ADDRESS"
chmod 600 "$ENV_FILE"

# ── 4. Перезапуск ──────────────────────────────────────────────────────────
log "Пересобираю и перезапускаю сервис…"
cd "$PROJECT_DIR"
bash scripts/deploy-master.sh update >/dev/null 2>&1 \
    || bash scripts/deploy-master.sh restart

# ── 5. Проверка ────────────────────────────────────────────────────────────
DOMAIN="$(grep -E '^PUBLIC_BASE_URL=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' || true)"
DOMAIN="${DOMAIN:-https://vpn.xxxmaxxxq.ru}"

log "Жду, пока поднимется сайт…"
for attempt in $(seq 1 20); do
    sleep 3
    code="$(curl -s -o /dev/null -w '%{http_code}' "$DOMAIN/" || echo 000)"
    if [[ "$code" == "200" ]]; then
        echo
        log "Сайт работает: $DOMAIN"
        log "Оферта:        $DOMAIN/offer"
        echo
        log "Проверьте, что на странице видны ФИО и ИНН:"
        curl -s "$DOMAIN/" | grep -o "ИНН[^<]*" | head -2 || true
        echo
        log "Готово. Можно отправлять заявку в ЮKassa на проверку."
        exit 0
    fi
    [[ $((attempt % 5)) -eq 0 ]] && warn "Пока отвечает $code, жду…"
done

fail "Сайт не поднялся. Посмотрите логи: cd $PROJECT_DIR && bash scripts/deploy-master.sh logs"
