# Codex 账号配置网页

## 启动

Windows 点击桌面快捷方式或 `Start-Web.vbs`，macOS/Linux 使用 `start-web.sh` 或安装的桌面入口。首次运行自动后台启动，后续点击复用后台服务并打开网页；关闭浏览器或启动窗口不影响定时维护。默认端口 8765，占用时顺延。跨平台安装、移动目录、停止服务、日志位置详见 [README.md](README.md)。

原来的 `Start.cmd` 和迁移命令继续可用。网页复用 `migrate.py` 的会话迁移、完整性校验和回退逻辑。

## 使用

1. 在“本机配置文件”导入 `.codex` 中已有的 `auth_账号.json` 和 `config_账号.toml` 配对文件；也支持 `auth-账号.json`、`auth.账号.json`。没有对应 config 时作为仅 auth 的账号保存。
2. 或点击“添加账号”，输入名称，选择 `auth.json` 及可选的 `config.toml`。可以先“保存当前配置”，为正在使用的账号命名。
3. 点击账号的“切换到此账号”，查看目标 provider 和旧会话清单。默认迁移所有 provider 不同的未归档会话，可以包含已归档会话。
4. 点击“切换并重启 Codex”后，工具自动关闭 Codex 桌面端，等待进程退出后备份、切换和迁移。正在执行的 Codex 任务会被中断，请先保存工作。若正常关闭未退出，5 秒后仅结束 Codex 桌面端及其子进程；不会结束独立 CLI / IDE 进程，60 秒后仍有此类进程会停止并提示。
5. 操作成功后自动重新启动 Codex，保留任务结果供重启后查看。关闭或等待阶段可取消；写入阶段不可取消。若启动失败，配置保持已切换状态，页面提示手动启动。

## 仅提供 auth.json

仅 auth 的账号按官方 OpenAI 账号处理。切换时，工具自动生成最小 `config.toml`：

```toml
model_provider = "openai"
model = "gpt-6"
```

不会沿用前一个账号的第三方 URL、provider 或模型。当前生成配置使用 gpt-6；如果账号不支持，请修改为可用模型。通过网页“启动 Codex”时，如果配置文件仍不存在，也会在启动前补齐上述配置。这里由本工具保证文件生成，不依赖某一版本 Codex 是否自行创建文件，也不拦截其他快捷方式的启动。

第三方 API Key 本身不包含服务地址；第三方账号必须同时提供匹配的 `config.toml`。现有完整 config 按原始字节保存与切换，不重写其他设置。顶层 `profile` 覆盖配置与原迁移脚本一样暂不支持。

官方配置位置和优先级：[OpenAI 配置文档](https://learn.chatgpt.com/docs/config-file/config-basic)。更高优先级的启动参数、系统管理配置或系统凭据库仍可能覆盖文件配置；本工具只管理 `CODEX_HOME` 中的文件。

## 文件存储与恢复

- 默认管理环境变量 `CODEX_HOME` 指定的位置，否则是当前用户的 `.codex`。可通过 `Start-Web.cmd --home "D:\CodexHome"` 指定。
- 账号副本保存在 `CODEX_HOME/account-manager/profiles/`。令牌和密钥保存在本机；账号列表不返回认证原文，但配置编辑器和账号导出会读取原始凭据。
- Windows 使用私有 ACL；macOS/Linux 使用 0700 私有目录。文件本身没有额外加密；不要公开分享账号目录或备份。
- 每次切换前备份原 `auth.json` 和 `config.toml`，记录在 `account-manager/backups/`。会话备份继续使用原脚本的 `provider-migration-backups/`。
- “备份记录”可恢复最近一次切换，且支持按相反顺序逐次恢复。会话或配置后续发生变化时会拒绝覆盖；此时重新切换目标账号即可按现状生成新的迁移。
- 正常失败会尝试恢复本次更改。如果进程被终止或系统断电，重新启动网页后会显示待恢复状态，恢复后才能继续切换。
- 活跃账号的 OAuth 令牌刷新后，下次切换会在账号 ID 一致时保存最新令牌。跨机器复制的登录可能失效；被撤销的 refresh_token 需要用户重新登录。

继续旧会话时，Codex 会把对应历史上下文发送给所选 provider。

## 定时凭证维护

“凭证维护”默认每天一次（1440 分钟），可以调整为 5–10080 分钟，也可以暂停。只维护已保存的 ChatGPT OAuth 账号；API Key 账号跳过。维护由本地网页服务执行，关闭浏览器不影响，关闭服务或关机后停止；重新运行服务会根据持久化的到期时间检查。

流程参考 [CLIProxyAPI 的 Codex OAuth 实现](https://github.com/router-for-me/CLIProxyAPI/blob/673131f57484517c3a1eae7e36c4cfa7b9bb4efc/internal/auth/codex/openai_auth.go)：检查 access_token 到期时间，在到期前 10 分钟或无法判断有效期时使用 refresh_token 换取新凭据。OAuth 端点固定为 `https://auth.openai.com/oauth/token`。轮换后立即保存 access_token、refresh_token、id_token 和刷新时间，保留其他 auth 字段。

短消息探测默认开启，调用 `https://chatgpt.com/backend-api/codex/responses`，只发送 `Reply with OK.`，不带历史、不启用工具、设置 `store=false`；会消耗少量账号额度。模型可在维护设置里填写，留空则使用各账号 config 的 model。初始探测模型取当前本机配置；若该模型不受某账号支持，请改为该账号可用的模型。探测必须收到完成事件才算成功。

真正的令牌续期由 OAuth 刷新完成，发送消息只是验证可用性，不能保证账号永久有效。探测 401 最多刷新后再试一次；刷新凭据撤销/失效后停止自动重试并提示重新登录。网络失败/限流会退避，不输出上游认证原文。

为避免与 Codex 同时消耗轮换令牌：当前运行账号只使用实时 access_token 做探测；临近过期时交由 Codex 刷新并延后维护。闲置账号正常维护。相同 refresh_token 的已保存副本和 `.codex/auth_*.json` 等源文件同步更新；运行中的当前账号不会被后台写入。写前日志 `account-manager/token-rotation.json` 用于中断恢复，不要公开此文件。

这些端点属于 Codex/ChatGPT 客户端协议，可能随上游版本变化。模型探测不通过第三方 provider 的 URL 转发 OAuth 令牌。Python 使用系统/环境代理设置。

## 界面

石墨黑主题、青绿状态指示、轻量扫描和任务图标动画。系统启用“减少动态效果”时关闭动画。移动端和桌面共用相同功能。

## 验证

`python -m unittest -v test_webui test_maintenance` 在临时目录验证真实 SQLite/JSONL 迁移、完整恢复、失败回退、令牌轮换与同步、重启顺序、等待取消、仅 auth 配置生成与中断恢复。网络和桌面关闭/启动使用测试替身，不会写入真实 `.codex`、发出真实模型请求或关闭正在使用的 Codex。

静态界面使用本地 Lucide 图标（ISC 许可），无需联网加载界面资源。启动服务后端仅使用 Python 标准库。
