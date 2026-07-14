"""Redfish BMC emulator — Vera Rubin NVL72 twin (DMTF Redfish subset).

Exposes host BMCs (one ComputerSystem + Chassis + Manager per compute tray)
and DPU BMCs (BlueField ComputerSystem/Manager per DPU) over a Redfish-shaped
API. State is backed by the STORE twin; the Off→PoweringOn→On power state
machine is delegated to STORE.set_power / STORE.power_state.

Router is mounted at prefix /redfish/v1 by app.main."""
import itertools
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException

from .store import STORE, _iso
from . import models as m

router = APIRouter(prefix="/redfish/v1", tags=["redfish"])

# EventService subscriptions live for the process lifetime (design DPU-RF-*).
_SUBSCRIPTIONS: Dict[str, dict] = {}
_sub_ids = itertools.count(1)


# ── helpers ───────────────────────────────────────────────────────────
def _tray(tray_id: str):
    t = STORE.trays.get(tray_id)
    if not t:
        raise HTTPException(404, f"ComputerSystem {tray_id} not found")
    return t


def _dpu(dpu_id: str):
    d = STORE.dpus.get(dpu_id)
    if not d:
        raise HTTPException(404, f"DPU system {dpu_id} not found")
    return d


def _rf_health(h: str) -> str:
    return {"ok": "OK", "warning": "Warning", "critical": "Critical"}.get(h, "OK")


def _tray_idx(tray_id: str) -> int:
    try:
        return int(tray_id.split("-")[-1])
    except (ValueError, IndexError):
        return abs(hash(tray_id)) % 18 + 1


def _tray_power_watts(tray) -> int:
    """Synthetic board power. ~gpus*180 W under load + platform base, near
    idle/standby when powered off."""
    if STORE.power_state(tray) == "Off":
        return 18  # standby BMC/DPU rail only
    return tray.gpus * 180 + 260  # +CPU/DPU/NVSwitch/board


# ── views ─────────────────────────────────────────────────────────────
def _system_view(tray) -> dict:
    ps = STORE.power_state(tray)
    return {
        "@odata.type": "#ComputerSystem.v1_20_0.ComputerSystem",
        "@odata.id": f"/redfish/v1/Systems/{tray.tray_id}",
        "Id": tray.tray_id,
        "Name": f"Rubin Compute Tray {tray.tray_id}",
        "SystemType": "Physical",
        "Manufacturer": "NVIDIA",
        "Model": "Vera Rubin NVL72 Compute Tray",
        "PowerState": ps,
        "ProcessorSummary": {"Count": tray.gpus, "Model": "NVIDIA Rubin",
                             "Status": {"State": "Enabled",
                                        "Health": _rf_health(tray.health)}},
        "MemorySummary": {"TotalSystemMemoryGiB": tray.gpus * 288},
        "Boot": {
            "BootSourceOverrideEnabled": tray.boot_enabled,
            "BootSourceOverrideTarget": tray.boot_source,
            "BootSourceOverrideTarget@Redfish.AllowableValues":
                ["None", "Pxe", "Hdd", "Cd", "Usb", "BiosSetup"],
            "BootSourceOverrideEnabled@Redfish.AllowableValues":
                ["Disabled", "Once", "Continuous"],
        },
        "Status": {"State": "Enabled", "Health": _rf_health(tray.health)},
        "Links": {
            "Chassis": [{"@odata.id": f"/redfish/v1/Chassis/{tray.tray_id}"}],
            "ManagedBy": [{"@odata.id": f"/redfish/v1/Managers/{tray.tray_id}"}],
        },
        "Actions": {
            "#ComputerSystem.Reset": {
                "target": f"/redfish/v1/Systems/{tray.tray_id}"
                          "/Actions/ComputerSystem.Reset",
                "ResetType@Redfish.AllowableValues":
                    ["On", "ForceOff", "GracefulShutdown", "ForceRestart",
                     "GracefulRestart"],
            }
        },
        "Oem": {"Nvidia": {
            "ComputeTrayId": tray.tray_id,
            "DpuId": tray.dpu_id,
            "LifecycleState": tray.lifecycle_state,
            "BootStage": tray.boot_stage,
            "BmcIp": tray.bmc_ip,
        }},
    }


def _dpu_system_view(d) -> dict:
    power = "Off" if d.arm_os_state == "off" else "On"
    return {
        "@odata.type": "#ComputerSystem.v1_20_0.ComputerSystem",
        "@odata.id": f"/redfish/v1/Systems/Bluefield/{d.dpu_id}",
        "Id": f"Bluefield/{d.dpu_id}",
        "Name": f"BlueField DPU {d.dpu_id}",
        "SystemType": "DPU",
        "Manufacturer": "NVIDIA",
        "Model": "BlueField-3 DPU",
        "PowerState": power,
        "ProcessorSummary": {"Count": 1, "Model": "NVIDIA BlueField Arm",
                             "Status": {"State": "Enabled",
                                        "Health": _rf_health(d.health)}},
        "Status": {"State": "Enabled", "Health": _rf_health(d.health)},
        "Links": {
            "ManagedBy": [{"@odata.id": f"/redfish/v1/Managers/{d.dpu_id}"}],
            "Oem": {"Nvidia": {"ComputeTray":
                    {"@odata.id": f"/redfish/v1/Systems/{d.compute_tray_id}"}}},
        },
        "Actions": {
            "#ComputerSystem.Reset": {
                "target": f"/redfish/v1/Systems/Bluefield/{d.dpu_id}"
                          "/Actions/ComputerSystem.Reset",
                "ResetType@Redfish.AllowableValues":
                    ["On", "ForceOff", "GracefulShutdown", "ForceRestart"],
            }
        },
        "Oem": {"Nvidia": {
            "DpuId": d.dpu_id,
            "ComputeTrayId": d.compute_tray_id,
            "OperatingMode": d.operating_mode,
            "BmcState": d.bmc_state,
            "ArmOsState": d.arm_os_state,
            "AttestationState": d.attestation_state,
            "SecureBootEnabled": d.secure_boot_enabled,
            "BmcIp": d.bmc_ip,
        }},
    }


def _resolve_manager(manager_id: str) -> Tuple[Optional[str], object]:
    if manager_id in STORE.trays:
        return "tray", STORE.trays[manager_id]
    if manager_id in STORE.dpus:
        return "dpu", STORE.dpus[manager_id]
    return None, None


def _manager_view(manager_id: str) -> dict:
    kind, obj = _resolve_manager(manager_id)
    if kind is None:
        raise HTTPException(404, f"Manager {manager_id} not found")
    if kind == "tray":
        health = _rf_health(obj.health)
        bmc_ip = obj.bmc_ip
        managed = {"@odata.id": f"/redfish/v1/Systems/{obj.tray_id}"}
        mtype = "BMC"
        name = f"Host BMC {manager_id}"
        oem = {"Nvidia": {"ManagerFor": "ComputeTray",
                          "ComputeTrayId": obj.tray_id}}
    else:
        health = _rf_health(obj.health)
        bmc_ip = obj.bmc_ip
        managed = {"@odata.id": f"/redfish/v1/Systems/Bluefield/{obj.dpu_id}"}
        mtype = "BMC"
        name = f"DPU BMC {manager_id}"
        oem = {"Nvidia": {"ManagerFor": "DPU", "DpuId": obj.dpu_id,
                          "BmcState": obj.bmc_state,
                          "ArmOsState": obj.arm_os_state}}
    return {
        "@odata.type": "#Manager.v1_18_0.Manager",
        "@odata.id": f"/redfish/v1/Managers/{manager_id}",
        "Id": manager_id,
        "Name": name,
        "ManagerType": mtype,
        "Manufacturer": "NVIDIA",
        "FirmwareVersion": "1.4.2",
        "PowerState": "On",
        "Status": {"State": "Enabled", "Health": health},
        "EthernetInterfaces": {"IPv4Address": bmc_ip},
        "Links": {"ManagerForServers": [managed]},
        "Actions": {"#Manager.Reset": {
            "target": f"/redfish/v1/Managers/{manager_id}"
                      "/Actions/Manager.Reset",
            "ResetType@Redfish.AllowableValues":
                ["GracefulRestart", "ForceRestart"],
        }},
        "Oem": oem,
    }


# ── Service Root ──────────────────────────────────────────────────────
@router.get("")
def service_root():
    return {
        "@odata.type": "#ServiceRoot.v1_16_0.ServiceRoot",
        "@odata.id": "/redfish/v1",
        "Id": "RootService",
        "Name": "NICo BMC Service Root",
        "RedfishVersion": "1.16.0",
        "Product": "NVIDIA Infra Controller — Vera Rubin NVL72",
        "Systems": {"@odata.id": "/redfish/v1/Systems"},
        "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
        "Managers": {"@odata.id": "/redfish/v1/Managers"},
        "UpdateService": {"@odata.id": "/redfish/v1/UpdateService"},
        "EventService": {"@odata.id": "/redfish/v1/EventService"},
    }


# ── Systems ───────────────────────────────────────────────────────────
@router.get("/Systems")
def systems():
    with STORE.lock:
        members = [{"@odata.id": f"/redfish/v1/Systems/{t}"}
                   for t in STORE.trays]
        return {
            "@odata.type": "#ComputerSystemCollection.ComputerSystemCollection",
            "@odata.id": "/redfish/v1/Systems",
            "Name": "Compute Tray System Collection",
            "Members@odata.count": len(members),
            "Members": members,
            "Oem": {"Nvidia": {"DpuSystems":
                    {"@odata.id": "/redfish/v1/Systems/Bluefield"}}},
        }


# DPU (BlueField) systems — declared before the generic {tray_id} route.
@router.get("/Systems/Bluefield")
def dpu_systems():
    with STORE.lock:
        members = [{"@odata.id": f"/redfish/v1/Systems/Bluefield/{d}"}
                   for d in STORE.dpus]
        return {
            "@odata.type": "#ComputerSystemCollection.ComputerSystemCollection",
            "@odata.id": "/redfish/v1/Systems/Bluefield",
            "Name": "BlueField DPU System Collection",
            "Members@odata.count": len(members),
            "Members": members,
        }


@router.get("/Systems/Bluefield/{dpu_id}")
def dpu_system(dpu_id: str):
    with STORE.lock:
        return _dpu_system_view(_dpu(dpu_id))


@router.post("/Systems/Bluefield/{dpu_id}/Actions/ComputerSystem.Reset")
def dpu_system_reset(dpu_id: str, body: m.ResetAction):
    with STORE.lock:
        d = _dpu(dpu_id)
        rt = body.ResetType
        if rt in ("ForceOff", "GracefulShutdown"):
            d.arm_os_state = "off"
        else:  # On / ForceRestart / GracefulRestart
            d.arm_os_state = "ready"
            d.bmc_state = "ok"
            d.health = "ok"
            d.telemetry["dpu_arm_up"] = 1
        STORE.event("info", "NeoCloudEmulator.1.0.DpuSystemReset", [dpu_id, rt])
        power = "Off" if d.arm_os_state == "off" else "On"
        return {"status": "accepted", "ResetType": rt, "PowerState": power,
                "ArmOsState": d.arm_os_state}


@router.get("/Systems/{tray_id}")
def system(tray_id: str):
    with STORE.lock:
        return _system_view(_tray(tray_id))


@router.post("/Systems/{tray_id}/Actions/ComputerSystem.Reset")
def system_reset(tray_id: str, body: m.ResetAction):
    with STORE.lock:
        tray = _tray(tray_id)
        STORE.set_power(tray, body.ResetType)
        ps = STORE.power_state(tray)
        STORE.event("info", "NeoCloudEmulator.1.0.ComputerSystemReset",
                    [tray_id, body.ResetType])
        return {"status": "accepted", "ResetType": body.ResetType,
                "PowerState": ps}


@router.patch("/Systems/{tray_id}")
def patch_system(tray_id: str, body: dict):
    """Accept Redfish {"Boot":{BootSourceOverrideTarget,BootSourceOverride
    Enabled}} or the BootOverride model shape {"boot_source","enabled"}."""
    with STORE.lock:
        tray = _tray(tray_id)
        boot = body.get("Boot")
        if isinstance(boot, dict):
            if "BootSourceOverrideTarget" in boot:
                tray.boot_source = boot["BootSourceOverrideTarget"]
            if "BootSourceOverrideEnabled" in boot:
                tray.boot_enabled = boot["BootSourceOverrideEnabled"]
        else:
            if "boot_source" in body:
                tray.boot_source = body["boot_source"]
            if "enabled" in body:
                tray.boot_enabled = body["enabled"]
        STORE.event("info", "NeoCloudEmulator.1.0.BootOverrideSet",
                    [tray_id, tray.boot_source, tray.boot_enabled])
        return _system_view(tray)


# ── Chassis ───────────────────────────────────────────────────────────
@router.get("/Chassis")
def chassis_collection():
    with STORE.lock:
        members = [{"@odata.id": f"/redfish/v1/Chassis/{t}"}
                   for t in STORE.trays]
        return {
            "@odata.type": "#ChassisCollection.ChassisCollection",
            "@odata.id": "/redfish/v1/Chassis",
            "Name": "Chassis Collection",
            "Members@odata.count": len(members),
            "Members": members,
        }


@router.get("/Chassis/{tray_id}")
def chassis(tray_id: str):
    with STORE.lock:
        tray = _tray(tray_id)
        idx = _tray_idx(tray_id)
        watts = _tray_power_watts(tray)
        gpu_c = 40 if watts < 100 else 55 + (idx % 8)
        return {
            "@odata.type": "#Chassis.v1_25_0.Chassis",
            "@odata.id": f"/redfish/v1/Chassis/{tray_id}",
            "Id": tray_id,
            "Name": f"Compute Tray Chassis {tray_id}",
            "ChassisType": "Blade",
            "Manufacturer": "NVIDIA",
            "Model": "Vera Rubin NVL72 Compute Tray",
            "PowerState": STORE.power_state(tray),
            "Status": {"State": "Enabled", "Health": _rf_health(tray.health)},
            "PowerWatts": watts,
            "Thermal": {"Inlet": {"ReadingCelsius": 24},
                        "GpuMax": {"ReadingCelsius": gpu_c}},
            "Sensors": {"@odata.id":
                        f"/redfish/v1/Chassis/{tray_id}/Sensors"},
            "EnvironmentMetrics": {"@odata.id":
                        f"/redfish/v1/Chassis/{tray_id}/EnvironmentMetrics"},
            "Links": {
                "ComputerSystems": [{"@odata.id":
                    f"/redfish/v1/Systems/{tray_id}"}],
                "ManagedBy": [{"@odata.id":
                    f"/redfish/v1/Managers/{tray_id}"}],
            },
        }


def _sensor_readings(tray):
    idx = _tray_idx(tray.tray_id)
    watts = _tray_power_watts(tray)
    on = watts >= 100
    readings = [
        {"Name": "InletTemp", "ReadingType": "Temperature",
         "Reading": 24.0, "ReadingUnits": "Cel"},
        {"Name": "TrayPower", "ReadingType": "Power",
         "Reading": float(watts), "ReadingUnits": "W"},
    ]
    for g in range(tray.gpus):
        readings.append({
            "Name": f"GPU{g}_Temp", "ReadingType": "Temperature",
            "Reading": (40.0 if not on else 55.0 + ((idx + g) % 8)),
            "ReadingUnits": "Cel"})
    return readings


@router.get("/Chassis/{tray_id}/Sensors")
def chassis_sensors(tray_id: str):
    with STORE.lock:
        tray = _tray(tray_id)
        readings = _sensor_readings(tray)
        members = []
        for r in readings:
            sid = r["Name"]
            members.append({
                "@odata.id": f"/redfish/v1/Chassis/{tray_id}/Sensors/{sid}",
                "@odata.type": "#Sensor.v1_9_0.Sensor",
                "Id": sid, **r})
        return {
            "@odata.type": "#SensorCollection.SensorCollection",
            "@odata.id": f"/redfish/v1/Chassis/{tray_id}/Sensors",
            "Name": f"Sensor Collection {tray_id}",
            "Members@odata.count": len(members),
            "Members": members,
        }


@router.get("/Chassis/{tray_id}/EnvironmentMetrics")
def chassis_env_metrics(tray_id: str):
    with STORE.lock:
        tray = _tray(tray_id)
        idx = _tray_idx(tray_id)
        watts = _tray_power_watts(tray)
        on = watts >= 100
        return {
            "@odata.type": "#EnvironmentMetrics.v1_3_0.EnvironmentMetrics",
            "@odata.id": f"/redfish/v1/Chassis/{tray_id}/EnvironmentMetrics",
            "Name": f"Environment Metrics {tray_id}",
            "TemperatureCelsius": {"Reading": (40.0 if not on else
                                               55.0 + (idx % 8))},
            "PowerWatts": {"Reading": float(watts)},
            "EnergykWh": {"Reading": round(watts * 24 / 1000.0, 2)},
            "PowerLimitWatts": {"Reading": tray.gpus * 300 + 400},
        }


# ── Managers ──────────────────────────────────────────────────────────
@router.get("/Managers")
def managers_collection():
    with STORE.lock:
        members = [{"@odata.id": f"/redfish/v1/Managers/{t}"}
                   for t in STORE.trays]
        members += [{"@odata.id": f"/redfish/v1/Managers/{d}"}
                    for d in STORE.dpus]
        return {
            "@odata.type": "#ManagerCollection.ManagerCollection",
            "@odata.id": "/redfish/v1/Managers",
            "Name": "Manager Collection",
            "Members@odata.count": len(members),
            "Members": members,
        }


@router.get("/Managers/{manager_id}")
def manager(manager_id: str):
    with STORE.lock:
        return _manager_view(manager_id)


@router.post("/Managers/{manager_id}/Actions/Manager.Reset")
def manager_reset(manager_id: str, body: m.ResetAction):
    with STORE.lock:
        kind, obj = _resolve_manager(manager_id)
        if kind is None:
            raise HTTPException(404, f"Manager {manager_id} not found")
        if kind == "tray":
            obj.health = "ok"
        else:  # DPU BMC reboot
            obj.bmc_state = "ok"
            if obj.health == "critical":
                obj.health = "warning"
            obj.telemetry["dpu_up"] = 1
        STORE.event("info", "NeoCloudEmulator.1.0.ManagerReset",
                    [manager_id, body.ResetType])
        return {"status": "accepted", "ManagerType": "BMC",
                "ResetType": body.ResetType}


# ── UpdateService ─────────────────────────────────────────────────────
@router.get("/UpdateService")
def update_service():
    return {
        "@odata.type": "#UpdateService.v1_14_0.UpdateService",
        "@odata.id": "/redfish/v1/UpdateService",
        "Id": "UpdateService",
        "Name": "Update Service",
        "ServiceEnabled": True,
        "FirmwareInventory": {"@odata.id":
            "/redfish/v1/UpdateService/FirmwareInventory"},
        "Actions": {"#UpdateService.SimpleUpdate": {
            "target": "/redfish/v1/UpdateService/Actions/"
                      "UpdateService.SimpleUpdate",
            "TransferProtocol@Redfish.AllowableValues": ["HTTP", "HTTPS"],
        }},
    }


_FW_VERSIONS = {
    "BMC": ("1.4.2", "NVIDIA"),
    "BIOS": ("U-Boot-2024.01-rubin", "NVIDIA"),
    "DpuBmc": ("BF-24.10-3", "NVIDIA"),
    "DpuNic": ("24.39.1002", "NVIDIA Mellanox"),
}


@router.get("/UpdateService/FirmwareInventory")
def firmware_inventory():
    with STORE.lock:
        members = []
        for tid in STORE.trays:
            for comp in ("BMC", "BIOS"):
                ver, mfr = _FW_VERSIONS[comp]
                fid = f"{tid}-{comp}"
                members.append({
                    "@odata.id": f"/redfish/v1/UpdateService/"
                                 f"FirmwareInventory/{fid}",
                    "@odata.type": "#SoftwareInventory.v1_10_0."
                                   "SoftwareInventory",
                    "Id": fid, "Name": f"{comp} {tid}",
                    "Version": ver, "Manufacturer": mfr,
                    "Updateable": True,
                    "SoftwareId": comp,
                    "RelatedItem": [{"@odata.id":
                        f"/redfish/v1/Systems/{tid}"}]})
        for did in STORE.dpus:
            for comp in ("DpuBmc", "DpuNic"):
                ver, mfr = _FW_VERSIONS[comp]
                fid = f"{did}-{comp}"
                members.append({
                    "@odata.id": f"/redfish/v1/UpdateService/"
                                 f"FirmwareInventory/{fid}",
                    "@odata.type": "#SoftwareInventory.v1_10_0."
                                   "SoftwareInventory",
                    "Id": fid, "Name": f"{comp} {did}",
                    "Version": ver, "Manufacturer": mfr,
                    "Updateable": True,
                    "SoftwareId": comp,
                    "RelatedItem": [{"@odata.id":
                        f"/redfish/v1/Systems/Bluefield/{did}"}]})
        return {
            "@odata.type": "#SoftwareInventoryCollection."
                           "SoftwareInventoryCollection",
            "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory",
            "Name": "Firmware Inventory Collection",
            "Members@odata.count": len(members),
            "Members": members,
        }


@router.post("/UpdateService/Actions/UpdateService.SimpleUpdate")
def simple_update(body: dict):
    image = body.get("ImageURI", "")
    targets = body.get("Targets", []) or []
    with STORE.lock:
        STORE.event("info", "NeoCloudEmulator.1.0.FirmwareUpdateApplied",
                    [image, ",".join(str(t) for t in targets) or "all"])
    return {
        "@odata.type": "#Task.v1_7_0.Task",
        "Id": "fwupdate-1",
        "Name": "SimpleUpdate",
        "TaskState": "Completed",
        "TaskStatus": "OK",
        "ImageURI": image,
        "Targets": targets,
        "Messages": [{"MessageId": "Update.1.0.UpdateSuccessful",
                      "Message": "Firmware applied to targets."}],
    }


# ── EventService ──────────────────────────────────────────────────────
@router.get("/EventService")
def event_service():
    with STORE.lock:
        delivered = len(STORE.events)
    return {
        "@odata.type": "#EventService.v1_10_0.EventService",
        "@odata.id": "/redfish/v1/EventService",
        "Id": "EventService",
        "Name": "Event Service",
        "ServiceEnabled": True,
        "DeliveryRetryAttempts": 3,
        "DeliveryRetryIntervalSeconds": 60,
        "EventTypesForSubscription": ["Alert", "StatusChange",
                                      "ResourceUpdated"],
        "DeliveredEventCount": delivered,
        "Subscriptions": {"@odata.id":
            "/redfish/v1/EventService/Subscriptions"},
    }


@router.get("/EventService/Subscriptions")
def list_subscriptions():
    with STORE.lock:
        members = [{"@odata.id": f"/redfish/v1/EventService/"
                                 f"Subscriptions/{s}"}
                   for s in _SUBSCRIPTIONS]
        return {
            "@odata.type": "#EventDestinationCollection."
                           "EventDestinationCollection",
            "@odata.id": "/redfish/v1/EventService/Subscriptions",
            "Name": "Event Subscriptions Collection",
            "Members@odata.count": len(members),
            "Members": members,
        }


@router.post("/EventService/Subscriptions", status_code=201)
def create_subscription(body: dict):
    with STORE.lock:
        sid = f"sub-{next(_sub_ids):04d}"
        sub = {
            "@odata.type": "#EventDestination.v1_15_0.EventDestination",
            "@odata.id": f"/redfish/v1/EventService/Subscriptions/{sid}",
            "Id": sid,
            "Name": body.get("Name", f"Subscription {sid}"),
            "Destination": body.get("Destination", ""),
            "EventTypes": body.get("EventTypes", ["Alert"]),
            "Protocol": body.get("Protocol", "Redfish"),
            "Context": body.get("Context", ""),
            "SubscriptionType": body.get("SubscriptionType", "RedfishEvent"),
            "Status": {"State": "Enabled", "Health": "OK"},
            "CreatedAt": _iso(),
        }
        _SUBSCRIPTIONS[sid] = sub
        STORE.event("info", "NeoCloudEmulator.1.0.EventSubscriptionCreated",
                    [sid, sub["Destination"]])
        return sub


@router.get("/EventService/Subscriptions/{sub_id}")
def get_subscription(sub_id: str):
    with STORE.lock:
        sub = _SUBSCRIPTIONS.get(sub_id)
        if not sub:
            raise HTTPException(404, f"Subscription {sub_id} not found")
        return sub


@router.delete("/EventService/Subscriptions/{sub_id}")
def delete_subscription(sub_id: str):
    with STORE.lock:
        if _SUBSCRIPTIONS.pop(sub_id, None) is None:
            raise HTTPException(404, f"Subscription {sub_id} not found")
        return {"deleted": sub_id}
