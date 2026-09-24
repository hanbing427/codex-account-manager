import base64
import io
import json
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

import desktop
import migrate as m
import oauth_maintenance as oauth
from maintenance import Keeper
import test_webui


def token(exp):
    payload = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).decode().rstrip('=')
    return 'header.' + payload + '.signature'


class MaintenanceTests(unittest.TestCase):
    setUp = test_webui.AccountManagerTests.setUp

    def add_oauth(self, valid=False):
        auth = {'tokens': {'access_token': token(time.time() + 7200 if valid else 1),
                           'refresh_token': 'old-refresh', 'account_id': 'test-oauth'}, 'extra': 'preserve'}
        pid = self.manager.create('OAuth', json.dumps(auth).encode())['id']
        self.manager.keeper.data['settings'].update(model='fixture-model')
        return pid, auth

    def updated(self, auth):
        return {**auth, 'tokens': {**auth['tokens'], 'refresh_token': 'new-refresh', 'access_token': token(time.time() + 7200)}}

    def test_rotation_synchronizes_saved_and_original_files(self):
        pid, auth = self.add_oauth()
        source = self.root / 'auth_test.json'
        m.save(source, auth)
        with patch.object(oauth, 'rotate', return_value=self.updated(auth)) as refresh, patch.object(oauth, 'probe') as probe:
            self.manager.keeper.tick(only=pid)
        refresh.assert_called_once()
        probe.assert_called_once()
        self.assertEqual(m.load(source)['tokens']['refresh_token'], 'new-refresh')
        self.assertEqual(m.load(self.manager.profile_dir(pid) / 'auth.json')['extra'], 'preserve')
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_a)
        self.assertEqual(self.manager.keeper.data['accounts'][pid]['status'], 'ok')
        self.assertFalse(self.manager.keeper.journal.exists())

    def test_active_expiring_token_is_not_rotated_while_codex_runs(self):
        pid, auth = self.add_oauth()
        m.save(self.root / 'auth.json', auth)
        with patch.object(m, 'busy', return_value=True), patch.object(oauth, 'rotate') as refresh, patch.object(oauth, 'probe') as probe:
            self.manager.keeper.tick(only=pid)
        refresh.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(self.manager.keeper.data['accounts'][pid]['status'], 'deferred')

    def test_active_valid_token_can_probe_without_rotation(self):
        pid, auth = self.add_oauth(valid=True)
        m.save(self.root / 'auth.json', auth)
        with patch.object(m, 'busy', return_value=True), patch.object(oauth, 'rotate') as refresh, patch.object(oauth, 'probe') as probe:
            self.manager.keeper.tick(only=pid)
        refresh.assert_not_called()
        probe.assert_called_once()

    def test_inactive_profile_failure_does_not_touch_current_auth(self):
        pid, _ = self.add_oauth()
        with patch.object(oauth, 'rotate', side_effect=oauth.MaintenanceError('需重新登录', terminal=True)) as refresh:
            self.manager.keeper.tick(only=pid)
            self.manager.keeper.tick(only=pid)
        refresh.assert_called_once()
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_a)
        self.assertEqual(self.manager.keeper.data['accounts'][pid]['status'], 'login_required')
        restarted = Keeper(self.manager)
        self.assertEqual(restarted.data['accounts'][pid]['status'], 'login_required')

    def test_api_keys_are_excluded(self):
        pid = self.manager.create('Key', self.auth_b)['id']
        with patch.object(oauth, 'rotate') as refresh, patch.object(oauth, 'probe') as probe:
            self.manager.keeper.tick(only=pid)
        refresh.assert_not_called()
        probe.assert_not_called()
        self.assertNotIn(pid, self.manager.keeper.data['accounts'])

    def test_retry_once_after_probe_401(self):
        pid, auth = self.add_oauth(valid=True)
        with patch.object(oauth, 'rotate', return_value=self.updated(auth)) as refresh, patch.object(oauth, 'probe', side_effect=[oauth.MaintenanceError('401', unauthorized=True), None]) as probe:
            self.manager.keeper.tick(only=pid)
        self.assertEqual(probe.call_count, 2)
        refresh.assert_called_once()

    def test_settings_persist_and_schedule_honors_interval(self):
        pid, _ = self.add_oauth(valid=True)
        keeper = self.manager.keeper
        keeper.settings({'enabled': True, 'probe': False, 'model': 'fixture', 'interval_minutes': 360})
        with patch.object(oauth, 'probe') as probe:
            keeper.tick()
            self.assertGreater(keeper.data['accounts'][pid]['next_due'], time.time() + 21500)
            keeper.tick()
            probe.assert_not_called()
        self.assertEqual(Keeper(self.manager).data['settings']['interval_minutes'], 360)

    def test_rotation_recovery_does_not_overwrite_external_changes(self):
        pid, auth = self.add_oauth()
        path = self.manager.profile_dir(pid) / 'auth.json'
        keeper = self.manager.keeper
        m.save(keeper.journal, {'auth': self.updated(auth), 'files': [{'path': str(path.relative_to(self.root)), 'before': m.sha(path.read_bytes())}]})
        external = self.updated(auth)
        external['tokens']['refresh_token'] = 'external-newer-refresh'
        m.save(path, external)
        keeper.replay_rotation()
        self.assertEqual(m.load(path)['tokens']['refresh_token'], 'external-newer-refresh')

    def test_probe_failure_keeps_rotated_token(self):
        pid, auth = self.add_oauth()
        with patch.object(oauth, 'rotate', return_value=self.updated(auth)), patch.object(oauth, 'probe', side_effect=oauth.MaintenanceError('模型不可用')):
            self.manager.keeper.tick(only=pid)
        self.assertEqual(m.load(self.manager.profile_dir(pid) / 'auth.json')['tokens']['refresh_token'], 'new-refresh')
        self.assertEqual(self.manager.keeper.data['accounts'][pid]['status'], 'error')

    def test_maintenance_skips_switch_in_progress(self):
        pid, _ = self.add_oauth()
        self.manager.job = {'state': 'closing'}
        with patch.object(oauth, 'rotate') as refresh:
            self.manager.keeper.tick(only=pid)
        refresh.assert_not_called()

    def test_restart_sequence_only_launches_after_successful_switch(self):
        pid = self.manager.create('Target', self.auth_b)['id']
        order = []
        original = self.manager.switch
        def switch(*args, **kwargs):
            result = original(*args, **kwargs)
            order.append('switch')
            return result
        with patch.object(self.manager, 'resolve_app', return_value='Fixture!App'), patch.object(desktop, 'close_desktop', side_effect=lambda cancel: order.append('close') or True), patch.object(m, 'busy', return_value=False), patch.object(self.manager, 'switch', side_effect=switch), patch.object(self.manager, 'launch', side_effect=lambda **kwargs: order.append('launch')):
            self.manager.start_job('switch', {'id': pid})
            deadline = time.time() + 10
            while self.manager.running_job() and time.time() < deadline:
                time.sleep(.02)
        self.assertEqual(order, ['close', 'switch', 'launch'])
        self.assertEqual(self.manager.job['state'], 'done')

    def test_failed_migration_does_not_launch(self):
        pid = self.manager.create('Target', self.auth_b)['id']
        with patch.object(self.manager, 'resolve_app', return_value='Fixture!App'), patch.object(desktop, 'close_desktop', return_value=True), patch.object(m, 'busy', return_value=False), patch.object(self.manager, 'switch', side_effect=RuntimeError('failed')), patch.object(self.manager, 'launch') as launch:
            self.manager.start_job('switch', {'id': pid})
            deadline = time.time() + 10
            while self.manager.running_job() and time.time() < deadline:
                time.sleep(.02)
        launch.assert_not_called()
        self.assertEqual(self.manager.job['state'], 'error')

    def test_launch_failure_preserves_successful_switch(self):
        pid = self.manager.create('Target', self.auth_b)['id']
        with patch.object(self.manager, 'resolve_app', return_value='Fixture!App'), patch.object(desktop, 'close_desktop', return_value=True), patch.object(m, 'busy', return_value=False), patch.object(self.manager, 'launch', side_effect=RuntimeError('startup failed')):
            self.manager.start_job('switch', {'id': pid})
            deadline = time.time() + 10
            while self.manager.running_job() and time.time() < deadline:
                time.sleep(.02)
        self.assertEqual((self.root / 'auth.json').read_bytes(), self.auth_b)
        self.assertEqual(self.manager.job['state'], 'error')
        self.assertIn('配置操作已完成', self.manager.job['message'])

    def test_default_interval_is_one_day(self):
        self.assertEqual(self.manager.keeper.data['settings']['interval_minutes'], 1440)

    def test_launch_fails_fast_if_another_thread_holds_operation_lock(self):
        ready = threading.Event()
        release = threading.Event()
        def hold():
            with self.manager.operation_lock:
                ready.set()
                release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(ready.wait(2))
            with self.assertRaisesRegex(RuntimeError, '稍后重试'):
                self.manager.launch(appid='Fixture!App')
        finally:
            release.set()
            thread.join(2)


class ProtocolTests(unittest.TestCase):
    def test_token_response_preserves_refresh_when_omitted(self):
        auth = {'tokens': {'access_token': 'old', 'refresh_token': 'keep', 'account_id': 'id'}, 'custom': 'keep'}
        with patch.object(oauth, 'open_request', return_value=io.BytesIO(b'{"access_token":"new"}')) as send:
            updated = oauth.rotate(auth)
        req = send.call_args.args[0]
        self.assertEqual(req.full_url, oauth.TOKEN_URL)
        self.assertIn(b'grant_type=refresh_token', req.data)
        self.assertEqual(updated['tokens']['refresh_token'], 'keep')
        self.assertEqual(updated['tokens']['access_token'], 'new')
        self.assertEqual(updated['custom'], 'keep')

    def test_probe_requires_completed_event_and_contains_no_tools_or_history(self):
        auth = {'tokens': {'access_token': 'fake', 'account_id': 'test'}}
        stream = b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        with patch.object(oauth, 'open_request', return_value=io.BytesIO(stream)) as send:
            oauth.probe(auth, 'fixture-model')
        req = send.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, oauth.RESPONSES_URL)
        self.assertFalse(body['store'])
        self.assertEqual(body['tools'], [])
        self.assertEqual(len(body['input']), 1)
        with patch.object(oauth, 'open_request', return_value=io.BytesIO(b'data: [DONE]\n\n')):
            with self.assertRaises(oauth.MaintenanceError):
                oauth.probe(auth, 'fixture-model')

    def test_http_error_does_not_echo_secret_body(self):
        error = urllib.error.HTTPError(oauth.TOKEN_URL, 400, 'bad', {}, io.BytesIO(b'secret-credential'))
        with patch.object(oauth.urllib.request.OpenerDirector, 'open', side_effect=error):
            with self.assertRaises(oauth.MaintenanceError) as caught:
                oauth.open_request(oauth.urllib.request.Request(oauth.TOKEN_URL), refresh=True)
        self.assertTrue(caught.exception.terminal)
        self.assertNotIn('secret-credential', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
