"""Paseo: keep the Paseo deploy inventory in step with the fleet.

Paseo is built once per platform on builder hosts and shipped to targets by
the Paseo repository's own deploy script. flotilla does not build or install
Paseo. It only adds missing hosts to the Paseo inventory (a JSON file on the
inventory host) and runs the deploy for those hosts only.

Hosts are matched by name, then by address (the two inventories can use
different names for the same machine). Of an existing entry, flotilla only
updates ``address`` and ``dns`` when the machine moved; the rest belongs to
the Paseo deploy.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Iterator

from . import remote
from .state import Fleet, Host, host_vars

DEFAULTS = {
    "enabled": True,
    "inventory_host": None,             # flotilla host that runs the deploy
    "inventory": "~/.paseo/fleet-hosts.json",
    "repo": "~/paseo",
}


def settings(fleet: Fleet) -> dict[str, Any]:
    v = (host_vars(fleet.host(fleet.controller)).get("paseo") or {})
    out = {**DEFAULTS, **{k: v[k] for k in DEFAULTS if k in v}}
    if not out["inventory_host"]:
        raise RuntimeError("set paseo.inventory_host in vars/all.yaml")
    return out


def read_inventory(fleet: Fleet, cfg: dict[str, Any]) -> dict[str, Any]:
    inv_host = fleet.host(cfg["inventory_host"])
    code, out = remote.run(fleet, inv_host,
                           f"cat {cfg['inventory'].replace('~', '$HOME', 1)}")
    if code != 0:
        raise RuntimeError(f"cannot read the Paseo inventory: {out[-200:]}")
    return json.loads(out[out.find("{"):])


def entry_for(host: Host, inventory: dict[str, Any]) -> dict[str, Any]:
    """The inventory entry a new target host gets."""
    builders = [h for h in inventory["hosts"] if h.get("role") == "builder"
                and h.get("platform") == host.platform]
    if not builders:
        raise RuntimeError(f"no Paseo builder for {host.platform}")
    dns = dns_of(host)
    entry = {"name": host.name, "ssh": host.name, "address": host.ip,
             "platform": host.platform, "role": "target",
             "buildFrom": builders[0]["name"]}
    if dns:
        entry["dns"] = dns
    return entry


def find(host: Host, inventory: dict[str, Any]) -> dict[str, Any] | None:
    for e in inventory["hosts"]:
        if e["name"] == host.name:
            return e
    for e in inventory["hosts"]:
        if e["address"] == host.ip:
            return e
    return None


def dns_of(host: Host) -> str | None:
    """The host's DNS name: a dotted ``address``, else its first dotted name."""
    return next((n for n in [host.address, *host.names]
                 if "." in n and n != host.ip), None)


def plan(fleet: Fleet, hosts: list[Host]) -> dict[str, list[str]]:
    cfg = settings(fleet)
    inventory = read_inventory(fleet, cfg)
    out: dict[str, list[str]] = {}
    for h in hosts:
        if not (host_vars(h).get("paseo") or {}).get("enabled", True):
            continue
        e = find(h, inventory)
        if e is None:
            e = entry_for(h, inventory)
            out[h.name] = [f"add to Paseo inventory (build from "
                           f"{e['buildFrom']})", "deploy Paseo to this host"]
        elif e["address"] != h.ip or (dns_of(h) and
                                      e.get("dns") != dns_of(h)):
            out[h.name] = [f"update Paseo inventory address "
                           f"{e['address']} -> {h.ip}",
                           "deploy Paseo to this host"]
    return out


def execute(fleet: Fleet, hosts: list[Host], dry: bool) -> Iterator[Any]:
    cfg = settings(fleet)
    yield f"Paseo inventory: {cfg['inventory_host']}:{cfg['inventory']}"
    changes = plan(fleet, hosts)
    for name, ops in sorted(changes.items()):
        yield f"[{name}] " + "; ".join(ops)
    if dry or not changes:
        if dry:
            yield ("plan", changes)
        yield ("exit", 0)
        return
    inventory = read_inventory(fleet, cfg)
    names = []
    for n in sorted(changes):
        host = fleet.host(n)
        e = find(host, inventory)
        if e is None:
            e = entry_for(host, inventory)
            inventory["hosts"].append(e)
        else:
            e["address"] = host.ip
            if dns_of(host):
                e["dns"] = dns_of(host)
        names.append(e["name"])
    inv_host = fleet.host(cfg["inventory_host"])
    path = cfg["inventory"].replace("~", "$HOME", 1)
    body = json.dumps(inventory, indent=2) + "\n"
    script = (f"cp {path} {path}.bak && "
              f"printf %s {shlex.quote(body)} > {path}.new && "
              f"mv {path}.new {path}")
    code, out = remote.run(fleet, inv_host, script)
    if code != 0:
        yield f"could not write the inventory: {out[-300:]}"
        yield ("exit", code)
        return
    yield f"inventory updated for {', '.join(names)}; starting the Paseo deploy"
    repo = cfg["repo"].replace("~", "$HOME", 1)
    # `--hosts` restarts only the named hosts; their builders only build.
    code, out = remote.run(
        fleet, inv_host,
        f"cd {repo} && ./scripts/deploy.sh --hosts "
        f"{shlex.quote(','.join(names))}",
        timeout=120)
    for line in out.splitlines()[-20:]:
        yield line
    yield ("The deploy detaches; follow it on the inventory host in "
           "~/.paseo/deploy-logs/latest.log")
    yield ("exit", code)
