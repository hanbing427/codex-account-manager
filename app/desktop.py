"""Desktop lifecycle and CLI launch fallbacks for Windows, macOS and Linux."""
import json
import base64
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time

import migrate as m
import portable

def powershell(command):
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                          capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)


def close_desktop(cancel):
    if os.name != 'nt':
        return close_posix(cancel)
    snapshot = portable.processes()
    selected = {p['pid']: p for p in snapshot if portable.is_desktop(p.get('executable'))}
    # Capture descendants before closing the window: a detached child may
    # otherwise survive after its original parent has already disappeared.
    while True:
        children = {p['pid']: p for p in snapshot if p['parent'] in selected
                    and int(p.get('created') or 0) >= int(selected[p['parent']].get('created') or 0)}
        if children.keys() <= selected.keys():
            break
        selected.update(children)
    targets = [p for p in selected.values() if p.get('executable') and p['pid'] != os.getpid()]
    close_windows_targets(targets, force=False)
    if cancel.wait(5):
        return False
    # A tray/background desktop may ignore WM_CLOSE. Terminate only its own tree.
    close_windows_targets(targets, force=True)
    deadline = time.monotonic() + 60
    while m.busy():
        if time.monotonic() >= deadline:
            raise RuntimeError(portable.blocker_message())
        if cancel.wait(1):
            return False
    return True


def close_windows_targets(targets, force):
    if not targets:
        return
    encoded = base64.b64encode(json.dumps(targets).encode('utf-8')).decode('ascii')
    action = "& taskkill.exe /PID $record.pid /T /F 2>$null | Out-Null" if force else "$p = Get-Process -Id $record.pid -ErrorAction SilentlyContinue; if ($p -and $p.MainWindowHandle -ne 0) { [void]$p.CloseMainWindow() }"
    # PID, executable and creation time must still match; a reused PID is never closed.
    command = ("$targets = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + encoded + "')) | ConvertFrom-Json; "
        "foreach ($record in $targets) { $live = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $record.pid); "
        "if ($live -and $live.ExecutablePath -eq $record.executable -and $live.CreationDate.ToUniversalTime().Ticks.ToString() -eq $record.created) { " + action + " } }")
    powershell(command)


def resolve_posix():
    override = os.environ.get('CODEX_DESKTOP_EXECUTABLE')
    if override:
        path = Path(override).expanduser().resolve()
        m.require(path.is_file() and os.access(path, os.X_OK), 'CODEX_DESKTOP_EXECUTABLE must be an executable file.')
        return [str(path)]
    if sys.platform == 'darwin':
        for path in (Path('/Applications/Codex.app'), Path.home() / 'Applications/Codex.app'):
            if path.is_dir():
                return ['open', '-a', str(path)]
    cli = shutil.which('codex')
    m.require(cli, '未找到 Codex。请安装 Codex CLI 并加入 PATH，或设置 CODEX_DESKTOP_EXECUTABLE。')
    if sys.platform == 'darwin':
        command = 'exec ' + shlex.quote(cli)
        return ['osascript', '-e', 'tell application "Terminal" to do script ' + json.dumps(command, ensure_ascii=False)]
    for name, flag in (('x-terminal-emulator', '-e'), ('gnome-terminal', '--'), ('konsole', '-e'), ('xfce4-terminal', '-x'), ('xterm', '-e')):
        executable = shutil.which(name)
        if executable:
            return [executable, flag, cli]
    raise RuntimeError('未找到图形终端。请安装终端模拟器；无桌面服务器请手动运行 Codex CLI。')


def close_posix(cancel):
    snapshot = portable.processes()
    selected = {p['pid']: p['executable'] for p in snapshot if portable.is_desktop(p['executable'])}
    # Include only descendants of verified desktop processes, never independent CLIs.
    while True:
        children = {p['pid']: p['executable'] for p in snapshot if p['parent'] in selected}
        if children.keys() <= selected.keys():
            break
        selected.update(children)
    if sys.platform == 'darwin' and selected:
        try:
            subprocess.run(['osascript', '-e', 'tell application "Codex" to quit'], capture_output=True, timeout=10)
        except subprocess.TimeoutExpired:
            pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        current = {p['pid']: p['executable'] for p in portable.processes()}
        for pid, executable in selected.items():
            if current.get(pid) == executable and pid != os.getpid():
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        if selected and cancel.wait(3):
            return False
    deadline = time.monotonic() + 60
    while m.busy():
        if time.monotonic() >= deadline:
            raise RuntimeError(portable.blocker_message())
        if cancel.wait(1):
            return False
    return not cancel.is_set()
