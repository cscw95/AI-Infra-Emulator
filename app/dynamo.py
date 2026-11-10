"""NVIDIA Dynamo-style inference serving emulator (F01 engine side).

DynamoGraphDeployment (DGD) = one model served by prefill + decode workers
(disaggregated) or combined workers (aggregated), each worker a
tensor-parallel group of Rubin GPUs on Dell PowerEdge XE9812 compute trays.

Latency is computed, not sampled at random:
  perf     = WPPS profile perf × clock^0.8, clock = min(1, DPS rack cap / demand)
             × thermal-throttle / NVLink-degraded factor of the worker's GPUs
  TTFT     = frontend + queue + uncached prompt / prefill rate + NIXL KV transfer
             + first decode step
  TPOT     = base / perf × (1 + 0.6 u²)  (u = decode utilisation from load)
So lowering a DPS power limit (domain limit, Flex event, Redfish/operator
cap) makes generation measurably slower, and GPU faults (gpufaults) take
workers out of rotation. Throughput per MW uses the same power numbers.

  GET    /dynamo/v1/models
  GET    /dynamo/v1/deployments           POST /dynamo/v1/deployments
  GET    /dynamo/v1/deployments/{id}      PATCH|DELETE /dynamo/v1/deployments/{id}
  POST   /dynamo/v1/generate              (stream=true → text/event-stream)
  GET    /dynamo/v1/stats?deployment=&model=&window=
  GET    /dynamo/v1/usage?since=&deployment=
  GET    /dynamo/v1/metrics               (Prometheus text)"""
import itertools
import json
import os
import time
import zlib
from collections import OrderedDict, deque
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from .store import STORE, GPU_PER_TRAY, _iso
from . import dps, gpufaults

router = APIRouter(prefix="/dynamo/v1", tags=["dynamo"])

# ── model catalog (Assumption values for Rubin NVL72, FP4/FP8) ────────
# tp: GPUs per worker; prefill_tps: prompt tokens/s per prefill worker at
# full perf; tpot_ms: decode step at nominal batch; slots: concurrent
# sequences per decode worker; reasoning: share of output that is reasoning.
MODELS = {
    "llama-3.3-70b-instruct": {"tp": 4, "context_length": 131072,
                               "prefill_tps": 60000.0, "tpot_ms": 12.0,
                               "slots": 64, "reasoning": 0.0, "params_b": 70},
    "qwen3-235b-a22b": {"tp": 8, "context_length": 131072,
                        "prefill_tps": 45000.0, "tpot_ms": 18.0,
                        "slots": 96, "reasoning": 0.0, "params_b": 235},
    "deepseek-r1": {"tp": 8, "context_length": 131072,
                    "prefill_tps": 30000.0, "tpot_ms": 22.0,
                    "slots": 128, "reasoning": 0.6, "params_b": 671},
    "kanana-1.5-8b-instruct": {"tp": 1, "context_length": 32768,
                               "prefill_tps": 40000.0, "tpot_ms": 6.0,
                               "slots": 128, "reasoning": 0.0, "params_b": 8},
}
MODES = ("disagg", "agg")
SLO_TARGET = {"ttft_p99_ms": 2000.0, "tpot_p99_ms": 50.0}
FRONTEND_MS = 15.0
NIXL_MS_PER_1K_TOKENS = 1.6       # KV transfer prefill → decode (disagg)
AGG_INTERFERENCE = 1.25           # prefill/decode contention on shared workers
NVLINK_DEGRADED_PERF = 0.5        # TP all-reduce falls back off NVLink
NOMINAL_ISL, NOMINAL_OSL = 1500, 300
MAX_DEPLOYMENTS = 64
MAX_WORKERS = 256
MAX_OUTPUT_TOKENS = 32768
MAX_LOAD_RPS = 10000.0
KV_CACHE_ENTRIES = 4096
SAMPLES_MAX = 5000
USAGE_MAX = 10000
PACE = float(os.environ.get("EMU_DYNAMO_PACE", "0"))   # 1.0 = real-time token pacing
BUCKETS_S = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)
BUCKETS_TOK = (128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 131072)
_WORDS = ("the", "rack", "power", "token", "model", "cluster", "GPU", "fast",
          "serving", "NeoCloud", "inference", "stream", "data", "is", "ready",
          "and", "decode", "prefill", "cache", "efficient")

_now = time.time                  # patchable clock (tests)


def _h(s: str) -> int:
    return zlib.crc32(s.encode())


# ── state ─────────────────────────────────────────────────────────────
class _Dynamo:
    def __init__(self):
        self.seed_gen = -1
        self.deps: Dict[str, dict] = {}
        self.usage: deque = deque(maxlen=USAGE_MAX)
        self.by_request: "OrderedDict[str, dict]" = OrderedDict()
        self._ids = itertools.count(1)
        self._req = itertools.count(1)
        self._usage_seq = itertools.count(1)

    def sync(self):
        cur = getattr(STORE, "seed_gen", 0)
        if cur == self.seed_gen:
            return
        self.seed_gen = cur
        self.deps, self.by_request = {}, OrderedDict()
        self.usage = deque(maxlen=USAGE_MAX)
        self._ids = itertools.count(1)
        _seed()


DYN = _Dynamo()


def _used_gpus() -> set:
    return {g for d in DYN.deps.values() for w in d["workers"] for g in w["gpus"]}


def _free_gpus(racks: List[str]) -> List[tuple]:
    used = _used_gpus()
    out = []
    for rid in racks:
        for tid in STORE.racks[rid].trays:
            for gi in range(GPU_PER_TRAY):
                u = f"GPU-{tid}-g{gi}"
                if u not in used:
                    out.append((rid, tid, u))
    return out


def _place(dep_id: str, model: str, racks: List[str], roles: List[str]) -> List[dict]:
    tp = MODELS[model]["tp"]
    free = _free_gpus(racks)
    workers, i = [], 0
    for n, role in enumerate(roles):
        # a TP group stays inside one rack (one NVLink domain)
        while i < len(free) and (i + tp > len(free) or
                                 free[i][0] != free[i + tp - 1][0]):
            i += 1
        if i + tp > len(free):
            raise HTTPException(409, f"not enough free GPUs on {racks} for "
                                     f"{len(roles)} × TP{tp} workers")
        grp = free[i:i + tp]
        i += tp
        workers.append({"id": f"{dep_id}-{role}-{n}", "role": role,
                        "rack": grp[0][0],
                        "trays": sorted({t for _, t, _ in grp}),
                        "gpus": [u for _, _, u in grp]})
    return workers


def _create(model: str, prefill: int, decode: int, racks: List[str],
            mode: str, name: Optional[str] = None,
            load_rps: Optional[float] = None) -> dict:
    if model not in MODELS:
        raise HTTPException(404, f"model {model} not found; one of {list(MODELS)}")
    if mode not in MODES:
        raise HTTPException(422, f"mode must be one of {list(MODES)}")
    if not racks:
        raise HTTPException(422, "racks must not be empty")
    for r in racks:
        if r not in STORE.racks:
            raise HTTPException(404, f"rack {r} not found")
    if mode == "agg":
        prefill = 0
    if decode < 1 or prefill < 0 or (mode == "disagg" and prefill < 1):
        raise HTTPException(422, "disagg needs prefill_workers ≥1 and decode_workers ≥1")
    if prefill + decode > MAX_WORKERS:
        raise HTTPException(422, f"at most {MAX_WORKERS} workers per deployment")
    if len(DYN.deps) >= MAX_DEPLOYMENTS:
        raise HTTPException(409, f"too many deployments (max {MAX_DEPLOYMENTS})")
    did = name or f"dgd-{next(DYN._ids):04d}"
    if did in DYN.deps:
        raise HTTPException(409, f"deployment {did} exists")
    roles = ["prefill"] * prefill + (["decode"] if mode == "disagg" else ["worker"]) * decode
    d = {"id": did, "kind": "DynamoGraphDeployment", "model": model,
         "mode": mode, "racks": list(dict.fromkeys(racks)),
         "prefill_workers": prefill, "decode_workers": decode,
         "tp": MODELS[model]["tp"], "workers": _place(did, model, racks, roles),
         "load_rps": float(load_rps if load_rps is not None else 2.0),
         "created_at": _iso(), "updated_at": _iso(),
         "kv": OrderedDict(), "samples": deque(maxlen=SAMPLES_MAX),
         "counters": _new_counters(), "rr": 0}
    DYN.deps[did] = d
    return d


def _seed():
    """Demo deployments on the first racks (token-factory pool)."""
    r0, r1 = "su-1-rack-00", "su-1-rack-01"
    try:
        if r0 in STORE.racks:
            _create("llama-3.3-70b-instruct", 2, 4, [r0], "disagg",
                    "dgd-llama-70b", 6.0)
            _create("qwen3-235b-a22b", 1, 2, [r0], "disagg", "dgd-qwen3-235b", 2.0)
        if r1 in STORE.racks:
            _create("deepseek-r1", 0, 2, [r1], "agg", "dgd-deepseek-r1", 1.0)
            _create("kanana-1.5-8b-instruct", 2, 4, [r1], "disagg",
                    "dgd-kanana-8b", 8.0)
    except HTTPException:
        pass


# ── power / health coupling ───────────────────────────────────────────
def _rack_power(rack_id: str, util: float) -> dict:
    """DPS cap → clock fraction → perf; GPU power drawn by this rack."""
    rec = dps.rack_power(rack_id) or {}
    profile = rec.get("profile", "max-p")
    n_gpu = dps.DPS.n_gpu(rack_id) or 1
    need = dps._demand_kw(n_gpu, profile, max(util, dps.WORKLOADS["inference"]["util"]))
    cap = rec.get("cap_kw", need)
    clock = max(0.05, min(1.0, cap / need)) if need else 1.0
    return {"rack": rack_id, "profile": profile,
            "cap_kw": round(cap, 1), "need_kw": round(need, 1),
            "clock": round(clock, 4),
            "perf": dps.PROFILES[profile]["perf_frac"] * clock ** 0.8,
            "kw_per_gpu": min(cap, need) / n_gpu,
            "throttled": need > cap + 1e-6}


def _worker_state(w: dict, gmap: dict, rack_perf: Dict[str, float]) -> dict:
    fs = [gmap[g] for g in w["gpus"] if g in gmap]
    perf = rack_perf[w["rack"]]
    state = "Ready"
    if any(f["kind"] == "xid" for f in fs):
        state, perf = "Unhealthy", 0.0
    elif any(f["kind"] == "nvlink" for f in fs):
        state, perf = "Degraded", perf * NVLINK_DEGRADED_PERF
    elif fs:
        state, perf = "Throttled", perf * gpufaults.THERMAL_THROTTLE_PERF
    return {"state": state, "perf": perf, "faults": [f["id"] for f in fs]}


def _evaluate(d: dict) -> dict:
    """Current capacity / utilisation / latency model of a deployment."""
    m = MODELS[d["model"]]
    gmap = gpufaults.gpu_map()
    # first pass at nominal util to get perf, then utilisation from load
    racks = {r: _rack_power(r, 0.0) for r in {w["rack"] for w in d["workers"]}}
    rperf = {r: p["perf"] for r, p in racks.items()}
    ws = {w["id"]: _worker_state(w, gmap, rperf) for w in d["workers"]}
    dec_role = "decode" if d["mode"] == "disagg" else "worker"
    dec = [w for w in d["workers"] if w["role"] == dec_role and ws[w["id"]]["perf"] > 0]
    pre = ([w for w in d["workers"] if w["role"] == "prefill" and ws[w["id"]]["perf"] > 0]
           if d["mode"] == "disagg" else dec)
    agg = AGG_INTERFERENCE if d["mode"] == "agg" else 1.0
    dec_perf = sum(ws[w["id"]]["perf"] for w in dec)
    pre_perf = sum(ws[w["id"]]["perf"] for w in pre) * (0.5 if d["mode"] == "agg" else 1.0)
    cap_tps = dec_perf * m["slots"] * 1000.0 / (m["tpot_ms"] * agg)
    pre_cap = pre_perf * m["prefill_tps"]
    out_demand = d["load_rps"] * NOMINAL_OSL
    u_dec = out_demand / cap_tps if cap_tps else float("inf")
    u_pre = d["load_rps"] * NOMINAL_ISL / pre_cap if pre_cap else float("inf")
    util = min(1.0, u_dec) if dec else 0.0
    if util > dps.WORKLOADS["inference"]["util"]:      # heavy load draws more power
        racks = {r: _rack_power(r, util) for r in racks}
    power_kw = sum(racks[w["rack"]]["kw_per_gpu"] * len(w["gpus"]) for w in d["workers"])
    served = min(out_demand, cap_tps)
    state = ("Unavailable" if not dec or not pre else
             "Degraded" if any(s["state"] != "Ready" for s in ws.values()) else "Ready")
    return {"model": m, "ws": ws, "dec": dec, "pre": pre, "racks": racks,
            "u_dec": u_dec, "u_pre": u_pre, "cap_tps": cap_tps, "pre_cap": pre_cap,
            "served_tps": served, "power_kw": power_kw, "state": state, "agg": agg}


def _queue_ms(u: float, service_ms: float) -> float:
    if u < 0.7:
        return 0.0
    if u < 0.98:
        return min(30000.0, service_ms * u / (1 - u))
    return min(60000.0, 5000.0 + 20000.0 * (u - 0.98))


def _latency(d: dict, ev: dict, prompt: int, kv_hit: int, dec_w: dict,
             pre_w: Optional[dict], jitter: float) -> dict:
    m = ev["model"]
    dperf = ev["ws"][dec_w["id"]]["perf"]
    pperf = ev["ws"][pre_w["id"]]["perf"] if pre_w else dperf
    if d["mode"] == "agg":
        pperf *= 0.5
    u = min(ev["u_dec"], 1.0)
    tpot = m["tpot_ms"] / dperf * ev["agg"] * (1 + 0.6 * u * u) * jitter
    prefill_ms = max(0, prompt - kv_hit) / (m["prefill_tps"] * pperf) * 1000.0
    nixl = NIXL_MS_PER_1K_TOKENS * prompt / 1000.0 if d["mode"] == "disagg" else 0.0
    queue = _queue_ms(ev["u_pre"], NOMINAL_ISL / (m["prefill_tps"] * max(pperf, 1e-3)) * 1000)
    queue += _queue_ms(ev["u_dec"], m["tpot_ms"] * 4)
    ttft = (FRONTEND_MS + queue + prefill_ms + nixl) * jitter + tpot
    return {"ttft_ms": round(ttft, 2), "tpot_ms": round(tpot, 3),
            "queue_ms": round(queue, 2), "prefill_ms": round(prefill_ms, 2),
            "kv_transfer_ms": round(nixl, 2)}


# ── metrics bookkeeping ───────────────────────────────────────────────
def _new_counters() -> dict:
    return {"req": {"success": 0, "error": 0}, "in_tok": 0, "out_tok": 0,
            "kv_hit_tok": 0, "kv_query_tok": 0,
            "h": {k: {"b": [0] * len(BUCKETS_S if k in ("ttft", "itl", "e2e")
                                     else BUCKETS_TOK), "sum": 0.0, "n": 0}
                  for k in ("ttft", "itl", "e2e", "isl", "osl")}}


def _observe(c: dict, key: str, v: float):
    h = c["h"][key]
    for i, b in enumerate(BUCKETS_S if key in ("ttft", "itl", "e2e") else BUCKETS_TOK):
        if v <= b:
            h["b"][i] += 1
    h["sum"] += v
    h["n"] += 1


def _record(d: dict, ok: bool, res: Optional[dict] = None):
    c = d["counters"]
    c["req"]["success" if ok else "error"] += 1
    if not ok:
        d["samples"].append({"ts": _now(), "ok": False})
        return
    c["in_tok"] += res["prompt_tokens"]
    c["out_tok"] += res["completion_tokens"]
    c["kv_hit_tok"] += res["kv_hit_tokens"]
    c["kv_query_tok"] += res["prompt_tokens"]
    _observe(c, "ttft", res["ttft_ms"] / 1000)
    _observe(c, "itl", res["tpot_ms"] / 1000)
    _observe(c, "e2e", res["e2e_ms"] / 1000)
    _observe(c, "isl", res["prompt_tokens"])
    _observe(c, "osl", res["completion_tokens"])
    d["samples"].append({"ts": _now(), "ok": True, "ttft": res["ttft_ms"],
                         "tpot": res["tpot_ms"], "out": res["completion_tokens"]})


def _pct(vals: List[float], p: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))], 3)


# ── views ─────────────────────────────────────────────────────────────
def _dep_or_404(did: str) -> dict:
    d = DYN.deps.get(did)
    if d is None:
        raise HTTPException(404, f"deployment {did} not found")
    return d


def _view(d: dict, ev: Optional[dict] = None) -> dict:
    ev = ev or _evaluate(d)
    return {"id": d["id"], "kind": d["kind"], "model": d["model"],
            "mode": d["mode"], "state": ev["state"], "racks": d["racks"],
            "prefill_workers": d["prefill_workers"],
            "decode_workers": d["decode_workers"], "tp": d["tp"],
            "gpus": sum(len(w["gpus"]) for w in d["workers"]),
            "load_rps": d["load_rps"],
            "workers": [{**w, "state": ev["ws"][w["id"]]["state"],
                         "perf": round(ev["ws"][w["id"]]["perf"], 4),
                         "faults": ev["ws"][w["id"]]["faults"]}
                        for w in d["workers"]],
            "capacity_tokens_per_s": round(ev["cap_tps"], 1),
            "power_kw": round(ev["power_kw"], 2),
            "created_at": d["created_at"], "updated_at": d["updated_at"]}


def decode_trays() -> List[str]:
    """Trays running decode workers (fault scenarios target these)."""
    with STORE.lock:
        DYN.sync()
        return [t for d in DYN.deps.values() for w in d["workers"]
                if w["role"] in ("decode", "worker") for t in w["trays"]]


def _stats(d: dict, window: float) -> dict:
    ev = _evaluate(d)
    since = _now() - window
    ss = [s for s in d["samples"] if s["ts"] >= since]
    ok = [s for s in ss if s["ok"]]
    ttft = [s["ttft"] for s in ok]
    tpot = [s["tpot"] for s in ok]
    exp = None
    if ev["dec"] and ev["pre"]:
        w = max(ev["dec"], key=lambda x: ev["ws"][x["id"]]["perf"])
        p = ev["pre"][0] if d["mode"] == "disagg" else None
        exp = _latency(d, ev, NOMINAL_ISL, 0, w, p, 1.0)
    src = "observed" if ok else "model"
    t99 = _pct(ttft, 99) if ok else (round(exp["ttft_ms"] * 1.6, 2) if exp else None)
    p99 = _pct(tpot, 99) if ok else (round(exp["tpot_ms"] * 1.15, 3) if exp else None)
    mw = ev["power_kw"] / 1000.0
    breach = (t99 is None or p99 is None or t99 > SLO_TARGET["ttft_p99_ms"]
              or p99 > SLO_TARGET["tpot_p99_ms"])
    return {"deployment": d["id"], "model": d["model"], "state": ev["state"],
            "window_s": window, "source": src, "requests": len(ss),
            "errors": len(ss) - len(ok),
            "ttft_p50_ms": _pct(ttft, 50), "ttft_p99_ms": t99,
            "tpot_p50_ms": _pct(tpot, 50), "tpot_p99_ms": p99,
            "expected": exp, "target": dict(SLO_TARGET), "breach": breach,
            "load_rps": d["load_rps"],
            "utilization": round(min(ev["u_dec"], 9.99), 4),
            "capacity_tokens_per_s": round(ev["cap_tps"], 1),
            "served_tokens_per_s": round(ev["served_tps"], 1),
            "power_kw": round(ev["power_kw"], 2),
            "tokens_per_s_per_mw": round(ev["served_tps"] / mw, 1) if mw else None,
            "capacity_tokens_per_s_per_mw": round(ev["cap_tps"] / mw, 1) if mw else None,
            "power": [{k: v for k, v in r.items() if k != "kw_per_gpu"}
                      for r in ev["racks"].values()]}


# ── API ───────────────────────────────────────────────────────────────
class DeploymentCreate(BaseModel):
    model: str
    prefill_workers: int = 1
    decode_workers: int = 1
    racks: List[str]
    mode: str = "disagg"
    name: Optional[str] = None
    load_rps: Optional[float] = None


class DeploymentPatch(BaseModel):
    prefill_workers: Optional[int] = None
    decode_workers: Optional[int] = None
    load_rps: Optional[float] = None


class GenerateBody(BaseModel):
    deployment: Optional[str] = None
    model: Optional[str] = None            # alternative to deployment (first Ready DGD)
    prompt_tokens: int
    max_tokens: int = 256
    stream: bool = False
    prefix_hash: Optional[str] = None
    request_id: Optional[str] = None       # = gateway x-request-id (idempotent)
    tenant_id: Optional[str] = None


def _check_load(v: Optional[float]):
    if v is not None and not 0 <= v <= MAX_LOAD_RPS:
        raise HTTPException(422, f"load_rps must be in [0, {MAX_LOAD_RPS}]")


@router.get("/models")
def models():
    with STORE.lock:
        DYN.sync()
        out = []
        for mid, m in MODELS.items():
            ds = [d for d in DYN.deps.values() if d["model"] == mid]
            out.append({"id": mid, "owned_by": "neocloud",
                        "context_length": m["context_length"],
                        "tiers": ["interactive", "batch"], "tp": m["tp"],
                        "params_b": m["params_b"],
                        "reasoning": m["reasoning"] > 0,
                        "deployments": [d["id"] for d in ds]})
        return {"object": "list", "data": out}


@router.get("/deployments")
def list_deployments(model: Optional[str] = None):
    with STORE.lock:
        DYN.sync()
        items = [_view(d) for d in DYN.deps.values()
                 if not model or d["model"] == model]
        return {"count": len(items), "deployments": items}


@router.post("/deployments", status_code=201)
def create_deployment(body: DeploymentCreate):
    _check_load(body.load_rps)
    with STORE.lock:
        DYN.sync()
        d = _create(body.model, body.prefill_workers, body.decode_workers,
                    body.racks, body.mode, body.name, body.load_rps)
        return _view(d)


@router.get("/deployments/{did}")
def get_deployment(did: str):
    with STORE.lock:
        DYN.sync()
        return _view(_dep_or_404(did))


@router.patch("/deployments/{did}")
def patch_deployment(did: str, body: DeploymentPatch):
    """Planner-style scaling (worker counts) and background load."""
    _check_load(body.load_rps)
    with STORE.lock:
        DYN.sync()
        d = _dep_or_404(did)
        if body.load_rps is not None:
            d["load_rps"] = float(body.load_rps)
        if body.prefill_workers is not None or body.decode_workers is not None:
            pre = d["prefill_workers"] if body.prefill_workers is None else body.prefill_workers
            dec = d["decode_workers"] if body.decode_workers is None else body.decode_workers
            old = d["workers"]
            d["workers"] = []                  # free the GPUs, re-place
            try:
                if d["mode"] == "agg":
                    pre = 0
                if dec < 1 or pre < 0 or (d["mode"] == "disagg" and pre < 1) \
                        or pre + dec > MAX_WORKERS:
                    raise HTTPException(422, "invalid worker counts")
                roles = (["prefill"] * pre +
                         (["decode"] if d["mode"] == "disagg" else ["worker"]) * dec)
                d["workers"] = _place(did, d["model"], d["racks"], roles)
            except HTTPException:
                d["workers"] = old
                raise
            d["prefill_workers"], d["decode_workers"] = pre, dec
            d["kv"] = OrderedDict()
        d["updated_at"] = _iso()
        return _view(d)


@router.delete("/deployments/{did}")
def delete_deployment(did: str):
    with STORE.lock:
        DYN.sync()
        _dep_or_404(did)
        del DYN.deps[did]
        return {"deleted": did}


def _resolve(body: GenerateBody) -> dict:
    if body.deployment:
        return _dep_or_404(body.deployment)
    if not body.model:
        raise HTTPException(422, "deployment or model is required")
    if body.model not in MODELS:
        raise HTTPException(404, f"model {body.model} not found")
    ds = [d for d in DYN.deps.values() if d["model"] == body.model]
    if not ds:
        raise HTTPException(404, f"model {body.model} has no deployment")
    ready = [d for d in ds if _evaluate(d)["state"] != "Unavailable"]
    return (ready or ds)[0]


def _run(body: GenerateBody) -> dict:
    """Route + compute one request; returns the result record."""
    d = _resolve(body)
    m = MODELS[d["model"]]
    if body.prompt_tokens < 0 or body.prompt_tokens > m["context_length"]:
        raise HTTPException(422, f"prompt_tokens must be 0..{m['context_length']}")
    if not 1 <= body.max_tokens <= min(MAX_OUTPUT_TOKENS,
                                       m["context_length"] - body.prompt_tokens):
        raise HTTPException(422, "max_tokens out of range for the model context")
    ev = _evaluate(d)
    if ev["state"] == "Unavailable":
        _record(d, False)
        raise HTTPException(503, f"deployment {d['id']} has no healthy "
                                 f"{'decode' if not ev['dec'] else 'prefill'} worker")
    rid = body.request_id or f"req-{next(DYN._req):06d}"
    # KV-aware routing: a known prefix goes back to the worker that holds it
    kv_hit, dec_w = 0, None
    if body.prefix_hash and body.prefix_hash in d["kv"]:
        wid, cached = d["kv"][body.prefix_hash]
        dec_w = next((w for w in ev["dec"] if w["id"] == wid), None)
        if dec_w is not None:
            kv_hit = min(body.prompt_tokens, cached)
            d["kv"].move_to_end(body.prefix_hash)
    if dec_w is None:
        dec_w = max(ev["dec"], key=lambda w: (ev["ws"][w["id"]]["perf"],
                                              -_h(f"{rid}{w['id']}") % 997))
    pre_w = None
    if d["mode"] == "disagg":
        d["rr"] = (d["rr"] + 1) % len(ev["pre"])
        pre_w = ev["pre"][d["rr"]]
    if body.prefix_hash:
        prev = d["kv"].get(body.prefix_hash, (None, 0))[1]
        d["kv"][body.prefix_hash] = (dec_w["id"], max(prev, body.prompt_tokens))
        while len(d["kv"]) > KV_CACHE_ENTRIES:
            d["kv"].popitem(last=False)
    jitter = 0.94 + (_h(rid) % 1000) / 1000 * 0.12
    lat = _latency(d, ev, body.prompt_tokens, kv_hit, dec_w, pre_w, jitter)
    out = body.max_tokens
    reasoning = int(out * m["reasoning"])
    res = {"id": rid, "deployment": d["id"], "model": d["model"],
           "tenant_id": body.tenant_id,
           "prompt_tokens": body.prompt_tokens, "cached_tokens": kv_hit,
           "kv_hit_tokens": kv_hit, "completion_tokens": out,
           "reasoning_tokens": reasoning,
           "total_tokens": body.prompt_tokens + out, **lat,
           "e2e_ms": round(lat["ttft_ms"] + lat["tpot_ms"] * (out - 1), 2),
           "worker": {"prefill": pre_w["id"] if pre_w else dec_w["id"],
                      "decode": dec_w["id"]},
           "racks": sorted({dec_w["rack"]} | ({pre_w["rack"]} if pre_w else set())),
           "perf": round(ev["ws"][dec_w["id"]]["perf"], 4),
           "finish_reason": "length", "at": _iso()}
    _record(d, True, res)
    DYN.usage.append({"seq": next(DYN._usage_seq), "source": "engine",
                      "request_id": rid, "deployment": d["id"], "model": d["model"],
                      "tenant_id": body.tenant_id,
                      "prompt_tokens": res["prompt_tokens"],
                      "cached_tokens": kv_hit, "completion_tokens": out,
                      "reasoning_tokens": reasoning,
                      "total_tokens": res["total_tokens"], "at": res["at"]})
    DYN.by_request[rid] = res
    while len(DYN.by_request) > USAGE_MAX:
        DYN.by_request.popitem(last=False)
    return res


def _token(rid: str, i: int) -> str:
    w = _WORDS[_h(f"{rid}:{i}") % len(_WORDS)]
    return w if i == 0 else " " + w


def _sse(res: dict):
    yield f"data: {json.dumps({'type': 'start', 'id': res['id'], 'deployment': res['deployment'], 'model': res['model']})}\n\n"
    if PACE > 0:
        time.sleep(res["ttft_ms"] / 1000 * PACE)
    for i in range(res["completion_tokens"]):
        if PACE > 0 and i:
            time.sleep(res["tpot_ms"] / 1000 * PACE)
        t_ms = round(res["ttft_ms"] + i * res["tpot_ms"], 2)
        yield (f"data: {json.dumps({'type': 'token', 'index': i, 'text': _token(res['id'], i), 't_ms': t_ms})}\n\n")
    yield f"data: {json.dumps({'type': 'done', **res})}\n\n"
    yield "data: [DONE]\n\n"


@router.post("/generate")
def generate(body: GenerateBody):
    with STORE.lock:
        DYN.sync()
        prev = DYN.by_request.get(body.request_id) if body.request_id else None
        res = {**prev, "replayed": True} if prev else _run(body)
    if body.stream:
        return StreamingResponse(_sse(res), media_type="text/event-stream",
                                 headers={"x-request-id": res["id"],
                                          "cache-control": "no-cache"})
    return {**res, "text": "".join(_token(res["id"], i)
                                   for i in range(min(res["completion_tokens"], 64)))}


@router.get("/stats")
def stats(deployment: Optional[str] = None, model: Optional[str] = None,
          window: float = 300.0):
    """Per-deployment SLO view (TTFT/TPOT p50/p99 vs deck target), tokens/s,
    power and tokens/s per MW (NOCP /api/v1/token/slo source)."""
    window = max(1.0, min(window, 30 * 86400.0))
    with STORE.lock:
        DYN.sync()
        ds = ([_dep_or_404(deployment)] if deployment else
              [d for d in DYN.deps.values() if not model or d["model"] == model])
        items = [_stats(d, window) for d in ds]
        tps = sum(i["served_tokens_per_s"] for i in items)
        kw = sum(i["power_kw"] for i in items)
        return {"window_s": window, "target": dict(SLO_TARGET),
                "deployments": items,
                "total": {"served_tokens_per_s": round(tps, 1),
                          "power_kw": round(kw, 2),
                          "tokens_per_s_per_mw": round(tps / (kw / 1000), 1) if kw else None,
                          "breach": any(i["breach"] for i in items)}}


@router.get("/usage")
def usage(since: int = 0, deployment: Optional[str] = None,
          tenant_id: Optional[str] = None, limit: int = 1000):
    """Engine-side usage records (F04 metering source "engine")."""
    with STORE.lock:
        DYN.sync()
        rows = [u for u in DYN.usage if u["seq"] > since
                and (not deployment or u["deployment"] == deployment)
                and (not tenant_id or u["tenant_id"] == tenant_id)]
        rows = rows[:max(1, min(limit, USAGE_MAX))]
        return {"count": len(rows), "next": rows[-1]["seq"] if rows else since,
                "records": rows}


def _hist(lines: List[str], name: str, lbl: str, h: dict, buckets):
    for b, n in zip(buckets, h["b"]):
        lines.append(f'{name}_bucket{{{lbl},le="{b}"}} {n}')
    lines.append(f'{name}_bucket{{{lbl},le="+Inf"}} {h["n"]}')
    lines.append(f"{name}_sum{{{lbl}}} {round(h['sum'], 6)}")
    lines.append(f"{name}_count{{{lbl}}} {h['n']}")


@router.get("/metrics", response_class=PlainTextResponse)
def metrics():
    """Prometheus exposition — metric names match the Dynamo frontend /
    router / operator metrics the console queries."""
    with STORE.lock:
        DYN.sync()
        L = []
        hist = [("dynamo_frontend_time_to_first_token_seconds", "ttft", BUCKETS_S),
                ("dynamo_frontend_inter_token_latency_seconds", "itl", BUCKETS_S),
                ("dynamo_frontend_request_duration_seconds", "e2e", BUCKETS_S),
                ("dynamo_frontend_input_sequence_tokens", "isl", BUCKETS_TOK),
                ("dynamo_frontend_output_sequence_tokens", "osl", BUCKETS_TOK)]
        for name, _, _ in hist:
            L.append(f"# TYPE {name} histogram")
        L.append("# TYPE dynamo_frontend_requests_total counter")
        L.append("# TYPE dynamo_frontend_output_tokens_total counter")
        L.append("# TYPE dynamo_frontend_input_tokens_total counter")
        for g in ("dynamo_frontend_inflight_requests", "dynamo_frontend_queued_requests",
                  "dynamo_component_router_kv_hit_rate", "dynamo_worker_count",
                  "neocloud_dynamo_power_kw", "neocloud_dynamo_tokens_per_second",
                  "neocloud_dynamo_tokens_per_second_per_mw",
                  "neocloud_dynamo_capacity_tokens_per_second",
                  "neocloud_dynamo_perf_factor", "dynamo_operator_resources_total"):
            L.append(f"# TYPE {g} gauge")
        for d in DYN.deps.values():
            ev = _evaluate(d)
            c = d["counters"]
            lbl = f'dynamo_namespace="{d["id"]}",model="{d["model"]}"'
            for st, n in c["req"].items():
                L.append(f'dynamo_frontend_requests_total{{{lbl},status="{st}"}} {n}')
            L.append(f"dynamo_frontend_output_tokens_total{{{lbl}}} {c['out_tok']}")
            L.append(f"dynamo_frontend_input_tokens_total{{{lbl}}} {c['in_tok']}")
            for name, key, b in hist:
                _hist(L, name, lbl, c["h"][key], b)
            m = ev["model"]
            e2e_s = (NOMINAL_OSL * m["tpot_ms"] / 1000) / max(
                ev["ws"][ev["dec"][0]["id"]]["perf"], 1e-3) if ev["dec"] else 0
            slots = len(ev["dec"]) * m["slots"]
            infl = d["load_rps"] * e2e_s
            L.append(f"dynamo_frontend_inflight_requests{{{lbl}}} {round(min(infl, slots), 2)}")
            L.append(f"dynamo_frontend_queued_requests{{{lbl}}} {round(max(0.0, infl - slots), 2)}")
            hit = c["kv_hit_tok"] / c["kv_query_tok"] if c["kv_query_tok"] else 0.0
            L.append(f"dynamo_component_router_kv_hit_rate{{{lbl}}} {round(hit, 4)}")
            for role in ("prefill", "decode", "worker"):
                n = sum(1 for w in d["workers"] if w["role"] == role)
                if n:
                    healthy = sum(1 for w in d["workers"] if w["role"] == role
                                  and ev["ws"][w["id"]]["perf"] > 0)
                    L.append(f'dynamo_worker_count{{{lbl},role="{role}",state="healthy"}} {healthy}')
                    L.append(f'dynamo_worker_count{{{lbl},role="{role}",state="unhealthy"}} {n - healthy}')
            mw = ev["power_kw"] / 1000
            L.append(f"neocloud_dynamo_power_kw{{{lbl}}} {round(ev['power_kw'], 3)}")
            L.append(f"neocloud_dynamo_tokens_per_second{{{lbl}}} {round(ev['served_tps'], 2)}")
            L.append(f"neocloud_dynamo_capacity_tokens_per_second{{{lbl}}} {round(ev['cap_tps'], 2)}")
            L.append(f"neocloud_dynamo_tokens_per_second_per_mw{{{lbl}}} "
                     f"{round(ev['served_tps'] / mw, 2) if mw else 0}")
            perf = max((s["perf"] for s in ev["ws"].values()), default=0.0)
            L.append(f"neocloud_dynamo_perf_factor{{{lbl}}} {round(perf, 4)}")
        L.append('dynamo_operator_resources_total{resource_type="DynamoGraphDeployment"} '
                 f"{len(DYN.deps)}")
        return "\n".join(L) + "\n"
