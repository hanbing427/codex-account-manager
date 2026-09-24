"""Plan a portable import using fixed schemas and collision-free target paths."""
from contextlib import closing
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import uuid

import conversation_bundle as b
import migrate as m

HISTORY_SCHEMA = {
 'thread_turns': 'thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL, status TEXT NOT NULL, error_json TEXT, started_at INTEGER, completed_at INTEGER, duration_ms INTEGER, first_user_item_id TEXT, final_agent_item_id TEXT, rollout_byte_offset INTEGER, rollout_end_ordinal INTEGER, rollout_end_byte_offset INTEGER, PRIMARY KEY(thread_id,turn_id)',
 'thread_items': "thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, item_json TEXT NOT NULL, item_type TEXT NOT NULL DEFAULT '', updated_at_ordinal INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(thread_id,turn_id,item_id)",
 'thread_history_projection_state': 'thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER NOT NULL, next_rollout_ordinal INTEGER NOT NULL',
 'thread_realtime_items': 'thread_id TEXT NOT NULL, item_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, item_type TEXT NOT NULL, item_json TEXT NOT NULL, PRIMARY KEY(thread_id,item_id)',
}


def safe_relative(value):
    m.require(isinstance(value, str) and value and '\\' not in value and ':' not in value, '包内文件路径无效。')
    path = PurePosixPath(value)
    m.require(not path.is_absolute() and '..' not in path.parts and path.parts[0] == 'files', '包内文件不在 files 目录中。')
    # Portable to Windows as well: do not create device files, ADS or trailing-dot aliases.
    for part in path.parts:
        m.require(part not in ('.', '..') and not part.endswith((' ', '.')) and not re.search(r'[<>"|?*\x00-\x1f]', part)
                  and part.split('.')[0].upper() not in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}, '文件名不兼容跨平台导入，请修改源文件名后重新导出。')
    return path


def migration_data(archive, record_ids):
    if 'migration.json' not in archive.namelist():
        return None
    m.require(archive.getinfo('migration.json').file_size <= 256 * 1024**2, '迁移元数据过大，请分批导出。')
    value = json.loads(archive.read('migration.json'))
    m.require(isinstance(value, dict) and isinstance(value.get('assets'), list) and isinstance(value.get('metadata'), dict), '迁移清单格式错误。')
    seen = set()
    for asset in value['assets']:
        m.require(isinstance(asset, dict), '文件条目格式错误。')
        relative = str(safe_relative(asset.get('path')))
        m.require(relative.casefold() not in seen, '文件路径存在大小写冲突。')
        seen.add(relative.casefold())
        info = archive.getinfo(relative)
        m.require(info.file_size == asset.get('bytes') and info.file_size <= 2 * 1024**3, '文件大小与清单不一致。')
        m.require(isinstance(asset.get('source'), str) and isinstance(asset.get('references', []), list)
                  and all(isinstance(x, str) for x in asset.get('references', [])), '文件源路径格式错误。')
        m.require(isinstance(asset.get('thread_ids'), list) and set(asset['thread_ids']) <= record_ids, '文件所属对话无效。')
        m.require(isinstance(asset.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', asset['sha256']), '文件校验值无效。')
    metadata = value['metadata']
    for group, tables in (('state', b.STATE_TABLES), ('history', b.HISTORY_TABLES)):
        m.require(isinstance(metadata.get(group), dict), '迁移元数据格式错误。')
        for table in tables:
            items = metadata[group].get(table, [])
            m.require(isinstance(items, list) and all(isinstance(r, dict) for r in items), '数据库条目格式错误。')
            if table in b.HISTORY_TABLES or table == 'thread_attachments':
                m.require(all(r.get('thread_id') in record_ids for r in items), '元数据包含包外对话。')
            if table == 'threads':
                m.require(all(r.get('id') in record_ids for r in items), '元数据包含包外对话。')
    m.require(isinstance(metadata.get('ui', {}), dict), '界面元数据格式错误。')
    for root in value.get('project_roots', []) + value.get('reference_roots', []):
        safe_relative(root.get('path'))
        m.require(isinstance(root.get('source'), str) and isinstance(root.get('thread_ids'), list)
                  and set(root['thread_ids']) <= record_ids, '项目路径格式错误。')
    return value


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def plan(root, archive, migration, selected, destination, package_key):
    if migration is None:
        return None
    m.require(isinstance(destination, str) and destination.strip(), '新版迁移包需要选择本机存放目录。')
    base = Path(destination).expanduser()
    m.require(base.is_absolute() and base.is_dir(), '存放目录必须是本机已存在的完整路径。')
    base = base.resolve() / ('Codex-Import-' + package_key[:16])
    ids = {r['id'] for r in selected}
    metadata = migration['metadata']
    mapping, cwd_map = {}, {}
    needed = {r.get('project_id') for r in metadata['state'].get('threads', []) if r.get('id') in ids} - {None, ''}
    for tid, value in metadata.get('ui', {}).get('thread-project-assignments', {}).items():
        if tid in ids and isinstance(value, dict) and value.get('projectId'):
            needed.add(value['projectId'])
    for old, new in metadata.get('ui', {}).get('legacy-project-map', {}).items():
        if old in needed or new in needed:
            needed.update((old, new))
    root_paths = [r['path'] for r in metadata['state'].get('project_roots', []) if r.get('project_id') in needed]
    root_paths += [p for pid, value in metadata.get('ui', {}).get('local-projects', {}).items() if pid in needed for p in value.get('rootPaths', [])]
    for old in root_paths:
        m.require(isinstance(old, str) and old, '项目目录元数据无效。')
        cwd_map[old] = str(base / 'workspaces' / hashlib.sha256(old.encode()).hexdigest()[:16])
    # Empty working directories are still recreated, so original project grouping is retained.
    for record in selected:
        old = record['cwd']
        if old:
            key = hashlib.sha256(old.encode()).hexdigest()[:16]
            cwd_map[old] = str(base / 'workspaces' / key)
    for project_root in migration.get('project_roots', []):
        if ids.intersection(project_root['thread_ids']):
            target = base.joinpath(*safe_relative(project_root['path']).parts)
            cwd_map[project_root['source']] = str(target)
    for reference_root in migration.get('reference_roots', []):
        old = reference_root['source']
        if ids.intersection(reference_root['thread_ids']) and not any(old.replace('\\', '/').startswith(k.replace('\\', '/').rstrip('/') + '/') or old == k for k in cwd_map):
            cwd_map[old] = str(base.joinpath(*safe_relative(reference_root['path']).parts))
    for old in list(cwd_map):
        for parent, new in sorted(cwd_map.items(), key=lambda x: -len(x[0])):
            old_norm, parent_norm = old.replace('\\', '/'), parent.replace('\\', '/')
            if old != parent and old_norm.startswith(parent_norm.rstrip('/') + '/'):
                cwd_map[old] = str(Path(new).joinpath(*PurePosixPath(old_norm[len(parent_norm):].lstrip('/')).parts))
                break
    mapping.update(cwd_map)
    files = []
    for asset in migration['assets']:
        if not ids.intersection(asset['thread_ids']):
            continue
        target = base.joinpath(*safe_relative(asset['path']).parts)
        # Assets from the working directory keep their relative layout (HTML resources, etc.).
        for old, new in sorted(cwd_map.items(), key=lambda x: -len(x[0])):
            original = asset['source'].replace('\\', '/')
            prefix = old.replace('\\', '/').rstrip('/') + '/'
            if original.startswith(prefix):
                relative = PurePosixPath(original[len(prefix):])
                safe_relative('files/' + relative.as_posix())
                target = Path(new).joinpath(*relative.parts)
                break
        m.require(target.resolve().is_relative_to(base.resolve()) and target.resolve() == target, '存放路径包含链接或超出导入目录。')
        mapping[asset['source']] = str(target)
        for reference in asset.get('references', []):
            mapping[reference] = str(target)
        if target.exists():
            m.require(target.is_file() and checksum(target) == asset['sha256'], '存放目录已有不同内容的同名文件，请选择其他目录。')
        files.append((asset, target))
    m.require(len({str(p).casefold() for _, p in files}) == len(files), '导入目标文件存在重名冲突，请调整源路径后重新导出。')
    project_ids, section_ids = {}, {}
    selected_rows = [r for r in metadata['state'].get('threads', []) if r['id'] in ids]
    needed_projects = {r.get('project_id') for r in selected_rows} - {None, ''}
    for tid, assignment in metadata.get('ui', {}).get('thread-project-assignments', {}).items():
        if tid in ids and isinstance(assignment, dict) and assignment.get('projectId'):
            needed_projects.add(assignment['projectId'])
    needed_projects.update(needed)
    needed_sections = {r.get('thread_section_id') for r in selected_rows} - {None, ''}
    for old in needed_projects:
        project_ids[old] = str(uuid.uuid5(uuid.NAMESPACE_URL, package_key + '/project/' + old))
    for old in needed_sections:
        section_ids[old] = str(uuid.uuid5(uuid.NAMESPACE_URL, package_key + '/section/' + old))
    mapping.update(project_ids); mapping.update(section_ids)
    return {'base': base, 'files': files, 'mapping': mapping, 'cwd_map': cwd_map, 'project_ids': project_ids,
            'section_ids': section_ids, 'metadata': metadata, 'ids': ids,
            'thread_rows': {r['id']: r for r in selected_rows}}


def insert(conn, schema, table, values):
    columns = [dict(c) for c in conn.execute(f'pragma {schema}.table_info("{table}")')]
    m.require(columns, '目标 Codex 版本缺少所需数据表：' + table)
    filtered = {c['name']: values[c['name']] for c in columns if c['name'] in values}
    for column in columns:
        m.require(column['name'] in filtered or not column['notnull'] or column['dflt_value'] is not None,
                  '目标数据库有不兼容的必填字段：' + table + '.' + column['name'])
    names = ','.join('"' + n.replace('"', '""') + '"' for n in filtered)
    conn.execute(f'insert into {schema}."{table}" ({names}) values (' + ','.join('?' for _ in filtered) + ')', list(filtered.values()))


def restore_metadata(conn, plan, offsets):
    metadata, ids, mapping = plan['metadata'], plan['ids'], plan['mapping']
    for table, key, selected in (('projects', 'id', plan['project_ids']), ('project_roots', 'project_id', plan['project_ids']),
                                  ('thread_sections', 'id', plan['section_ids']), ('thread_attachments', 'thread_id', ids)):
        for source in metadata['state'].get(table, []):
            if source.get(key) not in selected:
                continue
            value = b.remap(source, mapping)
            if table == 'thread_attachments':
                value['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, str(plan['base']) + '/attachment/' + str(source['id'])))
            primary = ('project_id', 'position') if table == 'project_roots' else ('id',)
            if conn.execute(f'select 1 from "{table}" where ' + ' and '.join(f'"{k}"=?' for k in primary), [value[k] for k in primary]).fetchone():
                continue
            insert(conn, 'main', table, value)
    for table in b.HISTORY_TABLES:
        for source in metadata['history'].get(table, []):
            if source['thread_id'] not in ids:
                continue
            value = b.remap(source, mapping)
            table_offsets = offsets.get(source['thread_id'], {})
            for key in ('rollout_byte_offset', 'rollout_end_byte_offset', 'next_rollout_byte_offset'):
                if value.get(key) is not None:
                    value[key] = table_offsets.get(value[key], max(table_offsets.values(), default=0))
            insert(conn, 'history', table, value)


def merge_ui(current, plan, root=None):
    source, ids, mapping = plan['metadata'].get('ui', {}), plan['ids'], plan['mapping']
    updated = json.loads(json.dumps(current))
    for key in b.THREAD_MAPS:
        for tid, value in source.get(key, {}).items():
            if tid in ids:
                updated.setdefault(key, {})[tid] = b.remap(value, mapping)
    for key in ('local-projects', 'project-appearances', 'sidebar-project-thread-orders'):
        for pid, value in source.get(key, {}).items():
            if pid in plan['project_ids']:
                if key == 'sidebar-project-thread-orders':
                    value = [tid for tid in value if tid in ids]
                updated.setdefault(key, {})[plan['project_ids'][pid]] = b.remap(value, mapping)
    for key, selected in (('project-order', plan['project_ids']), ('projectless-thread-ids', ids)):
        for value in source.get(key, []):
            if value in selected:
                new = mapping.get(value, value)
                if new not in updated.setdefault(key, []):
                    updated[key].append(new)
    if root:
        for old, new in source.get('legacy-project-map', {}).items():
            if old in plan['project_ids'] and new in plan['project_ids']:
                updated.setdefault('app-server-project-id-by-legacy-project-id-by-host', {}).setdefault('local:' + str(Path(root).resolve()), {})[plan['project_ids'][old]] = plan['project_ids'][new]
    return updated
