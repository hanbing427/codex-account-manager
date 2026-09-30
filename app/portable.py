"""Cross-platform file locks and process discovery without command-line secrets."""
from contextlib import contextmanager
import os
import json
import re
from pathlib import Path
import subprocess
import sys


@contextmanager
def file_lock(path):
    with Path(path).open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('Another instance is operating on this Codex home.') from None
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('Another instance is operating on this Codex home.') from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def processes():
    if os.name == 'nt':
        command = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; @(Get-CimInstance Win32_Process | ForEach-Object { [PSCustomObject]@{pid=$_.ProcessId;parent=$_.ParentProcessId;name=$_.Name;executable=$_.ExecutablePath;created=$(if ($_.CreationDate) {$_.CreationDate.ToUniversalTime().Ticks.ToString()} else {'0'})} }) | ConvertTo-Json -Compress"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
            capture_output=True, encoding='utf-8-sig', check=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW)
        data = json.loads(result.stdout) or []
        return [data] if isinstance(data, dict) else data
    result = subprocess.run(['ps', '-axo', 'pid=,ppid=,comm='], capture_output=True, text=True, check=True)
    records = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid, parent = int(parts[0]), int(parts[1])
        executable = parts[2]
        if sys.platform.startswith('linux'):
            try:
                executable = os.readlink(f'/proc/{pid}/exe')
            except OSError:
                pass
        records.append({'pid': pid, 'parent': parent, 'executable': executable})
    return records


def is_desktop(executable):
    executable = executable or ''
    override = os.environ.get('CODEX_DESKTOP_EXECUTABLE')
    if override and os.path.realpath(executable) == os.path.realpath(override):
        return True
    path = executable.replace('\\', '/').lower()
    if re.search(r'/windowsapps/openai\.codex_[^/]+/app/(?:codex|chatgpt)\.exe$', path):
        return True
    if re.search(r'/(?:openai/codex|programs/codex)/(?:app/)?(?:codex|chatgpt)\.exe$', path):
        return True
    return '/codex.app/contents/' in path or '/openai.codex/' in path


def is_codex(executable):
    return is_desktop(executable) or (executable or '').replace('\\', '/').rsplit('/', 1)[-1].lower() in ('codex', 'codex.exe', 'codex-tui', 'codex-exec-server')


def blockers():
    snapshot = processes()
    by_id = {p['pid']: p for p in snapshot}
    result = []
    for p in snapshot:
        if not is_codex(p.get('executable') or p.get('name', '')):
            continue
        parent = by_id.get(p['parent'], {})
        result.append({**p, 'parent_name': parent.get('name') or (parent.get('executable') or '').replace('\\', '/').rsplit('/', 1)[-1]})
    return result


def blocker_message():
    entries = blockers()
    details = [f"PID {p['pid']} · {p.get('executable') or p.get('name') or 'Codex'} · 父进程 {p['parent']} {p['parent_name']}" for p in entries[:8]]
    if len(entries) > 8:
        details.append(f'另有 {len(entries) - 8} 个进程。')
    return '仍检测到 Codex 进程，配置未修改。关闭窗口可能仍有托盘或 IDE 后台进程；请先保存工作，再按下列 PID 核对并退出：\n' + '\n'.join(details)
