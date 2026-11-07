"""Bundles: files flotilla places on hosts, as a base plus small overrides.

A bundle is one folder in the state directory::

    bundles/<name>/
      bundle.yaml            where the files go and how each one merges
      README.md              optional notes (not copied anywhere)
      base/...               files for every host
      groups/<group>/...     overrides for the hosts in one group
      hosts/<host>/...       overrides for one host

Layers apply in order: base, the host's groups (in the order the host lists
them), the host. The same path in a higher layer wins:

- ``merge: keys`` files (YAML or JSON) merge key by key; an override file
  holds only the keys that differ (see :mod:`flotilla.layers`).
- Other files are replaced whole by the highest layer that has them.
- A file that only one layer has is written exactly as it is.
- ``name.j2`` is a Jinja template rendered per host into ``name``.
- ``name.json.yml`` is YAML (comments allowed) written as ``name.json``.
- ``_layer.yml`` in a layer lists items that layer ``skip``s (not written
  there, left alone on the host) or ``delete``s (removed from the host).

``bundle.yaml``::

    description: omp config, agents, rules
    dest: ~/.omp/agent                 # directory on every host
    unit: file                         # or directory: one folder = one item
    files:                             # first match wins; default whole
      - {match: "*.yml", merge: keys}
      - {match: "config.json", merge: keys,
         own_keys: [agents.providers]} # only these keys of the host file
    owned: [agents, rules]             # extra files there are deleted
    after_apply: paseo daemon reload   # runs on hosts that changed

The settings Flotilla itself uses (``settings/``) have the same shape but no
``dest``: they are never copied to hosts.
"""

from __future__ import annotations

import base64
import copy
import difflib
import fnmatch
import hashlib
import json
import shlex
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import jinja2
import yaml

from . import layers, remote
from .secrets import REF_IN_TEXT, Resolver
from .state import Fleet, Host, dump_file, state_dir

BUNDLE_FILE = "bundle.yaml"
LAYER_FILE = "_layer.yml"
SETTINGS = "settings"
SETTINGS_FILE = "flotilla.yml"
MARK = "@@FLOTILLA@@"


class BundleError(ValueError):
    """A bundle or one of its files is invalid."""


# ---------------------------------------------------------------- model

@dataclass
class Rule:
    match: str
    merge: str = "whole"            # keys | whole
    own_keys: list[str] = field(default_factory=list)


@dataclass
class Bundle:
    name: str
    root: Path
    description: str = ""
    dest: str | None = None
    unit: str = "file"              # file | directory
    rules: list[Rule] = field(default_factory=list)
    owned: list[str] = field(default_factory=list)
    after_apply: str | None = None

    @property
    def deployable(self) -> bool:
        return bool(self.dest)

    def rule(self, out_rel: str) -> Rule:
        for r in self.rules:
            if fnmatch.fnmatch(out_rel, r.match):
                return r
        return Rule(match="*")

    def layer_ids(self, host: Host) -> list[str]:
        return ["base", *[f"group:{g}" for g in host.groups],
                f"host:{host.name}"]

    def layer_dir(self, layer_id: str) -> Path:
        if layer_id == "base":
            return self.root / "base"
        kind, _, name = layer_id.partition(":")
        if kind not in ("group", "host") or not name or "/" in name:
            raise BundleError(f"bad layer {layer_id!r}")
        return self.root / f"{kind}s" / name

    def existing_layers(self) -> list[str]:
        out = ["base"] if (self.root / "base").is_dir() else []
        for kind in ("groups", "hosts"):
            d = self.root / kind
            if d.is_dir():
                out += [f"{kind[:-1]}:{p.name}" for p in sorted(d.iterdir())
                        if p.is_dir()]
        return out


def _load(name: str, root: Path) -> Bundle:
    spec = yaml.safe_load((root / BUNDLE_FILE).read_text()) or {}
    unit = spec.get("unit", "file")
    if unit not in ("file", "directory"):
        raise BundleError(f"{name}: unit must be file or directory")
    rules = []
    for r in spec.get("files") or []:
        if r.get("merge", "whole") not in ("keys", "whole"):
            raise BundleError(f"{name}: merge must be keys or whole")
        rules.append(Rule(match=r["match"], merge=r.get("merge", "whole"),
                          own_keys=list(r.get("own_keys") or [])))
    return Bundle(name=name, root=root, description=spec.get("description", ""),
                  dest=spec.get("dest"), unit=unit, rules=rules,
                  owned=list(spec.get("owned") or []),
                  after_apply=spec.get("after_apply"))


def load_bundles(root: Path | None = None) -> dict[str, Bundle]:
    """Every bundle under ``bundles/``, by name."""
    base = (root or state_dir()) / "bundles"
    if not base.is_dir():
        return {}
    return {p.name: _load(p.name, p) for p in sorted(base.iterdir())
            if (p / BUNDLE_FILE).is_file()}


def settings_bundle(root: Path | None = None) -> Bundle:
    path = (root or state_dir()) / SETTINGS
    if (path / BUNDLE_FILE).is_file():
        return _load(SETTINGS, path)
    return Bundle(name=SETTINGS, root=path,
                  rules=[Rule(match="*.yml", merge="keys")])


def get_bundle(name: str, root: Path | None = None) -> Bundle:
    if name == SETTINGS:
        return settings_bundle(root)
    found = load_bundles(root).get(name)
    if not found:
        raise BundleError(f"unknown bundle: {name}")
    return found


# ---------------------------------------------------------------- files

def out_name(rel: str) -> str:
    """The host file a state file becomes."""
    if rel.endswith(".j2"):
        return rel[:-3]
    if rel.endswith(".json.yml"):
        return rel[:-4]
    return rel


def parse(rel: str, text: str) -> Any:
    """Parse a state file (``rel`` is its state name)."""
    try:
        if rel.endswith(".json"):
            return json.loads(text) if text.strip() else {}
        return yaml.safe_load(text) or {}
    except (ValueError, yaml.YAMLError) as exc:
        raise BundleError(f"{rel}: {exc}") from exc


def dump(out_rel: str, data: Any) -> str:
    """Serialize merged data for the host file ``out_rel``."""
    if out_rel.endswith(".json"):
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return dump_file(out_rel, data)


def layer_files(d: Path) -> dict[str, str | bytes]:
    """``{relative path: text}`` in one layer folder (bytes if binary).

    Dotfiles and ``__pycache__`` folders are not part of a layer.
    """
    if not d.is_dir():
        return {}
    out: dict[str, str | bytes] = {}
    for p in sorted(d.rglob("*")):
        rel = p.relative_to(d).as_posix()
        parts = rel.split("/")
        if p.is_file() and rel != LAYER_FILE and "__pycache__" not in parts \
                and not any(part.startswith(".") for part in parts):
            raw = p.read_bytes()
            try:
                out[rel] = raw.decode()
            except UnicodeDecodeError:
                out[rel] = raw
    return out


def layer_manifest(d: Path) -> dict[str, list[str]]:
    path = d / LAYER_FILE
    data = yaml.safe_load(path.read_text()) if path.is_file() else None
    data = data or {}
    return {"skip": list(data.get("skip") or []),
            "delete": list(data.get("delete") or [])}


# ---------------------------------------------------------------- compile

@dataclass
class OutFile:
    rel: str                         # path under dest on the host
    text: str | bytes                # content, aside:// refs unresolved
    sources: list[str]               # layers that supplied it
    data: Any = None                 # parsed content (merge: keys)
    explain: list[dict[str, Any]] | None = None
    own_keys: list[str] = field(default_factory=list)

    @property
    def secret(self) -> bool:
        return isinstance(self.text, str) and bool(REF_IN_TEXT.search(self.text))

    def resolved(self, resolver: Resolver) -> bytes:
        if isinstance(self.text, bytes):
            return self.text
        return resolver.resolve_text(self.text).encode()


@dataclass
class Compiled:
    files: dict[str, OutFile] = field(default_factory=dict)
    delete: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    items: dict[str, str] = field(default_factory=dict)   # item -> layer


def _item(bundle: Bundle, rel: str) -> str:
    return rel.split("/", 1)[0] if bundle.unit == "directory" else rel


def compile_bundle(bundle: Bundle, host: Host,
                   fleet: Fleet | None = None) -> Compiled:
    """What ``bundle`` puts on ``host`` (secrets stay as references)."""
    versions: dict[str, list[tuple[str, dict[str, str]]]] = {}
    state: dict[str, str] = {}
    for layer_id in bundle.layer_ids(host):
        d = bundle.layer_dir(layer_id)
        by_item: dict[str, dict[str, str]] = {}
        for rel, text in layer_files(d).items():
            by_item.setdefault(_item(bundle, rel), {})[rel] = text
        for item, fs in by_item.items():
            if state.get(item) != "present":
                versions[item] = []
            versions[item].append((layer_id, fs))
            state[item] = "present"
        manifest = layer_manifest(d)
        for kind in ("skip", "delete"):
            for item in manifest[kind]:
                state[item] = kind
                versions.pop(item, None)

    out = Compiled()
    for item, kind in state.items():
        if kind == "skip":
            out.skipped.append(item)
        elif kind == "delete":
            out.delete.append(item if bundle.unit == "directory"
                              else out_name(item))
    for item, vs in versions.items():
        if bundle.unit == "directory":
            layer_id, fs = vs[-1]
            out.items[item] = layer_id
            for rel, text in fs.items():
                out.files[rel] = OutFile(rel, text, [layer_id])
        else:
            f = _compile_file(bundle, item, [(lid, fs[item]) for lid, fs in vs],
                              host, fleet)
            out.items[item] = f.sources[-1]
            out.files[f.rel] = f
    out.skipped.sort()
    out.delete.sort()
    return out


def _compile_file(bundle: Bundle, rel: str, vs: list[tuple[str, str]],
                  host: Host, fleet: Fleet | None) -> OutFile:
    target = out_name(rel)
    rule = bundle.rule(target)
    if rel.endswith(".j2"):
        layer_id, text = vs[-1]
        return OutFile(target, _render(text, host, fleet, f"{bundle.name}/"
                                       f"{rel}"), [layer_id])
    if rule.merge != "keys":
        layer_id, text = vs[-1]
        return OutFile(target, text, [layer_id])
    try:
        prov: dict[layers.Path, list[str]] = {}
        first_layer, first_text = vs[0]
        data = parse(rel, first_text)
        if len(vs) == 1 and target == rel and not layers.has_ops(data):
            layers.leaves(data, first_layer, prov)
            return OutFile(target, first_text, [first_layer], data,
                           layers.explain(data, prov), rule.own_keys)
        if layers.has_ops(data):
            data = layers.apply({}, data, first_layer, prov)
        else:
            layers.leaves(data, first_layer, prov)
        for layer_id, text in vs[1:]:
            data = layers.apply(data, parse(rel, text), layer_id, prov)
    except layers.LayerError as exc:
        raise BundleError(f"{bundle.name}/{rel}: {exc}") from exc
    return OutFile(target, dump(target, data), [lid for lid, _ in vs], data,
                   layers.explain(data, prov), rule.own_keys)


def _render(text: str, host: Host, fleet: Fleet | None, label: str) -> str:
    if fleet is None:
        raise BundleError(f"{label}: templates need the fleet")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined,
                             keep_trailing_newline=True, trim_blocks=True,
                             lstrip_blocks=True)
    try:
        return env.from_string(text).render(fleet=fleet, me=host,
                                            hosts=fleet.managed())
    except jinja2.TemplateError as exc:
        raise BundleError(f"{label}: {exc}") from exc


def host_vars(host: Host, root: Path | None = None) -> dict[str, Any]:
    """Flotilla's own settings for one host (``settings/``, layered)."""
    compiled = compile_bundle(settings_bundle(root), host)
    f = compiled.files.get(SETTINGS_FILE)
    return dict(f.data or {}) if f else {}


def check(bundle: Bundle, fleet: Fleet) -> list[str]:
    """Problems compiling ``bundle`` for any managed host (empty: fine)."""
    problems = []
    for host in fleet.managed():
        try:
            compile_bundle(bundle, host, fleet)
        except (BundleError, layers.LayerError) as exc:
            msg = f"{host.name}: {exc}"
            if msg not in problems:
                problems.append(msg)
    return problems


def guarded_edit(bundle: Bundle, fleet: Fleet, path: Path,
                 change: Any) -> list[str]:
    """Run ``change()`` (which edits ``path``); undo it if the bundle no
    longer compiles for every host. Returns the problems (empty: kept)."""
    old = path.read_text() if path.is_file() else None
    change()
    problems = check(bundle, fleet)
    if problems:
        if old is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(old)
    return problems


# ---------------------------------------------------------------- hosts

INSPECT = r"""
import base64, hashlib, json, os, sys
req = json.load(sys.stdin)
dest = os.path.expanduser(req["dest"])
out = {"files": {}, "owned": {}, "keys": {}, "content": {}, "exists": {}}
def mode(p):
    return format(os.stat(p).st_mode & 0o777, "o")
for rel in req["files"]:
    p = os.path.join(dest, rel)
    if os.path.isfile(p):
        data = open(p, "rb").read()
        out["files"][rel] = {"sha": hashlib.sha256(data).hexdigest(),
                             "mode": mode(p)}
        if rel in req["content"]:
            out["content"][rel] = base64.b64encode(data).decode()
    else:
        out["files"][rel] = None
for d in req["owned"]:
    base, names = os.path.join(dest, d), []
    for root, dirs, fs in os.walk(base):
        names += [os.path.relpath(os.path.join(root, f), dest) for f in fs]
    out["owned"][d] = sorted(names)
for rel, paths in req["keys"].items():
    p = os.path.join(dest, rel)
    try:
        data = json.load(open(p))
    except FileNotFoundError:
        data = None
    vals = {}
    for path in paths:
        node, ok = data, True
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                ok = False
                break
        vals[path] = node if ok else {"$unset": True}
    out["keys"][rel] = vals
for rel in req["delete"]:
    out["exists"][rel] = os.path.lexists(os.path.join(dest, rel))
print("@@FLOTILLA@@" + json.dumps(out))
"""

APPLY = r"""
import base64, json, os, shutil, sys
req = json.load(sys.stdin)
dest = os.path.expanduser(req["dest"])
def put(p, data, mode):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".flotilla-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, p)
for w in req["writes"]:
    p = os.path.join(dest, w["rel"])
    keep = os.stat(p).st_mode & 0o777 if os.path.isfile(p) else 0o644
    put(p, base64.b64decode(w["b64"]), int(w["mode"], 8) if w["mode"] else keep)
    print("wrote " + w["rel"])
for rel in req["delete"]:
    p = os.path.join(dest, rel)
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    elif os.path.lexists(p):
        os.remove(p)
    print("deleted " + rel)
for patch in req["patches"]:
    p = os.path.join(dest, patch["rel"])
    try:
        config = json.load(open(p))
        keep = os.stat(p).st_mode & 0o777
    except FileNotFoundError:
        config, keep = {}, 0o600
    for path, value in patch["set"].items():
        parts, node = path.split("."), config
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
    for path in patch["unset"]:
        parts, node = path.split("."), config
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(parts[-1], None)
    put(p, (json.dumps(config, indent=2) + "\n").encode(),
        0o600 if patch["secret"] else keep)
    print("updated " + patch["rel"] + " (" + ", ".join(
        list(patch["set"]) + list(patch["unset"])) + ")")
"""

UNSET = {"$unset": True}


def _remote_python(fleet: Fleet, host: Host, program: str, payload: Any,
                   timeout: int = 120) -> tuple[int, str]:
    """Run a Python program on the host with ``payload`` as JSON on stdin."""
    return remote.run(fleet, host, f"python3 -c {shlex.quote(program)}",
                      timeout=timeout, input=json.dumps(payload))


def _safe(rel: str) -> str:
    if rel.startswith("/") or ".." in rel.split("/") or not rel:
        raise BundleError(f"unsafe path {rel!r}")
    return rel


@dataclass
class HostPlan:
    """What applying a bundle would change on one host."""

    ops: list[str] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)   # rels to diff


def inspect(bundle: Bundle, fleet: Fleet, host: Host, comp: Compiled,
            content: list[str] | None = None) -> dict[str, Any]:
    req = {"dest": bundle.dest,
           "files": [_safe(r) for r, f in comp.files.items() if not f.own_keys],
           "owned": [_safe(d) for d in bundle.owned],
           "keys": {_safe(r): f.own_keys for r, f in comp.files.items()
                    if f.own_keys},
           "delete": [_safe(r) for r in comp.delete],
           "content": content or []}
    code, out = _remote_python(fleet, host, INSPECT, req)
    if code != 0 or MARK not in out:
        raise RuntimeError(out.strip()[-300:] or f"exit {code}")
    return json.loads(out[out.index(MARK) + len(MARK):].splitlines()[0])


def plan_host(bundle: Bundle, fleet: Fleet, host: Host, resolver: Resolver,
              comp: Compiled | None = None,
              seen: dict[str, Any] | None = None) -> HostPlan:
    comp = comp or compile_bundle(bundle, host, fleet)
    seen = seen or inspect(bundle, fleet, host, comp)
    plan = HostPlan()
    for rel, f in sorted(comp.files.items()):
        if f.own_keys:
            _plan_keys(rel, f, seen["keys"].get(rel, {}), resolver, plan)
            continue
        data = f.resolved(resolver)
        have = seen["files"].get(rel)
        mode = "600" if f.secret else None
        if have is None:
            plan.ops.append(f"add {rel}")
        elif have["sha"] != hashlib.sha256(data).hexdigest():
            plan.ops.append(f"update {rel}")
        elif mode and have["mode"] != mode:
            plan.ops.append(f"chmod 600 {rel}")
        else:
            continue
        plan.changed.append(rel)
        plan.writes.append({"rel": rel, "mode": mode,
                            "b64": base64.b64encode(data).decode()})
    for names in seen["owned"].values():
        for name in names:
            if name not in comp.files:
                plan.ops.append(f"delete {name}")
                plan.delete.append(name)
    for rel in comp.delete:
        if seen["exists"].get(rel):
            plan.ops.append(f"delete {rel}")
            plan.delete.append(rel)
    return plan


def _plan_keys(rel: str, f: OutFile, have: dict[str, Any],
               resolver: Resolver, plan: HostPlan) -> None:
    data = resolver.resolve(f.data)
    patch = {"rel": rel, "set": {}, "unset": [], "secret": f.secret}
    for path in f.own_keys:
        want = layers.get_path(data, tuple(path.split(".")))
        cur = have.get(path, UNSET)
        if want is None:
            if cur != UNSET:
                patch["unset"].append(path)
                plan.ops.append(f"unset {path} in {rel}")
        elif cur != want:
            patch["set"][path] = want
            plan.ops.append(f"set {path} in {rel}")
    if patch["set"] or patch["unset"]:
        plan.patches.append(patch)
        plan.changed.append(rel)


def summarize(bundle: Bundle, ops: list[str]) -> list[str]:
    """Group per-file operations by item for directory bundles."""
    if bundle.unit != "directory":
        return ops
    counts: dict[tuple[str, str], int] = {}
    for op in ops:
        verb, _, rel = op.partition(" ")
        key = (verb, rel.split("/", 1)[0])
        counts[key] = counts.get(key, 0) + 1
    return [f"{verb} {item} ({n} file{'s' if n > 1 else ''})"
            for (verb, item), n in counts.items()]


def apply_host(bundle: Bundle, fleet: Fleet, host: Host,
               plan: HostPlan) -> tuple[int, list[str]]:
    lines: list[str] = []
    if not (plan.writes or plan.delete or plan.patches):
        return 0, lines
    code, out = _remote_python(fleet, host, APPLY, {
        "dest": bundle.dest, "writes": plan.writes, "delete": plan.delete,
        "patches": plan.patches}, timeout=300)
    lines += [ln for ln in out.splitlines() if ln.strip()]
    if code == 0 and bundle.after_apply:
        code, out = remote.run(fleet, host, bundle.after_apply, timeout=120)
        lines += [ln for ln in out.splitlines() if ln.strip()]
    return code, lines


def diffs(bundle: Bundle, fleet: Fleet, host: Host,
          resolver: Resolver) -> list[dict[str, Any]]:
    """Unified diffs between the host and the compiled bundle (masked)."""
    from .runner import mask

    comp = compile_bundle(bundle, host, fleet)
    plan = plan_host(bundle, fleet, host, resolver, comp)
    if not plan.ops:
        return []
    plain = [r for r in plan.changed if not comp.files[r].own_keys]
    seen = inspect(bundle, fleet, host, comp, content=plain)
    out = []
    for rel in plan.changed:
        f = comp.files[rel]
        if f.own_keys:
            have = {p: v for p, v in seen["keys"].get(rel, {}).items()
                    if v != UNSET}
            want = {p: layers.get_path(resolver.resolve(f.data),
                                       tuple(p.split("."))) for p in f.own_keys}
            old = json.dumps(have, indent=2, sort_keys=True).splitlines(True)
            new = json.dumps({k: v for k, v in want.items() if v is not None},
                             indent=2, sort_keys=True).splitlines(True)
            label = f"{rel} ({', '.join(f.own_keys)})"
        elif isinstance(f.text, bytes):
            out.append({"path": rel, "diff": f"{rel}: binary file differs\n"})
            continue
        else:
            raw = seen["content"].get(rel)
            old = (base64.b64decode(raw).decode(errors="replace")
                   if raw is not None else "").splitlines(True)
            new = f.resolved(resolver).decode().splitlines(True)
            label = rel
        text = "".join(difflib.unified_diff(
            old, new, fromfile=f"{host.name}:{label}",
            tofile=f"desired:{label}"))
        out.append({"path": rel, "diff": mask(resolver.redact(text))})
    for rel in plan.delete:
        out.append({"path": rel, "diff": f"delete {rel}\n"})
    return out


def execute(bundle: Bundle, fleet: Fleet, hosts: list[Host], dry: bool,
            resolver: Resolver | None = None) -> Iterator[Any]:
    """Plan or apply on ``hosts``; same protocol as ``runner.execute``."""
    resolver = resolver or Resolver()

    def one(host: Host) -> tuple[Host, HostPlan | None, str | None]:
        try:
            return host, plan_host(bundle, fleet, host, resolver), None
        except Exception as exc:  # noqa: BLE001 - reported per host
            return host, None, str(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, hosts))
    plans: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for host, plan, err in results:
        tag = f"[{host.name}] "
        if err is not None:
            errors[host.name] = err
            yield f"{tag}error: {err}"
            continue
        ops = summarize(bundle, plan.ops)
        for op in ops:
            yield tag + op
        if not ops:
            continue
        if dry:
            plans[host.name] = ops
            continue
        code, lines = apply_host(bundle, fleet, host, plan)
        for line in lines:
            yield tag + resolver.redact(line)
        if code != 0:
            errors[host.name] = f"apply exited {code}"
    if errors:
        yield f"failed on: {', '.join(sorted(errors))}"
    if dry:
        yield ("plan", {"plan": plans, "errors": errors})
    yield ("exit", 1 if errors else 0)


# ---------------------------------------------------------------- editing

def set_override(bundle: Bundle, layer_id: str, rel: str, key: str,
                 value: Any, op: str = "") -> Path:
    """Write one key into an override file of ``layer_id``.

    ``rel`` is the state file (for example ``config.yml``); ``op`` is
    ``""``, ``"+"``, ``"-"`` or ``"!"``. A key that is not in the file yet is
    appended, so the rest of the file (and its comments) stays as it is.
    """
    if layer_id == "base":
        raise BundleError("edit the base file itself; overrides go in a "
                          "group or host layer")
    if op not in ("", "+", "-", "!"):
        raise BundleError(f"bad operation {op!r}")
    if bundle.rule(out_name(rel)).merge != "keys":
        raise BundleError(f"{rel} is replaced whole, not merged by key")
    path = bundle.layer_dir(layer_id) / _safe(rel)
    text = path.read_text() if path.is_file() else ""
    doc = parse(rel, text) if text.strip() else {}
    text = _remove_text(rel, text, doc, tuple(key.split(".")))
    if rel.endswith(".json"):
        doc[key + op] = value
        text = dump(out_name(rel), doc)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += dump_file(rel, {key + op: value})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def clear_override(bundle: Bundle, layer_id: str, rel: str, key: str) -> bool:
    """Remove ``key`` from an override file; delete the file once empty."""
    path = bundle.layer_dir(layer_id) / _safe(rel)
    if not path.is_file():
        return False
    text = path.read_text()
    doc = parse(rel, text)
    before = copy.deepcopy(doc)
    text = _remove_text(rel, text, doc, tuple(key.split(".")))
    if doc == before:
        return False
    if doc:
        path.write_text(text)
    else:
        path.unlink()
    return True


def _remove_text(rel: str, text: str, doc: dict, path: layers.Path) -> str:
    """Remove ``path`` from ``doc`` and return matching file text.

    Whole top-level entries are cut out of the YAML text, so comments and
    the other keys stay as written. Anything else is written out again.
    """
    before = list(doc)
    if not layers.remove_key(doc, path):
        return text
    if rel.endswith(".json"):
        return dump(out_name(rel), doc)
    gone = {str(k) for k in before if k not in doc}
    out, skipping = [], False
    for line in text.splitlines(True):
        if skipping:
            if not line.strip() or line.startswith((" ", "\t", "-")):
                continue
            skipping = False
        head = line.split(":", 1)[0].strip().strip("'\"")
        if ":" in line and not line.startswith((" ", "\t", "#", "-")) \
                and head in gone:
            skipping = True
            continue
        out.append(line)
    new = "".join(out)
    try:
        if (yaml.safe_load(new) or {}) == doc:
            return new
    except yaml.YAMLError:
        pass
    return dump_file(rel, doc)


def set_item_state(bundle: Bundle, layer_id: str, item: str,
                   kind: str | None) -> Path:
    """Mark ``item`` as ``skip``/``delete`` in a layer, or clear the mark."""
    if kind not in (None, "skip", "delete"):
        raise BundleError(f"bad state {kind!r}")
    d = bundle.layer_dir(layer_id)
    manifest = layer_manifest(d)
    for k in ("skip", "delete"):
        manifest[k] = [i for i in manifest[k] if i != item]
    if kind:
        manifest[kind] = sorted({*manifest[kind], item})
    path = d / LAYER_FILE
    data = {k: v for k, v in manifest.items() if v}
    if data:
        d.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_file(LAYER_FILE, data))
    elif path.exists():
        path.unlink()
    return path
