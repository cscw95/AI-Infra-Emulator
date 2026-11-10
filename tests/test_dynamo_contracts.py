"""F01 contract tests — Dynamo emulator responses checked against the upstream
schemas extracted into tests/contracts/ (scripts/upstream/extract_contracts.py):
OpenAI openapi.yaml (CompletionUsage, chat/completion responses and chunks,
models, ErrorResponse), Dynamo kv-router RouterEvent/KvCacheEvent, Dynamo
prometheus_names.rs metric names, and the DGD CRD nvidia.com/v1beta1."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "contracts"))
import schema_check as sc  # noqa: E402

OA = sc.load("openai_schemas.json")["schemas"]
KV = sc.load("dynamo_kv_event.json")["schema"]
DGD = sc.load("dynamo_dgd_v1beta1.json")
NAMES = set(sc.load("dynamo_metric_names.json")["names"])
# operator metric (Go, deploy/operator) — not in prometheus_names.rs
OPERATOR_NAMES = {"dynamo_operator_resources_total"}
MODEL = "llama-3.3-70b-instruct"
SYS = {"role": "system", "content": "You are the NeoCloud token-factory assistant. " * 20}


def _chat(client, content="How many GPUs are in an NVL72 rack?", **kw):
    body = {"model": MODEL, "messages": [SYS, {"role": "user", "content": content}],
            "max_tokens": 16, **kw}
    return client.post("/v1/chat/completions", json=body)


def _chunks(text):
    out = [ln[6:] for ln in text.split("\n") if ln.startswith("data: ")]
    return [c if c == "[DONE]" else json.loads(c) for c in out]


def test_chat_completion_matches_openai(client):
    r = _chat(client)
    assert r.status_code == 200 and r.headers["x-request-id"]
    b = r.json()
    sc.check(b, OA["CreateChatCompletionResponse"])
    sc.check(b["usage"], OA["CompletionUsage"])
    assert set(b["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens",
                               "prompt_tokens_details", "completion_tokens_details"}
    assert b["choices"][0]["finish_reason"] == "length"
    assert b["usage"]["completion_tokens"] == 16


def test_chat_stream_chunks_and_usage(client):
    r = _chat(client, stream=True, stream_options={"include_usage": True})
    assert "text/event-stream" in r.headers["content-type"]
    cs = _chunks(r.text)
    assert cs[-1] == "[DONE]"
    objs = cs[:-1]
    for c in objs:
        sc.check(c, OA["CreateChatCompletionStreamResponse"])
    assert objs[0]["choices"][0]["delta"]["role"] == "assistant"
    assert objs[-2]["choices"][0]["finish_reason"] == "length"
    assert objs[-1]["choices"] == [] and objs[-1]["usage"]["completion_tokens"] == 16
    assert len({c["id"] for c in objs}) == 1
    # without include_usage there is no usage chunk
    cs = _chunks(_chat(client, stream=True).text)
    assert all("usage" not in c for c in cs[:-1])


def test_system_prompt_hits_kv_cache(client):
    a = _chat(client, "first question").json()["usage"]
    b = _chat(client, "second, different question").json()["usage"]
    assert a["prompt_tokens_details"]["cached_tokens"] == 0
    cached = b["prompt_tokens_details"]["cached_tokens"]
    assert 0 < cached < b["prompt_tokens"]


def test_reasoning_model_details(client):
    r = client.post("/v1/chat/completions", json={
        "model": "deepseek-r1", "max_tokens": 50,
        "messages": [{"role": "user", "content": "why?"}]}).json()
    sc.check(r, OA["CreateChatCompletionResponse"])
    assert r["usage"]["completion_tokens_details"]["reasoning_tokens"] == 30
    assert r["choices"][0]["message"]["reasoning_content"]


def test_completions_and_models(client):
    r = client.post("/v1/completions", json={"model": MODEL, "prompt": "Hello", "max_tokens": 8})
    sc.check(r.json(), OA["CreateCompletionResponse"])
    cs = _chunks(client.post("/v1/completions", json={
        "model": MODEL, "prompt": "Hello", "max_tokens": 4, "stream": True,
        "stream_options": {"include_usage": True}}).text)
    assert cs[-1] == "[DONE]" and cs[-2]["usage"]["completion_tokens"] == 4
    for c in cs[:-2]:
        sc.check(c, OA["CreateCompletionResponse"])
    m = client.get("/v1/models").json()
    sc.check(m, OA["ListModelsResponse"])
    for x in m["data"]:
        sc.check(x, OA["Model"])
    assert MODEL in {x["id"] for x in m["data"]}


def test_errors_are_openai_error_response(client):
    r = client.post("/v1/chat/completions", json={
        "model": "nope", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 404
    sc.check(r.json(), OA["ErrorResponse"])
    assert r.json()["error"]["code"] == "model_not_found"
    d = client.get("/dynamo/v1/deployments/dgd-qwen3-235b").json()
    for w in d["workers"]:
        if w["role"] == "decode":
            tray, g = w["gpus"][0].rsplit("-g", 1)
            client.post("/emulator/v1/gpu-faults/inject", json={
                "kind": "xid", "tray_id": tray[4:], "gpu": int(g), "xid": 43})
    r = client.post("/v1/chat/completions", json={
        "model": "qwen3-235b-a22b", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 503
    sc.check(r.json(), OA["ErrorResponse"])


def test_engine_generate_and_usage_records(client):
    g = client.post("/dynamo/v1/generate", json={
        "deployment": "dgd-llama-70b", "prompt_tokens": 100, "max_tokens": 4}).json()
    sc.check(g["usage"], OA["CompletionUsage"])
    recs = client.get("/dynamo/v1/usage").json()["records"]
    assert recs
    for rec in recs:
        sc.check(rec["usage"], OA["CompletionUsage"])
        assert "cached_tokens" not in rec


def test_kv_events_router_event_shape(client):
    base = {"deployment": "dgd-llama-70b", "max_tokens": 2, "prefix_hash": "kv-1"}
    client.post("/dynamo/v1/generate", json={**base, "prompt_tokens": 40})
    client.post("/dynamo/v1/generate", json={**base, "prompt_tokens": 100})
    ev = client.get("/dynamo/v1/kv-events?deployment=dgd-llama-70b").json()
    assert ev["kv_block_size"] == 16
    stored = [e["router_event"] for e in ev["events"]
              if isinstance(e["router_event"]["event"]["data"], dict)]
    assert len(stored) == 2
    for e in ev["events"]:
        sc.check(e["router_event"], KV)
    a, b = (s["event"]["data"]["stored"] for s in stored)
    assert a["parent_hash"] is None and len(a["blocks"]) == 2          # 40 // 16
    assert b["parent_hash"] == a["blocks"][-1]["block_hash"]          # chain grows
    assert b["start_position"] == 2 and len(b["blocks"]) == 4         # 100 // 16 = 6
    assert stored[1]["event"]["event_id"] == stored[0]["event"]["event_id"] + 1
    # a fatal XID on the worker that holds the prefix → Cleared
    w = ev["events"][0]["worker"]
    dep = client.get("/dynamo/v1/deployments/dgd-llama-70b").json()
    gpu = next(x for x in dep["workers"] if x["id"] == w)["gpus"][0]
    tray, gi = gpu.rsplit("-g", 1)
    client.post("/emulator/v1/gpu-faults/inject", json={
        "kind": "xid", "tray_id": tray[4:], "gpu": int(gi), "xid": 79})
    ev2 = client.get(f"/dynamo/v1/kv-events?since={ev['next']}").json()["events"]
    assert [(e["worker"], e["router_event"]["event"]["data"]) for e in ev2] == [(w, "cleared")]
    sc.check(ev2[0]["router_event"], KV)
    assert client.get(f"/dynamo/v1/kv-events?since={ev['next']}").json()["count"] == 1


def test_metric_names_are_upstream(client):
    client.post("/v1/chat/completions", json={
        "model": MODEL, "messages": [{"role": "user", "content": "x"}], "max_tokens": 2})
    t = client.get("/dynamo/v1/metrics").text
    fams = {re.sub(r"_(bucket|sum|count)$", "", ln.split("{")[0].split(" ")[0])
            for ln in t.splitlines() if ln and not ln.startswith("#")}
    ours = {f for f in fams if f.startswith("dynamo_")}
    assert ours and ours - NAMES - OPERATOR_NAMES == set()
    assert {f for f in fams if not f.startswith(("dynamo_", "kvbm_"))} <= {
        f for f in fams if f.startswith("neocloud_")}     # kvbm_*: test_kvbm.py
    assert re.search(r'dynamo_frontend_requests_total\{[^}]*endpoint="chat_completions",'
                     r'request_type="unary",status="success",error_type=""\} 1', t)
    for lbl in re.findall(r'[{,]([A-Za-z_][A-Za-z0-9_]*)=', t):
        assert not lbl.startswith("__") and ":" not in lbl


def test_dgd_custom_resource_shape(client):
    lst = client.get("/apis/nvidia.com/v1beta1/namespaces/dynamo/dynamographdeployments").json()
    assert lst["kind"] == "DynamoGraphDeploymentList" and len(lst["items"]) == 4
    for o in lst["items"]:
        sc.check(o, DGD["schema"])
        assert o["apiVersion"] == "nvidia.com/v1beta1" and o["kind"] == DGD["kind"]
    o = client.get("/apis/nvidia.com/v1beta1/namespaces/dynamo/"
                   "dynamographdeployments/dgd-llama-70b").json()
    types = {c["type"]: c["replicas"] for c in o["spec"]["components"]}
    assert types == {"frontend": 1, "prefill": 2, "decode": 4}
    assert o["status"]["state"] == "successful"
    assert o["status"]["conditions"][0]["status"] == "True"
    # fault → component not ready → pending / Ready=False
    client.post("/emulator/v1/gpu-faults/scenarios/xid-fatal")
    o = client.get("/apis/nvidia.com/v1beta1/namespaces/dynamo/"
                   "dynamographdeployments/dgd-llama-70b").json()
    sc.check(o, DGD["schema"])
    assert o["status"]["state"] == "pending"
    assert o["status"]["conditions"][0]["status"] == "False"
    dec = o["status"]["components"]["VllmDecodeWorker"]
    assert dec["readyReplicas"] == dec["replicas"] - 1
    # DPS cap is visible as the per-GPU power limit
    client.post("/emulator/v1/obs/racks/su-1-rack-00/control",
                json={"action": "power_cap", "params": {"cap_kw": 100}})
    o = client.get("/apis/nvidia.com/v1beta1/namespaces/dynamo/"
                   "dynamographdeployments/dgd-llama-70b").json()
    sc.check(o, DGD["schema"])
    assert 0 < o["status"]["components"]["VllmDecodeWorker"]["gpuPowerLimitWatts"] < 3000


def test_dgd_create_from_manifest(client):
    p = "/apis/nvidia.com/v1beta1/namespaces/team-a/dynamographdeployments"
    man = {"apiVersion": "nvidia.com/v1beta1", "kind": "DynamoGraphDeployment",
           "metadata": {"name": "kanana-agg", "annotations": {"neocloud.io/racks": "su-1-rack-03"}},
           "spec": {"backendFramework": "vllm", "components": [
               {"name": "Frontend", "type": "frontend", "replicas": 1},
               {"name": "VllmWorker", "type": "worker", "replicas": 2,
                "modelRef": {"name": "kanana-1.5-8b-instruct"}}]}}
    r = client.post(p, json=man)
    assert r.status_code == 201, r.text
    sc.check(r.json(), DGD["schema"])
    d = client.get("/dynamo/v1/deployments/kanana-agg").json()
    assert d["mode"] == "agg" and d["decode_workers"] == 2 and d["namespace"] == "team-a"
    assert client.post(p, json=man).json()["reason"] == "AlreadyExists"
    r = client.get(p + "/nope")
    assert r.status_code == 404 and r.json()["kind"] == "Status" and r.json()["reason"] == "NotFound"
    assert client.post(p, json={**man, "apiVersion": "v1"}).status_code == 400
    assert client.delete(p + "/kanana-agg").json()["status"] == "Success"
    assert client.get("/dynamo/v1/deployments/kanana-agg").status_code == 404
