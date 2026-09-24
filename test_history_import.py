from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
import zipfile

import history_import as h


class HistoryImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = self.root / 'account-manager'
        self.store.mkdir()
        self.db = self.root / 'state_5.sqlite'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('create table threads(id text primary key, rollout_path text not null, title text not null, archived integer not null default 0, model_provider text not null, cwd text not null)')
        self.tid = str(uuid.uuid4())
        self.path = self.root / 'package.zip'
        self.make_zip()

    def make_zip(self, malicious=False):
        relative = 'sessions/fixture.jsonl'
        body = json.dumps({'type': 'session_meta', 'payload': {'id': self.tid, 'cwd': '/old/machine', 'model_provider': 'old'}}) + '\n'
        body += json.dumps({'type': 'event_msg', 'payload': {'message': 'do not change /old/machine in conversation text'}}) + '\n'
        manifest = {'version': 1, 'records': [{'thread_id': self.tid, 'path': relative, 'title': 'Fixture title', 'cwd': '/old/machine'}]}
        with zipfile.ZipFile(self.path, 'w') as z:
            z.writestr('conversations.json', json.dumps(manifest))
            z.writestr(relative, body)
            if malicious:
                z.writestr('../auth.json', 'bad')

    def test_import_maps_paths_preserves_text_and_skips_duplicates(self):
        self.assertTrue(h.preview(self.root, self.path)[0]['needs_cwd'])
        result = h.merge(self.root, self.store, self.path, [self.tid], str(self.root), guard=lambda: None)
        self.assertEqual(result['count'], 1)
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute('select rollout_path,title,model_provider,cwd from threads').fetchone()
        self.assertEqual(row[1:], ('Fixture title', 'openai', str(self.root)))
        lines = Path(row[0]).read_text().splitlines()
        self.assertEqual(json.loads(lines[0])['payload']['cwd'], str(self.root))
        self.assertIn('/old/machine', lines[1])
        self.assertTrue((Path(result['backup']) / self.db.name).exists())
        again = h.merge(self.root, self.store, self.path, [self.tid], guard=lambda: None)
        self.assertEqual(again['count'], 0)
        self.assertEqual(again['skipped'], 1)

    def test_invalid_archive_and_missing_cwd_never_modify_local(self):
        with self.assertRaises(RuntimeError):
            h.merge(self.root, self.store, self.path, [self.tid], guard=lambda: None)
        self.make_zip(malicious=True)
        with self.assertRaises(RuntimeError):
            h.inspect_archive(self.path)
        self.assertFalse((self.root / 'auth.json').exists())
        self.assertFalse((self.root / 'sessions').exists())

    def test_guard_failure_rolls_back_rows_and_files(self):
        calls = 0
        def guard():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError('fixture interrupted')
        with self.assertRaises(RuntimeError):
            h.merge(self.root, self.store, self.path, [self.tid], str(self.root), guard=guard)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('select count(*) from threads').fetchone()[0], 0)
        self.assertFalse(list((self.root / 'sessions').rglob('*.jsonl')))

    def test_preflight_makes_no_changes(self):
        result = h.merge(self.root, self.store, self.path, [self.tid], str(self.root), dry_run=True)
        self.assertEqual(result['count'], 1)
        self.assertFalse((self.root / 'sessions').exists())
        self.assertFalse((self.store / 'conversation-import-backups').exists())
