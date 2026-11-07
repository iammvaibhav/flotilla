"""Command line: ``flotilla serve|scan-keys|bundles|show|override|skip|diff|plan|apply|exec``.

``plan``, ``apply`` and ``diff`` take a component (installs and logins) or a
bundle (config files); run ``flotilla bundles`` for the bundle names.
"""

from __future__ import annotations

import argparse
import json
import sys

import yaml

from . import bundles, remote, runner
from .secrets import Resolver
from .state import commit, load_fleet


def _hosts(fleet, names: list[str]):
    if not names:
        return fleet.managed()
    out = []
    for n in names:
        if n in fleet.hosts:
            if not fleet.hosts[n].managed:
                raise SystemExit(f"{n} is not managed by flotilla")
            out.append(fleet.hosts[n])
        else:  # group name
            members = fleet.groups().get(n)
            if not members:
                raise SystemExit(f"unknown host or group: {n}")
            out += [fleet.hosts[m] for m in members]
    return out


def _target(name: str):
    """A component name, or the bundle of that name."""
    if name in runner.COMPONENTS:
        return None
    try:
        return bundles.get_bundle(name)
    except bundles.BundleError as exc:
        raise SystemExit(f"{exc} (components: "
                         f"{', '.join(runner.COMPONENTS)})") from exc


def run(target: str, hosts: list[str], dry: bool) -> int:
    """Plan or apply a component or bundle and print a change summary."""
    fleet = load_fleet()
    bundle = _target(target)
    if bundle is not None and not bundle.deployable:
        raise SystemExit(f"{target} is not copied to hosts")
    items = (bundles.execute(bundle, fleet, _hosts(fleet, hosts), dry)
             if bundle else
             runner.execute(target, fleet, _hosts(fleet, hosts), dry))
    code = 1
    for item in items:
        if isinstance(item, str):
            print(item, flush=True)
        elif item[0] == "plan":
            plan = item[1].get("plan", item[1]) if bundle else item[1]
            if not plan:
                print("no changes")
            for host, ops in sorted(plan.items()):
                print(f"{host}: {len(ops)} change(s)")
                for op in ops:
                    print(f"    {op}")
        else:
            code = item[1]
    return code


def _layer(a) -> str:
    if bool(a.group) == bool(a.host):
        raise SystemExit("give exactly one of --group or --host")
    return f"group:{a.group}" if a.group else f"host:{a.host}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="flotilla")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8790)
    sub.add_parser("scan-keys", help="pin SSH host keys into the state")
    sub.add_parser("bundles", help="list the bundles")
    for name in ("diff", "plan", "apply"):
        c = sub.add_parser(name, help=f"{name} a component or a bundle")
        c.add_argument("target", help="component or bundle name")
        c.add_argument("hosts", nargs="*", help="hosts or groups")
    sh = sub.add_parser("show", help="a bundle file as one host gets it")
    sh.add_argument("bundle")
    sh.add_argument("file", help="file name on the host, e.g. config.yml")
    sh.add_argument("--host", required=True)
    sh.add_argument("--explain", action="store_true",
                    help="list every key and the layer that set it")
    o = sub.add_parser("override", help="set or clear one key for a group "
                       "or host")
    o.add_argument("bundle")
    o.add_argument("file", help="state file name, e.g. config.yml")
    o.add_argument("key", help="dotted key, e.g. modelRoles.default")
    o.add_argument("value", nargs="?", help="YAML value")
    o.add_argument("--op", choices=["+", "-", "!"], default="",
                   help="+ add to a list, - remove from a list, ! replace")
    o.add_argument("--clear", action="store_true")
    o.add_argument("--group")
    o.add_argument("--host")
    k = sub.add_parser("skip", help="do not place an item (e.g. a skill) on "
                       "a group or host")
    k.add_argument("bundle")
    k.add_argument("item")
    k.add_argument("--group")
    k.add_argument("--host")
    k.add_argument("--delete", action="store_true",
                   help="delete it there instead of leaving it alone")
    k.add_argument("--clear", action="store_true", help="undo skip/delete")
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
    if a.cmd == "bundles":
        for b in [bundles.settings_bundle(), *bundles.load_bundles().values()]:
            print(f"{b.name:14s} {b.dest or '(not copied to hosts)':28s} "
                  f"{b.description}")
        return 0
    if a.cmd == "diff":
        bundle = _target(a.target)
        resolver = Resolver()
        for h in _hosts(fleet, a.hosts):
            found = (bundles.diffs(bundle, fleet, h, resolver) if bundle
                     else runner.file_diffs(a.target, fleet, h, resolver))
            for d in found:
                print(d["diff"])
        return 0
    if a.cmd in ("plan", "apply"):
        return run(a.target, a.hosts, dry=a.cmd == "plan")
    if a.cmd == "show":
        bundle = bundles.get_bundle(a.bundle)
        comp = bundles.compile_bundle(bundle, fleet.host(a.host), fleet)
        f = comp.files.get(a.file)
        if f is None:
            raise SystemExit(f"{a.host} gets no {a.file} from {a.bundle} "
                             f"(files: {', '.join(sorted(comp.files))})")
        if not a.explain:
            sys.stdout.write(f.text)
            return 0
        if f.explain is None:
            print(f"{a.file}: whole file from {f.sources[-1]}")
            return 0
        for row in f.explain:
            value = json.dumps(row["value"], ensure_ascii=False)
            print(f"{row['key']:50s} {', '.join(row['sources']):24s} "
                  f"{value[:100]}")
        return 0
    if a.cmd == "override":
        bundle = bundles.get_bundle(a.bundle)
        layer = _layer(a)
        path = bundle.layer_dir(layer) / a.file
        if a.clear:
            def change():
                if not bundles.clear_override(bundle, layer, a.file, a.key):
                    raise SystemExit(f"{a.key} is not set in {layer}")
            message = f"{bundle.name}: clear {a.key} for {layer}"
        else:
            if a.value is None:
                raise SystemExit("give a value, or --clear")
            def change():
                bundles.set_override(bundle, layer, a.file, a.key,
                                     yaml.safe_load(a.value), a.op)
            message = f"{bundle.name}: {a.key}{a.op} for {layer}"
        problems = bundles.guarded_edit(bundle, fleet, path, change)
        if problems:
            raise SystemExit("not saved:\n" + "\n".join(problems))
        commit(message)
        return 0
    if a.cmd == "skip":
        bundle = bundles.get_bundle(a.bundle)
        layer = _layer(a)
        kind = None if a.clear else "delete" if a.delete else "skip"
        bundles.set_item_state(bundle, layer, a.item, kind)
        commit(f"{bundle.name}: {kind or 'place'} {a.item} on {layer}")
        return 0
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
