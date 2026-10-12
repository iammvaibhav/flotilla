"""Command line: ``flotilla serve|scan-keys|diff|plan|apply|exec``."""

from __future__ import annotations

import argparse
import json
import sys

from . import remote, runner
from .secrets import Resolver
from .state import load_fleet


def _hosts(fleet, names: list[str]):
    if not names:
        return list(fleet.hosts.values())
    out = []
    for n in names:
        if n in fleet.hosts:
            out.append(fleet.hosts[n])
        else:  # group name
            members = fleet.groups().get(n)
            if not members:
                raise SystemExit(f"unknown host or group: {n}")
            out += [fleet.hosts[m] for m in members]
    return out


def run_component(component: str, hosts: list[str], dry: bool) -> int:
    """Run pyinfra for ``component`` and print a change summary."""
    fleet = load_fleet()
    code = 1
    for item in runner.execute(component, fleet, _hosts(fleet, hosts), dry):
        if isinstance(item, str):
            print(item, flush=True)
        elif item[0] == "plan":
            if not item[1]:
                print("no changes")
            for host, ops in sorted(item[1].items()):
                print(f"{host}: {len(ops)} change(s)")
                for op in ops:
                    print(f"    {op}")
        else:
            code = item[1]
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="flotilla")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)
    sub.add_parser("scan-keys", help="pin SSH host keys into the state")
    for name in ("diff", "plan", "apply"):
        c = sub.add_parser(name)
        c.add_argument("component", choices=sorted(runner.COMPONENTS))
        c.add_argument("hosts", nargs="*", help="hosts or groups")
    e = sub.add_parser("exec", help="run a command on hosts")
    e.add_argument("command")
    e.add_argument("hosts", nargs="*")
    a = p.parse_args(argv)

    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("flotilla.web:app", host=a.host, port=a.port)
        return 0
    fleet = load_fleet()
    if a.cmd == "scan-keys":
        print(remote.scan_keys(fleet))
        return 0
    if a.cmd == "diff":
        resolver = Resolver()
        for h in _hosts(fleet, a.hosts):
            for d in runner.file_diffs(a.component, fleet, h, resolver):
                print(d["diff"] or f"(new) {d['path']}")
        return 0
    if a.cmd in ("plan", "apply"):
        return run_component(a.component, a.hosts, dry=a.cmd == "plan")
    if a.cmd == "exec":
        rc = 0
        for h in _hosts(fleet, a.hosts):
            code, out = remote.run(fleet, h, a.command)
            print(json.dumps({"host": h.name, "rc": code}))
            print(out.rstrip())
            rc |= code
        return rc
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
