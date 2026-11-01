#!/usr/bin/env bash
#
# Удаляет то, что поставил install.sh. Перед удалением сохраняет список
# пользователей бота и базу панели в /root/vpn-autoinstall-backup-<дата>/.
# Пакеты (nginx, python и др.) и правила UFW остаются.

set -Eeuo pipefail

STATE_FILE="/etc/vpn-autoinstall/state.env"

[[ $EUID -eq 0 ]] || { echo "Нужны права root." >&2; exit 1; }
# Без файла состояния сервер настроен не этим скриптом, и удалять с него нечего.
[[ -f "$STATE_FILE" ]] || { echo "Установка не найдена: нет $STATE_FILE. Ничего не изменено." >&2; exit 1; }

# shellcheck disable=SC1090
. "$STATE_FILE"

echo "Будут удалены: панель 3x-ui со всеми клиентами, AdGuard Home, Telegram-бот, сайт $VK_DOMAIN и его сертификат."
read -r -p "Введите домен $VK_DOMAIN для подтверждения: " answer </dev/tty
[[ "$answer" == "$VK_DOMAIN" ]] || { echo "Отменено."; exit 1; }

backup="/root/vpn-autoinstall-backup-$(date +%Y%m%d-%H%M%S)"
install -d -m 700 "$backup"
cp -a /var/lib/vpnbot/users.json /etc/x-ui/x-ui.db "$STATE_FILE" /etc/vpnbot.env \
    /opt/AdGuardHome/AdGuardHome.yaml "$backup/" 2>/dev/null || true

systemctl disable --now vpnbot x-ui AdGuardHome 2>/dev/null || true
rm -f /etc/systemd/system/vpnbot.service /etc/systemd/system/x-ui.service /etc/systemd/system/AdGuardHome.service
rm -rf /opt/AdGuardHome
rm -rf /opt/vpnbot /var/lib/vpnbot /etc/vpnbot.env /usr/local/x-ui /etc/x-ui /usr/bin/x-ui /var/log/x-ui
userdel vpnbot 2>/dev/null || true

if [[ -x /root/.acme.sh/acme.sh ]]; then
    /root/.acme.sh/acme.sh --remove --ecc -d "$VK_DOMAIN" >/dev/null 2>&1 || true
    rm -rf "/root/.acme.sh/${VK_DOMAIN}_ecc"
fi
rm -rf "/root/cert/$VK_DOMAIN"

rm -f /etc/nginx/sites-enabled/selfsteal /etc/nginx/sites-available/selfsteal \
    /etc/nginx/snippets/selfsteal-site.conf /etc/systemd/system/nginx.service.d/selfsteal.conf \
    /etc/nginx/sites-enabled/adguard-panel /etc/nginx/sites-available/adguard-panel \
    /etc/nginx/snippets/adguard-proxy.conf
rm -rf "/var/www/$VK_DOMAIN" /var/www/acme
systemctl daemon-reload
systemctl stop nginx 2>/dev/null || true

rm -f /etc/sysctl.d/90-vpn-tuning.conf
sed -i "/^127\.0\.0\.1 ${VK_DOMAIN//./\\.}$/d" /etc/hosts
rm -rf /etc/vpn-autoinstall /opt/vpn-autoinstall /var/log/vpn-autoinstall.log

echo "Готово. Резервная копия: $backup"
echo "В ней есть пароли и токен бота: удалите её, когда она станет не нужна."
echo "Правила UFW остались: посмотреть их можно командой ufw status numbered."
