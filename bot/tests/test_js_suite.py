"""U3/U7 (R12, R21): pytest-обёртка Node-прогонов JS и стражи дрейфа версий.

Три уровня защиты математики калькулятора:
1. Golden-прогон `node tests/js/run_golden.mjs` на браузерной копии движка и
   прогон модели величин `node tests/js/calc_model.test.mjs` (U7).
   Отсутствие node — КРАСНЫЙ тест, не skip: эти прогоны — единственная
   защита математики и не должны сниматься молча (R21).
2. Страж версии: маркер CALC_ENGINE_VERSION в статике совпадает с версией
   в обёртке golden_cases.json (оба пишет build_calc_engine_workflow.py).
3. Контентный страж (только на машине с папкой n8n-репо): текст канона
   calc_engine.js байт-в-байт (после strip) равен браузерной копии —
   ловит правку канона без билда и статику, отставшую от канона.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "widget-custom" / "cabinet-app"
BROWSER_ENGINE = APP_DIR / "calc_engine.browser.js"
GOLDEN_CASES = APP_DIR / "golden_cases.json"
CALC_MODEL = APP_DIR / "calc_model.js"

# Канон движка: папка n8n-репо (не под git), существует не на каждой машине.
CANON_LIB_DIR = "/Users/vitaln/Yandex.Disk.localized/ADC/Products/Landing/DEV/BOT/N8N/Alex_refactoring/flow/lib"

VERSION_RE = re.compile(r'CALC_ENGINE_VERSION\s*=\s*"([^"]+)"')


def _marker_version(js_text: str) -> str:
    m = VERSION_RE.search(js_text)
    assert m, "маркер CALC_ENGINE_VERSION не найден в тексте движка"
    return m.group(1)


def _run_node_suite(script: str, label: str):
    """Прогон JS-набора голым node. Отсутствие node — красный тест (R21)."""
    if shutil.which("node") is None:
        pytest.fail(f"node не найден — {label} не может быть снят молча")
    proc = subprocess.run(
        ["node", script],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"{label} упал (код {proc.returncode})\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


def test_golden_run_on_browser_engine():
    """Golden-кейсы зелёные на браузерной копии движка (голый node, без npm)."""
    _run_node_suite("tests/js/run_golden.mjs", "golden-прогон движка")


def test_calc_model_node_suite():
    """U7: модель величин calc_model.js — Node-тесты на assert/strict."""
    _run_node_suite("tests/js/calc_model.test.mjs", "прогон модели величин (U7)")


def test_calc_model_expects_engine_version_in_static():
    """Рантайм-страж дрейфа (R12/R18): EXPECTED_CALC_ENGINE_VERSION модели
    совпадает с маркером движка в статике — иначе калькулятор деградирует в
    ручной ввод на живой странице, а тесты этого не заметят."""
    marker = _marker_version(BROWSER_ENGINE.read_text(encoding="utf-8"))
    model = CALC_MODEL.read_text(encoding="utf-8")
    m = re.search(r'EXPECTED_CALC_ENGINE_VERSION\s*=\s*"([^"]+)"', model)
    assert m, "EXPECTED_CALC_ENGINE_VERSION не найдена в calc_model.js"
    assert m.group(1) == marker, (
        f"дрейф версий: модель ждёт {m.group(1)!r}, движок в статике {marker!r} — "
        f"обнови EXPECTED_CALC_ENGINE_VERSION после билда движка"
    )


def test_engine_version_matches_golden_cases():
    """Страж версии: маркер в статике == calc_engine_version в golden_cases.json."""
    marker = _marker_version(BROWSER_ENGINE.read_text(encoding="utf-8"))
    wrapper = json.loads(GOLDEN_CASES.read_text(encoding="utf-8"))
    assert "calc_engine_version" in wrapper and "cases" in wrapper, (
        "golden_cases.json не в формате обёртки {'calc_engine_version', 'cases'} — "
        "перезапусти build_calc_engine_workflow.py"
    )
    assert wrapper["calc_engine_version"] == marker, (
        f"дрейф версий: статика {marker!r}, golden_cases.json "
        f"{wrapper['calc_engine_version']!r} — запусти build_calc_engine_workflow.py"
    )


def test_browser_engine_matches_canon():
    """Контентный страж: браузерная копия байт-в-байт равна канону из n8n-репо."""
    if not os.path.exists(CANON_LIB_DIR):
        pytest.skip("канон недоступен на этой машине (папка n8n-репо не найдена)")
    # Тот же способ извлечения lib_code, что в build_calc_engine_workflow.py:
    # текст calc_engine.js как есть (диспетчер скрипт дописывает отдельно).
    canon = (pathlib.Path(CANON_LIB_DIR) / "calc_engine.js").read_text(encoding="utf-8")
    browser = BROWSER_ENGINE.read_text(encoding="utf-8")
    if canon.strip() == browser.strip():
        return
    canon_ver = _marker_version(canon)
    browser_ver = _marker_version(browser)
    if canon_ver == browser_ver:
        pytest.fail(
            f"канон правлен без билда: содержимое разошлось при равных маркерах "
            f"версии {canon_ver!r} — инкрементируй CALC_ENGINE_VERSION в calc_engine.js "
            f"и запусти build_calc_engine_workflow.py"
        )
    pytest.fail(
        f"статика отстала от канона — запусти билд: канон {canon_ver!r}, "
        f"статика {browser_ver!r} (build_calc_engine_workflow.py --browser-out ...)"
    )
