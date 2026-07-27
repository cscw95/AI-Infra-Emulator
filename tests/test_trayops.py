"""Tests for the compute-tray reboot / HW-replace lifecycle + KPI (app/trayops.py).

Verifies tick state-machine convergence, tenant attachment rejoin, replace with
a new serial/MAC/IP, KPI aggregation, rejoin skip for trays with no tenant, obs
alert fire→resolve (propagated to /faults), and per-device fault-target
resolution (UFM guid/name, VAST cluster name)."""
from app.store import STORE
from app.trayops import ENGINE as TOPS

TRAY = "su-1-rack-00-tray-03"
DPU = f"{TRAY}-dpu-0"


def _run(n=12):
    """Advance one stage per tick — bypass the TICK_SEC gate to force n stages."""
    for _ in range(n):
        TOPS.tick(force=True)


def _attach(client, tenant="tenant-ops"):
    body = {"tenant_id": tenant,
            "network": {"network_id": f"net-{tenant}", "tenant_id": tenant,
                        "network_type": "vxlan", "vni": 5900,
                        "vrf": tenant, "subnet": "10.9.9.0/24"}}
    r = client.post(f"/emulator/v1/dpus/{DPU}/tenant-attachments", json=body)
    assert r.status_code == 200, r.text


def test_reboot_walks_all_stages_to_in_service(client):
    r = client.post(f"/emulator/v1/trayops/{TRAY}/reboot")
    assert r.status_code == 200
    body = r.json()
    assert body["op"] == "reboot" and body["stage_idx"] == 0
    assert [s["name"] for s in body["stages"]] == [
        "power_cycle", "post", "nico_discovery", "dhcp_ip", "boot",
        "attestation", "tenant_rejoin", "in_service"]
    # a duplicate start returns 409
    assert client.post(f"/emulator/v1/trayops/{TRAY}/reboot").status_code == 409
    _run(10)
    d = client.get("/emulator/v1/obs/tray-ops").json()
    assert not any(o["tray_id"] == TRAY for o in d["inflight"])
    h = next(x for x in d["history"] if x["tray_id"] == TRAY)
    assert h["succeeded"] is True and h["total_s"] >= 0
    # per-stage measured duration is recorded
    for k in ("power_cycle", "post", "nico_discovery", "dhcp_ip", "boot",
              "attestation"):
        assert k in h["stage_durations"], k
    tray = STORE.trays[TRAY]
    assert tray.boot_stage == "HostReady"
    assert tray.lifecycle_state == "Ready"       # no tenant
    assert tray.health == "ok"


def test_reboot_rejoins_tenant_attachment(client):
    _attach(client)
    assert any(a["tenant_id"] == "tenant-ops" and a["dpu_id"] == DPU
               for a in STORE.attachments.values())
    client.post(f"/emulator/v1/trayops/{TRAY}/reboot")
    # while in progress the attachment is detached (the op remembers the original tenant)
    assert not any(a["dpu_id"] == DPU for a in STORE.attachments.values())
    _run(10)
    att = [a for a in STORE.attachments.values() if a["dpu_id"] == DPU]
    assert att and att[0]["tenant_id"] == "tenant-ops"
    assert any(f.tenant_id == "tenant-ops"
               for f in STORE.dpus[DPU].functions.values())
    assert STORE.trays[TRAY].lifecycle_state == "InService"
    h = next(x for x in client.get("/emulator/v1/obs/tray-ops").json()["history"]
             if x["tray_id"] == TRAY)
    assert h["tenant_id"] == "tenant-ops"
    assert "tenant_rejoin" in h["stage_durations"]


def test_replace_new_serial_mac_and_ip(client):
    # capture the existing lease (PXE boot walk)
    client.post(f"/emulator/v1/provision/{TRAY}")
    for _ in range(4):
        client.post(f"/emulator/v1/provision/{TRAY}/step")
    old = client.get("/emulator/v1/dhcp/leases",
                     params={"tray_id": TRAY}).json()[0]
    old_serial = STORE.trays[TRAY].serial
    r = client.post(f"/emulator/v1/trayops/{TRAY}/replace")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()["stages"]]
    assert names[0] == "drain" and "hw_swap" in names \
        and "pxe_os_install" in names
    _run(12)
    lease = client.get("/emulator/v1/dhcp/leases",
                       params={"tray_id": TRAY}).json()[0]
    assert lease["ip_address"] != old["ip_address"]      # new MAC → new IP
    assert lease["mac_address"] != old["mac_address"]
    assert STORE.trays[TRAY].serial != old_serial        # register the new serial
    assert STORE.trays[TRAY].mac_address == lease["mac_address"]
    h = next(x for x in client.get("/emulator/v1/obs/tray-ops").json()["history"]
             if x["tray_id"] == TRAY)
    assert h["op"] == "replace" and "pxe_os_install" in h["stage_durations"]


def test_kpi_aggregation(client):
    d0 = client.get("/emulator/v1/obs/tray-ops").json()
    assert d0["kpi"]["ops_24h"] >= 2             # 2 demo samples seeded on reset
    assert any(h.get("note") == "(sample)" for h in d0["history"])
    client.post(f"/emulator/v1/trayops/{TRAY}/reboot")
    _run(10)
    client.post(f"/emulator/v1/trayops/{TRAY}/replace")
    _run(12)
    k = client.get("/emulator/v1/obs/tray-ops").json()["kpi"]
    assert k["ops_24h"] >= 4
    assert k["reboots"] >= 2 and k["replacements"] >= 2   # includes the 1+1 samples
    for key in ("avg_discovery_s", "avg_ip_s", "avg_os_install_s",
                "avg_rejoin_s", "avg_total_s", "rejoin_success_pct"):
        assert key in k, key
    assert k["avg_total_s"] > 0
    assert 0.0 <= k["rejoin_success_pct"] <= 100.0


def test_reboot_without_tenant_skips_rejoin(client):
    client.post(f"/emulator/v1/trayops/{TRAY}/reboot")
    _run(10)
    h = next(x for x in client.get("/emulator/v1/obs/tray-ops").json()["history"]
             if x["tray_id"] == TRAY)
    assert h["tenant_id"] is None
    assert "tenant_rejoin" in h["skipped"]               # marked skip
    assert "tenant_rejoin" not in h["stage_durations"]


def test_trayop_alert_fires_resolves_and_feeds_faults(client):
    _attach(client)
    client.post(f"/emulator/v1/trayops/{TRAY}/reboot")
    alerts = client.get("/emulator/v1/obs/alerts").json()
    a = next(x for x in alerts
             if x["domain"] == "trayops" and x["resource"] == TRAY)
    assert a["state"] == "firing" and "TRAY_REBOOTING" in a["summary"]
    assert "tenant-ops" in a["summary"]                  # indicates tenant impact
    f = client.get("/emulator/v1/faults").json()
    rec = next(x for x in f["recent"]
               if x["tray_id"] == TRAY and x["kind"] == "trayops")
    assert rec["resolved"] is False
    _run(10)                                             # reach in_service
    alerts = client.get("/emulator/v1/obs/alerts").json()
    a = next(x for x in alerts
             if x["domain"] == "trayops" and x["resource"] == TRAY)
    assert a["state"] == "resolved"
    f2 = client.get("/emulator/v1/faults").json()
    rec2 = next(x for x in f2["recent"]
                if x["tray_id"] == TRAY and x["kind"] == "trayops")
    assert rec2["resolved"] is True


def test_trayop_marks_gpus_idle_and_tenant_unavail(client):
    from app.observability import ENGINE as OBS
    _attach(client)
    client.post(f"/emulator/v1/trayops/{TRAY}/reboot")
    OBS.tick(force=True)
    gpus = client.get("/emulator/v1/obs/dcgm/gpus",
                      params={"tenant": "tenant-ops"}).json()
    assert gpus["total"] == 4                    # still counted against the original tenant
    assert all(g["state"] == "idle" for g in gpus["gpus"])
    slo = client.get("/emulator/v1/obs/slo").json()
    t = next(x for x in slo["tenants"] if x["tenant_id"] == "tenant-ops")
    assert t["contracted_gpus"] == 4 and t["available_gpus"] == 0
    _run(10)
    OBS.tick(force=True)
    slo2 = client.get("/emulator/v1/obs/slo").json()
    t2 = next(x for x in slo2["tenants"] if x["tenant_id"] == "tenant-ops")
    assert t2["available_gpus"] == 4             # normal after return


# ── per-device individual control (feature 1) — fault-target resolution ──────────────────
def test_ufm_switch_targeting_by_guid_and_name(client):
    systems = client.get("/ufm/v1/resources/systems").json()["systems"]
    leaf = next(s for s in systems if s["type"] == "leaf")
    r = client.post("/ufm/v1/faults/inject",
                    json={"kind": "switch_down", "target": leaf["guid"]})
    assert r.status_code == 200 and r.json()["target"] == leaf["name"]
    sw = next(s for s in client.get("/ufm/v1/resources/systems")
              .json()["systems"] if s["guid"] == leaf["guid"])
    assert sw["state"] == "down"
    r2 = client.post("/ufm/v1/faults/recover", json={"target": leaf["name"]})
    assert leaf["name"] in r2.json()["recovered"]
    sw2 = next(s for s in client.get("/ufm/v1/resources/systems")
               .json()["systems"] if s["guid"] == leaf["guid"])
    assert sw2["state"] == "ok"
    # name target → link_degrade on the links associated with that switch
    r3 = client.post("/ufm/v1/faults/inject",
                     json={"kind": "link_degrade", "target": leaf["name"]})
    assert r3.status_code == 200 and leaf["name"] in r3.json()["target"]
    client.post("/ufm/v1/faults/recover", json={"target": leaf["name"]})


def test_vast_cluster_targeting_by_name(client):
    r = client.post("/vast/v1/faults/inject",
                    json={"kind": "latency_spike", "target": "vast-gasan"})
    assert r.status_code == 200 and r.json()["target"] == "vast-gasan"
    c = next(x for x in client.get("/vast/v1/clusters").json()["clusters"]
             if x["name"] == "vast-gasan")
    assert c["state"] == "DEGRADED"
    r2 = client.post("/vast/v1/faults/recover", json={"target": "vast-gasan"})
    assert any("vast-gasan" in x for x in r2.json()["recovered"])
    c2 = next(x for x in client.get("/vast/v1/clusters").json()["clusters"]
              if x["name"] == "vast-gasan")
    assert c2["state"] == "HEALTHY"
