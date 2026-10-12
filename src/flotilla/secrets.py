"""Secret references resolved through the ``aside-vault`` CLI.

State files never hold secret values. They hold references::

    aside://<account>/<item-id>            -> the item's password field
    aside://<account>/<item-id>#<field>    -> another field

The controller resolves references when it renders files for a host.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

REF = re.compile(r"^aside://(?P<account>[^/]+)/(?P<item>[^#/]+)"
                 r"(?:#(?P<field>\w+))?$")
# Keys that hold secrets. ``tokenizer`` and ``maxTokens`` are not secrets.
SECRET_KEY = re.compile(
    r"^(.*[_-]?)?(api[_-]?key|token|secret|password|passwd)$", re.I)
REF_IN_TEXT = re.compile(r"aside://[^/\s\"']+/[A-Za-z0-9_-]+(?:#\w+)?")
RECOVERY_ENV = Path("~/.config/aside-vault/recovery.env").expanduser()


def vault_env() -> dict[str, str]:
    """Process env plus the aside-vault recovery env file, if present."""
    env = dict(os.environ)
    if RECOVERY_ENV.exists():
        for line in RECOVERY_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                env.setdefault(key.strip(), shlex.split(val)[0] if val
                               else "")
    env["PATH"] = f"{Path('~/.local/bin').expanduser()}:" + \
        env.get("PATH", "")
    return env


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(REF.match(value))


class Resolver:
    """Resolve references once per run and remember the values."""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._synced: set[str] = set()

    @property
    def values(self) -> dict[str, str]:
        """Resolved values, keyed by reference (used for redaction)."""
        return dict(self._cache)

    def get(self, ref: str) -> str:
        if ref in self._cache:
            return self._cache[ref]
        m = REF.match(ref)
        if not m:
            raise ValueError(f"not a secret reference: {ref}")
        account = m["account"]
        cmd = ["aside-vault", "get", m["item"], "--account", account,
               "--field", m["field"] or "password"]
        if account in self._synced:
            cmd.append("--no-sync")
        out = subprocess.run(cmd, capture_output=True, text=True,
                             env=vault_env(), timeout=120)
        if out.returncode != 0:
            raise RuntimeError(
                f"aside-vault could not read {ref}: "
                f"{out.stderr.strip()[-300:]}")
        self._synced.add(account)
        value = out.stdout.rstrip("\n")
        self._cache[ref] = value
        return value

    def resolve(self, data: Any) -> Any:
        """Return a copy of ``data`` with every reference resolved."""
        return walk(data, lambda v: self.get(v) if is_ref(v) else v)

    def resolve_text(self, text: str) -> str:
        """Replace every reference that appears inside ``text``."""
        return REF_IN_TEXT.sub(lambda m: self.get(m.group(0)), text)

    def redact(self, text: str) -> str:
        """Replace resolved secret values in ``text`` with their refs."""
        for ref, value in sorted(self._cache.items(),
                                 key=lambda kv: -len(kv[1])):
            if len(value) >= 6:
                text = text.replace(value, f"«{ref}»")
        return text


def walk(data: Any, fn: Callable[[Any], Any]) -> Any:
    if isinstance(data, dict):
        return {k: walk(v, fn) for k, v in data.items()}
    if isinstance(data, list):
        return [walk(v, fn) for v in data]
    if isinstance(data, str):
        return fn(data)
    return data


def literal_secrets(data: Any, path: str = "") -> list[tuple[str, str]]:
    """Find secret-looking keys that hold a literal value.

    Returns ``[(dotted_path, value)]``. Used to block saving a literal
    secret into the state and to move existing ones into the vault.
    """
    found: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for key, val in data.items():
            sub = f"{path}.{key}" if path else str(key)
            if isinstance(val, str) and SECRET_KEY.match(str(key)) \
                    and val and not is_ref(val) \
                    and not val.startswith(("$", "!")):
                found.append((sub, val))
            else:
                found.extend(literal_secrets(val, sub))
    elif isinstance(data, list):
        for i, val in enumerate(data):
            found.extend(literal_secrets(val, f"{path}.{i}"))
    return found


def store(account: str, title: str, secret: str,
          category: str = "api") -> str:
    """Create a vault item and return a reference to it."""
    env = vault_env()
    add = subprocess.run(
        ["aside-vault", "add", "--account", account, "--category",
         category, "--title", title, "--secret", secret],
        capture_output=True, text=True, env=env, timeout=180)
    if add.returncode != 0:
        raise RuntimeError(f"aside-vault add failed: "
                           f"{add.stderr.strip()[-300:]}")
    listing = subprocess.run(
        ["aside-vault", "list", "--account", account, "--no-sync"],
        capture_output=True, text=True, env=env, timeout=120)
    for line in listing.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].startswith(title + " ["):
            return f"aside://{account}/{parts[0]}"
    raise RuntimeError(f"stored {title!r} but could not find its id")
