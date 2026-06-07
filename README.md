# Codex Restore Chat

Restore and keep Codex Desktop chat history visible across account, API provider, and model switching.

一个 Windows 小工具，用来修复 Codex Desktop 在切换账号、API、provider（提供方）或 model（模型）后，本地聊天记录还在但侧边栏不可见的问题。

## 功能模式

### 恢复模式

适合聊天记录已经看不见时手动修一次。

恢复模式会先自动备份，再把本机历史对齐到当前 Codex 设置，并修复这些常见隐藏条件：

- `model_provider（模型提供方）`
- `model（模型）`
- `session_index.jsonl（侧边栏索引）`
- `sessions/**/*.jsonl（会话文件）`
- `archived_sessions/**/*.jsonl（归档会话文件）`
- `archived（归档状态）`
- `has_user_event（用户消息标记）`
- Windows `\\?\` cwd（工作目录）前缀

恢复会做最多 3 轮稳定检查。如果 Codex 在恢复过程中又切换了 API 或 model（模型），工具会重新读取当前设置再补一轮，减少需要手动恢复多次的情况。

### 共享模式

适合长期在 ChatGPT 登录、API 登录、多个 provider（提供方）或多个 model（模型）之间切换。

开启后，工具会创建开机启动入口，并在后台持续检查 Codex 当前 provider（提供方）和 model（模型），把本机聊天记录保持在当前可见位置。关闭共享模式会移除开机启动入口，并停止本工具记录的后台进程。

## 图形界面

打开图形界面：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\launch_ui.ps1
```

创建或更新桌面入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\launch_ui.ps1 -InstallShortcutOnly
```

桌面入口会使用 `assets/codex-history-sync.ico` 作为图标。如果图标文件缺失，会自动回退到 Windows 系统图标。

## 命令行用法

查看状态：

```powershell
py -3 .\sync_backend.py --json status
```

执行一次恢复：

```powershell
py -3 .\sync_backend.py --json sync
```

执行一次共享同步：

```powershell
py -3 .\sync_backend.py --json share-once
```

开启共享模式：

```powershell
py -3 .\sync_backend.py --json share-enable --interval 2
```

关闭共享模式：

```powershell
py -3 .\sync_backend.py --json share-disable
```

手动备份：

```powershell
py -3 .\sync_backend.py --json backup
```

恢复最新备份：

```powershell
py -3 .\sync_backend.py --json restore
```

运行测试：

```powershell
py -3 -m unittest discover -s tests -v
```

## 项目结构

- `launch_ui.ps1`: 图形界面、桌面入口、按钮交互和提示。
- `sync_backend.py`: 恢复、共享、备份、还原和状态检查逻辑。
- `assets/`: 桌面图标资源。
- `docs/project-structure.md`: 项目整理说明。
- `tests/`: 后端测试。

## 安全说明

- 每次恢复和有变更的共享同步都会先备份 `state_5.sqlite`、`session_index.jsonl` 和会话文件首行元数据。
- 备份默认在 `%USERPROFILE%\.codex\history_sync_backups`。
- 共享模式日志默认在 `%USERPROFILE%\.codex\history_share_mode\share_mode.log`。
- 这个工具只能修复本机仍然存在的本地历史，不能恢复已经删除或另一台电脑上的聊天记录。
