"""HTTP-клиент 3x-ui для VPN-бота.

Модуль не содержит секретов: адрес панели, логин и пароль берутся из env
(подключается через EnvironmentFile в vpnbot.service).

Важно про единицы измерения: в API 3x-ui поле totalGB — это БАЙТЫ,
а expiryTime — миллисекунды. Не умножайте totalGB на 1024**3.
"""

import json
import logging
import os
import re
from urllib.parse import parse_qs, quote, urlsplit

import aiohttp
from yarl import URL

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25)
VPN_PROTOCOLS = ('hysteria', 'vless')
# Панель применяет flow только там, где он нужен: на Vision-инбаунде.
VISION_FLOW = 'xtls-rprx-vision'

XUI_BASE_URL = (os.getenv('XUI_BASE_URL') or '').rstrip('/')
XUI_USERNAME = os.getenv('XUI_USERNAME') or ''
XUI_PASSWORD = os.getenv('XUI_PASSWORD') or ''
# Токен не зависит от смены пароля и 2FA: с ними вход по паролю перестаёт работать.
XUI_API_TOKEN = os.getenv('XUI_API_TOKEN') or ''


class XuiError(Exception):
    """Ошибка общения с панелью (без секретов в тексте)."""


def parse_json(value):
    """settings/clientStats приходят то объектом, то строкой — поддерживаем оба."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value or {}


def _require_config():
    if not XUI_BASE_URL or not (XUI_API_TOKEN or (XUI_USERNAME and XUI_PASSWORD)):
        raise XuiError('В env должны быть заданы XUI_BASE_URL и XUI_API_TOKEN либо XUI_USERNAME и XUI_PASSWORD')


async def login(username=None, password=None):
    """Логин в панель. Возвращает {'success', 'cookies', 'csrf'} или {'success': False, 'msg'}."""
    _require_config()
    if XUI_API_TOKEN:
        return {'success': True, 'cookies': {}, 'csrf': ''}
    username = username or XUI_USERNAME
    password = password or XUI_PASSWORD
    jar = aiohttp.CookieJar(unsafe=True)
    try:
        async with aiohttp.ClientSession(cookie_jar=jar, timeout=REQUEST_TIMEOUT) as session:
            async with session.get(XUI_BASE_URL + '/') as page:
                html = await page.text()
            match = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
            if not match:
                return {'success': False, 'msg': 'Не удалось получить CSRF-токен панели'}
            csrf = match.group(1)

            async with session.post(
                XUI_BASE_URL + '/login',
                data={'username': username, 'password': password},
                headers={'X-CSRF-Token': csrf},
            ) as response:
                content_type = response.headers.get('Content-Type', '')
                if response.status != 200 or 'application/json' not in content_type:
                    logging.error('Логин в панель отклонён: HTTP %s', response.status)
                    return {'success': False, 'msg': f'Панель вернула HTTP {response.status}'}
                response_json = await response.json()
                if not response_json.get('success'):
                    logging.error('Логин в панель не удался: %s', response_json.get('msg'))
                    return {'success': False, 'msg': response_json.get('msg', 'Не удалось войти в панель')}

            async with session.get(XUI_BASE_URL + '/csrf-token') as csrf_response:
                if csrf_response.status == 200:
                    try:
                        csrf_json = await csrf_response.json()
                        csrf = csrf_json.get('obj', csrf)
                    except (aiohttp.ContentTypeError, ValueError):
                        pass

            session_cookies = session.cookie_jar.filter_cookies(URL(XUI_BASE_URL + '/'))
            return {
                'success': True,
                'cookies': {cookie.key: cookie.value for cookie in session_cookies.values()},
                'csrf': csrf,
            }
    except aiohttp.ClientError as error:
        logging.error('Логин в панель: сетевая ошибка %s', type(error).__name__)
        return {'success': False, 'msg': 'Панель временно недоступна'}


async def _request(method, path, cookies, csrf, json_data=None):
    _require_config()
    url = XUI_BASE_URL + path
    headers = {'Accept': 'application/json'}
    if XUI_API_TOKEN:
        headers['Authorization'] = f'Bearer {XUI_API_TOKEN}'
    if csrf:
        headers['X-CSRF-Token'] = csrf
    jar = aiohttp.CookieJar(unsafe=True)
    try:
        async with aiohttp.ClientSession(cookies=cookies, cookie_jar=jar, timeout=REQUEST_TIMEOUT) as session:
            async with session.request(method, url, json=json_data, headers=headers) as response:
                text = await response.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logging.error('%s %s: HTTP %s, ответ не JSON', method, path.split('/api/')[-1], response.status)
                    return {'success': False, 'msg': f'HTTP {response.status}: некорректный ответ панели'}
                if not isinstance(data, dict):
                    return {'success': False, 'msg': 'Некорректный ответ панели'}
                return data
    except aiohttp.ClientError as error:
        logging.error('%s %s: сетевая ошибка %s', method, path.split('/api/')[-1], type(error).__name__)
        return {'success': False, 'msg': 'Панель временно недоступна'}


async def get_inbounds(cookies, csrf=None):
    return await _request('GET', '/panel/api/inbounds/list', cookies, csrf)


async def add_client(inbound_ids, client, cookies, csrf=None):
    body = {'client': client, 'inboundIds': [int(inbound_id) for inbound_id in inbound_ids]}
    return await _request('POST', '/panel/api/clients/add', cookies, csrf, body)


async def attach_client(email, inbound_ids, cookies, csrf=None):
    path = f'/panel/api/clients/{quote(str(email), safe="")}/attach'
    body = {'inboundIds': [int(inbound_id) for inbound_id in inbound_ids]}
    return await _request('POST', path, cookies, csrf, body)


async def update_client(client, cookies, csrf=None):
    email = client.get('email', '')
    path = f'/panel/api/clients/update/{quote(str(email), safe="")}'
    # В копии клиента из инбаунда Hysteria нет flow. Без него панель стёрла бы flow и у Vision.
    body = {**client, 'flow': VISION_FLOW}
    return await _request('POST', path, cookies, csrf, body)


async def delete_client(email, cookies, csrf=None):
    path = f'/panel/api/clients/del/{quote(str(email), safe="")}'
    return await _request('POST', path, cookies, csrf)


async def get_client_links(email, cookies, csrf=None):
    path = f'/panel/api/clients/links/{quote(str(email), safe="")}'
    return await _request('GET', path, cookies, csrf)


async def delete_inbound(inbound_id, cookies, csrf=None):
    path = f'/panel/api/inbounds/del/{int(inbound_id)}'
    return await _request('POST', path, cookies, csrf)


# --------------------------------------------------------------------------
# Работа с уже полученным списком инбаундов
# --------------------------------------------------------------------------

def inbound_clients(inbound):
    settings = parse_json(inbound.get('settings'))
    if isinstance(settings, dict):
        return settings.get('clients') or []
    return []


def find_inbound(inbounds, port=None, protocol=None):
    for inbound in inbounds or []:
        try:
            if port is not None and int(inbound.get('port') or 0) != int(port):
                continue
        except (TypeError, ValueError):
            continue
        if protocol is not None and inbound.get('protocol') != protocol:
            continue
        return inbound
    return None


def vpn_inbounds(inbounds):
    return [i for i in inbounds or [] if i.get('protocol') in VPN_PROTOCOLS and i.get('enable')]


def find_client(inbound, email):
    for client in inbound_clients(inbound):
        if client.get('email') == email:
            return client
    return None


def find_client_any(inbounds, email):
    for inbound in inbounds:
        client = find_client(inbound, email)
        if client:
            return client
    return None


def find_client_email_ci(inbounds, email):
    """Ищет email клиента без учёта регистра; возвращает точный email из панели или None."""
    needle = (email or '').casefold()
    for inbound in inbounds:
        for client in inbound_clients(inbound):
            client_email = client.get('email') or ''
            if client_email.casefold() == needle:
                return client_email
    return None


def find_stats(inbound, email):
    for stats in inbound.get('clientStats') or []:
        if stats.get('email') == email:
            return stats
    return None


def traffic_used(inbounds, email):
    """Трафик клиента и последняя активность.

    В 3x-ui 3.x клиент глобальный: у него ОДНА строка client_traffics,
    и clientStats во всех инбаундах показывает одно и то же значение.
    Поэтому берём максимум (одно значение), а не сумму — иначе трафик умножится.
    """
    used = 0
    last_online = 0
    for inbound in inbounds:
        stats = find_stats(inbound, email)
        if not stats:
            continue
        used = max(used, int(stats.get('up') or 0) + int(stats.get('down') or 0))
        last_online = max(last_online, int(stats.get('lastOnline') or stats.get('last_online') or 0))
    return used, last_online


def active_ports(inbounds, email):
    """Порты инбаундов, где клиент реально включён (не удалён панелью)."""
    ports = set()
    for inbound in inbounds:
        client = find_client(inbound, email)
        if not client or not client.get('enable', True):
            continue
        stats = find_stats(inbound, email)
        if stats and not stats.get('enable', True):
            continue
        try:
            ports.add(int(inbound.get('port') or 0))
        except (TypeError, ValueError):
            continue
    return ports


def client_status(client, used, now_ms):
    """(active, reason). totalGB — байты, expiryTime — миллисекунды."""
    if not client:
        return False, 'not_found'
    if not client.get('enable', True):
        return False, 'disabled'
    try:
        expiry = int(client.get('expiryTime') or 0)
    except (TypeError, ValueError):
        expiry = 0
    if expiry and expiry < now_ms:
        return False, 'expired'
    try:
        limit = int(client.get('totalGB') or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0 and used >= limit:
        return False, 'exhausted'
    return True, 'active'


def link_kind(link):
    """Тип подключения по ссылке панели; None — ссылка не для выдачи."""
    scheme, _sep, rest = (link or '').partition('://')
    query = parse_qs(urlsplit(link or '').query)
    if scheme == 'hysteria2' and rest:
        return 'hysteria2-obfs' if 'obfs' in query else 'hysteria2'
    if scheme == 'vless' and rest:
        return 'vless-xhttp' if query.get('type') == ['xhttp'] else 'vless-vision'
    return None
