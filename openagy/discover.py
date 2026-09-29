"""Discover a running Antigravity language server: port + CSRF token.

The Electron app spawns `language_server.exe --standalone` on a random HTTPS
port with a per-session CSRF token. The hub UI is served by the language server
itself at `https://127.0.0.1:<port>/` and the index page embeds the token in
``window.__APP_CONFIG__``.

Discovery procedure:
  1. psutil: find a `language_server` process whose cmdline has --standalone.
  2. Collect its 127.0.0.1 TCP listen ports.
  3. HTTPS-GET ``/`` on each port (self-signed cert, so no verification) until
     one responds with an ``__APP_CONFIG__`` containing a csrfToken.
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

_APP_CONFIG_RE = re.compile(r"window\.__APP_CONFIG__\s*=\s*(\{.*?\});", re.DOTALL)

# (port, csrf, app_version) cache so we do not re-scan on every RPC.
_cache: dict[str, object] = {"ts": 0.0, "target": None}
_CACHE_TTL = 30.0


class DiscoveryError(RuntimeError):
    """Raised when no live Antigravity language server can be found."""


@dataclass(frozen=True)
class Target:
    port: int
    csrf_token: str
    app_version: str

    @property
    def base_url(self) -> str:
        return f"https://127.0.0.1:{self.port}"


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _parse_app_config(html: str) -> dict | None:
    m = _APP_CONFIG_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _probe(port: int, timeout: float = 3.0) -> Target | None:
    url = f"https://127.0.0.1:{port}/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "openagy"})
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=timeout) as r:
            html = r.read(65536).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
    cfg = _parse_app_config(html)
    if not cfg or "csrfToken" not in cfg:
        return None
    return Target(port=port, csrf_token=cfg["csrfToken"], app_version=str(cfg.get("appVersion", "")))


def _find_via_psutil() -> list[int]:
    import psutil

    ports: list[int] = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if "language_server" not in name:
                continue
            cmdline = proc.info.get("cmdline") or []
            if not any("--standalone" in a for a in cmdline):
                continue
            for conn in proc.net_connections(kind="tcp"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr:
                    if conn.laddr.ip in ("127.0.0.1", "0.0.0.0", "::1", "::"):
                        ports.append(conn.laddr.port)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return sorted(set(ports))


def _find_via_powershell() -> list[int]:
    """Fallback discovery when psutil is unavailable (Windows-only; macOS and
    Linux rely on psutil, which is a hard dependency there)."""
    import subprocess
    import sys as _sys

    if _sys.platform != "win32":
        return []
    cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='language_server.exe'\" | "
        "Where-Object {$_.CommandLine -match '--standalone'} | "
        "ForEach-Object { Get-NetTCPConnection -OwningProcess $_.ProcessId -State Listen -ErrorAction SilentlyContinue } | "
        "Where-Object {$_.LocalAddress -match '127.0.0.1|0.0.0.0|::'} | "
        "Select-Object -ExpandProperty LocalPort"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    ports = []
    for line in out.split():
        try:
            ports.append(int(line))
        except ValueError:
            continue
    return sorted(set(ports))


def discover(force: bool = False) -> Target:
    """Return the live language server target, using a short-lived cache."""
    now = time.time()
    cached = _cache.get("target")
    if cached and not force and now - _cache["ts"] < _CACHE_TTL:  # type: ignore[operator]
        return cached  # type: ignore[return-value]

    try:
        ports = _find_via_psutil()
    except ImportError:
        ports = _find_via_powershell()

    for port in ports:
        target = _probe(port)
        if target:
            _cache["target"] = target
            _cache["ts"] = now
            return target

    raise DiscoveryError(
        "No running Antigravity language server found. "
        "Start the Antigravity app first (it spawns language_server.exe)."
    )


def invalidate_cache() -> None:
    _cache["target"] = None
    _cache["ts"] = 0.0
