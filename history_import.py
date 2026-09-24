"""Validate exported archives and merge selected conversations without overwrites."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import sqlite3
import time
import uuid
import zipfile

import migrate as m
import history_export
import conversation_bundle as bundle
import conversation_restore as restore

MAX_UPLOAD = 8 * 1024**3


def inspect_archive(path):
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            m.require(len(entries) <= 60000 and sum(e.file_size for e in entries) <= 12 * 1024**3,
                      '解压内容过大，请分批导出再导入。')
            names = [e.filename for e in entries]
            m.require(len(names) == len(set(names)), '压缩包包含重复路径。')
            for entry in entries:
                name = PurePosixPath(entry.filename)
                m.require(not name.is_absolute() and '..' not in name.parts and '\\' not in entry.filename
                          and ':' not in entry.filename and (entry.external_attr >> 16) & 0o170000 != 0o120000,
                          '压缩包包含不安全路径或符号链接。')
            info = archive.getinfo('conversations.json')
            m.require(info.file_size <= 16 * 1024 * 1024, '对话清单过大。')
            manifest = json.loads(archive.read(info))
            m.require(isinstance(manifest, dict) and manifest.get('version') in (1, 2) and isinstance(manifest.get('records'), list),
                      '请使用本软件导出的对话 ZIP。')
            records, ids, paths = [], set(), set()
            session_bytes = 0
            for record in manifest['records']:
                m.require(isinstance(record, dict), '对话清单格式错误。')
                relative = record.get('path', '')
                m.require(isinstance(relative, str), '无效会话路径。')
                parts = PurePosixPath(relative).parts
                m.require(len(parts) >= 2 and parts[0] in ('sessions', 'archived_sessions') and relative.endswith('.jsonl'), '无效会话路径。')
                entry = archive.getinfo(relative)
                session_bytes += entry.file_size
                m.require(session_bytes <= 512 * 1024**2, '会话正文总量超过 512 MB，请分批导入。')
                m.require(0 < entry.file_size <= 128 * 1024 * 1024, '单条对话为空或超过 128 MB，请检查导出文件。')
                raw = archive.read(entry)
                first = None
                for line in raw.splitlines():
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    m.require(isinstance(value, dict), '会话包含无效记录。')
                    if first is None:
                        first = value
                m.require(first and first.get('type') == 'session_meta' and isinstance(first.get('payload'), dict), '会话缺少起始元数据。')
                meta = first['payload']
                tid = str(meta.get('id', ''))
                try:
                    uuid.UUID(tid)
                except ValueError:
                    raise RuntimeError('会话 ID 不是有效 UUID。') from None
                m.require(not record.get('thread_id') or record['thread_id'] == tid, '会话 ID 与清单不一致。')
                m.require(tid not in ids and relative not in paths, '压缩包包含重复会话。')
                ids.add(tid); paths.add(relative)
                records.append({'id': tid, 'title': str(record.get('title') or tid), 'path': relative,
                                'cwd': str(record.get('cwd') or meta.get('cwd') or ''),
                                'archived': parts[0] == 'archived_sessions' or bool(record.get('archived')),
                                'bytes': entry.file_size, 'sha256': m.sha(raw)})
            m.require(records, '压缩包内没有对话。')
            migration = restore.migration_data(archive, ids)
            for record in records:
                record['portable'] = migration is not None
                record['files'] = sum(record['id'] in a['thread_ids'] for a in migration['assets']) if migration else 0
                record['missing'] = len(migration.get('missing', [])) if migration else 0
            return records
    except (zipfile.BadZipFile, KeyError, ValueError, UnicodeError):
        raise RuntimeError('对话压缩包损坏或格式不兼容，请重新导出。') from None


def preview(root, path):
    records = inspect_archive(path)
    local = history_export.catalog(root)
    existing = {r['thread_id'] for r in local['records']}
    for db in local['databases']:
        with closing(m.connect(db, True)) as conn:
            existing.update(str(r[0]) for r in conn.execute('select id from threads'))
    for record in records:
        record['exists'] = record['id'] in existing
        record['needs_cwd'] = not (record['cwd'] and Path(record['cwd']).is_absolute() and Path(record['cwd']).is_dir())
    return records


def merge(root, store, path, ids, cwd='', guard=m.idle, dry_run=False, destination=''):
    m.require(isinstance(cwd, str), '项目目录格式错误。')
    m.require(isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids), '请选择要导入的对话。')
    records = preview(root, path)
    m.require(set(ids) <= {r['id'] for r in records}, '所选对话不存在，请重新预览。')
    selected = [r for r in records if r['id'] in ids and not r['exists']]
    skipped = len(set(ids)) - len(selected)
    if not selected:
        return {'count': 0, 'skipped': skipped}
    root = Path(root).resolve()
    if cwd:
        directory = Path(cwd).expanduser()
        m.require(directory.is_absolute() and directory.is_dir(), '项目目录必须是本机已存在的完整路径。')
        cwd = str(directory.resolve())
    m.require(destination or cwd or not any(r['needs_cwd'] for r in selected), '部分旧项目路径不可用，请指定本机项目目录。')
    db = m.database(root)
    cfg = m.configuration(root) if (root / 'config.toml').exists() else {'target': 'openai', 'model': None}
    prepared, offsets = [], {}
    history_path = None
    with zipfile.ZipFile(path) as archive, closing(m.connect(db)) as conn:
        migration = restore.migration_data(archive, {r['id'] for r in records})
        package_key = m.sha(archive.read('conversations.json'))
        portable = restore.plan(root, archive, migration, selected, destination, package_key) if migration else None
        if portable:
            for table in bundle.STATE_TABLES[1:]:
                if portable['metadata']['state'].get(table):
                    m.require(conn.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone(), '本机 Codex 版本缺少数据表：' + table)
            if any(row['thread_id'] in portable['ids'] for table in bundle.HISTORY_TABLES for row in portable['metadata']['history'].get(table, [])):
                candidates = sorted(root.glob('thread_history_*.sqlite'))
                m.require(len(candidates) == 1, '目标电脑尚无可用聊天历史数据库。请先安装相同或更新版本的 Codex，启动一次并退出，再导入。')
                history_path = candidates[0]
                m.require(not history_path.is_symlink() and history_path.resolve().parent == root, '历史数据库路径不安全。')
                with closing(m.connect(history_path, True)) as history_conn:
                    for table in restore.HISTORY_SCHEMA:
                        if portable['metadata']['history'].get(table):
                            m.require(history_conn.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone(), '目标 Codex 缺少历史数据表，请升级并启动一次后重试。')
        columns = [dict(c) for c in conn.execute('pragma table_info(threads)')]
        for record in selected:
            target_cwd = portable['cwd_map'].get(record['cwd'], str(portable['base'] / 'workspace')) if portable else cwd or record['cwd']
            raw = archive.read(record['path'])
            m.require(m.sha(raw) == record['sha256'], '导入包已变化，请重新预览。')
            lines = []
            offsets[record['id']] = {0: 0}
            old_offset, new_offset = 0, 0
            for line in raw.splitlines(keepends=True):
                if not line.strip():
                    lines.append(line)
                    old_offset += len(line); new_offset += len(line)
                    offsets[record['id']][old_offset] = new_offset
                    continue
                value = json.loads(line)
                old_length = len(line)
                if portable:
                    value = bundle.remap(value, portable['mapping'])
                if value.get('type') in ('session_meta', 'turn_context') and isinstance(value.get('payload'), dict):
                    value['payload']['cwd'] = target_cwd
                    value['payload']['model_provider'] = cfg['target']
                    line = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
                elif portable:
                    line = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
                lines.append(line)
                old_offset += old_length; new_offset += len(line)
                offsets[record['id']][old_offset] = new_offset
            content = b''.join(lines)
            folder = root / ('archived_sessions' if record['archived'] else 'sessions') / 'imported'
            target = folder / ('rollout-' + record['id'] + '.jsonl')
            m.require(target.resolve().is_relative_to(root) and target.resolve().parent == folder and not target.exists(), '目标会话路径已存在或不安全。')
            now = int(time.time())
            values = {'id': record['id'], 'rollout_path': str(target), 'title': record['title'],
                      'cwd': target_cwd, 'model_provider': cfg['target'], 'model': cfg.get('model'),
                      'created_at': now, 'updated_at': now, 'created_at_ms': now * 1000, 'updated_at_ms': now * 1000,
                      'recency_at': now, 'recency_at_ms': now * 1000, 'source': 'cli',
                      'sandbox_policy': '{"type":"read-only"}', 'approval_mode': 'on-request',
                      'archived': int(record['archived']), 'archived_at': now if record['archived'] else None,
                      'has_user_event': 1}
            if portable:
                original = portable['thread_rows'].get(record['id'], {})
                for field in ('is_pinned', 'project_id', 'thread_section_id', 'name'):
                    m.require(not original.get(field) or field in {c['name'] for c in columns}, '目标 Codex 版本无法保留分组或置顶信息，请升级后导入。')
                copied = bundle.remap(original, portable['mapping'])
                values.update({k: v for k, v in copied.items() if k not in ('id', 'rollout_path', 'cwd', 'model_provider', 'sandbox_policy', 'approval_mode')})
                if not history_path:
                    values['history_mode'] = 'legacy'
            insert = {c['name']: values[c['name']] for c in columns if c['name'] in values}
            for column in columns:
                m.require(column['name'] in insert or not column['notnull'] or column['dflt_value'] is not None,
                          '本机数据库版本包含未知必填字段，已停止导入。')
            prepared.append((target, content, insert))
        # Verify checksums before touching destination files or closing Codex.
        if portable:
            import hashlib
            for asset, target in portable['files']:
                digest = hashlib.sha256()
                with archive.open(asset['path']) as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                m.require(digest.hexdigest() == asset['sha256'], '生成文件校验失败，请重新导出。')
        if dry_run:
            return {'count': len(prepared), 'skipped': skipped}
        guard()
        backup = Path(store) / 'conversation-import-backups' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True)
        with closing(sqlite3.connect(backup / db.name)) as snapshot:
            conn.backup(snapshot)
        history_existed = bool(history_path and history_path.exists())
        if history_existed:
            with closing(m.connect(history_path, True)) as original, closing(sqlite3.connect(backup / history_path.name)) as snapshot:
                original.backup(snapshot)
        if history_path:
            conn.execute('attach database ? as history', (str(history_path),))
        ui_path = root / '.codex-global-state.json'
        previous_ui = ui_path.read_bytes() if portable and ui_path.exists() else None
        if previous_ui is not None:
            m.atomic(backup / 'global-state.json', previous_ui)
        updated_ui = restore.merge_ui(json.loads(previous_ui) if previous_ui else {}, portable, root) if portable else None
        journal = {'state': 'prepared', 'created': datetime.now().isoformat(), 'database': str(db),
                   'history_database': str(history_path) if history_path else None,
                   'history_before_present': history_existed,
                   'ui_before_present': previous_ui is not None, 'portable': portable is not None,
                   'files': [{'path': str(p), 'id': v['id'], 'sha256': m.sha(b)} for p, b, v in prepared]}
        if portable:
            journal['asset_base'] = str(portable['base'])
            journal['history_counts'] = {table: sum(row['thread_id'] in portable['ids'] for row in portable['metadata']['history'].get(table, [])) for table in bundle.HISTORY_TABLES}
            journal['assets'] = [{'path': str(target), 'sha256': asset['sha256'], 'created': not target.exists()} for asset, target in portable['files']]
        m.save(backup / 'manifest.json', journal)
        written = []
        ui_written = False
        committed = False
        try:
            conn.execute('begin immediate')
            if portable:
                for directory in set(portable['cwd_map'].values()) | {str(portable['base'])}:
                    folder = Path(directory)
                    m.require(folder.resolve() == folder and folder.is_relative_to(portable['base']), '目标目录包含链接。')
                    folder.mkdir(parents=True, exist_ok=True)
                for asset, target in portable['files']:
                    guard()
                    if target.exists():
                        m.require(restore.checksum(target) == asset['sha256'], '目标文件已变化，停止导入。')
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    m.require(target.resolve() == target, '目标路径包含链接。')
                    with target.open('xb') as output, archive.open(asset['path']) as source:
                        written.append(target)
                        import shutil
                        shutil.copyfileobj(source, output, 1024 * 1024)
                restore.restore_metadata(conn, portable, offsets)
            for target, content, values in prepared:
                guard()
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output:
                    written.append(target)
                    output.write(content)
                fields = ','.join('"' + name.replace('"', '""') + '"' for name in values)
                conn.execute('insert into threads (' + fields + ') values (' + ','.join('?' for _ in values) + ')', list(values.values()))
            guard()
            if portable:
                journal['ui_after_sha256'] = m.sha((json.dumps(updated_ui, ensure_ascii=False, indent=2) + '\n').encode())
                m.save(backup / 'manifest.json', journal)
                m.save(ui_path, updated_ui)
                ui_written = True
            conn.commit()
            committed = True
            journal['state'] = 'applied'
            m.save(backup / 'manifest.json', journal)
        except Exception:
            if not committed:
                conn.rollback()
                for target in written:
                    target.unlink(missing_ok=True)
                if ui_written:
                    if previous_ui is None:
                        ui_path.unlink(missing_ok=True)
                    else:
                        m.atomic(ui_path, previous_ui)
                journal['state'] = 'reverted'
                m.save(backup / 'manifest.json', journal)
            raise
    return {'count': len(prepared), 'skipped': skipped, 'backup': str(backup), 'files': len(portable['files']) if portable else 0}


def pending(store):
    return [p for p in (Path(store) / 'conversation-import-backups').glob('*/manifest.json')
            if m.load(p).get('state') in ('prepared', 'recovery_required')]


def recover(root, store, guard=m.idle):
    root = Path(root).resolve()
    for path in pending(store):
        guard()
        journal = m.load(path)
        for item in journal['files']:
            m.session_path(root, item['path'])
        for item in journal.get('assets', []):
            target = Path(item['path'])
            m.require(target.resolve().is_relative_to(Path(journal['asset_base']).resolve()) and target.resolve() == target, '恢复清单文件路径不安全。')
        db = Path(journal['database'])
        m.require(db.resolve().parent == root and db.name.startswith('state_'), '恢复清单数据库路径无效。')
        with closing(m.connect(db)) as conn:
            present = []
            for item in journal['files']:
                row = conn.execute('select rollout_path from threads where id=?', (item['id'],)).fetchone()
                if row:
                    m.require(str(Path(row[0]).resolve()) == str(Path(item['path']).resolve()), '已有对话路径发生变化，停止自动恢复。')
                    present.append(item)
        m.require(not present or len(present) == len(journal['files']), '导入状态不完整，请保留备份并检查数据库。')
        if present:
            m.require(all(Path(item['path']).is_file() and restore.checksum(Path(item['path'])) == item['sha256'] for item in journal['files']), '导入记录与会话文件不一致，停止自动恢复。')
            if journal.get('portable'):
                if journal.get('history_database'):
                    history = Path(journal['history_database'])
                    m.require(history.resolve().parent == root and history.exists(), '导入历史数据库不存在。')
                    with closing(m.connect(history, True)) as conn:
                        for table, expected in journal.get('history_counts', {}).items():
                            if expected:
                                m.require(table in restore.HISTORY_SCHEMA, '未知历史数据表。')
                                actual = sum(conn.execute(f'select count(*) from "{table}" where thread_id=?', (item['id'],)).fetchone()[0] for item in present)
                                m.require(actual >= expected, '历史数据库提交不完整，请保留备份并检查。')
                ui = root / '.codex-global-state.json'
                m.require(ui.exists() and m.sha(ui.read_bytes()) == journal.get('ui_after_sha256'), '界面状态发生变化，停止自动恢复。')
            journal['state'] = 'applied'
        else:
            history = journal.get('history_database')
            if history and Path(history).exists():
                m.require(Path(history).resolve().parent == root, '恢复清单历史数据库路径无效。')
                with closing(m.connect(history, True)) as conn:
                    for table in restore.HISTORY_SCHEMA:
                        if conn.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone():
                            for item in journal['files']:
                                m.require(not conn.execute(f'select 1 from "{table}" where thread_id=?', (item['id'],)).fetchone(), '历史数据库与主数据库提交状态不同，请保留备份并检查。')
            ui = root / '.codex-global-state.json'
            before = path.parent / 'global-state.json'
            if journal.get('portable') and journal.get('ui_after_sha256'):
                raw = ui.read_bytes() if ui.exists() else None
                expected_before = before.read_bytes() if journal.get('ui_before_present') else None
                m.require(raw == expected_before or raw is not None and m.sha(raw) == journal['ui_after_sha256'], '界面状态已被其他操作修改，停止自动恢复。')
                if raw != expected_before:
                    if expected_before is None:
                        ui.unlink(missing_ok=True)
                    else:
                        m.atomic(ui, expected_before)
            files = journal['files'] + [item for item in journal.get('assets', []) if item['created']]
            for item in files:
                target = Path(item['path'])
                if target.exists():
                    m.require(target.is_file() and not target.is_symlink() and restore.checksum(target) == item['sha256'], '导入文件已变化，停止自动清理。')
            for item in files:
                Path(item['path']).unlink(missing_ok=True)
            journal['state'] = 'reverted'
        m.save(path, journal)
    return {'ok': True}
