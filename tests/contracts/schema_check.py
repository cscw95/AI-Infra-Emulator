"""Minimal JSON Schema / OpenAPI 3.1 validator for the contract tests
(type, enum, const, required, properties, additionalProperties, items,
anyOf/oneOf/allOf/not, pattern, min/max, length) — no extra dependency."""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
_T = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def load(name: str) -> dict:
    with open(os.path.join(HERE, name)) as f:
        return json.load(f)


def _is(v, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _T[t])


def errors(v, s: dict, path: str = "$") -> list:
    out = []
    if not isinstance(s, dict) or not s:
        return out
    t = s.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_is(v, x) for x in ts):
            return [f"{path}: {type(v).__name__} is not {t}"]
    if "enum" in s and v not in s["enum"]:
        out.append(f"{path}: {v!r} not in {s['enum']}")
    if "const" in s and v != s["const"]:
        out.append(f"{path}: {v!r} != {s['const']!r}")
    for sub in s.get("allOf", []):
        out += errors(v, sub, path)
    for k in ("anyOf", "oneOf"):
        if k in s and not any(not errors(v, sub, path) for sub in s[k]):
            out.append(f"{path}: matches none of {k}")
    if "not" in s and not errors(v, s["not"], path):
        out.append(f"{path}: must not match {s['not']}")
    if isinstance(v, str):
        if "pattern" in s and not re.search(s["pattern"], v):
            out.append(f"{path}: {v!r} !~ {s['pattern']}")
        if len(v) < s.get("minLength", 0) or len(v) > s.get("maxLength", 1 << 30):
            out.append(f"{path}: length {len(v)} out of range")
    if _is(v, "number"):
        if "minimum" in s and v < s["minimum"]:
            out.append(f"{path}: {v} < {s['minimum']}")
        if "maximum" in s and v > s["maximum"]:
            out.append(f"{path}: {v} > {s['maximum']}")
    if isinstance(v, dict):
        props = s.get("properties", {})
        for r in s.get("required", []):
            if r not in v:
                out.append(f"{path}: missing required {r!r}")
        for k, x in v.items():
            if k in props:
                out += errors(x, props[k], f"{path}.{k}")
            elif s.get("additionalProperties") is False:
                out.append(f"{path}: unexpected property {k!r}")
            elif isinstance(s.get("additionalProperties"), dict):
                out += errors(x, s["additionalProperties"], f"{path}.{k}")
    if isinstance(v, list):
        if len(v) > s.get("maxItems", 1 << 30) or len(v) < s.get("minItems", 0):
            out.append(f"{path}: {len(v)} items out of range")
        if isinstance(s.get("items"), dict):
            for i, x in enumerate(v):
                out += errors(x, s["items"], f"{path}[{i}]")
    return out


def check(v, s: dict):
    e = errors(v, s)
    assert not e, "\n".join(e[:20])
