#!/usr/bin/env python3
"""Enable model-specific Notion reasoning effort in an existing notion2api VPS deployment.

Run from the notion2api project root while the existing notion-ai service is healthy:
    python3 enable_notion_reasoning_effort.py

This queries modelConfiguration.supportedReasoningEfforts using the account already
inside your running container. No credentials are copied to GitHub or printed.
It registers optional model variants such as claude-opus5.5-effort-high so that
Cherry Studio can change effort by switching models even if its knob is hidden.
"""

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LEVELS = ("low", "medium", "high", "xhigh", "max")
MARKER_BEGIN = "# >>> NOTION_REASONING_EFFORT_ALIAS_BEGIN"
MARKER_END = "# <<< NOTION_REASONING_EFFORT_ALIAS_END"

# This code runs *inside the existing container*, reusing its active credentials
# and browser-compatible curl_cffi client. It prints only public catalog facts.
CONTAINER_PROBE = r'''
import json, os
from pathlib import Path
from curl_cffi import requests

accounts = json.loads(Path('/app/accounts.json').read_text(encoding='utf-8'))
if not isinstance(accounts, list) or not accounts:
    raise SystemExit('Account list unavailable')
a = accounts[0]
root = os.getenv('NOTION_URL','https://www.notion.so').rstrip('/')
jar = dict(a.get('cookies') or {})
jar['token_v2'] = a['token_v2']
jar['notion_user_id'] = a['user_id']
headers = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'Origin': root,
    'Referer': root + '/chat',
    'x-notion-space-id': a['space_id'],
    'x-notion-active-user-header': a['user_id'],
    'notion-client-version': os.getenv('NOTION_CLIENT_VERSION','23.13.20260228.0625'),
    'Cookie': '; '.join(f'{k}={v}' for k,v in jar.items() if v),
}
r = requests.post(root+'/api/v3/getAvailableModels', headers=headers,
                  json={'spaceId': a['space_id']}, impersonate='chrome', timeout=35)
if r.status_code != 200:
    raise SystemExit('getAvailableModels failed, HTTP '+str(r.status_code))
response = r.json()
items = response.get('models', []) if isinstance(response,dict) else response
if not isinstance(items,list) or len(items) < 20:
    raise SystemExit('Unexpected getAvailableModels catalog shape')
levels = set(('low','medium','high','xhigh','max'))
out = {}
for entry in items:
    if not isinstance(entry,dict):
        continue
    if entry.get('isDisabled') or (entry.get('workflow') or {}).get('isDisabled'):
        continue
    cfg = entry.get('modelConfiguration') or {}
    raw = cfg.get('supportedReasoningEfforts') or []
    if not isinstance(raw,list):
        continue
    valid = []
    for item in raw:
        v = item if isinstance(item,str) else item.get('reasoningEffort', item.get('effort')) if isinstance(item,dict) else None
        if isinstance(v,str) and v.lower() in levels and v.lower() not in valid:
            valid.append(v.lower())
    code = (entry.get('workflow') or {}).get('finalModelName') or entry.get('model')
    if isinstance(code,str) and code and valid:
        default = cfg.get('defaultReasoningEffort')
        out[code] = {'levels':valid, 'default': default if default in valid else None}
if not out:
    raise SystemExit('No reasoning levels reported by upstream; not patching')
print(json.dumps(out,ensure_ascii=False,sort_keys=True))
'''


ALIAS_SOURCE = r'''
# >>> NOTION_REASONING_EFFORT_ALIAS_BEGIN
# These aliases expose each supported effort as a distinct model ID for
# clients whose custom-model reasoning selector is hidden (e.g. Cherry Studio).
# Levels are retrieved from Notion getAvailableModels, not guessed.
import json as _reasoning_json
from pathlib import Path as _ReasoningPath
_REASONING_FILE = _ReasoningPath(__file__).with_name('notion_reasoning_capabilities.json')
_REASONING_RAW = _reasoning_json.loads(_REASONING_FILE.read_text(encoding='utf-8'))
MODEL_EFFORTS = {}
EFFORT_VARIANTS = {}
for _base_id, _upstream_code in list(MODEL_MAP.items()):
    _cfg = _REASONING_RAW.get(_upstream_code, {})
    _levels = list(_cfg.get('levels', []))
    if not _levels:
        continue
    MODEL_EFFORTS[_base_id] = _levels
    for _level in _levels:
        _alias = _base_id + '-effort-' + _level
        if _alias in MODEL_MAP:
            raise RuntimeError('Reasoning alias collision: ' + _alias)
        EFFORT_VARIANTS[_alias] = (_base_id, _level)
        MODEL_MAP[_alias] = _upstream_code
        DISPLAY_NAMES[_alias] = DISPLAY_NAMES[_base_id] + ' / ' + _level.upper()
        MODEL_ICONS[_alias] = MODEL_ICONS[_base_id]


def resolve_reasoning_effort(model_id: str, explicit_effort=None):
    """Get supported requested effort; reject silent fallback and conflicts."""
    if not is_supported_model(model_id):
        raise ValueError('Unsupported model: ' + model_id)
    base_id, alias_effort = EFFORT_VARIANTS.get(model_id, (model_id, None))
    if explicit_effort is None or explicit_effort == 'default':
        effort = alias_effort
    elif not isinstance(explicit_effort, str):
        raise ValueError('Invalid reasoning_effort type')
    else:
        effort = explicit_effort.lower()
        if alias_effort and effort != alias_effort:
            raise ValueError('Conflicting effort between model alias and request')
    if not effort:
        return None
    available = MODEL_EFFORTS.get(base_id, [])
    if effort not in available:
        raise ValueError('Unsupported reasoning effort ' + effort + ' for ' + base_id
                         + '; supported: ' + (', '.join(available) or 'none'))
    return effort
# <<< NOTION_REASONING_EFFORT_ALIAS_END
'''


def replace_once(src, old, new, label):
    n = src.count(old)
    if new in src:
        return src  # repeat application: already updated
    if n != 1:
        raise RuntimeError('%s: expected one match, got %d' % (label, n))
    return src.replace(old, new, 1)


def fetch_catalog():
    cmd = ['docker','compose','exec','-T','notion-ai','python','-c',CONTAINER_PROBE]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=65)
    if completed.returncode:
        err = (completed.stderr or completed.stdout).strip()
        # In particular, never print HTTP response bodies or auth headers.
        raise RuntimeError('Catalog query failed. '+err[-400:])
    try:
        data = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError('Could not parse model capability metadata') from exc
    if not isinstance(data,dict):
        raise RuntimeError('Catalog capability metadata must be a JSON object')
    return data


def main():
    root = Path.cwd()
    paths = [Path(p) for p in (
        'app/model_registry.py', 'app/schemas.py', 'app/api/chat.py',
        'app/notion_client.py')]
    if not all(p.is_file() for p in paths):
        raise RuntimeError('Run from the notion2api project root')
    original = {p:p.read_text(encoding='utf-8') for p in paths}
    if 'if model_name in NOTION_MODEL_REVERSE_MAP:' not in original[paths[0]]:
        raise RuntimeError('Please apply the full model ID correction before this patch')

    caps = fetch_catalog()
    # Keep only model IDs in the active base registry (the last patch exposes 38).
    reg = original[paths[0]]
    # Only base model IDs: existing aliases (if installed) are dynamically appended
    # and are not literal MODEL_MAP entries in source.
    imports = {}
    # Executing this module is safe; it contains static model registry declarations.
    # For idempotent repeat installation, the existing capabilities file must exist.
    if MARKER_BEGIN in reg:
        reg_preview = reg[:reg.index(MARKER_BEGIN)]
    else:
        reg_preview = reg
    exec(compile(reg_preview, str(paths[0]), 'exec'), imports)
    base_map = imports['MODEL_MAP']
    matched = {upstream:caps[upstream] for upstream in set(base_map.values()) if upstream in caps}
    if not matched:
        raise RuntimeError('No reasoning-capable model IDs overlap with the 38 deployed models')
    for code,entry in matched.items():
        if not isinstance(entry,dict) or not all(x in LEVELS for x in entry.get('levels',[])):
            raise RuntimeError('Invalid catalog efforts for ' + code)

    if MARKER_BEGIN not in reg:
        reg = reg.rstrip() + '\n\n' + ALIAS_SOURCE + '\n'
    elif reg.count(MARKER_BEGIN) != 1 or reg.count(MARKER_END) != 1:
        raise RuntimeError('Duplicate reasoning alias sections')

    schema = original[paths[1]]
    schema = replace_once(
        schema,
        'from pydantic import BaseModel, Field, field_validator',
        'from pydantic import BaseModel, Field, field_validator, AliasChoices',
        'Import AliasChoices',
    )
    schema = replace_once(
        schema,
        '    conversation_id: Optional[str] = Field(default=None, description="Extension for stateful conversation tracking.")',
        '    conversation_id: Optional[str] = Field(default=None, description="Extension for stateful conversation tracking.")\n'
        '    reasoning_effort: Optional[str] = Field(default=None, validation_alias=AliasChoices("reasoning_effort", "reasoningEffort"))',
        'Reasoning field',
    )

    chat = original[paths[2]]
    chat = replace_once(
        chat,
        'from app.model_registry import is_supported_model, list_available_models',
        'from app.model_registry import is_supported_model, list_available_models, resolve_reasoning_effort',
        'Import effort resolver',
    )
    # All three paths (Lite, Standard, Heavy) allocate their pool in this way.
    pool_line = '    pool = request.app.state.account_pool\n'
    injection = (
        '    pool = request.app.state.account_pool\n'
        '    try:\n'
        '        selected_effort = resolve_reasoning_effort(req_body.model, req_body.reasoning_effort)\n'
        '    except ValueError as exc:\n'
        '        raise HTTPException(status_code=400, detail=str(exc)) from exc\n'
    )
    if chat.count('selected_effort = resolve_reasoning_effort') == 0:
        if chat.count(pool_line) != 3:
            raise RuntimeError('Unexpected Lite/Standard/Heavy handler structure')
        chat = chat.replace(pool_line, injection)
    elif chat.count('selected_effort = resolve_reasoning_effort') != 3:
        raise RuntimeError('Partial effort patch found in request handlers')
    chat = replace_once(chat, 'from app.model_registry import is_supported_model, list_available_models',
                        'from app.model_registry import is_supported_model, list_available_models, resolve_reasoning_effort',
                        'Second import guard') if 'resolve_reasoning_effort' not in chat.split('from app.model_registry import ')[1].split('\n')[0] else chat
    if 'thread_id=None, reasoning_effort=selected_effort' not in chat:
        if chat.count('client.stream_response(transcript, thread_id=None)') != 2:
            raise RuntimeError('Expected two stateless stream call sites')
        chat = chat.replace('client.stream_response(transcript, thread_id=None)',
                            'client.stream_response(transcript, thread_id=None, reasoning_effort=selected_effort)')
    if 'thread_id=thread_id, reasoning_effort=selected_effort' not in chat:
        if chat.count('client.stream_response(transcript, thread_id=thread_id)') != 1:
            raise RuntimeError('Expected one stateful stream call site')
        chat = chat.replace('client.stream_response(transcript, thread_id=thread_id)',
                            'client.stream_response(transcript, thread_id=thread_id, reasoning_effort=selected_effort)')
    # Heavy mode threads pin the original effort; switching levels requires a new thread.
    chat = replace_once(chat,
        'if not bound_model or bound_model != req_body.model:',
        'if not bound_model or bound_model != f"{req_body.model}|{selected_effort or \'default\'}":',
        'Heavy thread model+effort comparison')
    chat = replace_once(chat,
        '                    model_name=req_body.model,\n                )\n\n            if first_item is None:',
        '                    model_name=f"{req_body.model}|{selected_effort or \'default\'}",\n                )\n\n            if first_item is None:',
        'Heavy thread model+effort persistence')

    client = original[paths[3]]
    client = replace_once(client,
        'def stream_response(self, transcript: list, thread_id: Optional[str] = None)',
        'def stream_response(self, transcript: list, thread_id: Optional[str] = None, reasoning_effort: Optional[str] = None)',
        'Notion stream signature')
    client = replace_once(client,
        '        notion_transcript = self._to_notion_transcript(transcript)\n',
        '        notion_transcript = self._to_notion_transcript(transcript)\n'
        '        if reasoning_effort:\n'
        '            for block in notion_transcript:\n'
        '                if block.get("type") == "config" and isinstance(block.get("value"), dict):\n'
        '                    block["value"]["reasoningEffort"] = reasoning_effort\n'
        '                    logger.info("Notion reasoning effort selected", extra={"request_info": {\n'
        '                        "event": "notion_reasoning_effort",\n'
        '                        "notion_model": block["value"].get("model"),\n'
        '                        "reasoning_effort": reasoning_effort,\n'
        '                    }})\n'
        '                    break\n',
        'Inject Notion config reasoningEffort')

    edits = {paths[0]:reg,paths[1]:schema,paths[2]:chat,paths[3]:client}
    cap_path = Path('app/notion_reasoning_capabilities.json')
    cap_content = json.dumps(matched,ensure_ascii=False,indent=2,sort_keys=True)+'\n'
    for p,s in edits.items():
        compile(s, str(p), 'exec')
    # Verify the new registry is importable in a synthetic workspace without secrets.
    # Existing project may not have dependencies on host; registry imports only stdlib.
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix='n2a_effort_check_') as tmp:
        test_dir = Path(tmp)
        (test_dir/'model_registry.py').write_text(reg,encoding='utf-8')
        (test_dir/'notion_reasoning_capabilities.json').write_text(cap_content,encoding='utf-8')
        import importlib.util
        spec = importlib.util.spec_from_file_location('_n2a_effort_check', test_dir/'model_registry.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        variants = module.EFFORT_VARIANTS
        for alias,(base,effort) in variants.items():
            assert module.is_supported_model(alias)
            assert module.get_notion_model(alias) == module.get_notion_model(base)
            assert module.resolve_reasoning_effort(alias,None) == effort
            assert module.resolve_reasoning_effort(base,effort) == effort
        assert len(variants) >= 5, 'Unexpectedly few effort variants'
        try:
            module.resolve_reasoning_effort('not-a-model', 'high')
        except ValueError:
            pass
        else:
            raise RuntimeError('Invalid model unexpectedly accepted')
        print('PREVIEW: %d models with selectable effort, %d model variants' %
              (len(module.MODEL_EFFORTS), len(variants)))

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = Path('backups')/('reasoning_effort_'+stamp)
    for p in paths:
        dest = backup_dir/p
        dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(p,dest)
    if cap_path.is_file():
        dest=backup_dir/cap_path
        dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(cap_path,dest)
    for p,content in edits.items():
        p.write_text(content,encoding='utf-8')
    cap_path.write_text(cap_content,encoding='utf-8')
    print('PATCH SUCCESS: reasoning_effort / reasoningEffort propagated to Notion')
    print('PATCH SUCCESS: model variants available through /v1/models')
    print('PATCH SUCCESS: per-model supported levels validated; no silent fallback')
    print('PRESERVED: curl_cffi, existing credentials, model mapping and data')
    print('BACKUP_DIR='+str(backup_dir))
    print('NEXT: docker compose up -d --build --force-recreate notion-ai')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERROR:',str(exc),file=sys.stderr)
        sys.exit(1)
