"""Desired state: hosts, groups, layered vars and layered files.

The state directory is private (it holds secret references and host
addresses) and lives outside this repository, by default in
``~/.config/flotilla/state``. Layout::

    hosts.yaml                  controller + hosts
    vars/all.yaml               vars for every host
    vars/groups/<group>.yaml    vars for one group
    vars/hosts/<host>.yaml      vars for one host
    files/<tree>/all/...        files for every host (tree: omp, ...)
    files/<tree>/groups/<g>/... files for one group
    files/<tree>/hosts/<h>/...  files for one host
    known_hosts                 pinned SSH host keys (written by ``scan``)

Layers apply in order: ``all``, then each group in the order the host
lists them, then the host. Later layers win.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPLACE = "$replace"
YAML_SUFFIXES = (".yml", ".yaml")


def state_dir() -> Path:
    """Return the state directory (``FLOTILLA_STATE`` or the default)."""
    raw = os.environ.get("FLOTILLA_STATE", "~/.config/flotilla/state")
    return Path(raw).expanduser()


@dataclass
class Host:
    """One managed machine."""

    name: str
    address: str
    user: str
    platform: str
    groups: list[str]
    names: list[str] = field(default_factory=list)
    home: str = ""
    roles: list[str] = field(default_factory=list)

    @property
    def os(self) -> str:
        return self.platform.split("-", 1)[0]

    @property
    def all_names(self) -> list[str]:
        """Alias, extra names and address, without duplicates."""
        out: list[str] = []
        for n in [self.name, *self.names, self.address]:
            if n and n not in out:
                out.append(n)
        return out

    def layers(self) -> list[str]:
        """Layer ids from lowest to highest priority."""
        return ["all", *[f"groups/{g}" for g in self.groups],
                f"hosts/{self.name}"]

    def default_home(self) -> str:
        if self.home:
            return self.home
        base = "/Users" if self.os == "darwin" else "/home"
        return f"{base}/{self.user}"


@dataclass
class Fleet:
    """Everything loaded from ``hosts.yaml``."""

    controller: str
    hosts: dict[str, Host]

    def host(self, name: str) -> Host:
        if name not in self.hosts:
            raise KeyError(f"unknown host: {name}")
        return self.hosts[name]

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for h in self.hosts.values():
            for g in h.groups:
                out.setdefault(g, []).append(h.name)
        return out


def load_fleet(root: Path | None = None) -> Fleet:
    """Load ``hosts.yaml`` from the state directory."""
    root = root or state_dir()
    raw = yaml.safe_load((root / "hosts.yaml").read_text()) or {}
    hosts = {}
    for name, spec in (raw.get("hosts") or {}).items():
        hosts[name] = Host(
            name=name,
            address=spec["address"],
            user=spec["user"],
            platform=spec["platform"],
            groups=list(spec.get("groups") or []),
            names=list(spec.get("names") or []),
            home=spec.get("home", ""),
            roles=list(spec.get("roles") or []),
        )
    controller = os.environ.get("FLOTILLA_CONTROLLER") or \
        raw.get("controller", "")
    return Fleet(controller=controller, hosts=hosts)


def save_fleet(fleet: Fleet, root: Path | None = None) -> None:
    """Write ``hosts.yaml`` back (used by onboarding)."""
    root = root or state_dir()
    data: dict[str, Any] = {"controller": fleet.controller, "hosts": {}}
    for h in fleet.hosts.values():
        spec: dict[str, Any] = {
            "address": h.address, "user": h.user,
            "platform": h.platform, "groups": h.groups,
        }
        if h.names:
            spec["names"] = h.names
        if h.home:
            spec["home"] = h.home
        if h.roles:
            spec["roles"] = h.roles
        data["hosts"][h.name] = spec
    (root / "hosts.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False))


def merge(base: Any, over: Any) -> Any:
    """Deep-merge ``over`` onto ``base``.

    Dicts merge key by key. Lists and scalars replace. A ``None`` value
    deletes the key. A dict that holds ``$replace: true`` replaces the
    base value instead of merging into it.
    """
    if isinstance(over, dict) and over.get(REPLACE) is True:
        return {k: v for k, v in over.items() if k != REPLACE}
    if not (isinstance(base, dict) and isinstance(over, dict)):
        return copy.deepcopy(over)
    out = copy.deepcopy(base)
    for key, val in over.items():
        if val is None:
            out.pop(key, None)
        elif key in out:
            out[key] = merge(out[key], val)
        else:
            out[key] = merge(None, val) if isinstance(val, dict) \
                else copy.deepcopy(val)
    return out


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text()) if path.exists() else None


def host_vars(host: Host, root: Path | None = None) -> dict[str, Any]:
    """Merged vars for one host."""
    root = root or state_dir()
    out: dict[str, Any] = {}
    for layer in host.layers():
        data = _load_yaml(root / "vars" / f"{layer}.yaml")
        if data:
            out = merge(out, data)
    return out


def layer_dir(tree: str, layer: str, root: Path | None = None) -> Path:
    return (root or state_dir()) / "files" / tree / layer


def host_files(host: Host, tree: str,
               root: Path | None = None) -> dict[str, Any]:
    """Merged file tree for one host.

    Returns ``{relative_path: content}``. A file that only one layer
    supplies keeps its text as written. YAML files that several layers
    supply are merged (see :func:`merge`) and returned as parsed data.
    Other files are replaced by the highest layer that has them.
    """
    root = root or state_dir()
    texts: dict[str, list[str]] = {}
    for layer in host.layers():
        base = layer_dir(tree, layer, root)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                rel = path.relative_to(base).as_posix()
                texts.setdefault(rel, []).append(path.read_text())
    out: dict[str, Any] = {}
    for rel, versions in texts.items():
        if len(versions) == 1 or not rel.endswith(YAML_SUFFIXES):
            out[rel] = versions[-1]
            continue
        data: Any = {}
        for text in versions:
            data = merge(data, yaml.safe_load(text) or {})
        out[rel] = data
    return out


class _Dumper(yaml.SafeDumper):
    """Indent list items under their key, as hand-written YAML does."""

    def increase_indent(self, flow: bool = False,
                        indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def dump_file(rel: str, content: Any) -> str:
    """Serialize merged content back to text."""
    if isinstance(content, str):
        return content
    return yaml.dump(content, Dumper=_Dumper, sort_keys=False,
                     allow_unicode=True, width=4096)
