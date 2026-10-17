import re
import unittest

from ._compose import INFRA, env_example_keys, read, variables

KEYS = env_example_keys(INFRA / ".env.example")
COMPOSE_TEXT = read(INFRA / "compose.yaml")
ROLES_TEXT = read(INFRA / "postgres" / "init" / "10-roles.sh")
CADDY_TEXT = read(INFRA / "caddy" / "Caddyfile")
# Переменные, которые скрипты бэкапов читают из .env сами (env_get в infra/backup/*.sh), а не через compose.
BACKUP_TEXT = "\n".join(read(p) for p in sorted((INFRA / "backup").glob("*.sh")))
BACKUP_KEYS = set(re.findall(r"env_get ([A-Z0-9_]+)", BACKUP_TEXT))


class EnvExampleTest(unittest.TestCase):
    def test_no_values(self):
        self.assertEqual({k: v for k, v in KEYS.items() if v.strip()}, {})

    def test_required_compose_vars_listed(self):
        required = {n for n, kind in variables(COMPOSE_TEXT).items() if kind == ":?"}
        self.assertEqual(required - set(KEYS), set())

    def test_no_unused_keys(self):
        used = set(variables(COMPOSE_TEXT)) | {"COMPOSE_FILE"} | BACKUP_KEYS
        self.assertEqual(set(KEYS) - used, set())

    def test_optional_vars_have_defaults(self):
        for name, kind in variables(COMPOSE_TEXT).items():
            with self.subTest(var=name):
                self.assertIn(kind, (":?", ":-"))

    def test_role_passwords_passed_to_postgres(self):
        names = set(re.findall(r"\\getenv \w+ ([A-Z0-9_]+)", ROLES_TEXT))
        self.assertEqual(len(names), 7)
        self.assertEqual(names - set(KEYS), set())
        for name in names:
            self.assertIn(f"{name}: ${{{name}:?", COMPOSE_TEXT)

    def test_caddy_vars_passed(self):
        for name in re.findall(r"\{\$([A-Z0-9_]+)\}", CADDY_TEXT):
            with self.subTest(var=name):
                self.assertIn(f"{name}: ${{{name}:?", COMPOSE_TEXT)

    def test_env_file_ignored_by_git(self):
        gitignore = read(INFRA.parent / ".gitignore").splitlines()
        self.assertIn(".env", gitignore)
        self.assertIn("!.env.example", gitignore)


if __name__ == "__main__":
    unittest.main()
