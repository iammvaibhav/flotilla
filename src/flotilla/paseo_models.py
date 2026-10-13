"""Paseo models: which agent providers and models each Paseo daemon offers.

flotilla owns two keys of ``~/.paseo/config.json`` on every host whose vars
set ``paseo_models``; the daemon and the Paseo deploy own the rest::

    agents.providers      <- paseo_models.providers
    daemon.visibleModels  <- paseo_models.visible (keys whose value is true)

Both come from the layered vars (``all`` -> groups -> host), so a group
shares one set and a host can add or remove single entries:

    paseo_models:
      providers:
        omp: {enabled: true}       # maps merge; `null` drops a provider
      visible:
        "omp:anthropic/claude-opus-5-5": true    # shown
        "omp:grok-build/grok-4.7": null          # dropped from a lower layer

``visible`` is a map rather than a list so that a layer can add or remove
one model without repeating the others. An empty result means "every
model" to Paseo. Secrets (provider ``env`` values) are aside:// references
resolved on the controller. After writing, the daemon reloads the config;
both keys apply without a restart.
"""

from __future__ import annotations

import difflib
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterator

from . import remote
from .secrets import Resolver
from .state import Fleet, Host, host_vars

CONFIG = "~/.paseo/config.json"

# Runs on the host with the desired keys on stdin: replace the two keys,
# write atomically (mode 600: provider env can hold secrets), reload.
APPLY = r"""
python3 -c '
import json, os, sys
want = json.load(sys.stdin)
path = os.path.expanduser("~/.paseo/config.json")
try:
    with open(path) as fh:
        config = json.load(fh)
except FileNotFoundError:
    config = {}
config.setdefault("agents", {})["providers"] = want["providers"]
config.setdefault("daemon", {})["visibleModels"] = want["visibleModels"]
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".flotilla-tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(config, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
print("wrote " + path)
'
set -a; [ -f ~/.paseo/deploy.env ] && . ~/.paseo/deploy.env; set +a
if command -v paseo >/dev/null && paseo daemon status >/dev/null 2>&1; then
  paseo daemon reload --home ~/.paseo
else
  echo "no running Paseo daemon; it reads the config when it starts"
fi
"""


def desired(host: Host, resolver: Resolver) -> dict[str, Any] | None:
    """The managed keys for ``host``, or None when it is not managed."""
    spec = host_vars(host).get("paseo_models")
    if not spec:
        return None
    spec = resolver.resolve(spec)
    visible = spec.get("visible") or {}
    return {"providers": spec.get("providers") or {},
            "visibleModels": [m for m, on in visible.items() if on]}


def current(fleet: Fleet, host: Host) -> dict[str, Any]:
    """The managed keys as they are on the host (empty if no config)."""
    code, out = remote.run(
        fleet, host, f"cat {CONFIG.replace('~', '$HOME', 1)} 2>/dev/null"
        " || echo '{}'", timeout=30)
    start = out.find("{")
    if code != 0 or start < 0:
        raise RuntimeError(out.strip()[-300:] or f"exit {code}")
    config = json.loads(out[start:])
    return {"providers": (config.get("agents") or {}).get("providers") or {},
            "visibleModels": (config.get("daemon") or {}).get(
                "visibleModels") or []}


def changes(have: dict[str, Any], want: dict[str, Any]) -> list[str]:
    """One line per provider or model that would change."""
    out = []
    old, new = have["providers"], want["providers"]
    for pid in sorted(set(old) | set(new)):
        if pid not in new:
            out.append(f"remove provider {pid}")
        elif pid not in old:
            out.append(f"add provider {pid}")
        elif old[pid] != new[pid]:
            out.append(f"change provider {pid}")
    old_m, new_m = have["visibleModels"], want["visibleModels"]
    out += [f"show model {m}" for m in new_m if m not in old_m]
    out += [f"hide model {m}" for m in old_m if m not in new_m]
    if not out and old_m != new_m:
        out.append("reorder visible models")
    return out


def _text(keys: dict[str, Any]) -> list[str]:
    return json.dumps({"agents.providers": keys["providers"],
                       "daemon.visibleModels": keys["visibleModels"]},
                      indent=2, sort_keys=True).splitlines(True)


def diffs(fleet: Fleet, host: Host,
          resolver: Resolver) -> list[dict[str, Any]]:
    """Unified diff of the managed keys (secrets masked), like file diffs."""
    from .runner import mask  # runner imports this module

    want = desired(host, resolver)
    if want is None:
        return []
    have = current(fleet, host)
    if not changes(have, want):
        return []
    label = f"{CONFIG} (agents.providers, daemon.visibleModels)"
    text = "".join(difflib.unified_diff(
        _text(have), _text(want), fromfile=f"{host.name}:{label}",
        tofile=f"desired:{label}"))
    return [{"path": CONFIG, "new": False,
             "diff": mask(resolver.redact(text))}]


def execute(fleet: Fleet, hosts: list[Host], dry: bool,
            resolver: Resolver | None = None) -> Iterator[Any]:
    """Plan or apply; same protocol as :func:`flotilla.runner.execute`."""
    resolver = resolver or Resolver()
    managed = [(h, desired(h, resolver)) for h in hosts]
    managed = [(h, w) for h, w in managed if w is not None]
    for h in hosts:
        if all(h is not m for m, _ in managed):
            yield f"[{h.name}] not managed (no paseo_models in its vars)"

    def read(item):
        host, want = item
        try:
            return host, want, current(fleet, host), None
        except Exception as exc:  # noqa: BLE001 - reported per host
            return host, want, None, str(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(read, managed))
    plan: dict[str, list[str]] = {}
    failed = []
    for host, want, have, err in states:
        tag = f"[{host.name}] "
        if err is not None:
            yield f"{tag}cannot read {CONFIG}: {err}"
            failed.append(host.name)
            continue
        ops = changes(have, want)
        if not ops:
            continue
        plan[host.name] = ops
        if dry:
            continue
        code, out = remote.run(fleet, host, APPLY, timeout=60,
                               input=json.dumps(want))
        for line in out.splitlines():
            if line.strip():
                yield tag + line
        if code != 0:
            failed.append(host.name)
    if failed:
        yield f"failed on: {', '.join(sorted(failed))}"
    if dry:
        yield ("plan", plan)
    yield ("exit", 1 if failed else 0)
