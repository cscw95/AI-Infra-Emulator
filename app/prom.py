"""F26 Prometheus text exposition (0.0.4) / OpenMetrics 1.0 renderer + format lint.

Format follows the upstream specs vendored in `ref/upstream/prometheus/`
(prometheus/docs@cc0672badce8 `instrumenting/exposition_formats.md`,
`specs/om/open_metrics_spec.md`; Apache-2.0):

  text 0.0.4   Content-Type `text/plain; version=0.0.4; charset=utf-8`
               `# HELP <name> <escaped>` / `# TYPE <name> <type>` once per family,
               before its samples; counters are exposed as `<base>_total`.
  OpenMetrics  Content-Type `application/openmetrics-text; version=1.0.0; charset=utf-8`
               TYPE/HELP on the family name (`<base>`), samples `<base>_total`,
               mandatory `# EOF` terminator.

`lint()` is a dependency-free subset of `promtool check metrics` (name/label
grammar, HELP/TYPE placement, family contiguity, duplicate series, float values,
counter `_total` suffix). The same file is copied into NOCP, AI-Infra and NICo;
keep the copies identical.
"""

from __future__ import annotations

import math
import re
from typing import Iterable, Optional

CONTENT_TYPE_TEXT = "text/plain; version=0.0.4; charset=utf-8"
CONTENT_TYPE_OM = "application/openmetrics-text; version=1.0.0; charset=utf-8"

NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
TYPES = ("counter", "gauge", "histogram", "summary", "untyped")


def _esc_help(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace("\n", "\\n")


def _esc_label(s) -> str:
    return str(s).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    v = float(v)
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(v)


def sanitize(name: str) -> str:
    """Turn an arbitrary key into a valid metric/label name."""
    s = re.sub(r"[^a-zA-Z0-9_]", "_", str(name))
    return s if s and not s[0].isdigit() else "_" + s


class Family:
    """One metric family. Counters take the base name (no `_total`)."""

    def __init__(self, name: str, mtype: str, help_: str):
        if mtype not in TYPES:
            raise ValueError(f"bad metric type {mtype}")
        if mtype == "counter" and name.endswith("_total"):
            name = name[: -len("_total")]
        self.name, self.type, self.help = name, mtype, help_
        self.samples: list[tuple[dict, float]] = []

    def add(self, value, **labels) -> "Family":
        if value is None:
            return self
        self.samples.append(({k: v for k, v in labels.items() if v is not None}, value))
        return self


class Registry:
    def __init__(self) -> None:
        self.families: dict[str, Family] = {}

    def family(self, name: str, mtype: str, help_: str) -> Family:
        base = name[: -len("_total")] if mtype == "counter" and name.endswith("_total") else name
        f = self.families.get(base)
        if f is None:
            f = self.families[base] = Family(base, mtype, help_)
        return f

    def gauge(self, name: str, help_: str) -> Family:
        return self.family(name, "gauge", help_)

    def counter(self, name: str, help_: str) -> Family:
        return self.family(name, "counter", help_)

    def render(self, openmetrics: bool = False) -> str:
        return render(self.families.values(), openmetrics)


def render(families: Iterable[Family], openmetrics: bool = False) -> str:
    out: list[str] = []
    for f in families:
        expo = f.name + "_total" if f.type == "counter" else f.name
        head = f.name if openmetrics else expo
        out.append(f"# HELP {head} {_esc_help(f.help)}")
        out.append(f"# TYPE {head} {f.type}")
        seen = set()
        for labels, value in f.samples:
            key = tuple(sorted(labels.items()))
            if key in seen:                     # never emit a duplicate series
                continue
            seen.add(key)
            lab = ",".join(f'{k}="{_esc_label(v)}"' for k, v in labels.items())
            out.append(f"{expo}{{{lab}}} {_fmt(value)}" if lab else f"{expo} {_fmt(value)}")
    if openmetrics:
        out.append("# EOF")
    return "\n".join(out) + "\n"


def wants_openmetrics(accept: Optional[str]) -> bool:
    return bool(accept) and "application/openmetrics-text" in accept


# ── lint: promtool `check metrics` subset ──────────────────────────────────
_SAMPLE_RE = re.compile(r"^(?P<name>[^\s{]+)(?:\{(?P<labels>.*)\})?\s+(?P<value>\S+)(?:\s+(?P<ts>-?\d+))?$")
_LABEL_PAIR_RE = re.compile(r'\s*([^=\s,]+)\s*=\s*"((?:[^"\\]|\\.)*)"\s*(,|$)')
# promlint (client_golang testutil/promlint) unit rule subset
_NON_BASE_UNITS = {"minutes": "seconds", "hours": "seconds", "days": "seconds",
                   "weeks": "seconds", "milliseconds": "seconds", "microseconds": "seconds",
                   "nanoseconds": "seconds", "kilobytes": "bytes", "megabytes": "bytes",
                   "gigabytes": "bytes", "kilograms": "grams", "fahrenheit": "celsius"}
_CAMEL_RE = re.compile(r"[a-z][A-Z]")
_SUFFIXES = ("_total", "_created", "_bucket", "_count", "_sum", "_info", "_gsum", "_gcount")


def _family_of(name: str, types: dict) -> str:
    if name in types:
        return name
    for s in _SUFFIXES:
        if name.endswith(s) and name[: -len(s)] in types:
            return name[: -len(s)]
    return name


def lint(text: str, openmetrics: bool = False) -> list[str]:
    """Return a list of problems (empty = passes)."""
    problems: list[str] = []
    types: dict[str, str] = {}
    helps: set[str] = set()
    finished: set[str] = set()
    current: Optional[str] = None
    series: set[tuple] = set()
    sampled: set[str] = set()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    else:
        problems.append("exposition must end with a newline")
    if openmetrics and (not lines or lines[-1] != "# EOF"):
        problems.append("OpenMetrics exposition must end with '# EOF'")
    for no, line in enumerate(lines, 1):
        if line == "" or (openmetrics and line == "# EOF" and no == len(lines)):
            continue
        if line.startswith("#"):
            parts = line.split(None, 3)
            if len(parts) >= 2 and parts[1] in ("HELP", "TYPE"):
                if len(parts) < 3 or not NAME_RE.match(parts[2]):
                    problems.append(f"line {no}: invalid metric name in {parts[1]}")
                    continue
                name = parts[2]
                if parts[1] == "TYPE":
                    t = parts[3].strip() if len(parts) > 3 else ""
                    if t not in TYPES:
                        problems.append(f"line {no}: unknown type {t!r} for {name}")
                    if name in types:
                        problems.append(f"line {no}: second TYPE line for {name}")
                    if name in sampled:
                        problems.append(f"line {no}: TYPE for {name} after its samples")
                    types[name] = t
                    if t == "counter" and not openmetrics and not name.endswith("_total"):
                        problems.append(f"{name}: counter metrics should have \"_total\" suffix")
                    if t != "counter" and name.endswith("_total"):
                        problems.append(f"{name}: non-counter metrics should not have \"_total\" suffix")
                    for unit in name.lower().split("_"):
                        if unit in _NON_BASE_UNITS:
                            problems.append(f"{name}: use base unit \"{_NON_BASE_UNITS[unit]}\""
                                            f" instead of \"{unit}\"")
                else:
                    if name in helps:
                        problems.append(f"line {no}: second HELP line for {name}")
                    helps.add(name)
                if current and current != name:
                    finished.add(current)
                current = name
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            problems.append(f"line {no}: unparseable sample")
            continue
        name = m.group("name")
        if not NAME_RE.match(name):
            problems.append(f"line {no}: invalid metric name {name!r}")
            continue
        if _CAMEL_RE.search(name):
            problems.append(f"{name}: metric names should be written in 'snake_case' not 'camelCase'")
        fam = _family_of(name, types)
        if fam in finished:
            problems.append(f"line {no}: samples of {fam} are not contiguous")
        if current and current != fam:
            finished.add(current)
        current = fam
        sampled.add(fam)
        if fam not in helps:
            problems.append(f"{fam}: no help text")
        labels = []
        raw = m.group("labels")
        if raw:
            pos = 0
            while pos < len(raw):
                lm = _LABEL_PAIR_RE.match(raw, pos)
                if not lm:
                    problems.append(f"line {no}: bad label set")
                    break
                if not LABEL_RE.match(lm.group(1)) or lm.group(1).startswith("__"):
                    problems.append(f"line {no}: invalid label name {lm.group(1)!r}")
                elif _CAMEL_RE.search(lm.group(1)):
                    problems.append(f"{name}: label names should be written in 'snake_case' "
                                    "not 'camelCase'")
                labels.append((lm.group(1), lm.group(2)))
                pos = lm.end()
            if len({k for k, _ in labels}) != len(labels):
                problems.append(f"line {no}: duplicate label name")
        try:
            float(m.group("value").replace("Inf", "inf"))
        except ValueError:
            problems.append(f"line {no}: invalid value {m.group('value')!r}")
        key = (name, tuple(sorted(labels)))
        if key in series:
            problems.append(f"line {no}: duplicate series {name}{dict(labels)}")
        series.add(key)
    return problems


def parse(text: str) -> dict[str, list[tuple[dict, float]]]:
    """Tiny parser for tests: {sample_name: [(labels, value), ...]}."""
    out: dict[str, list] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        labels = {a: b for a, b, _ in _LABEL_PAIR_RE.findall(m.group("labels") or "")}
        out.setdefault(m.group("name"), []).append(
            (labels, float(m.group("value").replace("Inf", "inf"))))
    return out
