import unittest

from . import _paths  # noqa: F401
from platform_core.config import REQUIRED_VARS, ConfigError, check_or_exit, load_settings, missing_vars

SECRET = "s3cr3t-value-must-not-leak"


def full_env(**over):
    env = {
        "PLATFORM_ENV": "test",
        "PLATFORM_DB_HOST": "postgres",
        "PLATFORM_DB_NAME": "platform",
        "PLATFORM_DB_USER": "platform_app",
        "PLATFORM_DB_PASSWORD": SECRET,
        "MAIL_PROVIDER": "log",
        "MAIL_FROM": "no-reply@notify.example.org",
    }
    env.update(over)
    return env


class ConfigTest(unittest.TestCase):
    def test_full_env_loads(self):
        s = load_settings(full_env(PLATFORM_VERSION="abc123"))
        self.assertEqual((s.env, s.db_host, s.db_port, s.http_port, s.version), ("test", "postgres", 5432, 8000, "abc123"))

    def test_version_defaults_to_dev(self):
        self.assertEqual(load_settings(full_env()).version, "dev")

    def test_password_not_in_repr(self):
        self.assertNotIn(SECRET, repr(load_settings(full_env())))

    def test_missing_and_empty_vars_listed(self):
        env = full_env(PLATFORM_DB_HOST="  ")
        del env["PLATFORM_DB_PASSWORD"]
        self.assertEqual(missing_vars(env, REQUIRED_VARS), ["PLATFORM_DB_HOST", "PLATFORM_DB_PASSWORD"])

    def test_bad_env_name(self):
        with self.assertRaises(ConfigError):
            load_settings(full_env(PLATFORM_ENV="dev"))

    def test_bad_port(self):
        with self.assertRaises(ConfigError):
            load_settings(full_env(PLATFORM_DB_PORT="abc"))
        with self.assertRaises(ConfigError):
            load_settings(full_env(PLATFORM_HTTP_PORT="70000"))

    def test_check_or_exit_logs_name_only_and_exits_1(self):
        lines = []
        env = full_env()
        del env["PLATFORM_DB_USER"]
        with self.assertRaises(SystemExit) as cm:
            check_or_exit(env, log=lines.append)
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(lines, ["CONFIG ERROR: missing PLATFORM_DB_USER"])

    def test_error_text_never_contains_values(self):
        lines = []
        with self.assertRaises(SystemExit):
            check_or_exit(full_env(PLATFORM_ENV=SECRET), log=lines.append)
        self.assertNotIn(SECRET, " ".join(lines))

    def test_check_or_exit_returns_settings(self):
        self.assertEqual(check_or_exit(full_env(), log=lambda _: None).db_name, "platform")


if __name__ == "__main__":
    unittest.main()
