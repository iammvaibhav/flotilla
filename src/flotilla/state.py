"""Desired state: the machines, and the private state directory.

The state directory is private (it holds secret references and host
addresses) and lives outside this repository, by default in
``~/.config/flotilla/state``. It is its own git repository; every change
Flotilla makes is a commit. Layout::

    hosts.yaml          machines and their groups
    settings/           Flotilla's own settings, layered (see bundles.py)
    bundles/<name>/     files placed on hosts, layered (see bundles.py)
    known_hosts         pinned SSH host keys (written by ``scan-keys``)
"""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def state_dir() -> Path:
    """Return the state directory (``FLOTILLA_STATE`` or the default)."""
    raw = os.environ.get("FLOTILLA_STATE", "~/.config/flotilla/state")
    return Path(raw).expanduser()


@dataclass
class Host:
    """One machine in ``hosts.yaml``."""

    name: str
    address: str
    user: str
    platform: str
    groups: list[str]
    names: list[str] = field(default_factory=list)
    home: str = ""
    roles: list[str] = field(default_factory=list)
    # False: only an SSH name and a pinned key for the other hosts; no
    # component or bundle ever runs there.
    managed: bool = True

    @property
    def os(self) -> str:
        return self.platform.split("-", 1)[0]

    @property
    def all_names(self) -> list[str]:
        """Alias, extra names, address and IP, without duplicates."""
        out: list[str] = []
        for n in [self.name, *self.names, self.address, self.ip]:
            if n and n not in out:
                out.append(n)
        return out

    @property
    def ip(self) -> str:
        """The IPv4 address: ``address`` itself, or what it resolves to.

        A machine whose NetBird IP changes when it is rebuilt keeps its
        NetBird DNS name as ``address``; the controller resolves it.
        """
        return resolve_ipv4(self.address)

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

    def managed(self) -> list[Host]:
        return [h for h in self.hosts.values() if h.managed]

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for h in self.managed():
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
            managed=bool(spec.get("managed", True)),
        )
    controller = os.environ.get("FLOTILLA_CONTROLLER") or \
        raw.get("controller", "")
    return Fleet(controller=controller, hosts=hosts)


def commit(message: str, root: Path | None = None) -> bool:
    """Commit every change in the state directory; False if none."""
    root = root or state_dir()
    git = ["git", "-C", str(root)]
    if not (root / ".git").exists():
        subprocess.run(git + ["init", "-q"], check=True)
        (root / ".gitignore").write_text("import/\n")
    subprocess.run(git + ["add", "-A"], check=True)
    if subprocess.run(git + ["diff", "--cached", "--quiet"]).returncode == 0:
        return False
    subprocess.run(git + ["-c", "user.name=flotilla",
                          "-c", "user.email=flotilla@localhost",
                          "commit", "-q", "-m", message], check=True,
                   capture_output=True)
    return True


class _Dumper(yaml.SafeDumper):
    """Indent list items under their key, as hand-written YAML does."""

    def increase_indent(self, flow: bool = False,
                        indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def dump_file(rel: str, content: Any) -> str:
    """Serialize merged content back to YAML text."""
    if isinstance(content, str):
        return content
    return yaml.dump(content, Dumper=_Dumper, sort_keys=False,
                     allow_unicode=True, width=4096)


def resolve_ipv4(address: str) -> str:
    """``address`` if it is an IP literal, else its first IPv4 address."""
    try:
        ipaddress.ip_address(address)
        return address
    except ValueError:
        pass
    try:
        info = socket.getaddrinfo(address, None, socket.AF_INET,
                                  socket.SOCK_STREAM)
    except OSError as exc:
        raise RuntimeError(f"cannot resolve {address}: {exc}") from exc
    return str(info[0][4][0])
