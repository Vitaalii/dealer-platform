import re
import tempfile
import unittest
from pathlib import Path

from ._compose import INFRA, load_module

migrate = load_module("migrate", INFRA / "db" / "migrate.py")

ALL = migrate.discover()
SQL_0001 = ALL[0].sql


def grants_to(role: str, sql: str) -> list[str]:
    statements = [" ".join(s.split()) for s in sql.split(";")]
    return [s for s in statements if s.startswith(("GRANT", "ALTER DEFAULT")) and re.search(rf"\bTO [\w, ]*\b{role}\b", s)]


class MigrationsTest(unittest.TestCase):
    def test_discover_order_and_names(self):
        self.assertEqual([(m.version, m.name) for m in ALL][:1], [(1, "0001_schemas_and_grants")])
        self.assertEqual([m.version for m in ALL], sorted(m.version for m in ALL))

    def test_pending(self):
        self.assertEqual(migrate.pending(ALL, {1}), ALL[1:])
        self.assertEqual(migrate.pending(ALL, set()), ALL)

    def test_duplicate_and_empty_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "0001_a.py").write_text('SQL = "SELECT 1"')
            Path(d, "0001_b.py").write_text('SQL = "SELECT 2"')
            with self.assertRaises(ValueError):
                migrate.discover(Path(d))
        with tempfile.TemporaryDirectory() as d:
            Path(d, "0001_a.py").write_text('SQL = "  "')
            with self.assertRaises(ValueError):
                migrate.discover(Path(d))

    def test_other_files_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "__init__.py").write_text("")
            Path(d, "notes.txt").write_text("")
            Path(d, "0002_x.py").write_text('SQL = "SELECT 1"')
            self.assertEqual([m.version for m in migrate.discover(Path(d))], [2])

    def test_missing_vars(self):
        self.assertEqual(migrate.missing_vars({}), list(migrate.REQUIRED_VARS))
        full = {n: "x" for n in migrate.REQUIRED_VARS}
        self.assertEqual(migrate.missing_vars(full), [])

    def test_schemas_created(self):
        for schema in ("catalog", "content", "platform_ro"):
            self.assertIn(f"CREATE SCHEMA {schema} AUTHORIZATION platform_admin", SQL_0001)

    def test_nocodb_only_select_in_platform_ro(self):
        ro = [g for g in grants_to("nocodb_src", SQL_0001) if "platform_ro" in g and "ON TABLES" in g]
        self.assertEqual(ro, ["ALTER DEFAULT PRIVILEGES FOR ROLE platform_admin IN SCHEMA platform_ro GRANT SELECT ON TABLES TO nocodb_src"])
        for g in grants_to("nocodb_src", SQL_0001):
            if "INSERT" in g or "UPDATE" in g or "DELETE" in g:
                self.assertNotIn("platform_ro", g)
            self.assertNotIn("CREATE", g.replace("CREATE SCHEMA", ""))

    def test_bot_and_n8n_read_only_catalog_content(self):
        for role in ("bot_app", "n8n_reader"):
            for g in grants_to(role, SQL_0001):
                with self.subTest(role=role, grant=g):
                    self.assertNotIn("platform_ro", g)
                    self.assertNotRegex(g, r"\b(INSERT|UPDATE|DELETE)\b")

    def test_rights_check_table(self):
        self.assertIn("CREATE TABLE platform_ro.rights_check", SQL_0001)


if __name__ == "__main__":
    unittest.main()
