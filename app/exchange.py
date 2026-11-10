"""DSX Exchange emulation — in-memory event bus (NATS/MQTT stand-in).

Topics mirror the DSX Exchange AsyncAPI channels used by the AI factory:
power, thermal, leak, grid, health. Producers (OOB / DPS / Redfish actions)
call publish(); consumers read the backlog (JSON) or follow the SSE stream.
Listeners registered with add_listener() (e.g. the Redfish EventService push
dispatcher) are called synchronously on every publish.

  GET /exchange/v1/topics
  GET /exchange/v1/events      (SSE by default; ?stream=false → JSON list)"""
import asyncio
import itertools
import json
import os
from collections import deque
from typing import Callable, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .store import STORE, _iso

router = APIRouter(prefix="/exchange/v1", tags=["dsx-exchange"])

TOPICS = {
    "power": "Rack/domain power, power limit changes, DPS allocation, PSU state",
    "thermal": "Inlet / GPU temperature excursions from BMC thermal sensors",
    "leak": "Cold-plate / manifold leak detection state changes",
    "grid": "DSX Flex demand-response (grid) events against a power domain",
    "health": "SEL critical entries, BMC reachability, GPU reset, AC power cycle",
}


class _Bus:
    def __init__(self):
        self.seed_gen = -1
        self.events = deque(maxlen=2000)
        self.counts = {t: 0 for t in TOPICS}
        self._seq = itertools.count(1)
        self.listeners: List[Callable[[dict], None]] = []

    def _sync(self):
        # STORE.reset() reseeds the twin → drop the stale backlog
        cur = getattr(STORE, "seed_gen", 0)
        if cur != self.seed_gen:
            self.seed_gen = cur
            self.events.clear()
            self.counts = {t: 0 for t in TOPICS}


BUS = _Bus()


def add_listener(fn: Callable[[dict], None]):
    if fn not in BUS.listeners:
        BUS.listeners.append(fn)


def publish(topic: str, type_: str, severity: str = "info",
            resource: str = "", data: Optional[dict] = None,
            category: Optional[str] = None) -> dict:
    """Publish one event. category is the Redfish push category
    (leak|thermal|psu|sel|power|grid|health); defaults to the topic."""
    if topic not in TOPICS:
        raise ValueError(f"unknown topic {topic}")
    with STORE.lock:
        BUS._sync()
        ev = {"id": next(BUS._seq), "topic": topic, "type": type_,
              "severity": severity, "resource": resource,
              "category": category or topic, "at": _iso(),
              "data": data or {}}
        BUS.events.append(ev)
        BUS.counts[topic] += 1
        listeners = list(BUS.listeners)
    for fn in listeners:
        try:
            fn(ev)
        except Exception:      # a listener must never break the producer
            pass
    return ev


def recent(topic: Optional[str] = None, since: int = 0,
           limit: int = 200) -> List[dict]:
    with STORE.lock:
        BUS._sync()
        evs = [e for e in BUS.events
               if e["id"] > since and (not topic or e["topic"] in topic.split(","))]
    return evs[-limit:]


@router.get("/topics")
def topics():
    with STORE.lock:
        BUS._sync()
        return {"bus": "dsx-exchange (emulated)", "transport": "SSE",
                "topics": [{"name": t, "description": d,
                            "published": BUS.counts.get(t, 0)}
                           for t, d in TOPICS.items()]}


# SSE DoS guards: hard cap on stream lifetime and concurrent subscribers.
SSE_MAX_S = float(os.environ.get("EMU_SSE_MAX_S", "3600"))
SSE_MAX_CLIENTS = int(os.environ.get("EMU_SSE_MAX_CLIENTS", "64"))
_SSE_CLIENTS = [0]


@router.get("/events")
async def events(topic: Optional[str] = None, since: int = 0,
                 limit: int = 200, stream: bool = True, follow: bool = True,
                 timeout_s: float = 0.0):
    """SSE stream of Exchange events. Sends the backlog (id > since), then —
    when follow=true — keeps streaming new events (timeout_s > 0 ends the
    stream after that many seconds; 0 = until the client disconnects).
    stream=false returns the backlog as a JSON list instead."""
    if topic:
        bad = [t for t in topic.split(",") if t not in TOPICS]
        if bad:
            raise HTTPException(422, f"unknown topic(s) {bad}; "
                                     f"valid: {list(TOPICS)}")
    if not 0 <= timeout_s <= SSE_MAX_S or timeout_s != timeout_s:
        raise HTTPException(422, f"timeout_s must be 0..{SSE_MAX_S:g}")
    limit = max(1, min(limit, BUS.events.maxlen or 10000))
    if not stream:
        return recent(topic, since, limit)
    if follow and _SSE_CLIENTS[0] >= SSE_MAX_CLIENTS:
        raise HTTPException(503, f"too many SSE subscribers (max {SSE_MAX_CLIENTS})")
    # 0 = "until disconnect", bounded by SSE_MAX_S so streams cannot pile up
    timeout_s = timeout_s or SSE_MAX_S

    async def gen():
        if follow:
            _SSE_CLIENTS[0] += 1
        try:
            async for chunk in _gen():
                yield chunk
        finally:
            if follow:
                _SSE_CLIENTS[0] -= 1

    async def _gen():
        last = since
        for e in recent(topic, last, limit):
            last = e["id"]
            yield _sse(e)
        if not follow:
            return
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        idle = 0
        while not timeout_s or loop.time() - t0 < timeout_s:
            await asyncio.sleep(0.5)
            new = recent(topic, last, limit)
            for e in new:
                last = e["id"]
                yield _sse(e)
            idle = 0 if new else idle + 1
            if idle >= 30:                 # keep-alive comment every ~15 s
                idle = 0
                yield ": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


def _sse(e: dict) -> str:
    return f"id: {e['id']}\nevent: {e['topic']}\ndata: {json.dumps(e)}\n\n"
