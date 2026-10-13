"""Turn rendered state into pyinfra runs, and diff files before a run."""

from __future__ import annotations

import base64
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import queue
import tempfile
import threading
from importlib import resources
from pathlib import Path
from typing import Any

from . import paseo, paseo_models, remote
from .render import Rendered, expand, render
from .secrets import Resolver
from .state import Fleet, Host, state_dir

COMPONENTS = {
    "ssh": "Fleet SSH key, ssh config include, pinned host keys",
    "github": "gh CLI, gh login, git identity",
    "toolchain": "uv + pnpm and their 7-day release cooldown",
    "omp": "omp config layers, agents, rules, fleet AGENTS.md",
    "netbird": "NetBird client joined with a setup key (new machines)",
    "paseo": "Host is in the Paseo deploy inventory (build once, copy)",
    "paseo-models": "Paseo agent providers and visible models (layered)",
}
RUNS = Path("~/.config/flotilla/runs").expanduser()


def _fingerprint(pubkey: str) -> str:
    out = subprocess.run(["ssh-keygen", "-lf", "-"], input=pubkey,
                         capture_output=True, text=True, check=True)
    return out.stdout.split()[1]


def _component_data(component: str, r: Rendered, hdir: Path,
                    host: Host) -> dict[str, Any]:
    v = r.data["vars"]
    if component == "ssh":
        pub = v["ssh"]["pubkey"]
        return {"pubkey": pub, "pubkey_match": " ".join(pub.split()[:2]),
                "fingerprint": _fingerprint(pub),
                "remove_pubkeys": v["ssh"].get("remove_pubkeys") or [],
                "remove_keyfiles": v["ssh"].get("remove_keyfiles") or []}
    if component == "github":
        tok = hdir / "gh-token"
        tok.write_text(v["github"]["token"])
        tok.chmod(0o600)
        return {"git": v["git"], "token_file": str(tok)}
    if component == "toolchain":
        return {"toolchain": v["toolchain"]}
    if component == "netbird":
        key = (v.get("netbird") or {}).get("setup_key")
        if not key:
            return {}
        path = hdir / "nb-key"
        path.write_text(key)
        path.chmod(0o600)
        return {"setup_key_file": str(path)}
    if component == "omp":
        return {"keep": r.data["keep"],
                "remove_npm_global":
                    (v.get("omp") or {}).get("remove_npm_global") or []}
    return {}


def prepare(component: str, fleet: Fleet, hosts: list[Host],
            resolver: Resolver) -> Path:
    """Render bundles and an inventory into a private run directory."""
    if component not in COMPONENTS:
        raise KeyError(f"unknown component: {component}")
    RUNS.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{component}-", dir=RUNS))
    run_dir.chmod(0o700)
    known = str(state_dir() / "known_hosts")
    inv = []
    for h in hosts:
        r = render(component, fleet, h, resolver)
        hdir = run_dir / h.name
        (hdir / "files").mkdir(parents=True)
        files = []
        for i, f in enumerate(r.files):
            src = hdir / "files" / str(i)
            src.write_text(f.content)
            src.chmod(0o600)
            files.append({"src": str(src), "dest": expand(f.path, h),
                          "label": f.path, "mode": f.mode})
        b = {"home": h.default_home(), "os": h.os, "files": files,
             "data": _component_data(component, r, hdir, h)}
        (hdir / "bundle.json").write_text(json.dumps(b))
        data = {"flotilla_bundle": str(hdir / "bundle.json"),
                "flotilla_host": h.name}
        if remote.is_controller(fleet, h):
            inv.append(("@local", data))
        else:
            data.update(ssh_hostname=h.address, ssh_user=h.user,
                        ssh_key=os.path.expanduser("~/.ssh/id_ed25519"),
                        ssh_look_for_keys=False, ssh_allow_agent=False,
                        ssh_known_hosts_file=known,
                        ssh_strict_host_key_checking="yes")
            inv.append((h.name, data))
    (run_dir / "inventory.py").write_text(f"hosts = {inv!r}\n")
    return run_dir


def pyinfra_argv(component: str, run_dir: Path, dry: bool) -> list[str]:
    deploy = resources.files("flotilla").joinpath("deploys",
                                                  f"{component}.py")
    argv = [sys.executable, "-m", "pyinfra", str(run_dir / "inventory.py"),
            str(deploy)]
    # `-y` with `--dry` makes pyinfra skip change detection.
    return argv + (["--dry", "--json"] if dry else ["-y"])


def cleanup(run_dir: Path) -> None:
    """Delete a run directory (it holds resolved secrets)."""
    shutil.rmtree(run_dir, ignore_errors=True)


def file_diffs(component: str, fleet: Fleet, host: Host,
               resolver: Resolver) -> list[dict[str, Any]]:
    """Unified diffs between the host's files and the rendered ones.

    Secret values are replaced by their references in the output.
    """
    if component == "paseo-models":
        return paseo_models.diffs(fleet, host, resolver)
    r = render(component, fleet, host, resolver)
    if not r.files:
        return []
    paths = [expand(f.path, host) for f in r.files]
    script = "; ".join(
        f"echo '@@{i}'; test -f '{p}' && base64 < '{p}' | tr -d '\\n'; echo"
        for i, p in enumerate(paths))
    rc, out = remote.run(fleet, host, script, timeout=60)
    if rc != 0:
        raise RuntimeError(f"{host.name}: {out.strip()[-300:]}")
    current: dict[int, str | None] = {}
    idx = None
    for line in out.splitlines():
        if line.startswith("@@") and line[2:].isdigit():
            idx = int(line[2:])
            current[idx] = None
        elif idx is not None and line:
            current[idx] = base64.b64decode(line).decode(errors="replace")
    diffs = []
    for i, f in enumerate(r.files):
        old = current.get(i)
        if old == f.content:
            continue
        if f.sensitive:
            diffs.append({"path": f.path, "new": old is None,
                          "diff": f"{f.path}: differs (content hidden)\n"})
            continue
        text = "".join(difflib.unified_diff(
            (old or "").splitlines(True), f.content.splitlines(True),
            fromfile=f"{host.name}:{f.path}" if old is not None
            else "/dev/null", tofile=f"desired:{f.path}"))
        diffs.append({"path": f.path, "new": old is None,
                      "diff": mask(resolver.redact(text))})
    return diffs


_SECRET_LINE = re.compile(
    r"""^(?P<head>[-+ ]\s*["']?[\w.-]*(?:api[_-]?key|token|secret|password"""
    r"""|passwd)["']?\s*[:=]\s*)(?P<val>.+)$""", re.I | re.M)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.S)


def mask(text: str) -> str:
    """Hide secret-looking values that the vault does not know about.

    Hosts can hold literal secrets that never went through the vault;
    a diff would print them. Values already shown as «aside://…» stay.
    """
    text = _PRIVATE_KEY.sub("«private key»", text)
    return _SECRET_LINE.sub(
        lambda m: m["head"] + (m["val"] if "«aside://" in m["val"]
                               else "«secret»"), text)


def summarize_plan(output: str, controller: str) -> dict[str, list[str]]:
    """``{host: [operation names that would change]}`` from ``--json``.

    pyinfra prints log lines before the JSON document; take the last line
    that parses as JSON.
    """
    plan = None
    for line in reversed(output.splitlines()):
        if line.startswith("{"):
            try:
                plan = json.loads(line)["plan"]
                break
            except (ValueError, KeyError):
                continue
    if plan is None:
        raise ValueError("no pyinfra plan in output")
    out: dict[str, list[str]] = {}
    for op in plan:
        for h in op["hosts_with_change"] + op["hosts_with_conditional_change"]:
            out.setdefault(controller if h == "@local" else h,
                           []).append(op["name"])
    return out


def execute(component: str, fleet: Fleet, hosts: list[Host], dry: bool,
            resolver: Resolver | None = None):
    """Run pyinfra for ``component``; yield redacted output lines.

    pyinfra matches operations across hosts by call order, and hosts get
    different file sets, so each host runs in its own pyinfra process
    (in parallel). Lines are prefixed with the host name.

    The last two items are ``("plan", {host: [ops]})`` (dry runs only)
    and ``("exit", code)``; every other item is a log line string.
    """
    if component == "paseo":
        yield from paseo.execute(fleet, hosts, dry)
        return
    if component == "paseo-models":
        yield from paseo_models.execute(fleet, hosts, dry, resolver)
        return
    resolver = resolver or Resolver()
    lines: queue.Queue = queue.Queue()
    plans: dict[str, list[str]] = {}
    codes: dict[str, int] = {}

    def one(host: Host) -> None:
        tag = f"[{host.name}] "
        try:
            run_dir = prepare(component, fleet, [host], resolver)
        except Exception as exc:  # noqa: BLE001 - reported per host
            lines.put(f"{tag}render failed: {exc}")
            codes[host.name] = 1
            return
        try:
            proc = subprocess.Popen(
                pyinfra_argv(component, run_dir, dry), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE if dry else subprocess.STDOUT,
                text=True)
            assert proc.stdout
            chunks: list[str] = []
            if dry:
                assert proc.stderr
                reader = threading.Thread(
                    target=lambda: chunks.append(proc.stdout.read()))
                reader.start()
                stream = proc.stderr
            else:
                stream = proc.stdout
            for line in stream:
                if line.strip():
                    lines.put(tag + mask(resolver.redact(line)).rstrip("\n"))
            code = proc.wait()
            if dry:
                reader.join()
                try:
                    plans.update(summarize_plan("".join(chunks),
                                                fleet.controller))
                    plans.setdefault(host.name, [])
                except ValueError:
                    code = code or 1
            codes[host.name] = code
        finally:
            cleanup(run_dir)

    workers = [threading.Thread(target=one, args=(h,)) for h in hosts]
    for w in workers:
        w.start()
    while any(w.is_alive() for w in workers) or not lines.empty():
        try:
            yield lines.get(timeout=0.2)
        except queue.Empty:
            pass
    failed = sorted(h for h, c in codes.items() if c)
    if failed:
        yield f"failed on: {', '.join(failed)}"
    if dry:
        yield ("plan", {h: ops for h, ops in plans.items() if ops})
    yield ("exit", 1 if failed else 0)


_SECRET_PLAIN = re.compile(
    r"""^(?P<head>\s*(?:export\s+)?["']?[\w.-]*(?:api[_-]?key|token|"""
    r"""secret|password"""
    r"""|passwd)["']?\s*[:=]\s*)(?P<val>\S.*)$""", re.I)


def mask_plain(line: str) -> str:
    """:func:`mask` for one line of command output (no diff prefix)."""
    line = _PRIVATE_KEY.sub("«private key»", line)
    m = _SECRET_PLAIN.match(line.rstrip("\n"))
    if m and "«aside://" not in m["val"]:
        return m["head"] + "«secret»"
    return line.rstrip("\n")
