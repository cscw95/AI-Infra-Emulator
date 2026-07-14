"""AI Storage(VAST VMS) + Converged Network 에뮬레이터 검증.

clusters/views 테넌트 파생, performance 동적, fault inject→alarm→obs merge→
recover, converged paths, reset 재구축을 커버한다."""
from app import converged as converged_mod
from app import vast as vast_mod


def _attach(client, tenant="tnt-demo", vni=7001,
            dpu="su-1-rack-00-tray-00-dpu-0", suffix="a"):
    body = {
        "tenant_id": tenant,
        "network": {"network_id": f"{dpu}-net-{suffix}", "tenant_id": tenant,
                    "network_type": "vxlan", "vni": vni,
                    "subnet": "10.0.0.0/24"},
        "security": {"default_action": "deny", "spoof_check": True,
                     "allowed_macs": ["02:aa:00:00:00:01"]},
    }
    r = client.post(f"/emulator/v1/dpus/{dpu}/tenant-attachments", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_vast_clusters_seeded(client):
    r = client.get("/vast/v1/clusters")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    c = body["clusters"][0]
    assert c["name"] == f"vast-{c['site']}"
    assert c["cboxes"] == 8 and c["dboxes"] == 10
    assert c["raw_pb"] == 20.0 and c["usable_pb"] == 14.0
    assert 2.5 <= c["drr"] <= 3.5          # similarity reduction ~3:1 근방
    assert c["version"].startswith("5.")
    assert 0 < c["used_pb"] < c["usable_pb"]
    assert c["state"] == "HEALTHY"


def test_views_derived_from_tenant_attachment(client):
    # 시드 직후엔 테넌트 뷰 없음
    assert client.get("/vast/v1/views").json()["count"] == 0
    _attach(client, tenant="tnt-demo")
    body = client.get("/vast/v1/views", params={"tenant": "tnt-demo"}).json()
    assert body["count"] == 1
    v = body["views"][0]
    assert v["path"] == "/tnt-demo/dataset"
    assert v["cluster"].startswith("vast-")
    assert set(v["protocols"]) == {"NFS", "S3"}
    assert v["gpus"] == 4                   # 트레이 1 attach = GPU 4
    assert v["quota_tb"] >= 50 and 0 < v["used_tb"] < v["quota_tb"]
    assert v["qos"]["bw_gbps"] > 0 and v["qos"]["iops_k"] > 0
    # 다른 테넌트 필터엔 잡히지 않는다
    assert client.get("/vast/v1/views",
                      params={"tenant": "tnt-x"}).json()["count"] == 0


def test_performance_dynamic_with_tenant_load(client):
    _attach(client, tenant="tnt-demo")
    samples = []
    for _ in range(3):
        vast_mod.ENGINE.tick(force=True)    # 폴링 tick 강제 진행
        body = client.get("/vast/v1/performance").json()
        rows = body["performance"]
        cluster = [p for p in rows if p["scope"] == "cluster"]
        views = [p for p in rows if p["scope"] == "view"]
        assert cluster and views            # 클러스터 + 뷰 롤업 동시 노출
        assert views[0]["tenant_id"] == "tnt-demo"
        c = cluster[0]
        assert c["read_gbps"] > 0 and c["latency_ms_p99"] > 0
        assert 40.0 <= c["cache_hit_pct"] <= 100.0
        samples.append((c["read_gbps"], c["write_gbps"], c["latency_ms_p99"]))
    assert len(set(samples)) > 1            # tick마다 부하 파형이 움직인다


def test_latency_spike_alarm_obs_merge_recover(client):
    r = client.post("/vast/v1/faults/inject", json={"kind": "latency_spike"})
    assert r.status_code == 200
    target = r.json()["target"]
    assert r.json()["cluster"]["state"] == "DEGRADED"
    # 성능에 반영 — p99 급등
    perf = client.get("/vast/v1/performance").json()["performance"]
    c = next(p for p in perf if p["scope"] == "cluster" and p["name"] == target)
    assert c["latency_ms_p99"] >= 20.0
    # VMS alarm 발화
    al = client.get("/vast/v1/alarms").json()
    assert al["open"] >= 1
    assert any("latency_spike" in a["summary"] for a in al["alarms"]
               if a["state"] == "firing")
    # obs alerts에 domain=storage로 merge
    alerts = client.get("/emulator/v1/obs/alerts").json()
    firing = [a for a in alerts if a["domain"] == "storage"
              and a["state"] == "firing" and a.get("source") == "vast"]
    assert firing and any(target in a["resource"] for a in firing)
    # obs summary storage 블록
    s = client.get("/emulator/v1/obs/summary").json()["storage"]
    assert set(s) >= {"clusters", "used_pb", "usable_pb", "read_gbps",
                      "write_gbps", "alarms_open"}
    assert s["alarms_open"] >= 1
    # recover → alarm 해소 + obs에서도 firing 소멸
    r = client.post("/vast/v1/faults/recover", json={})
    assert any("latency_spike" in x for x in r.json()["recovered"])
    assert client.get("/vast/v1/alarms").json()["open"] == 0
    alerts = client.get("/emulator/v1/obs/alerts").json()
    assert not [a for a in alerts if a["domain"] == "storage"
                and a["state"] == "firing"]


def test_converged_paths_and_congestion(client):
    # attach 전엔 테넌트 경로 없음 / overview는 사이트 기반으로 항상 존재
    assert client.get("/converged/v1/paths").json()["count"] == 0
    ov = client.get("/converged/v1/overview").json()["sites"]
    assert ov and ov[0]["fabric"] == "Spectrum-X"
    sp = ov[0]["storage_paths"]
    assert sp["total"] > 0 and sp["active"] == sp["total"]
    assert ov[0]["mgmt_paths"]["total"] > 0
    _attach(client, tenant="tnt-demo", vni=7002)
    ov = client.get("/converged/v1/overview").json()["sites"][0]
    assert ov["vni_segments"] >= 1          # STORE.tenant_networks 파생
    rows = client.get("/converged/v1/paths",
                      params={"tenant": "tnt-demo"}).json()["paths"]
    assert len(rows) == 1
    p = rows[0]
    assert p["src_su"] == "su-1" and p["dst"] == f"vast-{p['site']}"
    assert p["state"] == "active" and p["bw_gbps"] > 0
    base_lat = p["latency_us"]
    # storage_congestion 주입 → 경로 혼잡 + obs storage 알림
    r = client.post("/converged/v1/faults/inject",
                    json={"kind": "storage_congestion"})
    assert r.status_code == 200
    p2 = client.get("/converged/v1/paths").json()["paths"][0]
    assert p2["state"] == "congested"
    assert p2["latency_us"] > base_lat and p2["pfc_pause"] > 0
    alerts = client.get("/emulator/v1/obs/alerts").json()
    assert any(a["domain"] == "storage" and a.get("source") == "converged"
               and a["state"] == "firing" for a in alerts)
    # recover → 정상 복귀
    r = client.post("/converged/v1/faults/recover", json={})
    assert r.json()["recovered"]
    assert client.get("/converged/v1/paths").json()["paths"][0]["state"] \
        == "active"


def test_reset_rebuilds_storage_planes(client):
    _attach(client, tenant="tnt-demo", vni=7003)
    client.post("/vast/v1/faults/inject", json={"kind": "cbox_down"})
    client.post("/converged/v1/faults/inject",
                json={"kind": "storage_congestion"})
    r = client.post("/emulator/v1/reset")
    assert r.status_code == 200
    # 재시드 후: 클러스터 재구축, 테넌트 파생물/알람은 초기화
    cl = client.get("/vast/v1/clusters").json()
    assert cl["count"] >= 1
    assert all(c["state"] == "HEALTHY" and c["fault"] is None
               for c in cl["clusters"])
    assert client.get("/vast/v1/views").json()["count"] == 0
    assert client.get("/vast/v1/alarms").json()["open"] == 0
    assert client.get("/converged/v1/paths").json()["count"] == 0
    assert all(s["state"] == "ok" for s in
               client.get("/converged/v1/overview").json()["sites"])
    # 리셋 직후에도 이벤트/샘플 이력이 비지 않는다 (엔진 패턴 준수)
    assert client.get("/vast/v1/events").json()
    assert client.get("/converged/v1/events").json()
    assert converged_mod.ENGINE.seed_gen == vast_mod.ENGINE.seed_gen


def test_nvme_and_capacity_faults(client):
    r = client.post("/vast/v1/faults/inject",
                    json={"kind": "nvme_drive_fail"})
    assert r.json()["cluster"]["failed_drives"] == 1
    r = client.post("/vast/v1/faults/inject",
                    json={"kind": "capacity_pressure"})
    c = r.json()["cluster"]
    assert c["used_pct"] >= 90.0            # 용량 압박 반영
    al = client.get("/vast/v1/alarms").json()
    assert any("capacity" in a["summary"] for a in al["alarms"]
               if a["state"] == "firing")
    client.post("/vast/v1/faults/recover", json={})
    cl = client.get("/vast/v1/clusters").json()["clusters"][0]
    assert cl["used_pct"] < 90.0 and cl["failed_drives"] == 0
