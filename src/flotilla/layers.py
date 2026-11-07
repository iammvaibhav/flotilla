"""Layered data: a base document plus override documents, most specific last.

An override document only holds what differs from the layers below it::

    modelRoles.default: anthropic/claude-opus-5-5:high   # one key, any depth
    disabledProviders+: [grok-build]                     # add to a list
    disabledProviders-: [ollama]                         # remove from a list
    retry.fallbackChains!: {default: [cursor/x]}         # replace, no merge
    auth.broker: ~                                       # delete the key

- Maps merge key by key; any other value replaces what is below.
- Dots split a key into a path only at the top level of an override, so a
  nested key that contains a dot (``"llama.cpp": ...``) stays literal.
- A key ending in ``+`` adds the listed items that are not there yet, ``-``
  removes the listed items, ``!`` replaces the value instead of merging it.
  These suffixes work at any depth.
- ``null`` (``~``) deletes the key.

:func:`apply` also records, per path, which layers set the value, so the UI
and ``flotilla show --explain`` can say where each key comes from.
"""

from __future__ import annotations

import copy
from typing import Any

Path = tuple[str, ...]
OPS = ("+", "-", "!")


class LayerError(ValueError):
    """An override that cannot be applied (for example ``+`` on a map)."""


def split_key(key: str, top: bool) -> tuple[Path, str]:
    """``("a.b.c+", top=True)`` -> ``(("a", "b", "c"), "+")``."""
    op = ""
    if key and key[-1] in OPS:
        op, key = key[-1], key[:-1]
    parts = tuple(key.split(".")) if top else (key,)
    if not all(parts):
        raise LayerError(f"bad key {key!r}")
    return parts, op


def has_ops(data: Any, top: bool = True) -> bool:
    """Whether a document uses override syntax (dotted keys or suffixes)."""
    if not isinstance(data, dict):
        return False
    for key, val in data.items():
        key = str(key)
        if (key and key[-1] in OPS) or (top and "." in key):
            return True
        if has_ops(val, top=False):
            return True
    return False


def _drop(prov: dict[Path, list[str]], path: Path) -> None:
    for p in [p for p in prov if p[:len(path)] == path]:
        del prov[p]


def leaves(data: Any, layer: str, prov: dict[Path, list[str]],
           path: Path = ()) -> None:
    """Record ``layer`` as the source of every value under ``path``."""
    if isinstance(data, dict) and data:
        for key, val in data.items():
            leaves(val, layer, prov, path + (str(key),))
    else:
        prov[path] = [layer]


def apply(base: Any, over: Any, layer: str,
          prov: dict[Path, list[str]] | None = None) -> Any:
    """Return ``base`` with the override document ``over`` applied."""
    prov = prov if prov is not None else {}
    if not isinstance(over, dict):
        raise LayerError(f"{layer}: an override must be a map")
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    _apply_map(out, over, layer, prov, (), top=True)
    return out


def _apply_map(target: dict, over: dict, layer: str,
               prov: dict[Path, list[str]], at: Path, top: bool) -> None:
    for raw, val in over.items():
        parts, op = split_key(str(raw), top)
        node = target
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = node[part] = {}
            node = nxt
        key, path = parts[-1], at + parts
        if val is None:
            node.pop(key, None)
            _drop(prov, path)
        elif op in ("+", "-"):
            _apply_list(node, key, val, op, layer, prov, path)
        elif op == "!" or not isinstance(val, dict):
            node[key] = _plain(val, layer)
            _drop(prov, path)
            leaves(node[key], layer, prov, path)
        else:
            if not isinstance(node.get(key), dict):
                node[key] = {}
                _drop(prov, path)
            _apply_map(node[key], val, layer, prov, path, top=False)
            if not val:
                prov.setdefault(path, [layer])


def _apply_list(node: dict, key: str, val: Any, op: str, layer: str,
                prov: dict[Path, list[str]], path: Path) -> None:
    where = f"{layer}: {'.'.join(path)}{op}"
    if not isinstance(val, list):
        raise LayerError(f"{where} needs a list of items")
    have = node.get(key, [])
    if not isinstance(have, list):
        raise LayerError(f"{where}: the value below is not a list")
    if op == "+":
        node[key] = have + [copy.deepcopy(v) for v in val if v not in have]
    else:
        node[key] = [v for v in have if v not in val]
    prov[path] = [*prov.get(path, []), f"{layer}{op}"]


def _plain(val: Any, layer: str) -> Any:
    """A value set as a whole: suffixes inside it are not allowed."""
    if has_ops(val, top=False):
        raise LayerError(f"{layer}: +, - and ! only work on keys that are "
                         f"merged, not inside a replaced value")
    return copy.deepcopy(val)


def explain(data: Any, prov: dict[Path, list[str]],
            path: Path = ()) -> list[dict[str, Any]]:
    """``[{"key": "a.b", "value": ..., "sources": [...]}]`` for each value.

    Maps are walked; lists and scalars are one entry each.
    """
    if isinstance(data, dict) and data:
        out: list[dict[str, Any]] = []
        for key, val in data.items():
            out += explain(val, prov, path + (str(key),))
        return out
    sources = prov.get(path)
    p = path
    while sources is None and p:
        p = p[:-1]
        sources = prov.get(p)
    return [{"key": ".".join(path), "value": data,
             "sources": sources or ["?"]}]


def get_path(data: Any, path: Path) -> Any:
    for part in path:
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def remove_key(doc: dict, path: Path) -> bool:
    """Remove every way an override document can name ``path``.

    Handles dotted top-level keys (``a.b.c``, with or without a suffix)
    and nested maps; empty maps left behind are removed. Returns whether
    anything was removed.
    """
    removed = False
    for raw in list(doc):
        parts, _ = split_key(str(raw), top=True)
        if parts == path:
            del doc[raw]
            removed = True
        elif len(parts) < len(path) and path[:len(parts)] == parts \
                and isinstance(doc[raw], dict):
            if _remove_nested(doc[raw], path[len(parts):]):
                removed = True
                if not doc[raw]:
                    del doc[raw]
    return removed


def _remove_nested(node: dict, path: Path) -> bool:
    removed = False
    for raw in list(node):
        name, _ = split_key(str(raw), top=False)
        if name[0] != path[0]:
            continue
        if len(path) == 1:
            del node[raw]
            removed = True
        elif isinstance(node[raw], dict) and _remove_nested(node[raw],
                                                            path[1:]):
            removed = True
            if not node[raw]:
                del node[raw]
    return removed
