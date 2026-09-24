"""OAuth rotation and minimal Codex probes, following CLIProxyAPI's protocol."""
import base64
from datetime import datetime, timezone
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = 'https://auth.openai.com/oauth/token'
RESPONSES_URL = 'https://chatgpt.com/backend-api/codex/responses'
CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'


class MaintenanceError(RuntimeError):
    def __init__(self, message, terminal=False, unauthorized=False):
        super().__init__(message)
        self.terminal = terminal
        self.unauthorized = unauthorized


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def claims(token):
    try:
        part = token.split('.')[1]
        value = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def expiry(auth):
    value = claims(auth['tokens'].get('access_token', '')).get('exp')
    return value if isinstance(value, (int, float)) else 0


def oauth(auth):
    return not auth.get('OPENAI_API_KEY') and isinstance(auth.get('tokens'), dict)


def open_request(request, refresh=False):
    try:
        return urllib.request.build_opener(NoRedirect()).open(request, timeout=30)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        # Never expose an upstream response body: it may contain credentials.
        if refresh and status in (400, 401):
            raise MaintenanceError('刷新凭据已失效或被撤销，请重新登录并导入 auth.json。', terminal=True) from None
        if status == 429:
            raise MaintenanceError('服务限流或账号额度不足，已延后重试。') from None
        if status == 401:
            raise MaintenanceError('访问令牌已失效。', unauthorized=True) from None
        raise MaintenanceError(f'上游请求失败（HTTP {status}），请检查网络或账号状态。') from None
    except (OSError, urllib.error.URLError):
        raise MaintenanceError('连接超时或网络不可达，请检查网络及系统代理。') from None


def rotate(auth):
    token = auth['tokens'].get('refresh_token')
    if not isinstance(token, str) or not token:
        raise MaintenanceError('缺少 refresh_token，请重新登录并导入完整 auth.json。', terminal=True)
    body = urllib.parse.urlencode({'client_id': CLIENT_ID, 'grant_type': 'refresh_token',
                                  'refresh_token': token, 'scope': 'openid profile email'}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
    try:
        with open_request(req, refresh=True) as response:
            data = json.loads(response.read(1_000_001))
        if not isinstance(data, dict) or not isinstance(data.get('access_token'), str) or not data['access_token']:
            raise ValueError('missing token')
        updated = json.loads(json.dumps(auth))
        for key in ('access_token', 'refresh_token', 'id_token'):
            if isinstance(data.get(key), str) and data[key]:
                updated['tokens'][key] = data[key]
        account = claims(updated['tokens'].get('id_token', '')).get('https://api.openai.com/auth', {})
        if not updated['tokens'].get('account_id') and isinstance(account, dict):
            if account.get('chatgpt_account_id'):
                updated['tokens']['account_id'] = account['chatgpt_account_id']
        updated['last_refresh'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        return updated
    except MaintenanceError:
        raise
    except (OSError, ValueError, TypeError):
        raise MaintenanceError('刷新响应不完整；请检查登录状态。') from None


def probe(auth, model):
    if not isinstance(model, str) or not model.strip():
        raise MaintenanceError('请在凭证维护设置中填写探测模型，或在账号 config 中指定 model。')
    headers = {'Authorization': 'Bearer ' + auth['tokens']['access_token'],
               'Content-Type': 'application/json', 'Accept': 'text/event-stream',
               'Originator': 'codex_cli_rs', 'User-Agent': 'CodexAccountManager/1.1'}
    if auth['tokens'].get('account_id'):
        headers['Chatgpt-Account-Id'] = auth['tokens']['account_id']
    body = {'model': model.strip(), 'instructions': 'Reply with OK only.',
            'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Reply with OK.'}]}],
            'stream': True, 'store': False, 'tools': []}
    req = urllib.request.Request(RESPONSES_URL, data=json.dumps(body).encode(), headers=headers)
    try:
        with open_request(req) as response:
            deadline, total, data_lines = time.monotonic() + 60, 0, []
            while time.monotonic() < deadline:
                line = response.readline(262145)
                total += len(line)
                if not line or total > 2_000_000 or len(line) > 262144:
                    break
                if line.startswith(b'data:'):
                    data_lines.append(line[5:].strip())
                elif not line.strip() and data_lines:
                    raw = b'\n'.join(data_lines)
                    data_lines = []
                    if raw == b'[DONE]':
                        break
                    event = json.loads(raw)
                    if event.get('type') == 'response.completed':
                        if event.get('response', {}).get('status', 'completed') == 'completed':
                            return
                    if event.get('type') in ('error', 'response.failed', 'response.incomplete'):
                        raise MaintenanceError('模型探测未完成，请检查模型权限或账号额度。')
        raise MaintenanceError('探测连接未返回完成事件，不能确认成功。')
    except MaintenanceError:
        raise
    except (OSError, ValueError, TypeError):
        raise MaintenanceError('探测连接中断或响应格式异常。') from None
