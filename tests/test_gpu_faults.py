"""GPU fault injection (XID / NVLink / thermal) — the F10 auto-remediation
source: Exchange events, DCGM, Redfish health, GpuReset outcome, Dynamo
worker impact, HW replace clears."""
from app import exchange

TRAY = "su-1-rack-00-tray-05"


def _inject(client, **kw):
    r = client.post("/emulator/v1/gpu-faults/inject", json={"tray_id": TRAY, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _gpu(client, uuid):
    gs = client.get("/emulator/v1/obs/dcgm/gpus?rack=su-1-rack-00&limit=100").json()["gpus"]
    return next(g for g in gs if g["gpu_uuid"] == uuid)


def test_xid_recoverable_reset_clears(client):
    f = _inject(client, kind="xid", xid=63, gpu=2)
    assert f["reset_recovers"] is True and f["recommended_action"] == "gpu_reset"
    assert f["gpu_uuid"] == f"GPU-{TRAY}-g2"
    ev = exchange.recent("health")[-1]
    assert ev["type"] == "GpuXidError" and ev["data"]["xid"] == 63
    assert ev["data"]["machine_id"] == TRAY and ev["data"]["fault_id"] == f["id"]
    g = _gpu(client, f["gpu_uuid"])
    assert g["xid_recent"] == [63] and g["state"] == "faulted"
    assert client.get(f"/redfish/v1/Systems/{TRAY}").json()["Status"]["Health"] == "Warning"
    r = client.post(f"/redfish/v1/Systems/{TRAY}/Actions/GpuReset", json={"GpuIds": [2]})
    assert r.json()["TaskState"] == "Completed"
    assert client.get(f"/emulator/v1/gpu-faults/{f['id']}").json()["state"] == "cleared"
    assert client.get(f"/redfish/v1/Systems/{TRAY}").json()["Status"]["Health"] == "OK"
    assert any(e["type"] == "GpuFaultCleared" for e in exchange.recent("health"))


def test_xid_fatal_reset_fails_then_replace_clears(client):
    f = _inject(client, kind="xid", xid=79, gpu=1)
    assert f["reset_recovers"] is False and f["recommended_action"] == "replace"
    assert client.get(f"/redfish/v1/Systems/{TRAY}").json()["Status"]["Health"] == "Critical"
    r = client.post(f"/redfish/v1/Systems/{TRAY}/Actions/GpuReset", json={})
    t = r.json()
    assert t["TaskState"] == "Exception" and t["Oem"]["NeoCloud"]["remaining"] == [f["id"]]
    assert exchange.recent("health")[-1]["type"] == "GpuResetFailed"
    # HW replace (trayops) swaps the board → new serial → fault cleared
    from app.store import STORE
    STORE.trays[TRAY].serial = "SN-new"
    assert client.get(f"/emulator/v1/gpu-faults/{f['id']}").json()["cleared_by"] == "hw_replaced"


def test_nvlink_and_thermal_telemetry(client):
    n = _inject(client, kind="nvlink", gpu=0)
    t = _inject(client, kind="thermal", gpu=3, delta_c=15)
    assert n["xid"] == 74 and t["recommended_action"] == "monitor"
    assert exchange.recent("thermal")[-1]["type"] == "GpuThermalThrottle"
    g0, g3 = _gpu(client, n["gpu_uuid"]), _gpu(client, t["gpu_uuid"])
    assert g0["xid_recent"] == [74]
    assert g3["state"] in ("throttled", "active") and g3["throttle_reasons"]
    # thermal never makes the tray unhealthy on its own
    client.post("/emulator/v1/gpu-faults/recover", json={"id": n["id"]})
    assert client.get(f"/redfish/v1/Systems/{TRAY}").json()["Status"]["Health"] == "OK"


def test_fault_feed_and_list(client):
    f = _inject(client, kind="xid", xid=48)
    feed = client.get("/emulator/v1/faults").json()["recent"]
    assert any(x.get("fault_id") == f["id"] and x["kind"] == "gpu_xid"
               and not x["resolved"] for x in feed)
    lst = client.get(f"/emulator/v1/gpu-faults?state=active&tray_id={TRAY}").json()
    assert lst["active"] == 1 and "79" in lst["xid_catalog"]
    client.post("/emulator/v1/gpu-faults/recover", json={"tray_id": TRAY})
    feed = client.get("/emulator/v1/faults").json()["recent"]
    assert all(x["resolved"] for x in feed if x.get("fault_id") == f["id"])


def test_validation(client):
    p = "/emulator/v1/gpu-faults/inject"
    assert client.post(p, json={"kind": "x", "tray_id": TRAY}).status_code == 422
    assert client.post(p, json={"kind": "xid", "tray_id": "nope"}).status_code == 404
    assert client.post(p, json={"kind": "xid", "tray_id": TRAY, "gpu": 9}).status_code == 422
    assert client.post(p, json={"kind": "xid", "tray_id": TRAY, "xid": 1}).status_code == 422
    assert client.post(p, json={"kind": "thermal", "tray_id": TRAY,
                                "delta_c": 99}).status_code == 422
    assert client.post("/emulator/v1/gpu-faults/recover",
                       json={"id": "gf-9999"}).status_code == 404
    assert client.get("/emulator/v1/gpu-faults/gf-9999").status_code == 404


def test_scenarios_hit_dynamo_workers(client):
    sc = client.get("/emulator/v1/gpu-faults/scenarios").json()["scenarios"]
    assert {s["name"] for s in sc} >= {"xid-recoverable", "xid-fatal", "nvlink-down",
                                       "thermal-throttle", "same-rack-pair"}
    r = client.post("/emulator/v1/gpu-faults/scenarios/xid-fatal")
    assert r.status_code == 201
    f = r.json()["faults"][0]
    assert r.json()["expected_outcome"] == "replaced"
    d = client.get("/dynamo/v1/deployments/dgd-llama-70b").json()
    hit = [w for w in d["workers"] if f["gpu_uuid"] in w["gpus"]]
    assert hit and hit[0]["state"] == "Unhealthy" and d["state"] == "Degraded"
    # traffic is routed around the dead worker
    for i in range(5):
        g = client.post("/dynamo/v1/generate", json={
            "deployment": "dgd-llama-70b", "prompt_tokens": 10, "max_tokens": 2,
            "request_id": f"s-{i}"}).json()
        assert g["worker"]["decode"] != hit[0]["id"]
    pair = client.post("/emulator/v1/gpu-faults/scenarios/same-rack-pair",
                       json={"tray_id": "su-1-rack-01-tray-00"}).json()["faults"]
    assert len({x["tray_id"] for x in pair}) == 2
    assert len({x["rack_id"] for x in pair}) == 1
    assert client.post("/emulator/v1/gpu-faults/scenarios/nope").status_code == 404


def test_all_decode_down_returns_503(client):
    d = client.get("/dynamo/v1/deployments/dgd-qwen3-235b").json()
    for w in d["workers"]:
        if w["role"] == "decode":
            tray, g = w["gpus"][0].rsplit("-g", 1)
            client.post("/emulator/v1/gpu-faults/inject", json={
                "kind": "xid", "tray_id": tray[4:], "gpu": int(g), "xid": 43})
    assert client.get("/dynamo/v1/deployments/dgd-qwen3-235b").json()["state"] == "Unavailable"
    r = client.post("/dynamo/v1/generate", json={"deployment": "dgd-qwen3-235b",
                                                 "prompt_tokens": 10})
    assert r.status_code == 503
    client.post("/emulator/v1/gpu-faults/recover", json={})
    assert client.get("/dynamo/v1/deployments/dgd-qwen3-235b").json()["state"] == "Ready"


def test_thermal_slows_worker(client):
    d = client.get("/dynamo/v1/deployments/dgd-kanana-8b").json()
    w = next(x for x in d["workers"] if x["role"] == "decode")
    tray, g = w["gpus"][0].rsplit("-g", 1)
    client.post("/emulator/v1/gpu-faults/inject", json={
        "kind": "thermal", "tray_id": tray[4:], "gpu": int(g)})
    d = client.get("/dynamo/v1/deployments/dgd-kanana-8b").json()
    w2 = next(x for x in d["workers"] if x["id"] == w["id"])
    assert w2["state"] == "Throttled" and w2["perf"] < w["perf"]
