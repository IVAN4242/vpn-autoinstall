"""Telegram-бот для выдачи VPN-подключений из 3x-ui.

Выдаёт ссылки Hysteria2 и VLESS (SelfSteal). Доступ выдаётся только
пользователям из users.json; бот никогда не заводит «нового пользователя»
сам. Администратор определяется по числовому Telegram ID из ADMIN_IDS,
а не по нику.
"""

import asyncio
import html
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

try:
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo('Europe/Moscow')
except Exception:  # на случай отсутствия tzdata
    MSK = timezone(timedelta(hours=3))

import store
import xui
from store import StoreError

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    raise RuntimeError('BOT_TOKEN environment variable is not set')

ADMIN_IDS = {
    int(part)
    for part in re.split(r'[,\s;]+', os.getenv('ADMIN_IDS') or '')
    if part.isdigit()
}
try:
    NEW_USER_DAYS = int(os.getenv('NEW_USER_DAYS') or '365')
except ValueError:
    NEW_USER_DAYS = 365
if NEW_USER_DAYS <= 0:
    NEW_USER_DAYS = 365

USER_STORE = store.UserStore()
INSTRUCTIONS_FILE = Path(os.getenv('INSTRUCTIONS_FILE') or (Path(__file__).resolve().parent / 'instructions.md'))

LINK_LABELS = {
    'hysteria2': 'Hysteria2 (UDP) — основной',
    'hysteria2-obfs': 'Hysteria2 с обфускацией salamander (UDP)',
    'vless-vision': 'VLESS Vision (TCP) — если UDP не работает',
    'vless-xhttp': 'VLESS XHTTP (TCP)',
}
LINK_ORDER = list(LINK_LABELS)

INACTIVE_TEXT = {
    'disabled': '⛔ Доступ к VPN отключён.',
    'disabled_by_admin': '⛔ Доступ к VPN отключён.',
    'expired': '⛔ Срок доступа к VPN истёк.',
    'exhausted': '⛔ Лимит трафика исчерпан.',
    'not_found': '⚠️ Не удалось найти подключение. Попробуйте позже.',
    'no_links': '⚠️ Не удалось подготовить ссылки. Попробуйте позже.',
}

STATUS_TEXT = {
    'active': '✅ включён',
    'disabled': '⛔ отключён',
    'expired': '⛔ срок истёк',
    'exhausted': '⛔ лимит исчерпан',
    'not_found': '⚠️ клиент не найден',
}

HELP_TEXT = (
    'Что я умею:\n'
    '• «🔑 Подключения» — получить ссылки для VPN\n'
    '• «📊 Статистика» — проверить доступ, трафик и срок\n'
    '• «📖 Инструкция» — как подключиться и что делать, если не работает\n'
    '• «🆔 Мой ID» — узнать свой Telegram ID\n'
    '• «❓ Помощь» — эта справка'
)

ADMIN_HELP_TEXT = (
    '<b>Админ-команды</b>\n'
    'Кнопка «⚙️ Админ» открывает эту справку.\n\n'
    '/admin_list [стр] — все пользователи\n'
    '/admin_stats &lt;ник|ID&gt; — подробная статистика\n'
    '/admin_add &lt;ник&gt; [дней] [ГБ] — добавить (по умолчанию 365 дней, без лимита)\n'
    '/admin_add &lt;ID&gt; &lt;ник&gt; [дней] [ГБ] — добавить, сразу привязав Telegram ID\n'
    '/admin_del &lt;ник&gt; — убрать из списка и отключить доступ\n'
    '/admin_limit &lt;ник&gt; &lt;ГБ|0&gt; — лимит трафика (0 = без лимита)\n'
    '/admin_date &lt;ник&gt; &lt;ДД.ММ.ГГГГ|ГГГГ-ММ-ДД|+N|off&gt; — срок\n'
    '/admin_enable &lt;ник&gt; / /admin_disable &lt;ник&gt; — включить/отключить\n'
    '/admin_set_id &lt;ник&gt; &lt;ID&gt; — привязать Telegram ID\n'
    '/admin_reset_id &lt;ник&gt; — сбросить привязку ID\n'
    '/admin_repair &lt;ник&gt; — создать/привязать клиента\n'
    '/admin_whoami — мой ID\n\n'
    'Новые пользователи получают 365 дней и без лимита трафика. '
    'Доступ по нику в списке привязывается к Telegram ID при первом обращении.\n\n'
    'Подробная инструкция — ADMIN.md в каталоге бота.'
)

bot = Bot(token=TOKEN)
dp = Dispatcher()


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def now_ms():
    return int(time.time() * 1000)


def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def fmt_bytes(value):
    value = int(value or 0)
    if value <= 0:
        return '0 Б'
    for suffix, size in (('ТБ', 1024 ** 4), ('ГБ', 1024 ** 3), ('МБ', 1024 ** 2), ('КБ', 1024)):
        if value >= size:
            return f'{value / size:.2f} {suffix}'
    return f'{value} Б'


def fmt_date(ms):
    if not ms:
        return 'бессрочно'
    return datetime.fromtimestamp(int(ms) / 1000, MSK).strftime('%d.%m.%Y')


def fmt_datetime(ms):
    if not ms:
        return 'нет данных'
    return datetime.fromtimestamp(int(ms) / 1000, MSK).strftime('%d.%m.%Y %H:%M') + ' МСК'


def days_left(ms):
    if not ms:
        return None
    return max(0, int((int(ms) - now_ms()) // 86400000))


def plural_days(number):
    number = abs(int(number))
    if number % 10 == 1 and number % 100 != 11:
        return f'{number} день'
    if number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        return f'{number} дня'
    return f'{number} дней'


def is_admin(user):
    return bool(user) and user.id in ADMIN_IDS


def md_escape(text):
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', str(text))


def md_code(text):
    return str(text).replace('\\', '\\\\').replace('`', '\\`')


def _norm(value):
    return (value or '').strip().casefold()


def inactive_text(reason):
    return INACTIVE_TEXT.get(reason, '⚠️ Доступ временно недоступен.')


def status_line(record, client, used):
    if not record.get('enabled', True):
        return '⛔ отключён'
    if not client:
        return STATUS_TEXT['not_found']
    _active, reason = xui.client_status(client, used, now_ms())
    return STATUS_TEXT.get(reason, '⚠️ неизвестно')


def stats_text(record, client, used, last_online, admin=False):
    username = record.get('username') or record.get('email') or '—'
    lines = []
    if admin:
        lines.append(f'📊 <b>Статистика: @{html.escape(username)}</b>')
        lines.append(f'ID: <code>{int(record.get("tg_id") or 0)}</code>')
        lines.append(f'Email: <code>{html.escape(record.get("email") or "")}</code>')
        lines.append(f'В списке: {"✅ да" if record.get("enabled", True) else "⛔ нет"}')
    else:
        lines.append('📊 <b>Ваша статистика</b>')
        lines.append(f'Пользователь: @{html.escape(username)}')
    lines.append(f'Статус: {status_line(record, client, used)}')
    if client:
        expiry = int(client.get('expiryTime') or 0)
        limit = int(client.get('totalGB') or 0)
        if expiry:
            left = days_left(expiry)
            lines.append(f'Срок: до {fmt_date(expiry)} (осталось {plural_days(left)})')
        else:
            lines.append('Срок: бессрочно')
        if limit:
            lines.append(f'Лимит: {fmt_bytes(limit)}')
        else:
            lines.append('Лимит: без ограничения')
        lines.append(f'Использовано: {fmt_bytes(used)}')
        if limit:
            lines.append(f'Осталось: {fmt_bytes(max(0, limit - used))}')
        lines.append(f'Последняя активность: {fmt_datetime(last_online)}')
    else:
        lines.append('Подключение ещё не создано — нажмите «🔑 Подключения».')
    return '\n'.join(lines)


def admin_list_line(record, vpn):
    email = record.get('email') or ''
    client = xui.find_client_any(vpn, email)
    used, _last = xui.traffic_used(vpn, email)
    username = record.get('username') or email
    if not record.get('enabled', True):
        status = '⛔ отключён'
    elif not client:
        status = '⚠️ нет клиента'
    else:
        _active, reason = xui.client_status(client, used, now_ms())
        status = {'active': '✅', 'disabled': '⛔ отключён', 'expired': '⛔ срок', 'exhausted': '⛔ лимит'}.get(reason, '⚠️')
    parts = [f'@{html.escape(username)} — {status}']
    if client:
        limit = int(client.get('totalGB') or 0)
        used_text = fmt_bytes(used) + '/' + (fmt_bytes(limit) if limit else '∞')
        expiry = int(client.get('expiryTime') or 0)
        parts.append(f'{used_text}, до {fmt_date(expiry) if expiry else "∞"}')
    return ' • '.join(parts)


def format_kit(record, links):
    username = record.get('username') or record.get('email') or ''
    lines = [f'Подключения для {md_escape(username)}:', '']
    for index, (label, link) in enumerate(links, 1):
        lines.append(f'{index}\\) {md_escape(label)}')
        lines.append('```')
        lines.append(md_code(link))
        lines.append('```')
        lines.append('')
    lines.append(md_escape('Пробуйте по порядку: сначала №1, если не работает — следующую.'))
    lines.append(md_escape('Если не работает ни одна — проверьте интернет и приложение, затем попробуйте позже.'))
    lines.append(md_escape('Что делать подробнее — кнопка «📖 Инструкция».'))
    return '\n'.join(lines)


def split_chunks(text, limit=3800):
    chunks = []
    current = ''
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current.rstrip('\n'))
            current = ''
        if len(line) > limit:
            for word in line.split(' '):
                piece = word + ' '
                if len(current) + len(piece) > limit and current:
                    chunks.append(current.rstrip('\n'))
                    current = ''
                current += piece
        else:
            current += line
    if current.strip():
        chunks.append(current.rstrip('\n'))
    return chunks or [text]


async def send_text(message, text, parse_mode=None):
    if len(text) <= 4000:
        await message.answer(text, parse_mode=parse_mode)
        return
    for chunk in split_chunks(text):
        await message.answer(chunk, parse_mode=parse_mode)


def _inline_md(text):
    escaped = html.escape(text, quote=False)
    escaped = re.sub(
        r'\[([^\]]+)\]\(([^)\s]+)\)',
        lambda match: f'<a href="{html.escape(match.group(2), quote=True)}">{match.group(1)}</a>',
        escaped,
    )
    escaped = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', escaped)
    escaped = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', escaped)
    escaped = re.sub(r'`([^`]+)`', r'<code>\1</code>', escaped)
    return escaped


def render_instruction(text):
    out = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            out.append('')
            continue
        heading = re.match(r'^#{1,4}\s+(.*)$', line)
        if heading:
            out.append(f'<b>{_inline_md(heading.group(1))}</b>')
            continue
        bullet = re.match(r'^[-*]\s+(.*)$', line)
        if bullet:
            out.append('• ' + _inline_md(bullet.group(1)))
            continue
        numbered = re.match(r'^(\d+)\.\s+(.*)$', line)
        if numbered:
            out.append(f'{numbered.group(1)}. ' + _inline_md(numbered.group(2)))
            continue
        out.append(_inline_md(line))
    return '\n'.join(out)


def main_keyboard(user=None):
    """Постоянное кнопочное меню. resize_keyboard=True — компактная клавиатура."""
    keyboard = [
        [KeyboardButton(text='🔑 Подключения'), KeyboardButton(text='📊 Статистика')],
        [KeyboardButton(text='📖 Инструкция'), KeyboardButton(text='🆔 Мой ID')],
        [KeyboardButton(text='❓ Помощь')],
    ]
    if is_admin(user):
        keyboard.append([KeyboardButton(text='⚙️ Админ')])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        # is_persistent=False: клавиатуру можно скрыть и снова открыть
        # стандартной кнопкой-иконкой в поле ввода.
        is_persistent=False,
        input_field_placeholder='Выберите действие',
    )


async def ensure_private(message):
    if message.chat.type != 'private':
        await message.answer('Напишите боту в личные сообщения.')
        return False
    return True


async def notify_admin(text):
    for admin_id in sorted(ADMIN_IDS):
        try:
            await bot.send_message(admin_id, text, parse_mode='HTML')
        except Exception as error:  # noqa: BLE001 - уведомление не должно ронять обработчик
            logging.warning('Не удалось уведомить админа %s: %s', admin_id, type(error).__name__)


def resolve_user(user):
    record = USER_STORE.find(user.id, user.username)
    if not record:
        return None, False
    changed = False
    claimed = False
    if not int(record.get('tg_id') or 0) and user.id:
        record['tg_id'] = int(user.id)
        claimed = True
        changed = True
    if user.username and record.get('username') != user.username:
        record['username'] = user.username
        changed = True
    if changed:
        USER_STORE.update(record)
    return record, claimed


async def send_denial(message, user):
    text = '🔒 <b>У вас пока нет доступа к VPN.</b>\n\n'
    if user.username:
        text += f'Ваш ник: @{html.escape(user.username)}\n'
    text += f'Ваш Telegram ID: <code>{user.id}</code>'
    await message.answer(text, parse_mode='HTML')


# ---------------------------------------------------------------------------
# 3x-ui
# ---------------------------------------------------------------------------

class PanelError(Exception):
    pass


async def panel_login():
    try:
        result = await xui.login()
    except xui.XuiError as error:
        raise PanelError(str(error)) from error
    if not result.get('success'):
        raise PanelError(result.get('msg') or 'Не удалось войти в панель')
    return result


async def panel_inbounds(session):
    try:
        result = await xui.get_inbounds(session['cookies'], session['csrf'])
    except xui.XuiError as error:
        raise PanelError(str(error)) from error
    if not result.get('success'):
        raise PanelError(result.get('msg') or 'Не удалось получить инбаунды')
    return result.get('obj') or []


async def ensure_client(record, session, inbounds, days=None, limit_bytes=None, enable=None):
    """Гарантирует, что у записи есть клиент, привязанный ко всем VPN-инбаундам."""
    email = record.get('email') or ''
    if not email:
        return {'success': False, 'msg': 'У записи нет email клиента.'}
    vpn = xui.vpn_inbounds(inbounds)
    if not vpn:
        return {'success': False, 'msg': 'VPN-инбаунды не найдены.'}
    inbound_ids = [int(inbound['id']) for inbound in vpn]

    client = xui.find_client_any(vpn, email)
    if not client:
        # Клиент мог остаться от старых инбаундов — сначала пробуем привязать.
        attached = await xui.attach_client(email, inbound_ids, session['cookies'], session['csrf'])
        if attached.get('success'):
            inbounds = await panel_inbounds(session)
            vpn = xui.vpn_inbounds(inbounds)
            client = xui.find_client_any(vpn, email)

    if not client:
        expiry_ms = now_ms() + int(days if days is not None else NEW_USER_DAYS) * 86400000
        new_client = {
            'id': str(uuid.uuid4()),
            'email': email,
            'flow': xui.VISION_FLOW,
            'limitIp': 0,
            'totalGB': int(limit_bytes or 0),
            'expiryTime': expiry_ms,
            'enable': bool(enable) if enable is not None else True,
            'tgId': int(record.get('tg_id') or 0),
            'subId': uuid.uuid4().hex[:16],
            'auth': uuid.uuid4().hex,
        }
        added = await xui.add_client(inbound_ids, new_client, session['cookies'], session['csrf'])
        if not added.get('success'):
            logging.warning('Не удалось добавить клиента %s: %s', email, added.get('msg'))
            return {'success': False, 'msg': added.get('msg') or 'Не удалось добавить клиента.'}
        inbounds = await panel_inbounds(session)
        vpn = xui.vpn_inbounds(inbounds)
        client = xui.find_client_any(vpn, email)
        if not client:
            return {'success': False, 'msg': 'Панель не вернула созданного клиента.'}

    missing = [int(inbound['id']) for inbound in vpn if not xui.find_client(inbound, email)]
    if missing:
        attached = await xui.attach_client(email, missing, session['cookies'], session['csrf'])
        if attached.get('success'):
            inbounds = await panel_inbounds(session)
            vpn = xui.vpn_inbounds(inbounds)
            client = xui.find_client_any(vpn, email) or client
        else:
            logging.warning('Не удалось привязать %s к %s: %s', email, missing, attached.get('msg'))

    desired = dict(client)
    changed = False
    tg_id = int(record.get('tg_id') or 0)
    if tg_id and int(desired.get('tgId') or 0) != tg_id:
        desired['tgId'] = tg_id
        changed = True
    if enable is not None and bool(desired.get('enable', True)) != bool(enable):
        desired['enable'] = bool(enable)
        changed = True
    if changed:
        updated = await xui.update_client(desired, session['cookies'], session['csrf'])
        if updated.get('success'):
            client = desired
        else:
            logging.warning('Не удалось обновить клиента %s: %s', email, updated.get('msg'))

    return {'success': True, 'client': client, 'inbounds': inbounds}


async def build_kit(record):
    if not record.get('enabled', True):
        return {'success': True, 'status': 'inactive', 'reason': 'disabled_by_admin'}
    email = record.get('email') or ''
    try:
        session = await panel_login()
        inbounds = await panel_inbounds(session)
    except PanelError as error:
        return {'success': False, 'reason': 'panel', 'msg': str(error) or 'Панель временно недоступна.'}
    if not xui.vpn_inbounds(inbounds):
        return {'success': False, 'reason': 'inbounds', 'msg': 'Подключение временно недоступно. Попробуйте позже.'}

    try:
        ensured = await ensure_client(record, session, inbounds)
    except PanelError as error:
        return {'success': False, 'reason': 'panel', 'msg': str(error)}
    if not ensured.get('success'):
        return {'success': False, 'reason': 'inbounds', 'msg': ensured.get('msg') or 'Не удалось подготовить подключение.'}
    inbounds = ensured['inbounds']
    vpn = xui.vpn_inbounds(inbounds)
    client = ensured.get('client') or xui.find_client_any(vpn, email)

    used, last_online = xui.traffic_used(vpn, email)
    active, reason = xui.client_status(client, used, now_ms())
    if not active:
        return {'success': True, 'status': 'inactive', 'reason': reason, 'used': used}

    if not xui.active_ports(vpn, email):
        return {'success': True, 'status': 'inactive', 'reason': 'not_found'}

    try:
        links_resp = await xui.get_client_links(email, session['cookies'], session['csrf'])
    except Exception as error:  # noqa: BLE001
        logging.error('clients/links: %s', type(error).__name__)
        return {'success': False, 'reason': 'panel', 'msg': 'Панель временно недоступна.'}
    if not links_resp.get('success'):
        return {'success': False, 'reason': 'panel', 'msg': links_resp.get('msg') or 'Не удалось получить ссылки.'}

    links = []
    for link in links_resp.get('obj') or []:
        kind = xui.link_kind(link)
        if kind in LINK_LABELS:
            links.append((LINK_ORDER.index(kind), LINK_LABELS[kind], link))
    links.sort(key=lambda item: item[0])
    if not links:
        return {'success': True, 'status': 'inactive', 'reason': 'no_links'}
    return {
        'success': True,
        'status': 'active',
        'links': [(label, link) for _order, label, link in links],
        'used': used,
        'last_online': last_online,
    }


async def send_kit(message):
    user = message.from_user
    try:
        record, claimed = resolve_user(user)
    except StoreError as error:
        logging.error('users.json: %s', error)
        await message.answer('⚠️ Список пользователей временно недоступен. Попробуйте позже.')
        return None
    if claimed:
        await notify_admin(
            f'🔔 Пользователь @{html.escape(user.username or str(user.id))} '
            f'(ID <code>{user.id}</code>) привязан к записи '
            f'<code>{html.escape(record.get("email") or "")}</code>.'
        )
    if not record:
        await send_denial(message, user)
        return None
    try:
        kit = await build_kit(record)
    except Exception as error:  # noqa: BLE001
        logging.exception('build_kit failed: %s', type(error).__name__)
        await message.answer('Не удалось получить подключения. Попробуйте позже.')
        return None

    reason = kit.get('reason')
    if kit.get('success') and kit.get('status') == 'active':
        await send_text(message, format_kit(record, kit['links']), parse_mode='MarkdownV2')
    elif reason in INACTIVE_TEXT:
        await message.answer(inactive_text(reason), parse_mode='HTML')
    else:
        await message.answer(kit.get('msg') or 'Не удалось получить подключения. Попробуйте позже.')
    return kit


# ---------------------------------------------------------------------------
# Пользовательские команды
# ---------------------------------------------------------------------------

@dp.message(Command('start'))
async def start_command(message: types.Message):
    if not await ensure_private(message):
        return
    await message.answer(
        'Привет! Я бот для VPN.\n\n'
        'Пользуйтесь кнопками ниже — меню всегда под рукой.',
        reply_markup=main_keyboard(message.from_user),
    )
    await send_kit(message)


@dp.message(F.text == '🔑 Подключения')
@dp.message(Command('links'))
async def links_command(message: types.Message):
    if not await ensure_private(message):
        return
    await send_kit(message)


@dp.message(F.text == '📊 Статистика')
@dp.message(Command('stats'))
async def stats_command(message: types.Message):
    if not await ensure_private(message):
        return
    user = message.from_user
    try:
        record, claimed = resolve_user(user)
    except StoreError as error:
        logging.error('users.json: %s', error)
        await message.answer('⚠️ Список пользователей временно недоступен. Попробуйте позже.')
        return
    if claimed:
        await notify_admin(
            f'🔔 Пользователь @{html.escape(user.username or str(user.id))} '
            f'(ID <code>{user.id}</code>) привязан к записи '
            f'<code>{html.escape(record.get("email") or "")}</code>.'
        )
    if not record:
        await send_denial(message, user)
        return
    if not record.get('enabled', True):
        await message.answer(inactive_text('disabled_by_admin'), parse_mode='HTML')
        return
    try:
        session = await panel_login()
        inbounds = await panel_inbounds(session)
    except PanelError as error:
        await message.answer(str(error) or 'Панель временно недоступна. Попробуйте позже.')
        return
    vpn = xui.vpn_inbounds(inbounds)
    email = record.get('email') or ''
    client = xui.find_client_any(vpn, email)
    used, last_online = xui.traffic_used(vpn, email)
    await send_text(message, stats_text(record, client, used, last_online), parse_mode='HTML')


@dp.message(F.text == '📖 Инструкция')
@dp.message(Command('info'))
async def info_command(message: types.Message):
    if not await ensure_private(message):
        return
    try:
        source = INSTRUCTIONS_FILE.read_text(encoding='utf-8')
    except OSError:
        logging.error('Не найден файл инструкции: %s', INSTRUCTIONS_FILE)
        await message.answer('Инструкция временно недоступна. Попробуйте позже.')
        return
    try:
        await send_text(message, render_instruction(source), parse_mode='HTML')
    except Exception as error:  # noqa: BLE001
        logging.exception('Не удалось отправить инструкцию: %s', type(error).__name__)
        plain = re.sub(r'\*\*|`|^#{1,4}\s*', '', source, flags=re.M)
        try:
            await send_text(message, plain)
        except Exception:  # noqa: BLE001
            await message.answer('Не удалось показать инструкцию. Попробуйте позже.')


@dp.message(F.text == '🆔 Мой ID')
@dp.message(Command('id'))
async def id_command(message: types.Message):
    if not await ensure_private(message):
        return
    user = message.from_user
    text = f'Ваш Telegram ID: <code>{user.id}</code>'
    if user.username:
        text += f'\nВаш ник: @{html.escape(user.username)}'
    await message.answer(text, parse_mode='HTML')


@dp.message(F.text == '❓ Помощь')
@dp.message(Command('help'))
async def help_command(message: types.Message):
    if not await ensure_private(message):
        return
    text = HELP_TEXT
    if is_admin(message.from_user):
        text += '\n• «⚙️ Админ» — управление доступами'
    text += '\n\nКоманды /links, /stats, /info, /id тоже работают.'
    await message.answer(text, reply_markup=main_keyboard(message.from_user))


# ---------------------------------------------------------------------------
# Админ-команды (только ADMIN_IDS)
# ---------------------------------------------------------------------------

def _args(message):
    return (message.text or '').split()[1:]


@dp.message(F.text == '⚙️ Админ')
@dp.message(Command('admin'))
@dp.message(Command('admin_help'))
async def admin_help_command(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    await message.answer(
        ADMIN_HELP_TEXT,
        parse_mode='HTML',
        reply_markup=main_keyboard(message.from_user),
    )


@dp.message(Command('admin_whoami'))
async def admin_whoami(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    admins = ', '.join(f'<code>{admin_id}</code>' for admin_id in sorted(ADMIN_IDS))
    await message.answer(f'Ваш Telegram ID: <code>{message.from_user.id}</code>\nАдмины: {admins}', parse_mode='HTML')


@dp.message(Command('admin_list'))
async def admin_list(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    args = _args(message)
    page = 1
    if args:
        try:
            page = max(1, int(args[0]))
        except ValueError:
            page = 1
    try:
        records = USER_STORE.all()
    except StoreError as error:
        await message.answer(f'⚠️ Список пользователей повреждён: {html.escape(str(error))}')
        return
    try:
        session = await panel_login()
        inbounds = await panel_inbounds(session)
    except PanelError as error:
        await message.answer(str(error))
        return
    vpn = xui.vpn_inbounds(inbounds)
    records = sorted(records, key=lambda record: _norm(record.get('username') or record.get('email')))
    page_size = 20
    total_pages = max(1, (len(records) + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    lines = [f'👥 <b>Пользователи: {len(records)}</b> (стр. {page}/{total_pages})', '']
    for record in records[start:start + page_size]:
        lines.append('• ' + admin_list_line(record, vpn))
    lines.append('')
    lines.append('Подробнее: /admin_stats &lt;ник&gt;')
    if total_pages > 1:
        lines.append(f'Следующая страница: /admin_list {min(page + 1, total_pages)}')
    await send_text(message, '\n'.join(lines), parse_mode='HTML')


@dp.message(Command('admin_stats'))
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    args = _args(message)
    if not args:
        await message.answer('Использование: /admin_stats <ник|ID>')
        return
    try:
        record = USER_STORE.find_by_identifier(args[0])
    except StoreError as error:
        await message.answer(f'⚠️ Список пользователей повреждён: {html.escape(str(error))}')
        return
    if not record:
        await message.answer('Пользователь не найден в списке.')
        return
    try:
        session = await panel_login()
        inbounds = await panel_inbounds(session)
    except PanelError as error:
        await message.answer(str(error))
        return
    vpn = xui.vpn_inbounds(inbounds)
    email = record.get('email') or ''
    client = xui.find_client_any(vpn, email)
    used, last_online = xui.traffic_used(vpn, email)
    await send_text(
        message,
        stats_text(record, client, used, last_online, admin=True),
        parse_mode='HTML',
    )


@dp.message(Command('admin_add'))
async def admin_add(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    args = _args(message)
    if not args:
        await message.answer('Использование: /admin_add <ник> [дней] [ГБ] или /admin_add <ID> <ник> [дней] [ГБ]')
        return

    tg_id = 0
    if args[0].isdigit():
        tg_id = int(args[0])
        if len(args) >= 2 and not args[1].isdigit():
            username = args[1]
            rest = args[2:]
        else:
            username = f'id{tg_id}'
            rest = args[1:]
    else:
        username = args[0]
        rest = args[1:]
    username = username.lstrip('@')

    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username):
        await message.answer('Неверный ник: нужен Telegram-ник (латиница, цифры, _, 5–32 символа, начинается с буквы). Для добавления по ID: /admin_add <ID> <ник>')
        return

    days = NEW_USER_DAYS
    limit_bytes = 0
    if len(rest) >= 1:
        try:
            days = int(rest[0])
        except ValueError:
            await message.answer('Не понимаю число дней. Пример: /admin_add nick 365 100')
            return
        if days < 0 or days > 3650:
            await message.answer('Дней должно быть от 0 до 3650.')
            return
    if len(rest) >= 2:
        try:
            gb = float(rest[1].replace(',', '.'))
        except ValueError:
            await message.answer('Не понимаю лимит в ГБ. Пример: /admin_add nick 365 100')
            return
        if gb < 0:
            await message.answer('Лимит не может быть отрицательным.')
            return
        limit_bytes = int(gb * 1024 ** 3)

    try:
        other = USER_STORE.find_by_tg_id(tg_id) if tg_id else None
        if other and _norm(other.get('username')) != _norm(username) and _norm(other.get('email')) != _norm(username):
            await message.answer(
                f'ID {tg_id} уже привязан к записи <code>{html.escape(other.get("email") or "")}</code>. '
                f'Используйте её ник или /admin_set_id.',
                parse_mode='HTML',
            )
            return
        existing = USER_STORE.find_by_identifier(username)
    except StoreError as error:
        await message.answer(f'⚠️ Список пользователей повреждён: {html.escape(str(error))}')
        return

    session = None
    inbounds = None
    panel_error = ''
    try:
        session = await panel_login()
        inbounds = await panel_inbounds(session)
    except PanelError as error:
        panel_error = str(error)

    if existing:
        existing['enabled'] = True
        if tg_id and not int(existing.get('tg_id') or 0):
            existing['tg_id'] = tg_id
            existing['username'] = username
        try:
            USER_STORE.update(existing)
        except StoreError as error:
            await message.answer(f'⚠️ Не удалось сохранить список: {html.escape(str(error))}')
            return
        if session is None:
            await message.answer('Пользователь уже в списке, но панель недоступна. Попробуйте позже.')
            return
        ensured = await ensure_client(existing, session, inbounds, enable=True)
        if not ensured.get('success'):
            await message.answer(f'Пользователь в списке, но не удалось подготовить клиента: {html.escape(str(ensured.get("msg")))}')
            return
        await message.answer(
            f'Пользователь @{html.escape(username)} уже в списке; доступ включён. '
            f'Подробности: /admin_stats {html.escape(username)}',
            parse_mode='HTML',
        )
        return

    email = username
    if inbounds:
        exact = xui.find_client_email_ci(inbounds, username)
        if exact:
            email = exact
    record = {
        'tg_id': tg_id,
        'username': username,
        'email': email,
        'enabled': True,
        'added_at': now_iso(),
        'added_by': f'admin:{message.from_user.id}',
        'carry_used_bytes': 0,
        'note': '',
    }
    try:
        USER_STORE.upsert(record)
    except StoreError as error:
        await message.answer(f'⚠️ Не удалось сохранить список: {html.escape(str(error))}')
        return
    if session is None:
        await message.answer(f'Пользователь добавлен в список, но панель недоступна ({html.escape(panel_error)}). Клиент будет создан при первом /links.')
        return
    ensured = await ensure_client(record, session, inbounds, days=days, limit_bytes=limit_bytes, enable=True)
    if not ensured.get('success'):
        await message.answer(f'Пользователь добавлен в список, но клиента создать не удалось: {html.escape(str(ensured.get("msg")))}. Повторите /admin_repair {html.escape(username)}')
        return
    client = ensured.get('client') or {}
    expiry = int(client.get('expiryTime') or 0)
    await message.answer(
        f'✅ Пользователь @{html.escape(username)} добавлен.\n'
        f'Email клиента: <code>{html.escape(email)}</code>\n'
        f'Срок: {fmt_date(expiry) if expiry else "бессрочно"}\n'
        f'Лимит: {fmt_bytes(limit_bytes) if limit_bytes else "без ограничения"}\n\n'
        f'Доступ активируется после первого /start этого пользователя.',
        parse_mode='HTML',
    )


@dp.message(Command('admin_del'))
async def admin_del(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    args = _args(message)
    if not args:
        await message.answer('Использование: /admin_del <ник>')
        return
    try:
        record = USER_STORE.find_by_identifier(args[0])
    except StoreError as error:
        await message.answer(f'⚠️ Список пользователей повреждён: {html.escape(str(error))}')
        return
    if not record:
        await message.answer('Пользователь не найден в списке.')
        return
    try:
        USER_STORE.remove(record.get('email'))
    except StoreError as error:
        await message.answer(f'⚠️ Не удалось сохранить список: {html.escape(str(error))}')
        return
    disabled = False
    panel_failed = False
    try:
        session = await panel_login()
        inbounds = await panel_inbounds(session)
        vpn = xui.vpn_inbounds(inbounds)
        client = xui.find_client_any(vpn, record.get('email') or '')
        if client:
            updated = dict(client)
            updated['enable'] = False
            result = await xui.update_client(updated, session['cookies'], session['csrf'])
            disabled = bool(result.get('success'))
        else:
            disabled = True
    except PanelError:
        panel_failed = True
    text = f'🗑 Пользователь @{html.escape(record.get("username") or record.get("email") or "")} удалён из списка.'
    if disabled:
        text += '\nДоступ отключён.'
    elif panel_failed:
        text += '\n⚠️ Панель недоступна: запись удалена, но клиента отключить не удалось. Повторите позже.'
    else:
        text += '\n⚠️ Не удалось отключить клиента в панели — проверьте вручную.'
    await message.answer(text, parse_mode='HTML')


@dp.message(Command('admin_limit'))
async def admin_limit(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    args = _args(message)
    if len(args) < 2:
        await message.answer('Использование: /admin_limit <ник> <ГБ|0>')
        return
    try:
        gb = float(args[1].replace(',', '.'))
        if gb < 0:
            raise ValueError
    except ValueError:
        await message.answer('Лимит должен быть числом ГБ (0 = без лимита).')
        return
    limit_bytes = int(gb * 1024 ** 3)
    try:
        record = USER_STORE.find_by_identifier(args[0])
    except StoreError as error:
        await message.answer(f'⚠️ Список пользователей повреждён: {html.escape(str(error))}')
        return
    if not record:
        await message.answer('Пользователь не найден в списке.')
        return
    try:
        session = await panel_login()
        inbounds = await panel_inbounds(session)
    except PanelError as error:
        await message.answer(str(error))
        return
    vpn = xui.vpn_inbounds(inbounds)
    client = xui.find_client_any(vpn, record.get('email') or '')
    if not client:
        await message.answer('Клиент не найден. Сначала /admin_repair ' + html.escape(record.get('username') or ''))
        return
    updated = dict(client)
    updated['totalGB'] = limit_bytes
    result = await xui.update_client(updated, session['cookies'], session['csrf'])
    if not result.get('success'):
        await message.answer('Не удалось изменить лимит: ' + html.escape(result.get('msg') or ''))
        return
    await message.answer(
        f'✅ Лимит для @{html.escape(record.get("username") or "")}: '
        f'{fmt_bytes(limit_bytes) if limit_bytes else "без ограничения"}.',
        parse_mode='HTML',
    )


def parse_date(value, current_expiry=0):
    value = (value or '').strip().lower()
    if value in ('off', '0', 'нет', 'бессрочно', 'none'):
        return 0
    if value.startswith('+'):
        days = int(value[1:])
        if days < 0:
            raise ValueError('negative days')
        base = int(current_expiry) if current_expiry and int(current_expiry) > now_ms() else now_ms()
        return base + days * 86400000
    for pattern in ('%d.%m.%Y', '%Y-%m-%d'):
        try:
            parsed = datetime.strptime(value, pattern)
        except ValueError:
            continue
        parsed = parsed.replace(tzinfo=MSK, hour=23, minute=59, second=59, microsecond=0)
        return int(parsed.timestamp() * 1000)
    raise ValueError('bad date')


@dp.message(Command('admin_date'))
async def admin_date(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    args = _args(message)
    if len(args) < 2:
        await message.answer('Использование: /admin_date <ник> <ДД.ММ.ГГГГ|ГГГГ-ММ-ДД|+N|off>')
        return
    try:
        record = USER_STORE.find_by_identifier(args[0])
    except StoreError as error:
        await message.answer(f'⚠️ Список пользователей повреждён: {html.escape(str(error))}')
        return
    if not record:
        await message.answer('Пользователь не найден в списке.')
        return
    try:
        session = await panel_login()
        inbounds = await panel_inbounds(session)
    except PanelError as error:
        await message.answer(str(error))
        return
    vpn = xui.vpn_inbounds(inbounds)
    client = xui.find_client_any(vpn, record.get('email') or '')
    if not client:
        await message.answer('Клиент не найден. Сначала /admin_repair ' + html.escape(record.get('username') or ''))
        return
    try:
        expiry = parse_date(args[1], int(client.get('expiryTime') or 0))
    except ValueError:
        await message.answer('Не понимаю дату. Примеры: 29.10.2027, 2027-10-29, +30, off.')
        return
    updated = dict(client)
    updated['expiryTime'] = expiry
    result = await xui.update_client(updated, session['cookies'], session['csrf'])
    if not result.get('success'):
        await message.answer('Не удалось изменить срок: ' + html.escape(result.get('msg') or ''))
        return
    if expiry:
        await message.answer(
            f'✅ Срок для @{html.escape(record.get("username") or "")}: до {fmt_date(expiry)} (23:59 МСК).',
            parse_mode='HTML',
        )
    else:
        await message.answer(f'✅ Срок для @{html.escape(record.get("username") or "")} убран (бессрочно).', parse_mode='HTML')


@dp.message(Command('admin_enable'))
async def admin_enable(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    args = _args(message)
    if not args:
        await message.answer('Использование: /admin_enable <ник>')
        return
    try:
        record = USER_STORE.find_by_identifier(args[0])
    except StoreError as error:
        await message.answer(f'⚠️ Список пользователей повреждён: {html.escape(str(error))}')
        return
    if not record:
        await message.answer('Пользователь не найден в списке.')
        return
    record['enabled'] = True
    try:
        USER_STORE.update(record)
    except StoreError as error:
        await message.answer(f'⚠️ Не удалось сохранить список: {html.escape(str(error))}')
        return
    try:
        session = await panel_login()
        inbounds = await panel_inbounds(session)
    except PanelError as error:
        await message.answer('Запись включена, но панель недоступна: ' + html.escape(str(error)))
        return
    ensured = await ensure_client(record, session, inbounds, enable=True)
    if not ensured.get('success'):
        await message.answer('Запись включена, но клиента включить не удалось: ' + html.escape(str(ensured.get('msg'))))
        return
    client = ensured.get('client') or {}
    vpn = xui.vpn_inbounds(ensured['inbounds'])
    used, _last = xui.traffic_used(vpn, record.get('email') or '')
    _active, reason = xui.client_status(client, used, now_ms())
    await message.answer(
        f'✅ Доступ @{html.escape(record.get("username") or "")} включён. Статус: {STATUS_TEXT.get(reason, "?")}. '
        f'Если нужен новый срок/лимит — /admin_date, /admin_limit.',
        parse_mode='HTML',
    )


@dp.message(Command('admin_disable'))
async def admin_disable(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    args = _args(message)
    if not args:
        await message.answer('Использование: /admin_disable <ник>')
        return
    try:
        record = USER_STORE.find_by_identifier(args[0])
    except StoreError as error:
        await message.answer(f'⚠️ Список пользователей повреждён: {html.escape(str(error))}')
        return
    if not record:
        await message.answer('Пользователь не найден в списке.')
        return
    record['enabled'] = False
    try:
        USER_STORE.update(record)
    except StoreError as error:
        await message.answer(f'⚠️ Не удалось сохранить список: {html.escape(str(error))}')
        return
    panel_ok = False
    try:
        session = await panel_login()
        inbounds = await panel_inbounds(session)
        vpn = xui.vpn_inbounds(inbounds)
        client = xui.find_client_any(vpn, record.get('email') or '')
        if client:
            updated = dict(client)
            updated['enable'] = False
            result = await xui.update_client(updated, session['cookies'], session['csrf'])
            panel_ok = bool(result.get('success'))
        else:
            panel_ok = True
    except PanelError:
        panel_ok = False
    text = f'⛔ Доступ @{html.escape(record.get("username") or "")} отключён.'
    if not panel_ok:
        text += '\n⚠️ Панель недоступна: запись отключена, но клиента отключить не удалось. Повторите позже.'
    await message.answer(text, parse_mode='HTML')


@dp.message(Command('admin_set_id'))
async def admin_set_id(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    args = _args(message)
    if len(args) < 2 or not args[1].isdigit():
        await message.answer('Использование: /admin_set_id <ник> <числовой Telegram ID>')
        return
    tg_id = int(args[1])
    try:
        record = USER_STORE.find_by_identifier(args[0])
    except StoreError as error:
        await message.answer(f'⚠️ Список пользователей повреждён: {html.escape(str(error))}')
        return
    if not record:
        await message.answer('Пользователь не найден в списке.')
        return
    other = USER_STORE.find_by_tg_id(tg_id)
    if other and _norm(other.get('email')) != _norm(record.get('email')):
        await message.answer(f'ID {tg_id} уже привязан к записи {html.escape(other.get("email") or "")}.')
        return
    record['tg_id'] = tg_id
    try:
        USER_STORE.update(record)
    except StoreError as error:
        await message.answer(f'⚠️ Не удалось сохранить список: {html.escape(str(error))}')
        return
    await message.answer(
        f'✅ Для @{html.escape(record.get("username") or "")} установлен ID <code>{tg_id}</code>.',
        parse_mode='HTML',
    )


@dp.message(Command('admin_reset_id'))
async def admin_reset_id(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    args = _args(message)
    if not args:
        await message.answer('Использование: /admin_reset_id <ник>')
        return
    try:
        record = USER_STORE.find_by_identifier(args[0])
    except StoreError as error:
        await message.answer(f'⚠️ Список пользователей повреждён: {html.escape(str(error))}')
        return
    if not record:
        await message.answer('Пользователь не найден в списке.')
        return
    record['tg_id'] = 0
    try:
        USER_STORE.update(record)
    except StoreError as error:
        await message.answer(f'⚠️ Не удалось сохранить список: {html.escape(str(error))}')
        return
    await message.answer(
        f'✅ Привязка ID для @{html.escape(record.get("username") or "")} сброшена. '
        f'Следующий вход с этим ником привяжет новый ID.',
        parse_mode='HTML',
    )


@dp.message(Command('admin_repair'))
async def admin_repair(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not await ensure_private(message):
        return
    args = _args(message)
    if not args:
        await message.answer('Использование: /admin_repair <ник>')
        return
    try:
        record = USER_STORE.find_by_identifier(args[0])
    except StoreError as error:
        await message.answer(f'⚠️ Список пользователей повреждён: {html.escape(str(error))}')
        return
    if not record:
        await message.answer('Пользователь не найден в списке.')
        return
    try:
        session = await panel_login()
        inbounds = await panel_inbounds(session)
    except PanelError as error:
        await message.answer(str(error))
        return
    ensured = await ensure_client(record, session, inbounds)
    if not ensured.get('success'):
        await message.answer('Не удалось подготовить клиента: ' + html.escape(str(ensured.get('msg'))))
        return
    client = ensured.get('client') or {}
    vpn = xui.vpn_inbounds(ensured['inbounds'])
    used, _last = xui.traffic_used(vpn, record.get('email') or '')
    links_resp = await xui.get_client_links(record.get('email') or '', session['cookies'], session['csrf'])
    links = [link for link in (links_resp.get('obj') or []) if xui.link_kind(link) in LINK_LABELS]
    _active, reason = xui.client_status(client, used, now_ms())
    await message.answer(
        f'✅ Клиент @{html.escape(record.get("username") or "")} готов.\n'
        f'Статус: {STATUS_TEXT.get(reason, "?")}\n'
        f'Ссылок: {len(links)}\n'
        f'Трафик: {fmt_bytes(used)}'
        + ('' if record.get('enabled', True) else '\n⚠️ Запись в списке отключена (/admin_enable).'),
        parse_mode='HTML',
    )


@dp.message()
async def fallback_message(message: types.Message):
    """Любое первое сообщение/неизвестный текст показывает кнопочное меню."""
    if not await ensure_private(message):
        return
    await message.answer(
        'Пользуйтесь кнопками ниже — меню всегда под рукой.',
        reply_markup=main_keyboard(message.from_user),
    )


async def main():
    await bot.delete_webhook()
    try:
        # Левое меню «/» не нужно: меню — кнопки. /start остаётся обработчиком
        # для кнопки «Начать» и ручного ввода.
        await bot.delete_my_commands()
    except Exception as error:  # noqa: BLE001
        logging.warning('Не удалось очистить меню команд: %s', type(error).__name__)
    logging.info('Бот запущен; админов: %d; новый срок: %d дней', len(ADMIN_IDS), NEW_USER_DAYS)
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
