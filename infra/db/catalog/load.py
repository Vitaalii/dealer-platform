"""Загрузка разовой копии каталогов марок из выгрузки CE (ARCHITECTURE.md, 2.4 п. 1–2; задача PDL-32).

Папка выгрузки:
    manifest.json                   — {"exported_at": "...", "tables": [{"brand", "table", "rows"}, ...]}
    <марка>/<таблица>.records.json  — записи как их отдаёт API NocoDB: массив или {"list": [...]}

Порядок: сверка manifest с файлами, сверка полей с колонками таблицы (platform_meta.catalog_columns),
затем по каждой таблице в одной транзакции — upsert по исходному id, перестановка счётчика и строка
журнала в platform_meta.catalog_imports. Повтор того же файла — 0 изменений. Строки, которых нет в
файле, по умолчанию только перечисляются; --prune их удаляет. --dry-run ничего не пишет.
Фото — только ссылки (DEC:20): из вложений убираются временные поля signedUrl, signedPath, thumbnails;
с --photo-base-url поле url = адрес S3 + path.

Запуск — в сервисе migrate (infra/README.md, раздел «Каталог»):
    python /app/db/catalog/load.py --dir /import [--dry-run] [--prune] [--photo-base-url URL]
Ключи только из окружения: PLATFORM_DB_HOST, PLATFORM_DB_NAME, MIGRATE_DB_USER, MIGRATE_DB_PASSWORD.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

REQUIRED_VARS = ("PLATFORM_DB_HOST", "PLATFORM_DB_NAME", "MIGRATE_DB_USER", "MIGRATE_DB_PASSWORD")
LOCK_ID = 5_5_0001  # постоянный номер advisory-блокировки загрузки каталогов

TEXT_TYPES = {"SingleLineText", "LongText", "Email", "URL", "PhoneNumber", "SingleSelect", "MultiSelect"}
DECIMAL_TYPES = {"Decimal", "Currency", "Percent"}
DATETIME_TYPES = {"DateTime", "CreatedTime", "LastModifiedTime"}
# Служебное поле ответа API NocoDB, не колонка таблицы (его же убирает бот, bot/main.py:2772).
API_SERVICE_FIELDS = {"ncRecordHash"}
# Что остаётся во вложении; signedUrl, signedPath, thumbnails и прочее — временное, убирается.
ATTACHMENT_KEEP = ("path", "title", "mimetype", "size")


class LoadError(ValueError):
    pass


@dataclass(frozen=True)
class Column:
    name: str
    uidt: str


@dataclass(frozen=True)
class TableMeta:
    brand: str
    source_table: str
    pg_table: str
    columns: tuple[Column, ...]

    @property
    def pk(self) -> str:
        return next(c.name for c in self.columns if c.uidt == "ID")


@dataclass(frozen=True)
class Entry:
    brand: str
    table: str
    rows: int
    path: Path


@dataclass
class Changes:
    inserts: list[dict] = field(default_factory=list)
    updates: list[dict] = field(default_factory=list)
    unchanged: int = 0
    missing: list = field(default_factory=list)  # id строк базы, которых нет в файле


@dataclass
class Prepared:
    rows: list[dict]
    photos_to_move: list[str]


# --- Файлы выгрузки -------------------------------------------------------------------------------

def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LoadError(f"{path.name}: bad JSON: {exc}") from exc


def read_manifest(directory: Path) -> list[Entry]:
    """manifest.json и файлы записей должны совпадать один к одному, число строк — с manifest."""
    path = directory / "manifest.json"
    if not path.is_file():
        raise LoadError("manifest.json not found")
    data = read_json(path)
    raw = data.get("tables") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise LoadError("manifest.json: no tables")
    entries: list[Entry] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("brand"), str) or not isinstance(item.get("table"), str) \
                or not isinstance(item.get("rows"), int) or isinstance(item.get("rows"), bool):
            raise LoadError(f"manifest.json: bad entry {item!r}")
        key = (item["brand"], item["table"])
        if key in seen:
            raise LoadError(f"manifest.json: duplicate {key[0]}/{key[1]}")
        seen.add(key)
        entries.append(Entry(item["brand"], item["table"], item["rows"],
                             directory / item["brand"] / f"{item['table']}.records.json"))
    on_disk = {(p.parent.name, p.name[: -len(".records.json")]) for p in directory.glob("*/*.records.json")}
    absent = sorted(seen - on_disk)
    extra = sorted(on_disk - seen)
    if absent or extra:
        raise LoadError("manifest.json and files differ: "
                           + "; ".join([f"no file {b}/{t}.records.json" for b, t in absent]
                                       + [f"not in manifest {b}/{t}.records.json" for b, t in extra]))
    return entries


def read_records(entry: Entry) -> tuple[list[dict], str]:
    """Записи файла и sha256 его байтов. Число записей должно совпасть с manifest."""
    raw = entry.path.read_bytes()
    data = read_json(entry.path)
    records = data.get("list") if isinstance(data, dict) else data
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise LoadError(f"{entry.brand}/{entry.table}: records must be a list of objects")
    if len(records) != entry.rows:
        raise LoadError(f"{entry.brand}/{entry.table}: {len(records)} records, manifest says {entry.rows}")
    return records, hashlib.sha256(raw).hexdigest()


def check_fields(records: list[dict], table: TableMeta) -> None:
    """Лишнее поле в любой записи или колонка, которой нет ни в одной записи, — отказ с перечнем.

    Колонка, которой нет только в части записей, считается пустой (null).
    """
    expected = {c.name for c in table.columns}
    present: set[str] = set()
    for r in records:
        present.update(k for k in r if k not in API_SERVICE_FIELDS)
    extra = sorted(present - expected)
    absent = sorted(expected - present) if records else []
    if extra or absent:
        raise LoadError(f"{table.brand}/{table.source_table}: fields differ from table {table.pg_table}: "
                           f"extra {extra}, missing {absent}")


# --- Подготовка строк -----------------------------------------------------------------------------

def clean_attachments(value, photo_base_url: str | None, photos: list[str]) -> str | None:
    """Вложение -> JSON-текст только со ссылками; без --photo-base-url путь попадает в список переноса."""
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not all(isinstance(a, dict) for a in value):
        raise ValueError("attachment must be a list of objects")
    cleaned = []
    for item in value:
        out = {k: item[k] for k in ATTACHMENT_KEEP if k in item}
        path = item.get("path")
        if path and photo_base_url:
            out["url"] = photo_base_url.rstrip("/") + "/" + str(path).lstrip("/")
        elif path:
            photos.append(str(path))
        elif item.get("url"):
            # Вложение без path: другой ссылки нет — сохраняем исходную и отмечаем к переносу.
            out["url"] = item["url"]
            photos.append(str(item["url"]))
        cleaned.append(out)
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def convert(value, uidt: str, photo_base_url: str | None, photos: list[str]):
    """Значение из JSON -> значение Python того же типа, что вернёт asyncpg для колонки."""
    if value is None:
        return None
    if uidt in TEXT_TYPES:
        if not isinstance(value, str):
            raise ValueError("expected text")
        return value
    if uidt in ("ID", "Number"):
        if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, float) and value.is_integer())):
            raise ValueError("expected integer")
        return int(value)
    if uidt in DECIMAL_TYPES:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("expected number")
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("expected number") from exc
    if uidt == "Checkbox":
        if not isinstance(value, bool):
            raise ValueError("expected true/false")
        return value
    if uidt == "Date":
        return date.fromisoformat(str(value))
    if uidt in DATETIME_TYPES:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            raise ValueError("datetime without time zone")
        return parsed
    if uidt == "Attachment":
        return clean_attachments(value, photo_base_url, photos)
    raise ValueError(f"unsupported type {uidt}")


def prepare_rows(records: list[dict], table: TableMeta, photo_base_url: str | None = None) -> Prepared:
    """Строки для базы: все колонки таблицы, значения нужных типов, вложения без временных полей."""
    rows: list[dict] = []
    photos: list[str] = []
    ids = set()
    pk = table.pk
    for n, record in enumerate(records, start=1):
        row = {}
        for c in table.columns:
            try:
                row[c.name] = convert(record.get(c.name), c.uidt, photo_base_url, photos)
            except (ValueError, TypeError) as exc:
                raise LoadError(f"{table.brand}/{table.source_table}: record {n}, field {c.name!r}: {exc}") from exc
        if row[pk] is None:
            raise LoadError(f"{table.brand}/{table.source_table}: record {n} has no {pk!r}")
        if row[pk] in ids:
            raise LoadError(f"{table.brand}/{table.source_table}: duplicate {pk!r} = {row[pk]}")
        ids.add(row[pk])
        rows.append(row)
    return Prepared(rows, photos)


def plan_changes(rows: list[dict], existing: list[dict], pk: str) -> Changes:
    """Что вставить, что обновить, сколько без изменений и каких id нет в файле."""
    by_id = {r[pk]: r for r in existing}
    changes = Changes()
    for row in rows:
        old = by_id.pop(row[pk], None)
        if old is None:
            changes.inserts.append(row)
        elif any(old.get(k) != v for k, v in row.items()):
            changes.updates.append(row)
        else:
            changes.unchanged += 1
    changes.missing = sorted(by_id)
    return changes


def summary(pg_table: str, changes: Changes, dry_run: bool, prune: bool) -> str:
    deleted = len(changes.missing) if prune else 0
    verb = "будет " if dry_run else ""
    return (f"{pg_table}: {verb}вставлено {len(changes.inserts)}, {verb}обновлено {len(changes.updates)}, "
            f"без изменений {changes.unchanged}, нет в файле {len(changes.missing)}, {verb}удалено {deleted}")


# --- База -----------------------------------------------------------------------------------------

def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class PgStore:
    """Работа с базой platform под platform_admin (asyncpg есть в образе платформы; тестам не нужен)."""

    def __init__(self, conn):
        self.conn = conn

    async def tables(self) -> dict[tuple[str, str], TableMeta]:
        rows = await self.conn.fetch(
            "SELECT brand, source_table, pg_table, column_name, uidt FROM platform_meta.catalog_columns"
            " ORDER BY pg_table, position")
        grouped: dict[tuple[str, str], list] = {}
        names: dict[tuple[str, str], str] = {}
        for r in rows:
            key = (r["brand"], r["source_table"])
            grouped.setdefault(key, []).append(Column(r["column_name"], r["uidt"]))
            names[key] = r["pg_table"]
        return {k: TableMeta(k[0], k[1], names[k], tuple(cols)) for k, cols in grouped.items()}

    async def existing(self, table: TableMeta) -> list[dict]:
        cols = ", ".join(quote_ident(c.name) for c in table.columns)
        rows = await self.conn.fetch(f"SELECT {cols} FROM catalog.{quote_ident(table.pg_table)}")
        return [dict(r) for r in rows]

    async def apply(self, table: TableMeta, changes: Changes, prune: bool, log: dict) -> None:
        target = f"catalog.{quote_ident(table.pg_table)}"
        names = [c.name for c in table.columns]
        pk = table.pk
        async with self.conn.transaction():
            if changes.inserts:
                cols = ", ".join(quote_ident(n) for n in names)
                params = ", ".join(f"${i}" for i in range(1, len(names) + 1))
                await self.conn.executemany(f"INSERT INTO {target} ({cols}) VALUES ({params})",
                                            [[row[n] for n in names] for row in changes.inserts])
            if changes.updates:
                others = [n for n in names if n != pk]
                sets = ", ".join(f"{quote_ident(n)} = ${i}" for i, n in enumerate(others, start=2))
                await self.conn.executemany(f"UPDATE {target} SET {sets} WHERE {quote_ident(pk)} = $1",
                                            [[row[pk]] + [row[n] for n in others] for row in changes.updates])
            if prune and changes.missing:
                await self.conn.execute(f"DELETE FROM {target} WHERE {quote_ident(pk)} = ANY($1::bigint[])",
                                        changes.missing)
            # Счётчик id — на максимум: новые строки из NocoDB не должны столкнуться с перенесёнными.
            await self.conn.execute(
                f"SELECT setval(pg_get_serial_sequence($1, $2),"
                f" COALESCE((SELECT max({quote_ident(pk)}) FROM {target}), 0) + 1, false)",
                target, pk)
            await self.conn.execute(
                "INSERT INTO platform_meta.catalog_imports (brand, source_table, pg_table, file_sha256,"
                " inserted, updated, unchanged, deleted, missing) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                log["brand"], log["source_table"], log["pg_table"], log["file_sha256"], log["inserted"],
                log["updated"], log["unchanged"], log["deleted"], log["missing"])


# --- Порядок загрузки -----------------------------------------------------------------------------

async def run(store, directory: Path, dry_run: bool = False, prune: bool = False,
              photo_base_url: str | None = None, out=print) -> int:
    """Сначала сверка всех файлов, потом запись по таблицам. Ошибка сверки — ничего не записано."""
    entries = read_manifest(directory)
    known = await store.tables()
    unknown = [f"{e.brand}/{e.table}" for e in entries if (e.brand, e.table) not in known]
    if unknown:
        raise LoadError("no such tables in platform_meta.catalog_columns (migration not applied?): "
                           + ", ".join(unknown))
    prepared = []
    for e in entries:
        table = known[(e.brand, e.table)]
        records, sha = read_records(e)
        check_fields(records, table)
        prepared.append((e, table, prepare_rows(records, table, photo_base_url), sha))

    photos: list[str] = []
    for e, table, p, sha in prepared:
        changes = plan_changes(p.rows, await store.existing(table), table.pk)
        if not dry_run:
            await store.apply(table, changes, prune, {
                "brand": e.brand, "source_table": e.table, "pg_table": table.pg_table, "file_sha256": sha,
                "inserted": len(changes.inserts), "updated": len(changes.updates), "unchanged": changes.unchanged,
                "deleted": len(changes.missing) if prune else 0, "missing": len(changes.missing)})
        out(summary(table.pg_table, changes, dry_run, prune))
        if changes.missing and not prune:
            out(f"  нет в файле, id: {', '.join(str(i) for i in changes.missing)} (удалить — ключ --prune)")
        photos.extend(p.photos_to_move)
    if photos:
        out(f"фото к переносу в S3: {len(photos)}")
        for path in photos:
            out(f"photo: {path}")
    out("dry run done, nothing written" if dry_run else "import done")
    return 0


def missing_vars(environ) -> list[str]:
    return [name for name in REQUIRED_VARS if not environ.get(name, "").strip()]


async def main_async(args, environ) -> int:
    import asyncpg

    conn = await asyncpg.connect(
        host=environ["PLATFORM_DB_HOST"],
        port=int(environ.get("PLATFORM_DB_PORT") or 5432),
        database=environ["PLATFORM_DB_NAME"],
        user=environ["MIGRATE_DB_USER"],
        password=environ["MIGRATE_DB_PASSWORD"],
        timeout=10,
    )
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
        return await run(PgStore(conn), args.dir, args.dry_run, args.prune, args.photo_base_url,
                         out=lambda s: print(s, flush=True))
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load a one-off catalog copy exported from NocoDB")
    parser.add_argument("--dir", type=Path, required=True, help="folder with manifest.json")
    parser.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    parser.add_argument("--prune", action="store_true", help="delete rows that are not in the file")
    parser.add_argument("--photo-base-url", help="S3 base URL; attachment url = base + path")
    args = parser.parse_args(argv)
    missing = missing_vars(os.environ)
    if missing:
        print("CONFIG ERROR: missing " + ", ".join(missing), file=sys.stderr, flush=True)
        return 1
    try:
        return asyncio.run(main_async(args, os.environ))
    except LoadError as exc:
        print(f"IMPORT ERROR: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
