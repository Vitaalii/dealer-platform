import unittest

from . import _paths  # noqa: F401
from platform_core.config import Settings
from platform_core.health import health_payload

SETTINGS = Settings(env="test", db_host="postgres", db_name="platform", db_user="u", db_password="p", version="abc")


class HealthTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(health_payload(SETTINGS, True), (200, {"status": "ok", "db": "ok", "env": "test", "version": "abc"}))

    def test_db_down(self):
        status, body = health_payload(SETTINGS, False)
        self.assertEqual((status, body["status"], body["db"]), (503, "error", "error"))

    def test_no_connection_details(self):
        _, body = health_payload(SETTINGS, True)
        self.assertNotIn("postgres", str(body))
        self.assertNotIn("platform", str(body))


if __name__ == "__main__":
    unittest.main()
