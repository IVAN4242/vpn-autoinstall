"""Список пользователей, которым разрешён VPN.

Файл users.json (600 root:root) — единственный источник разрешений.
Клиенты в 3x-ui — это выдача; список решает, кому её вообще можно показывать.

Запись атомарная (tmp + os.replace), перед заменой сохраняется users.json.bak.
Ручное редактирование — только при остановленном боте.
"""

import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PATH = Path(os.getenv('USERS_FILE') or (Path(__file__).resolve().parent / 'users.json'))


class StoreError(Exception):
    """Список повреждён или запись не найдена."""


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _norm(value):
    return (value or '').strip().casefold()


class UserStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else DEFAULT_PATH
        self._lock = threading.Lock()

    # -- низкий уровень -----------------------------------------------------
    def empty(self):
        return {'version': 1, 'updated_at': _now_iso(), 'users': []}

    def load(self):
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self):
        if not self.path.exists():
            return self.empty()
        try:
            with open(self.path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError) as error:
            raise StoreError(f'Не удалось прочитать {self.path.name}: {type(error).__name__}') from error
        if not isinstance(data, dict) or not isinstance(data.get('users'), list):
            raise StoreError(f'Неверный формат {self.path.name}')
        clean = []
        for record in data['users']:
            if not isinstance(record, dict) or not record.get('email'):
                continue
            clean.append(record)
        data['users'] = clean
        data.setdefault('version', 1)
        return data

    def save(self, data):
        with self._lock:
            self._save_unlocked(data)

    def _save_unlocked(self, data):
        data['version'] = 1
        data['updated_at'] = _now_iso()
        data['users'] = sorted(
            data.get('users') or [],
            key=lambda record: _norm(record.get('username') or record.get('email')),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix='.users-', suffix='.json', dir=str(self.path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            if self.path.exists():
                try:
                    shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + '.bak'))
                except OSError:
                    pass
            os.replace(tmp_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    # -- поиск --------------------------------------------------------------
    def all(self):
        return self.load()['users']

    def find(self, tg_id=None, username=None):
        """Поиск для обычного пользователя.

        Сначала точный Telegram ID. Ник — только если у записи ещё нет ID
        (первая привязка) или ID совпал. Иначе чужой ник не даёт доступа.
        """
        users = self.all()
        if tg_id:
            for record in users:
                if int(record.get('tg_id') or 0) == int(tg_id):
                    return record
        if username:
            matches = [r for r in users if _norm(r.get('username')) == _norm(username)]
            if len(matches) == 1:
                record = matches[0]
                if not int(record.get('tg_id') or 0):
                    return record
        return None

    def find_by_identifier(self, identifier):
        """Для админ-команд: @ник, email или числовой ID."""
        ident = (identifier or '').strip().lstrip('@')
        if not ident:
            return None
        users = self.all()
        if ident.isdigit():
            for record in users:
                if int(record.get('tg_id') or 0) == int(ident):
                    return record
        for record in users:
            if _norm(record.get('username')) == _norm(ident) or _norm(record.get('email')) == _norm(ident):
                return record
        return None

    def find_by_tg_id(self, tg_id):
        if not tg_id:
            return None
        for record in self.all():
            if int(record.get('tg_id') or 0) == int(tg_id):
                return record
        return None

    # -- изменение ----------------------------------------------------------
    def upsert(self, record):
        with self._lock:
            data = self._load_unlocked()
            email = _norm(record.get('email'))
            for index, existing in enumerate(data['users']):
                if _norm(existing.get('email')) == email:
                    data['users'][index] = record
                    self._save_unlocked(data)
                    return record
            data['users'].append(record)
            self._save_unlocked(data)
            return record

    def update(self, record):
        return self.upsert(record)

    def remove(self, identifier):
        record = self.find_by_identifier(identifier)
        if not record:
            return None
        with self._lock:
            data = self._load_unlocked()
            email = _norm(record.get('email'))
            data['users'] = [r for r in data['users'] if _norm(r.get('email')) != email]
            self._save_unlocked(data)
        return record

    def count(self):
        return len(self.all())
