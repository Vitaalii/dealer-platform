#!/usr/bin/env python3
"""U9/U10: генератор резолвера в двух частях.

1) flow/Tool_ Resolver Core.json — саб-воркфлоу (Execute Workflow Trigger →
   Get Intents → Build Prompt → LLM → Validate) — возвращает результат
   классификации. Используется и shadow-обёрткой, и конвейером v3.0.
2) flow/AI Resolver.json — shadow-обёртка: Webhook (ответ сразу) →
   Execute Workflow (Resolver Core) → Prepare Log → Save Log (NocoDB).

Ссылка 'Resolver Core' обёртки — боевой id; [TEST]-копию контур подменяет сам.
"""
import json
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLOW = HERE.parent / "flow"
IDS = json.loads((HERE / "table_ids.json").read_text())
BASE_ID = "pqw76hd7h1lztww"
NOCO_CRED = {"nocoDbApiToken": {"id": "ijM4z0UsgIXr8sqx", "name": "NocoDB Token account"}}
OPENROUTER_CRED = {"openRouterApi": {"id": "hrFobDiOtSwC9E0F", "name": "OpenRouter account"}}
MODEL = "google/gemini-2.5-flash-lite"
CORE_NAME = "Tool: Resolver Core"
WEBHOOK_PATH = "ai-resolver-shadow"

CLIENT_MODES = ("collector,reference,advocate_tradein,advocate_special,secretary,"
                "logistics_block,skeptic,price_compare,direct_agree,resistance")
CLIENT_SIGNALS = ("moving_forward,hesitating,asking_more,stalling,resisting,"
                  "scheduling,refusing_softly,refusing_hard,neutral,"
                  "handoff_requested,satisfied_done")

BUILD_JS = r"""
const body = $('Execute Workflow Trigger').first().json;
const sc = body.session_context || {};
const collected = sc.collected_data || {};
const scenario = sc.request_type || "unknown";
const stage = sc.current_stage || "data_collect";

const expand = (v, u) => (!v || v === "all") ? u : String(v).split(",").map(s => s.trim());
const SCENARIOS = ["credit","tradein","combo","stock","consult"];

// R1 (план 2026-07-17-001): стадия — только контекст промпта, НЕ фильтр списка.
// Stage_Context в таблице — справочное поле; фильтруем только Active и Scenarios.
const intents = $('Get Intents').all().map(i => i.json)
    .filter(i => i.Active)
    .filter(i => expand(i.Scenarios, SCENARIOS).includes(scenario) || i.Scenarios === "all")
    .sort((a, b) => (Number(b.Priority) || 0) - (Number(a.Priority) || 0));

const intentList = intents.map(i => `${i.Intent_ID} | ${i.Description} | Примеры: ${i.Example_Phrases}`).join("\n");
const history = (body.recent_history || []).slice(-8)
    .map(m => `${m.role === "client" ? "Клиент" : "Бот"}: ${m.text}`).join("\n");

const prompt = `Ты — классификатор состояния диалога в автосалоне.
Проанализируй последние сообщения и текущее сообщение клиента.
Определи точный момент диалога и верни СТРОГО JSON, без пояснений.

Сценарий: ${scenario}
Текущая стадия системы: ${stage}
Данные собраны: взнос=${Boolean(collected.payment)}, госномер=${Boolean(collected.plate)}, пробег=${Boolean(collected.mileage)}, телефон=${Boolean(collected.phone_collected)}
Телефон запрашивался: ${sc.phone_ask_count || 0} раз

--- НАЧАЛО СПИСКА ДОПУСТИМЫХ ИНТЕНТОВ ---
${intentList}
--- КОНЕЦ СПИСКА ---

Допустимые client_mode: __MODES__
Допустимые client_signal: __SIGNALS__

Последние сообщения:
${history}

Текущее сообщение клиента: "${body.message || ""}"

Верни ТОЛЬКО JSON:
{
  "dialog_intent": "<значение строго из списка интентов выше>",
  "client_mode": "<значение строго из списка client_mode>",
  "client_signal": "<значение строго из списка client_signal>",
  "key_data_extracted": { },
  "reasoning": "<одно предложение: почему именно этот интент>"
}`;

return [{ json: {
    prompt,
    offered: intents.map(i => i.Intent_ID),
    start_ts: Date.now(),
    meta: {
        session_id: body.session_id || "", user_id: body.user_id || "",
        scenario, stage, msg_count: sc.msg_count || 0,
        client_msg: body.message || "",
    },
}}];
""".replace("__MODES__", CLIENT_MODES).replace("__SIGNALS__", CLIENT_SIGNALS)

VALIDATE_JS = r"""
const build = $('Build Prompt').first().json;
const out = $input.first().json;
const latency = Date.now() - build.start_ts;

let verdict = "ok";
let parsed = {};
let raw = "";

if (out.error) {
    verdict = "llm_error";
    raw = String(out.error).slice(0, 2000);
} else {
    raw = String(out.text ?? out.output ?? "").trim();
    let candidate = raw.replace(/^```(json)?/m, "").replace(/```$/m, "").trim();
    const m = candidate.match(/\{[\s\S]*\}/);
    try {
        parsed = JSON.parse(m ? m[0] : candidate);
    } catch (e) {
        verdict = "parse_error";
    }
    if (verdict === "ok" && !build.offered.includes(parsed.dialog_intent)) {
        verdict = "unknown_intent";
    }
}

return [{ json: {
    verdict,
    dialog_intent: verdict === "ok" ? parsed.dialog_intent : "",
    client_mode: parsed.client_mode || "",
    client_signal: parsed.client_signal || "",
    key_data_extracted: parsed.key_data_extracted || {},
    reasoning: parsed.reasoning || "",
    latency_ms: latency,
    model: "__MODEL__",
    raw_output: verdict === "ok" ? "" : raw.slice(0, 3000),
    meta: build.meta,
}}];
""".replace("__MODEL__", MODEL)

LOG_JS = r"""
const r = $input.first().json;
return [{ json: {
    ts: new Date().toISOString(),
    session_id: r.meta.session_id, user_id: r.meta.user_id,
    scenario: r.meta.scenario, stage: r.meta.stage, msg_count: r.meta.msg_count,
    client_msg: r.meta.client_msg,
    dialog_intent: r.dialog_intent, client_mode: r.client_mode, client_signal: r.client_signal,
    key_data_extracted: JSON.stringify(r.key_data_extracted || {}),
    reasoning: r.reasoning, verdict: r.verdict, latency_ms: r.latency_ms,
    model: r.model, raw_output: r.raw_output,
}}];
"""


# Защита вебхука от чужих вызовов (docs/handoff/2026-10-03-n8n-webhook-auth-bot-plan.md, шаг 7).
# Боевой credential включается ТОЛЬКО по команде владельца после релиза бота,
# который шлёт X-N8N-Secret; до тех пор None — нода без аутентификации, как на
# бою сейчас. [TEST]-копию build_test_contour.py защищает тестовым секретом всегда.
INBOUND_AUTH = None  # бой: {"id": "CfzDWuMkUZABPQnM", "name": "N8N Inbound Secret [PROD]"}


def webhook_auth(params):
    """Header Auth по INBOUND_AUTH: (параметры ноды, credentials или None)."""
    if not INBOUND_AUTH:
        return params, None
    return dict(params, authentication="headerAuth"), {"httpHeaderAuth": dict(INBOUND_AUTH)}


def node(name, ntype, tv, params, pos, creds=None, extra=None):
    n = {"parameters": params, "type": ntype, "typeVersion": tv, "position": pos,
         "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"resolver/{name}")), "name": name}
    if creds:
        n["credentials"] = creds
    if extra:
        n.update(extra)
    return n


def shell(name, nodes, conns):
    return {"name": name, "nodes": nodes, "connections": conns,
            "settings": {"executionOrder": "v1"}, "pinData": {},
            "meta": {"instanceId": "65c0178e84d5c3db112ec6489beae5f53a11ae74e53d6537d4d9a5cd140a483b"}}


# ---------- 1. Tool: Resolver Core ----------
core_nodes = [
    node("Execute Workflow Trigger", "n8n-nodes-base.executeWorkflowTrigger", 1.1,
         {"inputSource": "passthrough"}, [0, 0]),
    node("Get Intents", "n8n-nodes-base.nocoDb", 3,
         {"authentication": "nocoDbApiToken", "operation": "getAll",
          "projectId": BASE_ID, "table": IDS["Intents"], "returnAll": True, "options": {}},
         [220, 0], NOCO_CRED, extra={"executeOnce": True}),  # защита от per-item запусков
    node("Build Prompt", "n8n-nodes-base.code", 2, {"jsCode": BUILD_JS.strip()}, [440, 0]),
    node("Resolver LLM", "@n8n/n8n-nodes-langchain.chainLlm", 1.9,
         {"promptType": "define", "text": "={{ $json.prompt }}", "batching": {}},
         [660, 0], extra={"onError": "continueRegularOutput"}),
    node("OpenRouter Flash Lite", "@n8n/n8n-nodes-langchain.lmChatOpenRouter", 1,
         {"model": MODEL, "options": {"temperature": 0}}, [660, 200], OPENROUTER_CRED),
    node("Validate", "n8n-nodes-base.code", 2, {"jsCode": VALIDATE_JS.strip()}, [880, 0]),
]
core_conns = {
    "Execute Workflow Trigger": {"main": [[{"node": "Get Intents", "type": "main", "index": 0}]]},
    "Get Intents": {"main": [[{"node": "Build Prompt", "type": "main", "index": 0}]]},
    "Build Prompt": {"main": [[{"node": "Resolver LLM", "type": "main", "index": 0}]]},
    "Resolver LLM": {"main": [[{"node": "Validate", "type": "main", "index": 0}]]},
    "OpenRouter Flash Lite": {"ai_languageModel": [[{"node": "Resolver LLM", "type": "ai_languageModel", "index": 0}]]},
}
(FLOW / "Tool_ Resolver Core.json").write_text(
    json.dumps(shell(CORE_NAME, core_nodes, core_conns), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# ---------- 2. AI Resolver (shadow-обёртка) ----------
shadow_nodes = [
    node("Webhook", "n8n-nodes-base.webhook", 2.1,
         webhook_auth({"httpMethod": "POST", "path": WEBHOOK_PATH, "responseMode": "onReceived", "options": {}})[0],
         [0, 0], creds=webhook_auth({})[1],
         extra={"webhookId": str(uuid.uuid5(uuid.NAMESPACE_URL, "resolver/webhook-id"))}),
    node("Unwrap Body", "n8n-nodes-base.code", 2,
         {"jsCode": "return [{ json: $input.first().json.body || {} }];"}, [200, 0]),
    node("Resolver Core", "n8n-nodes-base.executeWorkflow", 1.3,
         {"workflowId": {"__rl": True, "value": "qsotmM7sbmyckg3E",   # боевой Tool: Resolver Core
                         "mode": "list", "cachedResultName": CORE_NAME},
          "workflowInputs": {"mappingMode": "defineBelow", "value": {}, "matchingColumns": [], "schema": [],
                             "attemptToConvertTypes": False, "convertFieldsToString": True},
          "options": {"waitForSubWorkflow": True}}, [400, 0]),
    node("Prepare Log", "n8n-nodes-base.code", 2, {"jsCode": LOG_JS.strip()}, [600, 0]),
    node("Save Log", "n8n-nodes-base.nocoDb", 3,
         {"authentication": "nocoDbApiToken", "operation": "create",
          "projectId": BASE_ID, "table": IDS["Resolver_Shadow_Log"],
          "dataToSend": "autoMapInputData"}, [800, 0], NOCO_CRED),
]
shadow_conns = {
    "Webhook": {"main": [[{"node": "Unwrap Body", "type": "main", "index": 0}]]},
    "Unwrap Body": {"main": [[{"node": "Resolver Core", "type": "main", "index": 0}]]},
    "Resolver Core": {"main": [[{"node": "Prepare Log", "type": "main", "index": 0}]]},
    "Prepare Log": {"main": [[{"node": "Save Log", "type": "main", "index": 0}]]},
}
(FLOW / "AI Resolver.json").write_text(
    json.dumps(shell("AI Resolver (shadow)", shadow_nodes, shadow_conns), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8")

print("OK: Tool_ Resolver Core.json + AI Resolver.json")
print(f"Webhook path: /webhook/{WEBHOOK_PATH}; Resolver Core — боевой id")
