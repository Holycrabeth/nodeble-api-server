"""Shared helpers for shims that manage their own YAML I/O.

Group B (calendar), Group C (strangle), and Group D (straddle/collar/
ironbutterfly) all need to:
- read a YAML file
- validate a dotted-path edit against a whitelist
- mutate + atomically write back

rather than delegating to a strategy's own `set_config_value` — either
because the strategy doesn't expose one (Group D), or because the one it
exposes doesn't do atomic writes (Group B+C).

This module is imported by the other shim modules; it is NOT a shim
itself (no main()).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def parse_value(raw: str, ptype: str) -> Any:
    """Coerce a stringified value into the declared Python type."""
    if ptype == "int":
        if "." in raw:
            raise ValueError(f"{raw!r} must be a whole number")
        return int(raw)
    if ptype == "float":
        return float(raw)
    if ptype == "bool":
        lowered = raw.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True
        if lowered in ("false", "no", "0"):
            return False
        raise ValueError(f"{raw!r} is not a boolean")
    if ptype == "str":
        return raw
    raise ValueError(f"unknown type: {ptype}")


def validate_bounds(value: Any, defn: dict) -> str | None:
    """Return an error message if value violates min/max/choices; else None."""
    if "choices" in defn and value not in defn["choices"]:
        return f'value must be one of {defn["choices"]}'
    if "min" in defn and value < defn["min"]:
        return f'value {value} below min {defn["min"]}'
    if "max" in defn and value > defn["max"]:
        return f'value {value} above max {defn["max"]}'
    return None


def get_by_path(data: dict, dotted: str) -> Any:
    node: Any = data
    for key in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def set_by_path(data: dict, dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    node = data
    for key in keys[:-1]:
        existing = node.get(key)
        if not isinstance(existing, dict):
            node[key] = {}
        node = node[key]
    node[keys[-1]] = value


def atomic_write_yaml(path: Path, data: dict) -> None:
    """tempfile + os.replace — crash-safe. Leaves no .tmp file on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def emit(payload: dict) -> None:
    """Single-line JSON write to stdout for all shims."""
    import sys
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()
