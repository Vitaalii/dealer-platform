"""Нормализация и сопоставление с ожиданиями в check_sources_vs_live.py (без n8n).

Запуск: python3 -m pytest flow/cabinet-web-entry/api/test_check_sources_vs_live.py
"""
import copy

import check_sources_vs_live as c


def wf(**over):
    base = {
        "name": "W", "id": "x", "versionId": "v1", "active": True, "updatedAt": "t",
        "nodes": [
            {"id": "a", "name": "Code", "type": "n8n-nodes-base.code", "position": [0, 0],
             "parameters": {"jsCode": "return 1;"}},
            {"id": "b", "name": "Sub", "type": "n8n-nodes-base.executeWorkflow", "position": [1, 1],
             "parameters": {"workflowId": {"__rl": True, "value": "ID1", "mode": "list",
                                           "cachedResultName": "Tool", "cachedResultUrl": "/workflow/ID1"}}},
            {"id": "c", "name": "Webhook", "type": "n8n-nodes-base.webhook", "position": [2, 2],
             "parameters": {"path": "p", "responseMode": "onReceived"}},
        ],
        "connections": {"Code": {"main": [[{"node": "Sub", "type": "main", "index": 0}]]}},
        "settings": {"executionOrder": "v1"},
    }
    base.update(over)
    return base


def nd(w):
    return c.diff(c.normalize(w), c.normalize(wf()))


def test_server_fields_ids_positions_ignored():
    w = wf(id="y", versionId="v2", updatedAt="u", meta={"m": 1}, pinData={"a": 1})
    for n in w["nodes"]:
        n["id"], n["position"] = "other", [9, 9]
    assert nd(w) == []


def test_settings_set_by_n8n_ignored_only_with_default_values():
    assert nd(wf(settings={"executionOrder": "v1", "availableInMCP": False,
                           "callerPolicy": "workflowsFromSameOwner", "binaryMode": "separate"})) == []
    assert nd(wf(settings={"executionOrder": "v1", "callerPolicy": "any"})) == [
        ("settings.callerPolicy", "any", "<нет>")]
    assert nd(wf(settings={"executionOrder": "v1", "binaryMode": "combined"})) == [
        ("settings.binaryMode", "combined", "<нет>")]


def test_empty_connection_groups_and_default_response_mode_ignored():
    w = wf()
    w["connections"]["Webhook"] = {"main": [[]]}
    w["nodes"][2]["parameters"].pop("responseMode")
    assert nd(w) == []


def test_cached_result_labels_ignored_but_value_compared():
    w = wf()
    w["nodes"][1]["parameters"]["workflowId"].update(cachedResultName="[TEST] Tool", cachedResultUrl="/workflow/Z")
    assert nd(w) == []
    w["nodes"][1]["parameters"]["workflowId"]["value"] = "ID2"
    assert nd(w) == [("nodes.Sub.parameters.workflowId.value", "ID2", "ID1")]


def test_code_and_credentials_are_functional():
    w = wf()
    w["nodes"][0]["parameters"]["jsCode"] = "return 2;"
    w["nodes"][0]["credentials"] = {"httpHeaderAuth": {"id": "k", "name": "K"}}
    paths = [p for p, _, _ in nd(w)]
    assert paths == ["nodes.Code.credentials", "nodes.Code.parameters.jsCode"]


def test_node_only_on_one_side():
    w = wf()
    w["nodes"].pop(0)
    assert [(p, g) for p, g, _ in nd(w)] == [("nodes.Code", "<нет>")]


def test_expected_entry_suppresses_then_drift_and_stale():
    long_gen, long_live = "a" * 300, "b" * 300
    diffs = [("nodes.Code.parameters.jsCode", long_gen, long_live)]
    entry = {"id": "W1", "path": "nodes.Code.parameters.jsCode", "decision": "Q1", "reason": "r",
             "generated": c.fingerprint(long_gen), "live": c.fingerprint(long_live)}
    unexpected, suppressed, drift, stale = c.match_expected("W1", diffs, [entry])
    assert (unexpected, drift, stale) == ([], [], []) and len(suppressed) == 1
    # бой сдвинулся
    _, _, drift, _ = c.match_expected("W1", [(diffs[0][0], long_gen, "c" * 300)], [entry])
    assert len(drift) == 1
    # расхождения больше нет — запись устарела
    _, _, _, stale = c.match_expected("W1", [], [entry])
    assert stale == [entry]
    # запись другого воркфлоу не гасит
    unexpected, *_ = c.match_expected("W2", diffs, [entry])
    assert unexpected == diffs


def test_fingerprint_keeps_short_values():
    assert c.fingerprint("x") == "x"
    fp = c.fingerprint("y" * 500)
    assert set(fp) == {"sha256_16", "len"} and fp["len"] == 502


def test_pairs_unique_and_expected_entries_reference_pairs():
    import json
    ids = [wid for _, wid, _, _ in c.PAIRS]
    assert len(ids) == len(set(ids))
    for e in json.loads(c.EXPECTED.read_text(encoding="utf-8"))["entries"]:
        assert e["id"] in ids and e["decision"] and e["reason"]


def test_input_is_not_mutated():
    w = wf()
    before = copy.deepcopy(w)
    c.normalize(w)
    assert w == before
