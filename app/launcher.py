"""Portable background launcher and desktop shortcut installer. Python 3.11+."""
from contextlib import contextmanager
import argparse
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser

import migrate as m
from portable import file_lock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / 'scripts'
_children = []


def local_request(url, path, token=None):
    parts = urllib.parse.urlsplit(url)
    m.require(parts.scheme == 'http' and parts.hostname == '127.0.0.1' and parts.port
              and not parts.username and not parts.password and parts.path in ('', '/'), 'Invalid local service address.')
    headers = {'Origin': url, 'Content-Type': 'application/json'}
    if token:
        headers['X-CSRF-Token'] = token
    req = urllib.request.Request(url.rstrip('/') + path, headers=headers, data=b'{}' if token else None)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as response:
        return json.load(response)


def running(store):
    try:
        state = m.load(store / 'runtime.json')
        health = local_request(state['url'], '/health')
        if health.get('service') == 'codex-account-manager' and health.get('instance') == state['instance']:
            return state
    except (OSError, ValueError, KeyError, RuntimeError):
        pass
    return None


def stop_service(store, state):
    try:
        local_request(state['url'], '/api/shutdown', state['csrf'])
    except OSError:
        raise RuntimeError('Service is busy or cannot stop. Wait for switching/maintenance to finish and retry.') from None
    for _ in range(50):
        if not running(store):
            for process in list(_children):
                if process.pid == state['pid']:
                    process.wait(timeout=5)
                    _children.remove(process)
            return
        time.sleep(.2)
    raise RuntimeError('Background service did not stop in time.')


@contextmanager
def launch_lock(store):
    deadline = time.monotonic() + 40
    while True:
        lock = file_lock(store / 'launcher.lock')
        try:
            lock.__enter__()
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.2)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def start(root, port=8765):
    store = root / 'account-manager'
    m.private_folder(store)
    with launch_lock(store):
        state = running(store)
        if state and Path(state.get('source', '')).resolve() != HERE:
            stop_service(store, state)
            state = None
        if state:
            return state['url']
        log = store / 'server.log'
        if log.exists() and log.stat().st_size > 2_000_000:
            os.replace(log, store / 'server.previous.log')
        executable = Path(sys.executable)
        if os.name == 'nt' and executable.with_name('pythonw.exe').exists():
            executable = executable.with_name('pythonw.exe')
        options = {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        with log.open('ab') as output:
            process = subprocess.Popen([str(executable), '-u', str(HERE / 'webui.py'), '--home', str(root), '--port', str(port), '--no-browser'],
                cwd=HERE, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                env={**os.environ, 'PYTHONUTF8': '1'}, **options)
        _children.append(process)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = running(store)
            if state:
                return state['url']
            if process.poll() is not None:
                break
            time.sleep(.2)
        raise RuntimeError(f'Could not start the service. See log: {log}')


def desktop_quote(value):
    # Freedesktop Exec quoting (different from shell quoting); %% is a literal percent.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'


def install_shortcut():
    if sys.platform == 'win32':
        subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPTS / 'install-shortcut.ps1')],
                       check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return
    desktop = Path.home() / 'Desktop'
    if sys.platform != 'darwin' and shutil.which('xdg-user-dir'):
        result = subprocess.run(['xdg-user-dir', 'DESKTOP'], capture_output=True, text=True, check=True)
        if result.stdout.strip():
            desktop = Path(result.stdout.strip())
    desktop.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'darwin':
        bundle = desktop / 'Codex Account Manager.app' / 'Contents'
        (bundle / 'MacOS').mkdir(parents=True, exist_ok=True)
        (bundle / 'Resources').mkdir(exist_ok=True)
        with (bundle / 'Info.plist').open('wb') as output:
            plistlib.dump({'CFBundleName': 'Codex Account Manager', 'CFBundleDisplayName': 'Codex Account Manager', 'CFBundleIdentifier': 'local.codex.accountmanager',
                          'CFBundleExecutable': 'launch', 'CFBundleIconFile': 'account-manager.icns',
                          'CFBundlePackageType': 'APPL', 'LSUIElement': True}, output)
        entry = bundle / 'MacOS' / 'launch'
        entry.write_text('#!/bin/sh\nexec /bin/sh ' + shlex.quote(str(SCRIPTS / 'start-web.sh')) + '\n', encoding='utf-8')
        entry.chmod(0o755)
        shutil.copyfile(HERE / 'ui/account-manager.icns', bundle / 'Resources/account-manager.icns')
    else:
        content = ('[Desktop Entry]\nType=Application\nName=Codex Account Manager\n'
                   'Comment=Open the local Codex account manager\n'
                   'Exec=/bin/sh ' + desktop_quote(SCRIPTS / 'start-web.sh') + '\n'
                   'Icon=' + str(HERE / 'ui/account-manager.png') + '\nTerminal=false\nCategories=Utility;\n')
        application_dir = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'applications'
        application_dir.mkdir(parents=True, exist_ok=True)
        for path in (desktop / 'codex-account-manager.desktop', application_dir / 'codex-account-manager.desktop'):
            path.write_text(content, encoding='utf-8')
            path.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--install-shortcut', action='store_true')
    parser.add_argument('--setup', action='store_true', help='Create a desktop shortcut, start the background service and open the web UI.')
    args = parser.parse_args()
    if args.install_shortcut:
        install_shortcut()
        return
    root = args.home.expanduser().resolve()
    m.require(root.is_dir(), f'Codex home not found: {root}. Install/run Codex first, or specify --home.')
    if args.setup:
        install_shortcut()
    if args.stop:
        store = root / 'account-manager'
        if store.exists():
            with launch_lock(store):
                state = running(store)
                if state:
                    stop_service(store, state)
        return
    url = start(root, args.port)
    print(url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), 'Codex Account Manager', 0x10)
        else:
            print(str(exc), file=sys.stderr)
        sys.exit(1)
