#!/usr/bin/env bash
#
# Установка «всё в одном» на чистый Ubuntu/Debian:
#   nginx с сайтом для SelfSteal, сертификат Let's Encrypt, 3x-ui,
#   инбаунды Hysteria2 и VLESS, AdGuard Home, Telegram-бот как сервис systemd.
#
# Запуск:
#   bash <(curl -fsSL https://raw.githubusercontent.com/IVAN4242/vpn-autoinstall/main/install.sh)
#
# Ответы можно передать заранее через переменные VK_* (см. README),
# а с --yes скрипт вообще не задаёт вопросов.

set -Eeuo pipefail
umask 022

REPO_URL="${VK_REPO_URL:-https://github.com/IVAN4242/vpn-autoinstall.git}"
REPO_BRANCH="${VK_REPO_BRANCH:-main}"
REPO_DIR="/opt/vpn-autoinstall"

# На этой версии проверены все вызовы API. Ставить новее можно, выбрав latest.
XUI_TESTED_VERSION="v3.11.0"
XUI_BIN="/usr/local/x-ui/x-ui"
XUI_RAW_URL="https://raw.githubusercontent.com/MHSanaei/3x-ui"

# Архив проверяется по контрольной сумме из релиза, поэтому версия закреплена.
ADGUARD_VERSION="v0.107.79"
ADGUARD_SHA256_AMD64="c48f4a43000665484c5ec28177de11a004759b620dae8f77b2aabefc9ef3687f"
ADGUARD_SHA256_ARM64="3f7893c18e8aaadc456d0452839190561c306ca95175a2254958be80a769c1ae"
ADGUARD_DIR="/opt/AdGuardHome"
NGINX_ADGUARD_SITE="/etc/nginx/sites-available/adguard-panel"
NGINX_ADGUARD_SNIPPET="/etc/nginx/snippets/adguard-proxy.conf"

STATE_DIR="/etc/vpn-autoinstall"
STATE_FILE="$STATE_DIR/state.env"
LOG_FILE="/var/log/vpn-autoinstall.log"
BOT_DIR="/opt/vpnbot"
BOT_DATA_DIR="/var/lib/vpnbot"
BOT_ENV="/etc/vpnbot.env"
ACME_HOME="/root/.acme.sh"
ACME_ROOT="/var/www/acme"
SOCKET_H1="/dev/shm/selfsteal-h1.sock"
SOCKET_H2="/dev/shm/selfsteal-h2.sock"
NGINX_SITE="/etc/nginx/sites-available/selfsteal"
NGINX_SNIPPET="/etc/nginx/snippets/selfsteal-site.conf"
NGINX_DROPIN="/etc/systemd/system/nginx.service.d/selfsteal.conf"
SYSCTL_FILE="/etc/sysctl.d/90-vpn-tuning.conf"

ASSUME_YES=0
RERUN=0
CURRENT_STEP="подготовка"

C_RED=$'\033[0;31m'
C_GREEN=$'\033[0;32m'
C_YELLOW=$'\033[0;33m'
C_BLUE=$'\033[0;34m'
C_BOLD=$'\033[1m'
C_OFF=$'\033[0m'

info() { printf '%s\n' "$*"; }
ok() { printf '%s✓%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%s! %s%s\n' "$C_YELLOW" "$*" "$C_OFF" >&2; }
die() {
    printf '%s✗ %s%s\n' "$C_RED" "$*" "$C_OFF" >&2
    exit 1
}
step() {
    CURRENT_STEP="$*"
    printf '\n%s==> %s%s\n' "$C_BLUE$C_BOLD" "$*" "$C_OFF"
    printf '\n=== %s\n' "$*" >>"$LOG_FILE"
}
on_error() {
    local code=$?
    printf '%s✗ Сбой на шаге «%s» (код %s).%s\n' "$C_RED" "$CURRENT_STEP" "$code" "$C_OFF" >&2
    printf 'Подробности: %s. Исправьте причину и запустите скрипт ещё раз: готовые шаги повторятся без потери данных.\n' "$LOG_FILE" >&2
    exit "$code"
}

# Журнал может содержать пароль панели из вывода её установщика, поэтому права 600.
init_log() {
    touch "$LOG_FILE"
    chmod 600 "$LOG_FILE"
}
quiet() { "$@" >>"$LOG_FILE" 2>&1; }

rand_alnum() {
    local length=$1 value
    # head закрывает канал раньше tr, поэтому pipefail здесь отключается на время подстановки.
    value=$(set +o pipefail; LC_ALL=C tr -dc 'a-zA-Z0-9' </dev/urandom | head -c "$length")
    printf '%s' "$value"
}

# ---------------------------------------------------------------------------
# Диалог
# ---------------------------------------------------------------------------

# Вопросы читаются из /dev/tty: так они работают и при запуске через `curl | bash`.
read_answer() {
    local prompt=$1 __target=$2
    [[ -r /dev/tty ]] || die "Нет терминала для вопросов. Запустите с --yes и задайте параметры через VK_*."
    IFS= read -r -p "$prompt" "${__target?}" </dev/tty
}

# ask ПЕРЕМЕННАЯ "вопрос" "значение по умолчанию" [валидатор] ["текст ошибки"]
# Заранее заданное значение переменной принимается без вопроса.
ask() {
    local __var=$1 prompt=$2 default=${3:-} validator=${4:-} error=${5:-Некорректное значение.}
    local __answer=${!__var:-} suffix=""
    [[ -n "$default" ]] && suffix=" [$default]"
    while true; do
        if [[ -z "$__answer" ]]; then
            if ((ASSUME_YES)); then
                __answer=$default
            else
                read_answer "$prompt$suffix: " __answer
                __answer=${__answer:-$default}
            fi
        fi
        if [[ -z "$validator" ]] || "$validator" "$__answer"; then
            printf -v "$__var" '%s' "$__answer"
            return 0
        fi
        ((ASSUME_YES)) && die "$__var: $error"
        warn "$error"
        __answer=""
    done
}

confirm() {
    local prompt=$1 default=${2:-y} answer=""
    if ((ASSUME_YES)); then
        [[ "$default" == "y" ]]
        return
    fi
    local hint="[Y/n]"
    [[ "$default" == "n" ]] && hint="[y/N]"
    read_answer "$prompt $hint: " answer
    answer=${answer:-$default}
    [[ "$answer" =~ ^[YyДд] ]]
}

is_domain() { [[ "$1" =~ ^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$ ]]; }
is_email_or_none() { [[ "$1" == "-" || "$1" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; }
is_bot_token() { [[ "$1" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]]; }
is_id_list() { [[ "$1" =~ ^[0-9]+([,[:space:]]+[0-9]+)*$ ]]; }
is_port() { [[ "$1" =~ ^[0-9]+$ ]] && (($1 >= 1 && $1 <= 65535)); }
is_port_or_off() { [[ "$1" == "0" ]] || is_port "$1"; }
is_positive() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }
is_credential() { [[ "$1" =~ ^[A-Za-z0-9._-]{4,64}$ ]]; }
is_site_choice() { [[ "$1" =~ ^[123]$ ]]; }
is_xui_version() { [[ "$1" == "latest" || "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; }

port_busy() {
    local proto=$1 port=$2 flags="-Hltn"
    [[ "$proto" == "udp" ]] && flags="-Hlun"
    [[ -n "$(ss "$flags" "sport = :$port" 2>/dev/null)" ]]
}

require_free_port() {
    local proto=$1 port=$2
    if port_busy "$proto" "$port"; then
        die "Порт $proto/$port занят: $(ss -Hlnp "sport = :$port" 2>/dev/null | head -n 1). Освободите его или выберите другой."
    fi
}

# ---------------------------------------------------------------------------
# Проверки до любых изменений в системе
# ---------------------------------------------------------------------------

preflight() {
    [[ $EUID -eq 0 ]] || die "Нужны права root. Запустите: sudo -i, затем команду установки."
    [[ -r /etc/os-release ]] || die "Не удалось определить ОС."
    # shellcheck disable=SC1091
    . /etc/os-release
    local major=${VERSION_ID%%.*}
    case "${ID:-}" in
        ubuntu) ((major >= 22)) || die "Нужна Ubuntu 22.04 или новее." ;;
        debian) ((major >= 11)) || die "Нужен Debian 11 или новее." ;;
        *) die "Поддерживаются только Ubuntu и Debian (обнаружено: ${ID:-неизвестно})." ;;
    esac
    case "$(uname -m)" in
        x86_64 | amd64 | aarch64 | arm64) ;;
        *) die "Архитектура $(uname -m) не поддерживается." ;;
    esac
    command -v systemctl >/dev/null || die "Нужен systemd."

    # Защита действующих серверов: установленную не этим скриптом панель не трогаем.
    if [[ -f "$STATE_FILE" ]]; then
        RERUN=1
    elif [[ -e "$XUI_BIN" || -e /etc/x-ui/x-ui.db || -e /etc/systemd/system/x-ui.service || -e "$BOT_ENV" ]]; then
        die "На сервере уже есть 3x-ui или бот, установленные не этим скриптом. Скрипт рассчитан на чистый сервер и ничего не изменил."
    fi
}

# При запуске одной командой рядом со скриптом нет остальных файлов репозитория.
bootstrap_repo() {
    local self_dir
    self_dir=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
    if [[ -f "$self_dir/bot/bot.py" && -d "$self_dir/templates" ]]; then
        SOURCE_DIR=$self_dir
        return
    fi
    info "Загружаю файлы проекта из $REPO_URL..."
    apt_install git ca-certificates
    if [[ -d "$REPO_DIR/.git" ]]; then
        quiet git -C "$REPO_DIR" pull --ff-only
    else
        quiet git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"
    fi
    exec bash "$REPO_DIR/install.sh" "$@"
}

APT_UPDATED=0
apt_install() {
    export DEBIAN_FRONTEND=noninteractive
    if ((!APT_UPDATED)); then
        quiet apt-get update -y
        APT_UPDATED=1
    fi
    quiet apt-get install -y --no-install-recommends "$@"
}

public_ipv4() {
    local url ip
    for url in https://api4.ipify.org https://ipv4.icanhazip.com https://4.ident.me; do
        ip=$(curl -4 -fsS -m 8 "$url" 2>/dev/null | tr -d '[:space:]') || continue
        if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            printf '%s' "$ip"
            return 0
        fi
    done
    return 1
}

dns_a_records() {
    dig +short +time=3 +tries=1 A "$VK_DOMAIN" "$@" 2>/dev/null | sort -u | grep -E '^[0-9]+(\.[0-9]+){3}$'
}

# Не через getent: он читает /etc/hosts раньше DNS, а на многих VPS
# имя хоста там записано как 127.0.1.1. Сертификат выдаётся по публичной записи.
domain_a_records() {
    dns_a_records @1.1.1.1 || dns_a_records @8.8.8.8 || dns_a_records
}

check_dns() {
    local server_ip resolved shown
    server_ip=$(public_ipv4) || {
        warn "Не удалось определить внешний IP, проверка DNS пропущена."
        return 0
    }
    while true; do
        resolved=$(domain_a_records) || resolved=""
        if [[ "$resolved" == "$server_ip" ]]; then
            VK_SERVER_IP=$server_ip
            ok "DNS: $VK_DOMAIN указывает на этот сервер ($server_ip)"
            return 0
        fi
        shown=${resolved//$'\n'/, }
        warn "$VK_DOMAIN указывает на «${shown:-ничего}», а IP сервера — $server_ip."
        warn "Нужна A-запись на этот IP. При Cloudflare выключите проксирование (серое облако)."
        ((ASSUME_YES)) && die "DNS не настроен, сертификат выпустить не получится."
        confirm "Подождать и проверить ещё раз?" y || die "Настройте DNS и запустите скрипт снова."
        sleep 20
    done
}

check_bot_token() {
    local body code
    body=$(mktemp)
    code=$(curl -sS -m 15 -o "$body" -w '%{http_code}' -K - 2>/dev/null \
        <<<"url = \"https://api.telegram.org/bot$VK_BOT_TOKEN/getMe\"") || code=000
    case "$code" in
        200)
            BOT_USERNAME=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["result"]["username"])' "$body")
            ok "Бот найден: @$BOT_USERNAME"
            rm -f "$body"
            return 0
            ;;
        401 | 404)
            rm -f "$body"
            warn "Telegram отклонил токен."
            return 1
            ;;
        *)
            rm -f "$body"
            warn "Telegram не ответил (код $code), токен проверить не удалось. Продолжаю."
            return 0
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Сбор параметров
# ---------------------------------------------------------------------------

collect_bot_settings() {
    while true; do
        ask VK_BOT_TOKEN "Токен Telegram-бота (получить у @BotFather)" "" is_bot_token \
            "Токен выглядит так: 123456789:AAE..."
        check_bot_token && break
        ((ASSUME_YES)) && die "Неверный токен бота."
        VK_BOT_TOKEN=""
    done
    ask VK_ADMIN_IDS "Числовой Telegram ID администратора, можно несколько через запятую (узнать у @userinfobot)" "" \
        is_id_list "Нужны только числовые ID."
    VK_ADMIN_IDS=$(sed -E 's/[,[:space:]]+/,/g' <<<"$VK_ADMIN_IDS")
    ask VK_NEW_USER_DAYS "Срок доступа новых пользователей, дней" "365" is_positive "Нужно целое число больше нуля."
}

collect_adguard_settings() {
    ask VK_ADGUARD_PORT "TCP-порт панели AdGuard" "8443" is_port "Нужен порт от 1 до 65535."
    ask VK_ADGUARD_PASS "Пароль панели AdGuard (логин admin)" "$(rand_alnum 20)" is_credential \
        "4–64 символа: латиница, цифры, «.», «_», «-»."
}

check_adguard_ports() {
    [[ "$VK_ADGUARD_PORT" != "$VK_TLS_PORT" && "$VK_ADGUARD_PORT" != "$VK_PANEL_PORT" && "$VK_ADGUARD_PORT" != "80" ]] ||
        die "Порт панели AdGuard конфликтует с другим портом."
    require_free_port tcp "$VK_ADGUARD_PORT"
    require_free_port tcp 3000
    # Заглушка systemd-resolved на 127.0.0.53 не мешает, а всё остальное на этих адресах занимает порт.
    if ss -Hlun 'sport = :53' 2>/dev/null | grep -qE '(127\.0\.0\.1|0\.0\.0\.0|\*|\[::\]):53\b'; then
        die "Порт 53 на 127.0.0.1 занят другим DNS-сервером."
    fi
}

collect_settings() {
    step "Параметры установки"
    info "В квадратных скобках — значение по умолчанию, его принимает Enter."
    echo
    VK_SERVER_IP=${VK_SERVER_IP:-}

    ask VK_DOMAIN "Домен сайта для маскировки (A-запись уже указывает на этот сервер)" "" is_domain \
        "Нужен домен вида example.com или vpn.example.com."
    check_dns
    ask VK_ACME_EMAIL "E-mail для Let's Encrypt, «-» чтобы пропустить" "-" is_email_or_none "Некорректный e-mail."

    ask VK_BOT "Установить Telegram-бота для выдачи подключений? (y/n)" "y"
    if bot_enabled; then
        collect_bot_settings
    else
        VK_BOT_TOKEN=""
        VK_ADMIN_IDS=""
        VK_NEW_USER_DAYS=""
    fi

    echo
    info "Сайт для маскировки:"
    info "  1) Студия — лендинг небольшой компании"
    info "  2) Заметки — личный блог"
    info "  3) Заглушка — «Сайт скоро откроется»"
    ask VK_SITE_CHOICE "Шаблон" "1" is_site_choice "Выберите 1, 2 или 3."
    local default_title=${VK_DOMAIN%%.*}
    ask VK_SITE_TITLE "Название сайта" "${default_title^}"

    echo
    ask VK_HY2_PORT "UDP-порт Hysteria2" "443" is_port "Нужен порт от 1 до 65535."
    ask VK_HY2_OBFS_PORT "UDP-порт Hysteria2 с salamander, 0 чтобы отключить" "2096" is_port_or_off \
        "Нужен порт от 1 до 65535 или 0."
    ask VK_TLS_PORT "TCP-порт сайта и VLESS" "443" is_port "Нужен порт от 1 до 65535."
    ask VK_XHTTP "Создать ещё VLESS XHTTP? (y/n)" "y"
    ask VK_PANEL_PORT "TCP-порт панели 3x-ui" "$(shuf -i 20000-59999 -n 1)" is_port "Нужен порт от 1 до 65535."
    ask VK_PANEL_USER "Логин панели" "$(rand_alnum 10)" is_credential "4–64 символа: латиница, цифры, «.», «_», «-»."
    ask VK_PANEL_PASS "Пароль панели" "$(rand_alnum 24)" is_credential "4–64 символа: латиница, цифры, «.», «_», «-»."
    ask VK_XUI_VERSION "Версия 3x-ui: vX.Y.Z или latest (проверена $XUI_TESTED_VERSION)" "$XUI_TESTED_VERSION" \
        is_xui_version "Нужно vX.Y.Z или latest."
    ask VK_ADGUARD "Установить AdGuard Home (блокировка рекламы и трекеров, своя панель)? (y/n)" "y"
    if adguard_enabled; then
        collect_adguard_settings
    else
        VK_ADGUARD_PORT=""
        VK_ADGUARD_PASS=""
    fi
    ask VK_UFW "Включить файрвол UFW и открыть только нужные порты? (y/n)" "y"
    ask VK_TUNING "Включить BBR и увеличить буферы UDP? (y/n)" "y"

    [[ "$VK_HY2_OBFS_PORT" == "0" ]] && VK_HY2_OBFS_PORT=""
    [[ "$VK_HY2_PORT" != "$VK_HY2_OBFS_PORT" ]] || die "Порты двух инбаундов Hysteria2 должны различаться."
    [[ "$VK_PANEL_PORT" != "$VK_TLS_PORT" && "$VK_PANEL_PORT" != "80" ]] || die "Порт панели конфликтует с портом сайта."
    [[ "$VK_TLS_PORT" != "80" ]] || die "TCP 80 занят под выпуск сертификата."

    VK_WEB_PATH=${VK_WEB_PATH:-$(rand_alnum 18)}
    VK_API_TOKEN=""
    VK_XHTTP_LOCAL_PORT=${VK_XHTTP_LOCAL_PORT:-$(shuf -i 10000-19999 -n 1)}
    VK_XHTTP_PATH=""
    if [[ "$VK_XHTTP" =~ ^[YyДд] ]]; then
        VK_XHTTP_PATH="/$(rand_alnum 10)"
    fi

    require_free_port tcp 80
    require_free_port tcp "$VK_TLS_PORT"
    require_free_port tcp "$VK_PANEL_PORT"
    require_free_port udp "$VK_HY2_PORT"
    adguard_enabled && check_adguard_ports
    [[ -z "$VK_HY2_OBFS_PORT" ]] || require_free_port udp "$VK_HY2_OBFS_PORT"

    echo
    info "${C_BOLD}Будет установлено:${C_OFF}"
    info "  Сайт:           https://$VK_DOMAIN$(tls_port_suffix)"
    info "  Hysteria2:      UDP $VK_HY2_PORT${VK_HY2_OBFS_PORT:+, UDP $VK_HY2_OBFS_PORT (salamander)}"
    info "  VLESS Vision:   TCP $VK_TLS_PORT${VK_XHTTP_PATH:+; VLESS XHTTP на том же порту}"
    info "  Панель 3x-ui:   $VK_XUI_VERSION, TCP $VK_PANEL_PORT, скрытый путь"
    if adguard_enabled; then
        info "  AdGuard Home:   $ADGUARD_VERSION, панель на TCP $VK_ADGUARD_PORT"
    fi
    if bot_enabled; then
        info "  Telegram-бот:   ${BOT_USERNAME:+@$BOT_USERNAME, }администраторы $VK_ADMIN_IDS"
    fi
    echo
    confirm "Начать установку?" y || die "Установка отменена, сервер не изменён."
    save_state
}

adguard_enabled() { [[ "$VK_ADGUARD" =~ ^[YyДд] ]]; }
bot_enabled() { [[ "$VK_BOT" =~ ^[YyДд] ]]; }

tls_port_suffix() {
    [[ "$VK_TLS_PORT" == "443" ]] || printf ':%s' "$VK_TLS_PORT"
}

STATE_VARS=(
    VK_DOMAIN VK_ACME_EMAIL VK_BOT VK_BOT_TOKEN VK_ADMIN_IDS VK_NEW_USER_DAYS VK_SITE_CHOICE VK_SITE_TITLE
    VK_HY2_PORT VK_HY2_OBFS_PORT VK_TLS_PORT VK_XHTTP VK_XHTTP_PATH VK_XHTTP_LOCAL_PORT
    VK_PANEL_PORT VK_PANEL_USER VK_PANEL_PASS VK_WEB_PATH VK_XUI_VERSION VK_UFW VK_TUNING
    VK_ADGUARD VK_ADGUARD_PORT VK_ADGUARD_PASS VK_SERVER_IP VK_API_TOKEN
)

# Сохраняется до начала работы, чтобы после сбоя повтор продолжил с теми же секретами.
save_state() {
    local name
    install -d -m 700 "$STATE_DIR"
    : >"$STATE_FILE"
    chmod 600 "$STATE_FILE"
    for name in "${STATE_VARS[@]}"; do
        printf '%s=%q\n' "$name" "${!name}" >>"$STATE_FILE"
    done
}

# Добавляем компонент? Заранее заданная переменная важнее вопроса.
wants_component() {
    local preset=$1 prompt=$2
    if [[ -n "$preset" ]]; then
        [[ "$preset" =~ ^[YyДд] ]]
    else
        confirm "$prompt" n
    fi
}

load_state() {
    # Файл состояния перезапишет переменные, поэтому заданные при запуске значения сохраняем.
    local want_bot=${VK_BOT:-} want_adguard=${VK_ADGUARD:-}
    local token=${VK_BOT_TOKEN:-} admins=${VK_ADMIN_IDS:-} days=${VK_NEW_USER_DAYS:-}
    local adguard_port=${VK_ADGUARD_PORT:-} adguard_pass=${VK_ADGUARD_PASS:-}
    # shellcheck disable=SC1090
    . "$STATE_FILE"
    info "Найдена предыдущая установка для ${C_BOLD}$VK_DOMAIN${C_OFF}."
    info "Панель, пользователи и ссылки сохранятся. Во время работы скрипта VPN ненадолго прервётся."
    echo

    if ! bot_enabled && wants_component "$want_bot" "Добавить Telegram-бота?"; then
        VK_BOT=y VK_BOT_TOKEN=$token VK_ADMIN_IDS=$admins VK_NEW_USER_DAYS=$days
        collect_bot_settings
    fi
    if ! adguard_enabled && wants_component "$want_adguard" "Добавить AdGuard Home?"; then
        VK_ADGUARD=y VK_ADGUARD_PORT=$adguard_port VK_ADGUARD_PASS=$adguard_pass
        collect_adguard_settings
        check_adguard_ports
        [[ -n "$VK_SERVER_IP" ]] || VK_SERVER_IP=$(public_ipv4) || VK_SERVER_IP=""
    fi

    confirm "Продолжить?" y || die "Отменено, сервер не изменён."
    save_state
}

# ---------------------------------------------------------------------------
# Шаги установки
# ---------------------------------------------------------------------------

install_dependencies() {
    step "Установка зависимостей"
    local packages=(curl git nginx openssl python3 ca-certificates cron socat tar tzdata iproute2)
    bot_enabled && packages+=(python3-venv)
    adguard_enabled && packages+=(apache2-utils bind9-dnsutils)
    [[ "$VK_UFW" =~ ^[YyДд] ]] && packages+=(ufw)
    apt_install "${packages[@]}"
    ok "Пакеты установлены"
}

# render ШАБЛОН ЦЕЛЬ КЛЮЧ=ЗНАЧЕНИЕ...
render() {
    local source=$1 target=$2 content pair
    shift 2
    content=$(<"$source")
    for pair in "$@"; do
        content=${content//"__${pair%%=*}__"/"${pair#*=}"}
    done
    printf '%s\n' "$content" >"$target"
}

html_escape() {
    local text=$1
    text=${text//&/&amp;}
    text=${text//</&lt;}
    text=${text//>/&gt;}
    printf '%s' "$text"
}

# У nginx до 1.25.1 нет директивы `http2 on`, а с `listen ... http2` новые версии выдают предупреждение.
nginx_listen_h2() {
    local version
    version=$(nginx -v 2>&1 | sed -E 's#.*nginx/([0-9.]+).*#\1#')
    if [[ "$(printf '%s\n' "1.25.1" "$version" | sort -V | head -n 1)" == "1.25.1" ]]; then
        printf 'listen unix:%s proxy_protocol;\n    http2 on;' "$SOCKET_H2"
    else
        printf 'listen unix:%s proxy_protocol http2;' "$SOCKET_H2"
    fi
}

nginx_listen_adguard() {
    local version
    version=$(nginx -v 2>&1 | sed -E 's#.*nginx/([0-9.]+).*#\1#')
    if [[ "$(printf '%s\n' "1.25.1" "$version" | sort -V | head -n 1)" == "1.25.1" ]]; then
        printf 'listen %s ssl default_server;\n    http2 on;' "$VK_ADGUARD_PORT"
    else
        printf 'listen %s ssl http2 default_server;' "$VK_ADGUARD_PORT"
    fi
}

setup_site() {
    step "Сайт для маскировки и nginx"
    local web_root="/var/www/$VK_DOMAIN" template xhttp_block=""
    case "$VK_SITE_CHOICE" in
        2) template=notes ;;
        3) template=minimal ;;
        *) template=studio ;;
    esac

    install -d -m 755 "$web_root" "$ACME_ROOT" /etc/nginx/snippets
    # При повторном запуске свою страницу не перезаписываем.
    if [[ ! -f "$web_root/index.html" ]]; then
        render "$SOURCE_DIR/templates/site/$template/index.html" "$web_root/index.html" \
            "SITE_TITLE=$(html_escape "$VK_SITE_TITLE")"
    fi

    if [[ -n "$VK_XHTTP_PATH" ]]; then
        xhttp_block=$(
            render "$SOURCE_DIR/templates/nginx-xhttp.location" /dev/stdout \
                "XHTTP_PATH=$VK_XHTTP_PATH" "XHTTP_LOCAL_PORT=$VK_XHTTP_LOCAL_PORT"
        )
    fi
    render "$SOURCE_DIR/templates/nginx-site.snippet" "$NGINX_SNIPPET" "WEB_ROOT=$web_root"
    render "$SOURCE_DIR/templates/nginx-selfsteal.conf" "$NGINX_SITE" \
        "SOCKET_H1=$SOCKET_H1" "LISTEN_H2=$(nginx_listen_h2)" "DOMAIN=$VK_DOMAIN" \
        "XHTTP_LOCATION=$xhttp_block" "ACME_ROOT=$ACME_ROOT" "TLS_PORT_SUFFIX=$(tls_port_suffix)"
    ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/selfsteal
    rm -f /etc/nginx/sites-enabled/default

    # После аварийной остановки nginx сокеты остаются на диске и мешают его запуску.
    install -d -m 755 "$(dirname "$NGINX_DROPIN")"
    cat >"$NGINX_DROPIN" <<EOF
[Service]
ExecStartPre=-/bin/rm -f $SOCKET_H1 $SOCKET_H2
EOF
    systemctl daemon-reload
    quiet nginx -t || die "Конфигурация nginx не прошла проверку, см. $LOG_FILE."
    quiet systemctl enable nginx
    quiet systemctl restart nginx
    ok "Сайт и nginx настроены (шаблон: $template)"
}

issue_certificate() {
    step "Сертификат Let's Encrypt"
    local cert_dir="/root/cert/$VK_DOMAIN" code=0
    CERT_FILE="$cert_dir/fullchain.pem"
    KEY_FILE="$cert_dir/privkey.pem"

    if [[ ! -x "$ACME_HOME/acme.sh" ]]; then
        local installer
        installer=$(mktemp)
        curl -fsSL -m 60 https://get.acme.sh -o "$installer" || die "Не удалось скачать acme.sh."
        if [[ "$VK_ACME_EMAIL" == "-" ]]; then
            (cd /tmp && HOME=/root quiet sh "$installer") || die "Не удалось установить acme.sh."
        else
            (cd /tmp && HOME=/root quiet sh "$installer" "email=$VK_ACME_EMAIL") || die "Не удалось установить acme.sh."
        fi
        rm -f "$installer"
    fi

    install -d -m 700 /root/cert "$cert_dir"
    quiet "$ACME_HOME/acme.sh" --issue --server letsencrypt -d "$VK_DOMAIN" \
        --webroot "$ACME_ROOT" --keylength ec-256 || code=$?
    # Код 2 означает, что действующий сертификат есть и продлевать рано.
    if ((code != 0 && code != 2)); then
        die "Не выпустить сертификат. Проверьте A-запись $VK_DOMAIN и доступность TCP 80 снаружи (в том числе в панели хостера). Лог: $LOG_FILE."
    fi
    quiet "$ACME_HOME/acme.sh" --install-cert --ecc -d "$VK_DOMAIN" \
        --key-file "$KEY_FILE" --fullchain-file "$CERT_FILE" \
        --reloadcmd "systemctl restart x-ui >/dev/null 2>&1; systemctl reload nginx >/dev/null 2>&1 || true" ||
        die "Не удалось установить сертификат, см. $LOG_FILE."
    chmod 600 "$KEY_FILE"
    ok "Сертификат готов, продление настроено"
}

setup_adguard() {
    adguard_enabled || return 0
    step "AdGuard Home"
    local config="$ADGUARD_DIR/AdGuardHome.yaml"

    if [[ ! -x "$ADGUARD_DIR/AdGuardHome" ]]; then
        local arch sha archive
        case "$(uname -m)" in
            aarch64 | arm64)
                arch=arm64
                sha=$ADGUARD_SHA256_ARM64
                ;;
            *)
                arch=amd64
                sha=$ADGUARD_SHA256_AMD64
                ;;
        esac
        archive=$(mktemp)
        curl -fsSL -m 180 -o "$archive" \
            "https://github.com/AdguardTeam/AdGuardHome/releases/download/$ADGUARD_VERSION/AdGuardHome_linux_$arch.tar.gz" ||
            die "Не удалось скачать AdGuard Home."
        sha256sum -c - <<<"$sha  $archive" >>"$LOG_FILE" 2>&1 || die "Контрольная сумма AdGuard Home не совпала."
        tar -xzf "$archive" -C /opt
        rm -f "$archive"
    fi

    # При повторном запуске настройки из панели AdGuard не перезаписываются.
    if [[ ! -f "$config" ]]; then
        local admin_hash rewrites="  rewrites: []"
        admin_hash=$(htpasswd -inBC 10 admin <<<"$VK_ADGUARD_PASS" | cut -d: -f2)
        [[ -n "$admin_hash" ]] || die "Не удалось создать хеш пароля AdGuard."
        # Через VPN свой домен открывается по адресу из этой записи, а не из кэша внешнего DNS.
        if [[ -n "$VK_SERVER_IP" ]]; then
            printf -v rewrites '  rewrites:\n    - domain: %s\n      answer: %s\n      enabled: true' "$VK_DOMAIN" "$VK_SERVER_IP"
        fi
        render "$SOURCE_DIR/templates/AdGuardHome.yaml" "$config" \
            "ADMIN_HASH=$admin_hash" "REWRITES_BLOCK=$rewrites"
    fi
    chmod 600 "$config"

    install -m 644 "$SOURCE_DIR/templates/AdGuardHome.service" /etc/systemd/system/AdGuardHome.service
    systemctl daemon-reload
    quiet systemctl enable AdGuardHome
    quiet systemctl restart AdGuardHome

    install -m 644 "$SOURCE_DIR/templates/nginx-adguard-proxy.snippet" "$NGINX_ADGUARD_SNIPPET"
    render "$SOURCE_DIR/templates/nginx-adguard.conf" "$NGINX_ADGUARD_SITE" \
        "DOMAIN=$VK_DOMAIN" "ADGUARD_PORT=$VK_ADGUARD_PORT" "LISTEN_ADGUARD=$(nginx_listen_adguard)" \
        "CERT_FILE=$CERT_FILE" "KEY_FILE=$KEY_FILE"
    ln -sf "$NGINX_ADGUARD_SITE" /etc/nginx/sites-enabled/adguard-panel
    quiet nginx -t || die "Конфигурация nginx для AdGuard не прошла проверку, см. $LOG_FILE."
    quiet systemctl reload nginx
    ok "AdGuard Home работает, панель на порту $VK_ADGUARD_PORT"
}

# Порт и путь берутся из панели: их могли изменить в её настройках.
# Новый токен заменяет старый с тем же именем и не зависит от пароля и 2FA.
read_panel_access() {
    local settings port path
    settings=$("$XUI_BIN" setting -show)
    port=$(awk '$1 == "port:" {print $2}' <<<"$settings")
    path=$(awk '$1 == "webBasePath:" {print $2}' <<<"$settings")
    path=${path#/}
    path=${path%/}
    [[ -n "$port" && -n "$path" ]] || die "Не удалось прочитать настройки панели."
    VK_PANEL_PORT=$port
    VK_WEB_PATH=$path
    VK_API_TOKEN=$("$XUI_BIN" setting -getApiToken -tokenName vpn-autoinstall -tokenScope admin |
        awk '$1 == "apiToken:" {print $2}')
    [[ -n "$VK_API_TOKEN" ]] || die "Не удалось получить API-токен панели."
    save_state
}

install_xui() {
    step "Панель 3x-ui"
    CERT_FILE="/root/cert/$VK_DOMAIN/fullchain.pem"
    KEY_FILE="/root/cert/$VK_DOMAIN/privkey.pem"
    if [[ -x "$XUI_BIN" ]]; then
        ok "Панель уже установлена"
    else
        local ref=$VK_XUI_VERSION args=("$VK_XUI_VERSION") script
        if [[ "$VK_XUI_VERSION" == "latest" ]]; then
            ref=main
            args=()
        fi
        script=$(mktemp)
        curl -fsSL -m 60 "$XUI_RAW_URL/$ref/install.sh" -o "$script" || die "Не удалось скачать установщик 3x-ui."
        info "Скачиваю и ставлю 3x-ui $VK_XUI_VERSION из MHSanaei/3x-ui..."
        if ! XUI_NONINTERACTIVE=1 XUI_SSL_MODE=none \
            XUI_USERNAME="$VK_PANEL_USER" XUI_PASSWORD="$VK_PANEL_PASS" \
            XUI_PANEL_PORT="$VK_PANEL_PORT" XUI_WEB_BASE_PATH="$VK_WEB_PATH" \
            quiet bash "$script" ${args[@]+"${args[@]}"} </dev/null; then
            die "Установщик 3x-ui завершился с ошибкой, см. $LOG_FILE."
        fi
        rm -f "$script"
        [[ -x "$XUI_BIN" ]] || die "После установки не найден $XUI_BIN."
    fi
    quiet "$XUI_BIN" cert -webCert "$CERT_FILE" -webCertKey "$KEY_FILE"
    read_panel_access
    quiet systemctl enable x-ui
    quiet systemctl restart x-ui
    ok "Панель работает"
}

provision_inbounds() {
    step "Инбаунды Hysteria2 и VLESS"
    # Панель настраивается с этого же сервера, поэтому имя в сертификате не проверяется.
    local adguard_flag=""
    adguard_enabled && adguard_flag=1
    VK_ADGUARD_DNS="$adguard_flag" VK_CERT_FILE="$CERT_FILE" VK_KEY_FILE="$KEY_FILE" \
        VK_PANEL_URL="https://127.0.0.1:$VK_PANEL_PORT/$VK_WEB_PATH" VK_PANEL_INSECURE=1 \
        VK_SOCKET_H1="$SOCKET_H1" VK_SOCKET_H2="$SOCKET_H2" \
        VK_DOMAIN="$VK_DOMAIN" VK_API_TOKEN="$VK_API_TOKEN" \
        VK_HY2_PORT="$VK_HY2_PORT" VK_HY2_OBFS_PORT="$VK_HY2_OBFS_PORT" VK_TLS_PORT="$VK_TLS_PORT" \
        VK_XHTTP_PATH="$VK_XHTTP_PATH" VK_XHTTP_LOCAL_PORT="$VK_XHTTP_LOCAL_PORT" \
        python3 "$SOURCE_DIR/scripts/provision_inbounds.py" || die "Не удалось создать инбаунды."
    # Xray собирает config.json при запуске: так ловятся ошибки в настройках fallback.
    quiet systemctl restart x-ui
    ok "Инбаунды созданы"
}

setup_bot() {
    bot_enabled || return 0
    step "Telegram-бот"
    id vpnbot >/dev/null 2>&1 || quiet useradd --system --home-dir "$BOT_DATA_DIR" --shell /usr/sbin/nologin vpnbot
    install -d -m 755 "$BOT_DIR"
    install -d -m 700 -o vpnbot -g vpnbot "$BOT_DATA_DIR"
    install -m 644 "$SOURCE_DIR"/bot/{bot.py,xui.py,store.py,instructions.md,ADMIN.md,requirements.txt} "$BOT_DIR/"

    [[ -x "$BOT_DIR/venv/bin/python" ]] || quiet python3 -m venv "$BOT_DIR/venv"
    quiet "$BOT_DIR/venv/bin/pip" install --upgrade pip
    quiet "$BOT_DIR/venv/bin/pip" install -r "$BOT_DIR/requirements.txt" || die "Не удалось установить зависимости бота."

    : >"$BOT_ENV"
    chmod 600 "$BOT_ENV"
    cat >"$BOT_ENV" <<EOF
BOT_TOKEN=$VK_BOT_TOKEN
XUI_BASE_URL=https://$VK_DOMAIN:$VK_PANEL_PORT/$VK_WEB_PATH
XUI_API_TOKEN=$VK_API_TOKEN
ADMIN_IDS=$VK_ADMIN_IDS
NEW_USER_DAYS=$VK_NEW_USER_DAYS
USERS_FILE=$BOT_DATA_DIR/users.json
INSTRUCTIONS_FILE=$BOT_DIR/instructions.md
EOF
    render "$SOURCE_DIR/templates/vpnbot.service" /etc/systemd/system/vpnbot.service "BOT_DIR=$BOT_DIR"
    systemctl daemon-reload
    quiet systemctl enable vpnbot
    quiet systemctl restart vpnbot
    ok "Бот установлен как сервис vpnbot"
}

# Бот ходит в панель по домену. Некоторые хостеры не пропускают обращение сервера к своему же внешнему IP.
ensure_panel_reachable() {
    bot_enabled || return 0
    local url="https://$VK_DOMAIN:$VK_PANEL_PORT/$VK_WEB_PATH/"
    curl -fsS -m 10 -o /dev/null "$url" 2>/dev/null && return 0
    if ! grep -qE "^127\.0\.0\.1[[:space:]].*\b$VK_DOMAIN\b" /etc/hosts; then
        printf '127.0.0.1 %s\n' "$VK_DOMAIN" >>/etc/hosts
        warn "Сервер не видит сам себя по внешнему IP, в /etc/hosts добавлена запись для $VK_DOMAIN."
    fi
}

setup_tuning() {
    [[ "$VK_TUNING" =~ ^[YyДд] ]] || return 0
    step "Настройка сети"
    modprobe tcp_bbr 2>/dev/null || true
    cat >"$SYSCTL_FILE" <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.rmem_default = 4194304
net.core.netdev_max_backlog = 5000
EOF
    if quiet sysctl -p "$SYSCTL_FILE"; then
        quiet systemctl restart x-ui
        ok "BBR и буферы UDP включены"
    else
        warn "Часть настроек ядра не применилась (обычно это ограничения контейнера). На работу VPN это не влияет."
    fi
}

setup_firewall() {
    [[ "$VK_UFW" =~ ^[YyДд] ]] || return 0
    step "Файрвол UFW"
    local port ssh_ports
    # Открываем порт текущей сессии, чтобы не потерять доступ при нестандартном SSH.
    ssh_ports=$({
        { sshd -T 2>/dev/null || true; } | awk '$1 == "port" {print $2}'
        [[ -z "${SSH_CONNECTION:-}" ]] || awk '{print $4}' <<<"$SSH_CONNECTION"
    } | sort -u)
    [[ -n "$ssh_ports" ]] || ssh_ports=22
    for port in $ssh_ports; do
        quiet ufw allow "$port/tcp"
    done
    quiet ufw allow 80/tcp
    quiet ufw allow "$VK_TLS_PORT/tcp"
    quiet ufw allow "$VK_PANEL_PORT/tcp"
    quiet ufw allow "$VK_HY2_PORT/udp"
    [[ -z "$VK_HY2_OBFS_PORT" ]] || quiet ufw allow "$VK_HY2_OBFS_PORT/udp"
    [[ -z "$VK_ADGUARD_PORT" ]] || quiet ufw allow "$VK_ADGUARD_PORT/tcp"
    if quiet ufw --force enable; then
        ok "UFW включён, открыты только нужные порты"
    else
        warn "Не удалось включить UFW (подробности в $LOG_FILE). Закройте лишние порты у хостера."
    fi
}

verify_install() {
    step "Проверка"
    local failed=0 resolve="$VK_DOMAIN:$VK_TLS_PORT:127.0.0.1" url
    url="https://$VK_DOMAIN:$VK_TLS_PORT/"
    sleep 3

    check_item() {
        local label=$1
        shift
        if "$@" >>"$LOG_FILE" 2>&1; then
            ok "$label"
        else
            warn "Не прошло: $label"
            failed=1
        fi
    }
    check_item "сервис x-ui работает" systemctl is-active x-ui
    check_item "сервис nginx работает" systemctl is-active nginx
    check_item "конфигурация Xray корректна" check_xray
    check_item "сайт открывается по HTTP/1.1" curl -fsS -m 10 --http1.1 --resolve "$resolve" -o /dev/null "$url"
    check_item "сайт открывается по HTTP/2" curl -fsS -m 10 --http2 --resolve "$resolve" -o /dev/null "$url"
    check_item "Hysteria2 слушает UDP $VK_HY2_PORT" port_busy udp "$VK_HY2_PORT"
    if [[ -n "$VK_HY2_OBFS_PORT" ]]; then
        check_item "Hysteria2 salamander слушает UDP $VK_HY2_OBFS_PORT" port_busy udp "$VK_HY2_OBFS_PORT"
    fi
    if [[ -n "$VK_XHTTP_PATH" ]]; then
        check_item "XHTTP слушает локальный порт" port_busy tcp "$VK_XHTTP_LOCAL_PORT"
    fi
    if adguard_enabled; then
        check_item "сервис AdGuardHome работает" systemctl is-active AdGuardHome
        check_item "AdGuard отвечает на DNS-запросы" dig +short +time=3 +tries=2 @127.0.0.1 "$VK_DOMAIN"
        check_item "панель AdGuard открывается" curl -fsS -m 10 \
            --resolve "$VK_DOMAIN:$VK_ADGUARD_PORT:127.0.0.1" -o /dev/null "https://$VK_DOMAIN:$VK_ADGUARD_PORT/login.html"
        check_item "Xray использует AdGuard как DNS" grep -q 'dns_inbound' /usr/local/x-ui/bin/config.json
    fi
    if bot_enabled; then
        sleep 3
        check_item "сервис vpnbot работает" systemctl is-active vpnbot
    fi
    ((failed == 0)) || warn "Часть проверок не прошла. Журналы: journalctl -u x-ui -u vpnbot -u nginx -n 50"
}

check_xray() {
    local binary
    binary=$(find /usr/local/x-ui/bin -maxdepth 1 -name 'xray-linux-*' | head -n 1)
    "$binary" -test -config /usr/local/x-ui/bin/config.json
}

print_summary() {
    printf '\n%s%s══ Установка завершена ══%s\n\n' "$C_GREEN" "$C_BOLD" "$C_OFF"
    info "Сайт:          https://$VK_DOMAIN$(tls_port_suffix)"
    info "Панель 3x-ui:  https://$VK_DOMAIN:$VK_PANEL_PORT/$VK_WEB_PATH/"
    info "Логин:         $VK_PANEL_USER"
    info "Пароль:        $VK_PANEL_PASS"
    ((RERUN)) && info "               (указаны при первой установке; если вы их меняли, действуют новые)"
    if adguard_enabled; then
        echo
        info "AdGuard Home:  https://$VK_DOMAIN:$VK_ADGUARD_PORT/"
        info "Логин:         admin"
        info "Пароль:        $VK_ADGUARD_PASS"
    fi
    echo
    info "Эти данные сохранены в $STATE_FILE (доступны только root)."
    echo
    info "${C_BOLD}Что дальше:${C_OFF}"
    if bot_enabled; then
        info "  1. Откройте бота${BOT_USERNAME:+ @$BOT_USERNAME} и отправьте /start."
        info "  2. Выдайте себе доступ: /admin_add <ваш_ID> <ваш_ник>"
        info "  3. Нажмите «🔑 Подключения» и добавьте ссылку в Happ."
        info "  4. Включите в панели двухфакторную аутентификацию."
    else
        info "  1. Включите в панели двухфакторную аутентификацию."
        info "  2. Откройте раздел клиентов в панели и создайте клиента, привязав его к инбаундам."
        info "  3. Скопируйте его ссылки и добавьте их в Happ."
    fi
    echo
    info "Журнал установки: $LOG_FILE"
}

usage() {
    cat <<'EOF'
Использование: install.sh [--yes] [--help]

  --yes    не задавать вопросов: значения берутся из VK_* и значений по умолчанию
  --help   показать эту справку
EOF
}

main() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            -y | --yes) ASSUME_YES=1 ;;
            -h | --help)
                usage
                exit 0
                ;;
            *) die "Неизвестный параметр: $arg" ;;
        esac
    done

    preflight
    init_log
    trap on_error ERR
    bootstrap_repo "$@"

    printf '%s%sУстановка 3x-ui и Telegram-бота%s\n' "$C_BLUE" "$C_BOLD" "$C_OFF"
    apt_install curl iproute2 ca-certificates python3 bind9-dnsutils
    BOT_USERNAME=""
    if ((RERUN)); then
        load_state
    else
        collect_settings
    fi

    install_dependencies
    setup_site
    issue_certificate
    setup_adguard
    install_xui
    provision_inbounds
    setup_tuning
    setup_firewall
    setup_bot
    ensure_panel_reachable
    verify_install
    print_summary
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
    main "$@"
fi
