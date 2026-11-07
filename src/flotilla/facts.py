"""Read-only host facts for the host map and the model catalog."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import remote
from .state import Fleet, Host

STATUS_SCRIPT = r"""
export PATH="$HOME/.local/bin:$PATH"
v() { "$@" 2>/dev/null | head -1 | tr -d '\r'; }
echo "hostname=$(hostname -s)"
echo "uptime=$(uptime | sed -E 's/.*up ([^,]*),.*/\1/')"
echo "load=$(uptime | sed -E 's/.*load averages?: //')"
echo "disk=$(df -P / | awk 'NR==2{print $5}')"
echo "omp=$(v omp --version)"
echo "paseo=$(v paseo --version)"
echo "paseo_daemon=$(paseo daemon status 2>/dev/null | awk -F': ' '/^localDaemon/{print $2}')"
echo "uv=$(v uv --version | awk '{print $2}')"
echo "pnpm=$(v pnpm --version)"
echo "node=$(v node --version)"
# macOS keeps the gh token in the login keychain, which SSH sessions
# cannot open; a configured user there means "logged in (keychain)".
echo "gh=$(gh auth status >/dev/null 2>&1 && echo ok || { [ "$(uname)" = Darwin ] && gh config get -h github.com user >/dev/null 2>&1 && echo keychain; } || echo no)"
echo "git_email=$(git config --global user.email)"
echo "netbird=$( (netbird status 2>/dev/null || /usr/local/bin/netbird status 2>/dev/null) | awk -F': ' '/^Management/{split($2,a,","); m=a[1]} /^Peers count/{p=$2} END{if (m) print "mgmt " m " · peers " p}')"
"""


def host_status(fleet: Fleet, host: Host) -> dict[str, Any]:
    started = time.time()
    code, out = remote.run(fleet, host, STATUS_SCRIPT, timeout=25)
    facts: dict[str, Any] = {"name": host.name, "ok": code == 0,
                             "ms": int((time.time() - started) * 1000)}
    for line in out.splitlines():
        if "=" in line and not line.startswith(" "):
            key, val = line.split("=", 1)
            if key.isidentifier():
                facts[key] = val.strip()
    if code != 0:
        facts["error"] = out.strip()[-300:]
    return facts


def fleet_status(fleet: Fleet) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda h: host_status(fleet, h),
                             fleet.managed()))


def catalog_host(fleet: Fleet) -> Host:
    return fleet.host(os.environ.get("FLOTILLA_CATALOG_HOST") or fleet.controller)


def models(fleet: Fleet) -> list[dict[str, Any]]:
    """Model catalog from ``omp models --kind all --json``."""
    code, out = remote.run(fleet, catalog_host(fleet),
                           "omp models --kind all --json", timeout=90)
    start = out.find("{")
    if code != 0 or start < 0:
        raise RuntimeError(out.strip()[-300:])
    data = json.loads(out[start:])
    return [{"selector": m["selector"], "kind": m.get("kind"),
             "name": m.get("name"), "thinking": m.get("thinking") or [],
             "context": m.get("contextWindow")} for m in data["models"]]


def settings(fleet: Fleet) -> list[dict[str, Any]]:
    """omp settings keys from ``omp config list --json`` (no values)."""
    code, out = remote.run(fleet, catalog_host(fleet),
                           "omp config list --json", timeout=60)
    start = out.find("{")
    if code != 0 or start < 0:
        raise RuntimeError(out.strip()[-300:])
    data = json.loads(out[start:])
    return [{"key": k, "type": v.get("type"),
             "description": v.get("description", "")}
            for k, v in data.items()]
