import json
import io
import zipfile
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import migrate as m
import webui


class AccountManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.private = patch.object(m, 'private_folder', lambda p: p.mkdir(parents=True, exist_ok=True))
        self.private.start()
        self.addCleanup(self.private.stop)
        self.manager = webui.Manager(self.root)
        self.auth_a = b'{"OPENAI_API_KEY":"fixture-a"}'
        self.auth_b = b'{"OPENAI_API_KEY":"fixture-b"}'
        self.config_a = b'model_provider="proxy"\n[model_providers.proxy]\nname="Fixture"\nbase_url="https://example.invalid/v1"\n'
        (self.root / 'auth.json').write_bytes(self.auth_a)
        (self.root / 'config.toml').write_bytes(self.config_a)
        self.session = self.root / 'sessions' / 'fixture.jsonl'
        self.session.parent.mkdir()
        self.original = (json.dumps({'type': 'session_meta', 'payload': {'id': 'fixture', 'model_provider': 'proxy'}}) + '\n' +
                         json.dumps({'type': 'event_msg', 'payload': {'message': 'proxy text must remain unchanged'}}) + '\n').encode()
        self.session.write_bytes(self.original)
        self.db = self.root / 'state_5.sqlite'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('create table threads (id text primary key, title text, model_provider text, archived integer, rollout_path text)')
            conn.execute('insert into threads values (?,?,?,?,?)', ('fixture', 'Test thread', 'proxy', 0, str(self.session)))
            conn.commit()

    def profile(self, config=None):
        return self.manager.create('Test account', self.auth_b, config)['id']

    def provider(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('select model_provider from threads').fetchone()[0]

    def test_editor_backup_validation_and_conflict(self):
        pid = self.profile(self.config_a)
        data = self.manager.details(pid)
        data.update(color='#5588ff', config='')
        self.manager.edit(data)
        self.manager.set_color(pid, '#5588ff')
        self.assertFalse((self.manager.profile_dir(pid) / 'config.toml').exists())
        self.assertEqual(self.manager.details(pid)['color'], '#5588ff')
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_a)
        revisions = list((self.manager.store / 'profile-revisions' / pid).glob('*/config.toml'))
        self.assertEqual(revisions[0].read_bytes(), self.config_a)
        with self.assertRaises(RuntimeError):
            self.manager.edit(data)
        data = self.manager.details(pid)
        data['auth'] = '{}'
        with self.assertRaises(RuntimeError):
            self.manager.edit(data)
        self.assertEqual((self.manager.profile_dir(pid) / 'auth.json').read_bytes(), self.auth_b)

    def test_edited_oauth_is_not_overwritten_by_previous_live_token(self):
        auth = b'{"tokens":{"account_id":"fixture-account","access_token":"old"}}'
        pid = self.manager.create('OAuth fixture', auth)['id']
        self.manager.switch(pid, guard=lambda: None)
        data = self.manager.details(pid)
        data['auth'] = '{"tokens":{"account_id":"fixture-account","access_token":"edited"}}'
        self.manager.edit(data)
        self.manager.switch(pid, guard=lambda: None)
        self.assertEqual((self.root / 'auth.json').read_text(), data['auth'])

    def test_delete_backups_removes_linked_files_only(self):
        pid = self.profile()
        first = self.manager.switch(pid, guard=lambda: None)['backup']
        manifest = m.load(self.manager.backups / first / 'manifest.json')
        linked = Path(manifest['migration'])
        second = self.manager.switch(pid, guard=lambda: None)['backup']
        live = (self.root / 'auth.json').read_bytes()
        result = self.manager.delete_backups([first, second, first])
        self.assertEqual(result, {'deleted': [first, second], 'failed': []})
        self.assertFalse(linked.exists())
        self.assertFalse((self.manager.backups / first).exists())
        self.assertEqual((self.root / 'auth.json').read_bytes(), live)
        self.assertTrue(self.manager.profile_dir(pid).exists())
        self.assertTrue(self.session.exists())

    def test_delete_prevalidates_entire_batch_and_protects_pending(self):
        bid = self.manager.switch(self.profile(), guard=lambda: None)['backup']
        with self.assertRaises(RuntimeError):
            self.manager.delete_backups([bid, '20000101-000000-00000000'])
        self.assertTrue((self.manager.backups / bid).exists())
        with self.assertRaises(RuntimeError):
            self.manager.delete_backups(['../profiles'])
        pending, _ = self.manager.backup('Pending fixture')
        with self.assertRaises(RuntimeError):
            self.manager.delete_backups([pending.name])
        self.assertTrue(pending.exists())

    def test_delete_rejects_external_migration_path(self):
        bid = self.manager.switch(self.profile(), guard=lambda: None)['backup']
        path = self.manager.backups / bid / 'manifest.json'
        manifest = m.load(path)
        manifest['migration'] = str(self.manager.profiles)
        m.save(path, manifest)
        with self.assertRaises(RuntimeError):
            self.manager.delete_backups([bid])
        self.assertTrue(self.manager.profiles.exists())

    def test_state_includes_more_than_thirty_backups(self):
        for index in range(35):
            path, manifest = self.manager.backup(str(index))
            manifest['state'] = 'reverted'
            m.save(path / 'manifest.json', manifest)
        self.assertEqual(len(self.manager.state()['backups']), 35)

    def test_color_is_separate_from_credentials_and_revisions(self):
        pid = self.profile(self.config_a)
        before = self.manager.details(pid)
        self.manager.set_color(pid, '#abcdef')
        after = self.manager.details(pid)
        self.assertEqual(before['auth'], after['auth'])
        self.assertEqual(before['config'], after['config'])
        self.assertEqual(before['revision'], after['revision'])
        self.assertFalse((self.manager.store / 'profile-revisions').exists())
        before['color'] = '#000000'
        self.manager.edit(before)
        self.assertEqual(self.manager.details(pid)['color'], '#abcdef')
        other = self.manager.create('Other', self.auth_a, self.config_a)['id']
        self.assertNotEqual(self.manager.details(other)['color'], self.manager.details(pid)['color'])
        with self.assertRaises(RuntimeError):
            self.manager.set_color(pid, 'red;bad')

    def test_export_preserves_raw_files_and_auth_only_mode(self):
        pid = self.manager.create('../账号:one', self.auth_b, self.config_a)['id']
        self.manager.create('Second', self.auth_a)
        self.manager.create('未命名配置', b'{"OPENAI_API_KEY":"hidden-fixture"}')
        self.manager.set_color(pid, '#123456')
        with zipfile.ZipFile(io.BytesIO(self.manager.export_profiles())) as archive:
            self.assertIsNone(archive.testzip())
            records = json.loads(archive.read('accounts.json'))['accounts']
            self.assertEqual(len(records), 2)
            first = next(r for r in records if r['name'] == '../账号:one')
            second = next(r for r in records if r['name'] == 'Second')
            self.assertEqual(first['color'], '#123456')
            self.assertEqual(archive.read(first['folder'] + '/auth.json'), self.auth_b)
            self.assertEqual(archive.read(first['folder'] + '/config.toml'), self.config_a)
            self.assertNotIn(second['folder'] + '/config.toml', archive.namelist())
            self.assertTrue(second['auto_config'])
            self.assertTrue(all('..' not in name and not name.startswith('/') for name in archive.namelist()))
            self.assertFalse(any('backups' in name or 'sessions' in name for name in archive.namelist()))
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_a)

    def test_export_single_account_excludes_other_accounts(self):
        pid = self.profile()
        self.manager.create('Other account', self.auth_a, self.config_a)
        with zipfile.ZipFile(io.BytesIO(self.manager.export_profiles(pid))) as archive:
            records = json.loads(archive.read('accounts.json'))['accounts']
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['name'], 'Test account')
            auth_files = [name for name in archive.namelist() if name.endswith('/auth.json')]
            self.assertEqual(len(auth_files), 1)
            self.assertEqual(archive.read(auth_files[0]), self.auth_b)
            self.assertFalse(any(name.endswith('/config.toml') for name in archive.namelist()))
        with self.assertRaises(RuntimeError):
            self.manager.export_profiles('../bad-id')
        with self.assertRaises(RuntimeError):
            self.manager.export_profiles('0' * 32)

    def test_export_to_chosen_directory(self):
        pid = self.profile()
        destination = self.root / '导出目录'
        destination.mkdir()
        result = self.manager.export_to_directory(str(destination), pid)
        target = Path(result['path'])
        self.assertEqual(target.parent, destination)
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(len(json.loads(archive.read('accounts.json'))['accounts']), 1)
        second = self.manager.export_to_directory(str(destination), pid)
        self.assertNotEqual(result['path'], second['path'])
        with self.assertRaises(RuntimeError):
            self.manager.export_to_directory('relative-folder', pid)
        with self.assertRaises(RuntimeError):
            self.manager.export_to_directory(str(destination / 'missing'), pid)

    def test_export_empty_and_busy_are_rejected(self):
        with self.assertRaises(RuntimeError):
            self.manager.export_profiles()
        self.profile()
        self.manager.job['state'] = 'running'
        with self.assertRaises(RuntimeError):
            self.manager.export_profiles()

    def test_preview_does_not_touch_live_files(self):
        preview = self.manager.preview(self.profile())
        self.assertEqual(preview['provider'], 'openai')
        self.assertEqual(preview['count'], 1)
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_a)
        self.assertEqual((self.root / 'config.toml').read_bytes(), self.config_a)
        self.assertEqual(self.session.read_bytes(), self.original)

    def test_auth_only_switch_and_restore(self):
        result = self.manager.switch(self.profile(), guard=lambda: None)
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_b)
        self.assertEqual((self.root / 'config.toml').read_bytes(), webui.DEFAULT_CONFIG)
        self.assertEqual(self.provider(), 'openai')
        self.assertEqual(self.session.read_bytes().splitlines()[1], self.original.splitlines()[1])
        self.manager.restore(result['backup'], guard=lambda: None)
        self.assertEqual(self.provider(), 'proxy')
        self.assertEqual(self.session.read_bytes(), self.original)
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_a)
        self.assertEqual((self.root / 'config.toml').read_bytes(), self.config_a)

    def test_forked_session_metadata_is_migrated(self):
        forked = (
            json.dumps({'type': 'session_meta', 'payload': {
                'id': 'fixture', 'session_id': 'parent',
                'forked_from_id': 'parent', 'model_provider': 'proxy'}}) + '\n' +
            json.dumps({'type': 'session_meta', 'payload': {
                'id': 'parent', 'session_id': 'parent', 'model_provider': 'proxy'}}) + '\n'
        ).encode()
        self.session.write_bytes(forked)
        self.manager.switch(self.profile(), guard=lambda: None)
        records = [json.loads(line) for line in self.session.read_text().splitlines()]
        self.assertEqual([r['payload']['model_provider'] for r in records], ['openai', 'openai'])

    def test_custom_config_preserved(self):
        config = b'model_provider="other"\nmodel="custom-model"\n[model_providers.other]\nname="Other"\nbase_url="https://example.invalid/v1"\n'
        self.manager.switch(self.profile(config), guard=lambda: None)
        self.assertEqual((self.root / 'config.toml').read_bytes(), config)
        self.assertEqual(self.provider(), 'other')

    def test_delete_profile_and_protect_active_profile(self):
        profile_id = self.profile()
        self.manager.delete(profile_id)
        self.assertFalse((self.manager.profiles / profile_id).exists())
        active_id = self.manager.create('Active', self.auth_a, self.config_a)['id']
        self.manager.switch(active_id, guard=lambda: None)
        with self.assertRaisesRegex(RuntimeError, '当前正在使用'):
            self.manager.delete(active_id)

    def test_state_marks_switched_profile_saved(self):
        profile_id = self.profile()
        self.manager.switch(profile_id, guard=lambda: None)
        state = self.manager.state()
        self.assertFalse(state['current']['unsaved'])
        self.assertTrue(state['current']['saved'])
        self.assertEqual(state['current']['save_name'], 'Test account')

    def test_config_dedup_ignores_machine_only_settings(self):
        first = b'model_provider="proxy"\nmodel="gpt-6"\n[model_providers.proxy]\nname="Fixture"\nbase_url="https://example.invalid/v1"\n[desktop]\nappearanceTheme="dark"\n'
        second = b'model_provider="proxy"\nmodel="gpt-6"\n[model_providers.proxy]\nname="Fixture"\nbase_url="https://example.invalid/v1"\n[desktop]\nappearanceTheme="light"\n'
        self.assertTrue(webui.same_config(first, second))
        self.manager.create('First', self.auth_b, first)
        profile = self.manager.create('Second', self.auth_b, second)
        self.assertEqual(profile['id'], next(p.name for p in self.manager.profiles.iterdir()
                                             if json.loads((p / 'meta.json').read_text())['name'] == 'First'))

    def test_switch_updates_thread_model_when_schema_has_model(self):
        config = b'model_provider="other"\nmodel="custom-model"\n[model_providers.other]\nname="Other"\nbase_url="https://example.invalid/v1"\n'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('alter table threads add column model text')
            conn.execute('update threads set model=?', ('old-model',))
            conn.commit()
        self.manager.switch(self.profile(config), guard=lambda: None)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('select model from threads').fetchone()[0], 'custom-model')

    def test_failure_after_migration_restores_entire_operation(self):
        original_apply = m.apply_plan
        def fail_after_apply(*args, **kwargs):
            original_apply(*args, **kwargs)
            raise RuntimeError('Injected failure')
        with patch.object(m, 'apply_plan', fail_after_apply):
            with self.assertRaisesRegex(RuntimeError, 'Injected'):
                self.manager.switch(self.profile(), guard=lambda: None)
        self.assertEqual(self.provider(), 'proxy')
        self.assertEqual(self.session.read_bytes(), self.original)
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_a)
        self.assertEqual((self.root / 'config.toml').read_bytes(), self.config_a)
        self.assertFalse(self.manager.pending())

    def test_new_conversation_blocks_rollback(self):
        result = self.manager.switch(self.profile(), guard=lambda: None)
        updated = self.session.read_bytes() + b'{"type":"event_msg","payload":{"message":"new"}}\n'
        self.session.write_bytes(updated)
        with self.assertRaisesRegex(RuntimeError, 'changed since migration'):
            self.manager.restore(result['backup'], guard=lambda: None)
        self.assertEqual(self.session.read_bytes(), updated)
        self.assertEqual(self.provider(), 'openai')

    def test_unknown_schema_fails_before_config_write(self):
        self.db.unlink()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('create table threads (id text)')
        with self.assertRaisesRegex(RuntimeError, 'uniquely identify'):
            self.manager.switch(self.profile(), guard=lambda: None)
        self.assertEqual((self.root / 'config.toml').read_bytes(), self.config_a)

    def test_fresh_home_without_state_database(self):
        self.db.unlink()
        self.manager.switch(self.profile(), guard=lambda: None)
        self.assertEqual((self.root / 'config.toml').read_bytes(), webui.DEFAULT_CONFIG)

    def test_import_discovery_and_deduplication(self):
        (self.root / 'auth_person.json').write_bytes(self.auth_b)
        (self.root / 'config_person.toml').write_bytes(self.config_a)
        candidate = self.manager.candidates()[0]
        self.assertEqual(candidate['name'], 'person')
        a = self.manager.import_candidate(candidate['key'])
        b = self.manager.import_candidate(candidate['key'])
        self.assertEqual(a, b)
        with self.assertRaises(RuntimeError):
            self.manager.import_candidate('../auth.json')

    def test_validation_does_not_echo_secrets(self):
        for raw in (b'{}', b'[]', b'{secret-key', b'{"tokens":{}}'):
            with self.assertRaises(RuntimeError) as caught:
                self.manager.create('Bad', raw)
            self.assertNotIn('secret-key', str(caught.exception))
        with self.assertRaises(RuntimeError):
            self.profile(b'profile="unknown"')

    def test_busy_guard_prevents_writes(self):
        def blocked():
            raise RuntimeError('Codex running')
        with self.assertRaisesRegex(RuntimeError, 'Codex running'):
            self.manager.switch(self.profile(), guard=blocked)
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_a)
        self.assertEqual(self.provider(), 'proxy')

    def test_refreshed_oauth_token_preserved_when_switching_same_account(self):
        old = json.dumps({'tokens': {'account_id': 'test-account', 'access_token': 'old'}}).encode()
        new = json.dumps({'tokens': {'account_id': 'test-account', 'access_token': 'refreshed'}}).encode()
        pid = self.manager.create('OAuth', old)['id']
        self.manager.switch(pid, guard=lambda: None)
        (self.root / 'auth.json').write_bytes(new)
        self.manager.switch(pid, guard=lambda: None)
        self.assertEqual((self.root / 'auth.json').read_bytes(), new)
        self.assertEqual((self.manager.profile_dir(pid) / 'auth.json').read_bytes(), new)

    def test_archived_conversations_are_opt_in(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('update threads set archived=1')
            conn.commit()
        pid = self.profile()
        self.assertEqual(self.manager.preview(pid)['count'], 0)
        self.assertEqual(self.manager.preview(pid, archived=True)['count'], 1)
        self.manager.switch(pid, archived=True, guard=lambda: None)
        self.assertEqual(self.provider(), 'openai')

    def test_restart_can_recover_partial_file_switch(self):
        path, manifest = self.manager.backup('Interrupted switch')
        (self.root / 'auth.json').write_bytes(self.auth_b)
        restarted = webui.Manager(self.root)
        self.assertTrue(restarted.state()['recovery_required'])
        restarted.restore(path.name, guard=lambda: None)
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_a)
        self.assertFalse(restarted.pending())

    def test_wait_can_be_cancelled_without_writes(self):
        pid = self.profile()
        with patch.object(m, 'busy', return_value=True):
            self.manager.start_job('switch', {'id': pid, 'restart': False})
            self.manager.cancel.set()
            deadline = time.monotonic() + 5
            while self.manager.job['state'] == 'waiting' and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(self.manager.job['state'], 'cancelled')
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_a)
        self.assertEqual((self.root / 'config.toml').read_bytes(), self.config_a)

    def test_launch_generates_config_without_invoking_real_desktop(self):
        (self.root / 'config.toml').unlink()
        result = type('Result', (), {'stdout': 'OpenAI.Codex_fixture!App'})()
        with patch.object(m, 'busy', return_value=False), patch.object(webui.subprocess, 'run', return_value=result), patch.object(webui.subprocess, 'Popen') as launch:
            self.manager.launch()
            launch.assert_called_once_with(['explorer.exe', 'shell:AppsFolder\\OpenAI.Codex_fixture!App'])
        self.assertEqual((self.root / 'config.toml').read_bytes(), webui.DEFAULT_CONFIG)

    def test_rollback_refusal_does_not_block_a_new_switch(self):
        result = self.manager.switch(self.profile(), guard=lambda: None)
        self.session.write_bytes(self.session.read_bytes() + b'{"type":"event_msg","payload":{}}\n')
        with self.assertRaises(RuntimeError):
            self.manager.restore(result['backup'], guard=lambda: None)
        self.assertFalse(self.manager.pending())


if __name__ == '__main__':
    unittest.main()
