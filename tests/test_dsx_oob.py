"""DSX OS baseline — Redfish OOB surfaces (PowerSubsystem / ThermalSubsystem /
LeakDetection / SEL / PowerLimit / Chassis.Reset / GpuReset), EventService
push, OOB summary and the version matrix."""
import pytest

from app import redfish, oob
from app.store import STORE
from app import observability as obs

TRAY = "su-1-rack-00-tray-00"
RACK = "su-1-rack-00"
SUMMARY_KEYS = {"bmc_reachable", "bmc_total", "psu_ok", "psu_total", "leak",
                "max_inlet_c", "sel_critical", "power_kw", "cap_kw"}


def _inject(client, **body):
    r = client.post("/emulator/v1/oob/inject", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_versions_matrix(client):
    for path in ("/versions", "/emulator/v1/versions"):
        v = client.get(path).json()
        assert v["dsx_os"] == "2026.05" and v["nico"] == "2.3.0"
        assert v["nvke"] == "0.23.1" and v["dps"] == "0.10"
        assert v["k8s"] == ["1.33", "1.34"]
        assert v["platform"] == "Vera Rubin NVL72"


# ── PowerSubsystem ────────────────────────────────────────────────────
def test_power_subsystem_rack_and_tray(client):
    rk = client.get(f"/redfish/v1/Chassis/{RACK}/PowerSubsystem").json()
    assert rk["@odata.type"].startswith("#PowerSubsystem")
    assert rk["CapacityWatts"] == 24 * oob.PSU_RATED_W == 440_000
    assert rk["Allocation"]["AllocatedWatts"] > 0
    tr = client.get(f"/redfish/v1/Chassis/{TRAY}/PowerSubsystem").json()
    assert tr["Oem"]["Nvidia"]["SharedBusbar"] is True
    assert tr["Allocation"]["AllocatedWatts"] < rk["Allocation"]["AllocatedWatts"]


def test_power_supplies_collection_and_member(client):
    col = client.get(f"/redfish/v1/Chassis/{RACK}/PowerSubsystem/PowerSupplies").json()
    assert col["Members@odata.count"] == 24
    psu = client.get(f"/redfish/v1/Chassis/{RACK}/PowerSubsystem/PowerSupplies/PS0-PSU0").json()
    assert psu["Status"]["Health"] == "OK"
    assert psu["Metrics"]["OutputPowerWatts"]["Reading"] > 0
    assert client.get(f"/redfish/v1/Chassis/{RACK}/PowerSubsystem/"
                      "PowerSupplies/PS9-PSU9").status_code == 404
    assert client.get("/redfish/v1/Chassis/nope/PowerSubsystem").status_code == 404


def test_psu_failure_reflected(client):
    _inject(client, kind="psu_fail", target=f"{RACK}/PS1-PSU2")
    psu = client.get(f"/redfish/v1/Chassis/{RACK}/PowerSubsystem/PowerSupplies/PS1-PSU2").json()
    assert psu["Status"]["Health"] == "Critical"
    ps = client.get(f"/redfish/v1/Chassis/{RACK}/PowerSubsystem").json()
    assert ps["Status"]["Health"] == "Warning"
    row = next(x for x in client.get("/emulator/v1/oob/summary").json()["racks"]
               if x["rack_id"] == RACK)
    assert row["psu_ok"] == row["psu_total"] - 1
    evs = client.get("/exchange/v1/events?stream=false&topic=power").json()
    assert any(e["type"] == "PsuFailed" and e["category"] == "psu" for e in evs)
    client.post("/emulator/v1/oob/recover", json={"kind": "psu_fail"})
    psu = client.get(f"/redfish/v1/Chassis/{RACK}/PowerSubsystem/PowerSupplies/PS1-PSU2").json()
    assert psu["Status"]["Health"] == "OK"


# ── Thermal / Leak ────────────────────────────────────────────────────
def test_thermal_subsystem_and_leak(client):
    th = client.get(f"/redfish/v1/Chassis/{TRAY}/ThermalSubsystem").json()
    assert th["LeakDetection"]["DetectorState"] == "OK"
    assert th["ThermalMetrics"]["TemperatureReadingsCelsius"]
    _inject(client, kind="leak", target=TRAY, severity="Warning")
    ld = client.get(f"/redfish/v1/Chassis/{TRAY}/ThermalSubsystem/LeakDetection").json()
    assert ld["DetectorState"] == "Warning"
    _inject(client, kind="leak", target=TRAY, severity="Critical")
    rk = client.get(f"/redfish/v1/Chassis/{RACK}/ThermalSubsystem").json()
    assert rk["LeakDetection"]["DetectorState"] == "Critical"
    assert rk["Status"]["Health"] == "Critical"
    client.post("/emulator/v1/oob/recover", json={"kind": "leak", "target": TRAY})
    ld = client.get(f"/redfish/v1/Chassis/{TRAY}/ThermalSubsystem/LeakDetection").json()
    assert ld["DetectorState"] == "OK"


def test_leak_bad_severity(client):
    r = client.post("/emulator/v1/oob/inject",
                    json={"kind": "leak", "target": TRAY, "severity": "Bad"})
    assert r.status_code == 422


def test_cdu_leak_maps_to_rack_leak(client):
    cdu = "cdu-su-1"
    client.post(f"/emulator/v1/obs/dlc/cdus/{cdu}/inject", json={"kind": "leak"})
    leaking = next(iter(obs.ENGINE.cdus[cdu].closed_branches))
    ld = client.get(f"/redfish/v1/Chassis/{leaking}/ThermalSubsystem/LeakDetection").json()
    assert ld["DetectorState"] == "Critical"
    evs = client.get("/exchange/v1/events?stream=false&topic=leak").json()
    assert any(e["type"] == "CduFault" for e in evs)


def test_thermal_inject_raises_inlet(client):
    before = client.get(f"/emulator/v1/oob/racks/{RACK}").json()["max_inlet_c"]
    _inject(client, kind="thermal", target=RACK, delta_c=15)
    d = client.get(f"/emulator/v1/oob/racks/{RACK}").json()
    assert d["max_inlet_c"] >= before + 14.9
    assert any(t["thermal"] == "Critical" for t in d["trays"])
    evs = client.get("/exchange/v1/events?stream=false&topic=thermal").json()
    assert evs and evs[-1]["type"] == "InletTemperatureHigh"


# ── SEL ───────────────────────────────────────────────────────────────
def test_sel_entries_and_critical(client):
    base = f"/redfish/v1/Systems/{TRAY}/LogServices/SEL"
    assert client.get(f"/redfish/v1/Systems/{TRAY}/LogServices").json()["Members@odata.count"] == 1
    es = client.get(f"{base}/Entries").json()
    assert es["Members@odata.count"] >= 2
    _inject(client, kind="sel_critical", target=TRAY)
    crit = client.get(f"{base}/Entries?severity=Critical").json()["Members"]
    assert len(crit) == 1 and crit[0]["MessageId"].endswith("MCE_UNCORR")
    one = client.get(f"{base}/Entries/{crit[0]['Id']}").json()
    assert one["Severity"] == "Critical"
    row = client.get(f"/emulator/v1/oob/racks/{RACK}").json()
    assert row["sel_critical"] == 1
    client.post("/emulator/v1/oob/recover", json={"kind": "sel_critical", "target": RACK})
    assert client.get(f"/emulator/v1/oob/racks/{RACK}").json()["sel_critical"] == 0
    client.post(f"{base}/Actions/LogService.ClearLog")
    assert client.get(f"{base}/Entries").json()["Members@odata.count"] == 1
    assert client.get(f"{base}/Entries/9999").status_code == 404


# ── PowerLimit ────────────────────────────────────────────────────────
def test_power_limit_tray(client):
    url = f"/redfish/v1/Chassis/{TRAY}/Controls/PowerLimit"
    r = client.patch(url, json={"SetPoint": 3000})
    assert r.status_code == 200
    v = r.json()
    assert v["SetPoint"] == 3000 and v["ControlMode"] == "Override"
    assert v["Oem"]["Nvidia"]["EffectiveLimitWatts"] <= 3000
    ps = client.get(f"/redfish/v1/Chassis/{TRAY}/PowerSubsystem").json()
    assert ps["Allocation"]["AllocatedWatts"] <= 3000
    assert client.patch(url, json={"SetPoint": -5}).status_code == 400
    v = client.patch(url, json={"ControlMode": "Automatic"}).json()
    assert v["SetPoint"] is None and v["ControlMode"] == "Automatic"
    assert client.get(f"/redfish/v1/Chassis/{TRAY}/Controls").json()["Members@odata.count"] == 1


def test_power_limit_rack_is_single_cap_source(client):
    url = f"/redfish/v1/Chassis/{RACK}/Controls/PowerLimit"
    client.patch(url, json={"SetPoint": 60000})
    ctl = obs.ENGINE.rack_ctrl[RACK]
    assert ctl["cap_kw"] <= 60.0
    tele = client.get(f"/dps/v1/telemetry?rack={RACK}").json()
    rack = tele["domains"][0]["groups"][0]["racks"][0]
    assert rack["manual_cap_kw"] == 60.0 and rack["cap_kw"] <= 60.0
    assert rack["measured_kw"] <= 60.0
    # observability DCIM view reflects the same cap
    rv = next(x for x in client.get("/emulator/v1/obs/racks").json()
              if x["rack_id"] == RACK)
    assert rv["power_cap_kw"] <= 60.0
    client.patch(url, json={"SetPoint": None})
    rack = client.get(f"/dps/v1/telemetry?rack={RACK}").json()["domains"][0]["groups"][0]["racks"][0]
    assert rack["manual_cap_kw"] is None


# ── Chassis.Reset / GpuReset ──────────────────────────────────────────
def test_ac_power_cycle_restores_bmc(client):
    _inject(client, kind="bmc_unreachable", target=TRAY)
    assert client.get(f"/redfish/v1/Chassis/{TRAY}/ThermalSubsystem").status_code == 503
    assert client.post(f"/redfish/v1/Systems/{TRAY}/Actions/GpuReset",
                       json={}).status_code == 503
    s = client.get("/emulator/v1/oob/summary").json()
    assert s["totals"]["bmc_reachable"] == s["totals"]["bmc_total"] - 1
    r = client.post(f"/redfish/v1/Chassis/{TRAY}/Actions/Chassis.Reset",
                    json={"ResetType": "PowerCycle"})
    assert r.status_code == 200 and r.json()["ResetType"] == "PowerCycle"
    assert client.get(f"/redfish/v1/Chassis/{TRAY}/ThermalSubsystem").status_code == 200
    assert STORE.trays[TRAY].power_target == "On"
    sel = client.get(f"/redfish/v1/Systems/{TRAY}/LogServices/SEL/Entries").json()["Members"]
    assert sel[0]["MessageId"].endswith("AC_POWER_CYCLE")
    bad = client.post(f"/redfish/v1/Chassis/{TRAY}/Actions/Chassis.Reset",
                      json={"ResetType": "Explode"})
    assert bad.status_code == 400


def test_rack_power_cycle(client):
    r = client.post(f"/redfish/v1/Chassis/{RACK}/Actions/Chassis.Reset",
                    json={"ResetType": "PowerCycle"}).json()
    assert len(r["Trays"]) == 18
    assert client.get(f"/redfish/v1/Chassis/{RACK}").json()["ChassisType"] == "Rack"


def test_gpu_reset(client):
    url = f"/redfish/v1/Systems/{TRAY}/Actions/GpuReset"
    r = client.post(url, json={"GpuIds": [0, 2]})
    assert r.status_code == 200 and r.json()["GpuIds"] == [0, 2]
    assert client.post(url).json()["GpuIds"] == [0, 1, 2, 3]
    assert client.post(url, json={"GpuIds": [7]}).status_code == 400
    STORE.set_power(STORE.trays[TRAY], "ForceOff")
    assert client.post(url, json={}).status_code == 409
    sys = client.get(f"/redfish/v1/Systems/{TRAY}").json()
    assert "#GpuReset" in sys["Actions"]


# ── EventService push ────────────────────────────────────────────────
@pytest.fixture
def pushed(monkeypatch):
    got = []
    monkeypatch.setattr(redfish, "PUSH_SYNC", True)
    monkeypatch.setattr(redfish, "_http_post", lambda url, p: got.append((url, p)))
    yield got
    redfish._SUBSCRIPTIONS.clear()


def test_eventservice_push_leak_thermal_psu_sel(client, pushed):
    sub = client.post("/redfish/v1/EventService/Subscriptions",
                      json={"Destination": "http://127.0.0.1:8000/hook",
                            "Context": "nocp"}).json()
    assert sub["Oem"]["Nvidia"]["Categories"] == ["leak", "thermal", "psu", "sel"]
    _inject(client, kind="leak", target=TRAY)
    _inject(client, kind="psu_fail", target=RACK)
    _inject(client, kind="thermal", target=TRAY, delta_c=20)
    cats = [p["Events"][0]["Oem"]["Nvidia"]["Category"] for _, p in pushed]
    assert {"leak", "psu", "thermal", "sel"} <= set(cats)
    url, p = pushed[0]
    assert url == "http://127.0.0.1:8000/hook" and p["Context"] == "nocp"
    assert p["@odata.type"].startswith("#Event")
    assert p["Events"][0]["OriginOfCondition"]["@odata.id"].startswith("/redfish/v1/")
    got = client.get(f"/redfish/v1/EventService/Subscriptions/{sub['Id']}").json()
    assert got["Oem"]["Nvidia"]["Delivered"] == len(pushed)


def test_eventservice_category_filter_and_failure(client, monkeypatch):
    monkeypatch.setattr(redfish, "PUSH_SYNC", True)

    def boom(url, p):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(redfish, "_http_post", boom)
    sub = client.post("/redfish/v1/EventService/Subscriptions",
                      json={"Destination": "http://127.0.0.1:1/x",
                            "Oem": {"Nvidia": {"Categories": ["grid"]}}}).json()
    _inject(client, kind="leak", target=TRAY)                    # filtered out
    st = client.get(f"/redfish/v1/EventService/Subscriptions/{sub['Id']}").json()
    assert st["Oem"]["Nvidia"]["Failed"] == 0
    client.post("/dps/v1/flex-events", json={"domain": "pd-aif-stt-gasan-f1",
                                             "reduce_kw": 100, "duration_s": 60})
    st = client.get(f"/redfish/v1/EventService/Subscriptions/{sub['Id']}").json()
    assert st["Oem"]["Nvidia"]["Failed"] == 1
    assert "connection refused" in st["Oem"]["Nvidia"]["LastError"]
    redfish._SUBSCRIPTIONS.clear()


# ── OOB summary ───────────────────────────────────────────────────────
def test_oob_summary_contract(client):
    s = client.get("/emulator/v1/oob/summary?site=gasan").json()
    assert s["racks"] and all(r["site"] == "gasan" for r in s["racks"])
    assert SUMMARY_KEYS <= set(s["racks"][0])
    r0 = s["racks"][0]
    assert r0["bmc_total"] == 18 and r0["psu_total"] == 24
    assert r0["leak"] == "OK" and r0["power_kw"] > 0 and r0["cap_kw"] >= r0["power_kw"]
    assert client.get(f"/emulator/v1/oob/racks/{RACK}").json()["trays"][0]["bmc_reachable"]
    assert client.get("/emulator/v1/oob/racks/nope").status_code == 404
    assert client.post("/emulator/v1/oob/inject",
                       json={"kind": "nope", "target": TRAY}).status_code == 422
