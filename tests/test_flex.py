"""F20 DSX Flex 1.7.0 — Load Target Set → Power State Status → Breach Alert →
Enforcement, with every emitted CloudEvent validated against the vendored
upstream AsyncAPI schema (tests/contracts/dsx-flex.yaml)."""
import base64
import json
import re
import uuid
from pathlib import Path

import pytest
import yaml

from app import flex

SPEC = yaml.safe_load((Path(__file__).parent / "contracts" / "dsx-flex.yaml").read_text())
SCHEMAS = SPEC["components"]["schemas"]
EVENT_SCHEMA = {"grid.loadtarget.set.v1": "LoadTargetSetEvent",
                "grid.powerstate.status.v1": "PowerStateStatusEvent",
                "grid.powerbreach.alert.v1": "PowerBreachAlertEvent",
                "grid.powerbreach.enforcement.v1": "PowerBreachEnforcementEvent"}
FEED = "pd-aif-stt-gasan-f1"


def _errors(inst, sch, path="$"):
    """Small JSON Schema subset validator ($ref, allOf, oneOf, type, required,
    properties, additionalProperties, items, minItems, enum, const, pattern,
    minimum, format uuid/date-time) — enough for the AsyncAPI payloads."""
    if "$ref" in sch:
        return _errors(inst, SCHEMAS[sch["$ref"].split("/")[-1]], path)
    errs = []
    for s in sch.get("allOf", []):
        errs += _errors(inst, s, path)
    if "oneOf" in sch:
        ok = [s for s in sch["oneOf"] if not _errors(inst, s, path)]
        if len(ok) != 1:
            errs.append(f"{path}: oneOf matched {len(ok)}")
    t = sch.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool,
             "integer": int, "number": (int, float), "null": type(None)}
    if t and not isinstance(inst, types[t]) or (t in ("number", "integer")
                                                 and isinstance(inst, bool)):
        return errs + [f"{path}: expected {t}"]
    if "const" in sch and inst != sch["const"]:
        errs.append(f"{path}: {inst!r} != const {sch['const']!r}")
    if "enum" in sch and inst not in sch["enum"]:
        errs.append(f"{path}: {inst!r} not in {sch['enum']}")
    if isinstance(inst, str):
        if "pattern" in sch and not re.search(sch["pattern"], inst):
            errs.append(f"{path}: {inst!r} !~ {sch['pattern']}")
        if sch.get("format") == "uuid":
            uuid.UUID(inst)
        if sch.get("format") == "date-time" and not re.match(r"^\d{4}-\d\d-\d\dT", inst):
            errs.append(f"{path}: not date-time")
    if isinstance(inst, (int, float)) and "minimum" in sch and inst < sch["minimum"]:
        errs.append(f"{path}: < minimum")
    if isinstance(inst, dict):
        for k in sch.get("required", []):
            if k not in inst:
                errs.append(f"{path}: missing {k}")
        for k, v in inst.items():
            if k in sch.get("properties", {}):
                errs += _errors(v, sch["properties"][k], f"{path}.{k}")
            elif isinstance(sch.get("additionalProperties"), dict):
                errs += _errors(v, sch["additionalProperties"], f"{path}.{k}")
    if isinstance(inst, list):
        if len(inst) < sch.get("minItems", 0):
            errs.append(f"{path}: minItems")
        if "items" in sch:
            for i, v in enumerate(inst):
                errs += _errors(v, sch["items"], f"{path}[{i}]")
    return errs


def _validate_all(client):
    msgs = client.get("/flex/v1/messages").json()["items"]
    for m in msgs:
        env = m["cloudevent"]
        errs = _errors(env, {"$ref": "#/" + EVENT_SCHEMA[env["type"]]})
        assert not errs, (env["type"], errs)
    return msgs


@pytest.fixture(autouse=True)
def _flex_reset():
    flex.FLEX.reset()
    yield
    flex.FLEX.reset()


def _types(msgs):
    return [m["cloudevent"]["type"] for m in msgs]


def test_load_target_set_status_and_envelope(client):
    st0 = client.get(f"/flex/v1/power-state-status?site={FEED}").json()
    assert st0["target_kw"] is None and st0["compliance"] == "ok"
    r = client.post("/flex/v1/load-target-sets",
                    json={"site": FEED, "reduce_kw": 300, "duration_s": 900})
    assert r.status_code == 201, r.text
    lt = r.json()
    env = lt["cloudevent"]
    assert env["specversion"] == "1.0.2"          # upstream const, not CloudEvents "1.0"
    assert env["source"] == "//grid/v1/isv/neocloud-ops"
    assert env["type"] == "grid.loadtarget.set.v1"
    assert lt["target_kw"] == pytest.approx(lt["baseline_kw"] - 300, abs=0.2)
    st = client.get(f"/flex/v1/power-state-status?site={FEED}").json()
    assert st["target_kw"] == pytest.approx(lt["target_kw"], abs=0.2)
    assert st["compliance"] == "breach"           # nobody shed load yet
    assert set(st) >= {"measured_kw", "target_kw", "compliance", "headroom_kw"}
    msgs = _validate_all(client)
    assert _types(msgs)[:2] == ["grid.loadtarget.set.v1", "grid.powerstate.status.v1"]
    status = msgs[1]["cloudevent"]
    assert msgs[1]["mqtt_topic"] == "grid/v1/dsx-flex-agent/maxlps/powerstate/status"
    assert status["data"]["feeds"][FEED]["event"] == "target_set"
    assert status["correlationid"] == lt["correlation_id"]


def test_sustained_breach_alert_then_enforcement_ok(client):
    lt = client.post("/flex/v1/load-target-sets",
                     json={"site": FEED, "reduce_kw": 400, "duration_s": 900}).json()
    client.post("/flex/v1/clock", json={"advance_s": 10})
    assert client.get("/flex/v1/breaches").json()["items"] == []   # < 30 s
    client.post("/flex/v1/clock", json={"advance_s": 25})
    msgs = _validate_all(client)
    types = _types(msgs)
    assert "grid.powerbreach.alert.v1" in types
    assert "grid.powerbreach.enforcement.v1" in types
    alert = next(m for m in msgs if m["cloudevent"]["type"] == "grid.powerbreach.alert.v1")
    a = alert["cloudevent"]["data"]
    assert a["status"] == "active" and a["feed_tag"] == FEED and a["shed_hints"]
    assert alert["mqtt_topic"] == "grid/v1/dsx-flex-agent/maxlps/powerbreach"
    enf = next(m for m in msgs if m["cloudevent"]["type"] == "grid.powerbreach.enforcement.v1")
    assert enf["mqtt_topic"] == "grid/v1/infra/ima-emu/powerbreach/enforcement"
    assert enf["cloudevent"]["data"]["action"] == "applied"
    assert enf["cloudevent"]["data"]["code"] == "success"
    st = client.get(f"/flex/v1/power-state-status?site={FEED}").json()
    assert st["compliance"] == "ok", st
    assert st["measured_kw"] <= lt["target_kw"] * 1.005 + 0.5
    # DPS carries the enforcement as a flex event on the domain
    dom = client.get(f"/dps/v1/power-domains/{FEED}").json()
    assert dom["flex_active"]
    # Exchange topic `grid` republishes the four message kinds
    ex = client.get("/exchange/v1/events?topic=grid&stream=false").json()
    ex_types = {e["type"] for e in (ex["items"] if isinstance(ex, dict) else ex)}
    assert {"LoadTargetSet", "PowerStateStatus", "PowerBreachAlert",
            "PowerBreachEnforcement"} <= ex_types


def test_target_end_reverts_enforcement_and_resolves(client):
    client.post("/flex/v1/load-target-sets",
                json={"site": FEED, "reduce_kw": 400, "duration_s": 120})
    client.post("/flex/v1/clock", json={"advance_s": 40})
    assert client.get(f"/dps/v1/power-domains/{FEED}").json()["flex_active"]
    client.post("/flex/v1/clock", json={"advance_s": 100})
    msgs = _validate_all(client)
    enf = [m["cloudevent"]["data"]["action"] for m in msgs
           if m["cloudevent"]["type"] == "grid.powerbreach.enforcement.v1"]
    assert enf[-1] == "reverted"
    alerts = [m["cloudevent"]["data"]["status"] for m in msgs
              if m["cloudevent"]["type"] == "grid.powerbreach.alert.v1"]
    assert alerts[-1] == "resolved"
    assert client.get(f"/dps/v1/power-domains/{FEED}").json()["flex_active"] == []
    st = client.get(f"/flex/v1/power-state-status?site={FEED}").json()
    assert st["target_kw"] is None and st["compliance"] == "ok"


def test_site_split_target_kw_and_clear(client):
    r = client.post("/flex/v1/load-target-sets",
                    json={"site": "gasan", "target_kw": 3000, "duration_s": 600,
                          "ramp_s": 60})
    assert r.status_code == 201, r.text
    lt = r.json()
    assert FEED in lt["feeds"] and all(f.startswith("pd-aif-stt-gasan") for f in lt["feeds"])
    assert sum(f["target_kw"] for f in lt["feeds"].values()) == pytest.approx(3000, abs=1)
    st = client.get("/flex/v1/power-state-status?site=gasan").json()
    assert st["feeds"]["pd-aif-stt-gasan-f1"]["in_flight"] is True   # ramping
    d = client.delete(f"/flex/v1/load-target-sets/{lt['id']}").json()
    assert d["state"] == "cleared"
    last = _validate_all(client)
    assert any(m["cloudevent"]["type"] == "grid.loadtarget.set.v1" and
               m["cloudevent"]["data"]["targets"][0]["load_constraint"] is None for m in last)


def test_raw_cloudevent_ingress_and_validation(client):
    good = {"specversion": "1.0.2", "id": str(uuid.uuid4()),
            "source": "//grid/v1/isv/acme-energy", "type": "grid.loadtarget.set.v1",
            "time": "2026-11-10T10:00:00Z", "datacontenttype": "application/json",
            "subject": "loadtarget", "correlationid": str(uuid.uuid4()),
            "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
            "data": {"targets": [{"interval": {}, "feed_tags": [FEED],
                                  "load_constraint": {"value": 2.5, "unit": "megawatt"},
                                  "strategy": {"best_effort": True}}]}}
    assert not _errors(good, {"$ref": "#/LoadTargetSetEvent"})
    r = client.post("/flex/v1/isv/acme-energy/loadtarget/set", json=good)
    assert r.status_code == 202, r.text
    lid = r.json()["load_target_sets"][0]
    assert client.get(f"/flex/v1/load-target-sets/{lid}").json()["target_kw"] == 2500.0
    # JWS compact data is decoded (signature not verified in the emulator)
    payload = base64.urlsafe_b64encode(json.dumps(
        {"targets": [{"interval": {}, "feed_tags": [FEED], "load_constraint": None}]}
    ).encode()).decode().rstrip("=")
    jws = {**good, "id": str(uuid.uuid4()), "data": f"eyJhbGciOiJFUzI1NiJ9.{payload}.c2ln"}
    r = client.post("/flex/v1/isv/acme-energy/loadtarget/set", json=jws).json()
    assert r["cleared"] == [lid] and r["jws_verified"] is False
    bad = {**good, "specversion": "1.0"}
    r = client.post("/flex/v1/isv/acme-energy/loadtarget/set", json=bad)
    assert r.status_code == 422 and "specversion" in r.text


def test_input_bounds(client):
    assert client.post("/flex/v1/load-target-sets",
                       json={"site": "nowhere", "reduce_kw": 1}).status_code == 404
    assert client.post("/flex/v1/load-target-sets",
                       json={"site": FEED}).status_code == 422
    assert client.post("/flex/v1/load-target-sets",
                       json={"site": FEED, "reduce_kw": 1e9}).status_code == 422
    assert client.post("/flex/v1/load-target-sets",
                       json={"site": FEED, "reduce_kw": 1, "duration_s": 0}).status_code == 422
    assert client.post("/flex/v1/clock", json={"advance_s": -1}).status_code == 422
