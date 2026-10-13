"""Генератор двух воркфлоу вебхука оценки трейд-ина для веб-входа в кабинет (N1).

Пишет два JSON для импорта владельцем через интерфейс n8n (запись воркфлоу
через API запрещена — теряется binaryMode):
  - [TEST] WEBHOOK_ Cabinet Tradein Eval.json  — дев-копия, путь с суффиксом -test;
  - WEBHOOK_ Cabinet Tradein Eval.json         — боевая копия, НЕ активировать
                                                 без отдельной команды.

Дев-копия вызывает [TEST] TOOL: AUTO.RU EVALUATION (ToF5ApgK6qkk4mcH), боевая —
TOOL: AUTO.RU EVALUATION (XCPe53sUwk8i9e3T); код нод этих сабфлоу идентичен
(сверка 2026-09-30), решение владельца 2026-09-30.

Секрет в JSON не попадает: Webhook проверяет его встроенной Header Auth по
credential типа httpHeaderAuth, который владелец создаёт и выбирает в ноде
после импорта (свой для каждой копии). Неверный или отсутствующий заголовок
n8n отклоняет в обработчике вебхука, до старта исполнения — сабфлоу не
вызывается.

Запуск: python3 build_eval_webhooks.py  (идемпотентен: id нод и webhookId
детерминированы от имени копии).
"""
import json
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SECRET_HEADER = "X-Cabinet-Eval-Secret"

VARIANTS = {
    "test": {
        "name": "[TEST] WEBHOOK: Cabinet Tradein Eval",
        "file": "[TEST] WEBHOOK_ Cabinet Tradein Eval.json",
        "path": "cabinet-tradein-eval-test",
        "credential_hint": "Cabinet Eval Secret [TEST]",
        "credential_id": "zFKZt2QNOgURSnX8",
        "eval_id": "ToF5ApgK6qkk4mcH",
        "eval_name": "[TEST] TOOL: AUTO.RU EVALUATION",
    },
    "prod": {
        "name": "WEBHOOK: Cabinet Tradein Eval",
        "file": "WEBHOOK_ Cabinet Tradein Eval.json",
        "path": "cabinet-tradein-eval",
        "credential_hint": "Cabinet Eval Secret [PROD]",
        "credential_id": "KhaneOxa4PaPKXOM",
        "eval_id": "XCPe53sUwk8i9e3T",
        "eval_name": "TOOL: AUTO.RU EVALUATION",
    },
}

# --- Код нод -----------------------------------------------------------------

VALIDATE_CODE = r"""// [N1] Проверка тела запроса ДО платного вызова auto.ru.
// Секрет здесь уже проверен: Header Auth ноды Webhook отклоняет запрос
// раньше, чем стартует исполнение.
//
// Контракт запроса: { plate, mileage, request_id }.
//   plate      — госномер; нормализует его сабфлоу (нода Code in JavaScript1),
//                здесь только trim и проверка на непустую строку;
//   mileage    — пробег в км; сюда допускаются только цифры (пробелы и
//                разделители тысяч вырезаются). Сабфлоу во втором запросе
//                (stats/predict) шлёт пробег как пришёл, без очистки, —
//                поэтому чистим здесь, а не полагаемся на него;
//   request_id — идентификатор бота для сквозных логов, возвращается эхом.
const body = $input.first().json.body || {};

const requestId = body.request_id == null ? "" : String(body.request_id).trim();
const plate = body.plate == null ? "" : String(body.plate).trim();
const mileageDigits = body.mileage == null ? "" : String(body.mileage).replace(/[\s .,_']/g, "");

const errors = [];
if (!plate) errors.push("plate: пусто");
if (!/^\d{1,7}$/.test(mileageDigits) || Number(mileageDigits) <= 0) {
    errors.push("mileage: ожидается целое число км больше 0");
}

// request_id — в custom data исполнения: по нему исполнение находится
// в списке без открытия каждого. Сохранение работает на любой лицензии.
try {
    if (requestId) $execution.customData.set("request_id", requestId.slice(0, 255));
} catch (e) {}

return [{ json: {
    ok: errors.length === 0,
    request_id: requestId,
    plate,
    mileage: mileageDigits,
    errors,
}}];
"""

SHAPE_OK_CODE = r"""// [N1] Ответ бота: только поля контракта, без текстов для модели (message).
// Сабфлоу возвращает статусы SUCCESS / IN_PROGRESS / NOT_FOUND /
// INVALID_DATA / NO_REGION; статус передаётся как есть.
// found_info — только при SUCCESS и только если сабфлоу его отдал
// (контракт кабинета: «ключа нет», а не пустой объект).
const r = $input.first().json || {};
const req = $('Validate Request').first().json;
const num = v => (v === undefined || v === null || v === "" ? null : Number(v));

const out = {
    request_id: req.request_id,
    status: r.status || "ERROR",
    tradein_price_from: r.status === "SUCCESS" ? num(r.tradein_price_from) : null,
    tradein_price_to: r.status === "SUCCESS" ? num(r.tradein_price_to) : null,
};
if (r.status === "SUCCESS" && r.found_info && typeof r.found_info === "object"
        && !Array.isArray(r.found_info) && Object.keys(r.found_info).length) {
    out.found_info = r.found_info;
}
// Разобранная часть номера при NO_REGION / INVALID_DATA — для подсказки
// клиенту («вижу Т582ХР, допишите регион»); отдаётся, только если есть.
if (r.plate_seen) out.plate_seen = r.plate_seen;
return [{ json: out }];
"""

SHAPE_BAD_REQUEST_CODE = r"""// [N1] Тело запроса не прошло проверку — auto.ru не вызывался.
const req = $input.first().json;
return [{ json: {
    request_id: req.request_id,
    status: "BAD_REQUEST",
    errors: req.errors,
}}];
"""

SHAPE_ERROR_CODE = r"""// [N1] Сабфлоу оценки упал (например, HTTP-сбой ноды HTTP if_no без onError).
// Отдаём явный статус вместо зависшего запроса; подробности — в исполнении.
const req = $('Validate Request').first().json;
const err = $input.first().json || {};
return [{ json: {
    request_id: req.request_id,
    status: "ERROR",
    error: String((err.error && (err.error.message || err.error)) || "evaluation sub-workflow failed").slice(0, 300),
}}];
"""


def node_id(variant: str, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cabinet-tradein-eval/{variant}/{name}"))


def build(variant: str, cfg: dict) -> dict:
    nid = lambda n: node_id(variant, n)
    nodes = [
        {
            "parameters": {
                "content": (
                    f"## Вебхук оценки трейд-ина для веб-входа в кабинет ({'ДЕВ' if variant == 'test' else 'БОЙ'})\n\n"
                    f"POST `/webhook/{cfg['path']}`\n\n"
                    f"Секрет — заголовок `{SECRET_HEADER}`, credential Header Auth "
                    f"«{cfg['credential_hint']}». Секрет в этом JSON не хранится.\n\n"
                    f"Сабфлоу оценки: «{cfg['eval_name']}» (`{cfg['eval_id']}`).\n\n"
                    "Без секрета или с неверным — отказ n8n (403) до старта исполнения, "
                    "auto.ru не вызывается.\n\n"
                    + ("" if variant == "test" else
                       "**Боевая копия. Не активировать без отдельной команды.**\n\n")
                    + "Генератор: `flow/cabinet-web-entry/build_eval_webhooks.py`."
                ),
                "height": 300,
                "width": 420,
                "color": 3 if variant == "prod" else 5,
            },
            "id": nid("Note"),
            "name": "Note",
            "type": "n8n-nodes-base.stickyNote",
            "typeVersion": 1,
            "position": [-560, -260],
        },
        {
            "parameters": {
                "httpMethod": "POST",
                "path": cfg["path"],
                "authentication": "headerAuth",
                "responseMode": "responseNode",
                "options": {},
            },
            "id": nid("Webhook"),
            "name": "Webhook",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2.1,
            "position": [-520, 100],
            "webhookId": node_id(variant, "webhookId"),
            # credential, выбранный в n8n после импорта (= бой, план 2026-10-09-001);
            # секрет хранится в n8n, в JSON — только ссылка на credential
            "credentials": {"httpHeaderAuth": {"id": cfg["credential_id"], "name": cfg["credential_hint"]}},
        },
        {
            "parameters": {"jsCode": VALIDATE_CODE},
            "id": nid("Validate Request"),
            "name": "Validate Request",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [-300, 100],
        },
        {
            "parameters": {
                "conditions": {
                    "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 3},
                    "conditions": [{
                        "id": nid("cond-ok"),
                        "leftValue": "={{ $json.ok }}",
                        "rightValue": True,
                        # та же форма условия, что у живой If2 сабфлоу оценки
                        "operator": {"type": "boolean", "operation": "equals"},
                    }],
                    "combinator": "and",
                },
                "options": {},
            },
            "id": nid("Request OK?"),
            "name": "Request OK?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2.3,
            "position": [-80, 100],
        },
        {
            "parameters": {
                "workflowId": {
                    "__rl": True,
                    "value": cfg["eval_id"],
                    "mode": "list",
                    "cachedResultUrl": f"/workflow/{cfg['eval_id']}",
                    "cachedResultName": cfg["eval_name"],
                },
                "workflowInputs": {
                    "mappingMode": "defineBelow",
                    "value": {
                        "plate": "={{ $json.plate }}",
                        "mileage": "={{ $json.mileage }}",
                    },
                    "matchingColumns": [],
                    "schema": [
                        {"id": f, "displayName": f, "required": False, "defaultMatch": False,
                         "display": True, "canBeUsedToMatch": True, "type": "string", "removed": False}
                        for f in ("plate", "mileage")
                    ],
                    "attemptToConvertTypes": False,
                    "convertFieldsToString": True,
                },
                "options": {"waitForSubWorkflow": True},
            },
            "id": nid("Auto.ru Evaluation"),
            "name": "Auto.ru Evaluation",
            "type": "n8n-nodes-base.executeWorkflow",
            "typeVersion": 1.3,
            "position": [160, 0],
            "onError": "continueErrorOutput",
        },
        {
            "parameters": {"jsCode": SHAPE_OK_CODE},
            "id": nid("Shape Response"),
            "name": "Shape Response",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [400, -60],
        },
        {
            "parameters": {"respondWith": "firstIncomingItem", "options": {"responseCode": 200}},
            "id": nid("Respond 200"),
            "name": "Respond 200",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1.5,
            "position": [620, -60],
        },
        {
            "parameters": {"jsCode": SHAPE_ERROR_CODE},
            "id": nid("Shape Error"),
            "name": "Shape Error",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [400, 120],
        },
        {
            "parameters": {"respondWith": "firstIncomingItem", "options": {"responseCode": 502}},
            "id": nid("Respond 502"),
            "name": "Respond 502",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1.5,
            "position": [620, 120],
        },
        {
            "parameters": {"jsCode": SHAPE_BAD_REQUEST_CODE},
            "id": nid("Shape Bad Request"),
            "name": "Shape Bad Request",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [160, 280],
        },
        {
            "parameters": {"respondWith": "firstIncomingItem", "options": {"responseCode": 400}},
            "id": nid("Respond 400"),
            "name": "Respond 400",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1.5,
            "position": [400, 280],
        },
    ]
    main = lambda *targets: {"main": [[{"node": t, "type": "main", "index": 0}] if t else [] for t in targets]}
    connections = {
        "Webhook": main("Validate Request"),
        "Validate Request": main("Request OK?"),
        "Request OK?": main("Auto.ru Evaluation", "Shape Bad Request"),
        "Auto.ru Evaluation": main("Shape Response", "Shape Error"),
        "Shape Response": main("Respond 200"),
        "Shape Error": main("Respond 502"),
        "Shape Bad Request": main("Respond 400"),
    }
    return {
        "name": cfg["name"],
        "nodes": nodes,
        "connections": connections,
        "active": False,
        "settings": {
            "executionOrder": "v1",
            "binaryMode": "separate",
            "callerPolicy": "workflowsFromSameOwner",
        },
        "pinData": {},
        "tags": [],
    }


if __name__ == "__main__":
    for variant, cfg in VARIANTS.items():
        wf = build(variant, cfg)
        out = HERE / cfg["file"]
        out.write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{out.name}: {len(wf['nodes'])} нод, путь /webhook/{cfg['path']}")
