"""Dell AI Factory hardware identity — single source of every OEM string.

The whole AI Infra hardware supply vendor is Dell. Rack, compute/switch
trays, power shelves, CDU, BMC (iDRAC10), fabric switches and storage carry
Dell identity; the silicon inside (Rubin GPU, Vera CPU, ConnectX, BlueField,
NVLink switch ASIC) stays NVIDIA and keeps Manufacturer "NVIDIA".

Part names come from /Users/.../neocloud/RESEARCH-DELL.md (2026-11-10):
  VERIFIED   PowerEdge XE9812 (VR NVL72 rack-scale, DLC), iDRAC10 Redfish with
             Manager id iDRAC.Embedded.1, PowerCool CDU C7000, PowerSwitch
             SN5610 (Spectrum-4, 64x800GbE), Quantum-X800 Q3300-LD (Dell
             resale), PowerScale F910 / OneFS, Integrated Rack Controller +
             OpenManage Enterprise.
  UNVERIFIED IR9000 as the XE9812 rack, Dell 110 kW power shelf (Dell lists
             the IR7000 PS33 33 kW shelf), Dell resale of Quantum-X800 Q3400,
             iDRAC10 Oem.Dell field names (iDRAC9 conventions used).
Every BOM line carries "status" so consumers can tell the two apart.

Routes:
  GET /emulator/v1/inventory/bom              vendor summary + per-rack BOM template
  GET /emulator/v1/inventory/bom/{rack_id}    one rack → Dell part list with service tags
  GET /redfish/v1/Dell/{tray_id}/Managers/iDRAC.Embedded.1   per-iDRAC alias views
  GET /redfish/v1/Dell/{tray_id}/Systems/System.Embedded.1   (the ids a real iDRAC
  GET /redfish/v1/Dell/{tray_id}/Chassis/System.Embedded.1    serves at its own IP)
"""
import zlib

from fastapi import APIRouter, HTTPException

from .store import STORE, GPU_PER_TRAY
from . import power_model as pm

HW_VENDOR = "Dell"
MANUFACTURER = "Dell Inc."          # Redfish Manufacturer for Dell-built parts

COMPUTE_MODEL = "PowerEdge XE9812"
RACK_MODEL = "Dell IR9000 ORv3 rack"            # UNVERIFIED pairing
IDRAC_MODEL = "iDRAC10"
IDRAC_FW = "1.4.2"                  # kept: NICo firmware baseline/drift keys on it
IDRAC_MANAGER_ID = "iDRAC.Embedded.1"
DELL_SYSTEM_ID = "System.Embedded.1"
PSU_MODEL = "Dell ORv3 power shelf PSU"         # rating from power_model, see BOM
CDU_MODEL = "Dell PowerCool CDU C7000"
LEAF_MODEL = "SN5610"
LEAF_PRODUCT = "Dell PowerSwitch SN5610"
IB_PRODUCT = "NVIDIA Quantum-X800 Q3400 (Dell supplied)"
STORAGE_PRODUCT = "Dell PowerScale F910"
STORAGE_OS = "OneFS"
MGMT = ["Dell Integrated Rack Controller (IRC)", "Dell OpenManage Enterprise (OME)"]


def service_tag(key: str) -> str:
    """Deterministic 7-char Dell-style service tag (no I/O)."""
    n = zlib.crc32(key.encode()) * 7919 + len(key)
    s = ""
    for _ in range(7):
        n, r = divmod(n, 34)
        s += "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"[r]
    return s


def part_number(kind: str) -> str:
    return {"tray": "XE9812-CT", "rack": "IR9000-VR72", "switch": "XE9812-NVS",
            "psu": "ORV3-PSU-110", "cdu": "PWRCOOL-C7000"}.get(kind, kind.upper())


def oem_dell_system(key: str, model: str, kind: str) -> dict:
    tag = service_tag(key)
    return {"@odata.type": "#DellOem.v1_0_0.DellOem",
            "DellSystem": {"ServiceTag": tag, "ChassisServiceTag": tag,
                           "SystemID": kind, "Model": model,
                           "ManagedBy": MGMT[0]}}


def identity(key: str, model: str, kind: str) -> dict:
    """Redfish identity fields for a Dell-built resource."""
    tag = service_tag(key)
    return {"Manufacturer": MANUFACTURER, "Model": model, "SKU": tag,
            "SerialNumber": f"CN{tag}{zlib.crc32(key.encode()) % 10000:04d}",
            "PartNumber": part_number(kind)}


# ── BOM ───────────────────────────────────────────────────────────────
def _line(item, part, qty, status, note="", vendor=HW_VENDOR):
    return {"item": item, "vendor": vendor, "part": part, "qty": qty,
            "status": status, "note": note}


def rack_bom_lines(rack) -> list:
    trays = len(rack.trays)
    return [
        _line("rack", RACK_MODEL, 1, "unverified",
              "21\" ORv3; Dell names IR9000 next to XE9812, pairing not stated"),
        _line("compute tray", f"{COMPUTE_MODEL} compute tray", trays, "verified",
              f"{GPU_PER_TRAY}x NVIDIA Rubin GPU + 2x NVIDIA Vera CPU per tray"),
        _line("nvlink switch tray", f"{COMPUTE_MODEL} NVLink switch tray",
              rack.nvlink_switch_trays, "unverified", "tray name not published"),
        _line("power shelf", f"Dell ORv3 power shelf {pm.POWER_SHELF_KW:.0f} kW",
              rack.power_shelves, "unverified",
              "model rating (NOCP vr-nvl72); Dell lists IR7000 PS33 (6x5.5 kW)"),
        _line("psu", PSU_MODEL, rack.power_shelves * pm.PSU_PER_SHELF,
              "unverified", f"{pm.PSU_RATED_W / 1000:.1f} kW each"),
        _line("bmc", f"{IDRAC_MODEL} (per compute tray)", trays, "verified",
              "Redfish Manager iDRAC.Embedded.1"),
        _line("rack controller", MGMT[0], 1, "verified"),
        _line("gpu", "NVIDIA Rubin", trays * GPU_PER_TRAY, "verified",
              "silicon", vendor="NVIDIA"),
        _line("dpu", "NVIDIA BlueField DPU", len(rack.dpus), "verified",
              "silicon", vendor="NVIDIA"),
    ]


def site_bom_lines() -> list:
    return [
        _line("cdu", CDU_MODEL, None, "verified",
              ">220 kW per unit in 4U; twin models one SU-level in-row CDU"),
        _line("ethernet leaf/spine", LEAF_PRODUCT, None, "verified",
              "Spectrum-4, 64x800GbE"),
        _line("infiniband switch", IB_PRODUCT, None, "unverified",
              "Dell resale verified for Q3300-LD only"),
        _line("storage", f"{STORAGE_PRODUCT} ({STORAGE_OS})", None, "verified",
              "served at /powerscale/v1 (legacy /vast/v1 kept)"),
        _line("management", MGMT[1], None, "verified"),
    ]


def summary() -> dict:
    return {"hw_vendor": HW_VENDOR, "manufacturer": MANUFACTURER,
            "compute": COMPUTE_MODEL, "rack": RACK_MODEL, "bmc": IDRAC_MODEL,
            "cdu": CDU_MODEL, "ethernet": LEAF_PRODUCT, "infiniband": IB_PRODUCT,
            "storage": STORAGE_PRODUCT, "management": MGMT,
            "silicon_vendor": "NVIDIA"}


router = APIRouter(tags=["dell"])


@router.get("/emulator/v1/inventory/bom")
def bom():
    with STORE.lock:
        racks = list(STORE.racks.values())
        tmpl = rack_bom_lines(racks[0]) if racks else []
        return {**summary(), "racks": len(racks),
                "rack_bom": tmpl, "site_bom": site_bom_lines(),
                "rack_ids": [r.rack_id for r in racks]}


@router.get("/emulator/v1/inventory/bom/{rack_id}")
def rack_bom(rack_id: str):
    with STORE.lock:
        r = STORE.racks.get(rack_id)
        if not r:
            raise HTTPException(404, f"rack {rack_id} not found")
        return {"rack_id": rack_id, "hw_vendor": HW_VENDOR,
                "service_tag": service_tag(rack_id), "model": RACK_MODEL,
                "lines": rack_bom_lines(r),
                "trays": [{"tray_id": t, "model": COMPUTE_MODEL,
                           "service_tag": service_tag(t),
                           "idrac": IDRAC_MANAGER_ID,
                           "bmc_ip": STORE.trays[t].bmc_ip} for t in r.trays]}


# ── per-iDRAC aliases (Dell resource ids) ────────────────────────────
def _alias_ids(view: dict, tray_id: str) -> dict:
    base = f"/redfish/v1/Dell/{tray_id}"
    v = dict(view)
    v["Oem"] = {**v.get("Oem", {}),
                "Dell": {**v.get("Oem", {}).get("Dell", {}),
                         "CanonicalUri": view["@odata.id"]}}
    kind = view["@odata.id"].split("/")[3]          # Managers|Systems|Chassis
    rid = IDRAC_MANAGER_ID if kind == "Managers" else DELL_SYSTEM_ID
    v["@odata.id"] = f"{base}/{kind}/{rid}"
    v["Id"] = rid
    return v


@router.get(f"/redfish/v1/Dell/{{tray_id}}/Managers/{IDRAC_MANAGER_ID}")
def idrac_manager(tray_id: str):
    from .redfish import _manager_view, _tray
    with STORE.lock:
        _tray(tray_id)
        return _alias_ids(_manager_view(tray_id), tray_id)


@router.get(f"/redfish/v1/Dell/{{tray_id}}/Systems/{DELL_SYSTEM_ID}")
def idrac_system(tray_id: str):
    from .redfish import _system_view, _tray
    with STORE.lock:
        return _alias_ids(_system_view(_tray(tray_id)), tray_id)


@router.get(f"/redfish/v1/Dell/{{tray_id}}/Chassis/{DELL_SYSTEM_ID}")
def idrac_chassis(tray_id: str):
    from .redfish import chassis, _tray
    with STORE.lock:
        _tray(tray_id)
    return _alias_ids(chassis(tray_id), tray_id)
