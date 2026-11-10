"""Rack power model alignment with NOCP (VR NVL72 MaxP 227 kW / MaxQ 187 kW)
and per-ComputeBlock DPS power domains."""
import pytest

from app import dps, oob
from app import power_model as pm
from app.store import CLUSTER, COMPUTE_BLOCKS

RACK = "su-1-rack-00"
TRAY = "su-1-rack-00-tray-00"
DOM = "pd-aif-stt-gasan-f1"


def test_derived_constants_match_reference_rack():
    assert pm.rack_kw() == pytest.approx(227.0)
    assert pm.rack_kw(72, pm.MAXQ_POWER_FRAC) == pytest.approx(187.0)
    assert pm.RACK_OVERHEAD_KW == pytest.approx(61.4)
    assert pm.GPU_MAXQ_W == pytest.approx(1744.44, abs=0.01)
    # tray host + 4 GPUs, plus 9 NVSwitch trays, sum back to the rack MaxP
    assert (18 * pm.TRAY_MAXP_W + 9 * pm.NVSWITCH_TRAY_W) / 1000 == pytest.approx(227.0)
    assert pm.RACK_PSU_CAPACITY_KW == 440.0
    assert (pm.POWER_SHELVES - 1) * pm.POWER_SHELF_KW >= pm.RACK_MAXP_KW   # N+1 shelf


def test_dps_profiles_rack_peaks():
    items = dps.PROFILES
    assert dps.rack_peak_kw(72, "max-p") == pytest.approx(227.0)
    assert dps.rack_peak_kw(72, "max-q") == pytest.approx(187.0, abs=0.05)
    assert items["max-p"]["rack_kw"] == 227.0 and items["max-q"]["rack_kw"] == 187.0
    assert dps.rack_peak_kw(72, "inference") == pytest.approx(227 * 0.75, abs=0.05)


def test_compute_blocks_mirror_nocp_topology():
    su_racks = {su: n for s in CLUSTER for su, n in s["sus"]}
    by_block = {b["id"]: sum(su_racks[s] for s in b["sus"]) for b in COMPUTE_BLOCKS}
    assert by_block == {"aif-stt-gasan-f1": 24, "aif-stt-gasan-f2": 12,
                        "aif-igis-ansan-f1": 54, "aif-igis-ansan-f2": 50}
    assert {b["id"]: b["power_mw"] for b in COMPUTE_BLOCKS} == {
        "aif-stt-gasan-f1": 6.0, "aif-stt-gasan-f2": 3.0,
        "aif-igis-ansan-f1": 12.9, "aif-igis-ansan-f2": 10.4}
    # every block fits its rack count at MaxQ (NOCP seeds racks at maxq)
    for b in COMPUTE_BLOCKS:
        assert by_block[b["id"]] * pm.RACK_MAXQ_KW <= b["power_mw"] * 1000


def test_seeded_domain_per_compute_block(client):
    d = client.get(f"/dps/v1/power-domains/{DOM}").json()
    assert d["compute_block"] == "aif-stt-gasan-f1" and d["power_mw"] == 6.0
    assert d["operating_limit_kw"] == 6000.0 and d["site"] == "gasan"
    assert d["racks"] == 24          # test cluster cap = the whole f1 block


def test_redfish_rack_power_limit_range_and_170kw_not_clamped(client):
    url = f"/redfish/v1/Chassis/{RACK}/Controls/PowerLimit"
    v = client.get(url).json()
    assert v["AllowableMax"] == 227000.0 and v["AllowableMin"] == 1500.0
    r = client.patch(url, json={"SetPoint": 170000})
    assert r.status_code == 200
    v = r.json()
    assert v["SetPoint"] == 170000
    assert v["Oem"]["Nvidia"]["EffectiveLimitWatts"] == pytest.approx(170000, abs=100)
    assert client.patch(url, json={"SetPoint": 230000}).status_code == 400
    assert client.patch(url, json={"SetPoint": 1000}).status_code == 400


def test_tray_power_limit_and_gpu_share(client):
    v = client.get(f"/redfish/v1/Chassis/{TRAY}/Controls/PowerLimit").json()
    assert v["AllowableMax"] == pytest.approx(12611.1, abs=0.1)
    assert client.patch(f"/redfish/v1/Chassis/{TRAY}/Controls/PowerLimit",
                        json={"SetPoint": 13000}).status_code == 400
    # a full 227 kW rack cap gives each GPU its 2300 W Max-P board power
    tp = dps.tray_power(TRAY, {"cap_kw": 227.0, "demand_kw": 227.0})
    assert tp["gpu_cap_w"] == pytest.approx(2300, abs=1)


def test_psu_capacity_and_oob_cap(client):
    ps = client.get(f"/redfish/v1/Chassis/{RACK}/PowerSubsystem").json()
    assert ps["CapacityWatts"] == pytest.approx(440000)
    row = client.get(f"/emulator/v1/oob/racks/{RACK}").json()
    assert row["psu_total"] == 24 and row["psu_capacity_kw"] == 440.0
    assert 0 < row["cap_kw"] <= 227.0
    assert oob.PSU_RATED_W == pytest.approx(110000 / 6)
