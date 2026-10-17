#!/usr/bin/env python3
"""Apply six UI-verified Notion reasoning presets to an existing notion2api deployment.

Run from the notion2api project directory AFTER enable_notion_reasoning_effort.py.
Never reads account credentials or changes the working HTTP transport.
Safely rerunnable: replaces the single generated alias block rather than appending.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

LEVELS = ("low", "medium", "high", "xhigh", "max")
BEGIN = "# >>> NOTION_REASONING_EFFORT_ALIAS_BEGIN"
END = "# <<< NOTION_REASONING_EFFORT_ALIAS_END"

# Verified against the user's six Notion model-picker screenshots.
# No "none"/"no thinking" variant is exposed.
PRESETS = {
    "assam-chai": ("fable-5.1", ("low", "medium", "high", "max"), "high"),
    "omniberry-sundae": ("gpt-6.1-sol", ("low", "medium", "high", "xhigh", "max"), "medium"),
    "achira-donut": ("claude-sonnet5.5", ("low", "medium", "high", "xhigh", "max"), "high"),
    "amla-jam": ("claude-haiku5.5", ("low", "medium", "high", "xhigh", "max"), "medium"),
    "fireworks-kimi-k3": ("kimi-k3", ("low", "high", "max"), "high"),
    "albuquerque-quinn": ("claude-opus5.5", ("low", "medium", "high", "xhigh", "max"), "medium"),
}

ALIAS_BLOCK = '''
# >>> NOTION_REASONING_EFFORT_ALIAS_BEGIN
# Keep Notion model codes and reasoning levels explicit; never fall back silently.
import json as _effort_json
from pathlib import Path as _EffortPath
_EFFORT_FILE = _EffortPath(__file__).with_name("notion_reasoning_capabilities.json")
_EFFORT_CAPS = _effort_json.loads(_EFFORT_FILE.read_text(encoding="utf-8"))
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
MODEL_EFFORTS = {}
MODEL_DEFAULT_EFFORTS = {}
EFFORT_VARIANTS = {}
for _base_id, _code in list(MODEL_MAP.items()):
    _cfg = _EFFORT_CAPS.get(_code, {})
    _supported = [e for e in _cfg.get("levels", []) if e in _EFFORT_LEVELS]
    if not _supported:
        continue
    MODEL_EFFORTS[_base_id] = _supported
    _default = _cfg.get("default")
    if _default in _supported:
        MODEL_DEFAULT_EFFORTS[_base_id] = _default
    for _level in _supported:
        _alias = _base_id + "-effort-" + _level
        if _alias in MODEL_MAP:
            raise RuntimeError("Duplicate model effort alias: " + _alias)
        EFFORT_VARIANTS[_alias] = (_base_id, _level)
        MODEL_MAP[_alias] = _code
        DISPLAY_NAMES[_alias] = DISPLAY_NAMES[_base_id] + " / " + _level.upper() + (
            " (Default)" if _level == _default else ""
        )
        MODEL_ICONS[_alias] = MODEL_ICONS[_base_id]

def resolve_reasoning_effort(model_id: str, explicit_effort=None):
    """Honor aliases and per-model default; reject conflicts and unsupported efforts."""
    if not is_supported_model(model_id):
        raise ValueError("Unsupported model: " + model_id)
    base_id, alias_effort = EFFORT_VARIANTS.get(model_id, (model_id, None))
    if explicit_effort is None or explicit_effort == "default":
        effort = alias_effort or MODEL_DEFAULT_EFFORTS.get(base_id)
    elif not isinstance(explicit_effort, str):
        raise ValueError("Invalid reasoning_effort type")
    else:
        effort = explicit_effort.strip().lower()
        if alias_effort and effort != alias_effort:
            raise ValueError("Conflicting effort between model alias and request")
    if not effort:
        return None
    supported = MODEL_EFFORTS.get(base_id, [])
    if effort not in supported:
        raise ValueError(
            "Unsupported reasoning effort " + effort + " for " + base_id
            + "; supported: " + (", ".join(supported) or "none")
        )
    return effort
# <<< NOTION_REASONING_EFFORT_ALIAS_END
'''.strip()


def validate_candidate(registry: str, caps: dict, mapping: dict) -> int:
    with tempfile.TemporaryDirectory(prefix="notion_effort_audit_") as temp:
        file = Path(temp) / "model_registry.py"
        cap_file = Path(temp) / "notion_reasoning_capabilities.json"
        file.write_text(registry, encoding="utf-8")
        cap_file.write_text(json.dumps(caps, ensure_ascii=False), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("_notion_effort_preset_audit", file)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)

        if any(alias.endswith("-effort-none") for alias in module.EFFORT_VARIANTS):
            raise RuntimeError("A no-thinking alias is still present")

        expected_alias_count = 0
        for internal_code, (external_id, efforts, default) in PRESETS.items():
            if mapping.get(external_id) != internal_code:
                raise RuntimeError("Model mapping changed: " + external_id)
            actual = module.MODEL_EFFORTS.get(external_id, [])
            if actual != list(efforts):
                raise RuntimeError("Effort options mismatch: " + external_id)
            if module.MODEL_DEFAULT_EFFORTS.get(external_id) != default:
                raise RuntimeError("Default effort mismatch: " + external_id)
            if module.resolve_reasoning_effort(external_id) != default:
                raise RuntimeError("Base model does not send its default effort: " + external_id)
            for level in efforts:
                alias = external_id + "-effort-" + level
                expected_alias_count += 1
                if not module.is_supported_model(alias):
                    raise RuntimeError("Missing model alias: " + alias)
                if module.resolve_reasoning_effort(alias) != level:
                    raise RuntimeError("Wrong effort alias: " + alias)
                if module.get_notion_model(alias) != internal_code:
                    raise RuntimeError("Wrong upstream code: " + alias)
            try:
                module.resolve_reasoning_effort(external_id, "none")
            except ValueError:
                pass
            else:
                raise RuntimeError("No-thinking is not rejected: " + external_id)
        return expected_alias_count


def main():
    if len(sys.argv) != 1:
        raise RuntimeError("Run without arguments from ~/notion2api")

    registry_path = Path("app/model_registry.py")
    caps_path = Path("app/notion_reasoning_capabilities.json")
    api_path = Path("app/api/chat.py")
    notion_path = Path("app/notion_client.py")
    for path in (registry_path, caps_path, api_path, notion_path):
        if not path.is_file():
            raise RuntimeError("Missing " + str(path) + "; install the reasoning-effort patch first")

    api_source = api_path.read_text(encoding="utf-8")
    notion_source = notion_path.read_text(encoding="utf-8")
    if "resolve_reasoning_effort" not in api_source or "reasoning_effort=selected_effort" not in api_source:
        raise RuntimeError("Chat API does not forward effort; run enable_notion_reasoning_effort.py first")
    if 'block["value"]["reasoningEffort"] = reasoning_effort' not in notion_source:
        raise RuntimeError("Notion transport does not forward effort; run original patch first")

    original_registry = registry_path.read_text(encoding="utf-8")
    if original_registry.count(BEGIN) != 1 or original_registry.count(END) != 1:
        raise RuntimeError("Reasoning alias block missing or duplicated; run original patch first")

    start = original_registry.index(BEGIN)
    finish = original_registry.index(END) + len(END)
    if finish <= start:
        raise RuntimeError("Invalid alias block boundaries")
    prefix = original_registry[:start]
    namespace = {}
    exec(compile(prefix, str(registry_path), "exec"), namespace)
    mapping = namespace.get("MODEL_MAP")
    if not isinstance(mapping, dict):
        raise RuntimeError("MODEL_MAP is missing")

    original_caps = json.loads(caps_path.read_text(encoding="utf-8"))
    if not isinstance(original_caps, dict):
        raise RuntimeError("Capability cache must be a JSON object")

    updated_caps = {}
    for code, metadata in original_caps.items():
        if not isinstance(metadata, dict):
            raise RuntimeError("Invalid capability entry: " + str(code))
        raw = metadata.get("levels", [])
        if not isinstance(raw, list):
            raise RuntimeError("Invalid reasoning levels: " + str(code))
        valid = list(dict.fromkeys(
            x.lower() for x in raw if isinstance(x, str) and x.lower() in LEVELS
        ))
        default = metadata.get("default")
        updated_caps[code] = dict(metadata, levels=valid,
                                  default=default if default in valid else None)

    for code, (external_id, levels, default) in PRESETS.items():
        if mapping.get(external_id) != code:
            raise RuntimeError("Wrong or unavailable mapping: " + external_id)
        if code not in updated_caps:
            raise RuntimeError("Missing live model metadata: " + external_id)
        available = updated_caps[code]["levels"]
        missing = [effort for effort in levels if effort not in available]
        if missing:
            raise RuntimeError(
                "Notion metadata conflicts with screenshot for " + external_id
                + ": missing " + ", ".join(missing)
                + "; available " + ", ".join(available)
            )
        updated_caps[code]["levels"] = list(levels)
        updated_caps[code]["default"] = default

    candidate_registry = prefix + ALIAS_BLOCK + original_registry[finish:]
    compile(candidate_registry, str(registry_path), "exec")
    alias_count = validate_candidate(candidate_registry, updated_caps, mapping)

    backup = Path("backups") / ("reasoning_presets_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(registry_path, backup / "model_registry.py")
    shutil.copy2(caps_path, backup / "notion_reasoning_capabilities.json")
    registry_path.write_text(candidate_registry, encoding="utf-8")
    caps_path.write_text(
        json.dumps(updated_caps, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8"
    )
    print("SUCCESS: 6 model menus, " + str(alias_count) + " effort aliases checked")
    print("SUCCESS: no-thinking option removed; model defaults are explicit")
    print("SUCCESS: existing authentication, network and transport code untouched")
    print("BACKUP_DIR=" + str(backup))
    print("NEXT: docker compose up -d --build --force-recreate notion-ai")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        sys.exit(1)
