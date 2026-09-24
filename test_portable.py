import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import desktop
import launcher
import portable


class PortableTests(unittest.TestCase):
    def test_posix_flock_branch(self):
        fake = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=Mock())
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(portable, 'os', SimpleNamespace(name='posix')), patch.dict(sys.modules, {'fcntl': fake}):
                with portable.file_lock(Path(temp) / 'lock'):
                    self.assertEqual(fake.flock.call_args.args[1], 6)
            self.assertEqual(fake.flock.call_args.args[1], 8)

    def test_native_lock_excludes_another_process(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'lock'
            with portable.file_lock(path):
                code = 'from portable import file_lock; import sys\ntry:\n with file_lock(sys.argv[1]): pass\nexcept RuntimeError:\n sys.exit(7)'
                result = subprocess.run([sys.executable, '-c', code, str(path)], cwd=launcher.HERE, capture_output=True)
                self.assertEqual(result.returncode, 7)
            with portable.file_lock(path):
                pass

    def test_local_runtime_address_cannot_redirect_to_remote_host(self):
        for url in ('https://example.com', 'http://localhost:8765', 'http://127.0.0.1:8765/other', 'http://user@127.0.0.1:8765'):
            with self.assertRaises(RuntimeError):
                launcher.local_request(url, '/health')

    def test_unrelated_port_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp)
            (store / 'runtime.json').write_text(json.dumps({'url': 'http://127.0.0.1:8765', 'instance': 'correct'}))
            with patch.object(launcher, 'local_request', return_value={'service': 'codex-account-manager', 'instance': 'wrong'}):
                self.assertIsNone(launcher.running(store))

    def test_mac_desktop_resolution(self):
        with patch.object(desktop.sys, 'platform', 'darwin'), patch.dict(os.environ, {'CODEX_DESKTOP_EXECUTABLE': ''}), patch.object(Path, 'is_dir', return_value=True):
            result = desktop.resolve_posix()
        self.assertEqual(result[:2], ['open', '-a'])
        self.assertTrue(result[2].endswith('Codex.app'))

    def test_linux_cli_terminal_resolution(self):
        found = {'codex': '/usr/bin/codex', 'gnome-terminal': '/usr/bin/gnome-terminal'}
        with patch.object(desktop.sys, 'platform', 'linux'), patch.dict(os.environ, {'CODEX_DESKTOP_EXECUTABLE': ''}), patch.object(shutil, 'which', side_effect=lambda x: found.get(x)):
            self.assertEqual(desktop.resolve_posix(), ['/usr/bin/gnome-terminal', '--', '/usr/bin/codex'])

    def test_posix_close_only_signals_verified_desktop_tree(self):
        processes = [{'pid': 100, 'parent': 1, 'executable': '/Applications/Codex.app/Contents/MacOS/Codex'},
                     {'pid': 101, 'parent': 100, 'executable': '/usr/local/bin/codex'},
                     {'pid': 102, 'parent': 1, 'executable': '/usr/local/bin/codex'}]
        cancel = threading.Event()
        with patch.object(portable, 'processes', return_value=processes), patch.object(desktop.sys, 'platform', 'linux'), patch.object(desktop.signal, 'SIGKILL', 9, create=True), patch.object(desktop.os, 'kill') as kill, patch.object(desktop.m, 'busy', return_value=False), patch.object(cancel, 'wait', return_value=False):
            self.assertTrue(desktop.close_posix(cancel))
        self.assertEqual({call.args[0] for call in kill.call_args_list}, {100, 101})

    def test_mac_shortcut_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(launcher.sys, 'platform', 'darwin'), patch.object(Path, 'home', return_value=Path(temp)):
                launcher.install_shortcut()
            contents = Path(temp) / 'Desktop/Codex Account Manager.app/Contents'
            with (contents / 'Info.plist').open('rb') as stream:
                info = plistlib.load(stream)
            self.assertEqual(info['CFBundleExecutable'], 'launch')
            self.assertTrue((contents / 'Resources/account-manager.icns').exists())
            self.assertIn('start-web.sh', (contents / 'MacOS/launch').read_text())

    def test_linux_shortcuts(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(launcher.sys, 'platform', 'linux'), patch.object(Path, 'home', return_value=Path(temp)), patch.object(shutil, 'which', return_value=None), patch.dict(os.environ, {'XDG_DATA_HOME': str(Path(temp) / 'data')}):
                launcher.install_shortcut()
            entry = (Path(temp) / 'Desktop/codex-account-manager.desktop').read_text(encoding='utf-8')
            self.assertIn('Terminal=false', entry)
            self.assertIn('Exec=/bin/sh "', entry)
            self.assertTrue((Path(temp) / 'data/applications/codex-account-manager.desktop').exists())

    def test_background_start_reuse_stop_and_moved_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / '账号 配置 & space'
            root.mkdir()
            store = root / 'account-manager'
            copied = base / '工具 moved 空格'
            copied.mkdir()
            for file in launcher.HERE.glob('*.py'):
                shutil.copyfile(file, copied / file.name)
            shutil.copytree(launcher.HERE / 'ui', copied / 'ui')
            try:
                url = launcher.start(root, port=0)
                first = launcher.running(store)
                self.assertIsNotNone(first)
                self.assertEqual(launcher.start(root, port=0), url)
                self.assertEqual(launcher.running(store)['pid'], first['pid'])
                result = subprocess.run([sys.executable, str(copied / 'launcher.py'), '--home', str(root), '--port', '0', '--no-browser'],
                                        capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr)
                moved = launcher.running(store)
                self.assertNotEqual(moved['pid'], first['pid'])
                self.assertEqual(Path(moved['source']), copied.resolve())
                launcher.stop_service(store, moved)
                self.assertIsNone(launcher.running(store))
            finally:
                live = launcher.running(store)
                if live:
                    launcher.stop_service(store, live)
                # Give the native process a moment to release log/lock file handles.
                import time
                time.sleep(.5)


if __name__ == '__main__':
    unittest.main()
