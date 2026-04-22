"""Common shim body for whitelist-based strategies (B, C, D families).

Each of those strategies lacks a well-formed validate+set API, so we:
- own the whitelist in api-server
- validate type / min / max / choices inline
- read + mutate + atomic-write the YAML ourselves

The per-strategy shim modules import `run_shim` from here and pass their
own whitelist. This keeps parsing / path logic / error shape identical
across all non-Group-A strategies.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from nodeble_api_server.shims._yaml_helpers import (
    atomic_write_yaml,
    emit,
    get_by_path,
    parse_value,
    read_yaml,
    set_by_path,
    validate_bounds,
)


def run_shim(
    strategy_id: str,
    strategy_dir: Path,
    whitelist: dict[str, dict],
) -> None:
    """whitelist[dotted_path] = {"type": "float", "min": 0, "max": 1, ...,
       "config_file": "strategy.yaml" (default)}. Type is one of
       int / float / str / bool."""
    if len(sys.argv) < 3:
        emit({"ok": False, "old": None, "new": None, "error": "usage: <action> <strategy_id> [<param_path> <value_json>]"})
        sys.exit(0)

    action = sys.argv[1]

    # `list` action: emit the whitelist keys and exit. No value arg.
    if action == "list":
        emit({"ok": True, "old": None, "new": sorted(whitelist.keys()), "error": None})
        sys.exit(0)

    if len(sys.argv) < 5:
        emit({"ok": False, "old": None, "new": None, "error": "usage: <action> <strategy_id> <param_path> <value_json>"})
        sys.exit(0)

    _strategy_id_arg = sys.argv[2]
    param_path = sys.argv[3]
    value_json = sys.argv[4]

    defn = whitelist.get(param_path)
    if defn is None:
        emit({"ok": False, "old": None, "new": None, "error": f"param_path '{param_path}' not in whitelist for {strategy_id}"})
        sys.exit(0)

    try:
        value_obj = json.loads(value_json)
    except json.JSONDecodeError:
        value_obj = value_json
    value_str = value_obj if isinstance(value_obj, str) else json.dumps(value_obj)

    config_file = defn.get("config_file", "strategy.yaml")
    yaml_path = strategy_dir / "config" / config_file

    try:
        data = read_yaml(yaml_path)
    except Exception as e:
        emit({"ok": False, "old": None, "new": None, "error": f"read {yaml_path}: {e}"})
        sys.exit(0)

    old_value = get_by_path(data, param_path)

    # Parse + bounds-check.
    try:
        parsed: Any = parse_value(value_str, defn["type"])
    except ValueError as e:
        emit({"ok": False, "old": old_value, "new": None, "error": str(e)})
        sys.exit(0)

    bounds_err = validate_bounds(parsed, defn)
    if bounds_err:
        emit({"ok": False, "old": old_value, "new": None, "error": bounds_err})
        sys.exit(0)

    if action == "validate":
        emit({"ok": True, "old": old_value, "new": parsed, "error": None})
        sys.exit(0)

    if action == "set":
        try:
            set_by_path(data, param_path, parsed)
            atomic_write_yaml(yaml_path, data)
        except Exception as e:
            emit({"ok": False, "old": old_value, "new": None, "error": f"write {yaml_path}: {e}"})
            sys.exit(0)
        emit({"ok": True, "old": old_value, "new": parsed, "error": None})
        sys.exit(0)

    emit({"ok": False, "old": old_value, "new": None, "error": f"unknown action: {action}"})
    sys.exit(0)
