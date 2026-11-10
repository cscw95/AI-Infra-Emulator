"""F12 — peer publish onto the DSX Exchange (NICo machine state) and EventService
push of the new `machine` category."""
from app import redfish

MID = "nh-su-1-rack-00-tray-02"
CH = f"NICO/v1/machine/{MID}/state"


def _ce(state="ready"):
    return {"specversion": "1.0", "id": "e1", "source": "nico://neocloud/site/x",
            "type": "com.nvidia.nico.v1.machine.state", "subject": CH,
            "time": "2026-11-10T00:00:00Z", "datacontenttype": "application/json",
            "data": {"machine_id": MID, "timestamp": "2026-11-10T00:00:00Z",
                     "managed_host_state": {"state": state}}}


def test_publish_channel_maps_to_machine_topic(client):
    r = client.post("/exchange/v1/publish", json={"topic": CH, "type": "com.nvidia.nico.v1.machine.state",
                                                  "subject": CH, "data": _ce()})
    assert r.status_code == 201 and r.json()["topic"] == "machine"
    evs = client.get("/exchange/v1/events?stream=false&topic=machine").json()
    assert evs[-1]["resource"] == MID and evs[-1]["data"]["data"]["managed_host_state"]["state"] == "ready"
    t = {x["name"]: x["published"] for x in client.get("/exchange/v1/topics").json()["topics"]}
    assert t["machine"] >= 1


def test_publish_rejects_unknown_topic_and_bad_body(client):
    assert client.post("/exchange/v1/publish", json={"topic": "nope", "type": "x"}).status_code == 422
    assert client.post("/exchange/v1/publish", json={"topic": "NICO/v1/machine//state",
                                                     "type": "x"}).status_code == 422
    assert client.post("/exchange/v1/publish", json={"topic": "health"}).status_code == 422


def test_machine_event_pushed_to_default_subscription(client, monkeypatch):
    got = []
    monkeypatch.setattr(redfish, "PUSH_SYNC", True)
    monkeypatch.setattr(redfish, "_http_post", lambda url, p: got.append((url, p)))
    client.post("/redfish/v1/EventService/Subscriptions",
                json={"Destination": "http://127.0.0.1:1/hook", "Protocol": "Redfish"})
    client.post("/exchange/v1/publish", json={"topic": CH, "type": "com.nvidia.nico.v1.machine.state",
                                              "subject": CH, "data": _ce("assigned")})
    ev = got[-1][1]["Events"][0]
    assert ev["Oem"]["Nvidia"]["Topic"] == "machine"
    assert ev["Oem"]["Nvidia"]["Data"]["subject"] == CH
    assert ev["OriginOfCondition"]["@odata.id"].endswith(MID)
    redfish._SUBSCRIPTIONS.clear()
