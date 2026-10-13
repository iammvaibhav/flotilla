"""Render the files each component writes on each host."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import jinja2

from .secrets import REF_IN_TEXT, Resolver
from .state import Fleet, Host, dump_file, host_files, host_vars, state_dir

OMP_PRUNE_DIRS = ("agents", "rules")


@dataclass
class FileSpec:
    """One file to place on a host."""

    path: str          # remote path; ``~/`` is the user's home
    content: str
    mode: str | None = "644"   # None: leave the mode as it is
    sensitive: bool = False  # never show content in diffs


@dataclass
class Rendered:
    """What one component needs on one host."""

    files: list[FileSpec] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


def _template(name: str) -> jinja2.Template:
    override = state_dir() / "templates" / name
    if override.exists():
        text = override.read_text()
    else:
        text = resources.files("flotilla").joinpath(
            "templates", name).read_text()
    return jinja2.Environment(
        undefined=jinja2.StrictUndefined, keep_trailing_newline=True,
        trim_blocks=True, lstrip_blocks=True).from_string(text)


def ssh_config(fleet: Fleet, ips: bool = False) -> str:
    """The ``~/.ssh/config.d/flotilla.conf`` include for every host.

    ``ips``: for a machine that cannot resolve NetBird DNS names. Hosts
    whose address is a DNS name get their resolved IP, and a
    ``HostKeyAlias`` so the pinned key (listed under the host's name) still
    matches after the IP changes.
    """
    lines = ["# Managed by flotilla. Changes here are overwritten.", ""]
    every: list[str] = []
    for h in fleet.hosts.values():
        lines.append(f"Host {' '.join(h.all_names)}")
        if ips and h.ip != h.address:
            lines += [f"  HostName {h.ip}", f"  HostKeyAlias {h.name}"]
        else:
            lines.append(f"  HostName {h.address}")
        lines += [f"  User {h.user}", ""]
        every += h.all_names
    lines += [
        f"Host {' '.join(every)} github.com",
        "  IdentityFile ~/.ssh/id_ed25519",
        "  UserKnownHostsFile ~/.ssh/known_hosts ~/.ssh/known_hosts.flotilla",
        "  ServerAliveInterval 30",
        "",
    ]
    return "\n".join(lines)


def known_hosts() -> str:
    path = state_dir() / "known_hosts"
    if not path.exists():
        raise FileNotFoundError(
            "state/known_hosts is missing; run `flotilla scan-keys` first")
    return path.read_text()


def agents_md(fleet: Fleet, host: Host, vars_: dict[str, Any]) -> str:
    return _template("AGENTS.md.j2").render(
        fleet=fleet, me=host, hosts=list(fleet.hosts.values()),
        notes=(vars_.get("agents_md") or {}).get("notes", []))


def render(component: str, fleet: Fleet, host: Host,
           resolver: Resolver) -> Rendered:
    """Render one component for one host. Secrets are resolved."""
    v = resolver.resolve(host_vars(host))
    out = Rendered(data={"vars": v})
    if component == "ssh":
        out.files += [
            FileSpec("~/.ssh/id_ed25519", v["ssh"]["key"].rstrip("\n")
                     + "\n", "600", sensitive=True),
            FileSpec("~/.ssh/id_ed25519.pub", v["ssh"]["pubkey"] + "\n"),
            FileSpec("~/.ssh/config.d/flotilla.conf", ssh_config(
                fleet, ips=not v["ssh"].get("netbird_dns", True))),
            FileSpec("~/.ssh/known_hosts.flotilla", known_hosts()),
        ]
    elif component == "omp":
        tree = host_files(host, "omp")
        for rel, content in tree.items():
            raw = content if isinstance(content, str) else dump_file(
                rel, content)
            # Files that hold secrets are private; leave other modes alone.
            mode = "600" if REF_IN_TEXT.search(raw) else None
            text = resolver.resolve_text(raw)
            out.files.append(FileSpec(f"~/.omp/agent/{rel}", text, mode))
        out.files.append(FileSpec("~/.omp/agent/AGENTS.md",
                                  agents_md(fleet, host, v)))
        out.data["keep"] = {
            d: sorted(Path(r).name for r in tree if r.startswith(d + "/"))
            for d in OMP_PRUNE_DIRS}
    return out


def expand(path: str, host: Host) -> str:
    return host.default_home() + path[1:] if path.startswith("~/") \
        else path
