import subprocess
import unittest

from ._compose import INFRA

SCRIPT = INFRA / "postgres" / "init" / "10-roles.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
ROLES = ("platform_admin", "platform_app", "nocodb_src", "bot_app", "n8n_reader", "n8n", "nocodb_meta")


class RolesScriptTest(unittest.TestCase):
    def test_bash_syntax(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_all_roles_and_databases(self):
        for role in ROLES:
            self.assertIn(f"CREATE ROLE {role} LOGIN PASSWORD :'", TEXT)
        for db, owner in (("platform", "platform_admin"), ("n8n", "n8n"), ("nocodb_meta", "nocodb_meta")):
            self.assertIn(f"CREATE DATABASE {db} OWNER {owner};", TEXT)

    def test_passwords_not_on_command_line(self):
        self.assertNotIn("-v ", TEXT.replace("-v ON_ERROR_STOP", ""))
        self.assertIn("<<'EOSQL'", TEXT)

    def test_fails_on_missing_password_without_value(self):
        result = subprocess.run(["bash", str(SCRIPT)], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "CONFIG ERROR: missing PLATFORM_ADMIN_PASSWORD")


if __name__ == "__main__":
    unittest.main()
