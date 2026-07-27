"""Rack control plane — verifies the full rack-control API and its telemetry effects."""
from fastapi.testclient import TestClient

from app.main import app
from app.store import STORE

client = TestClient(app)
OBS = "/emulator/v1/obs"


def _rack(rid):
    return next(r for r in client.get(f"{OBS}/racks").json()
                if r["rack_id"] == rid)


def _first_rack_id():
    return next(iter(STORE.racks))


def test_power_off_nulls_dcgm_and_marks_rack_oob():
    """rack off = in-band (DCGM) telemetry lost — readings null / state off;
    the rack view is kept only from OOB (BMC/DCIM) sources (standby power, inlet)."""
    rid = _first_rack_id()
    r = client.post(f"{OBS}/racks/{rid}/control", json={"action": "power_off"})
    assert r.status_code == 200 and r.json()["state"]["power_state"] == "off"
    view = _rack(rid)
    assert view["power_state"] == "off"
    assert view["it_power_kw"] < 1.0            # standby power — kept via OOB
    assert view["telemetry_source"] == "oob"
    gpus = client.get(f"{OBS}/dcgm/gpus", params={"rack": rid, "limit": 5}).json()
    assert gpus["gpus"], "rack filter returned no GPUs"
    for g in gpus["gpus"]:
        assert g["state"] == "off" and g["health"] == "unknown"
        assert g["telemetry_source"] == "none"
        for k in ("power_w", "util_pct", "sm_util_pct", "temp_c", "mem_temp_c",
                  "sm_clock_mhz", "mem_used_gb", "nvlink_tx_gbps",
                  "nvlink_rx_gbps"):
            assert g[k] is None, k
        assert g["throttle_reasons"] == []
    # state=off filter works + summary off count
    off = client.get(f"{OBS}/dcgm/gpus",
                     params={"rack": rid, "state": "off", "limit": 5}).json()
    assert off["total"] == 72
    s = client.get(f"{OBS}/summary").json()
    assert s["gpus"]["off"] >= 72
    su = next(x for x in client.get(f"{OBS}/dcgm/su-summary").json()["sus"]
              if x["su_id"] == view["su_id"])
    assert su["off"] >= 72
    # restore → normal readings recovered
    client.post(f"{OBS}/racks/{rid}/control", json={"action": "power_on"})
    assert _rack(rid)["power_state"] == "on"
    assert _rack(rid)["telemetry_source"] == "inband"
    gpus2 = client.get(f"{OBS}/dcgm/gpus", params={"rack": rid, "limit": 5}).json()
    for g in gpus2["gpus"]:
        assert g["telemetry_source"] == "dcgm" and g["state"] != "off"
        assert g["power_w"] is not None and g["util_pct"] is not None


def test_bulk_power_cap_sets_throttle_reason():
    r = client.post(f"{OBS}/racks/control", json={
        "scope": {"all": True}, "action": "power_cap",
        "params": {"cap_pct": 40}})
    body = r.json()
    assert r.status_code == 200
    assert body["matched"] == len(STORE.racks) == body["applied"]
    views = client.get(f"{OBS}/racks").json()
    assert all(v["power_cap_kw"] for v in views)
    summary = client.get(f"{OBS}/summary").json()
    assert summary["racks_capped"] == len(STORE.racks)
    # if there are active (tenant) GPUs, the power_cap throttle reason is exposed
    gpus = client.get(f"{OBS}/dcgm/gpus",
                      params={"state": "active", "limit": 50}).json()["gpus"]
    if gpus:
        assert any("power_cap" in g["throttle_reasons"] for g in gpus)
    client.post(f"{OBS}/racks/control", json={
        "scope": {"all": True}, "action": "power_uncap"})
    assert client.get(f"{OBS}/summary").json()["racks_capped"] == 0


def test_cordon_flags_and_alerts():
    rid = _first_rack_id()
    client.post(f"{OBS}/racks/{rid}/control",
                json={"action": "cordon", "params": {"reason": "PM 점검"}})
    assert _rack(rid)["cordoned"] is True
    alerts = client.get(f"{OBS}/alerts").json()
    assert any("RACK_CORDONED" in a["summary"] and a["state"] == "firing"
               for a in alerts)
    client.post(f"{OBS}/racks/{rid}/control", json={"action": "uncordon"})
    assert _rack(rid)["cordoned"] is False


def test_restart_converges_back_to_on():
    rid = _first_rack_id()
    client.post(f"{OBS}/racks/{rid}/control", json={"action": "restart"})
    assert _rack(rid)["power_state"] == "mixed"
    # 2 forced ticks (the control endpoint force-ticks) → boot complete
    client.post(f"{OBS}/racks/{rid}/control", json={"action": "workload",
                "params": {"profile": "steady"}})
    client.post(f"{OBS}/racks/{rid}/control", json={"action": "workload",
                "params": {"profile": "steady"}})
    assert _rack(rid)["power_state"] == "on"


def test_invalid_action_and_scope_rejected():
    rid = _first_rack_id()
    assert client.post(f"{OBS}/racks/{rid}/control",
                       json={"action": "explode"}).status_code == 422
    assert client.post(f"{OBS}/racks/control",
                       json={"scope": {}, "action": "power_on"}).status_code == 422
    assert client.post(f"{OBS}/racks/{rid}/control",
                       json={"action": "workload",
                             "params": {"profile": "warp"}}).status_code == 422


def test_workload_profile_drives_idle_racks():
    # demo load can be applied even to an unallocated rack
    rid = _first_rack_id()
    client.post(f"{OBS}/racks/{rid}/control",
                json={"action": "workload", "params": {"profile": "train"}})
    gpus = client.get(f"{OBS}/dcgm/gpus",
                      params={"rack": rid, "limit": 10}).json()["gpus"]
    assert sum(g["util_pct"] for g in gpus) / len(gpus) > 30
    client.post(f"{OBS}/racks/{rid}/control",
                json={"action": "workload", "params": {"profile": "idle"}})
    gpus = client.get(f"{OBS}/dcgm/gpus",
                      params={"rack": rid, "limit": 10}).json()["gpus"]
    assert sum(g["util_pct"] for g in gpus) / len(gpus) < 10
