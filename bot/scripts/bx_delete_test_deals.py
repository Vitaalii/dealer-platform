#!/usr/bin/env python3
"""Удаление тестовых сделок и их контактов в Битриксе (только по команде владельца).

Без --apply — холостой проход: для каждой сделки проверяется, что она есть,
в воронке 9, «Чат ID» начинается с --code-prefix, её контакт — из списка;
для каждого контакта — что других сделок вне списка у него нет. Любое
несовпадение — выход без удаления. После --apply — удаление и проверка
«осталось: ничего». Вебхук — BITRIX_WEBHOOK_URL из .env (в вывод не попадает).

    venv/bin/python scripts/bx_delete_test_deals.py --deals 53347 53345 --contacts 50431 50429 --code-prefix 01
    venv/bin/python scripts/bx_delete_test_deals.py ... --apply
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

from dotenv import dotenv_values

CODE_FIELD = "UF_CRM_I2CRM_CHAT"   # «Чат ID» — код заявки


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deals", nargs="+", required=True)
    ap.add_argument("--contacts", nargs="+", required=True)
    ap.add_argument("--code-prefix", required=True, help="код дилера, напр. 01 или 99")
    ap.add_argument("--keep", nargs="*", default=[], help="сделки, которые трогать нельзя (стоп, если в списке)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    wh = dotenv_values(".env")["BITRIX_WEBHOOK_URL"].rstrip("/") + "/"

    def call(method, params):
        req = urllib.request.Request(wh + method, data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b"{}")

    deals, contacts = a.deals, a.contacts
    ok = len(set(deals)) == len(deals) and len(set(contacts)) == len(contacts)
    ok &= not set(deals) & set(a.keep)
    print(f"в списке: сделок {len(deals)}, контактов {len(contacts)}")
    used = set()
    for deal in deals:
        d = call("crm.deal.get", {"id": deal}).get("result") or {}
        code, cid = str(d.get(CODE_FIELD) or ""), str(d.get("CONTACT_ID") or "")
        good = bool(d) and d.get("CATEGORY_ID") == "9" and code.startswith(a.code_prefix) and cid in contacts
        used.add(cid)
        ok &= good
        print(f"{'OK ' if good else 'СТОП'} сделка {deal} кат {d.get('CATEGORY_ID')} код {code} "
              f"{d.get('STAGE_ID')} создана {str(d.get('DATE_CREATE'))[:16]} контакт {cid}")
    for contact in contacts:
        c = call("crm.contact.get", {"id": contact}).get("result") or {}
        others = sorted(x["ID"] for x in call("crm.deal.list", {"filter": {"CONTACT_ID": contact},
                                                               "select": ["ID"]}).get("result", []))
        phone = (c.get("PHONE") or [{}])[0].get("VALUE", "")
        good = bool(c) and set(others) <= set(deals) and contact in used
        ok &= good
        print(f"{'OK ' if good else 'СТОП'} контакт {contact} тел {phone[:5]}…{phone[-3:]} сделки {others}")
    if not ok:
        sys.exit("Есть несовпадения — удаление не выполняется")
    if not a.apply:
        print("Холостой проход: ничего не удалено.")
        return
    for deal in deals:
        print("crm.deal.delete", deal, call("crm.deal.delete", {"id": deal}).get("result"))
    for contact in contacts:
        print("crm.contact.delete", contact, call("crm.contact.delete", {"id": contact}).get("result"))
    left = [x for x in deals if call("crm.deal.get", {"id": x}).get("result")]
    left += [x for x in contacts if call("crm.contact.get", {"id": x}).get("result")]
    print("осталось:", left or "ничего")
    for keep in a.keep:
        print(f"{keep} на месте:", bool(call("crm.deal.get", {"id": keep}).get("result")))


if __name__ == "__main__":
    main()
