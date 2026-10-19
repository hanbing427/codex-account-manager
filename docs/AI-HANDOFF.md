# Codex Account Manager：AI 交接说明

这份文档用于把项目交给另一台电脑上的 AI 继续维护。项目是一个运行在本机的 Codex 账号、配置、凭据、备份和对话迁移管理器，不是云端账号服务。

## 项目与仓库

- GitHub：<https://github.com/hanbing427/codex-account-manager>
- 分支：`main`
- 项目目录可以放在任意路径，程序不能把路径写死。
- 默认网页监听 `127.0.0.1`，端口从 `8765` 开始，冲突时自动顺延。
- 当前项目不需要第三方 Python 依赖；需要 Python 3.11 或更新版本。
- 用户的 Codex 数据默认在 `~/.codex`，也可能由 `CODEX_HOME` 或启动参数覆盖。

## 接手后的第一步

在修改前先执行以下检查，不要凭历史对话猜测当前代码状态：

```sh
git status --short
git log --oneline -8
git remote -v
python --version
```

阅读 `README.md`、`docs/WEB-README.md`、`docs/ADVANCED.md`，再检查 `app/`、`scripts/` 和三个部署入口。先启动现有版本确认网页可访问，再针对具体问题修改。

Windows 启动：

```powershell
python app/launcher.py --no-browser
```

或者双击 `Deploy.cmd`。停止服务：

```powershell
scripts/Stop-Web.cmd
```

macOS/Linux 使用 `Deploy.command` / `deploy.sh`，停止时使用 `sh scripts/start-web.sh --stop`。

## 必须保留的行为

1. **切换账号的顺序**：先停止 Codex，创建备份，移除当前生效的 `auth.json` / `config.toml`，复制目标账号文件，再应用 provider，最后重启 Codex。不要把旧账号文件留在目标位置。
2. **仅 auth 账号**：可以没有 `config.toml`。切换时自动生成官方 provider 的最小 config；当前默认模型是 `gpt-6-astra`。不要重新改成 `gpt-6` 或 `gpt-5.5`。
3. **账号命名**：`zhf` 必须保持为 `zhf`，不能重命名为 `rp`。`rp` 是另一个账号。保存前要按内容和凭据指纹去重，已存在的账号不要重复保存。
4. **当前配置识别**：页面显示当前真实生效配置；无法匹配已保存账号时显示“未保存配置”，但要自动保留临时快照，用户之后仍能命名保存。
5. **缺失会话文件**：数据库中存在记录但对应 `sessions/*.jsonl` 不存在时，切换和迁移不能整体失败。预览中显示缺失数量并跳过这些记录，同时保留其它会话迁移。
6. **路径可移植**：禁止写死 `C:\Users\11635`、项目绝对路径或某台电脑的用户名。路径必须来自 `Path.home()`、`CODEX_HOME`、命令行参数或运行时探测。
7. **ChatGPT 登录**：密码不能经过管理器后端，也不能写入日志。登录应通过官方浏览器授权和独立临时目录完成；保存账号不会自动迁移 ChatGPT 网页版历史。
8. **备份**：切换备份、配置版本和对话导入备份分开管理；切换成功后不自动删除备份。备份页面支持逐项和批量删除。
9. **敏感文件**：不要把 `.codex`、`auth.json`、账号 ZIP、备份、迁移计划、日志或运行状态提交到 GitHub。

## 主要代码位置

- `app/webui.py`：HTTP API、账号和对话迁移接口。
- `app/migrate.py`：provider 迁移计划、会话路径处理、缺失会话跳过逻辑。
- `app/desktop.py`：Codex / IDE 进程检测、关闭和重启；Windows 不要把超长数据直接拼到 PowerShell `-Command`。
- `app/launcher.py`：跨平台服务启动、停止和端口处理。
- `app/ui/app.js`：网页交互、预览提示、账号/备份/导入导出页面。
- `app/ui/`：网页样式、图标和静态资源。
- `scripts/`：启动、停止、快捷方式和兼容入口。
- `Deploy.cmd`、`Deploy.command`、`deploy.sh`：跨平台一键部署入口。

## 修改后的最小验证

根据改动范围至少执行：

```powershell
python -m py_compile app/migrate.py app/webui.py app/desktop.py app/launcher.py
node --check app/ui/app.js
```

涉及迁移逻辑时，必须测试：正常 config+auth、仅 auth、重复账号、缺失 `rollout-*.jsonl`、Windows 长命令、取消切换和失败回滚。涉及网页时启动服务，用浏览器确认页面和错误提示；不要只看 Python 进程退出码。

## Git 工作流

修改前后都检查状态：

```sh
git status --short
git diff --stat
```

提交前确认没有敏感文件。提交信息使用简短英文，例如：

```sh
git add README.md docs app scripts Deploy.cmd Deploy.command deploy.sh
git commit -m "Fix missing session migration handling"
git pull --rebase origin main
git push origin main
```

如果远程比本地新，先 `git fetch origin` 或 `git pull --rebase origin main`，不要强制 push，也不要使用 `reset --hard` 覆盖用户修改。推送后核对 `git status` 和远程 `main` 的提交号。

## 与用户沟通时的重点

- 先说明实际改动和验证结果，再说明仍未验证的系统差异。
- 不要求用户提供或粘贴 `auth.json`、密码、令牌和完整日志。
- 如果用户只说“同步 GitHub”，直接检查、提交并推送，不要重复询问仓库地址。
- 如果用户要求删除或覆盖账号，先确认目标账号名称和备份策略；`zhf` 的名称必须保留。
- 其它电脑部署优先使用仓库 ZIP 或 `git clone`，然后运行对应系统的部署入口。程序目录可以变化，账号数据仍从目标电脑自己的 Codex 目录读取。

