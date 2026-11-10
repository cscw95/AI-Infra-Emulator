"""Emulator security controls (identical copy in AI-Infra and NICo emulators).

Everything is opt-in so the default dev setup and the test suites behave as
before; set the env vars below to harden a shared/demo deployment.

  EMU_API_KEY            when set, POST/PUT/PATCH/DELETE need
                         `X-API-Key: <key>` or `Authorization: Bearer <key>`.
  EMU_REDFISH_USER/PASS  when both set, every /redfish/v1/* request except the
                         service root needs HTTP Basic (like a real BMC).
                         Basic with these credentials also satisfies EMU_API_KEY
                         on Redfish paths.
  EMU_CORS_ORIGINS       comma list of allowed browser origins (default: the
                         localhost NeoCloud ports).
  EMU_OUTBOUND_ALLOWLIST comma list of hosts / CIDRs / ".suffix" domains this
                         service may call (EventService push, AI_INFRA_URL).
                         Default: localhost,127.0.0.1,::1
  EMU_MAX_BODY_BYTES     request body cap (default 1 MiB).
  EMU_MAX_PAGE           cap on `limit`/`top`/`pageSize` query params (default 10000).
"""
import base64
import hmac
import ipaddress
import os
import socket
from typing import Iterable, List, Optional
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
DEFAULT_CORS = ["http://127.0.0.1:8000", "http://127.0.0.1:8090",
                "http://127.0.0.1:9000", "http://localhost:8000",
                "http://localhost:8090", "http://localhost:9000"]
CORS_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_HEADERS = ["Content-Type", "Authorization", "X-API-Key", "X-Tenant-Id",
                "X-Operator", "X-Request-Id", "Last-Event-ID", "If-Match"]
PAGE_PARAMS = ("limit", "top", "$top", "pageSize")
MAX_RANGES = 16
# Always refused, even when allowlisted: cloud metadata endpoints.
METADATA_IPS = {ipaddress.ip_address(a) for a in
                ("169.254.169.254", "169.254.170.2", "100.100.100.200",
                 "fd00:ec2::254", "192.0.0.192")}


def _env_list(name: str, default: Iterable[str]) -> List[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def cors_origins() -> List[str]:
    return _env_list("EMU_CORS_ORIGINS", DEFAULT_CORS)


def add_cors(app, default_origins: Optional[List[str]] = None):
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_env_list("EMU_CORS_ORIGINS", default_origins or DEFAULT_CORS),
        allow_methods=CORS_METHODS, allow_headers=CORS_HEADERS,
        allow_credentials=False)


# ── outbound URL validation (SSRF) ────────────────────────────────────
class UnsafeURL(ValueError):
    pass


def _allow_entries():
    return _env_list("EMU_OUTBOUND_ALLOWLIST", ["localhost", "127.0.0.1", "::1"])


def _host_allowed(host: str, ips) -> bool:
    for ent in _allow_entries():
        ent_l = ent.lower()
        if ent_l == "*":
            return True
        if ent_l.startswith(".") and (host == ent_l[1:] or host.endswith(ent_l)):
            return True
        if host == ent_l:
            return True
        try:
            net = ipaddress.ip_network(ent_l, strict=False)
        except ValueError:
            continue
        if ips and all(ip in net for ip in ips):
            return True
    return False


def _resolve(host: str):
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeURL(f"cannot resolve host {host!r}") from e
    return sorted({ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos},
                  key=str)


def _forbidden_ip(ip) -> Optional[str]:
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip in METADATA_IPS:
        return "cloud metadata address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast or ip.is_unspecified or (ip.is_reserved and not ip.is_loopback):
        return "non-routable address"
    return None


def check_outbound_url(url: str) -> str:
    """Raise UnsafeURL unless url is http(s) to an allowlisted, non-metadata,
    non-link-local host. Re-run at send time (DNS rebinding)."""
    if not isinstance(url, str) or not url:
        raise UnsafeURL("URL is required")
    try:
        parts = urlsplit(url)
        port = parts.port          # raises on a bad port
    except ValueError as e:
        raise UnsafeURL(f"malformed URL: {e}") from e
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("only http/https URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeURL("credentials in URL are not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeURL("URL has no host")
    if port is not None and not 0 < port < 65536:
        raise UnsafeURL("bad port")
    ips = _resolve(host)
    for ip in ips:
        why = _forbidden_ip(ip)
        if why:
            raise UnsafeURL(f"{host} resolves to a {why}")
    if not _host_allowed(host, ips):
        raise UnsafeURL(f"host {host!r} is not in EMU_OUTBOUND_ALLOWLIST")
    return url


# ── auth + request limits ─────────────────────────────────────────────
def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _basic_ok(auth: str, user: str, pw: str) -> bool:
    if not auth.lower().startswith("basic "):
        return False
    try:
        u, _, p = base64.b64decode(auth[6:].strip()).decode().partition(":")
    except Exception:
        return False
    return _eq(u, user) & _eq(p, pw)


def _key_ok(request, key: str) -> bool:
    got = request.headers.get("x-api-key")
    if got is None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
    return got is not None and _eq(got, key)


def _deny(status: int, msg: str, basic: bool = False):
    hdr = {"WWW-Authenticate": 'Basic realm="Redfish"'} if basic else {}
    return JSONResponse({"detail": msg}, status_code=status, headers=hdr)


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        method = request.method.upper()
        if method == "OPTIONS":                       # CORS preflight
            return await call_next(request)
        max_body = int(os.environ.get("EMU_MAX_BODY_BYTES", "1048576"))
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > max_body:
                    return _deny(413, f"request body exceeds {max_body} bytes")
            except ValueError:
                return _deny(400, "bad Content-Length")
        rng = request.headers.get("range")
        if rng and rng.count(",") >= MAX_RANGES:   # CVE-2025-62727 (FileResponse O(n^2))
            return _deny(416, "too many byte ranges")
        max_page = int(os.environ.get("EMU_MAX_PAGE", "10000"))
        for p in PAGE_PARAMS:
            v = request.query_params.get(p)
            if v is not None:
                try:
                    iv = int(v)
                except ValueError:
                    continue                          # let FastAPI return 422
                if iv < 0 or iv > max_page:
                    return _deny(422, f"{p} must be 0..{max_page}")
        off = request.query_params.get("offset")
        if off is not None and off.lstrip("-").isdigit() and int(off) < 0:
            return _deny(422, "offset must be >= 0")

        # scope["path"], not request.url.path: Starlette < 1.0.1 lets a crafted
        # Host header rewrite request.url.path (CVE-2026-48710)
        path = request.scope.get("path", "")
        auth = request.headers.get("authorization", "")
        rf_user = os.environ.get("EMU_REDFISH_USER")
        rf_pass = os.environ.get("EMU_REDFISH_PASSWORD")
        is_redfish = path.startswith("/redfish/v1/")
        basic_valid = False
        if is_redfish and rf_user and rf_pass:
            basic_valid = _basic_ok(auth, rf_user, rf_pass)
            key = os.environ.get("EMU_API_KEY")
            if not basic_valid and not (key and _key_ok(request, key)):
                return _deny(401, "Redfish requires HTTP Basic authentication",
                             basic=True)
        key = os.environ.get("EMU_API_KEY")
        if key and method in MUTATING and not basic_valid and not _key_ok(request, key):
            return _deny(401, "missing or invalid API key "
                              "(X-API-Key or Authorization: Bearer)")
        return await call_next(request)


def outbound_auth_headers() -> dict:
    """Headers for calls to a peer emulator that enforces EMU_API_KEY."""
    key = os.environ.get("EMU_PEER_API_KEY") or os.environ.get("AI_INFRA_API_KEY")
    return {"X-API-Key": key} if key else {}
