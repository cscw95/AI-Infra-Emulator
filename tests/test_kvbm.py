"""F33 KV cache tiers G1–G4 (Dynamo KVBM): capacities, offload cascade,
onboarding hits counted as usage cached_tokens, RouterEvent storage_tier,
kvbm_* metric names (prometheus_names.rs mod kvbm)."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "contracts"))
import schema_check as sc  # noqa: E402

OA = sc.load("openai_schemas.json")["schemas"]
KV = sc.load("dynamo_kv_event.json")["schema"]
KVBM = sc.load("dynamo_kvbm_names.json")
DEP = "dgd-llama-70b"
MODEL = "llama-3.3-70b-instruct"


def _gen(client, prefix, prompt=2000, **kw):
    r = client.post("/dynamo/v1/generate", json={
        "deployment": DEP, "prompt_tokens": prompt, "max_tokens": 8,
        "prefix_hash": prefix, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _tiers(client):
    b = client.get("/dynamo/v1/kv/tiers", params={"deployment": DEP}).json()
    return {t["tier"]: t for t in b["deployments"][0]["tiers"]}


def _cfg(client, **kw):
    r = client.put("/dynamo/v1/kv/tiers/config", json={"deployment": DEP, **kw})
    assert r.status_code == 200, r.text


def test_tiers_physical_capacity(client):
    b = client.get("/dynamo/v1/kv/tiers").json()
    assert b["tiers"] == ["G1", "G2", "G3", "G4"] and len(b["deployments"]) == 4
    t = _tiers(client)
    assert [t[k]["storage_tier"] for k in ("G1", "G2", "G3", "G4")] == \
        KV["properties"]["storage_tier"]["enum"]
    # 4 decode workers × TP4 = 16 GPUs; 288 GB × 0.9 − 70 GB/4 FP8 weights
    assert t["G1"]["unit"]["hbm_gb_per_gpu"] == 288.0 and t["G1"]["unit"]["gpus"] == 16
    assert t["G1"]["physical_gb"] == round(16 * (288 * 0.9 - 17.5), 1)
    assert t["G2"]["medium"] == "LPDDR5X" and t["G2"]["unit"]["lpddr5x_gb_per_vera"] == 1536
    assert t["G4"]["medium"] == "VAST Data" and t["G4"]["vast"]["protocol"] == "S3"
    assert t["G4"]["vast"]["cluster"].startswith("vast-")
    caps = [t[k]["capacity_blocks"] for k in ("G1", "G2", "G3", "G4")]
    assert all(c > 0 for c in caps) and caps == sorted(caps)   # each tier larger


def test_offload_to_host_then_onboard_counts_cached(client):
    _cfg(client, num_gpu_blocks_override=150)        # G1 holds one 2000-token prefix
    a = _gen(client, "pA")
    assert a["kv_hit_tier"] is None and a["usage"]["prompt_tokens_details"]["cached_tokens"] == 0
    assert _gen(client, "pA")["kv_hit_tier"] == "G1"
    _gen(client, "pB")                                # evicts pA → G2 + G4 (write-through on offload)
    t = _tiers(client)
    assert t["G1"]["entries"] == 1 and t["G2"]["entries"] == 1 and t["G4"]["entries"] == 1
    r = _gen(client, "pA")
    assert r["kv_hit_tier"] == "G2" and r["kv_hit_tokens"] == 2000
    assert r["usage"]["prompt_tokens_details"]["cached_tokens"] == 2000
    assert r["kv_onboard_ms"] > 0 and r["prefill_ms"] == 0
    sc.check(r["usage"], OA["CompletionUsage"])
    v = client.get("/dynamo/v1/kv/tiers", params={"deployment": DEP}).json()["deployments"][0]
    assert v["counters"]["offload_blocks_d2h"] >= 125 and v["counters"]["onboard_blocks_h2d"] == 125
    assert v["matched_tokens"] == 4000 and t["G2"]["hit_rate"] == 0.0


def test_cascade_host_to_disk_and_object(client):
    _cfg(client, num_gpu_blocks_override=150, DYN_KVBM_CPU_CACHE_OVERRIDE_NUM_BLOCKS=150)
    for p in ("p1", "p2", "p3"):                      # p1: G1→G2→G3
        _gen(client, p)
    t = _tiers(client)
    assert (t["G1"]["entries"], t["G2"]["entries"], t["G3"]["entries"]) == (1, 1, 1)
    r = _gen(client, "p1")
    assert r["kv_hit_tier"] == "G3"
    # disable host/disk: the shared VAST object tier still serves
    _cfg(client, num_gpu_blocks_override=150, DYN_KVBM_CPU_CACHE_OVERRIDE_NUM_BLOCKS=0,
         DYN_KVBM_DISK_CACHE_OVERRIDE_NUM_BLOCKS=0)
    _gen(client, "p4")
    r = _gen(client, "p2")
    assert r["kv_hit_tier"] == "G4" and r["usage"]["prompt_tokens_details"]["cached_tokens"] == 2000
    g3 = _gen(client, "p3", prompt=2000)              # p3 only in G4 now
    assert g3["kv_hit_tier"] == "G4"
    # onboarding cost: G2 < G3 < G4 for the same 2000 tokens
    assert r["kv_onboard_ms"] > 2.0


def test_object_tier_disabled(client):
    _cfg(client, num_gpu_blocks_override=150, DYN_KVBM_CPU_CACHE_OVERRIDE_NUM_BLOCKS=0,
         DYN_KVBM_DISK_CACHE_OVERRIDE_NUM_BLOCKS=0, DYN_KVBM_OBJECT_ENABLED=False)
    _gen(client, "q1")
    _gen(client, "q2")
    assert _gen(client, "q1")["kv_hit_tier"] is None
    assert _tiers(client)["G4"]["enabled"] is False


def test_chat_completion_cached_tokens_after_offload(client):
    sysmsg = {"role": "system", "content": "NeoCloud KV tier policy. " * 400}
    other = {"role": "system", "content": "Another long system prompt. " * 400}

    def chat(s):
        r = client.post("/v1/chat/completions", json={
            "model": MODEL, "max_tokens": 4,
            "messages": [s, {"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        return r.json()["usage"]
    chat(sysmsg)
    pre = chat(sysmsg)["prompt_tokens_details"]["cached_tokens"]
    assert pre > 0
    _cfg(client, num_gpu_blocks_override=pre // 16 + 10)
    chat(other)                                       # evicts sysmsg prefix to G2
    u = chat(sysmsg)
    sc.check(u, OA["CompletionUsage"])
    assert u["prompt_tokens_details"]["cached_tokens"] == pre
    assert "cached_tokens" not in u                   # no flat copy (upstream shape)


def test_kv_events_carry_storage_tier(client):
    _cfg(client, num_gpu_blocks_override=150)
    _gen(client, "e1")
    _gen(client, "e2")
    ev = client.get("/dynamo/v1/kv-events", params={"deployment": DEP}).json()["events"]
    tiers = {e["router_event"]["storage_tier"] for e in ev}
    assert {"device", "host_pinned", "external"} <= tiers
    for e in ev:
        sc.check(e["router_event"], KV)
    host = [e for e in ev if e["router_event"]["storage_tier"] == "host_pinned"]
    assert "stored" in host[0]["router_event"]["event"]["data"]


def test_kvbm_metric_names_are_upstream(client):
    _cfg(client, num_gpu_blocks_override=150)
    _gen(client, "m1")
    _gen(client, "m2")
    t = client.get("/dynamo/v1/metrics").text
    fams = {ln.split("{")[0].split(" ")[0] for ln in t.splitlines()
            if ln.startswith("kvbm_")}
    up = {KVBM["prefix"] + n for n in KVBM["names"]}
    assert fams and fams <= up
    assert re.search(rf'kvbm_offload_blocks_d2h\{{dynamo_namespace="{DEP}"[^}}]*\}} 125', t)
    assert 'neocloud_kvbm_tier_used_blocks{' in t


def test_clear_kv_blocks_clears_all_tiers(client):
    _cfg(client, num_gpu_blocks_override=150)
    _gen(client, "c1")
    _gen(client, "c2")
    assert client.post("/clear_kv_blocks").status_code == 200
    t = _tiers(client)
    assert all(t[k]["entries"] == 0 for k in t)


def test_config_validation(client):
    r = client.put("/dynamo/v1/kv/tiers/config", json={"DYN_KVBM_BOGUS": 1})
    assert r.status_code == 422
    r = client.put("/dynamo/v1/kv/tiers/config", json={"deployment": "nope"})
    assert r.status_code == 404
    r = client.put("/dynamo/v1/kv/tiers/config", json={"num_gpu_blocks_override": -1})
    assert r.status_code == 422
    # global config applies to every deployment, reset by PUT {}
    assert client.put("/dynamo/v1/kv/tiers/config",
                      json={"DYN_KVBM_CPU_CACHE_GB": 10}).status_code == 200
    b = client.get("/dynamo/v1/kv/tiers").json()
    assert b["global_config"] == {"DYN_KVBM_CPU_CACHE_GB": 10.0}
    client.put("/dynamo/v1/kv/tiers/config", json={})
    assert client.get("/dynamo/v1/kv/tiers").json()["global_config"] == {}
