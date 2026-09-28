"""Isolated official CLI browser login; passwords never reach this service."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import migrate as m


class AccountLogin:
    def __init__(self, manager):
        self.manager = manager
        self.process = None
        self.folder = None
        self.deadline = 0

    def start(self, name):
        self.manager.ensure_ready()
        m.require(isinstance(name, str) and 0 < len(name.strip()) <= 80, '请输入 1–80 字的账号名称。')
        m.require(self.folder is None, '已有登录正在进行，请先保存或取消。')
        executable = shutil.which('codex.exe' if os.name == 'nt' else 'codex')
        m.require(executable, '未找到 Codex CLI，请安装官方 Codex CLI 并加入 PATH，然后重启本服务。')
        self.name = name.strip()
        self.folder = Path(tempfile.mkdtemp(prefix='browser-login-', dir=self.manager.store))
        try:
            m.private_folder(self.folder)
            m.atomic(self.folder / 'config.toml', b'cli_auth_credentials_store = "file"\n')
            env = {k: v for k, v in os.environ.items() if k not in
                   ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_ACCESS_TOKEN')}
            env['CODEX_HOME'] = str(self.folder)
            self.process = subprocess.Popen([executable, 'login'], cwd=self.folder, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.deadline = time.monotonic() + 600
        except Exception:
            self.cancel()
            raise
        return self.status()

    def status(self):
        if self.folder is None:
            return {'state': 'idle'}
        if time.monotonic() > self.deadline:
            self.cancel()
            return {'state': 'error', 'message': '登录已超时，请重新开始。'}
        code = self.process.poll()
        if code is None:
            return {'state': 'waiting', 'message': '等待官方浏览器页面完成登录…'}
        if code != 0 or not (self.folder / 'auth.json').is_file():
            self.cancel()
            return {'state': 'error', 'message': '登录未完成。请检查网络、浏览器授权或 1455 回调端口是否被占用，然后重试。'}
        return {'state': 'ready', 'message': '授权成功，可以保存账号。'}

    def finish(self):
        m.require(self.status()['state'] == 'ready', '请先完成官方登录。')
        with self.manager.operation_lock, m.lock(self.manager.root):
            auth = (self.folder / 'auth.json').read_bytes()
            result = self.manager.create(self.name, auth)
            # Re-login must replace expired credentials even when create deduplicates.
            m.atomic(self.manager.profile_dir(result['id']) / 'auth.json', auth)
        self.cancel()
        return result

    def cancel(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process = None
        if self.folder is not None:
            shutil.rmtree(self.folder)
            self.folder = None
        return {'state': 'idle'}
