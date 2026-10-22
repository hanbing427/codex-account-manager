"""Large projects must not be rejected solely for their file count."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import conversation_bundle as bundle
import history_import


class LargeConversationBundleTests(unittest.TestCase):
    def test_collect_more_than_50000_project_files(self):
        root = Path.cwd() / 'test-codex-home'
        project = Path.cwd() / 'test-project'
        metadata = {'state': {'thread_attachments': []},
                    'history': {table: [] for table in bundle.HISTORY_TABLES}}
        projects = [{'id': 'project', 'roots': [str(project)], 'ids': []}]
        files = [f'file-{i}.txt' for i in range(50001)]
        with patch.object(bundle, 'capture', return_value=metadata), \
                patch.object(bundle, 'project_list', return_value=projects), \
                patch.object(bundle.os, 'walk', return_value=[(str(project), [], files)]), \
                patch.object(Path, 'resolve', lambda p: p), \
                patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(Path, 'is_junction', return_value=False, create=True), \
                patch.object(Path, 'is_file', return_value=True), \
                patch.object(Path, 'is_dir', return_value=True), \
                patch.object(Path, 'stat', return_value=SimpleNamespace(st_size=3 * 1024**3)):
            result = bundle.collect(root, [], project_ids=['project'])
        self.assertEqual(len(result['assets']), 50001)
        self.assertEqual(result['bytes'], 50001 * 3 * 1024**3)
        self.assertTrue(all(a['path'].startswith('files/projects/') for a in result['assets']))

    def test_inspect_zip64_with_more_than_65535_entries(self):
        tid = str(uuid.uuid4())
        session = 'sessions/rollout.jsonl'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'large.zip'
            with zipfile.ZipFile(path, 'w', allowZip64=True) as archive:
                archive.writestr('conversations.json', json.dumps({
                    'version': 1, 'records': [{'path': session, 'thread_id': tid}]}))
                archive.writestr(session, json.dumps({
                    'type': 'session_meta', 'payload': {'id': tid}}) + '\n')
                for i in range(65536):
                    archive.writestr(f'files/file-{i}.txt', b'')
            records = history_import.inspect_archive(path)
        self.assertEqual([r['id'] for r in records], [tid])

    def test_inspect_sizes_above_previous_limits(self):
        tid = str(uuid.uuid4())
        session = 'sessions/rollout.jsonl'
        asset_path = 'files/large.bin'
        migration = {
            'assets': [{'path': asset_path, 'bytes': 3 * 1024**3,
                        'source': '/project/large.bin', 'thread_ids': [tid], 'sha256': '0' * 64}],
            'metadata': {'state': {}, 'history': {}}}
        payloads = {
            'conversations.json': {'version': 2, 'records': [{'path': session, 'thread_id': tid}]},
            'migration.json': migration,
            session: {'type': 'session_meta', 'payload': {'id': tid}}}
        sizes = {'conversations.json': 17 * 1024**2, 'migration.json': 257 * 1024**2,
                 session: 13 * 1024**3, asset_path: 3 * 1024**3}
        infos = {name: zipfile.ZipInfo(name) for name in sizes}
        for name, info in infos.items():
            info.file_size = sizes[name]
        # Simulate large entries without allocating gigabytes for a boundary test.
        archive = MagicMock()
        archive.__enter__.return_value = archive
        archive.infolist.return_value = list(infos.values())
        archive.namelist.return_value = list(infos)
        archive.getinfo.side_effect = infos.__getitem__
        archive.read.side_effect = lambda info: json.dumps(payloads[
            info if isinstance(info, str) else info.filename]).encode()
        with patch.object(history_import.zipfile, 'ZipFile', return_value=archive):
            records = history_import.inspect_archive('large.zip')
        self.assertEqual(records[0]['bytes'], sizes[session])
        self.assertEqual(records[0]['files'], 1)


if __name__ == '__main__':
    unittest.main()
