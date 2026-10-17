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
    def test_all_sample_tables(self):
        self.assertEqual(sorted(t.source_table for t in TABLES), sorted([
            "catalog", "features", "sites", "parsed_prices", "staging_changes", "run_log",
            "site_config_history", "option_silences", "credit_programs"]))
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
        self.assertNotRegex(SQL[len(gen.META_SQL):].upper(), r"\bCHECK\b")
        self.assertIn("""'Transmission', 'SingleSelect', 'text', '["AT", "MT", "CVT"]'::jsonb, NULL)""", SQL)

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
        for uidt in ("Formula", "Lookup", "Rollup", "Count"):
            with self.subTest(uidt=uidt):
                self.assertIn("computed", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "X", "uidt": uidt}]))
        self.assertIn("unknown type", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "X", "uidt": "Rating"}]))
        self.assertIn("unknown type", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "X"}]))

    def test_bad_columns_rejected(self):
        self.assertIn("no ID column and no key", schema_error([{"title": "X", "uidt": "SingleLineText"}]))
        self.assertIn("need one ID", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "Id2", "uidt": "ID"}]))
        self.assertIn("duplicate", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "Id", "uidt": "Number"}]))
        self.assertIn("longer than", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "Я" * 32, "uidt": "LongText"}]))
        self.assertIn('has "', schema_error([{"title": "Id", "uidt": "ID"}, {"title": 'a"b', "uidt": "LongText"}]))
        self.assertIn("spaces", schema_error([{"title": "Id", "uidt": "ID"}, {"title": "a ", "uidt": "LongText"}]))

    def test_system_fields_and_links_skipped(self):
        catalog = next(t for t in TABLES if t.source_table == "catalog")
        roles = {c.name: c.role for c in catalog.columns}
        for name in ("nc_created_by", "nc_order", "Stock", "_nc_m2m_demo_stock"):
            with self.subTest(column=name):
                self.assertEqual(roles[name], "skipped")
                self.assertNotIn(f'    "{name}" ', SQL)
        self.assertIn("'nc_created_by', 'CreatedBy', NULL, NULL, 'skipped')", SQL)
        self.assertIn("'Stock', 'Links', NULL, NULL, 'skipped')", SQL)
        self.assertIn("'Id', 'ID', 'bigserial', NULL, 'key')", SQL)

    def test_table_without_id_gets_key_and_generated_id(self):
        table = next(t for t in TABLES if t.source_table == "credit_programs")
        self.assertEqual(table.key, "Program_id")
        self.assertIn('CREATE TABLE catalog."demo_credit_programs" (\n    "Id" bigserial PRIMARY KEY,\n', SQL)
        self.assertIn('    "Program_id" text NOT NULL UNIQUE,', SQL)
        self.assertIn("'demo_credit_programs', 1, 'Id', 'ID', 'bigserial', NULL, 'generated')", SQL)

    def test_keys_file_errors(self):
        no_id = [{"title": "Code", "uidt": "SingleLineText"}, {"title": "Name", "uidt": "LongText"}]
        cases = (
            ({"demo/other": "Code"}, no_id, "no ID column and no key"),
            ({"demo/catalog": "Name"}, no_id, "must be a SingleLineText or Number"),
            ({"demo/catalog": "Nope"}, no_id, "must be a SingleLineText or Number"),
            ({"demo/catalog": "Code"}, [{"title": "Id", "uidt": "ID"}] + no_id, "has ID column"),
            ({"demo/catalog": "Code"}, no_id + [{"title": "Id", "uidt": "Number"}], "is not ID"),
        )
        for keys, columns, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as d:
                write_columns(Path(d), "demo", "catalog", columns)
                Path(d, "keys.json").write_text(json.dumps(keys), encoding="utf-8")
                with self.assertRaisesRegex(gen.SchemaError, message):
                    gen.load_schema(Path(d))
        with tempfile.TemporaryDirectory() as d:
            write_columns(Path(d), "demo", "catalog", [{"title": "Id", "uidt": "ID"}])
            Path(d, "keys.json").write_text(json.dumps({"demo/ghost": "Code"}), encoding="utf-8")
            with self.assertRaisesRegex(gen.SchemaError, "no such tables: demo/ghost"):
                gen.load_schema(Path(d))

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

    def test_repo_schema_from_ce(self):
        """Описания колонок от CE (17.10) в infra/db/catalog/schema/ и миграция 0002 из них."""
        tables = gen.load_schema()
        self.assertEqual(len(tables), 43)
        self.assertEqual({t.brand for t in tables}, {"geely", "belgee", "tenet", "haval"})
        by_name = {t.pg_table: t for t in tables}
        for brand in ("geely", "belgee", "tenet", "haval"):
            with self.subTest(brand=brand):
                self.assertIn(CYRILLIC_C, {c.name for c in by_name[f"{brand}_catalog"].created})
        self.assertEqual(by_name["geely_credit_programs"].key, "Program_id")
        self.assertEqual(by_name["geely_credit_rates"].key, "Rate_id")
        sql = gen.render_sql(tables)
        for name in ("nc_created_by", "nc_updated_by", "nc_order", "GEELY_Stock", "GEELY copy", "GEELY Color copy"):
            self.assertNotIn(f'    "{name}" ', sql)
        self.assertNotRegex(sql, r'    "_nc_m2m_')
        [m] = [m for m in migrate.discover() if m.version == 2]
        self.assertEqual(m.name, "0002_catalog_brands")
        self.assertEqual(m.sql, "\n" + sql)

    def test_repo_catalog_migration_has_no_grants(self):
        for m in migrate.discover():
            if "catalog" in m.name:
                with self.subTest(migration=m.name):
                    self.assertNotRegex(m.sql.upper(), r"\b(GRANT|REVOKE)\b|ALTER\s+DEFAULT")
                    self.assertEqual(run_main("--check"), 0)


if __name__ == "__main__":
    unittest.main()
