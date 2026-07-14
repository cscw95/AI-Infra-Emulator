"""UFM Enterprise·NetQ 에뮬레이터 — 개별 스위치·링크·장애 결합 검증."""
from fastapi.testclient import TestClient

from app.main import app
from app.store import STORE

client = TestClient(app)


def test_ufm_systems_shape_and_scale():
    sw = client.get("/ufm/v1/resources/systems").json()["systems"]
    assert sw, "스위치 없음"
    s = sw[0]
    for k in ("guid", "name", "type", "plane", "site", "model",
              "ports_total", "ports_active", "state"):
        assert k in s
    assert any(x["type"] == "spine" for x in sw)
    assert any(x["type"] == "leaf" for x in sw)
    # leaf는 SU마다 plane당 4대
    n_su = len({r.su_id for r in STORE.racks.values()})
    leafs = [x for x in sw if x["type"] == "leaf"]
    assert len(leafs) == n_su * 4 * 2


def test_ufm_ports_counters_increase():
    sw = client.get("/ufm/v1/resources/systems?type=leaf").json()["systems"][0]
    p1 = client.get(f"/ufm/v1/resources/ports?system_guid={sw['guid']}").json()["ports"]
    assert p1 and "counters" in p1[0]
    import time
    time.sleep(0.05)
    # 카운터는 단조 증가(같은 tick이면 동일 허용)
    p2 = client.get(f"/ufm/v1/resources/ports?system_guid={sw['guid']}").json()["ports"]
    c1 = p1[0]["counters"]["xmit_wait"]
    c2 = p2[0]["counters"]["xmit_wait"]
    assert c2 >= c1


def test_ufm_pkeys_from_real_attachments():
    from app import dpu as dpu_mod
    from app import models as m
    d = next(iter(STORE.dpus.values()))
    dpu_mod.create_attachment(d.dpu_id, m.AttachmentCreate(
        tenant_id="tnt-ufm-test",
        network=m.TenantNetwork(network_id="net-u", tenant_id="tnt-ufm-test",
                                network_type="vxlan", vni=11111,
                                vrf="tnt-ufm-test", subnet="10.9.0.0/16")))
    pks = client.get("/ufm/v1/resources/pkeys").json()["pkeys"]
    assert any(p["tenant_id"] == "tnt-ufm-test" for p in pks)


def test_ufm_inject_link_degrade_health_and_obs_merge():
    h0 = client.get("/ufm/v1/fabric/health").json()
    r = client.post("/ufm/v1/faults/inject",
                    json={"kind": "link_degrade", "target": "su-1"})
    assert r.status_code == 200
    h1 = client.get("/ufm/v1/fabric/health").json()
    assert h1["links_degraded"] > h0["links_degraded"]
    alerts = client.get("/emulator/v1/obs/alerts").json()
    assert any(a["domain"] == "fabric" and a["state"] == "firing"
               and "IB 링크 이상" in a["summary"] for a in alerts)
    client.post("/ufm/v1/faults/recover", json={})
    h2 = client.get("/ufm/v1/fabric/health").json()
    assert h2["links_degraded"] <= h0["links_degraded"]


def test_netq_switches_and_protocols():
    sw = client.get("/netq/v1/switches").json()["switches"]
    assert sw and sw[0]["model"].startswith("SN")
    pr = client.get("/netq/v1/protocols").json()["protocols"]
    assert pr and "bgp_peers_up" in pr[0]


def test_netq_validation_fail_inject_and_obs_merge():
    checks0 = client.get("/netq/v1/validation").json()["checks"]
    assert checks0 and all("result" in c for c in checks0)
    client.post("/netq/v1/faults/inject", json={"kind": "validation_fail"})
    checks = client.get("/netq/v1/validation").json()["checks"]
    assert any(c["result"] == "fail" for c in checks)
    alerts = client.get("/emulator/v1/obs/alerts").json()
    assert any(a["domain"] == "fabric" and "NetQ validation" in a["summary"]
               for a in alerts)
    client.post("/netq/v1/faults/recover", json={})
    checks2 = client.get("/netq/v1/validation").json()["checks"]
    assert not any(c["result"] == "fail" for c in checks2)


def test_reset_rebuilds_fabric_emulators():
    n0 = len(client.get("/ufm/v1/resources/systems").json()["systems"])
    client.post("/emulator/v1/reset")
    assert len(client.get("/ufm/v1/resources/systems").json()["systems"]) == n0
    assert client.get("/netq/v1/switches").json()["switches"]


def test_ufm_links_filter():
    lk = client.get("/ufm/v1/resources/links").json()["links"]
    assert lk and {"link_id", "plane", "state"} <= set(lk[0])
    deg = client.get("/ufm/v1/resources/links?state=degraded").json()["links"]
    assert all(x["state"] == "degraded" for x in deg)
