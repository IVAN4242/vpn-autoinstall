"""Проверка настроек инбаунда WebSocket: python3 -m unittest discover -s tests"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

import provision_inbounds as provision  # noqa: E402


class FakePanel:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.calls = []

    def call(self, method, path, body=None, form=None):
        self.calls.append((method, path, body))
        if method == 'GET':
            return {'success': True, 'obj': self.existing}
        return {'success': True}


class WsInboundTest(unittest.TestCase):
    def test_listens_locally_without_tls(self):
        body = provision.ws_inbound('vless-ws', 14553, '/secret', 'd.com')
        stream = json.loads(body['streamSettings'])
        self.assertEqual((body['listen'], body['port'], body['protocol']), ('127.0.0.1', 14553, 'vless'))
        self.assertEqual((stream['network'], stream['security']), ('ws', 'none'))
        self.assertEqual(stream['wsSettings']['path'], '/secret')
        self.assertEqual(stream['sockopt'], {'trustedXForwardedFor': ['X-Real-IP']})

    def test_public_host_uses_http1(self):
        panel = FakePanel()
        provision.ensure_public_host(panel, 5, 'public-tls-ws', ['http/1.1'], 'd.com', '443')
        _method, path, body = panel.calls[-1]
        self.assertEqual(path, '/panel/api/hosts/add')
        self.assertEqual(body['inboundIds'], [5])
        self.assertEqual(body['hosts'], ['d.com:443'])
        self.assertEqual(body['alpn'], ['http/1.1'])

    def test_existing_host_is_kept(self):
        panel = FakePanel(existing=[{'id': 1}])
        provision.ensure_public_host(panel, 5, 'public-tls-ws', ['http/1.1'], 'd.com', '443')
        self.assertEqual(len(panel.calls), 1)


if __name__ == '__main__':
    unittest.main()
