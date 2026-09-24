"""Cross-platform file locks and process discovery without command-line secrets."""
from contextlib import contextmanager
import os
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
    override = os.environ.get('CODEX_DESKTOP_EXECUTABLE')
    if override and os.path.realpath(executable) == os.path.realpath(override):
        return True
    return '/Codex.app/Contents/' in executable or '/OpenAI.Codex/' in executable


def is_codex(executable):
    return is_desktop(executable) or Path(executable).name.lower() in ('codex', 'codex-tui', 'codex-exec-server')
