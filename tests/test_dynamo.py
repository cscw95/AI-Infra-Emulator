"""F01 engine side — Dynamo-style serving emulator: deployments, routing,
TTFT/TPOT, streaming, KV hits, DPS power coupling, metrics, usage."""
import json

DEP = "dgd-llama-70b"
RACK = "su-1-rack-00"


def _gen(client, **kw):
    body = {"deployment": DEP, "prompt_tokens": 2000, "max_tokens": 64, **kw}
    r = client.post("/dynamo/v1/generate", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_seeded_deployments_and_models(client):
    ds = client.get("/dynamo/v1/deployments").json()["deployments"]
    ids = {d["id"] for d in ds}
    assert {DEP, "dgd-qwen3-235b", "dgd-deepseek-r1", "dgd-kanana-8b"} <= ids
    d = next(x for x in ds if x["id"] == DEP)
    assert d["kind"] == "DynamoGraphDeployment" and d["mode"] == "disagg"
    assert d["state"] == "Ready" and d["gpus"] == 24 and d["tp"] == 4
    roles = [w["role"] for w in d["workers"]]
    assert roles.count("prefill") == 2 and roles.count("decode") == 4
    # no GPU is shared between deployments
    gpus = [g for x in ds for w in x["workers"] for g in w["gpus"]]
    assert len(gpus) == len(set(gpus))
    ms = client.get("/dynamo/v1/models").json()["data"]
    m = next(x for x in ms if x["id"] == "llama-3.3-70b-instruct")
    assert m["owned_by"] == "neocloud" and m["tiers"] == ["interactive", "batch"]
    assert m["context_length"] == 131072 and DEP in m["deployments"]


def test_create_patch_delete_deployment(client):
    r = client.post("/dynamo/v1/deployments", json={
        "model": "qwen3-235b-a22b", "prefill_workers": 1, "decode_workers": 2,
        "racks": ["su-1-rack-02"], "mode": "disagg"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["gpus"] == 24 and all(w["rack"] == "su-1-rack-02" for w in d["workers"])
    r = client.patch(f"/dynamo/v1/deployments/{d['id']}", json={"decode_workers": 4})
    assert r.status_code == 200 and r.json()["decode_workers"] == 4
    assert r.json()["gpus"] == 40
    # 72 GPUs per rack: 9 TP8 workers max → 10 is rejected, state kept
    r = client.patch(f"/dynamo/v1/deployments/{d['id']}", json={"decode_workers": 9})
    assert r.status_code == 409
    assert client.get(f"/dynamo/v1/deployments/{d['id']}").json()["decode_workers"] == 4
    assert client.delete(f"/dynamo/v1/deployments/{d['id']}").status_code == 200
    assert client.get(f"/dynamo/v1/deployments/{d['id']}").status_code == 404


def test_create_validation(client):
    base = {"model": "llama-3.3-70b-instruct", "racks": ["su-1-rack-03"]}
    assert client.post("/dynamo/v1/deployments", json={**base, "model": "nope"}).status_code == 404
    assert client.post("/dynamo/v1/deployments", json={**base, "racks": ["x"]}).status_code == 404
    assert client.post("/dynamo/v1/deployments", json={**base, "mode": "x"}).status_code == 422
    assert client.post("/dynamo/v1/deployments",
                       json={**base, "prefill_workers": 0}).status_code == 422
    r = client.post("/dynamo/v1/deployments", json={**base, "mode": "agg",
                                                     "decode_workers": 2})
    assert r.status_code == 201 and r.json()["prefill_workers"] == 0
    assert client.post("/dynamo/v1/deployments",
                       json={**base, "load_rps": -1}).status_code == 422


def test_generate_latency_and_usage_fields(client):
    g = _gen(client)
    assert g["prompt_tokens"] == 2000 and g["completion_tokens"] == 64
    assert g["total_tokens"] == 2064 and g["cached_tokens"] == 0
    assert 0 < g["ttft_ms"] < 2000 and 0 < g["tpot_ms"] < 50
    assert g["worker"]["prefill"].startswith(DEP + "-prefill")
    assert g["worker"]["decode"].startswith(DEP + "-decode")
    assert g["kv_transfer_ms"] > 0                      # disagg → NIXL hop
    assert g["e2e_ms"] > g["ttft_ms"]
    r = client.post("/dynamo/v1/generate", json={"model": "deepseek-r1",
                                                 "prompt_tokens": 10, "max_tokens": 100})
    assert r.json()["reasoning_tokens"] == 60 and r.json()["kv_transfer_ms"] == 0


def test_generate_validation(client):
    p = "/dynamo/v1/generate"
    assert client.post(p, json={"deployment": "nope", "prompt_tokens": 1}).status_code == 404
    assert client.post(p, json={"model": "nope", "prompt_tokens": 1}).status_code == 404
    assert client.post(p, json={"prompt_tokens": 1}).status_code == 422
    assert client.post(p, json={"deployment": DEP, "prompt_tokens": -1}).status_code == 422
    assert client.post(p, json={"deployment": DEP, "prompt_tokens": 1,
                                "max_tokens": 0}).status_code == 422


def test_kv_prefix_hit_lowers_ttft(client):
    a = _gen(client, prefix_hash="sys-prompt-1", request_id="r-a")
    b = _gen(client, prefix_hash="sys-prompt-1", request_id="r-b")
    assert a["kv_hit_tokens"] == 0 and b["kv_hit_tokens"] == 2000
    assert b["cached_tokens"] == 2000
    assert b["worker"]["decode"] == a["worker"]["decode"]   # KV-aware routing
    assert b["ttft_ms"] < a["ttft_ms"]


def test_request_id_idempotent(client):
    a = _gen(client, request_id="dup-1")
    b = _gen(client, request_id="dup-1")
    assert b["replayed"] is True and b["ttft_ms"] == a["ttft_ms"]
    rows = client.get(f"/dynamo/v1/usage?deployment={DEP}").json()["records"]
    assert [r["request_id"] for r in rows].count("dup-1") == 1
    assert rows[0]["source"] == "engine"


def test_stream_sse_tokens_then_usage(client):
    r = client.post("/dynamo/v1/generate", json={
        "deployment": DEP, "prompt_tokens": 100, "max_tokens": 5, "stream": True})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-request-id"]
    evs = [l[6:] for l in r.text.splitlines() if l.startswith("data: ")]
    assert evs[-1] == "[DONE]"
    body = [json.loads(e) for e in evs[:-1]]
    assert body[0]["type"] == "start"
    toks = [e for e in body if e["type"] == "token"]
    assert len(toks) == 5 and toks[1]["t_ms"] > toks[0]["t_ms"]
    done = body[-1]
    assert done["type"] == "done" and done["completion_tokens"] == 5
    assert done["total_tokens"] == 105


def test_power_cap_lowers_perf_and_raises_tpot(client):
    """F01 acceptance: lowering the rack power cap (DPS) slows generation."""
    before = _gen(client, request_id="pw-1")
    r = client.post(f"/emulator/v1/obs/racks/{RACK}/control",
                    json={"action": "power_cap", "params": {"cap_kw": 100}})
    assert r.status_code == 200
    after = _gen(client, request_id="pw-1b")
    assert after["perf"] < before["perf"]
    assert after["tpot_ms"] > before["tpot_ms"] * 1.2
    s = client.get(f"/dynamo/v1/stats?deployment={DEP}").json()["deployments"][0]
    assert s["power"][0]["throttled"] is True and s["power"][0]["cap_kw"] == 100
    assert s["capacity_tokens_per_s"] < 21333


def test_dps_domain_limit_couples(client):
    doms = client.get("/dps/v1/power-domains").json()["items"]
    dom = next(d for d in doms if RACK in str(client.get(
        f"/dps/v1/power-domains/{d['id']}").json()))
    s0 = client.get(f"/dynamo/v1/stats?deployment={DEP}").json()["deployments"][0]
    r = client.patch(f"/dps/v1/power-domains/{dom['id']}",
                     json={"operating_limit_kw": 500})
    assert r.status_code == 200, r.text
    s1 = client.get(f"/dynamo/v1/stats?deployment={DEP}").json()["deployments"][0]
    assert s1["power"][0]["cap_kw"] < s0["power"][0]["cap_kw"]
    assert s1["power_kw"] < s0["power_kw"]
    assert s1["expected"]["tpot_ms"] > s0["expected"]["tpot_ms"]
    # capping trades speed for efficiency: tokens/s per MW does not fall
    assert s1["capacity_tokens_per_s_per_mw"] >= s0["capacity_tokens_per_s_per_mw"]


def test_stats_slo_shape(client):
    s = client.get(f"/dynamo/v1/stats?deployment={DEP}").json()
    d = s["deployments"][0]
    assert d["source"] == "model" and d["ttft_p99_ms"] > 0
    assert s["target"] == {"ttft_p99_ms": 2000.0, "tpot_p99_ms": 50.0}
    for _ in range(5):
        _gen(client)
    d = client.get(f"/dynamo/v1/stats?deployment={DEP}").json()["deployments"][0]
    assert d["source"] == "observed" and d["requests"] == 5
    assert d["breach"] is False and d["tokens_per_s_per_mw"] > 0
    assert client.get("/dynamo/v1/stats?deployment=nope").status_code == 404


def test_overload_queues_and_breaches(client):
    client.patch(f"/dynamo/v1/deployments/{DEP}", json={"load_rps": 5000})
    g = _gen(client)
    assert g["queue_ms"] > 1000
    d = client.get(f"/dynamo/v1/stats?deployment={DEP}").json()["deployments"][0]
    assert d["utilization"] > 1 and d["breach"] is True


def test_metrics_prometheus(client):
    _gen(client, prefix_hash="m1")
    _gen(client, prefix_hash="m1")
    t = client.get("/dynamo/v1/metrics").text
    for name in ("dynamo_frontend_time_to_first_token_seconds_bucket",
                 "dynamo_frontend_inter_token_latency_seconds_count",
                 "dynamo_frontend_request_duration_seconds_sum",
                 "dynamo_frontend_requests_total", "dynamo_frontend_output_tokens_total",
                 "dynamo_frontend_inflight_requests", "dynamo_frontend_queued_requests",
                 "dynamo_component_router_kv_hit_rate", "dynamo_operator_resources_total",
                 "neocloud_dynamo_tokens_per_second_per_mw", "neocloud_dynamo_power_kw"):
        assert name in t, name
    assert (f'dynamo_frontend_requests_total{{dynamo_namespace="{DEP}",'
            f'model="llama-3.3-70b-instruct",status="success"}} 2') in t
    assert (f'dynamo_component_router_kv_hit_rate{{dynamo_namespace="{DEP}",'
            f'model="llama-3.3-70b-instruct"}} 0.5') in t


def test_reset_reseeds(client):
    client.delete(f"/dynamo/v1/deployments/{DEP}")
    client.post("/emulator/v1/reset")
    from app.store import STORE
    STORE.reset()
    assert client.get(f"/dynamo/v1/deployments/{DEP}").status_code == 200
