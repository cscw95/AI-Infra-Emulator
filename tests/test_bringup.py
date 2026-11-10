"""F23 Config Manager / RMS and F24 firmware bundle (AI-Infra side)."""
import re

NEW = "bu-test-rack-01"
BUNDLE = {"version": "vr-2026.05",
          "components": {"gpu": "97.10.2C.00.11", "bmc": "1.4.2", "bf": "3.1.0",
                         "cx": "40.44.1036", "nvswitch": "2.5.1"}}
PLAN = [{"a": f"{NEW}-tray-{i:02d}:p0", "b": f"{NEW}-leaf-sw-0:swp{i + 1}"}
        for i in range(18)]


def test_staged_rack_switches_and_ztp(client):
    r = client.get("/config-manager/v1/switches", params={"rack": NEW}).json()
    assert r["count"] == 2
    assert {s["state"] for s in r["switches"]} == {"discovered"}
    sid = r["switches"][0]["id"]
    z = client.post(f"/config-manager/v1/switches/{sid}/ztp").json()
    assert z["state"] == "configured"
    assert [s["stage"] for s in z["stages"]] == ["validate_serial", "boot-script", "config",
                                                 "firmware", "provisioned"]
    assert z["mgmt_ip"] and z["config_hash"]
    bad = client.post(f"/config-manager/v1/switches/{sid}/ztp",
                      json={"fail": "provisioned"}).json()
    assert bad["state"] == "ztp_failed"
    assert client.post("/config-manager/v1/switches/nope/ztp").status_code == 404


def test_fleet_leaves_listed_for_seeded_rack(client):
    r = client.get("/config-manager/v1/switches", params={"rack": "su-1-rack-00"}).json()
    assert r["count"] > 0
    assert all(s["source"] == "fleet" and s["su_id"] == "su-1" for s in r["switches"])


def test_lldp_validate_clean_and_miswired(client):
    ok = client.post("/config-manager/v1/lldp-validate",
                     json={"rack": NEW, "cable_plan": PLAN}).json()
    assert ok["ok"] and ok["checked"] == 18 and ok["mismatches"] == []
    client.post("/config-manager/v1/lldp-faults",
                json={"rack": NEW, "a": PLAN[3]["a"], "actual_b": f"{NEW}-leaf-sw-0:swp9"})
    client.post("/config-manager/v1/lldp-faults",
                json={"rack": NEW, "a": PLAN[5]["a"], "actual_b": None})
    bad = client.post("/config-manager/v1/lldp-validate",
                      json={"rack": NEW, "cable_plan": PLAN}).json()
    assert not bad["ok"] and bad["matched"] == 16
    kinds = {m["a"]: m["kind"] for m in bad["mismatches"]}
    assert kinds == {PLAN[3]["a"]: "miswired", PLAN[5]["a"]: "missing"}
    client.delete("/config-manager/v1/lldp-faults", params={"rack": NEW})
    assert client.post("/config-manager/v1/lldp-validate",
                       json={"rack": NEW, "cable_plan": PLAN}).json()["ok"]
    assert client.post("/config-manager/v1/lldp-validate",
                       json={"rack": NEW, "cable_plan": [{"a": "x"}]}).status_code == 400


def test_rms_power_inventory_firmware(client):
    inv = client.get(f"/rms/v1/racks/{NEW}/inventory").json()
    assert inv["source"] == "bringup" and inv["power"] == "Off"
    assert len(inv["compute_trays"]) == 18 and inv["nvlink_switch_trays"] == 9
    assert inv["power_shelves"] == 4
    assert inv["compute_trays"][0]["model"] == "PowerEdge XE9812"
    # firmware refused while powered off
    assert client.post(f"/rms/v1/racks/{NEW}/firmware",
                       json={"bundle": BUNDLE}).status_code == 409
    assert client.post(f"/rms/v1/racks/{NEW}/power", json={"state": "On"}).status_code == 200
    assert client.get(f"/rms/v1/racks/{NEW}/inventory").json()["power"] == "On"
    fw = client.post(f"/rms/v1/racks/{NEW}/firmware", json={"bundle": BUNDLE}).json()
    assert fw["order"][:3] == ["bmc", "bf", "gpu"]
    assert fw["firmware"]["gpu"] == "97.10.2C.00.11"
    assert fw["firmware"]["bmc"] == "1.4.2"
    assert client.post(f"/rms/v1/racks/{NEW}/power",
                       json={"state": "warp"}).status_code == 400
    assert client.post(f"/rms/v1/racks/{NEW}/firmware",
                       json={"bundle": {"version": "x", "components": {"foo": "1"}}}
                       ).status_code == 400


def test_rms_fleet_rack_inventory(client):
    inv = client.get("/rms/v1/racks/su-1-rack-00/inventory").json()
    assert inv["source"] == "fleet" and len(inv["compute_trays"]) == 18
    r = client.post("/rms/v1/racks/su-1-rack-00/power", json={"state": "Off"})
    assert r.status_code == 200
    assert client.get("/rms/v1/racks/su-1-rack-00/inventory").json()["power"] == "Off"


def test_simple_update_bundle_and_legacy(client):
    t = client.post("/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
                    json={"bundle": BUNDLE, "Targets": ["/redfish/v1/Systems/x"]}).json()
    assert t["TaskState"] == "Completed" and t["Bundle"] == "vr-2026.05"
    assert t["Oem"]["Nvidia"]["UpdateOrder"][:3] == ["bmc", "bf", "gpu"]
    legacy = client.post("/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
                         json={"ImageURI": "http://img/fw.bin"}).json()
    assert legacy["Id"] == "fwupdate-1"
    assert client.post("/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
                       json={"bundle": "1.0"}).status_code == 400


def test_reset_clears_staged(client):
    client.get(f"/rms/v1/racks/{NEW}/inventory")
    client.post("/emulator/v1/reset")
    from app import bringup
    assert NEW not in bringup.STAGED


# ── upstream contract: DMTF SimpleUpdate, nv-config-manager ZTP, nv-rms proto ──
import json as _json  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

UP = _json.loads((_Path(__file__).parent / "fixtures" / "bringup_upstream.json").read_text())


def test_simple_update_oem_bundle_uses_only_dmtf_parameters(client):
    body = {"ImageURI": "https://fw.example/nvfwupd/vr-2026.05.fwpkg",
            "Targets": ["/redfish/v1/Systems/su-1-rack-00-tray-00"],
            "Oem": {"Nvidia": {"Bundle": BUNDLE}}}
    assert set(body) - {"Oem"} <= set(UP["simple_update_parameters"])
    t = client.post("/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
                    json=body).json()
    assert t["@odata.type"].startswith("#Task.") and t["TaskState"] == "Completed"
    assert t["Oem"]["Nvidia"]["UpdateOrder"][:3] == ["bmc", "bf", "gpu"]


def test_ztp_device_flow_matches_upstream_paths(client):
    sw = client.get("/config-manager/v1/switches", params={"rack": NEW}).json()["switches"][0]
    z = client.post(f"/config-manager/v1/switches/{sw['id']}/ztp").json()
    u = z["device_uuid"]
    for st in z["stages"]:
        method, path = st["call"].split(" ", 1)
        tmpl = re.sub(r"/ztp/v1/device/[^/]+", "/v1/device/{device_uuid}", path)
        tmpl = re.sub(r"/config/[^/]+$", "/config/{configlet}", tmpl)
        assert method.lower() in UP["ztp_paths"][tmpl], st
    serial = sw["serial"]
    assert set({"serial": serial}) == set(UP["ValidateSerialBody"]["required"])
    assert client.post(f"/ztp/v1/device/{u}/validate_serial", json={"serial": serial}).json() == "OK"
    assert client.post(f"/ztp/v1/device/{u}/validate_serial",
                       json={"serial": "wrong"}).status_code == 404
    assert client.post(f"/ztp/v1/device/{u}/validate_serial", json={}).status_code == 422
    bs = client.get(f"/ztp/v1/device/{u}/boot-script")
    assert bs.headers["content-type"].startswith("text/plain") and "CUMULUS-AUTOPROVISIONING" in bs.text
    assert client.get(f"/ztp/v1/device/{u}/config/startup").headers["content-type"].startswith(
        "text/plain")
    assert client.get(f"/ztp/v1/device/{u}/firmware").status_code == 200
    assert client.post(f"/ztp/v1/device/{u}/provisioned").json() == "OK"
    assert client.get("/ztp/v1/device/00000000-0000-0000-0000-000000000000/boot-script"
                      ).status_code == 404


def test_rms_accepts_and_returns_proto_fields(client):
    rms = UP["rms"]
    req = {"rack_id": NEW, "node_id": NEW, "operation": "POWER_OPERATION_ON"}
    assert set(req) <= set(rms["SetPowerStateRequest"])
    r = client.post(f"/rms/v1/racks/{NEW}/power", json=req).json()
    assert set(rms["SetPowerStateResponse"]) <= set(r) and r["status"] in rms["ReturnCode"]
    assert r["state"] == "On"
    fw_req = {"rack_id": NEW, "activate": True,
              "firmware_targets": [{"target": k, "filename": v}
                                   for k, v in BUNDLE["components"].items()]}
    assert set(fw_req) <= set(rms["UpdateFirmwareRequest"])
    f = client.post(f"/rms/v1/racks/{NEW}/firmware", json=fw_req).json()
    assert {"status", "message", "job_id"} <= set(f) and f["status"] == "RETURN_CODE_SUCCESS"
    assert f["order"][:3] == ["bmc", "bf", "gpu"]
