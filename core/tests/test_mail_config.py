import unittest

from . import _paths  # noqa: F401
from platform_core.config import ConfigError, check_or_exit, load_settings

from .test_config import full_env

KEY_SECRET = "postbox-secret-must-not-leak"


def postbox_env(**over):
    env = full_env(MAIL_PROVIDER="postbox", POSTBOX_KEY_ID="key-id", POSTBOX_SECRET_KEY=KEY_SECRET)
    env.update(over)
    return env


class MailConfigTest(unittest.TestCase):
    def test_log_provider_defaults(self):
        mail = load_settings(full_env()).mail
        self.assertEqual(
            (mail.provider, mail.from_address, mail.from_name, mail.postbox_endpoint, mail.postbox_region, mail.max_per_second),
            ("log", "no-reply@notify.example.org", "", "https://postbox.cloud.yandex.net", "ru-central1", 1.0),
        )

    def test_postbox_optional_vars(self):
        mail = load_settings(postbox_env(
            MAIL_FROM_NAME=" Платформа дилеров ",
            POSTBOX_ENDPOINT="https://postbox.example/",
            POSTBOX_REGION="ru-test1",
            MAIL_MAX_PER_SECOND="14",
        )).mail
        self.assertEqual(
            (mail.from_name, mail.postbox_endpoint, mail.postbox_region, mail.max_per_second),
            ("Платформа дилеров", "https://postbox.example", "ru-test1", 14.0),
        )

    def test_mail_vars_required(self):
        lines = []
        env = full_env()
        del env["MAIL_PROVIDER"], env["MAIL_FROM"]
        with self.assertRaises(SystemExit) as cm:
            check_or_exit(env, log=lines.append)
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(lines, ["CONFIG ERROR: missing MAIL_PROVIDER, MAIL_FROM"])

    def test_postbox_keys_required(self):
        env = postbox_env(POSTBOX_KEY_ID=" ")
        del env["POSTBOX_SECRET_KEY"]
        with self.assertRaisesRegex(ConfigError, "^missing POSTBOX_KEY_ID, POSTBOX_SECRET_KEY$"):
            load_settings(env)

    def test_log_keys_not_required(self):
        self.assertEqual(load_settings(full_env()).mail.postbox_key_id, "")

    def test_unknown_provider(self):
        with self.assertRaisesRegex(ConfigError, "invalid MAIL_PROVIDER"):
            load_settings(full_env(MAIL_PROVIDER="smtp"))

    def test_log_forbidden_in_prod(self):
        with self.assertRaisesRegex(ConfigError, "not allowed when PLATFORM_ENV=prod"):
            load_settings(full_env(PLATFORM_ENV="prod"))
        self.assertEqual(load_settings(postbox_env(PLATFORM_ENV="prod")).mail.provider, "postbox")

    def test_bad_from_address(self):
        with self.assertRaisesRegex(ConfigError, "invalid MAIL_FROM"):
            load_settings(full_env(MAIL_FROM="no-reply"))

    def test_bad_rate(self):
        for value in ("abc", "0", "-1"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_settings(full_env(MAIL_MAX_PER_SECOND=value))

    def test_secret_not_in_repr_or_errors(self):
        self.assertNotIn(KEY_SECRET, repr(load_settings(postbox_env())))
        lines = []
        with self.assertRaises(SystemExit):
            check_or_exit(postbox_env(MAIL_PROVIDER=KEY_SECRET), log=lines.append)
        self.assertNotIn(KEY_SECRET, " ".join(lines))


if __name__ == "__main__":
    unittest.main()
