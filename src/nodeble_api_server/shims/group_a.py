"""Group A shim — ic / wheel / pmcc / directionalspread.

All four strategies share `validate_param(name, value_str, params) ->
(bool, err)` and `set_config_value(yaml_path, name, value_str, params)
-> (old, new)`. The `params` registry is per-strategy (IC_PARAMS /
WHEEL_PARAMS / PMCC_PARAMS / DS_PARAMS), with entries like:

    {"yaml_path": "selection.put_delta_max", "type": "float",
     "min": 0.01, "max": 0.50, "config_file": "risk.yaml"?}

The UI sends a dotted path (`selection.put_delta_max`); we reverse-look
it up against the registry to find the strategy's short alias (e.g.
`delta_max`) since that's what `validate_param` and `set_config_value`
expect as `name`.

Invoked by the api-server via the strategy's own venv:

    <strategy-venv-python> -m nodeble_api_server.shims.group_a \
        <action> <strategy_id> <param_path> <value_json>

Stdout is one JSON line:
    {"ok": bool, "old": any, "new": any, "error": str|null}
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

STRATEGY_CONFIG = {
    "ic":                ("nodeble.bot.bot_helpers",                  "IC_PARAMS",    Path.home() / ".nodeble"),
    "wheel":             ("nodeble_wheel.bot.bot_helpers",            "WHEEL_PARAMS", Path.home() / ".nodeble-wheel"),
    "pmcc":              ("nodeble_pmcc.bot.bot_helpers",             "PMCC_PARAMS",  Path.home() / ".nodeble-pmcc"),
    "directionalspread": ("nodeble_directionalspread.bot.bot_helpers", "DS_PARAMS",    Path.home() / ".nodeble-directionalspread"),
}


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


def _alias_for_path(params: dict, param_path: str) -> str | None:
    """Reverse-look-up: find the registry alias whose yaml_path matches."""
    for alias, defn in params.items():
        if defn.get("yaml_path") == param_path:
            return alias
    return None


def _config_path_for(strategy_dir: Path, defn: dict) -> Path:
    """Resolve strategy.yaml vs risk.yaml based on the ParamDef's flag."""
    fname = defn.get("config_file", "strategy.yaml")
    return strategy_dir / "config" / fname


def main() -> None:
    if len(sys.argv) < 3:
        emit({"ok": False, "old": None, "new": None, "error": "usage: <action> <strategy_id> [<param_path> <value_json>]"})
        sys.exit(0)

    action = sys.argv[1]
    strategy_id = sys.argv[2]

    if strategy_id not in STRATEGY_CONFIG:
        emit({"ok": False, "old": None, "new": None, "error": f"unsupported strategy: {strategy_id}"})
        sys.exit(0)

    module_name, params_name, strategy_dir = STRATEGY_CONFIG[strategy_id]
    try:
        mod = importlib.import_module(module_name)
    except Exception as e:
        emit({"ok": False, "old": None, "new": None, "error": f"import {module_name}: {e}"})
        sys.exit(0)

    params = getattr(mod, params_name, None)
    if not isinstance(params, dict):
        emit({"ok": False, "old": None, "new": None, "error": f"{params_name} missing or wrong type"})
        sys.exit(0)

    # `list` emits the editable param_path whitelist for this strategy.
    # No value_json arg required — short-circuit before the 5-arg check.
    if action == "list":
        paths = sorted({
            defn.get("yaml_path")
            for defn in params.values()
            if isinstance(defn, dict) and defn.get("yaml_path")
        })
        emit({"ok": True, "old": None, "new": list(paths), "error": None})
        sys.exit(0)

    if len(sys.argv) < 5:
        emit({"ok": False, "old": None, "new": None, "error": "usage: <action> <strategy_id> <param_path> <value_json>"})
        sys.exit(0)

    param_path = sys.argv[3]
    value_json = sys.argv[4]

    alias = _alias_for_path(params, param_path)
    if alias is None:
        emit({"ok": False, "old": None, "new": None, "error": f"param_path '{param_path}' not in whitelist"})
        sys.exit(0)

    defn = params[alias]
    yaml_path = _config_path_for(strategy_dir, defn)

    # value_json comes in as JSON to preserve type (bool / number / string
    # / list). `validate_param` / `set_config_value` want a string, so we
    # stringify — but drop JSON quotes for strings.
    try:
        value_obj = json.loads(value_json)
    except json.JSONDecodeError:
        value_obj = value_json  # best-effort: raw string

    value_str = value_obj if isinstance(value_obj, str) else json.dumps(value_obj)
    # json.dumps on a plain number yields "0.2"; on a bool yields "true".
    # Both are accepted by parse_value. No further massaging needed.

    try:
        old_value = mod.get_config_value(str(yaml_path), alias, params)
    except Exception as e:
        emit({"ok": False, "old": None, "new": None, "error": f"read current: {e}"})
        sys.exit(0)

    if action == "validate":
        ok, err = mod.validate_param(alias, value_str, params)
        if not ok:
            emit({"ok": False, "old": old_value, "new": None, "error": err})
            sys.exit(0)
        parsed = mod.parse_value(value_str, defn)
        emit({"ok": True, "old": old_value, "new": parsed, "error": None})
        sys.exit(0)

    if action == "set":
        ok, err = mod.validate_param(alias, value_str, params)
        if not ok:
            emit({"ok": False, "old": old_value, "new": None, "error": err})
            sys.exit(0)
        try:
            old, new = mod.set_config_value(str(yaml_path), alias, value_str, params)
            emit({"ok": True, "old": old, "new": new, "error": None})
        except Exception as e:
            emit({"ok": False, "old": old_value, "new": None, "error": f"write: {e}"})
        sys.exit(0)

    emit({"ok": False, "old": None, "new": None, "error": f"unknown action: {action}"})
    sys.exit(0)


if __name__ == "__main__":
    main()
