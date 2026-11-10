"""F33 KV cache tiers G1–G4 (Dynamo KV Block Manager, KVBM) for dynamo.py.

Upstream (ref/upstream/UPSTREAM-REFS.md, ai-dynamo/dynamo v1.6.2 @
3a524d0d70f9, Apache-2.0):
  tiers    docs/fern/pages/reference/components/kvbm-configuration.mdx —
           "KVBM offloads KV blocks GPU (G1) → host/CPU (G2) → disk (G3) →
           object storage (G4)"; a tier is enabled by a size in GB or an
           explicit block count (the block-count override wins). Config keys
           are the DYN_KVBM_* env var names; G1 uses vLLM's
           num_gpu_blocks_override.
  events   lib/kv-router/src/protocols.rs StorageTier (serde snake_case):
           device | host_pinned | disk | external → G1 | G2 | G3 | G4.
           Stored/Removed events carry the tier in RouterEvent.storage_tier.
  metrics  lib/runtime/src/metrics/prometheus_names.rs `pub mod kvbm`
           (offload_blocks_d2h/h2d/d2o, onboard_blocks_h2d/d2d/o2d,
           matched_tokens, host/disk/object_cache_hit_rate), exported with
           the kvbm_ prefix.
NeoCloud mapping (x-neocloud, not upstream): G1 = Rubin HBM4 288 GB/GPU,
G2 = Vera LPDDR5X 1.5 TB/CPU (2 Vera per tray), G3 = tray-local NVMe,
G4 = a VAST Data view (S3) per site. KVBM has no REST API upstream; the
routes below are the emulator's inspection/config surface.

Flow (KVBM offload/onboard): G1 LRU eviction → copy to G2 (d2h) and, when the
object tier is on, write-through to G4 (d2o); G2 eviction → G3 (h2d); G3
eviction drops the block (G4 still holds it). A G1 miss looks up G2 → G3 →
G4 and onboards the prefix into G1 of the decode worker; those tokens are
prompt_tokens_details.cached_tokens (no prefill) but TTFT pays the
onboarding transfer. G2/G3 are local to the worker that offloaded; G4 is
shared, any decode worker can onboard from it.

  GET /dynamo/v1/kv/tiers?deployment=     tiers, capacity, occupancy, hit rate
  PUT /dynamo/v1/kv/tiers/config          {deployment?, <DYN_KVBM_* keys>}
"""
import math
from collections import OrderedDict
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from .store import STORE, GPU_PER_TRAY

router = APIRouter(prefix="/dynamo/v1/kv", tags=["dynamo-kvbm"])

# ── hardware (Vera Rubin NVL72 on Dell PowerEdge XE9812; Assumption where noted)
HBM_GB_PER_GPU = 288.0             # Rubin HBM4
LPDDR5X_GB_PER_VERA = 1536.0       # 1.5 TB per Vera CPU
VERA_PER_TRAY = 2
NVME_TB_PER_TRAY = 61.44           # 8 × 7.68 TB E1.S (Assumption)
GPU_MEM_UTIL = 0.9                 # vLLM gpu_memory_utilization default
HOST_KV_FRAC = 0.5                 # share of LPDDR5X given to KVBM (Assumption)
DISK_KV_FRAC = 0.5                 # share of NVMe given to KVBM (Assumption)
OBJECT_DEFAULT_TB = 200.0          # VAST kvcache view quota per site (Assumption)
# onboard path → effective GB/s and fixed setup ms (Assumption)
ONBOARD = {"G2": (900.0, 0.05),    # NVLink-C2C host → HBM
           "G3": (50.0, 0.2),      # NVMe → HBM (GDS)
           "G4": (100.0, 2.0)}     # VAST over ConnectX-9 RDMA
# bf16 KV bytes per token = layers × kv_heads × head_dim × 2 (K,V) × 2 B;
# DeepSeek MLA = layers × (kv_lora_rank 512 + rope 64) × 2 B
KV_BYTES_PER_TOKEN = {"llama-3.3-70b-instruct": 80 * 8 * 128 * 4,
                      "qwen3-235b-a22b": 94 * 4 * 128 * 4,
                      "deepseek-r1": 61 * 576 * 2,
                      "kanana-1.5-8b-instruct": 32 * 8 * 128 * 4}
WEIGHT_BYTES_PER_PARAM = 1.0       # FP8 weights
TIERS = ("G1", "G2", "G3", "G4")
STORAGE_TIER = {"G1": "device", "G2": "host_pinned", "G3": "disk", "G4": "external"}
MEDIUM = {"G1": "HBM4", "G2": "LPDDR5X", "G3": "NVMe", "G4": "VAST Data"}
CONFIG_KEYS = ("num_gpu_blocks_override",
               "DYN_KVBM_CPU_CACHE_GB", "DYN_KVBM_CPU_CACHE_OVERRIDE_NUM_BLOCKS",
               "DYN_KVBM_DISK_CACHE_GB", "DYN_KVBM_DISK_CACHE_OVERRIDE_NUM_BLOCKS",
               "DYN_KVBM_OBJECT_ENABLED", "DYN_KVBM_OBJECT_NUM_BLOCKS",
               "DYN_KVBM_OBJECT_BUCKET")
MAX_BLOCKS = 10 ** 12


class _State:
    def __init__(self):
        self.seed_gen = -1
        self.config: Dict[str, dict] = {}       # "*" or deployment id → keys

    def sync(self):
        cur = getattr(STORE, "seed_gen", 0)
        if cur != self.seed_gen:
            self.seed_gen, self.config = cur, {}


KV = _State()


def _dyn():
    from . import dynamo
    return dynamo


def _bs() -> int:
    return _dyn().KV_BLOCK_SIZE


def _cfg(d: dict) -> dict:
    KV.sync()
    return {**KV.config.get("*", {}), **KV.config.get(d["id"], {})}


def _state(d: dict) -> dict:
    s = d.get("kvbm")
    if s is None:
        s = d["kvbm"] = {"tiers": {t: OrderedDict() for t in TIERS[1:]},
                         "c": {k: 0 for k in (
                             "offload_blocks_d2h", "offload_blocks_h2d",
                             "offload_blocks_d2o", "onboard_blocks_h2d",
                             "onboard_blocks_d2d", "onboard_blocks_o2d",
                             "offload_bytes_object", "onboard_bytes_object",
                             "matched_tokens", "query_tokens",
                             "hit_G1", "hit_G2", "hit_G3", "hit_G4")}}
    return s


def kv_bytes(model: str) -> int:
    return KV_BYTES_PER_TOKEN.get(model, 80 * 8 * 128 * 4)


def _kv_workers(d: dict) -> List[dict]:
    return [w for w in d["workers"] if w["role"] != "prefill"]


def _site(d: dict) -> str:
    r = STORE.racks.get(d["racks"][0]) if d["racks"] else None
    return getattr(r, "site_id", None) or "site-1"


def physical(d: dict) -> Dict[str, float]:
    """Physical GB per tier for the deployment's KV-holding workers."""
    m = _dyn().MODELS[d["model"]]
    gpus = sum(len(w["gpus"]) for w in _kv_workers(d))
    weights_gb = m["params_b"] * WEIGHT_BYTES_PER_PARAM / m["tp"]
    g1 = gpus * max(0.0, HBM_GB_PER_GPU * GPU_MEM_UTIL - weights_gb)
    tray_share = gpus / GPU_PER_TRAY          # trays' worth of GPUs held
    g2 = tray_share * VERA_PER_TRAY * LPDDR5X_GB_PER_VERA * HOST_KV_FRAC
    g3 = tray_share * NVME_TB_PER_TRAY * 1000 * DISK_KV_FRAC
    return {"G1": g1, "G2": g2, "G3": g3, "G4": OBJECT_DEFAULT_TB * 1000}


def capacity_blocks(d: dict) -> Dict[str, int]:
    """Blocks per tier: explicit block count wins over GB, GB over physical."""
    c, gb, bpb = _cfg(d), physical(d), kv_bytes(d["model"]) * _bs()

    def blocks(g):
        return int(g * 1e9 // bpb)
    out = {"G1": c.get("num_gpu_blocks_override") or blocks(gb["G1"])}
    for t, (k_gb, k_n) in {"G2": ("DYN_KVBM_CPU_CACHE_GB",
                                  "DYN_KVBM_CPU_CACHE_OVERRIDE_NUM_BLOCKS"),
                           "G3": ("DYN_KVBM_DISK_CACHE_GB",
                                  "DYN_KVBM_DISK_CACHE_OVERRIDE_NUM_BLOCKS")}.items():
        if c.get(k_n) is not None:
            out[t] = int(c[k_n])
        elif c.get(k_gb) is not None:
            out[t] = blocks(c[k_gb])
        else:
            out[t] = blocks(gb[t])
    if c.get("DYN_KVBM_OBJECT_ENABLED", True) is False:
        out["G4"] = 0
    else:
        out["G4"] = int(c["DYN_KVBM_OBJECT_NUM_BLOCKS"]) \
            if c.get("DYN_KVBM_OBJECT_NUM_BLOCKS") is not None else blocks(gb["G4"])
    return out


def _used_blocks(entries) -> int:
    bs = _bs()
    return sum(n // bs for _, n in entries.values())


def _emit_store(d, prefix, worker, tokens, tier):
    if tokens >= _bs():
        _dyn()._kv_store(d, prefix, worker, 0, tokens, STORAGE_TIER[tier])


def _emit_remove(d, prefix, worker, tokens, tier):
    _dyn()._kv_remove(d, prefix, worker, tokens, STORAGE_TIER[tier])


def _put(d: dict, tier: str, prefix: str, worker: str, tokens: int):
    """Insert/refresh prefix in a lower tier, then evict that tier's LRU."""
    s, cap = _state(d), capacity_blocks(d)
    if cap[tier] <= 0 or tokens // _bs() == 0:
        return
    t = s["tiers"][tier]
    old = t.get(prefix)
    if old and old[1] >= tokens:
        t.move_to_end(prefix)
        return
    if old:
        _emit_remove(d, prefix, old[0], old[1], tier)
    t[prefix] = (worker, tokens)
    t.move_to_end(prefix)
    _emit_store(d, prefix, worker, tokens, tier)
    _evict(d, tier, cap[tier])


def _evict(d: dict, tier: str, cap: int):
    s = _state(d)
    t = s["tiers"][tier]
    while t and _used_blocks(t) > cap:
        k, (w, n) = t.popitem(last=False)
        _emit_remove(d, k, w, n, tier)
        if tier == "G2":                       # host → disk
            s["c"]["offload_blocks_h2d"] += n // _bs()
            _put(d, "G3", k, w, n)
        # G3 / G4 eviction drops the block


def offload(d: dict, prefix: str, worker: str, tokens: int):
    """Called when G1 evicts a prefix (after its device Removed event)."""
    s, cap, n = _state(d), capacity_blocks(d), tokens // _bs()
    if n == 0:
        return
    if cap["G2"] > 0:
        s["c"]["offload_blocks_d2h"] += n
        _put(d, "G2", prefix, worker, tokens)
    elif cap["G3"] > 0:
        _put(d, "G3", prefix, worker, tokens)
    if cap["G4"] > 0:                          # object tier write-through
        s["c"]["offload_blocks_d2o"] += n
        s["c"]["offload_bytes_object"] += n * _bs() * kv_bytes(d["model"])
        _put(d, "G4", prefix, worker, tokens)


def enforce_g1(d: dict):
    """Evict G1 (d["kv"]) LRU while it holds more blocks than G1 capacity."""
    dyn, cap = _dyn(), capacity_blocks(d)["G1"]
    while d["kv"] and _used_blocks(d["kv"]) > cap:
        k, (w, n) = d["kv"].popitem(last=False)
        dyn._kv_remove(d, k, w, n)
        offload(d, k, w, n)


def onboard(d: dict, ev: dict, prefix: str, prompt_tokens: int) -> Optional[dict]:
    """G1 miss: find the prefix in G2 → G3 → G4 and onboard it."""
    s = _state(d)
    healthy = {w["id"]: w for w in ev["dec"]}
    for tier in TIERS[1:]:
        e = s["tiers"][tier].get(prefix)
        if not e:
            continue
        wid, tokens = e
        if tier == "G4":
            w = healthy.get(wid) or (max(ev["dec"], key=lambda x: ev["ws"][x["id"]]["perf"])
                                     if ev["dec"] else None)
        else:
            w = healthy.get(wid)               # host/disk are worker-local
        if w is None:
            continue
        s["tiers"][tier].move_to_end(prefix)
        hit = min(prompt_tokens, tokens)
        blocks = hit // _bs()
        key = {"G2": "onboard_blocks_h2d", "G3": "onboard_blocks_d2d",
               "G4": "onboard_blocks_o2d"}[tier]
        s["c"][key] += blocks
        nbytes = hit * kv_bytes(d["model"])
        if tier == "G4":
            s["c"]["onboard_bytes_object"] += nbytes
        gbps, fixed = ONBOARD[tier]
        return {"worker": w, "tokens": hit, "tier": tier,
                "onboard_ms": round(fixed + nbytes / (gbps * 1e9) * 1000.0, 3)}
    return None


def account(d: dict, prompt_tokens: int, hit: int, tier: Optional[str]):
    c = _state(d)["c"]
    c["query_tokens"] += prompt_tokens
    if tier and hit:
        c["matched_tokens"] += hit
        c[f"hit_{tier}"] += hit


def clear(d: dict):
    """clear_kv_blocks: drop every KVBM tier (publishes Removed per entry)."""
    s = _state(d)
    for tier, t in s["tiers"].items():
        for k, (w, n) in list(t.items()):
            _emit_remove(d, k, w, n, tier)
        t.clear()
    for k in s["c"]:
        s["c"][k] = 0


def _rate(c: dict, tier: str) -> float:
    return round(c[f"hit_{tier}"] / c["query_tokens"], 4) if c["query_tokens"] else 0.0


def tiers_view(d: dict) -> dict:
    s, cap, gb = _state(d), capacity_blocks(d), physical(d)
    bs, kvb = _bs(), kv_bytes(d["model"])
    c, cfg, site = s["c"], _cfg(d), _site(d)
    gpus = sum(len(w["gpus"]) for w in _kv_workers(d))
    rows = []
    for t in TIERS:
        entries = d["kv"] if t == "G1" else s["tiers"][t]
        used = _used_blocks(entries)
        row = {"tier": t, "storage_tier": STORAGE_TIER[t], "medium": MEDIUM[t],
               "enabled": cap[t] > 0,
               "physical_gb": round(gb[t], 1),
               "capacity_blocks": cap[t], "capacity_tokens": cap[t] * bs,
               "capacity_gb": round(cap[t] * bs * kvb / 1e9, 3),
               "used_blocks": used, "used_tokens": used * bs,
               "entries": len(entries),
               "utilization": round(used / cap[t], 4) if cap[t] else 0.0,
               "hit_tokens": c[f"hit_{t}"], "hit_rate": _rate(c, t)}
        if t == "G1":
            row["unit"] = {"hbm_gb_per_gpu": HBM_GB_PER_GPU, "gpus": gpus,
                           "gpu_memory_utilization": GPU_MEM_UTIL}
        elif t == "G2":
            row["unit"] = {"lpddr5x_gb_per_vera": LPDDR5X_GB_PER_VERA,
                           "vera_per_tray": VERA_PER_TRAY, "kv_fraction": HOST_KV_FRAC}
        elif t == "G3":
            row["unit"] = {"nvme_tb_per_tray": NVME_TB_PER_TRAY, "kv_fraction": DISK_KV_FRAC}
        else:
            bucket = (cfg.get("DYN_KVBM_OBJECT_BUCKET") or "kv-cache-{worker_id}")
            row["vast"] = {"cluster": f"vast-{site}", "site": site,
                           "view_path": f"/neocloud/kvcache/{d['id']}",
                           "protocol": "S3", "bucket": bucket}
        if t != "G1":
            row["onboard_gbps"], row["onboard_setup_ms"] = ONBOARD[t]
        rows.append(row)
    return {"deployment": d["id"], "model": d["model"], "kv_block_size": bs,
            "kv_bytes_per_token": kvb, "tiers": rows,
            "matched_tokens": c["matched_tokens"], "query_tokens": c["query_tokens"],
            "cached_ratio": round(c["matched_tokens"] / c["query_tokens"], 4)
            if c["query_tokens"] else 0.0,
            "counters": {k: v for k, v in c.items() if not k.startswith("hit_")
                         and k != "query_tokens"},
            "config": cfg}


def metric_lines() -> List[str]:
    """kvbm_* series (prometheus_names.rs mod kvbm) per deployment."""
    dyn = _dyn()
    L = []
    for name in ("offload_blocks_d2h", "offload_blocks_h2d", "offload_blocks_d2o",
                 "onboard_blocks_h2d", "onboard_blocks_d2d", "onboard_blocks_o2d",
                 "offload_bytes_object", "onboard_bytes_object", "matched_tokens"):
        L.append(f"# TYPE kvbm_{name} counter")
    for name in ("host_cache_hit_rate", "disk_cache_hit_rate", "object_cache_hit_rate"):
        L.append(f"# TYPE kvbm_{name} gauge")
    L.append("# TYPE neocloud_kvbm_tier_used_blocks gauge")
    L.append("# TYPE neocloud_kvbm_tier_capacity_blocks gauge")
    for d in dyn.DYN.deps.values():
        s, cap = _state(d), capacity_blocks(d)
        lbl = f'dynamo_namespace="{d["id"]}",model="{d["model"]}"'
        for k, v in s["c"].items():
            if not k.startswith("hit_") and k != "query_tokens":
                L.append(f"kvbm_{k}{{{lbl}}} {v}")
        for t, name in (("G2", "host"), ("G3", "disk"), ("G4", "object")):
            L.append(f"kvbm_{name}_cache_hit_rate{{{lbl}}} {_rate(s['c'], t)}")
        for t in TIERS:
            entries = d["kv"] if t == "G1" else s["tiers"][t]
            tl = f'{lbl},tier="{t}",storage_tier="{STORAGE_TIER[t]}"'
            L.append(f"neocloud_kvbm_tier_used_blocks{{{tl}}} {_used_blocks(entries)}")
            L.append(f"neocloud_kvbm_tier_capacity_blocks{{{tl}}} {cap[t]}")
    return L


# ── routes ────────────────────────────────────────────────────────────
class TierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deployment: Optional[str] = None
    num_gpu_blocks_override: Optional[int] = None
    DYN_KVBM_CPU_CACHE_GB: Optional[float] = None
    DYN_KVBM_CPU_CACHE_OVERRIDE_NUM_BLOCKS: Optional[int] = None
    DYN_KVBM_DISK_CACHE_GB: Optional[float] = None
    DYN_KVBM_DISK_CACHE_OVERRIDE_NUM_BLOCKS: Optional[int] = None
    DYN_KVBM_OBJECT_ENABLED: Optional[bool] = None
    DYN_KVBM_OBJECT_NUM_BLOCKS: Optional[int] = None
    DYN_KVBM_OBJECT_BUCKET: Optional[str] = None


@router.get("/tiers")
def get_tiers(deployment: Optional[str] = None):
    dyn = _dyn()
    with STORE.lock:
        dyn.DYN.sync()
        ds = [dyn._dep_or_404(deployment)] if deployment else list(dyn.DYN.deps.values())
        items = [tiers_view(d) for d in ds]
        tot = {t: {"capacity_blocks": sum(next(r for r in i["tiers"] if r["tier"] == t)
                                          ["capacity_blocks"] for i in items),
                   "used_blocks": sum(next(r for r in i["tiers"] if r["tier"] == t)
                                      ["used_blocks"] for i in items)} for t in TIERS}
        m = sum(i["matched_tokens"] for i in items)
        q = sum(i["query_tokens"] for i in items)
        return {"x-neocloud": True, "tiers": list(TIERS),
                "storage_tier": dict(STORAGE_TIER), "deployments": items,
                "total": {"tiers": tot, "matched_tokens": m, "query_tokens": q,
                          "cached_ratio": round(m / q, 4) if q else 0.0},
                "global_config": dict(KV.config.get("*", {}))}


@router.put("/tiers/config")
def put_config(body: TierConfig):
    """Replace the KVBM config (DYN_KVBM_* names) for one deployment or all;
    then evict down to the new capacities."""
    dyn = _dyn()
    with STORE.lock:
        dyn.DYN.sync()
        KV.sync()
        if body.deployment:
            dyn._dep_or_404(body.deployment)
        vals = {k: v for k, v in body.model_dump().items()
                if k != "deployment" and v is not None}
        for k, v in vals.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and (
                    not math.isfinite(v) or v < 0 or v > MAX_BLOCKS):
                raise HTTPException(422, f"{k} must be in [0, {MAX_BLOCKS}]")
        KV.config[body.deployment or "*"] = vals
        ds = [dyn.DYN.deps[body.deployment]] if body.deployment else \
            list(dyn.DYN.deps.values())
        for d in ds:
            enforce_g1(d)
            cap = capacity_blocks(d)
            for t in TIERS[1:]:
                _evict(d, t, cap[t])
        return {"deployment": body.deployment, "config": vals,
                "applied_to": [d["id"] for d in ds]}
