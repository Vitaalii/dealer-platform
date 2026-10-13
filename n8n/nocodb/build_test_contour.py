#!/usr/bin/env python3
"""Тестовый контур v3.0: полностью изолированный набор воркфлоу [TEST] на том же
инстансе n8n. Прод-воркфлоу не перезаписываются и не упоминаются по ID нигде.

С 2026-08-10 (план 2026-08-10-001, U2) форкается и TOOL: AUTO.RU EVALUATION:
раньше он сознательно шарился как read-only, но U1 правит его терминальную ноду
(проброс found_info в личный кабинет), а правка шаренного воркфлоу немедленно
меняла бы observation тула на боевом контуре. Шарируемых саб-воркфлоу больше нет.

Конвенция: имя воркфлоу — префикс "[TEST] "; webhook-пути — суффикс "-test";
внутренние имена нод не меняются (они видны только внутри своего воркфлоу).
Все ссылки на форкнутые саб-воркфлоу заменены id живых [TEST]-копий (TEST_IDS,
план 2026-10-09-001, KTD4): генерация = живой [TEST]-контур без ручной перепривязки.

Исключение (решение владельца 2026-10-09, Q3): живой shadow F3eluZCNKGAcPwOT
назван без префикса [TEST] и зовёт БОЕВОЙ Tool: Resolver Core (qsotmM7sbmyckg3E).
Генератор пишет правильную изоляцию ([TEST]-имя, [TEST] Resolver Core); отличие
живой копии записано в flow/cabinet-web-entry/api/expected_divergences.json,
перевод shadow на [TEST] Resolver Core — в отложенных.

Вход: канонические файлы в flow/. Выход: flow/test-contour/.
"""
import json
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLOW = HERE.parent / "flow"
OUT = FLOW / "test-contour"
OUT.mkdir(exist_ok=True)

# саб-воркфлоу, которые ОБЯЗАНЫ быть форкнуты (их трогает v3-цепочка или они
# зовут форкнутые)
FORK_REFS = {
    "Tool: Calc Engine": "[TEST] Tool: Calc Engine",
    "Tool - Car Search": "[TEST] Tool - Car Search",
    "Tool: Combo Calculator": "[TEST] Tool: Combo Calculator",
    # Save to CRM НЕ форкается: тестовый вариант в n8n уже есть (снимок —
    # flow/test-contour/TOOL_ Save to CRM_test.json)
    "TOOL: Save to CRM": "TOOL: Save to CRM_test",
    "Tool: Resolver Core": "[TEST] Tool: Resolver Core",
    # U2: форкается с 2026-08-10 — U1 правит его терминальную ноду; ссылок две
    # (evaluate_auto в AI Agent v3.0 и Execute Workflow в Combo Calculator)
    "TOOL: AUTO.RU EVALUATION": "[TEST] TOOL: AUTO.RU EVALUATION",
}
# механика сохранена на будущее, но шарируемых саб-воркфлоу сейчас нет:
# любая неучтённая ссылка обязана всплыть предупреждением в отчёте fork()
SHARED_REFS = set()

# id живых [TEST]-копий по их имени (сверка: check_sources_vs_live.py)
TEST_IDS = {
    "[TEST] Tool: Calc Engine": "1K9aRaJrWRCF4InF",
    "[TEST] Tool - Car Search": "o3vH8ZVKW5dNMkK2",
    "[TEST] Tool: Combo Calculator": "GjppGA6LqjbaqX4G",
    "TOOL: Save to CRM_test": "12IRy1rcYOuZzV7m",
    "[TEST] Tool: Resolver Core": "phecB9jOVAR1DqnX",
    "[TEST] TOOL: AUTO.RU EVALUATION": "ToF5ApgK6qkk4mcH",
}
assert set(TEST_IDS) == set(FORK_REFS.values()), "TEST_IDS и FORK_REFS разошлись"

# Header Auth на ВСЕХ [TEST]-вебхуках (2026-10-03, docs/handoff/2026-10-03-n8n-webhook-auth-bot-plan.md):
# дев-бот шлёт X-N8N-Secret с тестовым секретом. Боевой credential из исходника,
# если он там появится, заменяется тестовым — тестовый контур боевой секрет не принимает.
# TOOL: Save to CRM_test не форкается (см. FORK_REFS): заголовок X-N8N-Callback-Secret
# в его HTTP-ноде задан в n8n набором test-save-crm-callback
# (flow/cabinet-web-entry/api/apply_workflow_patch.py).
TEST_INBOUND_AUTH = {"id": "8WHmaHbGppnSXL9F", "name": "N8N Inbound Secret [TEST]"}

PROD_BOT_URL = "https://obut-5oaj-5i2u.gw-1a.dockhost.net/n8n_callback"
TEST_BOT_URL_PLACEHOLDER = "REPLACE_WITH_TEST_BOT_URL/n8n_callback"

FILES = [  # (исходник, [TEST]-файл)
    ("TOOL_ AUTO.RU EVALUATION.json", "[TEST] TOOL_ AUTO.RU EVALUATION.json"),
    ("Tool_ Calc Engine.json", "[TEST] Tool_ Calc Engine.json"),
    ("Test_ Calc Engine.json", "[TEST] Test_ Calc Engine.json"),
    ("Tool - Car Search.json", "[TEST] Tool - Car Search.json"),
    ("Tool_ Combo Calculator.json", "[TEST] Tool_ Combo Calculator.json"),
    ("Tool_ Resolver Core.json", "[TEST] Tool_ Resolver Core.json"),
    ("AI Resolver.json", "[TEST] AI Resolver.json"),
    ("AI Agent v3.0.json", "[TEST] AI Agent v3.0.json"),
    ("Tool_ Blocks Lint.json", "[TEST] Tool_ Blocks Lint.json"),
]


def fork(src_name, dst_name):
    wf = json.loads((FLOW / src_name).read_text(encoding="utf-8"))
    report = []

    wf["name"] = "[TEST] " + wf.get("name", src_name.replace(".json", ""))
    for key in ("id", "versionId", "active", "tags"):
        wf.pop(key, None)

    for n in wf["nodes"]:
        params = n.get("parameters", {})
        # webhook-пути и id
        if n["type"] == "n8n-nodes-base.webhook":
            params["path"] = params["path"] + "-test"
            n["webhookId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-contour/{dst_name}/{n['name']}"))
            params["authentication"] = "headerAuth"
            n["credentials"] = {"httpHeaderAuth": dict(TEST_INBOUND_AUTH)}
            report.append(f"webhook: /webhook/{params['path']} (Header Auth: {TEST_INBOUND_AUTH['name']})")
        # ссылки на саб-воркфлоу
        wid = params.get("workflowId")
        if isinstance(wid, dict) and wid.get("__rl"):
            ref = wid.get("cachedResultName", "")
            if ref in FORK_REFS:
                params["workflowId"] = {"__rl": True, "value": TEST_IDS[FORK_REFS[ref]],
                                        "mode": "list", "cachedResultName": FORK_REFS[ref]}
                report.append(f"нода '{n['name']}': → '{FORK_REFS[ref]}' ({TEST_IDS[FORK_REFS[ref]]})")
            elif ref in SHARED_REFS:
                report.append(f"нода '{n['name']}': ШАРИТСЯ прод '{ref}' (read-only, ID сохранён)")
            else:
                report.append(f"⚠️ нода '{n['name']}': неучтённая ссылка '{ref}'")
        # callback-URL бота -> локальный тестовый бот
        if params.get("url") == PROD_BOT_URL:
            params["url"] = TEST_BOT_URL_PLACEHOLDER
            report.append(f"нода '{n['name']}': URL -> {TEST_BOT_URL_PLACEHOLDER} (подставить туннель локального бота)")

    (OUT / dst_name).write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return wf["name"], report


print("Генерация тестового контура -> flow/test-contour/\n")
for src, dst in FILES:
    name, report = fork(src, dst)
    print(f"— {name}")
    for r in report:
        print(f"    {r}")
print("\nПрод-файлы не изменялись. Шарируемых саб-воркфлоу нет: с 2026-08-10 (U2)\n"
      "AUTO.RU EVALUATION форкается — прод-копия в n8n остаётся без правки U1.")
