"""컴퓨트 트레이 재기동·HW 교체 수명주기 + KPI (app/trayops.py) 테스트.

tick 상태머신 수렴, tenant attachment rejoin, replace 신규 시리얼/MAC/IP,
KPI 집계, 테넌트 없는 트레이 rejoin skip, obs 알림 fire→resolve(/faults 전파),
그리고 장비별 fault target 해석(UFM guid/name, VAST 클러스터 name)을 검증한다."""
from app.store import STORE
from app.trayops import ENGINE as TOPS

TRAY = "su-1-rack-00-tray-03"
DPU = f"{TRAY}-dpu-0"


def _run(n=12):
    """tick당 1단계 진행 — TICK_SEC 게이트를 우회해 n단계 강제 진행."""
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
    # 중복 시작은 409
    assert client.post(f"/emulator/v1/trayops/{TRAY}/reboot").status_code == 409
    _run(10)
    d = client.get("/emulator/v1/obs/tray-ops").json()
    assert not any(o["tray_id"] == TRAY for o in d["inflight"])
    h = next(x for x in d["history"] if x["tray_id"] == TRAY)
    assert h["succeeded"] is True and h["total_s"] >= 0
    # 단계별 실측 duration 기록
    for k in ("power_cycle", "post", "nico_discovery", "dhcp_ip", "boot",
              "attestation"):
        assert k in h["stage_durations"], k
    tray = STORE.trays[TRAY]
    assert tray.boot_stage == "HostReady"
    assert tray.lifecycle_state == "Ready"       # 테넌트 없음
    assert tray.health == "ok"


def test_reboot_rejoins_tenant_attachment(client):
    _attach(client)
    assert any(a["tenant_id"] == "tenant-ops" and a["dpu_id"] == DPU
               for a in STORE.attachments.values())
    client.post(f"/emulator/v1/trayops/{TRAY}/reboot")
    # 진행 중에는 attachment 해제 상태 (원 테넌트는 op가 기억)
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
    # 기존 lease 확보 (PXE 부트 워크)
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
    assert lease["ip_address"] != old["ip_address"]      # 신규 MAC → 신규 IP
    assert lease["mac_address"] != old["mac_address"]
    assert STORE.trays[TRAY].serial != old_serial        # 신규 시리얼 등록
    assert STORE.trays[TRAY].mac_address == lease["mac_address"]
    h = next(x for x in client.get("/emulator/v1/obs/tray-ops").json()["history"]
             if x["tray_id"] == TRAY)
    assert h["op"] == "replace" and "pxe_os_install" in h["stage_durations"]


def test_kpi_aggregation(client):
    d0 = client.get("/emulator/v1/obs/tray-ops").json()
    assert d0["kpi"]["ops_24h"] >= 2             # 리셋 시 데모 샘플 2건 시드
    assert any(h.get("note") == "(sample)" for h in d0["history"])
    client.post(f"/emulator/v1/trayops/{TRAY}/reboot")
    _run(10)
    client.post(f"/emulator/v1/trayops/{TRAY}/replace")
    _run(12)
    k = client.get("/emulator/v1/obs/tray-ops").json()["kpi"]
    assert k["ops_24h"] >= 4
    assert k["reboots"] >= 2 and k["replacements"] >= 2   # 샘플 1+1 포함
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
    assert "tenant_rejoin" in h["skipped"]               # skip 표기
    assert "tenant_rejoin" not in h["stage_durations"]


def test_trayop_alert_fires_resolves_and_feeds_faults(client):
    _attach(client)
    client.post(f"/emulator/v1/trayops/{TRAY}/reboot")
    alerts = client.get("/emulator/v1/obs/alerts").json()
    a = next(x for x in alerts
             if x["domain"] == "trayops" and x["resource"] == TRAY)
    assert a["state"] == "firing" and "TRAY_REBOOTING" in a["summary"]
    assert "tenant-ops" in a["summary"]                  # tenant 영향 표기
    f = client.get("/emulator/v1/faults").json()
    rec = next(x for x in f["recent"]
               if x["tray_id"] == TRAY and x["kind"] == "trayops")
    assert rec["resolved"] is False
    _run(10)                                             # in_service 도달
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
    assert gpus["total"] == 4                    # 원 테넌트로 계속 집계
    assert all(g["state"] == "idle" for g in gpus["gpus"])
    slo = client.get("/emulator/v1/obs/slo").json()
    t = next(x for x in slo["tenants"] if x["tenant_id"] == "tenant-ops")
    assert t["contracted_gpus"] == 4 and t["available_gpus"] == 0
    _run(10)
    OBS.tick(force=True)
    slo2 = client.get("/emulator/v1/obs/slo").json()
    t2 = next(x for x in slo2["tenants"] if x["tenant_id"] == "tenant-ops")
    assert t2["available_gpus"] == 4             # 복귀 후 정상


# ── 장비별 개별 제어(기능 1) — fault target 해석 검증 ──────────────────
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
    # name target → 해당 스위치 연관 링크에 link_degrade
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
