"""Persistent, serialized maintenance of saved OAuth credential files."""
import hashlib
import json
from pathlib import Path
import threading
import time

import migrate as m
import oauth_maintenance as oauth


class Keeper:
    def __init__(self, manager):
        self.manager = manager
        self.path = manager.store / 'maintenance.json'
        self.journal = manager.store / 'token-rotation.json'
        self.running = False
        self.stop = threading.Event()
        model = ''
        try:
            model = m.configuration(manager.root).get('model') or ''
        except (OSError, RuntimeError, ValueError):
            pass
        default = {'enabled': True, 'interval_minutes': 1440, 'probe': True, 'model': model}
        self.data = m.load(self.path) if self.path.exists() else {'settings': default, 'accounts': {}}
        self.data['settings'] = {**default, **self.data['settings']}

    def save(self):
        m.save(self.path, self.data)

    def public(self):
        return {'settings': self.data['settings'].copy(), 'running': self.running,
                'accounts': {pid: {k: v for k, v in value.copy().items() if k != 'fingerprint'}
                             for pid, value in list(self.data['accounts'].items())}}

    def settings(self, value):
        interval = value.get('interval_minutes')
        m.require(isinstance(interval, int) and not isinstance(interval, bool) and 5 <= interval <= 10080,
                  '间隔须为 5–10080 分钟。')
        m.require(isinstance(value.get('enabled'), bool) and isinstance(value.get('probe'), bool), '设置格式错误。')
        model = value.get('model', '')
        m.require(isinstance(model, str) and len(model) <= 128 and '\n' not in model, '模型名称格式错误。')
        self.data['settings'] = {'enabled': value['enabled'], 'probe': value['probe'],
                                 'interval_minutes': interval, 'model': model.strip()}
        for record in self.data['accounts'].values():
            record['next_due'] = time.time() + interval * 60
        self.save()
        return {'ok': True}

    def credential_paths(self):
        paths = [self.manager.root / 'auth.json']
        paths += [self.manager.root / x['auth'] for x in self.manager.candidates()]
        paths += list(self.manager.profiles.glob('*/auth.json'))
        return [p for p in paths if p.exists()]

    @staticmethod
    def read(path):
        try:
            obj = json.loads(path.read_bytes().decode('utf-8-sig'))
            return obj if isinstance(obj, dict) else {}
        except (OSError, ValueError):
            return {}

    def replay_rotation(self):
        if not self.journal.exists():
            return
        record = m.load(self.journal)
        root = self.manager.root
        for item in record['files']:
            path = (root / item['path']).resolve()
            m.require(path in self.credential_paths(), '凭据恢复目标已变化，请保留 token-rotation.json 并检查文件。')
            current = path.read_bytes()
            if m.sha(current) != item['before']:
                continue
            if path == root / 'auth.json':
                m.require(not m.busy(), '等待 Codex 退出后完成凭据同步。')
            m.save(path, record['auth'])
        self.journal.unlink()

    def persist_rotation(self, old, updated):
        refresh = old['tokens'].get('refresh_token')
        targets = []
        for path in self.credential_paths():
            auth = self.read(path)
            if oauth.oauth(auth) and auth['tokens'].get('refresh_token') == refresh:
                targets.append({'path': str(path.relative_to(self.manager.root)), 'before': m.sha(path.read_bytes())})
        # Write-ahead recovery preserves a newly rotated token even if copying fails.
        m.save(self.journal, {'auth': updated, 'files': targets})
        self.replay_rotation()

    def is_live(self, auth):
        live = self.read(self.manager.root / 'auth.json')
        if not oauth.oauth(live):
            return False
        a, b = auth['tokens'], live['tokens']
        return (bool(a.get('account_id')) and a.get('account_id') == b.get('account_id')
                or a.get('refresh_token') and a.get('refresh_token') == b.get('refresh_token')
                or a.get('access_token') == b.get('access_token'))

    def maintain(self, pid):
        self.replay_rotation()
        meta, raw, _, cfg = self.manager.read_profile(pid)
        auth = json.loads(raw.decode('utf-8-sig'))
        if not oauth.oauth(auth):
            return
        settings = self.data['settings']
        previous = self.data['accounts'].get(pid, {})
        now = time.time()
        record = {**previous, 'last_attempt': now, 'next_due': now + settings['interval_minutes'] * 60,
                  'status': 'running', 'message': '正在检查凭据', 'fingerprint': m.sha(raw)}
        self.data['accounts'][pid] = record
        self.save()
        try:
            live_running = self.is_live(auth) and m.busy()
            if live_running:
                # The desktop owns its refresh token while running. Use its latest access token for probing.
                live = self.read(self.manager.root / 'auth.json')
                if oauth.oauth(live):
                    auth = live
            due = oauth.expiry(auth) <= now + 600
            if due and live_running:
                record.update(status='deferred', message='当前账号由 Codex 管理刷新；退出后再维护。', next_due=now + 300)
                return
            if due:
                updated = oauth.rotate(auth)
                self.persist_rotation(auth, updated)
                auth = updated
                record['last_refresh'] = time.time()
            if settings['probe']:
                try:
                    oauth.probe(auth, settings['model'] or cfg['model'])
                except oauth.MaintenanceError as exc:
                    if not exc.unauthorized or live_running or due:
                        raise
                    updated = oauth.rotate(auth)
                    self.persist_rotation(auth, updated)
                    auth = updated
                    record['last_refresh'] = time.time()
                    oauth.probe(auth, settings['model'] or cfg['model'])
                record['last_probe'] = time.time()
            record.update(status='ok', message='凭据有效，短消息探测成功。' if settings['probe'] else '凭据检查完成。', failures=0)
        except oauth.MaintenanceError as exc:
            failures = record.get('failures', 0) + 1
            record.update(status='login_required' if exc.terminal else 'error', message=str(exc), failures=failures,
                          next_due=time.time() + min(max(settings['interval_minutes'] * 60, 300 * 2 ** min(failures, 8)), 86400))
        except Exception:
            record.update(status='error', message='凭据同步未完成，请检查本机权限或等待 Codex 退出。', next_due=time.time() + 300)
        finally:
            path = self.manager.profile_dir(pid) / 'auth.json'
            record['fingerprint'] = m.sha(path.read_bytes())
            self.save()

    def tick(self, only=None):
        if not self.manager.operation_lock.acquire(blocking=False):
            return
        try:
            with self.manager.mutex:
                if self.stop.is_set() or self.manager.running_job() or self.manager.pending() or self.running:
                    return
                if only is None and not self.data['settings']['enabled']:
                    return
                self.running = True
            with m.lock(self.manager.root):
                seen = set()
                for path in sorted(self.manager.profiles.iterdir()):
                    if only and path.name != only:
                        continue
                    auth = self.read(path / 'auth.json')
                    if not oauth.oauth(auth):
                        continue
                    fingerprint = m.sha((path / 'auth.json').read_bytes())
                    record = self.data['accounts'].get(path.name)
                    if record is None and only is None:
                        self.data['accounts'][path.name] = {'status': 'scheduled', 'message': '等待首次维护',
                            'next_due': time.time() + self.data['settings']['interval_minutes'] * 60, 'fingerprint': fingerprint}
                        self.save()
                        continue
                    if record and record.get('status') == 'login_required' and record.get('fingerprint') == fingerprint:
                        continue
                    if only is None and record and record.get('next_due', 0) > time.time():
                        continue
                    key = auth['tokens'].get('refresh_token') or auth['tokens'].get('access_token')
                    digest = hashlib.sha256(key.encode()).hexdigest()
                    if digest in seen:
                        continue
                    self.maintain(path.name)
                    seen.add(digest)
                    new = self.read(path / 'auth.json')['tokens']
                    seen.add(hashlib.sha256((new.get('refresh_token') or new['access_token']).encode()).hexdigest())
        finally:
            with self.manager.mutex:
                self.running = False
            self.manager.operation_lock.release()

    def request(self, pid):
        self.manager.ensure_ready()
        _, raw, _, _ = self.manager.read_profile(pid)
        m.require(oauth.oauth(json.loads(raw.decode('utf-8-sig'))), 'API Key 账号无需 OAuth 维护。')
        threading.Thread(target=self.tick, args=(pid,), daemon=True).start()
        return {'ok': True}

    def start(self):
        def loop():
            while not self.stop.wait(15):
                try:
                    self.tick()
                except Exception:
                    # Keep the timer alive; never print tokens or upstream bodies.
                    continue
        threading.Thread(target=loop, daemon=True).start()
