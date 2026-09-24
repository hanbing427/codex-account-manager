"""Read-only conversation archives; never collect authentication files."""
from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile

import migrate as m
import conversation_bundle as bundle


def catalog(root):
    root = Path(root).resolve()
    metadata, databases, warnings = {}, [], []
    for db in sorted(root.glob('state_*.sqlite')):
        m.require(not db.is_symlink() and db.resolve().parent == root, '数据库路径不安全。')
        try:
            with closing(m.connect(db, True)) as conn:
                columns = {r['name'] for r in conn.execute('pragma table_info(threads)')}
                if not {'id', 'rollout_path'} <= columns:
                    continue
                fields = [c for c in ('id', 'title', 'rollout_path', 'archived', 'updated_at', 'cwd') if c in columns]
                rows = list(conn.execute('select ' + ','.join(fields) + ' from threads'))
                databases.append(db)
                for row in rows:
                    record = dict(row)
                    try:
                        path = m.session_path(root, record['rollout_path'])
                    except (RuntimeError, OSError):
                        warnings.append('数据库中有会话路径位于当前会话目录外，未读取该路径。')
                        continue
                    metadata[str(path)] = record
        except sqlite3.Error:
            raise RuntimeError('无法读取对话数据库，请稍后重试。') from None
    records = []
    for folder_name in ('sessions', 'archived_sessions'):
        folder = root / folder_name
        if not folder.exists():
            continue
        m.require(folder.resolve() == folder and not folder.is_symlink(), '会话目录不能是链接。')
        for file in sorted(folder.rglob('*.jsonl')):
            m.require(not file.is_symlink() and file.resolve().is_relative_to(folder), '会话文件路径不安全。')
            relative = file.relative_to(root).as_posix()
            row = metadata.pop(str(file.resolve()), {})
            tid = str(row.get('id') or '')
            title = row.get('title') or ''
            if not tid:
                with file.open('rb') as stream:
                    line = stream.readline(1024 * 1024)
                try:
                    value = json.loads(line)
                    if value.get('type') == 'session_meta':
                        tid = str(value.get('payload', {}).get('id', ''))
                except (ValueError, AttributeError):
                    pass
            records.append({'id': hashlib.sha256(relative.encode()).hexdigest(),
                            'thread_id': tid, 'title': title or tid or file.stem,
                            'path': relative, 'archived': folder_name == 'archived_sessions' or bool(row.get('archived')),
                            'bytes': file.stat().st_size, 'modified': file.stat().st_mtime,
                            'cwd': row.get('cwd', ''), '_database_linked': bool(row)})
    # A copied rollout can retain its original thread ID. Import requires one
    # file per ID; the database's active rollout is authoritative, not the
    # most recently copied file. Never modify or remove the alternate files.
    unique = {}
    for record in records:
        key = record['thread_id'] or record['id']
        previous = unique.get(key)
        rank = lambda r: (r['_database_linked'], r['modified'], r['bytes'], r['path'])
        if previous is None or rank(record) > rank(previous):
            unique[key] = record
    duplicates = len(records) - len(unique)
    if duplicates:
        warnings.append(f'{duplicates} 个重复会话文件未重复列出或打包；优先使用数据库关联的文件，其余使用最新文件。原文件均已保留。')
    records = list(unique.values())
    for record in records:
        record.pop('_database_linked')
    if metadata:
        warnings.append(f'{len(metadata)} 条数据库记录没有对应的会话文件，无法导出其正文。')
    return {'records': sorted(records, key=lambda r: r['modified'], reverse=True),
            'warnings': list(dict.fromkeys(warnings)), 'databases': databases}


def copy_prefix(source, archive, name, size):
    # Read at most the initial size: ongoing conversations cannot grow the export forever.
    with source.open('rb') as incoming, archive.open(name, 'w', force_zip64=True) as outgoing:
        remaining = size
        while remaining:
            chunk = incoming.read(min(1024 * 1024, remaining))
            m.require(chunk, '导出期间会话文件被截断，请停止生成后重试。')
            outgoing.write(chunk)
            remaining -= len(chunk)


@contextmanager
def archive_history(root, store, ids=None, project_ids=None, extra_paths=None):
    data = catalog(root)
    records = data['records']
    if ids is not None:
        m.require(isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids), '请选择要导出的对话。')
        selected = set(ids)
        m.require(selected <= {r['id'] for r in records}, '部分对话已不存在，请刷新列表。')
        records = [r for r in records if r['id'] in selected]
    m.require(records, '未发现可导出的对话记录。')
    migration = bundle.collect(root, records, project_ids, extra_paths)
    with tempfile.TemporaryDirectory(prefix='history-export-', dir=store) as temp:
        temp = Path(temp)
        output = temp / 'conversations.zip'
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for record in records:
                source = m.session_path(root, Path(root) / record['path'])
                # Exclude an incomplete last record while Codex is appending a JSON line.
                size = source.stat().st_size
                with source.open('rb') as stream:
                    if size:
                        stream.seek(max(0, size - 1))
                        if stream.read(1) != b'\n':
                            cursor = size
                            while cursor:
                                start = max(0, cursor - 65536)
                                stream.seek(start)
                                chunk = stream.read(cursor - start)
                                last = chunk.rfind(b'\n')
                                if last >= 0:
                                    # A valid final JSON record need not have a newline.
                                    tail_start = start + last + 1
                                    break
                                cursor = start
                            else:
                                tail_start = 0
                            stream.seek(tail_start)
                            try:
                                json.loads(stream.read(size - tail_start))
                            except ValueError:
                                size = tail_start
                                data['warnings'].append('部分会话正在写入，已排除末尾未完成的记录。')
                copy_prefix(source, archive, record['path'], size)
                record['exported_bytes'] = size
            if ids is None:
                for db in data['databases']:
                    snapshot = temp / db.name
                    with closing(m.connect(db, True)) as source, closing(sqlite3.connect(snapshot)) as target:
                        source.backup(target)
                    archive.write(snapshot, db.name)
                for name in ('session_index.jsonl', 'history.jsonl'):
                    path = Path(root) / name
                    if path.is_file():
                        m.require(not path.is_symlink() and path.resolve().parent == Path(root).resolve(), '历史索引路径不安全。')
                        copy_prefix(path, archive, name, path.stat().st_size)
            for asset in migration['assets']:
                source = Path(asset['source'])
                before = source.stat()
                digest = hashlib.sha256()
                with source.open('rb') as incoming, archive.open(asset['path'], 'w', force_zip64=True) as outgoing:
                    while chunk := incoming.read(1024 * 1024):
                        digest.update(chunk); outgoing.write(chunk)
                after = source.stat()
                m.require(before.st_size == after.st_size == asset['bytes'] and before.st_mtime_ns == after.st_mtime_ns, '文件在打包期间发生变化，请保存文件并重新导出。')
                asset['sha256'] = digest.hexdigest()
            migration_raw = json.dumps(migration, ensure_ascii=False, indent=2).encode()
            m.require(len(migration_raw) <= 256 * 1024**2, '聊天历史元数据超过 256 MB，请分批导出。')
            archive.writestr('migration.json', migration_raw)
            if migration['missing'] or migration['omitted']:
                data['warnings'].append(f'有 {len(migration["missing"])} 个文件引用缺失、{len(migration["omitted"])} 个文件或目录未打包，详见 migration.json。')
            manifest = {'version': 2, 'mode': 'project' if project_ids else 'all' if ids is None else 'selected',
                        'created': datetime.now().isoformat(), 'source_home': str(root),
                        'records': records, 'warnings': list(dict.fromkeys(data['warnings']))}
            archive.writestr('conversations.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr('README.txt',
                'Codex Account Manager 对话导出\n\n'
                'sessions/ 和 archived_sessions/：原始会话事件，包含消息及本地存储的上下文。\n'
                'conversations.json：所选对话目录、原始路径、工作目录和导出警告。\n'
                '完整导出另含可读取的 state 数据库的一致性快照，以及存在的会话索引和输入历史。\n'
                '部分导出不包含全局数据库或其他对话，只包含选定的原始会话文件；分支会话文件本身可能包含继承的父对话上下文。\n'
                'migration.json 包含所选对话的聊天历史、分组、置顶等元数据，以及文件迁移映射。files/ 是生成文件及本地引用文件；只有项目导出才包含整个项目目录。\n'
                '在另一台电脑的对话迁移模块选择导入 ZIP，勾选对话并指定存放目录；程序合并而不替换数据库，重写文件路径并重启 Codex。不要直接覆盖目标数据库。\n'
                '不会自动导出 Codex 账号凭据；对话或项目文件可能包含隐私、环境配置和密钥，请自行保管。缺失文件、远程链接和未识别引用无法自动恢复，可在导出前补充文件。\n'
                '这不是运行中模型的实时内存快照。压缩过的上下文只能保留本地已有记录。在线导出各文件的时点可能不同，建议停止生成后导出。\n')
        yield output, manifest
