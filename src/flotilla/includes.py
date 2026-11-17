"""Values read from a text file in the bundle folder.

Long text that a program keeps in a config key (Paseo's appended system
prompt) is easier to edit as its own file than as one YAML string. A value
in a keys-merged state file can name such a file::

    daemon:
      appendSystemPrompt:
        $file: append-system-prompt.md

The path is relative to the bundle folder (``bundles/paseo/``), next to
``bundle.yaml`` and outside the layers, so the file itself is never copied
to hosts. The value becomes the file's text with surrounding whitespace
removed. ``flotilla show --explain`` lists the file as the value's source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import layers

KEY = "$file"


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


def read(root: Path, rel: Any) -> str:
    """The stripped text of ``rel`` in the bundle folder ``root``."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") \
            or ".." in rel.split("/"):
        raise IncludeError(f"{KEY}: {rel!r} is not a path in the bundle "
                           f"folder")
    path = root / rel
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        raise IncludeError(f"{KEY}: {rel} does not exist") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise IncludeError(f"{KEY}: cannot read {rel}: {exc}") from exc


def expand(data: Any, prov: dict[layers.Path, list[str]], root: Path,
           label: str, path: layers.Path = ()) -> Any:
    """``data`` with every ``{$file: rel}`` replaced by the file's text.

    The value's provenance becomes the layers that set ``$file`` plus
    ``<label>/<rel>``, so ``--explain`` names the file.
    """
    if is_include(data):
        rel = data[KEY]
        text = read(root, rel)
        setters = prov.pop(path + (KEY,), None) or prov.get(path) or []
        prov[path] = [*setters, f"{label}/{rel}"]
        return text
    if isinstance(data, dict):
        return {k: expand(v, prov, root, label, path + (str(k),))
                for k, v in data.items()}
    if isinstance(data, list) and present(data):
        raise IncludeError(f"{'.'.join(path)}: {KEY} only works as a map "
                           f"value, not inside a list")
    return data
