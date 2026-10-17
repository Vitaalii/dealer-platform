import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from ._compose import INFRA, load_module

gen = load_module("catalog_gen", INFRA / "db" / "catalog" / "gen_migration.py")
migrate = load_module("migrate", INFRA / "db" / "migrate.py")

SAMPLE = INFRA / "tests" / "fixtures" / "catalog_sample"
TABLES = gen.load_schema(SAMPLE / "schema")
SQL = gen.render_sql(TABLES)
CYRILLIC_C = "Сonsumption"


def run_main(*argv) -> int:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return gen.main(list(argv))


def write_columns(root: Path, brand: str, table: str, columns) -> None:
    path = root / brand / f"{table}.columns.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"columns": columns}, ensure_ascii=False), encoding="utf-8")


def schema_error(columns) -> str:
    with tempfile.TemporaryDirectory() as d:
        write_columns(Path(d), "demo", "catalog", columns)
        with unittest.TestCase().assertRaises(gen.SchemaError) as ctx:
            gen.load_schema(Path(d))
        return str(ctx.exception)


class GenMigrationTest(unittest.TestCase):
    def test_all_eight_sample_tables(self):
        self.assertEqual(sorted(t.source_table for t in TABLES), sorted([
            "catalog", "features", "sites", "parsed_prices", "staging_changes", "run_log",
            "site_config_history", "option_silences"]))
        for t in TABLES:
            self.assertIn(f'CREATE TABLE catalog."demo_{t.source_table}" (', SQL)

    def test_table_name_rule(self):
        self.assertEqual(gen.pg_table_name("haval", "catalog"), "haval_catalog")
        self.assertEqual({t.pg_table for t in TABLES}, {gen.pg_table_name("demo", t.source_table) for t in TABLES})

    def test_names_quoted_byte_exact(self):
        self.assertIn('    "Model id" bigint,', SQL)
        self.assertIn('    "Id" bigserial PRIMARY KEY,', SQL)
        line = f'    "{CYRILLIC_C}" numeric,'
        self.assertIn(line, SQL)
        self.assertIn(b"\xd0\xa1onsumption", SQL.encode("utf-8"))  # U+0421, не латинская C
        self.assertNotIn('"Consumption"', SQL)

    def test_types(self):
        catalog = next(t for t in TABLES if t.source_table == "catalog")
        types = {c.name: c.pg_type for c in catalog.columns}
        self.assertEqual(types["Transmission"], "text")
        self.assertEqual(types["Price new"], "numeric")
        self.assertEqual(types["Active"], "boolean")
        self.assertEqual(types["Active_1"], "boolean")
        self.assertEqual(types["Pics"], "text")
        self.assertEqual(types["CreatedAt"], "timestamptz")

    def test_single_select_without_check_options_in_meta(self):
        self.assertNotRegex(SQL.upper(), r"\bCHECK\b")
        self.assertIn("""'Transmission', 'SingleSelect', 'text', '["AT", "MT", "CVT"]'::jsonb)""", SQL)

    def test_options_from_nocodb_meta_shape(self):
        with tempfile.TemporaryDirectory() as d:
            write_columns(Path(d), "demo", "t", [
                {"title": "Id", "uidt": "ID"},
                {"title": "Kind", "uidt": "SingleSelect", "colOptions": {"options": [{"title": "A"}, {"title": "B'"}]}},
            ])
            table = gen.load_schema(Path(d))[0]
        self.assertEqual(table.columns[1].options, ("A", "B'"))
        self.assertIn("""'["A", "B''"]'::jsonb""", gen.render_sql([table]))

    def test_computed_and_unknown_types_rejected(self):
        for uidt in ("Formula", "Lookup", "Rollup", "Links", "LinkToAnotherRecord"):
            with self.subTest(uidt=uidt):
                self.assertIn("computed", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "X", "uidt": uidt}]))
        self.assertIn("unknown type", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "X", "uidt": "Rating"}]))
        self.assertIn("unknown type", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "X"}]))

    def test_bad_columns_rejected(self):
        self.assertIn("exactly one ID", schema_error([{"title": "X", "uidt": "SingleLineText"}]))
        self.assertIn("duplicate", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "Id", "uidt": "Number"}]))
        self.assertIn("longer than", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "Я" * 32, "uidt": "LongText"}]))
        self.assertIn('has "', schema_error([{"title": "Id", "uidt": "ID"}, {"title": 'a"b', "uidt": "LongText"}]))
        self.assertIn("spaces", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "a ", "uidt": "LongText"}]))

    def test_bad_folder_names_and_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            write_columns(Path(d), "Haval", "catalog", [{"title": "Id", "uidt": "ID"}])
            with self.assertRaises(gen.SchemaError):
                gen.load_schema(Path(d))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(gen.SchemaError):
                gen.load_schema(Path(d))

    def test_no_grants_only_catalog_and_platform_meta(self):
        self.assertNotRegex(SQL.upper(), r"\b(GRANT|REVOKE)\b|ALTER\s+DEFAULT")
        for target in re.findall(r"CREATE TABLE (\S+)", SQL):
            self.assertRegex(target, r'^(catalog\."demo_[a-z_]+"|platform_meta\.catalog_(columns|imports))$')
        self.assertNotRegex(SQL, r"CREATE SCHEMA")

    def test_module_round_trip_and_discover(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "0002_catalog_brands.py"
            self.assertEqual(run_main("--schema", str(SAMPLE / "schema"), "--out", str(out)), 0)
            self.assertEqual(run_main("--schema", str(SAMPLE / "schema"), "--out", str(out), "--check"), 0)
            [m] = migrate.discover(Path(d))
            self.assertEqual((m.version, m.name), (2, "0002_catalog_brands"))
            self.assertEqual(m.sql, "\n" + SQL)
            out.write_text(out.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
            self.assertEqual(run_main("--schema", str(SAMPLE / "schema"), "--out", str(out), "--check"), 1)

    def test_tricky_text_survives_module(self):
        tables = [gen.Table("demo", "t", "demo_t", (
            gen.Column("Id", "ID", "bigserial", None),
            gen.Column("K", "SingleSelect", "text", ('a\\b', 'x"""y', "it's")),
        ))]
        sql = gen.render_sql(tables)
        namespace = {}
        exec(gen.render_module(sql), namespace)
        self.assertEqual(namespace["SQL"], "\n" + sql)

    def test_repo_catalog_migration_has_no_grants(self):
        for m in migrate.discover():
            if "catalog" in m.name:
                with self.subTest(migration=m.name):
                    self.assertNotRegex(m.sql.upper(), r"\b(GRANT|REVOKE)\b|ALTER\s+DEFAULT")
                    self.assertEqual(run_main("--check"), 0)


if __name__ == "__main__":
    unittest.main()
