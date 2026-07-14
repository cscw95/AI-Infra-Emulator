"""통합 Observability 텔레메트리 생성기 (app/observability.py) 테스트.

DCGM/DCIM/DLC/SLA 4개 플레인 + GPU↔DLC 열 결합(장애 주입→온도 상승→throttle
→alerts/correlate, recover→정상화)을 검증한다."""
from app.observability import ENGINE
from app.store import STORE, GPU_PER_TRAY


def _ticks(n=1):
    """랜덤워크/장애 다이내믹스를 n tick 강제로 진행 (TICK_SEC 게이트 우회)."""
    for _ in range(n):
        ENGINE.tick(force=True)


def _attach(client, dpu, tenant):
    body = {"tenant_id": tenant,
            "network": {"network_id": f"{dpu}-net-obs", "tenant_id": tenant,
                        "network_type": "vxlan", "vni": 5301,
                        "subnet": "10.9.0.0/24"}}
    r = client.post(f"/emulator/v1/dpus/{dpu}/tenant-attachments", json=body)
    assert r.status_code == 200, r.text


def test_summary_shape(client):
    s = client.get("/emulator/v1/obs/summary").json()
    assert set(s["gpus"]) == {"total", "active", "idle", "throttled",
                              "faulted", "off"}
    assert s["gpus"]["total"] == len(STORE.trays) * GPU_PER_TRAY
    for k in ("avg_util_pct", "it_power_mw", "cooling", "racks",
              "tenants", "alerts_open", "slo"):
        assert k in s, k
    assert set(s["cooling"]) == {"cdus", "alarms_open",
                                 "avg_utilization_pct", "headroom_kw"}
    assert s["racks"] == len(STORE.racks)
    assert s["cooling"]["cdus"] == 2          # NICO_RACKS_LIMIT=24 → su-1, su-2
    assert 0.0 <= s["slo"]["gpu_availability_pct"] <= 100.0
    assert s["it_power_mw"] > 0


def test_dcgm_filters_pagination_and_detail(client):
    r = client.get("/emulator/v1/obs/dcgm/gpus",
                   params={"rack": "su-1-rack-00", "limit": 100}).json()
    assert r["total"] == 72 and len(r["gpus"]) == 72
    g = r["gpus"][0]
    for k in ("gpu_uuid", "idx", "tray_id", "rack_id", "su_id", "site",
              "tenant_id", "util_pct", "sm_util_pct", "mem_used_gb",
              "mem_total_gb", "temp_c", "mem_temp_c", "power_w",
              "power_limit_w", "sm_clock_mhz", "throttle_reasons", "ecc_corr",
              "ecc_uncorr", "xid_recent", "nvlink_tx_gbps", "nvlink_rx_gbps",
              "pcie_replay", "health"):
        assert k in g, k
    assert g["mem_total_gb"] == 288
    p = client.get("/emulator/v1/obs/dcgm/gpus",
                   params={"limit": 10, "offset": 5}).json()
    assert len(p["gpus"]) == 10
    assert p["total"] == len(STORE.trays) * GPU_PER_TRAY
    # per-GPU detail + history
    d = client.get(f"/emulator/v1/obs/dcgm/gpus/{g['gpu_uuid']}").json()
    assert set(d["history"]) == {"ts", "util", "temp", "power"}
    assert len(d["history"]["ts"]) >= 1
    assert client.get("/emulator/v1/obs/dcgm/gpus/GPU-nope-g0").status_code == 404
    # tenant filter — 미할당은 idle, 할당 GPU만 매칭
    _attach(client, "su-1-rack-01-tray-00-dpu-0", "tenant-obs")
    _ticks(1)
    t = client.get("/emulator/v1/obs/dcgm/gpus",
                   params={"tenant": "tenant-obs"}).json()
    assert t["total"] == GPU_PER_TRAY
    assert all(x["tenant_id"] == "tenant-obs" for x in t["gpus"])
    assert all(x["util_pct"] >= 40 for x in t["gpus"])


def test_racks_and_cdu_physical_consistency(client):
    racks = client.get("/emulator/v1/obs/racks", params={"su": "su-1"}).json()
    assert len(racks) == 16
    for k in ("rack_id", "su_id", "site", "it_power_kw", "gpu_power_kw",
              "inlet_c", "outlet_c", "cdu_id", "cooling_headroom_kw",
              "throttled_gpus", "health"):
        assert k in racks[0], k
    assert all(r["cdu_id"] == "cdu-su-1" for r in racks)
    cdus = client.get("/emulator/v1/obs/dlc/cdus").json()
    assert {c["cdu_id"] for c in cdus} == {"cdu-su-1", "cdu-su-2"}
    c1 = next(c for c in cdus if c["cdu_id"] == "cdu-su-1")
    # 물리 정합: rack heat 합 ≈ CDU measured_heat (heat-capture 0.97)
    rack_heat = sum(r["it_power_kw"] for r in racks) * 0.97
    assert abs(c1["measured_heat_kw"] - rack_heat) < max(5.0, rack_heat * 0.02)
    # delta_t = return - supply
    sec = c1["secondary"]
    assert abs(sec["delta_t"] - (sec["return_c"] - sec["supply_c"])) < 0.2
    assert c1["oem"] == "Supermicro" and c1["type"] == "in-row-dlc2"
    assert c1["rated_capacity_kw"] == 1600.0
    assert len(c1["pumps"]) == 2
    # 상세: branch = rack당 1개(CDM), leak 센서 포함
    det = client.get("/emulator/v1/obs/dlc/cdus/cdu-su-1").json()
    assert len(det["branches"]) == 16
    assert {b["rack_id"] for b in det["branches"]} == {r["rack_id"] for r in racks}
    assert len(det["leak_sensors"]) == 17


def test_flow_loss_couples_gpu_thermals_alerts_correlation(client):
    _attach(client, "su-1-rack-02-tray-00-dpu-0", "tenant-hot")
    _ticks(1)

    def hot_gpus():
        return client.get("/emulator/v1/obs/dcgm/gpus",
                          params={"tenant": "tenant-hot"}).json()["gpus"]

    before = sum(g["temp_c"] for g in hot_gpus()) / GPU_PER_TRAY
    r = client.post("/emulator/v1/obs/dlc/cdus/cdu-su-1/inject",
                    json={"kind": "flow_loss"})
    assert r.status_code == 200 and r.json()["fault"] == "flow_loss"
    _ticks(12)                                # flow_factor 0.30, offset ≈ +31.5°C
    after_g = hot_gpus()
    after = sum(g["temp_c"] for g in after_g) / GPU_PER_TRAY
    assert after > before + 8, (before, after)
    assert any("thermal" in g["throttle_reasons"] for g in after_g)
    assert any(g["health"] in ("warning", "critical") for g in after_g)
    # summary 실시간 집계 반영
    s = client.get("/emulator/v1/obs/summary").json()
    assert s["gpus"]["throttled"] >= 1
    assert s["cooling"]["alarms_open"] >= 1
    # 정규화 알림: cooling FLOW_LOW firing + gpu thermal 알림
    alerts = client.get("/emulator/v1/obs/alerts").json()
    assert any(a["domain"] == "cooling" and a["state"] == "firing"
               and a["resource"] == "cdu-su-1" for a in alerts)
    assert any(a["domain"] == "gpu" and a["state"] == "firing" for a in alerts)
    # RCA 상관 뷰
    corr = client.get("/emulator/v1/obs/correlate/cooling").json()
    hit = next(x for x in corr if x["cdu_id"] == "cdu-su-1")
    assert "tenant-hot" in hit["tenant_impact"]
    assert hit["affected_gpus"] >= 1
    assert "su-1" in hit["finding"]
    # 미영향 CDU(su-2)는 상관 항목 없어야 함
    assert not any(x["cdu_id"] == "cdu-su-2" for x in corr)


def test_recover_normalizes_gradually(client):
    _attach(client, "su-2-rack-00-tray-00-dpu-0", "tenant-cool")
    client.post("/emulator/v1/obs/dlc/cdus/cdu-su-2/inject",
                json={"kind": "pump_failure"})
    _ticks(5)
    c = client.get("/emulator/v1/obs/dlc/cdus/cdu-su-2").json()
    assert any(p["state"] == "failed" for p in c["pumps"])
    assert any(a["code"] == "PUMP_FAILURE" for a in c["alarms"])
    assert c["health"] == "critical"
    client.post("/emulator/v1/obs/dlc/cdus/cdu-su-2/recover")
    _ticks(15)                                # flow 복구 + offset 점진 감쇠
    c2 = client.get("/emulator/v1/obs/dlc/cdus/cdu-su-2").json()
    assert all(p["state"] != "failed" for p in c2["pumps"])
    assert c2["alarms"] == [] and c2["health"] == "ok"
    corr = client.get("/emulator/v1/obs/correlate/cooling").json()
    assert not any(x["cdu_id"] == "cdu-su-2" for x in corr)
    alerts = client.get("/emulator/v1/obs/alerts").json()
    assert not any(a["resource"] == "cdu-su-2" and a["state"] == "firing"
                   for a in alerts)


def test_leak_closes_branch_and_fires_critical(client):
    client.post("/emulator/v1/obs/dlc/cdus/cdu-su-1/inject",
                json={"kind": "leak"})
    _ticks(2)
    d = client.get("/emulator/v1/obs/dlc/cdus/cdu-su-1").json()
    assert d["leak"]["detected"] is True and d["leak"]["location"]
    closed = [b for b in d["branches"] if b["valve"] == "closed"]
    assert len(closed) == 1 and closed[0]["flow_lpm"] == 0.0
    assert any(s["state"] == "wet" for s in d["leak_sensors"])
    alerts = client.get("/emulator/v1/obs/alerts").json()
    assert any(a["domain"] == "cooling" and a["severity"] == "critical"
               and a["state"] == "firing" for a in alerts)
    # 닫힌 branch의 랙은 critical
    racks = client.get("/emulator/v1/obs/racks", params={"su": "su-1"}).json()
    leak_rack = next(r for r in racks if r["rack_id"] == closed[0]["rack_id"])
    assert leak_rack["health"] == "critical"


def test_slo_tenant_calculation(client):
    _attach(client, "su-2-rack-01-tray-00-dpu-0", "tenant-slo")
    _ticks(1)
    s = client.get("/emulator/v1/obs/slo").json()
    t = next(x for x in s["tenants"] if x["tenant_id"] == "tenant-slo")
    assert t["contracted_gpus"] == GPU_PER_TRAY
    assert t["slo_target_pct"] == 99.5
    assert 0 <= t["available_gpus"] <= GPU_PER_TRAY
    assert 0.0 <= t["gpu_availability_pct"] <= 100.0
    for k in ("error_budget_remaining_pct", "burn_rate",
              "cooling_caused_unavail_min", "throttling_min"):
        assert k in t, k
    # availability와 available_gpus 정합
    assert abs(t["gpu_availability_pct"]
               - 100.0 * t["available_gpus"] / t["contracted_gpus"]) < 0.01


def test_alerts_include_provisioning_faults(client):
    # store 시드 샘플 fault(resolved)가 provisioning 도메인으로 노출
    alerts = client.get("/emulator/v1/obs/alerts").json()
    assert any(a["domain"] == "provisioning" for a in alerts)
    for a in alerts:
        for k in ("alert_id", "domain", "severity", "resource",
                  "summary", "at", "state"):
            assert k in a, k
