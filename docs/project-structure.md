# Project Structure（项目结构）

这个项目保持“小工具”结构：入口少、文件少、桌面启动方式稳定。

## Files（文件）

- `launch_ui.ps1`: Windows 图形界面、桌面快捷方式、按钮交互和提示。
- `sync_backend.py`: Codex 历史恢复、共享模式、备份、还原和状态检查逻辑。
- `tests/test_sync_backend.py`: 后端恢复、共享、备份和可见性修复测试。
- `assets/codex-history-sync.svg`: 桌面图标源文件。
- `assets/codex-history-sync.ico`: 桌面快捷方式使用的图标文件。
- `README.md`: 用户使用说明。

## Runtime Data（运行数据）

工具不会把 Codex 数据复制进项目目录。运行时数据仍保存在用户目录：

- `%USERPROFILE%\.codex\history_sync_backups`: 自动和手动备份。
- `%USERPROFILE%\.codex\history_share_mode`: 共享模式状态和日志。

## Maintenance（维护）

- 界面入口保持为 `launch_ui.ps1`，避免桌面入口和已有使用习惯失效。
- 后端入口保持为 `sync_backend.py`，方便测试和命令行排查。
- 新增视觉资源统一放在 `assets`，说明文档统一放在 `docs`。

