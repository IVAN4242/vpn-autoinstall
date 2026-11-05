#!/usr/bin/env python3
"""Создаёт инбаунды 3x-ui через HTTP API панели.

Запускается из install.sh, но безопасен и при повторном запуске: инбаунд с уже
существующим remark не трогается, поэтому ссылки клиентов не меняются.

Только стандартная библиотека: скрипт работает до установки зависимостей бота.
Настройки приходят через переменные окружения, чтобы токен панели
не попадал в список процессов. Вход по токену работает и с включённой 2FA.
"""

import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SNIFFING = {
    'enabled': True,
    'destOverride': ['http', 'tls', 'quic'],
    'metadataOnly': False,
    'routeOnly': False,
}
FINGERPRINT = 'chrome'
BBR = {'quicParams': {'congestion': 'bbr'}}
RETRIES = 30
DNS_TAG = 'dns_inbound'
DNS_OUTBOUND = 'dns-local'


class PanelError(Exception):
    """Панель не ответила или отклонила запрос."""


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or value == '':
        raise SystemExit(f'Не задана переменная {name}')
    return value


class Panel:
    def __init__(self, base_url, api_token, insecure=False):
        self.base_url = base_url.rstrip('/')
        self.api_token = api_token
        handlers = []
        if insecure:
            # Только для обращения к панели по адресу, не совпадающему с сертификатом.
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self.opener = urllib.request.build_opener(*handlers)

    def call(self, method, path, body=None, form=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {'Accept': 'application/json', 'Authorization': f'Bearer {self.api_token}'}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=25) as response:
                text = response.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as error:
            raise PanelError(f'{method} {path}: HTTP {error.code}') from error
        try:
            return json.loads(text)
        except ValueError as error:
            raise PanelError(f'{method} {path}: ответ не JSON') from error

    def inbounds(self):
        answer = self.call('GET', '/panel/api/inbounds/list')
        if not answer.get('success'):
            raise PanelError(f'Не удалось получить инбаунды: {answer.get("msg")}')
        return answer.get('obj') or []


def inbound_body(remark, port, protocol, settings, stream, domain, listen=''):
    return {
        'enable': True,
        'remark': remark,
        'listen': listen,
        'port': int(port),
        'protocol': protocol,
        'expiryTime': 0,
        'total': 0,
        'settings': json.dumps(settings),
        'streamSettings': json.dumps(stream),
        'sniffing': json.dumps(SNIFFING),
        # Ссылки всегда с доменом, с какого бы адреса ни пришёл запрос к панели.
        'shareAddrStrategy': 'custom',
        'shareAddr': domain,
    }


def hysteria_tls(domain, certificates):
    return {'serverName': domain, 'alpn': ['h3'], 'minVersion': '1.3', 'certificates': certificates}


def hysteria_inbound(remark, port, domain, certificates, obfs_password=None):
    finalmask = dict(BBR)
    if obfs_password:
        finalmask['udp'] = [{'type': 'salamander', 'settings': {'password': obfs_password}}]
    stream = {
        'network': 'hysteria',
        'hysteriaSettings': {'version': 2, 'udpIdleTimeout': 60},
        'security': 'tls',
        'tlsSettings': hysteria_tls(domain, certificates),
        'finalmask': finalmask,
    }
    return inbound_body(remark, port, 'hysteria', {'version': 2, 'clients': []}, stream, domain)


def vision_inbound(remark, port, domain, certificates, socket_h1, socket_h2):
    # Всё, что не является VLESS, Xray отдаёт в nginx: так и работает SelfSteal.
    # Для h2 нужен отдельный сокет: nginx до 1.25.1 на одном сокете с h2c не отвечает по HTTP/1.1.
    settings = {
        'clients': [],
        'decryption': 'none',
        'fallbacks': [
            {'dest': socket_h1, 'xver': 2},
            {'alpn': 'h2', 'dest': socket_h2, 'xver': 2},
        ],
    }
    stream = {
        'network': 'tcp',
        'security': 'tls',
        'tlsSettings': {
            'serverName': domain,
            'alpn': ['h2', 'http/1.1'],
            'minVersion': '1.2',
            'certificates': certificates,
            'settings': {'fingerprint': FINGERPRINT},
        },
    }
    return inbound_body(remark, port, 'vless', settings, stream, domain)


def xhttp_inbound(remark, port, path, domain):
    # TLS завершает Xray на Vision-инбаунде, сюда запрос приходит от nginx без шифрования.
    stream = {
        'network': 'xhttp',
        'xhttpSettings': {'mode': 'stream-up', 'path': path, 'host': ''},
        'security': 'none',
        'sockopt': {'trustedXForwardedFor': ['X-Real-IP']},
    }
    settings = {'clients': [], 'decryption': 'none'}
    return inbound_body(remark, port, 'vless', settings, stream, domain, listen='127.0.0.1')


def ws_inbound(remark, port, path, domain):
    # Как и у XHTTP: TLS завершает Xray на Vision-инбаунде, сюда запрос приходит от nginx.
    stream = {
        'network': 'ws',
        'wsSettings': {'acceptProxyProtocol': False, 'path': path, 'host': '', 'headers': {}, 'heartbeatPeriod': 0},
        'security': 'none',
        'sockopt': {'trustedXForwardedFor': ['X-Real-IP']},
    }
    settings = {'clients': [], 'decryption': 'none'}
    return inbound_body(remark, port, 'vless', settings, stream, domain, listen='127.0.0.1')


def ensure_public_host(panel, inbound_id, remark, alpn, domain, public_port):
    """Клиент ходит на публичный TLS-порт, а не на внутренний порт инбаунда."""
    answer = panel.call('GET', f'/panel/api/hosts/byInbound/{inbound_id}')
    if answer.get('obj'):
        return
    created = panel.call('POST', '/panel/api/hosts/add', {
        'remark': remark,
        'inboundIds': [inbound_id],
        'hosts': [f'{domain}:{public_port}'],
        'security': 'tls',
        'sni': domain,
        'alpn': alpn,
        'fingerprint': FINGERPRINT,
    })
    if not created.get('success'):
        raise PanelError(f'Не удалось создать публичный адрес {remark}: {created.get("msg")}')


def apply_adguard_dns(template):
    """DNS Xray -> AdGuard. Возвращает True, если шаблон изменился."""
    before = json.dumps(template, sort_keys=True)
    template['dns'] = {
        'tag': DNS_TAG,
        'queryStrategy': 'UseIP',
        'servers': [
            # Если AdGuard не ответит за 1,5 с, запрос уйдёт на резервный сервер и VPN не встанет.
            {'address': '127.0.0.1', 'port': 53, 'queryStrategy': 'UseIP', 'skipFallback': False, 'timeoutMs': 1500},
            {'address': '8.8.8.8', 'port': 53, 'queryStrategy': 'UseIP', 'skipFallback': False, 'finalQuery': True},
        ],
    }

    outbounds = template.setdefault('outbounds', [])
    if not any(outbound.get('tag') == DNS_OUTBOUND for outbound in outbounds):
        outbounds.append({'tag': DNS_OUTBOUND, 'protocol': 'freedom', 'settings': {}})
    # Xray сам резолвит домены: только так запросы пользователей попадают в AdGuard.
    for outbound in outbounds:
        if outbound.get('tag') == 'direct':
            sockopt = outbound.setdefault('streamSettings', {}).setdefault('sockopt', {})
            sockopt.setdefault('domainStrategy', 'ForceIPv4')

    # Заблокированный домен AdGuard отдаёт как 0.0.0.0, а это режет правило geoip:private.
    routing = template.setdefault('routing', {})
    routing['domainStrategy'] = 'IPIfNonMatch'
    rules = routing.setdefault('rules', [])
    if not any(DNS_TAG in (rule.get('inboundTag') or []) for rule in rules):
        # Правило должно идти раньше geoip:private, иначе запросы к 127.0.0.1 будут заблокированы.
        position = next(
            (index + 1 for index, rule in enumerate(rules) if 'api' in (rule.get('inboundTag') or [])), 0,
        )
        rules.insert(position, {'type': 'field', 'inboundTag': [DNS_TAG], 'outboundTag': DNS_OUTBOUND})
    return json.dumps(template, sort_keys=True) != before


def ensure_adguard_dns(panel):
    answer = panel.call('POST', '/panel/api/xray/')
    if not answer.get('success'):
        raise PanelError(f'Не удалось прочитать шаблон Xray: {answer.get("msg")}')
    current = json.loads(answer['obj'])
    template = current['xraySetting']
    if not apply_adguard_dns(template):
        print('  = DNS Xray: уже направлен в AdGuard')
        return
    saved = panel.call('POST', '/panel/api/xray/update', form={
        'xraySetting': json.dumps(template),
        'outboundTestUrl': current.get('outboundTestUrl') or '',
    })
    if not saved.get('success'):
        raise PanelError(f'Не удалось сохранить шаблон Xray: {saved.get("msg")}')
    print('  + DNS Xray: AdGuard, резерв 8.8.8.8')


def wait_ready(panel):
    last_error = None
    for _ in range(RETRIES):
        try:
            panel.inbounds()
            return
        except (PanelError, urllib.error.URLError, ValueError, OSError) as error:
            last_error = error
            time.sleep(2)
    raise PanelError(f'Панель не отвечает: {last_error}')


def main():
    domain = env('VK_DOMAIN')
    certificates = [{'certificateFile': env('VK_CERT_FILE'), 'keyFile': env('VK_KEY_FILE')}]
    panel = Panel(
        env('VK_PANEL_URL'), env('VK_API_TOKEN'),
        insecure=os.environ.get('VK_PANEL_INSECURE') == '1',
    )
    wait_ready(panel)
    existing = {inbound.get('remark'): inbound for inbound in panel.inbounds()}

    wanted = []
    hy2_port = env('VK_HY2_PORT')
    wanted.append(hysteria_inbound(f'hy2-{hy2_port}', hy2_port, domain, certificates))
    obfs_port = os.environ.get('VK_HY2_OBFS_PORT') or ''
    if obfs_port:
        wanted.append(hysteria_inbound(
            f'hy2-{obfs_port}-salamander', obfs_port, domain, certificates, secrets.token_urlsafe(18),
        ))
    public_port = env('VK_TLS_PORT')
    wanted.append(vision_inbound(
        f'vless-vision-{public_port}', public_port, domain, certificates,
        env('VK_SOCKET_H1'), env('VK_SOCKET_H2'),
    ))
    # Инбаунд за nginx -> публичный адрес и ALPN: nginx принимает XHTTP по HTTP/2, а WebSocket по HTTP/1.1.
    public_hosts = {}
    xhttp_path = os.environ.get('VK_XHTTP_PATH') or ''
    if xhttp_path:
        wanted.append(xhttp_inbound('vless-xhttp', env('VK_XHTTP_LOCAL_PORT'), xhttp_path, domain))
        public_hosts['vless-xhttp'] = ('public-tls', ['h2'])
    ws_path = os.environ.get('VK_WS_PATH') or ''
    if ws_path:
        wanted.append(ws_inbound('vless-ws', env('VK_WS_LOCAL_PORT'), ws_path, domain))
        public_hosts['vless-ws'] = ('public-tls-ws', ['http/1.1'])

    for body in wanted:
        if body['remark'] in existing:
            print(f'  = {body["remark"]}: уже есть')
            continue
        answer = panel.call('POST', '/panel/api/inbounds/add', body)
        if not answer.get('success'):
            raise PanelError(f'{body["remark"]}: {answer.get("msg")}')
        print(f'  + {body["remark"]}: порт {body["port"]}')

    if public_hosts:
        created = {inbound.get('remark'): inbound for inbound in panel.inbounds()}
        for remark, (host_remark, alpn) in public_hosts.items():
            ensure_public_host(panel, int(created[remark]['id']), host_remark, alpn, domain, public_port)

    if os.environ.get('VK_ADGUARD_DNS') == '1':
        ensure_adguard_dns(panel)


if __name__ == '__main__':
    try:
        main()
    except PanelError as error:
        print(f'Ошибка настройки инбаундов: {error}', file=sys.stderr)
        sys.exit(1)
