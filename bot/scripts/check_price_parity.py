#!/usr/bin/env python3
"""Сверка цен бота с эталоном n8n (план 2026-09-30-001, U4 / R48).

Бот считает credit_price и combo_price по каталогу NocoDB (cabinet_service.
compute_prices, R17); агент CE n8n отдаёт эталон — те же цены из живых нод
Car Search и Combo Calculator. Любое расхождение или позиция без эталона —
код выхода 1. Скрипт только читает (NocoDB, файл эталона), в БД не пишет.

Формат эталона (JSON):

    {
      "dealer_code": "02",
      "generated_at": "2026-10-01T12:00:00+03:00",
      "source": "n8n: Car Search + Combo Calculator",
      "positions": [
        {"model_id": "15", "complectation_id": "12",
         "credit_price": 2750000, "combo_price": 2500000},
        ...
      ]
    }

model_id / complectation_id — колонки «Model id» / «Complectation id» каталога
(строками); цены — целые рубли; combo_price может быть null, если нода его
не считает для позиции. Позиции каталога без цены (нет «Price old») в сверку
не входят. Строки без «Model id» пропускаются (их нет и в эталоне n8n). Если пара
(Model id, Complectation id) повторена, сверяется первая строка в порядке NocoDB —
её же берёт бот (get_catalog_row_by_pair, limit 1) и n8n Car Search; повторы
перечисляются в отчёте, но не валят сверку.

Примеры:
    venv/bin/python scripts/check_price_parity.py --dealer 02 --reference ref_02.json
    # до правки ноды Combo Calculator — только кредитная цена:
    venv/bin/python scripts/check_price_parity.py --dealer 02 --reference ref_02.json --only credit
    # 3.4.1 — только позиции, активные у дилера (колонка Dealers.Catalog_Active_Field,
    # у 02 — Active_1); позиции эталона вне активных — в отчёте «эталон без позиции», не валят сверку:
    venv/bin/python scripts/check_price_parity.py --dealer 02 --reference ref_02.json --active-only
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from cabinet_service import _flag_on, compute_prices, parse_catalog_active_field  # noqa: E402

DEALERS_TABLE = "muruk10ipof33m5"
CATALOG_COLUMN = "Sourse table id"   # имя колонки Dealers в NocoDB (как есть)
PAGE = 25            # бережное чтение NocoDB (OOM 2026-10-06): страницы ≤ 25 и пауза
PAGE_PAUSE = 0.5
FIELDS = ("credit_price", "combo_price")


def _key(model_id, compl_id) -> tuple:
    return (str(model_id or "").strip(), str(compl_id or "").strip())


def compare(rows: list, reference: dict, only: str = "both") -> dict:
    """Сверка строк каталога с эталоном. Возвращает отчёт:
    {"checked": N, "mismatches": [...], "missing_reference": [...],
     "extra_reference": [...], "skipped_unpriced": [...],
     "skipped_no_model_id": N, "duplicates": [...]}."""
    fields = FIELDS if only == "both" else (f"{only}_price",)
    ref = {}
    for pos in reference.get("positions") or []:
        ref[_key(pos.get("model_id"), pos.get("complectation_id"))] = pos
    report = {"checked": 0, "mismatches": [], "missing_reference": [],
              "extra_reference": [], "skipped_unpriced": [],
              "skipped_no_model_id": 0, "duplicates": []}
    seen = set()
    first_rows = set()
    for row in rows:
        key = _key(row.get("Model id"), row.get("Complectation id"))
        if not key[0]:
            report["skipped_no_model_id"] += 1
            continue
        if key in first_rows:  # повтор пары: бот и n8n берут первую строку
            report["duplicates"].append({"position": key, "id": row.get("Id"),
                                         "complectation": row.get("Complectation")})
            continue
        first_rows.add(key)
        prices = compute_prices(row)
        if prices is None:
            report["skipped_unpriced"].append(key)
            continue
        seen.add(key)
        expected = ref.get(key)
        if expected is None:
            report["missing_reference"].append(key)
            continue
        report["checked"] += 1
        for field in fields:
            if expected.get(field) != prices[field]:
                report["mismatches"].append({
                    "position": key, "field": field,
                    "bot": prices[field], "n8n": expected.get(field),
                    "model": row.get("Model"), "complectation": row.get("Complectation")})
    report["extra_reference"] = sorted(k for k in ref if k not in seen)
    return report


def is_ok(report: dict) -> bool:
    return not (report["mismatches"] or report["missing_reference"])


async def _fetch_all(session, base, headers, table_id) -> list:
    rows, offset = [], 0
    while True:
        async with session.get(f"{base}/api/v2/tables/{table_id}/records", headers=headers,
                               params={"limit": PAGE, "offset": offset}, ssl=False) as resp:
            if resp.status != 200:
                raise RuntimeError(f"NocoDB HTTP {resp.status} (table {table_id})")
            data = await resp.json()
        page = data.get("list") or []
        rows += [r for r in page if isinstance(r, dict)]
        if (data.get("pageInfo") or {}).get("isLastPage", True) or not page:
            return rows
        offset += len(page)
        await asyncio.sleep(PAGE_PAUSE)


def active_rows(rows: list, active_field: str) -> list:
    """Позиции, активные у дилера по его колонке (3.4.1, --active-only)."""
    return [r for r in rows if isinstance(r, dict) and _flag_on(r.get(active_field))]


async def _load_catalog(dealer_code: str):
    import aiohttp
    base = os.environ["NOCODB_API_URL"].rstrip("/")
    headers = {"xc-token": os.environ["NOCODB_TOKEN"]}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        dealers = await _fetch_all(session, base, headers, DEALERS_TABLE)
        dealer = next((d for d in dealers if str(d.get("Dealer_Code")) == dealer_code), None)
        if dealer is None or not dealer.get(CATALOG_COLUMN):
            raise SystemExit(f"Дилер {dealer_code}: нет в Dealers или не задан каталог")
        active_field, bad = parse_catalog_active_field(
            next((v for k, v in dealer.items()
                  if k.lower().replace("_", "").replace(" ", "") == "catalogactivefield"), None))
        if bad is not None:
            print(f"ВНИМАНИЕ: Catalog_Active_Field дилера {dealer_code} = {bad!r} — взято {active_field!r}")
        return await _fetch_all(session, base, headers, dealer[CATALOG_COLUMN]), active_field


def _print_report(report: dict, only: str) -> None:
    print(f"Сверено позиций: {report['checked']} (поля: {only})")
    for m in report["mismatches"]:
        print(f"  РАСХОЖДЕНИЕ {m['position']} {m['model']} {m['complectation']}: "
              f"{m['field']} бот={m['bot']} n8n={m['n8n']}")
    for k in report["missing_reference"]:
        print(f"  НЕТ ЭТАЛОНА для позиции {k}")
    for k in report["extra_reference"]:
        print(f"  (эталон без позиции в каталоге: {k})")
    for d in report["duplicates"]:
        print(f"  (повтор пары {d['position']}: строка Id {d['id']} {d['complectation']} "
              f"не сверяется — берётся первая)")
    if report["skipped_no_model_id"]:
        print(f"  Без Model id, пропущено: {report['skipped_no_model_id']}")
    if report["skipped_unpriced"]:
        print(f"  Без цены в каталоге, пропущено: {len(report['skipped_unpriced'])}")
    print("ИТОГ:", "ПАРИТЕТ" if is_ok(report) else "СТОП — есть расхождения")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dealer", required=True, help="код дилера (Dealer_Code)")
    p.add_argument("--reference", required=True, help="JSON-эталон от n8n")
    p.add_argument("--only", choices=["both", "credit", "combo"], default="both")
    p.add_argument("--active-only", action="store_true",
                   help="сверять только позиции, активные у дилера (колонка Catalog_Active_Field, по умолчанию Active)")
    args = p.parse_args()
    load_dotenv()
    reference = json.loads(Path(args.reference).read_text(encoding="utf-8"))
    if str(reference.get("dealer_code")) != args.dealer:
        print(f"ОТКАЗ: эталон для дилера {reference.get('dealer_code')!r}, а не {args.dealer!r}")
        return 2
    rows, active_field = asyncio.run(_load_catalog(args.dealer))
    if args.active_only:
        total = len(rows)
        rows = active_rows(rows, active_field)
        print(f"Только активные у дилера (колонка «{active_field}»): {len(rows)} из {total}")
    report = compare(rows, reference, args.only)
    _print_report(report, args.only)
    return 0 if is_ok(report) else 1


if __name__ == "__main__":
    sys.exit(main())
