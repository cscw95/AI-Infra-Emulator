"""Reprovision-as-fault behaviour: /emulator/v1/provision on an already-ready
tray must raise a fault episode (health warning + /emulator/v1/faults entry)
that resolves when the boot machine reaches HostReady again."""
from app.store import STORE

TRAY = "su-1-rack-00-tray-05"


def test_faults_endpoint_has_seeded_sample(client):
    f = client.get("/emulator/v1/faults").json()
    assert f["count"] >= 1
    # faults 피드는 이제 전 도메인 merge — reprovision 샘플이 포함돼 있으면 됨
    sample = [x for x in f["recent"]
              if "(sample)" in x["detail"] and x["kind"] == "reprovision"]
    assert sample and sample[0]["resolved"] is True


def test_reprovision_of_ready_tray_is_a_fault_until_hostready(client):
    # seeded twin: every tray is Ready/HostReady -> starting provisioning
    # again counts as an unplanned reprovision fault
    r = client.post(f"/emulator/v1/provision/{TRAY}")
    assert r.status_code == 200 and r.json()["lifecycle_state"] == "Provisioning"

    f = client.get("/emulator/v1/faults").json()
    rec = next(x for x in f["recent"]
               if x["tray_id"] == TRAY and not x["resolved"])
    assert rec["kind"] == "reprovision" and "unplanned reprovision" in rec["detail"]
    assert STORE.trays[TRAY].health == "warning"
    assert any(e["message_id"].endswith("UnplannedReprovision")
               and e["severity"] == "critical" for e in STORE.events)

    # walk the boot machine to Host Agent Ready -> fault resolves, health ok
    for _ in range(4):
        step = client.post(f"/emulator/v1/provision/{TRAY}/step").json()
    assert step["complete"] is True

    f2 = client.get("/emulator/v1/faults").json()
    rec2 = next(x for x in f2["recent"] if x["tray_id"] == TRAY)
    assert rec2["resolved"] is True and rec2["resolved_at"]
    assert STORE.trays[TRAY].health == "ok"
    assert f2["open"] == 0
