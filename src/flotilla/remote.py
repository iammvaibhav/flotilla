"""Run commands on hosts, and pin their SSH host keys."""

from __future__ import annotations

import json
import shlex
import subprocess
import urllib.request
from pathlib import Path

from .render import ssh_config
from .state import Fleet, Host, state_dir

CONTROLLER_SSH = Path("~/.config/flotilla/ssh_config").expanduser()


def is_controller(fleet: Fleet, host: Host) -> bool:
    return host.name == fleet.controller


def write_controller_ssh_config(fleet: Fleet) -> Path:
    """SSH config the controller uses: fleet hosts + pinned keys only."""
    body = ssh_config(fleet).replace(
        "UserKnownHostsFile ~/.ssh/known_hosts ~/.ssh/known_hosts.flotilla",
        f"UserKnownHostsFile {state_dir() / 'known_hosts'}\n"
        "  StrictHostKeyChecking yes\n  BatchMode yes\n"
        "  ConnectTimeout 8")
    CONTROLLER_SSH.parent.mkdir(parents=True, exist_ok=True)
    CONTROLLER_SSH.write_text(body)
    return CONTROLLER_SSH


def argv(fleet: Fleet, host: Host, script: str) -> list[str]:
    """Command line that runs ``script`` in the user's login shell."""
    inner = 'exec "${SHELL:-/bin/bash}" -lc ' + shlex.quote(script)
    if is_controller(fleet, host):
        return ["bash", "-c", inner]
    return ["ssh", "-F", str(write_controller_ssh_config(fleet)), "-T",
            host.name, inner]


def run(fleet: Fleet, host: Host, script: str, timeout: int = 60,
        input: str | None = None) -> tuple[int, str]:
    """Run ``script`` on ``host``; return (exit code, stdout+stderr).

    ``input`` goes to the script's stdin (keeps secrets out of argv).
    """
    try:
        out = subprocess.run(argv(fleet, host, script), capture_output=True,
                             text=True, timeout=timeout, input=input,
                             stdin=None if input is not None
                             else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return out.returncode, (out.stdout + out.stderr)


def _keyscan(address: str) -> list[str]:
    out = subprocess.run(
        ["ssh-keyscan", "-T", "20", "-t", "ed25519,ecdsa,rsa", address],
        capture_output=True, text=True, timeout=90)
    return [ln for ln in out.stdout.splitlines()
            if ln and not ln.startswith("#")]


def _local_host_keys() -> list[str]:
    keys = []
    for pub in sorted(Path("/etc/ssh").glob("ssh_host_*_key.pub")):
        parts = pub.read_text().split()
        keys.append(f"localhost {parts[0]} {parts[1]}")
    return keys


def _github_keys() -> list[str]:
    """github.com keys from keyscan, checked against GitHub's API."""
    with urllib.request.urlopen("https://api.github.com/meta",
                                timeout=20) as resp:
        published = set(json.load(resp)["ssh_keys"])
    good = [ln for ln in _keyscan("github.com")
            if " ".join(ln.split()[1:3]) in published]
    if not good:
        raise RuntimeError("github.com keys do not match api.github.com/meta")
    return good


def scan_keys(fleet: Fleet) -> str:
    """Write ``state/known_hosts`` with every host under all its names.

    Keys come from ``ssh-keyscan`` over NetBird. NetBird already
    authenticates the peers, so this first-use pin is over a trusted
    link. The controller reads its own keys from /etc/ssh (macOS cannot
    reach its own NetBird address).
    """
    lines = ["# Managed by flotilla (`flotilla scan-keys`)."]
    missing = []
    for h in fleet.hosts.values():
        raw = _local_host_keys() if is_controller(fleet, h) else []
        raw = raw or _keyscan(h.address)
        if not raw:
            missing.append(h.name)
            continue
        names = ",".join(h.all_names)
        lines += [f"{names} {' '.join(ln.split()[1:3])}" for ln in raw]
    lines += _github_keys()
    (state_dir() / "known_hosts").write_text("\n".join(lines) + "\n")
    if missing:
        raise RuntimeError(f"no SSH keys from: {', '.join(missing)}")
    return "\n".join(lines)
