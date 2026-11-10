"""DSX Flex 1.7.0 emulation (F20) — Load Target Set, Power State Status,
Power Breach Alert / Enforcement over the DSX Exchange `grid` topic.

Wire format follows the upstream AsyncAPI 3.1 spec
`dsx-ai-factory/dsx-exchange@3358e108d9ad schemas/asyncapi/dsx-flex/dsx-flex.yaml`
(info.version 1.7.0, Apache-2.0): MQTT structured-mode CloudEvents with
`specversion: "1.0.2"` (not the CloudEvents 1.0 "1.0"), `source`
`//grid/v1/(isv|dsx-flex-agent|infra)/<id>`, required W3C `traceparent`, and
event types grid.loadtarget.set.v1, grid.powerstate.status.v1,
grid.powerbreach.alert.v1, grid.powerbreach.enforcement.v1. `data` is sent as
the plaintext object (the spec's oneOf alternative to the JWS compact string);
JWS strings are accepted on ingress and decoded without signature verification.

Flow (DSX Flex integrator guide, 공개 문서 기준 — the Flex Agent source and the
DPS NvGrid gRPC proto are not public):
  1. ISV → Agent  `grid/v1/isv/{id}/loadtarget/set` (fire-and-forget).
  2. Agent → subscribers `grid/v1/dsx-flex-agent/{id}/powerstate/status`
     (event target_set, start_ramp_down … periodic heartbeat).
  3. Best effort: the Agent does not cut DPS limits itself; subscribers (NOCP
     curtailment policy, F21) shed load. If measured load stays above the
     target for BREACH_AFTER_S after the ramp, the Agent publishes
     `grid/v1/dsx-flex-agent/{id}/powerbreach` (active → escalated → resolved)
     with shed_hints.
  4. Infra agent (emulated here as `ima-emu`) applies the hints by lowering the
     DPS domain limit (a DPS flex event) and publishes
     `grid/v1/infra/{id}/powerbreach/enforcement` (applied; reverted at target end).
Feeds are DPS power domains (`pd-{ComputeBlock}`); a site target is split over
its feeds by current measured load.

  POST /flex/v1/load-target-sets            GET /flex/v1/load-target-sets[/{id}]
  DELETE /flex/v1/load-target-sets/{id}     (load_constraint null → revert)
  POST /flex/v1/isv/{identifier}/loadtarget/set   (raw CloudEvent ingress)
  GET  /flex/v1/power-state-status?site=    GET /flex/v1/breaches
  GET  /flex/v1/messages?since=&type=       POST|GET /flex/v1/clock (emulator)"""
import base64
import json
import math
import os
import re
import threading
import time
import uuid
from collections import deque
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .store import STORE
from . import exchange
from . import dps

router = APIRouter(prefix="/flex/v1", tags=["dsx-flex"])

SPEC_VERSION = "1.0.2"                 # dsx-flex.yaml BaseCloudEvent.specversion const
SCHEMA_VERSION = "1.7.0"               # dsx-flex.yaml info.version
AGENT_ID = "maxlps"                    # //grid/v1/dsx-flex-agent/maxlps (spec example)
INFRA_ID = "ima-emu"
ISV_ID = "neocloud-ops"
BREACH_AFTER_S = float(os.environ.get("FLEX_BREACH_AFTER_S", "30"))
STATUS_INTERVAL_S = 10.0
TOLERANCE = 0.005                      # 0.5 % of target before calling it a breach
CRITICAL_EXCESS = 0.20                 # spec: critical when > 20 % over target
MAX_DURATION_S = 24 * 3600
MAX_LTS = 256
_SOURCE_RE = re.compile(r"^//grid/v1/(isv|dsx-flex-agent|infra)/[^/]+$")
_TRACE_RE = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
_UNIT_KW = {"watt": 0.001, "kilowatt": 1.0, "megawatt": 1000.0}


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _parse_ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise HTTPException(422, f"bad date-time {s!r}")


def _traceparent() -> str:
    return f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01"


def _kw(v: float) -> dict:
    return {"value": round(float(v), 1), "unit": "kilowatt"}


# ── state ─────────────────────────────────────────────────────────────
class _Flex:
    def __init__(self):
        self.lock = threading.RLock()
        self.seed_gen = -1
        self.reset()

    def reset(self):
        self.offset = 0.0                       # emulator clock advance (s)
        self.lts: Dict[str, dict] = {}
        self.breaches: Dict[str, dict] = {}     # breach_id → breach
        self.open_breach: Dict[str, str] = {}   # feed → open breach_id
        self.over_since: Dict[str, float] = {}  # feed → first time above target
        self.messages = deque(maxlen=1000)
        self.seq = 0
        self.last_status = 0.0
        self.last_phase: Dict[str, str] = {}    # feed → last FeedState.event

    def now(self) -> float:
        return time.time() + self.offset

    def sync(self):
        cur = getattr(STORE, "seed_gen", 0)
        if cur != self.seed_gen:
            self.seed_gen = cur
            self.reset()


FLEX = _Flex()


def envelope(role: str, ident: str, type_: str, subject: str, data: dict,
             correlation_id: Optional[str] = None) -> dict:
    env = {"specversion": SPEC_VERSION, "id": str(uuid.uuid4()),
           "source": f"//grid/v1/{role}/{ident}", "type": type_,
           "time": _iso(FLEX.now()), "datacontenttype": "application/json",
           "subject": subject, "traceparent": _traceparent(), "data": data}
    if correlation_id:
        env["correlationid"] = correlation_id
    return env


_EXCHANGE_TYPE = {"grid.loadtarget.set.v1": "LoadTargetSet",
                  "grid.powerstate.status.v1": "PowerStateStatus",
                  "grid.powerbreach.alert.v1": "PowerBreachAlert",
                  "grid.powerbreach.enforcement.v1": "PowerBreachEnforcement"}


def _emit(mqtt_topic: str, env: dict, severity: str = "info", resource: str = ""):
    """Log the raw MQTT message and republish it on the Exchange `grid` topic."""
    FLEX.seq += 1
    FLEX.messages.append({"seq": FLEX.seq, "mqtt_topic": mqtt_topic,
                          "qos": 2 if "powerbreach" in mqtt_topic else 1,
                          "cloudevent": env})
    exchange.publish("grid", _EXCHANGE_TYPE[env["type"]], severity,
                     resource or env["subject"],
                     {"mqtt_topic": mqtt_topic, "schema": f"dsx-flex {SCHEMA_VERSION}",
                      "cloudevent": env}, category="grid")


# ── feeds (DPS power domains) ─────────────────────────────────────────
def site_feeds(site: str) -> List[str]:
    dps.DPS.sync()
    if site in dps.DPS.domains:
        return [site]
    feeds = [did for did, d in dps.DPS.domains.items()
             if site in (d["site"], d.get("compute_block"))]
    if not feeds:
        raise HTTPException(404, f"site or feed {site} not found")
    return feeds


def feed_measured_kw(feed: str) -> float:
    return sum(r["measured_kw"] for r in dps.allocate(feed).values())


def _feed_metadata(feed: str) -> dict:
    d = dps.DPS.domains[feed]
    racks = dps.DPS.domain_racks(feed)
    return {"power_minimum": _kw(len(racks) * dps.STANDBY_KW),
            "power_maximum": _kw(d["operating_limit_kw"]),
            "default_constraint": _kw(d["operating_limit_kw"])}


def _active_targets(feed: str, now: float) -> List[dict]:
    out = []
    for lt in FLEX.lts.values():
        if lt["state"] in ("cleared", "completed"):
            continue
        if feed in lt["feeds"] and lt["_start"] <= now < lt["_end"]:
            out.append(lt)
    return out


def _target_now(lt: dict, feed: str, now: float) -> float:
    """Ramped target: linear from the baseline at start to the target over ramp_s."""
    tgt = lt["feeds"][feed]["target_kw"]
    base = lt["feeds"][feed]["baseline_kw"]
    if lt["ramp_s"] and now < lt["_start"] + lt["ramp_s"]:
        frac = (now - lt["_start"]) / lt["ramp_s"]
        return max(tgt, base + (tgt - base) * frac)
    return tgt


def _feed_target(feed: str, now: float):
    """Tightest active target on a feed → (lts, target_kw now, final target_kw)."""
    best = None
    for lt in _active_targets(feed, now):
        t = lt["feeds"][feed]["target_kw"]
        if best is None or t < best[2]:
            best = (lt, _target_now(lt, feed, now), t)
    return best


# ── evaluation (Agent + infra agent state machine) ────────────────────
def _shed_hints(feed: str, excess_kw: float) -> List[dict]:
    """Racks of the lowest DPS group priority first, largest draw first."""
    recs = dps.allocate(feed)
    prio = {g["id"]: g["priority"] for g in dps.DPS.groups.values()}
    order = sorted(recs.items(), key=lambda kv: (prio.get(kv[1]["group"], 50),
                                                 -kv[1]["measured_kw"]))
    hints, left = [], excess_kw
    for i, (rid, rec) in enumerate(order, 1):
        if left <= 0:
            break
        cut = min(left, max(0.0, rec["measured_kw"] - dps.STANDBY_KW))
        if cut <= 0:
            continue
        hints.append({"resource_id": rid, "resource_type": "chassis",
                      "action": "power_cap", "priority": i,
                      "target_power": _kw(rec["measured_kw"] - cut)})
        left -= cut
    return hints


def _alert(feed: str, br: dict, status: str, measured: float, hints=None):
    lt = FLEX.lts.get(br["lts_id"], {})
    excess = (measured - br["target_kw"]) / br["target_kw"] if br["target_kw"] else 1
    sev = "critical" if excess > CRITICAL_EXCESS else "warning"
    br["status"], br["severity"] = status, sev
    breach = {"breach_id": br["breach_id"], "detected_at": br["detected_at"]}
    if status == "resolved":
        breach["resolved_at"] = br["resolved_at"]
    data = {"feed_tag": feed, "status": status, "severity": sev, "breach": breach,
            "target": {"correlation_id": lt.get("correlation_id", br["breach_id"]),
                       "load_constraint": _kw(br["target_kw"])},
            "measured_load": _kw(measured),
            "infrastructure_actions": [] if status == "resolved" else
            (["SHUTDOWN_NODES", "SHUTDOWN_RACKS"] if sev == "critical" else [])}
    if status != "resolved":
        data["shed_hints"] = hints or []
    _emit(f"grid/v1/dsx-flex-agent/{AGENT_ID}/powerbreach",
          envelope("dsx-flex-agent", AGENT_ID, "grid.powerbreach.alert.v1",
                   "powerbreach", data, lt.get("correlation_id")),
          "info" if status == "resolved" else sev, feed)


def _enforce(feed: str, br: dict, hints: List[dict]):
    """Infra agent: apply the hints as a DPS limit cut until measured ≤ target."""
    d = dps.DPS.domains[feed]
    if feed_measured_kw(feed) > br["target_kw"] * (1 + TOLERANCE * 0.5):
        fid = dps.DPS.nid("flex")
        f = dps.DPS.flex[fid] = {
            "id": fid, "domain": feed, "reduce_kw": 0.0,
            "duration_s": max(1, int(br["_end"] - FLEX.now())), "status": "active",
            "start": _iso(FLEX.now()), "end": _iso(br["_end"]),
            "source": "dsx-flex-enforcement", "breach_id": br["breach_id"],
            "_end": br["_end"] - FLEX.offset}       # DPS expiry runs on wall time
        br["dps_flex"].append(fid)
        # binary search the DPS limit cut that brings the feed to its target
        lo, hi = 0.0, max(0.0, dps.effective_limit_kw(d) - 1.0)
        for _ in range(40):
            f["reduce_kw"] = (lo + hi) / 2
            if feed_measured_kw(feed) > br["target_kw"] * (1 + TOLERANCE * 0.5):
                lo = f["reduce_kw"]
            else:
                hi = f["reduce_kw"]
        f["reduce_kw"] = round(hi + 0.05, 1)
        dps.enforce(feed)
    measured = feed_measured_kw(feed)
    ok = measured <= br["target_kw"] * (1 + TOLERANCE)
    br["enforced"] = True
    results = [{"resource_id": h["resource_id"], "resource_type": h["resource_type"],
                "requested_action": h["action"],
                "code": "applied" if br["dps_flex"] else "skipped",
                "applied_power": h["target_power"]} for h in hints]
    data = {"breach_id": br["breach_id"], "feed_tag": feed, "action": "applied",
            "code": "success" if ok else ("partial" if br["dps_flex"] else "error"),
            "results": results, "timestamp": _iso(FLEX.now())}
    if not ok:
        data["diag_msg"] = f"measured {measured:.1f} kW still above {br['target_kw']:.1f} kW"
    _emit(f"grid/v1/infra/{INFRA_ID}/powerbreach/enforcement",
          envelope("infra", INFRA_ID, "grid.powerbreach.enforcement.v1",
                   "powerbreach", data), "warning", feed)


def _revert(br: dict):
    if not br.get("dps_flex") or br.get("reverted"):
        return
    feed = br["feed"]
    for fid in br["dps_flex"]:
        f = dps.DPS.flex.get(fid)
        if f and f["status"] == "active":
            f["status"] = "completed"
    dps.enforce(feed)
    br["reverted"] = True
    data = {"breach_id": br["breach_id"], "feed_tag": feed, "action": "reverted",
            "code": "success",
            "results": [{"resource_id": h["resource_id"], "resource_type": h["resource_type"],
                         "requested_action": h["action"], "code": "applied"}
                        for h in br.get("hints", [])],
            "timestamp": _iso(FLEX.now())}
    _emit(f"grid/v1/infra/{INFRA_ID}/powerbreach/enforcement",
          envelope("infra", INFRA_ID, "grid.powerbreach.enforcement.v1",
                   "powerbreach", data), "info", feed)


def _feed_state(feed: str, now: float, event: str) -> dict:
    cur = _feed_target(feed, now)
    measured = feed_measured_kw(feed)
    targets = []
    for lt in FLEX.lts.values():
        if feed not in lt["feeds"] or lt["state"] in ("cleared", "completed"):
            continue
        targets.append({"active": lt["_start"] <= now < lt["_end"],
                        "load_constraint": _kw(lt["feeds"][feed]["target_kw"]),
                        "interval": {"start_time": lt["start"], "end_time": lt["end"]},
                        "strategy": {"best_effort": lt["best_effort"]},
                        "correlation_id": lt["correlation_id"]})
    st = {"metadata": _feed_metadata(feed), "targets": targets,
          "calculated_load": _kw(min(measured, cur[1]) if cur else measured),
          "in_flight": bool(cur and cur[0]["ramp_s"] and
                            now < cur[0]["_start"] + cur[0]["ramp_s"]),
          "event": event,
          "compliant": not cur or measured <= cur[1] * (1 + TOLERANCE) + 0.5}
    if cur:
        st["power_event_start_time"] = cur[0]["start"]
    return st


def _publish_status(feeds: List[str], now: float, events: Dict[str, str],
                    correlation_id: Optional[str] = None):
    data = {"snapshot_time": _iso(now),
            "feeds": {f: _feed_state(f, now, events.get(f, "periodic")) for f in feeds}}
    _emit(f"grid/v1/dsx-flex-agent/{AGENT_ID}/powerstate/status",
          envelope("dsx-flex-agent", AGENT_ID, "grid.powerstate.status.v1",
                   "powerstate", data, correlation_id))
    FLEX.last_status = now


def evaluate(now: Optional[float] = None) -> None:
    """Advance the Agent state machine to `now` (lazy: called on every read)."""
    with STORE.lock, FLEX.lock:
        FLEX.sync()
        dps.DPS.sync()
        now = FLEX.now() if now is None else now
        changed: Dict[str, str] = {}
        for lt in FLEX.lts.values():
            if lt["state"] == "scheduled" and now >= lt["_start"]:
                lt["state"] = "active"
                for f in lt["feeds"]:
                    changed[f] = "start_ramp_down"
            if lt["state"] == "active" and lt["ramp_s"] and \
                    now >= lt["_start"] + lt["ramp_s"] and not lt.get("_ramped"):
                lt["_ramped"] = True
                for f in lt["feeds"]:
                    changed.setdefault(f, "end_ramp_down")
            if lt["state"] == "active" and now >= lt["_end"]:
                lt["state"] = "completed"
                for f in lt["feeds"]:
                    changed[f] = "start_ramp_up"
        feeds = {f for lt in FLEX.lts.values() for f in lt["feeds"]}
        for feed in sorted(feeds):
            cur = _feed_target(feed, now)
            bid = FLEX.open_breach.get(feed)
            br = FLEX.breaches.get(bid) if bid else None
            measured = feed_measured_kw(feed)
            if not cur:                          # no active target → close + revert
                FLEX.over_since.pop(feed, None)
                if br:
                    br["resolved_at"] = _iso(now)
                    _revert(br)
                    _alert(feed, br, "resolved", feed_measured_kw(feed))
                    FLEX.open_breach.pop(feed, None)
                    changed.setdefault(feed, "end_ramp_up")
                continue
            lt, tgt_now, tgt = cur
            over = measured > tgt_now * (1 + TOLERANCE) + 0.5
            after_ramp = now >= lt["_start"] + (lt["ramp_s"] or 0)
            if over and after_ramp:
                since = FLEX.over_since.setdefault(feed, now)
                if not br and now - since >= BREACH_AFTER_S:
                    br = {"breach_id": str(uuid.uuid4()), "feed": feed,
                          "lts_id": lt["id"], "target_kw": tgt,
                          "detected_at": _iso(now), "status": "active",
                          "dps_flex": [], "_end": lt["_end"], "_t": now}
                    FLEX.breaches[br["breach_id"]] = br
                    FLEX.open_breach[feed] = br["breach_id"]
                    br["hints"] = _shed_hints(feed, measured - tgt)
                    _alert(feed, br, "active", measured, br["hints"])
                    _enforce(feed, br, br["hints"])
                    changed[feed] = "end_ramp_down"
                elif br and br["status"] == "active" and \
                        now - br["_t"] >= BREACH_AFTER_S:
                    br["hints"] = _shed_hints(feed, measured - tgt)
                    _alert(feed, br, "escalated", measured, br["hints"])
                    _enforce(feed, br, br["hints"])
            else:
                FLEX.over_since.pop(feed, None)
                if br and br["status"] != "resolved" and not over:
                    # stays open (DPS cut active) until the target ends; report
                    # compliance through PowerStateStatus.
                    pass
        if changed:
            for f, ev in changed.items():
                FLEX.last_phase[f] = ev
            _publish_status(sorted(feeds), now, changed)
        elif feeds and now - FLEX.last_status >= STATUS_INTERVAL_S and \
                any(_feed_target(f, now) for f in feeds):
            _publish_status(sorted(feeds), now, {})


# ── ingest ────────────────────────────────────────────────────────────
def _new_lts(site: str, feeds: List[str], target_kw: Optional[float],
             reduce_kw: Optional[float], start: float, duration_s: int,
             ramp_s: int, best_effort: bool, lts_id: Optional[str],
             correlation_id: Optional[str], source: str) -> dict:
    if len(FLEX.lts) >= MAX_LTS:
        for k in [k for k, v in FLEX.lts.items() if v["state"] in ("completed", "cleared")]:
            FLEX.lts.pop(k)
        if len(FLEX.lts) >= MAX_LTS:
            raise HTTPException(503, f"load-target-set limit ({MAX_LTS}) reached")
    if not 0 < duration_s <= MAX_DURATION_S:
        raise HTTPException(422, f"duration_s must be 1..{MAX_DURATION_S}")
    if ramp_s < 0 or ramp_s > duration_s:
        raise HTTPException(422, "ramp_s must be 0..duration_s")
    meas = {f: feed_measured_kw(f) for f in feeds}
    total = sum(meas.values()) or 1.0
    if target_kw is None:
        if reduce_kw is None or not math.isfinite(reduce_kw) or reduce_kw <= 0:
            raise HTTPException(422, "target_kw or reduce_kw (> 0) is required")
        target_kw = total - reduce_kw
    if not math.isfinite(target_kw) or target_kw <= 0 or target_kw >= dps.MAX_LIMIT_KW:
        raise HTTPException(422, "target must leave a positive load (target_kw > 0, "
                                 "reduce_kw below the measured load)")
    lid = lts_id or f"lts-{uuid.uuid4().hex[:8]}"
    if lid in FLEX.lts and FLEX.lts[lid]["state"] in ("scheduled", "active"):
        raise HTTPException(409, f"load target set {lid} already active")
    per = {f: {"baseline_kw": round(meas[f], 1),
               "target_kw": round(target_kw * meas[f] / total, 1)} for f in feeds}
    lt = {"id": lid, "site": site, "feeds": per,
          "target_kw": round(target_kw, 1), "reduce_kw": round(total - target_kw, 1),
          "baseline_kw": round(total, 1), "duration_s": duration_s, "ramp_s": ramp_s,
          "best_effort": best_effort, "start": _iso(start),
          "end": _iso(start + duration_s), "correlation_id": correlation_id or str(uuid.uuid4()),
          "state": "scheduled", "source": source,
          "_start": start, "_end": start + duration_s}
    FLEX.lts[lid] = lt
    return lt


def _clear_feeds(feeds: List[str], now: float) -> List[str]:
    out = []
    for lt in FLEX.lts.values():
        if lt["state"] in ("scheduled", "active") and set(lt["feeds"]) & set(feeds):
            lt["state"], lt["_end"], lt["end"] = "cleared", now, _iso(now)
            out.append(lt["id"])
    return out


def _view(lt: dict) -> dict:
    now = FLEX.now()
    out = {k: v for k, v in lt.items() if not k.startswith("_")}
    out["feeds"] = {}
    for f, v in lt["feeds"].items():
        cur = _feed_target(f, now)
        out["feeds"][f] = {**v, "measured_kw": round(feed_measured_kw(f), 1),
                           "target_now_kw": round(cur[1], 1) if cur and cur[0] is lt else None}
    out["breaches"] = [b["breach_id"] for b in FLEX.breaches.values() if b["lts_id"] == lt["id"]]
    return out


class LoadTargetBody(BaseModel):
    id: Optional[str] = None
    site: str
    target_kw: Optional[float] = None
    reduce_kw: Optional[float] = None
    start: Optional[str] = None
    duration_s: int = 900
    ramp_s: int = 0
    feed_tags: Optional[List[str]] = None
    best_effort: bool = True


def _feeds_for(site: str, tags: Optional[List[str]]) -> List[str]:
    feeds = site_feeds(site)
    if tags:
        bad = [t for t in tags if t not in feeds]
        if bad:
            raise HTTPException(404, f"feed_tags not in site {site}: {bad}")
        feeds = list(tags)
    return feeds


@router.post("/load-target-sets", status_code=201)
def create_load_target_set(body: LoadTargetBody):
    """Contract shorthand of grid.loadtarget.set.v1; the generated ISV CloudEvent
    is logged on the upstream topic and returned as `cloudevent`."""
    with STORE.lock, FLEX.lock:
        FLEX.sync()
        feeds = _feeds_for(body.site, body.feed_tags)
        start = _parse_ts(body.start) or FLEX.now()
        lt = _new_lts(body.site, feeds, body.target_kw, body.reduce_kw, start,
                      body.duration_s, body.ramp_s, body.best_effort, body.id, None,
                      "rest")
        data = {"targets": [{"interval": {"start_time": lt["start"], "end_time": lt["end"]},
                             "feed_tags": feeds,
                             "load_constraint": _kw(lt["target_kw"]),
                             "strategy": {"best_effort": body.best_effort}}]}
        env = envelope("isv", ISV_ID, "grid.loadtarget.set.v1", "loadtarget", data,
                       lt["correlation_id"])
        _emit(f"grid/v1/isv/{ISV_ID}/loadtarget/set", env, "warning", body.site)
        lt["cloudevent_id"] = env["id"]
        _publish_status(feeds, FLEX.now(), {f: "target_set" for f in feeds},
                        lt["correlation_id"])
    evaluate()
    with STORE.lock, FLEX.lock:
        return {**_view(lt), "cloudevent": env}


def _decode_jws(s: str) -> dict:
    try:
        part = s.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        raise HTTPException(422, "data is not a JWS compact serialization")


@router.post("/isv/{identifier}/loadtarget/set", status_code=202)
def ingest_load_target(identifier: str, env: dict):
    """Raw MQTT ingress for topic grid/v1/isv/{identifier}/loadtarget/set."""
    errs = []
    if env.get("specversion") != SPEC_VERSION:
        errs.append(f"specversion must be '{SPEC_VERSION}'")
    if env.get("type") != "grid.loadtarget.set.v1":
        errs.append("type must be grid.loadtarget.set.v1")
    if not _SOURCE_RE.match(str(env.get("source", ""))):
        errs.append("source must match //grid/v1/(isv|dsx-flex-agent|infra)/<id>")
    if not _TRACE_RE.match(str(env.get("traceparent", ""))):
        errs.append("traceparent (W3C) is required")
    for k in ("id", "time", "subject"):
        if not env.get(k):
            errs.append(f"{k} is required")
    if env.get("datacontenttype") != "application/json":
        errs.append("datacontenttype must be application/json")
    data = env.get("data")
    jws = isinstance(data, str)
    if jws:
        data = _decode_jws(data)
    if not isinstance(data, dict) or not isinstance(data.get("targets"), list) \
            or not data["targets"]:
        errs.append("data.targets (minItems 1) is required")
    if errs:
        raise HTTPException(422, {"errors": errs})
    created, cleared = [], []
    with STORE.lock, FLEX.lock:
        FLEX.sync()
        _emit(f"grid/v1/isv/{identifier}/loadtarget/set", env, "warning", identifier)
        for t in data["targets"]:
            iv = t.get("interval") or {}
            start = _parse_ts(iv.get("start_time")) or FLEX.now()
            end = _parse_ts(iv.get("end_time")) or start + 900
            tags = t.get("feed_tags") or None
            feeds = _feeds_for(tags[0], tags) if tags else sorted(dps.DPS.domains)
            lc = t.get("load_constraint")
            if lc is None:
                cleared += _clear_feeds(feeds, FLEX.now())
                continue
            if lc.get("unit") not in _UNIT_KW or not isinstance(lc.get("value"), (int, float)):
                raise HTTPException(422, "load_constraint {value, unit: watt|kilowatt|megawatt}")
            lt = _new_lts(",".join(feeds), feeds, lc["value"] * _UNIT_KW[lc["unit"]], None,
                          start, int(end - start), 0,
                          bool((t.get("strategy") or {}).get("best_effort", False)),
                          None, env.get("correlationid"), f"mqtt:{env['source']}")
            lt["cloudevent_id"] = env["id"]
            created.append(lt["id"])
        allf = sorted({f for lid in created for f in FLEX.lts[lid]["feeds"]})
        if allf:
            _publish_status(allf, FLEX.now(), {f: "target_set" for f in allf},
                            env.get("correlationid"))
    evaluate()
    return {"accepted": True, "jws_verified": False if jws else None,
            "load_target_sets": created, "cleared": cleared}


@router.get("/load-target-sets")
def list_load_target_sets(site: Optional[str] = None, state: Optional[str] = None):
    evaluate()
    with STORE.lock, FLEX.lock:
        return {"items": [_view(lt) for lt in FLEX.lts.values()
                          if (not site or lt["site"] == site or site in lt["feeds"])
                          and (not state or lt["state"] == state)]}


@router.get("/load-target-sets/{lid}")
def get_load_target_set(lid: str):
    evaluate()
    with STORE.lock, FLEX.lock:
        lt = FLEX.lts.get(lid)
        if not lt:
            raise HTTPException(404, f"load target set {lid} not found")
        return _view(lt)


@router.delete("/load-target-sets/{lid}")
def clear_load_target_set(lid: str):
    """Equivalent of a grid.loadtarget.set.v1 with load_constraint null."""
    with STORE.lock, FLEX.lock:
        FLEX.sync()
        lt = FLEX.lts.get(lid)
        if not lt:
            raise HTTPException(404, f"load target set {lid} not found")
        feeds = list(lt["feeds"])
        if lt["state"] in ("scheduled", "active"):
            now = FLEX.now()
            lt["state"], lt["_end"], lt["end"] = "cleared", now, _iso(now)
            data = {"targets": [{"interval": {"start_time": _iso(now), "end_time": _iso(now)},
                                 "feed_tags": feeds, "load_constraint": None}]}
            _emit(f"grid/v1/isv/{ISV_ID}/loadtarget/set",
                  envelope("isv", ISV_ID, "grid.loadtarget.set.v1", "loadtarget", data,
                           lt["correlation_id"]), "info", lt["site"])
    evaluate()
    with STORE.lock, FLEX.lock:
        return _view(lt)


@router.get("/power-state-status")
def power_state_status(site: Optional[str] = None):
    """Power State Status reply: site totals + the upstream FeedState map."""
    evaluate()
    with STORE.lock, FLEX.lock:
        dps.DPS.sync()
        feeds = site_feeds(site) if site else sorted(dps.DPS.domains)
        now = FLEX.now()
        measured = target = 0.0
        has_target, compliant = False, True
        fs = {}
        for f in feeds:
            st = _feed_state(f, now, FLEX.last_phase.get(f, "periodic"))
            fs[f] = st
            m = feed_measured_kw(f)
            measured += m
            cur = _feed_target(f, now)
            if cur:
                has_target = True
                target += cur[1]
            else:
                target += dps.effective_limit_kw(dps.DPS.domains[f])
            compliant &= st["compliant"]
        open_b = [FLEX.breaches[b] for f in feeds
                  for b in [FLEX.open_breach.get(f)] if b]
        return {"site": site, "measured_kw": round(measured, 1),
                "target_kw": round(target, 1) if has_target else None,
                "compliance": "ok" if compliant else "breach",
                "headroom_kw": round(target - measured, 1),
                "snapshot_time": _iso(now),
                "open_breaches": [{"breach_id": b["breach_id"], "feed_tag": b["feed"],
                                   "status": b["status"], "enforced": b.get("enforced", False)}
                                  for b in open_b],
                "feeds": fs,
                "schema": f"dsx-flex {SCHEMA_VERSION} PowerStateStatusData.feeds"}


@router.get("/breaches")
def list_breaches(feed: Optional[str] = None):
    evaluate()
    with FLEX.lock:
        return {"items": [{k: v for k, v in b.items() if not k.startswith("_")}
                          for b in FLEX.breaches.values() if not feed or b["feed"] == feed]}


@router.get("/messages")
def messages(since: int = 0, type: Optional[str] = None, limit: int = 200):
    """Raw MQTT message log (topic + CloudEvent) for demos and contract checks."""
    evaluate()
    with FLEX.lock:
        out = [m for m in FLEX.messages if m["seq"] > since
               and (not type or m["cloudevent"]["type"] == type)]
        return {"items": out[-max(1, min(limit, 1000)):]}


class ClockBody(BaseModel):
    advance_s: float


@router.get("/clock")
def clock():
    return {"now": _iso(FLEX.now()), "offset_s": FLEX.offset,
            "breach_after_s": BREACH_AFTER_S}


@router.post("/clock")
def advance_clock(body: ClockBody):
    """Emulator control: move the Flex Agent clock forward (demo/test)."""
    if not math.isfinite(body.advance_s) or not 0 <= body.advance_s <= MAX_DURATION_S:
        raise HTTPException(422, f"advance_s must be 0..{MAX_DURATION_S}")
    with FLEX.lock:
        FLEX.sync()
        FLEX.offset += body.advance_s
    evaluate()
    return clock()
