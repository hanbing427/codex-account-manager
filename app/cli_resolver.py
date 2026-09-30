"""Find a runnable Codex CLI without passing npm shims through a shell."""
import json
import os
from pathlib import Path
import platform


def _file(path, executable=False):
    try:
        return path.is_file() and os.access(path, os.R_OK) and (
            not executable or os.access(path, os.X_OK))
    except (OSError, ValueError):
        return False


def _directories(value):
    return [Path(part.strip('"')) for part in value.split(os.pathsep) if part.strip('"')]


def _npm_command(shim, env, windows):
    # Never execute .cmd/.ps1 content: only recognize the official npm package.
    package = shim.parent / 'node_modules' / '@openai' / 'codex'
    if shim.name == 'codex.js':
        package = shim.parent.parent
    try:
        if json.loads((package / 'package.json').read_text(encoding='utf-8'))['name'] != '@openai/codex':
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    machine = platform.machine().lower()
    arch = 'arm64' if machine in ('arm64', 'aarch64') else 'x64'
    triple = ('aarch64' if arch == 'arm64' else 'x86_64') + '-pc-windows-msvc'
    if windows:
        optional = 'codex-win32-' + arch
        for vendor in (package / 'vendor',
                       package.parent / optional / 'vendor',
                       package / 'node_modules' / '@openai' / optional / 'vendor'):
            binary = vendor / triple / 'codex' / 'codex.exe'
            if _file(binary):
                return [str(binary)]
    script = package / 'bin' / 'codex.js'
    if not _file(script):
        return None
    node_name = 'node.exe' if windows else 'node'
    folders = [shim.parent, *_directories(env.get('PATH', ''))]
    if windows:
        folders += [Path(env[key]) / 'nodejs' for key in ('ProgramFiles', 'ProgramFiles(x86)') if env.get(key)]
    for folder in folders:
        node = folder / node_name
        if _file(node, executable=not windows):
            return [str(node), str(script)]
    return None


def _command(candidate, env, windows):
    # Login uses a different cwd, so PATH entries such as '.' must be frozen
    # to an absolute location before spawning the process.
    candidate = candidate.absolute()
    if not _file(candidate, executable=not windows):
        return None
    if windows:
        if candidate.suffix.lower() == '.exe':
            return [str(candidate)]
        if candidate.suffix.lower() in ('.cmd', '.ps1') or candidate.name == 'codex.js':
            return _npm_command(candidate, env, windows)
        return None
    if candidate.name == 'codex.js':
        return _npm_command(candidate, env, windows)
    return [str(candidate)]


def _recent_bundles(folder):
    try:
        candidates = list(folder.glob('*/codex.exe'))
    except OSError:
        return []
    found = []
    for candidate in candidates:
        try:
            found.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    return [path for _, path in sorted(found, key=lambda item: item[0], reverse=True)]


def resolve_cli(*, environ=None, windows=None, home=None):
    """Return a subprocess argv prefix; optional inputs allow isolated checks."""
    env = os.environ if environ is None else environ
    windows = os.name == 'nt' if windows is None else windows
    home = Path.home() if home is None else Path(home)
    override = env.get('CODEX_CLI_EXECUTABLE', '').strip().strip('"')
    if override:
        candidate = Path(os.path.expandvars(override)).expanduser()
        command = _command(candidate, env, windows) if candidate.is_absolute() else None
        if command:
            return command
        raise RuntimeError('CODEX_CLI_EXECUTABLE 指向的 Codex CLI 无法运行。请填写现有可执行文件的完整路径；'
                           'npm 的 codex.cmd 需要完整安装 @openai/codex 和 Node.js。不要附加命令参数。')

    names = ('codex.exe', 'codex.cmd', 'codex.ps1') if windows else ('codex',)
    candidates = [folder / name for folder in _directories(env.get('PATH', '')) for name in names]
    if windows:
        appdata = Path(env.get('APPDATA') or home / 'AppData' / 'Roaming')
        local = Path(env.get('LOCALAPPDATA') or home / 'AppData' / 'Local')
        candidates += [appdata / 'npm' / name for name in names]
        candidates += [local / 'Programs' / 'OpenAI' / 'Codex' / 'bin' / 'codex.exe']
        for folder in (local / 'OpenAI' / 'Codex' / 'bin',
                       local / 'Programs' / 'OpenAI' / 'Codex' / 'bin'):
            candidates += _recent_bundles(folder)
        for key in ('ProgramFiles', 'ProgramFiles(x86)'):
            if env.get(key):
                candidates += [Path(env[key]) / 'nodejs' / name for name in names]
    else:
        candidates += [folder / 'codex' for folder in
                       (home / '.local' / 'bin', Path('/opt/homebrew/bin'), Path('/usr/local/bin'))]
        candidates += [folder / 'Codex.app' / 'Contents' / 'Resources' / 'codex' for folder in
                       (Path('/Applications'), home / 'Applications')]
    seen = set()
    for candidate in candidates:
        key = str(candidate).casefold() if windows else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        command = _command(candidate, env, windows)
        if command:
            return command
    raise RuntimeError('未找到可运行的 Codex CLI。请按 README 的“ChatGPT 登录”说明安装官方 Codex CLI，'
                       '确认 codex --version 可用，然后重启本服务。自定义安装路径可设置 CODEX_CLI_EXECUTABLE。')
