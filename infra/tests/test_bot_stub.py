import json
import threading
import unittest
import urllib.error
import urllib.request

from ._compose import INFRA, load_module

stub = load_module("stub", INFRA / "bot-stub" / "stub.py")


class BotStubTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = stub.make_server(0, host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_health(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=3) as r:
            self.assertEqual((r.status, json.loads(r.read())), (200, {"status": "ok", "service": "bot-stub"}))

    def test_other_paths_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=3)
        self.assertEqual(cm.exception.code, 404)

    def test_missing_vars(self):
        self.assertEqual(stub.missing_vars({}), [])
        self.assertEqual(stub.missing_vars({}, ("BOT_TOKEN",)), ["BOT_TOKEN"])


if __name__ == "__main__":
    unittest.main()
