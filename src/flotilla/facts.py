"""Read-only host facts for the host map, plus the SSH mesh check."""

from __future__ import annotations

import json
import os
import shlex
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
echo "gh=$(gh auth status >/dev/null 2>&1 && echo ok || echo no)"
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
                             fleet.hosts.values()))


def mesh(fleet: Fleet) -> dict[str, Any]:
    """From every host, ``ssh <other> hostname`` by short name.

    Returns ``{"hosts": [...], "cells": {src: {dst: "ok"|reason}}}``.
    macOS cannot reach its own NetBird address, so self-checks are skipped.
    """
    names = list(fleet.hosts)
    probe = "; ".join(
        f"r=$(ssh -n -o BatchMode=yes -o StrictHostKeyChecking=yes "
        f"-o ConnectTimeout=6 {shlex.quote(n)} 'echo ok' 2>&1 | tail -1); "
        f"echo {shlex.quote(n)}=$r" for n in names)
    probe += ("; r=$(ssh -n -o BatchMode=yes -o StrictHostKeyChecking=yes "
              "-T git@github.com 2>&1 | head -1); "
              "case $r in Hi*) echo github=ok;; *) echo github=$r;; esac")

    def one(h: Host) -> tuple[str, dict[str, str]]:
        code, out = remote.run(fleet, h, probe, timeout=120)
        cells = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k in names or k == "github":
                    cells[k] = "ok" if v.strip() == "ok" else v.strip()[-80:]
        cells.pop(h.name, None)
        return h.name, cells

    with ThreadPoolExecutor(max_workers=8) as pool:
        result = dict(pool.map(one, fleet.hosts.values()))
    return {"hosts": names, "cells": result}


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
