#!/usr/bin/env python3
"""Sync Notion2API model registry and both UIs, and publish port 8000.

Run from the root of the already-patched notion2api repository.
Keeps the curl_cffi repair untouched and creates timestamped backups.
"""

from pathlib import Path
from datetime import datetime
import json
import re
import shutil
import sys

# (external API ID, visible label, Notion internal ID)
CATALOG = {
    "Anthropic": [
        ("claude-sonnet4.6", "Sonnet 4.6", "almond-croissant-low"),
        ("claude-sonnet5", "Sonnet 5", "angel-cake-high"),
        ("claude-sonnet5.5", "Sonnet 5.5", "achira-donut"),
        ("claude-haiku4.5", "Haiku 4.5", "anthropic-haiku-4.5"),
        ("claude-haiku5.5", "Haiku 5.5", "amla-jam"),
        ("claude-opus4.6", "Opus 4.6", "avocado-froyo-medium"),
        ("claude-opus4.7", "Opus 4.7", "apricot-sorbet-high"),
        ("claude-opus4.8", "Opus 4.8", "ambrosia-tart-high"),
        ("claude-opus5", "Opus 5", "agave-flan"),
        ("claude-opus5.5", "Opus 5.5", "albuquerque-quinn"),
    ],
    "OpenAI": [
        ("gpt-6-astra", "GPT-6 Astra", "orlando-quinn"),
        ("gpt-6.1-sol", "GPT-6.1 Sol", "omniberry-sundae"),
        ("gpt-6-luna", "GPT-6 Luna", "omodi"),
        ("gpt-5.6-sol", "GPT-5.6 Sol", "orange-mousse"),
        ("gpt-5.6-terra", "GPT-5.6 Terra", "orchid-muffin"),
        ("gpt-5.6-luna", "GPT-5.6 Luna", "olive-jellyroll"),
        ("gpt-5.2", "GPT-5.2", "oatmeal-cookie"),
        ("gpt-5.4", "GPT-5.4", "oval-kumquat-medium"),
        ("gpt-5.5", "GPT-5.5", "opal-quince-medium"),
        ("gpt-5.4-mini", "GPT-5.4 Mini", "oregon-grape-medium"),
        ("gpt-5.4-nano", "GPT-5.4 Nano", "otaheite-apple-medium"),
    ],
    "Google": [
        ("gemini-3.1pro", "Gemini 3.1 Pro", "galette-medium-thinking"),
        ("gemini-3.5flash", "Gemini 3.5 Flash", "vertex-gemini-3.5-flash"),
        ("gemini-3.6flash", "Gemini 3.6 Flash", "vertex-gemini-3.6-flash"),
        ("gemini-3.7flash", "Gemini 3.7 Flash", "grapefruit-zeppole"),
        ("gemini-3flash", "Gemini 3 Flash", "gingerbread"),
    ],
    "Moonshot": [
        ("kimi-k3", "Kimi K3", "fireworks-kimi-k3"),
    ],
    "xAI": [
        ("grok-4.3", "Grok 4.3", "xigua-mochi-medium"),
        ("grok-4.5", "Grok 4.5", "strawberry-whoopiepie"),
        ("grok-4.6", "Grok 4.6", "soursop-shortcake"),
        ("grok-build0.1", "Grok Build 0.1", "xinomavro-cake"),
    ],
    "DeepSeek": [
        ("deepseek-v4pro", "DeepSeek V4 Pro", "baseten-deepseek-v4-pro"),
        ("deepseek-v4flash", "DeepSeek V4 Flash", "baseten-deepseek-v4-flash"),
        ("deepseek-v4.1flash", "DeepSeek V4.1 Flash", "baseten-deepseek-v4.1-flash"),
    ],
    "Zhipu": [
        ("glm-5.2", "GLM 5.2", "baseten-glm-5.2"),
        ("glm-5.3", "GLM 5.3", "baseten-glm-5.3"),
        ("glm-5.3-flash", "GLM 5.3 Flash", "baseten-glm-5.3-flash"),
    ],
    "Notion": [
        ("fable-5.1", "Fable 5.1", "assam-chai"),
    ],
}

ICONS = {
    "Anthropic": "\u2733",
    "OpenAI": "\u2699",
    "Google": "\u2726",
    "Moonshot": "\u263d",
    "xAI": "\u26a1",
    "DeepSeek": "\u223f",
    "Zhipu": "\u25c6",
    "Notion": "\u25c7",
}

BETA = {
    "claude-sonnet5.5", "claude-haiku5.5", "claude-opus5.5",
    "gpt-6.1-sol", "glm-5.3", "glm-5.3-flash",
}


def replace_py_dict(source, name, value):
    pattern = re.compile(
        r"(?ms)^(" + re.escape(name) + r":\s*dict\[str,\s*str\]\s*=\s*)\{.*?^\}"
    )
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise RuntimeError(f"Cannot find exactly one Python dictionary: {name}")
    m = matches[0]
    return source[:m.start()] + m.group(1) + json.dumps(value, ensure_ascii=False, indent=4) + source[m.end():]


def replace_py_set(source, name, values):
    pattern = re.compile(
        r"(?ms)^" + re.escape(name) + r":\s*set\[str\]\s*=\s*\{.*?^\}"
    )
    replacement = (
        name + ": set[str] = {\n"
        + "".join("    " + json.dumps(v) + ",\n" for v in sorted(values))
        + "}"
    )
    new, count = pattern.subn(lambda _: replacement, source)
    if count != 1:
        raise RuntimeError(f"Cannot find exactly one Python set: {name}")
    return new


def replace_js_field(source, key, data):
    # Only modify top-level dictionary fields of the frontend model constants.
    p = re.compile(r"(?m)^([ \t]*)" + re.escape(key) + r"[ \t]*:[ \t]*")
    matches = list(p.finditer(source))
    if len(matches) != 1:
        raise RuntimeError(f"Cannot find exactly one JavaScript field: {key}")
    m = matches[0]
    start = m.end()
    opener = source[start:start+1]
    if opener not in ("[", "{"):
        raise RuntimeError(f"Unexpected JavaScript field opening: {key}")
    brackets = {"[": "]", "{": "}"}
    stack = [brackets[opener]]
    quote = None
    escaped = False
    end = None
    for i in range(start + 1, len(source)):
        ch = source[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in ('"', "'", "`"):
            quote = ch
        elif ch in brackets:
            stack.append(brackets[ch])
        elif ch in ("}", "]"):
            if not stack or ch != stack.pop():
                raise RuntimeError(f"Unbalanced JS delimiters: {key}")
            if not stack:
                end = i + 1
                break
    if end is None:
        raise RuntimeError(f"Could not scan complete JS field: {key}")
    literal = json.dumps(data, ensure_ascii=False, indent=2)
    literal = literal.replace("\n", "\n" + m.group(1))
    return source[:start] + literal + source[end:]




def read_json_js_field(source, name):
    pattern = re.compile(r"(?m)^[ \t]*" + re.escape(name) + r"[ \t]*:[ \t]*")
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one JSON-style JavaScript field {name}; found {len(matches)}")
    try:
        value, _ = json.JSONDecoder().raw_decode(source[matches[0].end():])
        return value
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cannot parse frontend JSON field {name}") from exc


def verify_all_models(reg, html, js, compose, client, flat):
    # One exhaustive offline audit of all known model IDs and both frontend menus.
    namespace = {}
    exec(compile(reg, 'app/model_registry.py', 'exec'), namespace)
    if len(flat) != 38:
        raise RuntimeError(f"Unexpected enabled model count: {len(flat)}")
    expected_map = {model: code for provider, model, label, code in flat}
    expected_names = {model: label for provider, model, label, code in flat}
    expected_icons = {model: ICONS[provider] for provider, model, label, code in flat}
    expected_providers = {model: provider for provider, model, label, code in flat}
    ids = list(expected_map)
    if len(set(ids)) != 38 or len(set(expected_map.values())) != 38:
        raise RuntimeError('Duplicate external or upstream model ID')

    for field, expected in (
        ('MODEL_MAP', expected_map),
        ('DISPLAY_NAMES', expected_names),
        ('MODEL_ICONS', expected_icons),
    ):
        if namespace[field] != expected:
            raise RuntimeError(f'Backend catalog mismatch: {field}')

    for model_id, code in expected_map.items():
        if namespace['get_notion_model'](model_id) != code:
            raise RuntimeError(f'First conversion failed: {model_id}')
        if namespace['get_notion_model'](code) != code:
            raise RuntimeError(f'Double conversion fell back: {model_id} -> {code}')
        if namespace['get_standard_model'](code) != model_id:
            raise RuntimeError(f'Reverse mapping failed: {model_id}')
        if namespace['get_standard_model'](model_id) != model_id:
            raise RuntimeError(f'External ID lookup failed: {model_id}')
        if namespace['get_display_name'](model_id) != expected_names[model_id]:
            raise RuntimeError(f'Display name mismatch: {model_id}')
        if not namespace['is_supported_model'](model_id):
            raise RuntimeError(f'Unsupported catalog model: {model_id}')
        thread = 'markdown-chat' if code in namespace['MARKDOWN_CHAT_MODELS'] else 'workflow'
        for name in (model_id, code):
            if namespace['get_thread_type'](name) != thread:
                raise RuntimeError(f'Thread type mismatch: {name}')

    for invalid in ('this-is-not-a-model', 'fable-5'):
        if namespace['is_supported_model'](invalid):
            raise RuntimeError(f'Invalid/disabled model was enabled: {invalid}')
        for func_name in ('get_notion_model', 'get_standard_model'):
            try:
                namespace[func_name](invalid)
            except ValueError:
                continue
            raise RuntimeError(f'{func_name} silently accepted invalid model {invalid}')

    if namespace['list_available_models']() != ids:
        raise RuntimeError('Listed model IDs do not match catalog order')

    for filename, content in (
        ('frontend/index.html', html),
        ('frontend/js/core/constants.js', js),
    ):
        groups = read_json_js_field(content, 'MODEL_GROUPS')
        models = read_json_js_field(content, 'MODELS')
        displays = read_json_js_field(content, 'MODEL_DISPLAY_NAMES')
        if models != [{'id': model, 'label': expected_names[model]} for model in ids]:
            raise RuntimeError(f'{filename}: flat model dropdown differs from backend')
        if displays != expected_names:
            raise RuntimeError(f'{filename}: displayed model names differ from backend')
        seen = {}
        for group in groups:
            provider = group['label']
            for item in group['models']:
                model_id = item['id']
                if model_id in seen:
                    raise RuntimeError(f'{filename}: duplicate {model_id}')
                seen[model_id] = provider
                if item['label'] != expected_names.get(model_id):
                    raise RuntimeError(f'{filename}: model label mismatch: {model_id}')
                if filename.endswith('index.html') and item.get('provider') != provider:
                    raise RuntimeError(f'{filename}: provider mismatch: {model_id}')
                if filename.endswith('constants.js') and item.get('icon') != expected_icons.get(model_id):
                    raise RuntimeError(f'{filename}: icon mismatch: {model_id}')
        if seen != expected_providers:
            raise RuntimeError(f'{filename}: model group membership differs from backend')
        field, expected = (
            ('MODEL_PROVIDERS', expected_providers) if filename.endswith('index.html')
            else ('MODEL_ICONS', expected_icons)
        )
        if read_json_js_field(content, field) != expected:
            raise RuntimeError(f'{filename}: {field} mismatch')
        if 'fable-5' in seen:
            raise RuntimeError('Disabled Fable 5 appeared in frontend')

    if '"0.0.0.0:${HOST_PORT:-8000}:8000"' not in compose:
        raise RuntimeError('Docker port is not open as requested')
    if '"event": "notion_model_selected"' not in client:
        raise RuntimeError('Missing safe outgoing model instrumentation')
    compile(client, 'app/notion_client.py', 'exec')
    print('AUDIT PASS: 38 unique model IDs and upstream codes')
    print('AUDIT PASS: two-stage conversion and reverse mapping, all 38 models')
    print('AUDIT PASS: model thread routing, both frontend menus, API registry')
    print('AUDIT PASS: unknown/disabled models reject instead of falling back')
    print('AUDIT PASS: port mapping, safe upstream-model logging')

def main():
    root = Path.cwd()
    paths = [
        Path("app/model_registry.py"),
        Path("frontend/index.html"),
        Path("frontend/js/core/constants.js"),
        Path("docker-compose.yml"),
        Path("app/notion_client.py"),
    ]
    if not all(path.is_file() for path in paths):
        raise RuntimeError("Run this script inside the notion2api repository root")
    originals = {p: p.read_text(encoding="utf-8") for p in paths}

    flat = [(provider, *item) for provider, items in CATALOG.items() for item in items]
    ids = [item[1] for item in flat]
    upstream_ids = [item[3] for item in flat]
    if len(flat) != 38 or len(set(ids)) != 38 or len(set(upstream_ids)) != 38:
        raise RuntimeError("Expected 38 distinct enabled models")

    model_map = {model_id: upstream for provider, model_id, label, upstream in flat}
    display = {model_id: label for provider, model_id, label, upstream in flat}
    icons = {model_id: ICONS[provider] for provider, model_id, label, upstream in flat}
    providers = {model_id: provider for provider, model_id, label, upstream in flat}
    js_groups = []
    html_groups = []
    for provider, entries in CATALOG.items():
        js_models, html_models = [], []
        for model_id, label, upstream in entries:
            js_model = {"id": model_id, "label": label, "icon": ICONS[provider]}
            html_model = {"id": model_id, "label": label, "provider": provider}
            if model_id in BETA:
                js_model["badge"] = "Beta"
                html_model["badge"] = "Beta"
            js_models.append(js_model)
            html_models.append(html_model)
        js_groups.append({"label": provider, "models": js_models})
        html_groups.append({"label": provider, "models": html_models})
    flat_js = [{"id": model_id, "label": label} for _, model_id, label, _ in flat]

    reg_path, html_path, js_path, compose_path, client_path = paths
    if sys.argv[1:] == ['--check']:
        verify_all_models(
            originals[reg_path], originals[html_path], originals[js_path],
            originals[compose_path], originals[client_path], flat,
        )
        return
    if len(sys.argv) > 1:
        raise RuntimeError('Unknown option. Use --check or no argument')
    reg = originals[reg_path]
    for key, obj in (("MODEL_MAP", model_map), ("DISPLAY_NAMES", display), ("MODEL_ICONS", icons)):
        reg = replace_py_dict(reg, key, obj)
    reg = replace_py_set(reg, "MARKDOWN_CHAT_MODELS", {
        "vertex-gemini-3.5-flash", "vertex-gemini-3.6-flash"
    })
    # Standard/Lite already convert model IDs. The next conversion must
    # preserve recognized Notion codenames, never silently choose Sonnet 4.6.
    legacy_resolver = (
        'def get_notion_model(model_name: str) -> str:\n'
        '    return MODEL_MAP.get(model_name, MODEL_MAP[DEFAULT_MODEL])'
    )
    idempotent_resolver = (
        'def get_notion_model(model_name: str) -> str:\n'
        '    if model_name in NOTION_MODEL_REVERSE_MAP:\n'
        '        return model_name\n'
        '    return MODEL_MAP.get(model_name, MODEL_MAP[DEFAULT_MODEL])'
    )
    strict_resolver = (
        'def get_notion_model(model_name: str) -> str:\n'
        '    if model_name in MODEL_MAP:\n'
        '        return MODEL_MAP[model_name]\n'
        '    if model_name in NOTION_MODEL_REVERSE_MAP:\n'
        '        return model_name\n'
        '    raise ValueError(f"Unsupported model: {model_name}")'
    )
    if strict_resolver not in reg:
        if legacy_resolver in reg:
            reg = reg.replace(legacy_resolver, strict_resolver, 1)
        elif idempotent_resolver in reg:
            reg = reg.replace(idempotent_resolver, strict_resolver, 1)
        else:
            raise RuntimeError('Cannot identify get_notion_model resolver')

    legacy_standard = (
        'def get_standard_model(model_name: str) -> str:\n'
        '    if model_name in MODEL_MAP:\n'
        '        return model_name\n'
        '    return NOTION_MODEL_REVERSE_MAP.get(model_name, DEFAULT_MODEL)'
    )
    strict_standard = (
        'def get_standard_model(model_name: str) -> str:\n'
        '    if model_name in MODEL_MAP:\n'
        '        return model_name\n'
        '    if model_name in NOTION_MODEL_REVERSE_MAP:\n'
        '        return NOTION_MODEL_REVERSE_MAP[model_name]\n'
        '    raise ValueError(f"Unsupported model: {model_name}")'
    )
    if strict_standard not in reg:
        if legacy_standard not in reg:
            raise RuntimeError('Cannot identify get_standard_model resolver')
        reg = reg.replace(legacy_standard, strict_standard, 1)

    compile(reg, str(reg_path), "exec")

    html = originals[html_path]
    for key, data in (("MODEL_GROUPS", html_groups), ("MODELS", flat_js),
                      ("MODEL_DISPLAY_NAMES", display), ("MODEL_PROVIDERS", providers)):
        html = replace_js_field(html, key, data)

    js = originals[js_path]
    for key, data in (("MODEL_GROUPS", js_groups), ("MODELS", flat_js),
                      ("MODEL_DISPLAY_NAMES", display), ("MODEL_ICONS", icons)):
        js = replace_js_field(js, key, data)

    client = originals[client_path]
    log_anchor = '        notion_transcript = self._to_notion_transcript(transcript)\n'
    marker = '"event": "notion_model_selected"'
    if marker not in client:
        if client.count(log_anchor) != 1:
            raise RuntimeError('Cannot locate Notion upstream conversion point')
        log_code = (
            '        upstream_model = next((block["value"].get("model")\n'
            '            for block in notion_transcript\n'
            '            if block.get("type") == "config" and isinstance(block.get("value"), dict)), None)\n'
            '        logger.info("Selected upstream model", extra={"request_info": {\n'
            '            "event": "notion_model_selected", "notion_model": upstream_model\n'
            '        }})\n'
        )
        client = client.replace(log_anchor, log_anchor + log_code, 1)
    compile(client, str(client_path), 'exec')

    compose = originals[compose_path]
    port_pattern = re.compile(
        r'(?m)^(\s*-\s*)"(?:(?:127\.0\.0\.1|0\.0\.0\.0):)?\$\{HOST_PORT:-8000\}:8000"([ \t]*(?:#.*)?)$'
    )
    compose, count = port_pattern.subn(
        lambda m: m.group(1) + '"0.0.0.0:${HOST_PORT:-8000}:8000"' + m.group(2),
        compose,
    )
    if count != 1:
        raise RuntimeError("Docker port mapping was not in the expected format")

    updates = {
        reg_path: reg,
        html_path: html,
        js_path: js,
        compose_path: compose,
        client_path: client,
    }
    verify_all_models(reg, html, js, compose, client, flat)

    backup_dir = root / "backups" / ("models_port_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    for p in paths:
        target = backup_dir / p
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)

    for p, text in updates.items():
        p.write_text(text, encoding="utf-8")

    print("UPDATED: 38 enabled models; Fable 5 omitted because upstream disabled it")
    print("UPDATED: model registry + both frontends + public Docker port")
    print("FIXED: all 38 models reject silent fallback; log actual outgoing codename")
    print("FIXED: prevent internal model codes from falling back to Sonnet 4.6")
    print("PRESERVED: working curl_cffi transport, .env, accounts.json, data")
    print("BACKUP_DIR=" + str(backup_dir))
    print("NEXT: docker compose config -q && docker compose up -d --build --force-recreate notion-ai")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)