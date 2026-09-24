import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing

import history_export as h


class HistoryExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = self.root / 'account-manager'
        self.store.mkdir()
        self.files = []
        for index, folder in enumerate(('sessions', 'archived_sessions')):
            path = self.root / folder / f'fixture-{index}.jsonl'
            path.parent.mkdir()
            path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': str(index)}}) + '\n' +
                            json.dumps({'type': 'event_msg', 'payload': {'message': f'fixture {index}'}}) + '\n', encoding='utf-8')
            self.files.append(path)
        self.db = self.root / 'state_5.sqlite'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('create table threads(id text, rollout_path text, title text, archived integer, cwd text)')
            for index, path in enumerate(self.files):
                conn.execute('insert into threads values(?,?,?,?,?)', (str(index), str(path), f'Title {index}', index, '/fixture/project'))
            conn.commit()
        (self.root / 'auth.json').write_text('private fixture credentials')
        (self.root / 'config.toml').write_text('private fixture configuration')
        (self.root / 'history.jsonl').write_text('global fixture history\n')

    def test_all_includes_archived_and_database_without_auth(self):
        catalog = h.catalog(self.root)
        self.assertEqual(len(catalog['records']), 2)
        self.assertEqual(sum(r['archived'] for r in catalog['records']), 1)
        with h.archive_history(self.root, self.store) as (path, manifest):
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn('state_5.sqlite', archive.namelist())
                self.assertIn('history.jsonl', archive.namelist())
                self.assertNotIn('auth.json', archive.namelist())
                self.assertNotIn('config.toml', archive.namelist())
                for original in self.files:
                    self.assertEqual(archive.read(original.relative_to(self.root).as_posix()), original.read_bytes())
                self.assertEqual(manifest['mode'], 'all')
                snapshot = self.root / 'test-snapshot.sqlite'
                snapshot.write_bytes(archive.read('state_5.sqlite'))
                with closing(sqlite3.connect(snapshot)) as conn:
                    self.assertEqual(conn.execute('select count(*) from threads').fetchone()[0], 2)
        self.assertFalse(path.exists())

    def test_selected_does_not_include_global_history_or_other_conversations(self):
        selected = next(r for r in h.catalog(self.root)['records'] if r['archived'])
        with h.archive_history(self.root, self.store, [selected['id']]) as (path, manifest):
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), {selected['path'], 'conversations.json', 'migration.json', 'README.txt'})
                self.assertEqual(len(manifest['records']), 1)
                self.assertEqual(manifest['mode'], 'selected')
                self.assertEqual(archive.read(selected['path']), self.files[1].read_bytes())

    def test_duplicate_prefers_database_rollout_even_when_copy_is_newer(self):
        duplicate = self.files[0].with_name('newer-copy.jsonl')
        duplicate.write_bytes(self.files[0].read_bytes())
        os.utime(duplicate, (2000000000, 2000000000))
        catalog = h.catalog(self.root)
        self.assertEqual(len(catalog['records']), 2)
        self.assertTrue(catalog['warnings'])
        record = next(r for r in catalog['records'] if r['thread_id'] == '0')
        self.assertEqual(record['path'], 'sessions/fixture-0.jsonl')
        with h.archive_history(self.root, self.store) as (path, manifest):
            self.assertEqual(len(manifest['records']), 2)
            with zipfile.ZipFile(path) as archive:
                self.assertNotIn('sessions/newer-copy.jsonl', archive.namelist())
        self.assertTrue(duplicate.exists())

    def test_orphan_duplicates_choose_latest_without_deleting_originals(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("delete from threads where id='0'")
            conn.commit()
        duplicate = self.files[0].with_name('newer-copy.jsonl')
        duplicate.write_bytes(self.files[0].read_bytes())
        os.utime(duplicate, (2000000000, 2000000000))
        record = next(r for r in h.catalog(self.root)['records'] if r['thread_id'] == '0')
        self.assertEqual(record['path'], 'sessions/newer-copy.jsonl')
        self.assertTrue(self.files[0].exists())

    def test_stale_selection_rejected_and_missing_files_reported(self):
        self.files[0].unlink()
        self.assertTrue(h.catalog(self.root)['warnings'])
        with self.assertRaises(RuntimeError):
            with h.archive_history(self.root, self.store, ['missing']):
                pass
        self.assertFalse(list(self.store.glob('history-export-*')))

    def test_incomplete_tail_excluded_but_complete_record_without_newline_preserved(self):
        original = self.files[0].read_bytes()
        self.files[0].write_bytes(original + b'{"unfinished":')
        self.files[1].write_bytes(self.files[1].read_bytes().rstrip(b'\n'))
        with h.archive_history(self.root, self.store) as (path, manifest):
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.read('sessions/fixture-0.jsonl'), original)
                self.assertEqual(archive.read('archived_sessions/fixture-1.jsonl'), self.files[1].read_bytes())
                self.assertTrue(manifest['warnings'])
        self.assertTrue(self.files[0].read_bytes().endswith(b'{"unfinished":'))


if __name__ == '__main__':
    unittest.main()
