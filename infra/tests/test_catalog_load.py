import asyncio
import copy
import json
import re
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from ._compose import INFRA, load_module

gen = load_module("catalog_gen", INFRA / "db" / "catalog" / "gen_migration.py")
load = load_module("catalog_load", INFRA / "db" / "catalog" / "load.py")

SAMPLE = INFRA / "tests" / "fixtures" / "catalog_sample"
CYRILLIC_C = "Сonsumption"


def sample_tables() -> dict:
    """Метаданные таблиц так, как их отдаст platform_meta.catalog_columns после миграции образца."""
    return {(t.brand, t.source_table): load.TableMeta(t.brand, t.source_table, t.pg_table,
                                                      tuple(load.Column(c.name, c.uidt) for c in t.columns))
            for t in gen.load_schema(SAMPLE / "schema")}


class MemoryStore:
    """Замена PgStore: таблицы в памяти, журнал загрузок — список."""

    def __init__(self):
        self.meta = sample_tables()
        self.data = {t.pg_table: {} for t in self.meta.values()}
        self.imports = []

    async def tables(self):
        return self.meta

    async def existing(self, table):
        return [copy.deepcopy(r) for r in self.data[table.pg_table].values()]

    async def apply(self, table, changes, prune, log):
        rows = self.data[table.pg_table]
        for r in changes.inserts + changes.updates:
            rows[r[table.pk]] = copy.deepcopy(r)
        if prune:
            for i in changes.missing:
                del rows[i]
        self.imports.append(log)


def run(store, directory, **kw):
    lines = []
    asyncio.run(load.run(store, Path(directory), out=lines.append, **kw))
    return lines


def table_line(lines, name):
    return next(line for line in lines if line.startswith(name + ":"))


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "import"
        shutil.copytree(SAMPLE / "import", self.dir)
        self.meta = sample_tables()
        self.catalog = self.meta[("demo", "catalog")]

    def tearDown(self):
        self.tmp.cleanup()

    def records(self, table="catalog"):
        return json.loads((self.dir / "demo" / f"{table}.records.json").read_text(encoding="utf-8"))

    def write(self, records, table="catalog", rows=None):
        (self.dir / "demo" / f"{table}.records.json").write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        if rows is not None:
            manifest = json.loads((self.dir / "manifest.json").read_text(encoding="utf-8"))
            for t in manifest["tables"]:
                if t["table"] == table:
                    t["rows"] = rows
            (self.dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # --- подготовка строк

    def test_prepare_types(self):
        rows = load.prepare_rows(self.records(), self.catalog).rows
        first = rows[0]
        self.assertEqual(first["Id"], 1)
        self.assertEqual(first["Price new"], Decimal("950000.50"))
        self.assertEqual(first[CYRILLIC_C], Decimal("6.5"))
        self.assertIs(first["Active"], True)
        self.assertEqual(first["CreatedAt"], datetime(2026, 1, 15, 10, tzinfo=timezone.utc))
        self.assertIsNone(rows[2]["CreatedAt"])  # поля нет в записи — пусто
        hist = self.meta[("demo", "site_config_history")]
        self.assertEqual(load.prepare_rows(self.records("site_config_history"), hist).rows[0]["Changed on"], date(2026, 1, 10))

    def test_photo_temporary_fields_removed(self):
        prepared = load.prepare_rows(self.records(), self.catalog)
        pics = json.loads(prepared.rows[0]["Pics"])
        self.assertEqual(pics, [{"mimetype": "image/jpeg", "path": "download/demo/pic1.jpg", "size": 1001, "title": "pic1.jpg"}])
        for row in prepared.rows:
            for field in ("Pics", "Title_pic"):
                self.assertNotRegex(row[field] or "", r"signedUrl|signedPath|thumbnails")
        self.assertEqual(prepared.photos_to_move, ["download/demo/pic1.jpg", "download/demo/pic2.jpg"])
        self.assertEqual(prepared.rows[2]["Pics"], "[]")

    def test_photo_base_url(self):
        prepared = load.prepare_rows(self.records(), self.catalog, "https://s3.example.invalid/bucket/")
        self.assertEqual(json.loads(prepared.rows[0]["Title_pic"])[0]["url"],
                         "https://s3.example.invalid/bucket/download/demo/pic2.jpg")
        self.assertEqual(prepared.photos_to_move, [])

    def test_bad_values_rejected(self):
        for field, value in (("Active", "yes"), ("Model id", "1"), ("Model", 5), ("CreatedAt", "2026-01-15 10:00:00"),
                             ("Price new", "abc"), ("Pics", {"path": "x"})):
            with self.subTest(field=field):
                recs = self.records()
                recs[0][field] = value
                with self.assertRaisesRegex(load.LoadError, re.escape(repr(field))):
                    load.prepare_rows(recs, self.catalog)
        recs = self.records()
        recs[1]["Id"] = 1
        with self.assertRaisesRegex(load.LoadError, "duplicate"):
            load.prepare_rows(recs, self.catalog)

    def test_check_fields(self):
        load.check_fields(self.records(), self.catalog)  # ncRecordHash — служебное поле API, не ошибка
        recs = self.records()
        recs[0]["Consumption"] = 1  # латинская C — лишнее поле
        with self.assertRaisesRegex(load.LoadError, r"extra \['Consumption'\]"):
            load.check_fields(recs, self.catalog)
        recs = self.records()
        for r in recs:
            del r["slug"]
        with self.assertRaisesRegex(load.LoadError, r"missing \['slug'\]"):
            load.check_fields(recs, self.catalog)

    # --- manifest

    def test_manifest_mismatch(self):
        self.write(self.records()[:2])
        with self.assertRaisesRegex(load.LoadError, "2 records, manifest says 3"):
            run(MemoryStore(), self.dir)
        (self.dir / "demo" / "colors.records.json").write_text("[]")
        with self.assertRaisesRegex(load.LoadError, "not in manifest demo/colors"):
            load.read_manifest(self.dir)
        (self.dir / "demo" / "colors.records.json").unlink()
        (self.dir / "demo" / "sites.records.json").unlink()
        with self.assertRaisesRegex(load.LoadError, "no file demo/sites"):
            load.read_manifest(self.dir)

    def test_unknown_table(self):
        store = MemoryStore()
        del store.meta[("demo", "run_log")]
        with self.assertRaisesRegex(load.LoadError, "migration not applied"):
            run(store, self.dir)

    def test_error_writes_nothing(self):
        recs = self.records("run_log")
        recs[0]["Extra"] = 1
        self.write(recs, "run_log")
        store = MemoryStore()
        with self.assertRaises(load.LoadError):
            run(store, self.dir)
        self.assertEqual(store.imports, [])
        self.assertTrue(all(not rows for rows in store.data.values()))

    # --- загрузка

    def test_load_then_repeat_is_noop(self):
        store = MemoryStore()
        lines = run(store, self.dir)
        self.assertEqual(table_line(lines, "demo_catalog"),
                         "demo_catalog: вставлено 3, обновлено 0, без изменений 0, нет в файле 0, удалено 0")
        self.assertEqual(lines[-1], "import done")
        self.assertEqual(len(store.imports), 8)
        self.assertEqual(sorted(store.data["demo_catalog"]), [1, 2, 5])
        sha = {i["pg_table"]: i["file_sha256"] for i in store.imports}
        self.assertRegex(sha["demo_catalog"], r"^[0-9a-f]{64}$")

        lines = run(store, self.dir)
        for t in self.meta.values():
            self.assertRegex(table_line(lines, t.pg_table), r": вставлено 0, обновлено 0, без изменений \d+,")
        self.assertEqual(sum(i["inserted"] + i["updated"] for i in store.imports[8:]), 0)

    def test_changed_row_updated(self):
        store = MemoryStore()
        run(store, self.dir)
        recs = self.records()
        recs[1]["Price new"] = 1100000
        self.write(recs)
        lines = run(store, self.dir)
        self.assertIn("demo_catalog: вставлено 0, обновлено 1, без изменений 2", table_line(lines, "demo_catalog"))
        self.assertEqual(store.data["demo_catalog"][2]["Price new"], Decimal("1100000"))

    def test_dry_run_writes_nothing(self):
        store = MemoryStore()
        lines = run(store, self.dir, dry_run=True)
        self.assertIn("demo_catalog: будет вставлено 3, будет обновлено 0", table_line(lines, "demo_catalog"))
        self.assertEqual(lines[-1], "dry run done, nothing written")
        self.assertEqual(store.imports, [])
        self.assertTrue(all(not rows for rows in store.data.values()))

    def test_missing_rows_listed_then_pruned(self):
        store = MemoryStore()
        run(store, self.dir)
        self.write(self.records()[:2], rows=2)
        lines = run(store, self.dir)
        self.assertIn("нет в файле 1, удалено 0", table_line(lines, "demo_catalog"))
        self.assertIn("  нет в файле, id: 5 (удалить — ключ --prune)", lines)
        self.assertIn(5, store.data["demo_catalog"])
        lines = run(store, self.dir, prune=True)
        self.assertIn("нет в файле 1, удалено 1", table_line(lines, "demo_catalog"))
        self.assertNotIn(5, store.data["demo_catalog"])
        self.assertEqual(store.imports[-8:][0]["deleted"], 1)

    def test_photo_report(self):
        lines = run(MemoryStore(), self.dir)
        self.assertIn("фото к переносу в S3: 2", lines)
        self.assertIn("photo: download/demo/pic1.jpg", lines)
        lines = run(MemoryStore(), self.dir, photo_base_url="https://s3.example.invalid/b")
        self.assertFalse(any(line.startswith("фото к переносу") for line in lines))

    def test_missing_vars(self):
        self.assertEqual(load.missing_vars({}), list(load.REQUIRED_VARS))


class SampleIsSyntheticTest(unittest.TestCase):
    """Образец выдуман: в нём нет ничего похожего на токен, телефон, почту или реальный адрес."""

    def test_no_secrets_or_personal_data(self):
        for path in sorted(SAMPLE.rglob("*.json")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=str(path.relative_to(SAMPLE))):
                self.assertNotRegex(text, r"(?i)xc-token|xc-auth|password|secret|bearer")
                no_dates = re.sub(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2}\+00:00)?", "", text)
                self.assertNotRegex(no_dates, r"\+?\d[\d\s()-]{9,}\d")  # телефоны и длинные номера
                self.assertNotRegex(text, r"[\w.+-]+@[\w-]+\.[\w.]+")  # почта
                self.assertNotRegex(text, r"\b[0-9a-fA-F]{32,}\b|[A-Za-z0-9+/_-]{40,}")  # ключи
                for host in re.findall(r"https?://([^/\"]+)", text):
                    self.assertTrue(host.endswith(".invalid"), host)

    def test_sample_has_required_cases(self):
        text = (SAMPLE / "schema" / "demo" / "catalog.columns.json").read_text(encoding="utf-8")
        for needle in (f'"{CYRILLIC_C}"', '"Model id"', '"SingleSelect"', '"Attachment"', '"Active"', '"Active_1"'):
            self.assertIn(needle, text)
        self.assertIn("signedUrl", (SAMPLE / "import" / "demo" / "catalog.records.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
