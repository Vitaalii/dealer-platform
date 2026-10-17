"""Генератор миграции каталогов марок (docs/architecture/ARCHITECTURE.md, 2.2–2.4; задача PDL-32).

Читает описания колонок от CE — schema/<марка>/<таблица>.columns.json — и пишет обычную миграцию
infra/db/migrations/0002_catalog_brands.py: по таблице на каждую пару марка + таблица в схеме catalog
и служебные таблицы platform_meta.catalog_columns и platform_meta.catalog_imports.

Имена колонок — побайтно как поля NocoDB (в кавычках, с пробелами и кириллицей). SingleSelect — text
без CHECK: новый вариант от грабера не должен давать ошибку; варианты и исходные типы сохраняются в
platform_meta.catalog_columns. Вычисляемые и неизвестные типы — ошибка, без догадок.
Прав миграция не выдаёт: их дают ALTER DEFAULT PRIVILEGES из 0001.

Решения владельца и Координатора по файлам CE (PDL-32, 17.10):
- системные поля старой NocoDB (CreatedBy, LastModifiedBy, Order) и поля-связи со складом (Links,
  LinkToAnotherRecord) не переносятся: колонки не создаются, загрузчик пропускает их в записях;
- таблица без колонки типа ID получает ключ из schema/keys.json ("<марка>/<таблица>": "<поле>"):
  поле — text NOT NULL UNIQUE, загрузчик сверяет строки по нему, колонку "Id" база создаёт сама.

Запуск из корня репозитория:
    python3 infra/db/catalog/gen_migration.py           # записать миграцию
    python3 infra/db/catalog/gen_migration.py --check   # сверить: файл совпадает с описаниями
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_DIR = HERE / "schema"
KEYS_FILE = "keys.json"
SYNTHETIC_ID = "Id"
OUT_FILE = HERE.parent / "migrations" / "0002_catalog_brands.py"

BRAND_RE = re.compile(r"^[a-z][a-z0-9]*$")
TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PG_NAME_MAX_BYTES = 63  # длиннее PostgreSQL молча обрезает имя (NAMEDATALEN - 1)

# Тип поля NocoDB (uidt) -> тип колонки PostgreSQL.
TYPE_MAP = {
    "ID": "bigserial",
    "SingleLineText": "text",
    "LongText": "text",
    "Email": "text",
    "URL": "text",
    "PhoneNumber": "text",
    "SingleSelect": "text",
    "MultiSelect": "text",
    "Number": "bigint",
    "Decimal": "numeric",
    "Currency": "numeric",
    "Percent": "numeric",
    "Checkbox": "boolean",
    "Date": "date",
    "DateTime": "timestamptz",
    "CreatedTime": "timestamptz",
    "LastModifiedTime": "timestamptz",
    "Attachment": "text",  # JSON-текст: только ссылки на фото (DEC:20)
}
# Не переносятся (решения 1 и 2, PDL-32): колонки нет, значение в записи пропускается.
SKIPPED = {"CreatedBy", "LastModifiedBy", "Order", "Links", "LinkToAnotherRecord"}
COMPUTED = {"Lookup", "Rollup", "Formula", "Count", "Barcode", "QrCode", "Button", "ForeignKey"}
SELECT_TYPES = {"SingleSelect", "MultiSelect"}

HEADER = '''"""Каталоги марок в схеме catalog и служебные таблицы загрузки (задача PDL-32).

СГЕНЕРИРОВАНО infra/db/catalog/gen_migration.py из infra/db/catalog/schema/ — руками не править:
поменять описания колонок и запустить генератор заново.
Прав миграция не выдаёт: таблицы catalog получают их из ALTER DEFAULT PRIVILEGES миграции 0001,
служебные таблицы platform_meta доступны только platform_admin.
"""

'''

META_SQL = """CREATE TABLE platform_meta.catalog_columns (
    brand text NOT NULL,
    source_table text NOT NULL,
    pg_table text NOT NULL,
    position integer NOT NULL,
    column_name text NOT NULL,
    uidt text NOT NULL,
    pg_type text,
    options jsonb,
    role text CHECK (role IN ('key', 'generated', 'skipped')),
    PRIMARY KEY (pg_table, column_name)
);
COMMENT ON COLUMN platform_meta.catalog_columns.role IS
    'key: поле сверки строк при загрузке; generated: колонку создала база, в выгрузке её нет; '
    'skipped: поле NocoDB не переносится, колонки нет';

CREATE TABLE platform_meta.catalog_imports (
    id bigserial PRIMARY KEY,
    imported_at timestamptz NOT NULL DEFAULT now(),
    brand text NOT NULL,
    source_table text NOT NULL,
    pg_table text NOT NULL,
    file_sha256 text NOT NULL,
    inserted integer NOT NULL,
    updated integer NOT NULL,
    unchanged integer NOT NULL,
    deleted integer NOT NULL,
    missing integer NOT NULL
);
"""


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class Column:
    name: str
    uidt: str
    pg_type: str | None
    options: tuple[str, ...] | None
    role: str | None = None  # key / generated / skipped


@dataclass(frozen=True)
class Table:
    brand: str
    source_table: str
    pg_table: str
    columns: tuple[Column, ...]

    @property
    def key(self) -> str:
        return next(c.name for c in self.columns if c.role == "key")

    @property
    def created(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.role != "skipped")


def pg_table_name(brand: str, source_table: str) -> str:
    """Единственное место правила имени: таблица марки в PostgreSQL — <марка>_<таблица>.

    Риск (handoff PDL-32): на стенде 5.6 может выясниться, что граберу нужны исходные имена таблиц.
    """
    return f"{brand}_{source_table}"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def check_name(name, where: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise SchemaError(f"{where}: empty column name")
    if name != name.strip():
        raise SchemaError(f"{where}: column name {name!r} has leading or trailing spaces")
    if any(ch in name for ch in '"\\\x00'):
        raise SchemaError(f"{where}: column name {name!r} has \", \\ or NUL")
    if len(name.encode("utf-8")) > PG_NAME_MAX_BYTES:
        raise SchemaError(f"{where}: column name {name!r} is longer than {PG_NAME_MAX_BYTES} bytes")
    return name


def parse_options(raw: dict, where: str) -> tuple[str, ...]:
    """Варианты SingleSelect: "options": ["a", ...] или как в API NocoDB — colOptions.options[].title."""
    options = raw.get("options")
    if options is None:
        options = [o.get("title") if isinstance(o, dict) else o
                   for o in ((raw.get("colOptions") or {}).get("options") or [])]
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise SchemaError(f"{where}: options must be a list of strings")
    return tuple(options)


def parse_columns(data, where: str, key: str | None = None) -> tuple[Column, ...]:
    """Файл .columns.json: список полей или объект с ключом columns (как в мета-API NocoDB).

    key — поле сверки из keys.json для таблицы без колонки типа ID.
    """
    raw_columns = data.get("columns") if isinstance(data, dict) else data
    if not isinstance(raw_columns, list) or not raw_columns:
        raise SchemaError(f"{where}: no columns")
    columns: list[Column] = []
    seen: set[str] = set()
    for raw in raw_columns:
        if not isinstance(raw, dict):
            raise SchemaError(f"{where}: column must be an object")
        name = check_name(raw.get("title"), where)
        if name in seen:
            raise SchemaError(f"{where}: duplicate column {name!r}")
        seen.add(name)
        uidt = raw.get("uidt")
        if uidt in SKIPPED:
            columns.append(Column(name, uidt, None, None, "skipped"))
            continue
        if uidt in COMPUTED:
            raise SchemaError(f"{where}: column {name!r} is computed ({uidt}), it cannot be copied")
        if uidt not in TYPE_MAP:
            raise SchemaError(f"{where}: column {name!r} has unknown type {uidt!r}")
        options = parse_options(raw, f"{where}: {name!r}") if uidt in SELECT_TYPES else None
        columns.append(Column(name, uidt, TYPE_MAP[uidt], options))
    ids = [c.name for c in columns if c.uidt == "ID"]
    if len(ids) > 1:
        raise SchemaError(f"{where}: need one ID column, found {len(ids)}")
    if ids and key:
        raise SchemaError(f"{where}: has ID column, remove it from {KEYS_FILE}")
    if ids:
        return tuple(Column(c.name, c.uidt, c.pg_type, c.options, "key" if c.uidt == "ID" else c.role)
                     for c in columns)
    if not key:
        raise SchemaError(f"{where}: no ID column and no key in {KEYS_FILE}")
    if SYNTHETIC_ID in seen:
        raise SchemaError(f"{where}: column {SYNTHETIC_ID!r} exists but is not ID")
    found = [c for c in columns if c.name == key]
    if not found or found[0].uidt not in ("SingleLineText", "Number"):
        raise SchemaError(f"{where}: key {key!r} must be a SingleLineText or Number column")
    keyed = [Column(c.name, c.uidt, c.pg_type, c.options, "key") if c.name == key else c for c in columns]
    return (Column(SYNTHETIC_ID, "ID", TYPE_MAP["ID"], None, "generated"),) + tuple(keyed)


def load_keys(directory: Path) -> dict[str, str]:
    path = directory / KEYS_FILE
    if not path.exists():
        return {}
    keys = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(keys, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in keys.items()):
        raise SchemaError(f"{KEYS_FILE}: must map \"<brand>/<table>\" to a column name")
    return keys


def load_schema(directory: Path = SCHEMA_DIR) -> list[Table]:
    """Все таблицы из schema/<марка>/<таблица>.columns.json, по порядку марки и таблицы."""
    tables: list[Table] = []
    keys = load_keys(directory)
    for path in sorted(directory.glob("*/*.columns.json")):
        brand = path.parent.name
        source_table = path.name[: -len(".columns.json")]
        where = f"{brand}/{path.name}"
        if not BRAND_RE.match(brand):
            raise SchemaError(f"{where}: brand folder must match {BRAND_RE.pattern}")
        if not TABLE_RE.match(source_table):
            raise SchemaError(f"{where}: table name must match {TABLE_RE.pattern}")
        data = json.loads(path.read_text(encoding="utf-8"))
        pg_table = pg_table_name(brand, source_table)
        if len(pg_table.encode("utf-8")) > PG_NAME_MAX_BYTES:
            raise SchemaError(f"{where}: table name {pg_table!r} is too long")
        tables.append(Table(brand, source_table, pg_table,
                            parse_columns(data, where, keys.pop(f"{brand}/{source_table}", None))))
    if not tables:
        raise SchemaError(f"no *.columns.json in {directory}")
    if keys:
        raise SchemaError(f"{KEYS_FILE}: no such tables: {', '.join(sorted(keys))}")
    return tables


def column_ddl(column: Column) -> str:
    if column.uidt == "ID":
        suffix = " PRIMARY KEY"
    elif column.role == "key":
        suffix = " NOT NULL UNIQUE"
    else:
        suffix = ""
    return f"    {quote_ident(column.name)} {column.pg_type}{suffix}"


def table_sql(table: Table) -> str:
    body = ",\n".join(column_ddl(c) for c in table.created)
    return f"CREATE TABLE catalog.{quote_ident(table.pg_table)} (\n{body}\n);\n"


def meta_rows_sql(table: Table) -> str:
    rows = []
    for position, c in enumerate(table.columns, start=1):
        options = "NULL" if c.options is None else quote_literal(json.dumps(list(c.options), ensure_ascii=False)) + "::jsonb"
        pg_type = "NULL" if c.pg_type is None else quote_literal(c.pg_type)
        role = "NULL" if c.role is None else quote_literal(c.role)
        rows.append(f"    ({quote_literal(table.brand)}, {quote_literal(table.source_table)}, "
                    f"{quote_literal(table.pg_table)}, {position}, {quote_literal(c.name)}, "
                    f"{quote_literal(c.uidt)}, {pg_type}, {options}, {role})")
    return ("INSERT INTO platform_meta.catalog_columns\n"
            "    (brand, source_table, pg_table, position, column_name, uidt, pg_type, options, role) VALUES\n"
            + ",\n".join(rows) + ";\n")


def render_sql(tables: list[Table]) -> str:
    parts = [META_SQL]
    for table in tables:
        parts.append(f"-- {table.brand} / {table.source_table}\n" + table_sql(table) + meta_rows_sql(table))
    return "\n".join(parts)


def render_module(sql: str) -> str:
    escaped = sql.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    text = HEADER + 'SQL = """\n' + escaped + '"""\n'
    namespace: dict = {}
    exec(compile(text, "0002_catalog_brands.py", "exec"), namespace)
    if namespace["SQL"] != "\n" + sql:
        raise SchemaError("rendered module does not reproduce SQL")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate catalog migration from *.columns.json")
    parser.add_argument("--schema", type=Path, default=SCHEMA_DIR)
    parser.add_argument("--out", type=Path, default=OUT_FILE)
    parser.add_argument("--check", action="store_true", help="only compare with the existing file")
    args = parser.parse_args(argv)
    try:
        tables = load_schema(args.schema)
        text = render_module(render_sql(tables))
    except (SchemaError, json.JSONDecodeError) as exc:
        print(f"SCHEMA ERROR: {exc}", file=sys.stderr)
        return 1
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if current != text:
            print(f"OUTDATED: {args.out.name} differs from schema files", file=sys.stderr)
            return 1
        print(f"up to date: {args.out.name}, {len(tables)} tables")
        return 0
    args.out.write_text(text, encoding="utf-8")
    print(f"written: {args.out.name}, {len(tables)} tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
