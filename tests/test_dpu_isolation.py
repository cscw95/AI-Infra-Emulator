"""DPU isolation engine: inter-tenant deny, MAC-spoof drop, recovery."""


def _attach(client, dpu, tenant, suffix, mac, vni):
    body = {
        "tenant_id": tenant,
        "network": {"network_id": f"{dpu}-net-{suffix}", "tenant_id": tenant,
                    "network_type": "vxlan", "vni": vni, "subnet": "10.0.0.0/24"},
        "security": {"default_action": "deny", "spoof_check": True,
                     "allowed_macs": [mac]},
    }
    r = client.post(f"/emulator/v1/dpus/{dpu}/tenant-attachments", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_inter_tenant_traffic_blocked(client):
    dpu = "su-1-rack-00-tray-00-dpu-0"
    a = _attach(client, dpu, "tenant-a", "a", "02:aa:00:00:00:01", 5001)
    b = _attach(client, dpu, "tenant-b", "b", "02:bb:00:00:00:01", 5002)

    r = client.post(f"/emulator/v1/dpus/{dpu}/traffic", json={
        "source_function": a["function_id"],
        "destination_function": b["function_id"],
        "packet_count": 1000,
    }).json()
    assert r["dropped"] == 1000
    assert r["forwarded"] == 0
    assert r["reason"] == "INTER_TENANT_DENY"

    metrics = client.get(f"/emulator/v1/dpus/{dpu}/telemetry").json()["metrics"]
    assert metrics["dpu_intertenant_drops_total"] >= 1000
    assert metrics["dpu_default_deny_drops_total"] >= 1000


def test_intra_tenant_traffic_forwarded(client):
    """Same-tenant traffic must be forwarded, not dropped."""
    dpu = "su-1-rack-00-tray-03-dpu-0"
    a = _attach(client, dpu, "tenant-a", "a1", "02:aa:00:00:00:11", 5101)
    b = _attach(client, dpu, "tenant-a", "a2", "02:aa:00:00:00:12", 5102)
    r = client.post(f"/emulator/v1/dpus/{dpu}/traffic", json={
        "source_function": a["function_id"],
        "destination_function": b["function_id"],
        "packet_count": 250,
    }).json()
    assert r["forwarded"] == 250
    assert r["dropped"] == 0
    assert r["reason"] == "FORWARDED"


def test_mac_spoof_dropped(client):
    dpu = "su-1-rack-00-tray-01-dpu-0"
    a = _attach(client, dpu, "tenant-a", "a", "02:aa:00:00:00:0a", 5003)
    r = client.post(f"/emulator/v1/dpus/{dpu}/traffic", json={
        "source_function": a["function_id"],
        "source_mac": "02:de:ad:be:ef:00",
        "packet_count": 300,
    }).json()
    assert r["dropped"] == 300
    assert r["reason"] == "SOURCE_MAC_SPOOF"

    metrics = client.get(f"/emulator/v1/dpus/{dpu}/telemetry").json()["metrics"]
    assert metrics["dpu_spoof_drops_total"] >= 300


def test_arm_os_crash_and_recover_restores(client):
    dpu = "su-1-rack-00-tray-02-dpu-0"
    _attach(client, dpu, "tenant-a", "a", "02:aa:00:00:00:03", 5004)

    client.post(f"/emulator/v1/dpus/{dpu}/faults", json={"type": "DPU_ARM_OS_CRASH"})
    t = client.get(f"/emulator/v1/dpus/{dpu}/telemetry").json()
    assert t["metrics"]["dpu_arm_up"] == 0
    assert t["arm_os_state"] == "failed"

    client.post(f"/emulator/v1/dpus/{dpu}/recover")
    t2 = client.get(f"/emulator/v1/dpus/{dpu}/telemetry").json()
    assert t2["metrics"]["dpu_arm_up"] == 1
    assert t2["arm_os_state"] == "ready"
    assert t2["health"] == "ok"
