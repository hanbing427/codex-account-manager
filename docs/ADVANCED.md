> 本文所有命令均在项目根目录执行；首次使用请先看 [快速开始](../README.md)。

# Codex 对话 Provider 迁移工具

## 网页版：后台运行与跨平台迁移

支持 Windows、macOS、Linux，Python 3.11+，无需 Node.js 或第三方 Python 包。将整个工具文件夹放到任意有读取权限的路径，保留目录结构即可运行，路径可以包含空格或中文。账号数据保存在当前用户的 `~/.codex/account-manager/`，不会写入工具所在目录。

### Windows

1. 双击 `scripts/Install-Shortcut.cmd`，桌面会生成带图标的 **Codex Account Manager** 快捷方式。
2. 点击桌面快捷方式：自动在后台启动服务并打开网页，没有常驻终端窗口。也可以直接双击 `scripts/Start-Web.vbs`。
3. 以后再点击，会复用已有后台服务，不重复启动。关闭浏览器不影响定时维护。
4. 需要结束后台服务时，双击 `scripts/Stop-Web.cmd`。正在切换账号或维护令牌时会拒绝退出，请稍后重试。

启动器会寻找本机 Python 或 Codex 附带的 Python。找不到时请安装 Python 3.11+ 并加入 PATH。`scripts/Start-Web.cmd` 也可启动，短暂窗口退出后服务继续运行。

### macOS

先安装 Python 3.11+，例如通过 python.org 安装包或 Homebrew。打开终端，在工具目录运行：

```sh
chmod +x scripts/Start-Web.command scripts/Install-Shortcut.command scripts/start-web.sh
./scripts/Install-Shortcut.command
```

桌面将生成 **Codex Account Manager.app**，双击即可启动后台服务并打开网页。也可以运行 `sh scripts/start-web.sh` 或双击 `scripts/Start-Web.command`。如果系统提示未受信任的下载文件，请检查来源后按 macOS 的“右键 → 打开”流程处理；不会自动绕过系统安全设置。

默认查找 `/Applications/Codex.app`、`~/Applications/Codex.app`。只有 Codex CLI 时，在 Terminal 中启动 CLI。停止后台服务：`sh scripts/start-web.sh --stop`。

### Linux

安装 Python 3.11+，在工具目录运行：

```sh
sh scripts/start-web.sh --install-shortcut
sh scripts/start-web.sh
```

会在应用菜单和桌面生成带图标的启动项。部分桌面环境需要右键 `.desktop` 文件选择“允许启动/信任”。Linux 的图形入口行为因桌面环境而异；命令行入口始终为 `sh scripts/start-web.sh`。

默认通过已安装的终端模拟器打开 PATH 中的 `codex` CLI。支持 x-terminal-emulator、GNOME Terminal、Konsole、XFCE Terminal、xterm。如果使用其他 Codex 桌面构建，可将环境变量 `CODEX_DESKTOP_EXECUTABLE` 设置为该桌面程序可执行文件的绝对路径（不是 CLI），并从带此环境变量的启动入口运行。停止后台服务：`sh scripts/start-web.sh --stop`。

### 迁移、路径及运行边界

- 只需复制完整工具目录；不用修改源码中的路径。不要同时复制旧的 `migration-plan.json`、账号数据或私人备份给其他人。发布 ZIP 只包含程序、图标、启动入口及文档。
- **移动工具目录后，重新运行快捷方式安装入口**，更新快捷方式所指向的位置。启动脚本本身使用相对路径；操作系统快捷方式必须指向实际安装位置。新启动器遇到来自旧目录的服务时，会在其空闲时停止旧服务并启动新服务。
- 运行账户必须能访问自己的 `.codex`。可用 `--home "/path/to/codex-home"` 指定其他目录；GUI 快捷方式使用默认 `CODEX_HOME` 环境变量或 `~/.codex`。
- 后台运行期间，默认每天执行一次已保存 OAuth 账号的维护，可在网页调整。**关闭浏览器不影响；注销、关机或主动停止后台服务后会停止。没有擅自设置开机/登录自启**，下次点击快捷方式即可恢复，到期任务会重新检查。
- 日志：`~/.codex/account-manager/server.log`。运行状态：同目录 `runtime.json`（含本机服务控制令牌，不要公开）。端口默认 8765，冲突时自动选择后续端口，快捷方式始终打开实际地址。
- 服务只监听 `127.0.0.1`。每台电脑运行各自的服务，管理自己的配置。不能用访问另一台机器网页的方式切换本机文件。
- Windows 使用系统文件锁及私有 ACL；macOS/Linux 使用 `flock`、0700 私有目录和原子文件写入。建议将 `.codex` 放在本地磁盘，避免网络文件系统的锁语义差异。
- 自动切换会退出已识别的 Codex 桌面进程及其子进程。**独立的 CLI/IDE 会话需要先手动退出**；工具不会为了迁移结束不相关的进程。只有 CLI 的机器在切换完成后自动打开新终端。无图形桌面的服务器无法自动打开浏览器或终端。
- 请在另一台机器重新登录，避免多台电脑共用并同时轮换同一个 refresh_token。
- 当前 Windows 已做实机启动/复用/停止及快捷方式检查；macOS/Linux 的进程与启动分支有模拟测试，本次环境没有 macOS 或 Linux 实机，仍需在目标系统验证桌面集成。

### 开发与排错

```sh
python3 app/launcher.py --no-browser       # 后台启动并打印实际 URL
python3 app/launcher.py --stop             # 安全停止后台服务
python3 app/webui.py --no-browser          # 前台运行，便于排错
```

原有账号切换、定时维护的说明见 [WEB-README.md](WEB-README.md)。以下为原迁移命令行说明。

新增网页账号管理界面：双击 **scripts/Start-Web.cmd**，可保存/切换账号配置、仅 auth 自动生成 config、同步会话 provider 和恢复备份。详见 [网页使用说明](WEB-README.md)。原命令行入口保持可用。

Windows、macOS、Linux / Python 3.11 及以上 / 无需安装第三方依赖。原 `scripts/Start.cmd` 为 Windows 入口；其他平台直接运行 `python3 app/migrate.py`。

用于切换 config 和登录方式后，把已有对话迁到新 Provider。目标 Provider 每次从实际的 `config.toml` 读取，不写死用户名、模型、旧 Provider、新 Provider、数据库版本或任务 ID。

## 最常用：双击运行

1. 先切换好你要用的 `config.toml` 及对应登录凭据。重启 Codex，用**新对话成功发送一条消息**，确认目标服务可用。
2. 双击项目根目录里的 **scripts/Start.cmd**。
3. 查看目标 Provider 和待迁移对话列表。默认选择**所有 Provider 与目标不同的未归档对话**。
4. 确认列表后输入 **MIGRATE**。不想执行时直接回车退出。
5. **完全退出 Codex 桌面端、CLI 和 IDE 中运行的 Codex**，保留这个终端窗口。工具默认最多等待 60 分钟；不要与其他迁移工具同时运行。
6. 终端显示 **SUCCESS** 后，再启动 Codex，打开旧对话发送消息验证。

新 Provider 将在你继续旧对话时收到相应的历史上下文。工具本身不发起模型请求，不自动登录，也不修改 config、auth、模型、推理强度、消息正文或归档状态。

终端使用英文状态：`WAITING` 等待退出；`SUCCESS` 成功；`FAILED` 失败。失败时保留终端中打印的备份路径。

## 可选：命令行精确选择

在项目根目录打开 PowerShell，使用下列命令。`scripts/Start.cmd` 会自动寻找 Codex 自带或本机安装的 Python。

```powershell
# 只预览，不修改对话；输出 migration-plan.json
.\scripts/Start.cmd plan

# 只迁移指定旧 Provider；目标始终读取当前 config
.\scripts/Start.cmd --from-provider openai

# 把归档对话也包括在内
.\scripts/Start.cmd --include-archived

# 只迁移指定 ID，可重复 --id
.\scripts/Start.cmd --id "实际的任务 UUID"

# 多个来源 Provider，可重复 --from-provider
.\scripts/Start.cmd --from-provider openai --from-provider old_proxy

# 指定另一个 CODEX_HOME
.\scripts/Start.cmd --home "D:\CodexHome"

# 检测到多个 state 数据库时，明确指定实际在用的数据库
.\scripts/Start.cmd plan --database "C:\Users\你的用户名\.codex\state_5.sqlite"

# 应用已经检查过的预览计划，等待退出后执行
.\scripts/Start.cmd apply --plan ".\migration-plan.json" --wait

# 等待退出的时限改为 120 分钟
.\scripts/Start.cmd --timeout-minutes 120
```

`apply` 是明确的执行命令，不再二次提问。不带 `--wait` 时，Codex 仍运行便停止。直接运行 Python 或 `scripts/start.ps1` 也支持这些参数。

## 备份和回退

每次执行都会新建独立备份目录：

```text
实际的 CODEX_HOME/provider-migration-backups/日期时间-随机编号/
```

其中包括所选 JSONL、SQLite 一致性备份、分页历史数据库备份、config 参考副本及记录文件哈希和原 Provider 的 `manifest.json`。Windows 下备份父目录限制为当前用户及 SYSTEM 访问。不会读取或复制 `auth.json`。

回退示例：

```powershell
.\scripts/Start.cmd rollback --backup "C:\Users\你的用户名\.codex\provider-migration-backups\实际备份目录" --wait
```

回退只恢复这次迁移的会话文件和数据库 Provider 字段，**不会整体覆盖当前数据库或恢复全局 config**。回退前还会保存当前状态。若迁移后这些会话有新消息、路径或归档状态变化，自动回退会拒绝执行，以免覆盖新内容。此时应重新切换到希望使用的 config，再生成一次反方向迁移计划。

写入中被断电或强制结束时，备份中的 `prepared` 状态可供同一回退命令恢复。请先完全退出 Codex，并保留备份；不要手动只复制 SQLite 主文件覆盖正在运行的数据库。

## 边界和异常处理

- 这是本地故障恢复工具，不是官方迁移接口。按当前实际表结构验证；遇到未知或不一致的结构会停止。
- `CODEX_HOME` 优先取环境变量，否则取当前用户的 `.codex`。多个候选数据库时必须指定 `--database`，不按修改时间猜测。
- 顶层未设置 `model_provider` 时，按默认 `openai` 处理。自定义 Provider 必须有对应定义。
- 若 config 顶层设置了 `profile`，工具会停止；请先把目标 Provider 明确配置到顶层并取消该选择。CLI `--profile` / `-c` 覆盖不在自动识别范围内。
- config 或所选会话在预览后变化，会要求重新生成计划，不继续使用过期快照。建议生成计划后不要再使用所选旧对话。
- `session_meta.payload.model_provider` 必须与数据库一致。`turn_context` 仅在存在明确 Provider 字段且与原值一致时修改；不存在时不新增。正文同名字符串保留。
- 本工具不修改模型名。若换服务后模型不受支持，需要在 Codex 中选择新服务支持的模型。
- 进程检测采用保守方式：其他 CODEX_HOME 的 Codex CLI 也会使工具等待。不能防止用户在写入瞬间重新启动应用，务必等 SUCCESS。
- 每个 CODEX_HOME 有一个系统文件锁，阻止本工具的多个实例同时写入；旧版一次性脚本不使用此锁，应先结束它。
- 仅 Provider 名不变、只是同名 Provider 的地址或密钥变化时，会提示没有需要迁移的会话；此时通常只需验证连接。

官方配置参考：[Codex Sample Configuration](https://learn.chatgpt.com/docs/config-file/config-sample)。

## 文件说明

- `scripts/Start.cmd`：双击入口，运行结束后保留窗口。
- `scripts/start.ps1`：自动寻找 Python，转发命令行参数。
- `migrate.py`：预览、迁移、校验和回退逻辑。
- `migration-plan.json`：运行预览后生成，包含任务 ID、标题和文件哈希，不要公开分享。

整个文件夹可以移动保存，无需修改脚本里的路径。
# Codex Account Manager

## 对话与项目迁移（含生成文件）

侧栏“对话迁移”支持普通与归档对话，可搜索、筛选、勾选。全选只选择筛选结果；“完整备份全部对话”不受筛选限制。导出前显示文件清单、大小、缺失和排除项，可逐行填写额外文件的完整路径补充。支持选择保存位置。

三种导出范围：

- **导出所选对话**：会话及上下文、对应的分页聊天历史、识别到的本地文件引用和文件修改产物、项目关联、对话名称、置顶和归档状态。不复制整个项目目录。
- **完整备份全部对话**：上述内容覆盖全部对话，另含原始状态库快照和会话索引。不等于复制每个项目目录。
- **导出整个项目**：先在项目下拉框选择项目，导出其全部对话和整个项目目录，包括未在对话中引用的文件。符号链接、目录联接和 Codex 内部数据不递归打包，并列出排除项。项目文件可能包含环境配置和密钥。

HTML/CSS/SVG 报告会收集可识别的本地图片、样式等依赖。无法自动收集远程资源、已经删除的文件或所有动态引用，请检查预览并手动补充遗漏。分支文件本身可能包含父对话上下文。`conversations.json` 记录会话范围，`migration.json` 保存元数据、文件映射和缺失/排除清单。

另一台电脑的导入步骤：

1. 安装相同或更新版本的 Codex 并至少启动一次，初始化本地数据库；再启动本工具。
2. 点击“对话迁移 → 导入对话”，选择本工具导出的 ZIP，预览并勾选对话。已有相同 ID 的对话默认跳过。
3. 新版包填写本机已有存放目录。程序创建独立的 `Codex-Import-...` 文件夹，保留项目文件的相对结构，并重写对话、分页聊天记录和项目关联里的路径。不会覆盖不同内容的同名文件。
4. 点击“导入所选并重启 Codex”。会中断当前任务；独立 CLI / IDE 会话需先退出。导入前备份本地数据库和相关界面状态，合并所选记录后重启 Codex。当前账号凭据保持不变，导入对话的 provider 适配当前账号。

新版包保留原始名称、时间、置顶、归档、分组和项目关联；生成文件保存在本机后可通过更新后的链接打开。跨系统迁移已验证路径映射逻辑，但没有在真实 macOS/Linux 桌面上做端到端验收。不同 Codex 版本的数据库结构可能不兼容，遇到未知必填字段或缺少历史表会停止导入，需先升级目标 Codex。不会迁移操作系统软件、运行中的浏览器标签、外部服务登录、远程网页或应用权限设置。

旧版 ZIP（只有会话记录、没有 migration.json）仍可导入对话，但不能补回未打包的文档实体或完整界面组织信息。需要完整迁移时请在源电脑重新导出新版包。在线导出各文件时点可能不同，建议停止生成后操作；本地已经丢失的文件或未保留的上下文无法恢复。

导入备份位于 `~/.codex/account-manager/conversation-import-backups/`。普通错误会回滚本次新增记录和文件；中断后可点击“恢复中断的导入并重启 Codex”，状态不一致时会保留备份并停止自动恢复。临时上传包保存在 `conversation-import-staging/`，成功导入或取消预览时删除，遗留超过一天的包在下次上传时清理。ZIP 最大 8 GB、解压内容最大 12 GB、会话正文总量最大 512 MB、迁移元数据最大 256 MB，超限请分批导入。此类导入备份与账号切换备份分开保留。

每个账号卡片底部的下载图标支持单独导出该账号，文件名包含账号名称；顶部“一键导出”仍导出全部已保存账号。两种方式均下载 ZIP，并在下载前提示登录凭据风险。

导出时可选择保存位置：支持 File System Access 的浏览器显示“另存为”对话框；其他浏览器会要求输入本机已有文件夹的完整路径，由本机服务写入 ZIP，文件名自动生成且不覆盖已有文件。取消选择不会导出。

账号列表右侧的“一键导出”可将列表中的全部已保存账号下载为 ZIP。每个账号包含原始 auth.json、可选的 config.toml，以及记录名称和颜色的 account.json；只有 auth 的账号不会补入 config。导出的是管理器保存的副本，不含历史备份、会话、未命名快照或未导入文件，不自动同步当前运行中的凭据。压缩包包含明文登录凭据，请妥善保管。导出在内存中完成，服务端不额外保存 ZIP。换电脑后解压，通过“添加账号”选择对应文件并填写名称，颜色可独立设置。

点击账号卡片图标可独立设置颜色，不修改 auth/config，也不重启 Codex。新账号自动分配不同的默认颜色；已有自定义颜色会保留。点击小齿轮或账号名称，可查看 auth.json、config.toml 的完整路径、打开所在文件夹并编辑配置。铅笔按钮用于重命名账号。

“保存副本”不会改变正在运行的 Codex；“更新并重启 Codex”会保存并应用所选账号配置，自动关闭和重启 Codex，正在执行的任务会中断。只提供 auth 的账号可将 config 编辑框留空，切换时会清除上一份配置并重新生成默认配置。外部编辑文件后请重新打开编辑窗口，以加载最新内容。

所有数据位于当前用户的 Codex 数据目录（默认 `~/.codex`，或 `CODEX_HOME` 指定的目录），与程序安装路径无关：

- `account-manager/profiles/<账号ID>/`：已保存的配置副本。
- `account-manager/backups/<时间-ID>/`：每次切换前的 auth、config 和恢复清单。
- `provider-migration-backups/`：涉及会话 provider 迁移时的关联备份。
- `account-manager/profile-revisions/<账号ID>/<时间-ID>/`：编辑保存前的旧版本。

备份不会在切换后自动删除，也没有自动清理策略。“备份记录”显示全部切换备份，支持逐条删除、勾选、全选和批量删除。确认删除后，对应切换备份及其关联的 provider 迁移备份会永久删除；当前配置、已保存账号及实际会话不受影响。待恢复备份禁止删除，切换或维护期间不可删除。编辑前的旧版本（profile-revisions）独立保留，不在此列表中删除。备份含登录凭据，请勿与便携程序一起分享。
