"""Helpers shared by the pyinfra deploy files in this package."""

from __future__ import annotations

import json
import shlex
from typing import Any

from pyinfra import host
from pyinfra.facts.server import Command
from pyinfra.operations import files, server

# pyinfra runs plain `sh -c`; add the user and Homebrew bin directories.
PATH = ('export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:'
        '$PATH";')


def bundle() -> dict[str, Any]:
    """The per-host bundle the runner wrote for this component."""
    with open(host.data.flotilla_bundle) as fh:
        return json.load(fh)


def fact(cmd: str) -> str:
    """Run a read-only command on the host and return stdout."""
    out = host.get_fact(Command, f"{PATH} {cmd} 2>/dev/null || true")
    return (out or "").strip()


def sh(name: str, script: str) -> None:
    """Run a shell script on the host (``set -e``, local bin on PATH)."""
    server.shell(name=name,
                 commands=[f"bash -c {shlex.quote('set -e; ' + PATH + script)}"])


def fail(name: str, message: str) -> None:
    """Record a failing operation without stopping other hosts."""
    sh(name, f"echo {shlex.quote(message)} >&2; exit 1")


def put_files(b: dict[str, Any]) -> None:
    dirs = sorted({(f["dest"].rsplit("/", 1)[0], f["label"].rsplit("/", 1)[0])
                   for f in b["files"]})
    for path, label in dirs:
        files.directory(name=f"dir {label}", path=path, present=True)
    for f in b["files"]:
        files.put(name=f"file {f['label']}", src=f["src"], dest=f["dest"],
                  mode=f["mode"] or None)
