"""Values read from a file in a GitHub repository.

A value in a keys-merged state file can be the text of a file that lives in
another repository, so that repository keeps the only copy::

    daemon:
      appendSystemPrompt:
        $file: github:iammvaibhav/paseo@vaibhav/customizations:scripts/paseo-system-prompt.md

The value becomes the file's text with surrounding whitespace removed. The
controller reads the file at ``<ref>`` with ``gh api`` (the fleet GitHub
login) when it compiles the bundle, so a push to that branch reaches the
hosts on the next apply. ``flotilla show --explain`` lists the file as the
value's source.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import layers

KEY = "$file"
SOURCE = re.compile(r"^github:(?P<repo>[\w.-]+/[\w.-]+)@(?P<ref>[^:\s]+)"
                    r":(?P<path>\S+)$")
# The web app compiles bundles on every save and re-plans in the background;
# one read per file per minute is enough there.
TTL = 60.0

_cache: dict[str, tuple[float, str]] = {}
# Hosts compile in parallel threads; the first one reads, the rest hit cache.
_lock = threading.Lock()


class IncludeError(ValueError):
    """A ``$file`` value that is malformed or cannot be read."""


def is_include(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {KEY}


def present(data: Any) -> bool:
    if is_include(data):
        return True
    if isinstance(data, dict):
        return any(present(v) for v in data.values())
    if isinstance(data, list):
        return any(present(v) for v in data)
    return False


def read(source: str) -> str:
    """The text of ``github:<owner>/<repo>@<ref>:<path>``, stripped."""
    m = SOURCE.match(source) if isinstance(source, str) else None
    if not m:
        raise IncludeError(f"{KEY}: {source!r} is not "
                           f"github:<owner>/<repo>@<ref>:<path>")
    with _lock:
        hit = _cache.get(source)
        if hit and time.monotonic() - hit[0] < TTL:
            return hit[1]
        url = (f"repos/{m['repo']}/contents/{quote(m['path'])}"
               f"?ref={quote(m['ref'], safe='')}")
        env = {**os.environ, "PATH": f"{Path('~/.local/bin').expanduser()}:"
               f"{os.environ.get('PATH', '')}"}
        try:
            out = subprocess.run(
                ["gh", "api", "-H", "Accept: application/vnd.github.raw+json",
                 url], capture_output=True, text=True, env=env, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IncludeError(f"cannot read {source}: {exc}") from exc
        if out.returncode != 0:
            raise IncludeError(f"cannot read {source}: "
                               f"{out.stderr.strip()[-300:]}")
        text = out.stdout.strip()
        _cache[source] = (time.monotonic(), text)
        return text


def expand(data: Any, prov: dict[layers.Path, list[str]],
           path: layers.Path = ()) -> Any:
    """``data`` with every ``{$file: source}`` replaced by the file's text.

    The value's provenance becomes the layers that set ``$file`` plus the
    source, so ``--explain`` names the file.
    """
    if is_include(data):
        source = data[KEY]
        text = read(source)
        setters = prov.pop(path + (KEY,), None) or prov.get(path) or []
        prov[path] = [*setters, source]
        return text
    if isinstance(data, dict):
        return {k: expand(v, prov, path + (str(k),)) for k, v in data.items()}
    if isinstance(data, list):
        if present(data):
            raise IncludeError(f"{'.'.join(path)}: {KEY} only works as a "
                               f"map value, not inside a list")
        return data
    return data
