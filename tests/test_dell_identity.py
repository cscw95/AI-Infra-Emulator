"""Dell hardware identity (PowerEdge XE9812 / iDRAC10), VAST Data storage, BOM."""
from app import dell, power_model as pm

TRAY = "su-1-rack-00-tray-00"
RACK = "su-1-rack-00"


def test_versions_hw_vendor(client):
    for p in ("/versions", "/emulator/v1/versions"):
        d = client.get(p).json()
        assert d["hw_vendor"] == "Dell" and d["dsx_os"] == "2026.05"


def test_system_and_chassis_are_dell(client):
    s = client.get(f"/redfish/v1/Systems/{TRAY}").json()
    assert s["Manufacturer"] == "Dell Inc." and s["Model"] == "PowerEdge XE9812"
    assert len(s["SKU"]) == 7 and s["SKU"] == dell.service_tag(TRAY)
    assert s["SerialNumber"] and s["PartNumber"]
    assert s["Oem"]["Dell"]["DellSystem"]["ServiceTag"] == s["SKU"]
    assert s["Oem"]["Nvidia"]["ComputeTrayId"] == TRAY          # kept for NICo
    assert s["ProcessorSummary"]["Model"] == "NVIDIA Rubin"     # silicon stays NVIDIA
    c = client.get(f"/redfish/v1/Chassis/{TRAY}").json()
    assert c["Manufacturer"] == "Dell Inc." and c["SKU"] == s["SKU"]
    r = client.get(f"/redfish/v1/Chassis/{RACK}").json()
    assert r["Manufacturer"] == "Dell Inc." and r["ChassisType"] == "Rack"
    assert r["Oem"]["Dell"]["RackController"].startswith("Dell Integrated Rack")


def test_dpu_stays_nvidia(client):
    did = client.get(f"/redfish/v1/Systems/{TRAY}").json()["Oem"]["Nvidia"]["DpuId"]
    d = client.get(f"/redfish/v1/Systems/Bluefield/{did}").json()
    assert d["Manufacturer"] == "NVIDIA"
    assert client.get(f"/redfish/v1/Managers/{did}").json()["Manufacturer"] == "NVIDIA"


def test_idrac_manager_and_aliases(client):
    m = client.get(f"/redfish/v1/Managers/{TRAY}").json()
    assert m["Manufacturer"] == "Dell Inc." and m["Model"] == "iDRAC10"
    assert m["Oem"]["Dell"]["DelliDRACCard"]["IDRACId"] == "iDRAC.Embedded.1"
    base = f"/redfish/v1/Dell/{TRAY}"
    a = client.get(f"{base}/Managers/iDRAC.Embedded.1").json()
    assert a["Id"] == "iDRAC.Embedded.1" and a["Model"] == "iDRAC10"
    assert a["Oem"]["Dell"]["CanonicalUri"] == f"/redfish/v1/Managers/{TRAY}"
    assert client.get(f"{base}/Systems/System.Embedded.1").json()["Id"] == \
        "System.Embedded.1"
    assert client.get(f"{base}/Chassis/System.Embedded.1").json()["Model"] == \
        "PowerEdge XE9812"
    assert client.get("/redfish/v1/Dell/nope/Managers/iDRAC.Embedded.1"
                      ).status_code == 404


def test_psu_dell_and_power_model_unchanged(client):
    ps = client.get(f"/redfish/v1/Chassis/{RACK}/PowerSubsystem/PowerSupplies"
                    ).json()["Members"]
    p = client.get(ps[0]["@odata.id"]).json()
    assert p["Manufacturer"] == "Dell Inc."
    assert p["PowerCapacityWatts"] == pm.PSU_RATED_W
    assert (pm.RACK_MAXP_KW, pm.RACK_MAXQ_KW) == (227.0, 187.0)


def test_firmware_inventory_idrac(client):
    fw = client.get("/redfish/v1/UpdateService/FirmwareInventory").json()
    bmc = next(m for m in fw["Members"] if m["Id"] == f"{TRAY}-BMC")
    assert bmc["Manufacturer"] == "Dell Inc." and bmc["Version"] == dell.IDRAC_FW


def test_cdu_switches_dell_storage_vast(client):
    c = client.get("/emulator/v1/obs/dlc/cdus").json()
    cdu = c[0]
    assert cdu["model"] == "Dell PowerCool CDU C7000" and cdu["oem"] == "Dell"
    sw = client.get("/netq/v1/switches").json()
    sw = sw.get("switches", sw)
    assert sw[0]["vendor"] == "Dell" and sw[0]["product"].startswith("Dell PowerSwitch")
    ib = client.get("/ufm/v1/resources/systems").json()["systems"]
    assert ib[0]["vendor"] == "Dell"
    st = client.get("/vast/v1/clusters").json()["clusters"][0]
    assert st["vendor"] == "VAST Data" and st["product"] == "VAST Data Platform"
    assert st["os"] == "VAST OS" and st["hardware"]["architecture"] == "DASE"


def test_vast_primary_no_powerscale(client):
    r = client.get("/vast/v1/clusters")
    assert r.status_code == 200
    assert "Deprecation" not in r.headers and "Link" not in r.headers
    c = r.json()["clusters"][0]
    for k in ("name", "cboxes", "dboxes", "drr", "used_pb"):
        assert k in c                                   # NOCP consumes these
    assert c["name"].startswith("vast-") and "dell" not in c
    assert client.get("/powerscale/v1/clusters").status_code == 404


def test_bom(client):
    b = client.get("/emulator/v1/inventory/bom").json()
    assert b["hw_vendor"] == "Dell" and b["compute"] == "PowerEdge XE9812"
    items = {l["item"]: l for l in b["rack_bom"]}
    assert items["compute tray"]["qty"] == 18 and items["gpu"]["qty"] == 72
    assert items["gpu"]["vendor"] == "NVIDIA"
    assert items["power shelf"]["qty"] == 4 and items["psu"]["qty"] == 24
    assert {l["status"] for l in b["rack_bom"]} <= {"verified", "unverified"}
    assert b["storage_vendor"] == "VAST Data"
    st = [l for l in b["site_bom"] if l["item"].startswith("storage")]
    assert st and {l["vendor"] for l in st} == {"VAST Data"}
    assert {l["part"] for l in st} >= {"VAST CBox (CNode)", "VAST DBox (DNode, SCM+QLC)"}
    r = client.get(f"/emulator/v1/inventory/bom/{RACK}").json()
    assert len(r["trays"]) == 18 and r["trays"][0]["idrac"] == "iDRAC.Embedded.1"
    assert r["trays"][0]["service_tag"] == dell.service_tag(r["trays"][0]["tray_id"])
    assert client.get("/emulator/v1/inventory/bom/nope").status_code == 404
