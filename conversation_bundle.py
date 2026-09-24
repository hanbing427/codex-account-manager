"""Portable conversation metadata and referenced/project file collection."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

import migrate as m

HISTORY_TABLES = ('thread_turns', 'thread_items', 'thread_history_projection_state', 'thread_realtime_items')
STATE_TABLES = ('threads', 'projects', 'project_roots', 'thread_sections', 'thread_attachments')
THREAD_MAPS = ('thread-project-assignments', 'thread-projectless-output-directories', 'thread-workspace-root-hints',
               'thread-project-membership-host-ids', 'electron-thread-read-state-v1')


def rows(conn, table):
    if not conn.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone():
        return []
    return [dict(row) for row in conn.execute('select * from "' + table + '"')]


def selected_rows(conn, table, key, ids):
    if not ids or not conn.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone():
        return []
    result, values = [], list(ids)
    for start in range(0, len(values), 500):
        part = values[start:start + 500]
        result += [dict(row) for row in conn.execute(f'select * from "{table}" where "{key}" in (' + ','.join('?' for _ in part) + ')', part)]
    return result


def capture(root, records, include_history=True):
    root = Path(root)
    ids = {r['thread_id'] for r in records}
    result = {'state': {t: [] for t in STATE_TABLES}, 'history': {t: [] for t in HISTORY_TABLES}, 'ui': {}}
    for db in sorted(root.glob('state_*.sqlite')):
        with closing(m.connect(db, True)) as conn:
            threads = selected_rows(conn, 'threads', 'id', ids)
            result['state']['threads'] += threads
            projects = {r.get('project_id') for r in threads} - {None, ''}
            sections = {r.get('thread_section_id') for r in threads} - {None, ''}
            for table, key, values in (('projects', 'id', projects), ('project_roots', 'project_id', projects),
                                       ('thread_sections', 'id', sections), ('thread_attachments', 'thread_id', ids)):
                result['state'][table] += selected_rows(conn, table, key, values)
    for db in sorted(root.glob('thread_history_*.sqlite')) if include_history else []:
        m.require(not db.is_symlink() and db.resolve().parent == root.resolve(), '对话数据库路径不安全。')
        with closing(m.connect(db, True)) as conn:
            for table in HISTORY_TABLES:
                result['history'][table] += selected_rows(conn, table, 'thread_id', ids)
    state_path = root / '.codex-global-state.json'
    if state_path.exists():
        source = m.load(state_path)
        ui = result['ui']
        for key in THREAD_MAPS:
            value = source.get(key, {})
            if isinstance(value, dict):
                ui[key] = {k: v for k, v in value.items() if k in ids}
        project_ids = {r.get('project_id') for r in result['state']['threads']} - {None, ''}
        for item in ui.get('thread-project-assignments', {}).values():
            if isinstance(item, dict) and item.get('projectKind') in ('local', 'codex', None):
                project_ids.add(item.get('projectId'))
        project_map = source.get('app-server-project-id-by-legacy-project-id-by-host', {}).get('local:' + str(root.resolve()), {})
        if isinstance(project_map, dict):
            ui['legacy-project-map'] = {old: new for old, new in project_map.items() if old in project_ids or new in project_ids}
            project_ids.update(ui['legacy-project-map'])
            project_ids.update(ui['legacy-project-map'].values())
        for key in ('local-projects', 'project-appearances'):
            value = source.get(key, {})
            if isinstance(value, dict):
                ui[key] = {k: v for k, v in value.items() if k in project_ids}
        ui['project-order'] = [p for p in source.get('project-order', []) if p in project_ids]
        ui['projectless-thread-ids'] = [p for p in source.get('projectless-thread-ids', []) if p in ids]
        ui['sidebar-project-thread-orders'] = {k: [i for i in v if i in ids] for k, v in source.get('sidebar-project-thread-orders', {}).items()
                                             if k in project_ids and isinstance(v, list)}
    return result


def project_list(root, records, metadata=None):
    metadata = metadata or capture(root, records, include_history=False)
    projects = {}
    thread_projects = {r['id']: r.get('project_id') for r in metadata['state']['threads']}
    for project in metadata['state']['projects']:
        roots = [r['path'] for r in metadata['state']['project_roots'] if r['project_id'] == project['id']]
        projects[project['id']] = {'id': project['id'], 'name': project['name'], 'roots': roots, 'ids': []}
    for pid, project in metadata['ui'].get('local-projects', {}).items():
        projects.setdefault(pid, {'id': pid, 'name': project.get('name', pid), 'roots': project.get('rootPaths', []), 'ids': []})
    for record in records:
        assignment = metadata['ui'].get('thread-project-assignments', {}).get(record['thread_id'], {})
        pid = thread_projects.get(record['thread_id']) or assignment.get('projectId')
        if not pid:
            pid = 'cwd-' + hashlib.sha256(record['cwd'].encode()).hexdigest()[:16]
        if pid not in projects:
            cwd = record['cwd']
            projects[pid] = {'id': pid, 'name': Path(cwd).name or '无项目目录', 'roots': [cwd] if cwd else [], 'ids': []}
        projects[pid]['ids'].append(record['id'])
    return [p for p in projects.values() if p['ids']]


def strings(value):
    if isinstance(value, str):
        yield value
        if value.startswith(('{', '[')):
            try:
                nested = json.loads(value)
                if isinstance(nested, (dict, list)):
                    yield from strings(nested)
            except ValueError:
                pass
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


LINK = re.compile(r'!?\[[^\]\n]*\]\((?:<([^>]+)>|([^\s)]+))\)')
ABSOLUTE = re.compile(r'(?:[A-Za-z]:[\\/]|/(?:mnt|home|Users|tmp|var|workspace|workspaces)/)[^\s<>"\x00]+')


def local_path(value, cwd):
    value = unquote(value.strip().strip('`"<>'))
    if value.startswith(('http:', 'https:', 'data:', 'app:', 'codex:', 'plugin:')):
        return None
    if value.startswith('file:'):
        parsed = urlparse(value)
        if parsed.netloc not in ('', 'localhost'):
            return None
        value = parsed.path
        if os.name == 'nt' and re.match(r'^/[A-Za-z]:', value):
            value = value[1:]
    if value.startswith('sandbox:'):
        value = value[8:]
    value = re.sub(r':\d+(?::\d+)?$', '', value.split('#')[0])
    path = Path(value).expanduser()
    if not path.is_absolute():
        if not cwd or not Path(cwd).is_absolute():
            return None
        path = Path(cwd) / path
    return path


def collect(root, records, project_ids=None, extra_paths=None):
    metadata = capture(root, records)
    projects = project_list(root, records, metadata)
    selected_projects = [p for p in projects if p['id'] in (project_ids or [])]
    m.require(not project_ids or set(project_ids) <= {p['id'] for p in projects}, '项目已变化，请重新预览。')
    assets, missing, omitted = {}, set(), set()
    roots = []

    def add(path, thread_ids, explicit=False):
        try:
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                omitted.add(str(path) + '（链接未打包）'); return
            if not path.is_file():
                if not path.exists():
                    missing.add(str(path))
                return
            resolved = path.resolve()
            # Never automatically collect credential files from the Codex home.
            if resolved.is_relative_to(Path(root).resolve()) and (resolved.name.startswith(('auth', 'config')) or
                    resolved.is_relative_to(Path(root).resolve() / 'account-manager')):
                omitted.add(str(path) + '（账号配置或管理器内部文件）'); return
            key = str(resolved)
            entry = assets.setdefault(key, {'source': key, 'bytes': resolved.stat().st_size, 'thread_ids': set(), 'references': set()})
            entry['thread_ids'].update(thread_ids)
            entry['references'].add(str(path))
            m.require(len(assets) <= 50000, '文件超过 50000 个，请拆分导出。')
        except OSError:
            omitted.add(str(path) + '（无法读取）')

    by_id = {r['thread_id']: r for r in records}
    def scan(text, record):
        candidates = [a or b for a, b in LINK.findall(text)]
        candidates += [v.rstrip('),;，。') for v in ABSOLUTE.findall(text)]
        if len(text) < 4096 and '\n' not in text:
            candidates.append(text)
        for value in candidates:
            try:
                path = local_path(value, record['cwd'])
                if path:
                    # Restrict speculative plain strings to existing files.
                    if path.is_file() or value in [a or b for a, b in LINK.findall(text)]:
                        add(path, [record['thread_id']])
            except (OSError, ValueError, RuntimeError):
                continue
    for record in records:
        with m.session_path(Path(root), Path(root) / record['path']).open('r', encoding='utf-8-sig') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    payload = event.get('payload', {})
                    # Tool-call code and shell logs contain runtime paths, not deliverables.
                    if event.get('type') == 'event_msg' and payload.get('type') in ('agent_message', 'user_message') or event.get('type') == 'response_item' and payload.get('type') == 'message' or event.get('type') == 'event_msg' and 'message' in payload and not payload.get('type'):
                        for text in strings(payload):
                            scan(text, record)
                except ValueError:
                    continue
    for table in HISTORY_TABLES:
        for row in metadata['history'][table]:
            if row.get('item_type') == 'fileChange':
                record = by_id.get(row.get('thread_id'))
                if record:
                    try:
                        changes = json.loads(row.get('item_json', '{}')).get('changes', [])
                        for change in changes:
                            path = local_path(change.get('path', ''), record['cwd'])
                            if path:
                                add(path, [record['thread_id']])
                    except (ValueError, OSError, TypeError):
                        pass
                continue
            if row.get('item_type') not in ('message', 'agentMessage', 'userMessage', 'imageGeneration', 'imageView'):
                continue
            record = by_id.get(row.get('thread_id'))
            if record:
                for text in strings(row):
                    scan(text, record)
    for row in metadata['state']['thread_attachments']:
        record = by_id.get(row.get('thread_id'))
        if record:
            for text in strings(row):
                scan(text, record)
    for project in selected_projects:
        associated = [r['thread_id'] for r in records if r['id'] in project['ids']]
        for original in project['roots']:
            path = Path(original).expanduser()
            m.require(path.is_absolute() and path.is_dir() and path.resolve() != Path(path.anchor), '项目目录不可用，或项目指向磁盘根目录，请检查项目设置。')
            m.require(not path.is_symlink() and not getattr(path, 'is_junction', lambda: False)(), '项目根目录是链接，请使用实际目录。')
            roots.append({'source': str(path.resolve()), 'project_id': project['id'], 'thread_ids': associated})
            for parent, dirs, files in os.walk(path, followlinks=False):
                # Do not traverse reparse points, and never include staging/backups inside the live Codex home.
                for name in list(dirs):
                    child = Path(parent) / name
                    if child.is_symlink() or getattr(child, 'is_junction', lambda: False)() or child.resolve().is_relative_to(Path(root).resolve()):
                        dirs.remove(name); omitted.add(str(child) + '（链接或 Codex 内部数据）')
                for name in files:
                    add(Path(parent) / name, associated, True)
    m.require(extra_paths is None or isinstance(extra_paths, list) and all(isinstance(x, str) for x in extra_paths), '补充文件列表格式错误。')
    for value in extra_paths or []:
        path = Path(value).expanduser()
        m.require(path.is_absolute() and path.is_file(), '补充文件必须是已存在的完整文件路径。')
        add(path, list(by_id), True)
    reference_roots = []
    for asset in list(assets.values()):
        parent = str(Path(asset['source']).parent)
        if not any(parent == r['source'] for r in reference_roots):
            reference_roots.append({'source': parent, 'thread_ids': sorted(asset['thread_ids']),
                                    'path': 'files/references/' + hashlib.sha256(parent.encode()).hexdigest()[:16]})
        else:
            next(r for r in reference_roots if r['source'] == parent)['thread_ids'] = sorted(set(next(r for r in reference_roots if r['source'] == parent)['thread_ids']) | asset['thread_ids'])
    # Preserve local dependencies for generated HTML/SVG/CSS artifacts.
    scanned = set()
    while True:
        pending = [a for a in assets.values() if a['source'] not in scanned and Path(a['source']).suffix.lower() in ('.html', '.htm', '.css', '.svg')]
        if not pending:
            break
        for asset in pending:
            scanned.add(asset['source'])
            source = Path(asset['source'])
            if asset['bytes'] > 16 * 1024**2:
                omitted.add(str(source) + '（过大，未扫描文档内嵌资源）'); continue
            text = source.read_text(encoding='utf-8', errors='replace')
            for value in re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+)["\']|url\(["\']?([^\)"\']+)', text, flags=re.I):
                ref = value[0] or value[1]
                if ref.startswith(('#', 'data:', 'http:', 'https:', '//')):
                    continue
                dependency = local_path(ref, str(source.parent))
                if dependency:
                    add(dependency, asset['thread_ids'])
    total = sum(a['bytes'] for a in assets.values())
    m.require(total <= 8 * 1024**3 and all(a['bytes'] <= 2 * 1024**3 for a in assets.values()), '文件总量超过 8 GB 或单文件超过 2 GB，请拆分导出。')
    for asset in assets.values():
        asset['thread_ids'] = sorted(asset['thread_ids'])
        asset['references'] = sorted(asset['references'])
        owner = next((r for r in sorted(roots, key=lambda r: -len(r['source'])) if Path(asset['source']).is_relative_to(Path(r['source']))), None)
        if owner:
            key = hashlib.sha256(owner['source'].encode()).hexdigest()[:16]
            asset['path'] = 'files/projects/' + key + '/' + Path(asset['source']).relative_to(owner['source']).as_posix()
        else:
            key = hashlib.sha256(asset['source'].encode()).hexdigest()[:16]
            asset['path'] = 'files/references/' + key + '/' + Path(asset['source']).name
    for r in roots:
        r['path'] = 'files/projects/' + hashlib.sha256(r['source'].encode()).hexdigest()[:16]
    return {'assets': list(assets.values()), 'project_roots': roots, 'reference_roots': reference_roots, 'metadata': metadata,
            'missing': sorted(missing), 'omitted': sorted(omitted), 'bytes': total, 'projects': selected_projects}


def remap(value, mappings):
    if isinstance(value, str):
        # JSON inside JSON (history item payloads) must be decoded before replacing paths.
        if value.startswith(('{', '[')):
            try:
                decoded = json.loads(value)
                if isinstance(decoded, (dict, list)):
                    return json.dumps(remap(decoded, mappings), ensure_ascii=False, separators=(',', ':'))
            except ValueError:
                pass
        replacements = {}
        for old, new in mappings.items():
            for variant in {old, old.replace('\\', '/')}:
                if variant:
                    replacements[variant] = new.replace('\\', '/')
                    if '/' in variant or '\\' in variant:
                        replacements['sandbox:' + variant] = new.replace('\\', '/')
        if replacements:
            pattern = '|'.join(re.escape(key) for key in sorted(replacements, key=len, reverse=True))
            value = re.sub(pattern, lambda match: replacements[match.group()], value)
        return value
    if isinstance(value, list):
        return [remap(v, mappings) for v in value]
    if isinstance(value, dict):
        return {k: remap(v, mappings) for k, v in value.items()}
    return value
