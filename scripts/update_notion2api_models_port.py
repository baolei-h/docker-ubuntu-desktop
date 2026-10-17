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


def main():
    root = Path.cwd()
    paths = [
        Path("app/model_registry.py"),
        Path("frontend/index.html"),
        Path("frontend/js/core/constants.js"),
        Path("docker-compose.yml"),
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

    reg_path, html_path, js_path, compose_path = paths
    reg = originals[reg_path]
    for key, obj in (("MODEL_MAP", model_map), ("DISPLAY_NAMES", display), ("MODEL_ICONS", icons)):
        reg = replace_py_dict(reg, key, obj)
    reg = replace_py_set(reg, "MARKDOWN_CHAT_MODELS", {
        "vertex-gemini-3.5-flash", "vertex-gemini-3.6-flash"
    })
    compile(reg, str(reg_path), "exec")

    html = originals[html_path]
    for key, data in (("MODEL_GROUPS", html_groups), ("MODELS", flat_js),
                      ("MODEL_DISPLAY_NAMES", display), ("MODEL_PROVIDERS", providers)):
        html = replace_js_field(html, key, data)

    js = originals[js_path]
    for key, data in (("MODEL_GROUPS", js_groups), ("MODELS", flat_js),
                      ("MODEL_DISPLAY_NAMES", display), ("MODEL_ICONS", icons)):
        js = replace_js_field(js, key, data)

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
    }
    backup_dir = root / "backups" / ("models_port_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    for p in paths:
        target = backup_dir / p
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)

    for p, text in updates.items():
        p.write_text(text, encoding="utf-8")

    print("UPDATED: 38 enabled models; Fable 5 omitted because upstream disabled it")
    print("UPDATED: model registry + both frontends + public Docker port")
    print("PRESERVED: working curl_cffi fix, .env, accounts.json, data")
    print("BACKUP_DIR=" + str(backup_dir))
    print("NEXT: docker compose config -q && docker compose up -d --build --force-recreate notion-ai")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e, file=sys.stderr)
        sys.exit(1)