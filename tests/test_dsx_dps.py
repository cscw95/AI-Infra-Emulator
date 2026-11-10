"""DSX MaxLPS / DPS (nvidia.dcpower.v1 shape) and the DSX Exchange bus."""
import json

from app import dps
from app import observability as obs

RACK = "su-1-rack-00"


def _tele(client, did="pd-gasan"):
    return client.get(f"/dps/v1/telemetry?domain={did}").json()["domains"][0]


def _racks(dom):
    return [r for g in dom["groups"] for r in g["racks"]]


def test_seeded_domains_and_groups(client):
    ds = client.get("/dps/v1/power-domains").json()["items"]
    ids = {d["id"] for d in ds}
    assert "pd-gasan" in ids
    d = client.get("/dps/v1/power-domains/pd-gasan").json()
    assert d["mode"] == "static" and d["operating_limit_kw"] > 0
    assert {g["id"] for g in d["groups"]} >= {"rg-su-1"}
    gs = client.get("/dps/v1/resource-groups?domain=pd-gasan").json()["items"]
    assert all(g["domain"] == "pd-gasan" for g in gs)
    assert client.get("/dps/v1/power-domains/nope").status_code == 404


def test_patch_domain_validation_and_publish(client):
    assert client.patch("/dps/v1/power-domains/pd-gasan",
                        json={"mode": "turbo"}).status_code == 422
    assert client.patch("/dps/v1/power-domains/pd-gasan",
                        json={"operating_limit_kw": 0}).status_code == 422
    d = client.patch("/dps/v1/power-domains/pd-gasan",
                     json={"mode": "dynamic", "smoothing": True,
                           "operating_limit_kw": 2000}).json()
    assert d["mode"] == "dynamic" and d["smoothing"] is True
    assert d["allocated_kw"] <= 2000.0 + 0.5
    evs = client.get("/exchange/v1/events?stream=false&topic=power").json()
    assert evs[-1]["type"] == "PowerDomainUpdated"


def test_static_vs_dynamic_allocation(client):
    client.patch("/dps/v1/power-domains/pd-gasan", json={"mode": "static"})
    st = _tele(client)
    caps = {round(r["cap_kw"], 1) for r in _racks(st)}
    assert len(caps) == 1                       # equal fixed share
    assert st["measured_kw"] <= st["effective_limit_kw"] + 0.5
    client.patch("/dps/v1/power-domains/pd-gasan", json={"mode": "dynamic"})
    dy = _tele(client)
    assert dy["measured_kw"] >= st["measured_kw"] - 0.5       # steering ≥ static
    assert dy["throttled_racks"] <= st["throttled_racks"]
    assert dy["allocated_kw"] <= dy["effective_limit_kw"] + 0.5
    for r in _racks(dy):
        assert r["measured_kw"] <= r["cap_kw"] + 0.01
        assert r["cap_kw"] <= r["nameplate_kw"] + 0.01
    # enforcement pushed caps into the rack control state
    assert obs.ENGINE.rack_ctrl[RACK]["cap_source"] in ("dps", "manual")


def test_priority_group_wins_under_tight_limit(client):
    client.patch("/dps/v1/power-domains/pd-gasan",
                 json={"mode": "dynamic", "operating_limit_kw": 1200})
    g = client.post("/dps/v1/resource-groups",
                    json={"domain": "pd-gasan", "name": "gold",
                          "racks": [RACK], "priority": 100}).json()
    assert g["racks"] == [RACK]
    dom = _tele(client)
    gold = next(x for x in dom["groups"] if x["group_id"] == g["id"])["racks"][0]
    assert not gold["throttled"]
    assert any(r["throttled"] for r in _racks(dom) if r["rack_id"] != RACK)
    bad = client.post("/dps/v1/resource-groups",
                      json={"domain": "pd-gasan", "name": "x",
                            "racks": ["su-4-rack-00"], "priority": 1})
    assert bad.status_code in (404, 422)


def test_group_max_kw_bounds(client):
    client.patch("/dps/v1/power-domains/pd-gasan", json={"mode": "dynamic"})
    g = client.post("/dps/v1/resource-groups",
                    json={"domain": "pd-gasan", "name": "capped",
                          "racks": [RACK, "su-1-rack-01"], "max_kw": 100}).json()
    grp = next(x for x in _tele(client)["groups"] if x["group_id"] == g["id"])
    assert grp["cap_kw"] <= 100.5
    assert client.post("/dps/v1/resource-groups",
                       json={"domain": "pd-gasan", "name": "bad", "racks": [],
                             "min_kw": 50, "max_kw": 10}).status_code == 422


def test_create_domain_moves_racks(client):
    d = client.post("/dps/v1/power-domains",
                    json={"name": "hall-B", "operating_limit_kw": 500,
                          "mode": "dynamic", "racks": [RACK, "su-1-rack-01"]})
    assert d.status_code == 201
    did = d.json()["id"]
    assert d.json()["racks"] == 2
    gasan = client.get("/dps/v1/power-domains/pd-gasan").json()
    assert all(RACK not in g["racks"] for g in gasan["groups"])
    assert _tele(client, did)["allocated_kw"] <= 500.5


def test_profiles_and_rack_profile(client):
    names = {p["name"] for p in client.get("/dps/v1/profiles").json()["items"]}
    assert names == {"max-p", "max-q", "training", "inference", "balanced"}
    before = client.get(f"/dps/v1/telemetry?rack={RACK}").json()
    b = before["domains"][0]["groups"][0]["racks"][0]
    r = client.post(f"/dps/v1/racks/{RACK}/profile", json={"profile": "max-q"})
    assert r.status_code == 200 and r.json()["profile"] == "max-q"
    a = client.get(f"/dps/v1/telemetry?rack={RACK}").json()["domains"][0]["groups"][0]["racks"][0]
    assert a["profile"] == "max-q" and a["nameplate_kw"] < b["nameplate_kw"]
    assert client.post(f"/dps/v1/racks/{RACK}/profile",
                       json={"profile": "warp"}).status_code == 422
    assert client.post("/dps/v1/racks/nope/profile",
                       json={"profile": "max-q"}).status_code == 404


def test_telemetry_gpu_level(client):
    t = client.get(f"/dps/v1/telemetry?rack={RACK}").json()
    rack = t["domains"][0]["groups"][0]["racks"][0]
    assert len(rack["gpus"]) == 72
    g = rack["gpus"][0]
    assert g["measured_w"] <= g["cap_w"] and g["headroom_w"] >= 0
    all_t = client.get("/dps/v1/telemetry").json()
    assert len(all_t["domains"]) >= 1
    assert "gpus" not in _racks(all_t["domains"][0])[0]


def test_simulate_static_vs_dynamic(client):
    r = client.post("/dps/v1/simulate",
                    json={"domain": "pd-gasan", "operating_limit_kw": 10000,
                          "workload_mix": {"training": 2, "inference": 6, "idle": 2},
                          "smoothing": True})
    assert r.status_code == 200
    s = r.json()
    for case in ("static", "dynamic"):
        for k in ("racks_supported", "gpus_supported", "tokens_per_mw", "headroom_kw"):
            assert k in s[case]
    assert s["dynamic"]["racks_supported"] > s["static"]["racks_supported"]
    assert s["dynamic"]["tokens_per_mw"] > s["static"]["tokens_per_mw"]
    assert s["delta"]["gpus_pct"] > 0
    assert abs(sum(s["inputs"]["workload_mix"].values()) - 1) < 1e-3
    no_smooth = client.post("/dps/v1/simulate",
                            json={"operating_limit_kw": 10000, "smoothing": False,
                                  "workload_mix": {"inference": 1}}).json()
    smooth = client.post("/dps/v1/simulate",
                         json={"operating_limit_kw": 10000, "smoothing": True,
                               "workload_mix": {"inference": 1}}).json()
    assert smooth["dynamic"]["racks_supported"] >= no_smooth["dynamic"]["racks_supported"]
    assert client.post("/dps/v1/simulate", json={}).status_code == 422
    assert client.post("/dps/v1/simulate",
                       json={"operating_limit_kw": 1000,
                             "workload_mix": {"gaming": 1}}).status_code == 422


def test_flex_event_lowers_limit_and_expires(client, monkeypatch):
    d0 = client.get("/dps/v1/power-domains/pd-gasan").json()
    r = client.post("/dps/v1/flex-events",
                    json={"domain": "pd-gasan", "reduce_kw": 300, "duration_s": 60})
    assert r.status_code == 201
    f = r.json()
    assert f["status"] == "active"
    assert f["domain_after"]["effective_limit_kw"] == round(d0["operating_limit_kw"] - 300, 1)
    dom = _tele(client)
    assert dom["allocated_kw"] <= dom["effective_limit_kw"] + 0.5
    grid = client.get("/exchange/v1/events?stream=false&topic=grid").json()
    assert grid[-1]["type"] == "FlexEventStarted"
    # jump past the end of the event
    real = dps.time.time
    monkeypatch.setattr(dps.time, "time", lambda: real() + 120)
    items = client.get("/dps/v1/flex-events?domain=pd-gasan").json()["items"]
    assert items[0]["status"] == "completed"
    d1 = client.get("/dps/v1/power-domains/pd-gasan").json()
    assert d1["effective_limit_kw"] == d0["operating_limit_kw"]
    grid = client.get("/exchange/v1/events?stream=false&topic=grid").json()
    assert grid[-1]["type"] == "FlexEventCompleted"
    assert client.post("/dps/v1/flex-events",
                       json={"domain": "pd-gasan", "reduce_kw": 10 ** 9,
                             "duration_s": 60}).status_code == 422
    assert client.post("/dps/v1/flex-events",
                       json={"domain": "nope", "reduce_kw": 1}).status_code == 404


def test_operator_cap_adopted_as_manual(client):
    client.post(f"/emulator/v1/obs/racks/{RACK}/control",
                json={"action": "power_cap", "params": {"cap_kw": 50}})
    rack = client.get(f"/dps/v1/telemetry?rack={RACK}").json()["domains"][0]["groups"][0]["racks"][0]
    assert rack["manual_cap_kw"] == 50.0 and rack["cap_kw"] <= 50.0


# ── DSX Exchange ──────────────────────────────────────────────────────
def test_exchange_topics_and_sse(client):
    t = client.get("/exchange/v1/topics").json()
    assert {x["name"] for x in t["topics"]} == {"power", "thermal", "leak", "grid", "health"}
    client.post(f"/dps/v1/racks/{RACK}/profile", json={"profile": "inference"})
    client.post("/emulator/v1/oob/inject", json={"kind": "leak", "target": RACK})
    with client.stream("GET", "/exchange/v1/events?follow=false") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    frames = [f for f in body.split("\n\n") if f.startswith("id:")]
    assert len(frames) >= 2
    data = [json.loads(f.split("data: ", 1)[1]) for f in frames]
    assert {"power", "leak"} <= {d["topic"] for d in data}
    since = data[0]["id"]
    rest = client.get(f"/exchange/v1/events?stream=false&since={since}").json()
    assert all(e["id"] > since for e in rest)
    leak_only = client.get("/exchange/v1/events?stream=false&topic=leak").json()
    assert leak_only and all(e["topic"] == "leak" for e in leak_only)
    assert client.get("/exchange/v1/events?topic=bogus").status_code == 422
    t = client.get("/exchange/v1/topics").json()
    assert next(x for x in t["topics"] if x["name"] == "leak")["published"] >= 1


def test_exchange_sse_follow_timeout(client):
    with client.stream("GET", "/exchange/v1/events?follow=true&timeout_s=0.6") as r:
        body = "".join(r.iter_text())
    assert body == "" or body.startswith((":", "id:"))
