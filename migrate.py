"""Offline Codex provider migration. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from portable import file_lock

VERSION = 1
HERE = Path(__file__).resolve().parent
BUILTINS = {'openai', 'ollama', 'lmstudio', 'amazon-bedrock'}

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def atomic(path, data):
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def save(path, obj):
    atomic(path, (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))

def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def connect(path, readonly=False):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def configuration(root):
    path = root / 'config.toml'
    raw = path.read_bytes()
    cfg = tomllib.loads(raw.decode('utf-8-sig'))
    # A profile can change provider selection. Refuse ambiguity instead of guessing.
    require(not cfg.get('profile'), 'config.toml selects a profile. Put the intended provider in the top-level config and remove profile before migration.')
    target = cfg.get('model_provider', 'openai')
    require(target in BUILTINS or target in cfg.get('model_providers', {}), f'Provider definition missing: {target}')
    return {'path': str(path), 'sha256': sha(raw), 'target': target, 'model': cfg.get('model')}

def database(root, explicit=None):
    choices = [Path(explicit).resolve()] if explicit else sorted(root.glob('state_*.sqlite'))
    valid = []
    for path in choices:
        try:
            with closing(connect(path, True)) as conn:
                columns = {r['name'] for r in conn.execute('pragma table_info(threads)')}
                if {'id', 'rollout_path', 'model_provider', 'archived', 'title'} <= columns:
                    valid.append(path.resolve())
        except sqlite3.Error:
            continue
    require(len(valid) == 1, 'Cannot uniquely identify the state database. Use --database with its full path. Candidates: ' + ', '.join(map(str, valid)))
    require(valid[0].parent == root, 'State database must be directly inside CODEX_HOME.')
    return valid[0]

def session_path(root, value):
    value = str(value)
    # SQLite often stores Windows extended-length paths; pathlib preserves that
    # prefix, which otherwise defeats containment/equality checks against home.
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    path = Path(value).resolve()
    require(any(path.is_relative_to(root / folder) for folder in ('sessions', 'archived_sessions')), f'Session path outside CODEX_HOME session folders: {path}')
    return path

def transform(raw, tid, source, target):
    """Change execution metadata only; keep every other line byte-for-byte."""
    require(source != target, 'Source and target must differ.')
    result, meta = [], 0
    # Forked Codex sessions can contain one session_meta record for the
    # current thread and additional records for its parent chain. The first
    # record must identify the database row; later records are accepted only
    # when their IDs were explicitly referenced by an already-seen metadata
    # record, which keeps the integrity check while supporting forks.
    related_ids = {str(tid)}
    for line in raw.splitlines(keepends=True):
        obj = json.loads(line)
        payload = obj.get('payload', {})
        changed = False
        if obj.get('type') == 'session_meta':
            session_id = str(payload.get('id', ''))
            require(session_id in related_ids, f'Session ID mismatch: {tid}')
            require(payload.get('model_provider') == source, f'Session provider mismatch: {tid}')
            for key in ('id', 'session_id', 'forked_from_id', 'parent_thread_id'):
                value = payload.get(key)
                if value:
                    related_ids.add(str(value))
            meta += 1
            changed = True
        elif obj.get('type') == 'turn_context' and 'model_provider' in payload:
            require(payload['model_provider'] == source, f'Unexpected turn provider: {tid}')
            changed = True
        if changed:
            payload['model_provider'] = target
            end = b'\r\n' if line.endswith(b'\r\n') else b'\n' if line.endswith(b'\n') else b''
            encoded = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + end
            # Verify the only semantic difference is the selected field.
            check = json.loads(encoded)
            check['payload']['model_provider'] = source
            require(check == json.loads(line), f'Unexpected content change: {tid}')
            result.append(encoded)
        else:
            result.append(line)
    require(meta >= 1, f'Expected at least one session_meta record: {tid}; found {meta}')
    return b''.join(result)

def make_plan(root, db=None, sources=None, ids=None, archived=False, config=None):
    cfg = config if config is not None else configuration(root)
    db = database(root, db)
    records = []
    with closing(connect(db, True)) as conn:
        columns = {r['name'] for r in conn.execute('pragma table_info(threads)')}
        model_column = 'model' if 'model' in columns else 'NULL AS model'
        query = f'select id,title,model_provider,{model_column},archived,rollout_path from threads order by id'
        for row in conn.execute(query):
            if row['model_provider'] == cfg['target']:
                continue
            if row['archived'] and not archived:
                continue
            if sources and row['model_provider'] not in sources:
                continue
            if ids and row['id'] not in ids:
                continue
            path = session_path(root, row['rollout_path'])
            raw = path.read_bytes()
            updated = transform(raw, row['id'], row['model_provider'], cfg['target'])
            records.append({'id': row['id'], 'title': row['title'], 'source': row['model_provider'],
                            'model': row['model'], 'archived': row['archived'], 'path': str(path),
                            'before_sha256': sha(raw), 'after_sha256': sha(updated)})
    if ids:
        missing = set(ids) - {r['id'] for r in records}
        require(not missing, 'Requested IDs not eligible (already target, archived, filtered or missing): ' + ', '.join(sorted(missing)))
    return {'version': VERSION, 'created_at': datetime.now().isoformat(), 'home': str(root), 'database': str(db), 'config': cfg, 'records': records}

def preview(plan):
    print(f"\nCODEX_HOME: {plan['home']}\nDatabase: {plan['database']}\nTarget provider: {plan['config']['target']}\nCurrent model: {plan['config']['model']}\nSelected conversations: {len(plan['records'])}\n")
    for item in plan['records']:
        title = ' '.join(item['title'].split())[:72]
        print(f"{item['id']}  {item['source']} -> {plan['config']['target']}  {'[archived] ' if item['archived'] else ''}{title}")
    print('\nConfig, credentials, model selection and conversation text will not be changed.')

def busy():
    if os.name != 'nt':
        from portable import processes, is_codex
        return any(is_codex(p['executable']) for p in processes())
    command = "@(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'codex.exe' -or $_.Name -eq 'Codex.exe' -or ($_.Name -eq 'ChatGPT.exe' -and $_.ExecutablePath -like '*OpenAI.Codex*') }).Count"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return int(result.stdout.strip()) > 0

def idle():
    require(not busy(), 'Codex is running. Exit Codex desktop, CLI and IDE sessions, then retry.')

def wait_for_exit(minutes):
    deadline = time.monotonic() + minutes * 60
    print('\nWAITING: Fully quit Codex. Keep this terminal open. Do not reopen Codex until SUCCESS.', flush=True)
    while busy():
        require(time.monotonic() < deadline, 'Wait timed out; no migration started.')
        time.sleep(3)
    time.sleep(2)
    idle()

@contextmanager
def lock(root):
    with file_lock(root / 'provider-migration.lock'):
        yield

def private_folder(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        path.chmod(0o700)
    if os.name == 'nt':
        sid = subprocess.run(['powershell.exe', '-NoProfile', '-Command', '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'], capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        require(sid.startswith('S-1-'), 'Cannot determine current user SID.')
        subprocess.run(['icacls.exe', str(path), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'], capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)

def check_row(conn, record, provider, root, model=None):
    columns = {r['name'] for r in conn.execute('pragma table_info(threads)')}
    model_column = ',model' if 'model' in columns else ''
    row = conn.execute(f'select model_provider,archived,rollout_path{model_column} from threads where id=?', (record['id'],)).fetchone()
    require(row is not None and row['model_provider'] == provider and row['archived'] == record['archived'], f'Database record changed: {record["id"]}; generate a fresh plan.')
    if model is not None and 'model' in columns:
        require(row['model'] == model, f'Database model changed: {record["id"]}; generate a fresh plan.')
    require(session_path(root, row['rollout_path']) == Path(record['path']), f'Session path changed: {record["id"]}')
    return row

def sqlite_backup(source, destination):
    with closing(connect(source, True)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        require(dst.execute('pragma integrity_check').fetchone()[0] == 'ok', 'Backup integrity check failed.')

def write_transaction(conn, prepared, guard):
    """prepared entries: record, old/new provider, old/new model, session bytes."""
    written = []
    conn.execute('begin immediate')
    try:
        for record, old, new, old_model, new_model, before, after in prepared:
            guard()
            path = Path(record['path'])
            require(path.read_bytes() == before, f'Session changed while migrating: {path}')
            columns = {r['name'] for r in conn.execute('pragma table_info(threads)')}
            if 'model' in columns:
                cursor = conn.execute('update threads set model=?, model_provider=? where id=? and model_provider=? and archived=?',
                                      (new_model, new, record['id'], old, record['archived']))
            else:
                cursor = conn.execute('update threads set model_provider=? where id=? and model_provider=? and archived=?',
                                      (new, record['id'], old, record['archived']))
            require(cursor.rowcount == 1, f'Unexpected update count: {record["id"]}')
            atomic(path, after)
            written.append((path, before))
            require(path.read_bytes() == after, f'Write verification failed: {path}')
        guard()
        require(conn.execute('pragma integrity_check').fetchone()[0] == 'ok', 'Database integrity check failed.')
        conn.commit()
    except BaseException:
        conn.rollback()
        for path, original in reversed(written):
            atomic(path, original)
        raise

def apply_plan(plan, guard=idle, on_backup=None):
    require(plan['version'] == VERSION, 'Unsupported plan version.')
    root = Path(plan['home']).resolve()
    require(configuration(root) == plan['config'], 'Config changed after preview. Generate a new plan.')
    require(plan['records'], 'Nothing to migrate.')
    require(len({r['id'] for r in plan['records']}) == len(plan['records']), 'Duplicate IDs in plan.')
    db = database(root, plan['database'])
    guard()
    with closing(connect(db)) as conn:
        prepared = []
        for record in plan['records']:
            row = check_row(conn, record, record['source'], root)
            before = Path(record['path']).read_bytes()
            require(sha(before) == record['before_sha256'], 'Conversation changed after preview. Generate a new plan: ' + record['id'])
            after = transform(before, record['id'], record['source'], plan['config']['target'])
            require(sha(after) == record['after_sha256'], 'Plan content mismatch.')
            prepared.append((record, record['source'], plan['config']['target'], row['model'] if 'model' in row.keys() else None,
                             plan['config']['model'], before, after))
        base = root / 'provider-migration-backups'
        private_folder(base)
        backup = base / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        backup.mkdir()
        (backup / 'sessions').mkdir()
        # Config is reference only. Credentials are never read or backed up.
        (backup / 'config.toml').write_bytes((root / 'config.toml').read_bytes())
        sqlite_backup(db, backup / db.name)
        for history in root.glob('thread_history_*.sqlite'):
            sqlite_backup(history, backup / history.name)
        for record, _, _, _, _, before, _ in prepared:
            (backup / 'sessions' / (record['id'] + '.jsonl')).write_bytes(before)
        manifest = {'version': VERSION, 'state': 'prepared', 'plan': plan}
        save(backup / 'manifest.json', manifest)
        if on_backup is not None:
            on_backup(backup)
        print(f'Backup: {backup}', flush=True)
        guard()
        require(configuration(root) == plan['config'], 'Config changed before writing.')
        try:
            write_transaction(conn, prepared, guard)
        except BaseException:
            # "prepared" also supports recovering an interrupted partial write.
            print(f'Migration stopped. Inspect or rollback: {backup}', flush=True)
            raise
        manifest['state'] = 'applied'
        save(backup / 'manifest.json', manifest)
        for record, _, new, _, new_model, _, after in prepared:
            check_row(conn, record, new, root, new_model)
            require(Path(record['path']).read_bytes() == after, 'Post-commit file verification failed.')
    return backup

def rollback(backup, guard=idle):
    backup = Path(backup).resolve()
    manifest = load(backup / 'manifest.json')
    require(manifest['version'] == VERSION, 'Unsupported manifest version.')
    require(manifest['state'] in ('prepared', 'applied'), 'Backup already rolled back or unsupported state.')
    plan = manifest['plan']
    root = Path(plan['home']).resolve()
    db = database(root, plan['database'])
    guard()
    with closing(connect(db)) as conn:
        prepared = []
        for record in plan['records']:
            original = (backup / 'sessions' / (record['id'] + '.jsonl')).read_bytes()
            require(sha(original) == record['before_sha256'], 'Backup hash mismatch: ' + record['id'])
            current = session_path(root, record['path']).read_bytes()
            require(sha(current) in (record['before_sha256'], record['after_sha256']), 'Conversation has changed since migration; rollback refused to preserve newer history: ' + record['id'])
            columns = {r['name'] for r in conn.execute('pragma table_info(threads)')}
            model_column = ',model' if 'model' in columns else ''
            row = conn.execute(f'select model_provider{model_column} from threads where id=?', (record['id'],)).fetchone()
            require(row and row['model_provider'] in (record['source'], plan['config']['target']), 'Provider changed since migration.')
            check_row(conn, record, row['model_provider'], root)
            prepared.append((record, row['model_provider'], record['source'], row['model'] if 'model' in row.keys() else None,
                             record.get('model'), current, original))
        # Save the exact pre-rollback state too. Never overwrite the entire live DB.
        recovery = backup / ('before-rollback-' + uuid.uuid4().hex[:8])
        recovery.mkdir()
        sqlite_backup(db, recovery / db.name)
        for record, _, _, _, _, before, _ in prepared:
            (recovery / (record['id'] + '.jsonl')).write_bytes(before)
        write_transaction(conn, prepared, guard)
        for record, _, new, _, new_model, _, after in prepared:
            check_row(conn, record, new, root, new_model)
            require(Path(record['path']).read_bytes() == after, 'Rollback verification failed.')
        manifest['state'] = 'rolled_back'
        save(backup / 'manifest.json', manifest)

def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', nargs='?', default='wizard', choices=['wizard', 'plan', 'apply', 'rollback'])
    p.add_argument('--home', type=Path, default=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))))
    p.add_argument('--database', type=Path)
    p.add_argument('--from-provider', action='append', dest='sources', help='Repeat to select multiple old providers')
    p.add_argument('--id', action='append', dest='ids', help='Repeat to select explicit conversation IDs')
    p.add_argument('--include-archived', action='store_true')
    p.add_argument('--plan', type=Path, default=HERE / 'migration-plan.json')
    p.add_argument('--backup', type=Path, help='Backup directory for rollback')
    p.add_argument('--wait', action='store_true', help='Wait for Codex to exit before writing')
    p.add_argument('--timeout-minutes', type=float, default=60)
    return p

def main():
    args = parser().parse_args()
    root = args.home.resolve()
    require(root.is_dir(), f'CODEX_HOME does not exist: {root}')
    if args.command in ('wizard', 'plan'):
        plan = make_plan(root, args.database, args.sources, args.ids, args.include_archived)
        preview(plan)
        args.plan.parent.mkdir(parents=True, exist_ok=True)
        save(args.plan, plan)
        print(f'Plan saved: {args.plan}')
        if args.command == 'plan' or not plan['records']:
            return
        print('\nFirst verify the NEW config/login with a new Codex conversation. When you continue migrated conversations, their history is sent to the selected provider.')
        choice = input('Type MIGRATE to approve the preview, or Enter to cancel: ').strip()
        if choice != 'MIGRATE':
            print('Cancelled; conversations unchanged.')
            return
        args.wait = True
    elif args.command == 'apply':
        plan = load(args.plan)
        root = Path(plan['home']).resolve()
        preview(plan)
    else:
        require(args.backup is not None, 'rollback requires --backup DIRECTORY')
        root = Path(load(args.backup / 'manifest.json')['plan']['home']).resolve()
    with lock(root):
        if args.wait:
            wait_for_exit(args.timeout_minutes)
        else:
            idle()
        if args.command == 'rollback':
            rollback(args.backup)
            print('SUCCESS: Provider metadata restored. Global config was not changed.')
        else:
            backup = apply_plan(plan)
            print(f'SUCCESS: Migrated {len(plan["records"])} conversations. Backup: {backup}')
            print('Reopen Codex and send a message in an old conversation to verify inference.')

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nCANCELLED. If writing had begun, inspect the printed backup directory.', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f'FAILED: {exc}', file=sys.stderr)
        sys.exit(1)
