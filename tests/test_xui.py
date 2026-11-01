"""Проверка разбора ссылок панели: python3 -m unittest discover -s tests"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.modules.setdefault('aiohttp', mock.MagicMock())
sys.modules.setdefault('yarl', mock.MagicMock())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'bot'))

import xui  # noqa: E402


class LinkKindTest(unittest.TestCase):
    def test_kinds(self):
        cases = {
            'hysteria2://a@d.com:443?alpn=h3&security=tls&sni=d.com#x': 'hysteria2',
            'hysteria2://a@d.com:2096?obfs=salamander&obfs-password=p&sni=d.com#x': 'hysteria2-obfs',
            'vless://u@d.com:443?flow=xtls-rprx-vision&security=tls&type=tcp#x': 'vless-vision',
            'vless://u@d.com:443?mode=stream-up&path=%2Fa&security=tls&type=xhttp#x': 'vless-xhttp',
            'trojan://p@d.com:443#x': None,
            '': None,
        }
        for link, kind in cases.items():
            with self.subTest(link=link):
                self.assertEqual(xui.link_kind(link), kind)

    def test_vpn_inbounds(self):
        inbounds = [
            {'protocol': 'hysteria', 'enable': True},
            {'protocol': 'vless', 'enable': True},
            {'protocol': 'vless', 'enable': False},
            {'protocol': 'trojan', 'enable': True},
        ]
        self.assertEqual(xui.vpn_inbounds(inbounds), inbounds[:2])


class TokenAuthTest(unittest.TestCase):
    def test_token_login_skips_password(self):
        with mock.patch.multiple(xui, XUI_BASE_URL='https://d.com', XUI_API_TOKEN='t', XUI_USERNAME='', XUI_PASSWORD=''):
            self.assertEqual(asyncio.run(xui.login()), {'success': True, 'cookies': {}, 'csrf': ''})

    def test_update_keeps_vision_flow(self):
        calls = []

        async def fake_request(method, path, cookies, csrf, json_data=None):
            calls.append(json_data)
            return {'success': True}

        with mock.patch.object(xui, '_request', fake_request):
            asyncio.run(xui.update_client({'email': 'a', 'totalGB': 5}, {}, ''))
        self.assertEqual(calls[0], {'email': 'a', 'totalGB': 5, 'flow': xui.VISION_FLOW})

    def test_no_credentials_rejected(self):
        with mock.patch.multiple(xui, XUI_BASE_URL='https://d.com', XUI_API_TOKEN='', XUI_USERNAME='', XUI_PASSWORD=''):
            with self.assertRaises(xui.XuiError):
                asyncio.run(xui.login())


if __name__ == '__main__':
    unittest.main()
