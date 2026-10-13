"""Изоляция [TEST]-контура (план 2026-10-09-001, U4): ссылки на саб-воркфлоу —
только на [TEST]-копии, боевых credentials и адреса боевого бота нет.

Проверяются закоммиченные flow/test-contour/*.json; что они = генерации и =
живому [TEST]-контуру, проверяет flow/cabinet-web-entry/api/check_sources_vs_live.py.
Запуск: python3 -m pytest nocodb/test_build_test_contour.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTOUR = HERE.parent / "flow" / "test-contour"
sys.path.insert(0, str(HERE.parent / "flow" / "cabinet-web-entry" / "api"))
import check_sources_vs_live as c  # noqa: E402

TEST_IDS = {wid for f, wid, _, _ in c.PAIRS if f.startswith("test-contour/") or "[TEST]" in f}
PROD_IDS = {wid for _, wid, _, _ in c.PAIRS} - TEST_IDS
PROD_BOT_HOST = "obut-5oaj-5i2u.gw-1a.dockhost.net"


def contour():
    files = sorted(CONTOUR.glob("*.json"))
    assert len(files) >= 10
    return [(f.name, json.loads(f.read_text(encoding="utf-8"))) for f in files]


def test_sub_workflow_refs_point_only_to_test_copies():
    seen = 0
    for name, wf in contour():
        for n in wf["nodes"]:
            wid = (n.get("parameters") or {}).get("workflowId")
            if not isinstance(wid, dict):
                continue
            seen += 1
            v = wid.get("value")
            assert v not in PROD_IDS, f"{name}: нода «{n['name']}» ссылается на боевой {v}"
            assert v in TEST_IDS, f"{name}: нода «{n['name']}» ссылается на {v!r} — не [TEST]-копия"
    assert seen >= 10


def test_no_prod_credentials():
    for name, wf in contour():
        for n in wf["nodes"]:
            for cred in (n.get("credentials") or {}).values():
                assert "[PROD]" not in cred.get("name", ""), f"{name}: «{n['name']}» — {cred['name']}"


def test_no_prod_bot_address():
    for name, wf in contour():
        assert PROD_BOT_HOST not in json.dumps(wf, ensure_ascii=False), name


def test_webhooks_are_test_paths_with_test_secret():
    for name, wf in contour():
        for n in wf["nodes"]:
            if n["type"] != "n8n-nodes-base.webhook":
                continue
            assert n["parameters"]["path"].endswith("-test"), name
            assert n["credentials"]["httpHeaderAuth"]["name"].endswith("[TEST]"), name
