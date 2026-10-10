"""Account recognition must survive model changes without trusting stale state."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from webui import Manager, same_config


AUTH = json.dumps({'OPENAI_API_KEY': 'test-only-key'}).encode()


def config(model='saved-model', endpoint='https://relay.example.invalid/v1', effort='high'):
    return (
        'model_provider = "relay"\n'
        f'model = "{model}"\n'
        f'model_reasoning_effort = "{effort}"\n'
        '[model_providers.relay]\n'
        'name = "Relay"\n'
        f'base_url = "{endpoint}"\n'
        'wire_api = "responses"\n'
    ).encode()


class ProfileIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = Manager(self.root)
        self.pid = self.manager.create('Saved relay', AUTH, config())['id']

    def live(self, cfg, auth=AUTH, active=True):
        (self.root / 'auth.json').write_bytes(auth)
        if cfg is not None:
            (self.root / 'config.toml').write_bytes(cfg)
        if active:
            (self.manager.store / 'active.json').write_text(json.dumps({'id': self.pid}))

    def assert_current(self, state, pid, changed):
        current = state['current']
        self.assertTrue(current['saved'])
        self.assertFalse(current['unsaved'])
        self.assertEqual(current['config_changed'], changed)
        marked = [p for p in state['profiles'] if p['active'] or p['matches']]
        self.assertEqual([p['id'] for p in marked], [pid])
        self.assertEqual(current['save_name'], marked[0]['name'])
        self.assertFalse(any(k.startswith('_') for p in state['profiles'] for k in p))
        self.assertNotIn('test-only-key', json.dumps(state))

    def test_model_change_keeps_account_and_live_model(self):
        self.live(config(model='current-model'))
        self.assert_current(self.manager.state(), self.pid, changed=True)
        self.assertEqual(self.manager.state()['current']['model'], 'current-model')
        self.assertEqual(self.manager.read_profile(self.pid)[3]['model'], 'saved-model')

    def test_reasoning_change_keeps_account_without_active_record(self):
        self.live(config(effort='low'), active=False)
        self.assert_current(self.manager.state(), self.pid, changed=True)

    def test_exact_configuration_is_unchanged(self):
        self.live(config())
        self.assert_current(self.manager.state(), self.pid, changed=False)

    def test_stale_active_record_does_not_override_different_key(self):
        self.live(config(), auth=json.dumps({'OPENAI_API_KEY': 'other-test-key'}).encode())
        state = self.manager.state()
        self.assertFalse(state['current']['saved'])
        self.assertFalse(any(p['active'] or p['matches'] for p in state['profiles']))

    def test_different_service_is_not_recognized(self):
        self.live(config(endpoint='https://other.example.invalid/v1'))
        state = self.manager.state()
        self.assertFalse(state['current']['saved'])
        self.assertFalse(any(p['active'] or p['matches'] for p in state['profiles']))

    def test_different_provider_id_is_not_recognized(self):
        self.live(config().replace(b'relay', b'other'))
        self.assertFalse(self.manager.state()['current']['saved'])

    def test_exact_variant_wins_over_last_selected_variant(self):
        second = self.manager.create('Second model', AUTH, config(model='second-model'))['id']
        self.assertNotEqual(second, self.pid)
        self.live(config(model='second-model'))
        self.assert_current(self.manager.state(), second, changed=False)

    def test_active_record_resolves_model_variants(self):
        self.manager.create('Second model', AUTH, config(model='second-model'))
        self.live(config(model='current-model'))
        self.assert_current(self.manager.state(), self.pid, changed=True)

    def test_ambiguous_model_variants_are_not_guessed(self):
        self.manager.create('Second model', AUTH, config(model='second-model'))
        self.live(config(model='current-model'), active=False)
        state = self.manager.state()
        self.assertFalse(state['current']['saved'])
        self.assertFalse(any(p['active'] or p['matches'] for p in state['profiles']))

    def test_unused_provider_definition_does_not_change_account(self):
        self.live(config() + b'\n[model_providers.unused]\nname = "Unused"\n')
        self.assert_current(self.manager.state(), self.pid, changed=True)

    def test_machine_settings_do_not_mark_model_config_changed(self):
        self.live(config() + b'\n[projects."/local"]\ntrust_level = "trusted"\n')
        self.assert_current(self.manager.state(), self.pid, changed=False)

    def test_oauth_rotation_with_same_account_is_recognized(self):
        saved = json.dumps({'tokens': {'account_id': 'test-account', 'access_token': 'old-test-token'}}).encode()
        fresh = json.dumps({'tokens': {'account_id': 'test-account', 'access_token': 'new-test-token'}}).encode()
        pid = self.manager.create('OAuth', saved, config())['id']
        self.live(config(model='current-model'), fresh, active=False)
        self.assert_current(self.manager.state(), pid, changed=True)

    def test_missing_config_uses_generated_default(self):
        pid = self.manager.create('Default provider', AUTH)['id']
        self.live(None, active=False)
        self.assert_current(self.manager.state(), pid, changed=False)

    def test_deduplication_still_distinguishes_models(self):
        self.assertFalse(same_config(config(), config(model='other-model')))
        self.assertEqual(self.manager.create('Duplicate', AUTH, config())['id'], self.pid)
        self.assertNotEqual(self.manager.create('Other', AUTH, config(model='other-model'))['id'], self.pid)


if __name__ == '__main__':
    unittest.main()
