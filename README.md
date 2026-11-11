# Codex Sidebar Recovery

恢复 macOS 上 Codex 切换账号后缺失的本地项目入口，以及经核验的本机会话归组。

## macOS 窗口版

从 [GitHub Releases](https://github.com/jcz111/codex-sidebar-recovery/releases) 下载 macOS ZIP，解压后打开 **Codex Sidebar Sync.app**。应用包同时支持 Apple Silicon 和 Intel，需要 **macOS 13+、Python 3.10+**，并且本机已保存两个 Codex 登录账号。发布包采用临时签名，尚未公证。如果 macOS 阻止打开，可按下文从源码构建。

1. **先切号。** 在 Codex 或原有切号工具中切到要使用的账号，等待登录完成。等正在运行的任务结束后再开始同步。
2. **选同步范围。** 本地项目入口和排序会一起同步。「同步会话归组」默认开启。只想补项目时，取消勾选。
3. **点击「退出 Codex 并同步」。** 工具会等待客户端正常退出、备份并核对配置，再写入双向合并结果。完成后会重新打开 Codex。
4. **核对结果。** 窗口显示新增数量、共有项目和跳过记录。点「查看备份」可在 Finder 查看本次备份和 `exclusions.json`。在两个账号中分别检查项目入口和聊天归组。

同步更新两个账号保存的本地侧栏。日常切号直接使用已同步的结果。有新增项目、顺序或归组要同步时再运行。两边已有不同归组的会话会保留各自的设置，云端项目和远程条目继续由各账号管理。

| 窗口提示 | 接下来怎么做 |
| --- | --- |
| 正在等待 Codex 退出 | 回到 Codex 处理退出提示 |
| Codex 后台进程仍在运行 | 结束相关 Codex CLI 或终端任务，再重试 |
| 需要两个已保存的登录账号 | 分别登录一次两个账号，并让客户端保存侧栏状态 |
| 同步时检测到配置变化 | 等切号和启动完成，再运行同步 |
| 有跳过记录 | 在「查看备份」中检查 `exclusions.json`，核对冲突或缺少核验依据的条目 |

**回退上一次写入。** 点「查看备份」找到本次备份目录，完全退出 Codex 和相关后台程序，在源码目录运行：

```sh
python3 recover.py rollback --backup-dir "/path/to/sync-backup"
```

如果客户端已重写侧栏配置，脚本会因为哈希不同而停止。备份仍可用于核对差异。

**从源码构建。** 安装 Xcode Command Line Tools 后，在仓库目录运行：

```sh
macos/build.sh ./dist
```

生成的应用在 `dist/`。构建通用二进制时加上第二个参数 `universal`。也可通过下文的命令行流程使用同步功能。

## 问题现象

在同一台 Mac 上从账号 A 切换到账号 B 后，项目侧栏为空或只显示 B 的项目。原有会话仍可能出现在「最近」。切回 A 时，原有项目和分组仍在。重新添加同一目录可能生成一个新项目 ID，旧会话的分组不会随目录自动恢复。

本方案依据 **macOS ChatGPT/Codex 桌面版 26.1103.60933，build 25894** 的复现和备份结构编写。项目入口和单条会话归组曾在该版本上恢复。通用脚本的候选结果经过真实备份只读比对，应用与回退使用模拟双账号数据测试。其他桌面版本和平台未验证。相关公开报告见 [openai/codex #54155](https://github.com/openai/codex/issues/54155)。

## 恢复原理

在观察到的版本里，`state_5.sqlite` 保存本地项目 ID、目录和会话索引。`.codex-global-state.json` 顶层包含当前账号的侧栏状态，`electron-account-renderer-state-v1.inactive` 下保存其他账号的侧栏状态。同一条本机会话可以被两个账号用同一个会话 ID 引用，侧栏归组由各账号分别保存。

脚本先从已退出客户端的本机状态创建私有备份。它用 SQLite 副本核对来源项目 ID、幂等键和目录，再向当前账号追加匹配的项目入口、本机 ID 映射与可用的项目顺序。可选的会话恢复只接受来源账号已有的显式本地归组，要求会话已在目标账号中可见、创建者可核验且工作目录属于原项目。目标账号已有的项目、会话归组和明确的「无项目」设置有优先权。

`prepare` 生成候选和跳过清单。`apply` 再次确认客户端未运行，并比较原始 JSON、SQLite 和 WAL 的哈希后，原子替换侧栏 JSON。原账号保存的状态和原始会话文件均保留。脚本读取的只有侧栏 JSON、项目索引和可选的会话 ID 列表。

## 命令行单向恢复

需要 **macOS 和 Python 3.10+**。默认 Codex 状态目录为 `~/.codex`，也可以在子命令前传入 `--codex-home PATH` 指定其他目录。先登录接收项目的账号，再彻底退出 ChatGPT/Codex、正在使用该目录的 Codex CLI、VS Code 和账号切换工具。

1. 在本仓库中运行以下命令，准备私有备份和候选。目标目录应尚未存在，并位于 Git 仓库和 Codex 状态目录之外。

   ```sh
   backup_dir="$HOME/Library/Application Support/codex-sidebar-recovery-$(date +%Y%m%d-%H%M%S)"
   python3 recover.py prepare --backup-dir "$backup_dir"
   ```

   如果有多个已保存的非当前账号，用 `--source-key` 选择正确的 `inactive` 键。只在本机核对该键和对应项目。

   默认只恢复项目。如需恢复部分会话归组，退出客户端前先在目标账号核对这些会话可以打开。将对应 ID 每行一个写入 Git 仓库外的私有文件，准备时加上 `--visible-threads /path/to/private-thread-ids.txt`。备份目录内的 `exclusions.json` 逐项记录跳过原因，可能含项目及会话 ID。

2. 查看 `prepare` 输出的新增数量和跳过原因。看到 `PREPARE OK` 后，在客户端仍完全退出时运行：

   ```sh
   python3 recover.py apply --backup-dir "$backup_dir"
   ```

   看到 `APPLY OK` 再打开客户端，检查目标项目和会话归组。若看到 `STOP`，根据提示核对状态。哈希不同需要用新的备份目录重新准备。

3. 需要撤回时，先完全退出相关程序，再运行：

   ```sh
   python3 recover.py rollback --backup-dir "$backup_dir"
   ```

   回退要求当前 JSON 仍与候选字节一致。客户端重写配置后，脚本会拒绝覆盖，原始备份仍可用于核对差异。

以上是单向恢复流程。请把备份、会话 ID 列表、真实项目路径、登录数据及运行截图留在本机。提交问题时请使用模拟数据。

## 命令行双向同步

双向工具适用于状态文件里恰好保存了两个账号的情况。它一次补齐两边通过核验的本地项目、项目顺序和可选的会话归组。两边已经一致时，再运行只生成核验快照。现有归组冲突、明确的「无项目」标记、云端项目和远程条目按各账号原来的配置保留。

在 Codex 已退出时，可以用命令行预览双向合并。以下命令只生成候选。加上 `--apply` 后会以当次快照重新核对并应用。

```sh
python3 sync_sidebar.py --backup-parent "$HOME/Library/Application Support/Codex Sidebar Sync/Backups" --include-chats
```

双向会话同步依据两边已保存的显式本地归组和本机 SQLite 索引。只有创建者能核验为这两个账号之一，且工作目录属于关联项目的会话才会进入候选。候选只修改侧栏引用，实际可见性仍由 Codex 决定。同步保留两边的记录，删除不会传播。当前版本的双向写入已用模拟账号测试。

## 测试

```sh
python3 -m unittest -v
```

测试在临时目录构造模拟账号、项目和会话，覆盖关联筛选、双向合并、重复运行、哈希变化和回退。测试无需登录 Codex 或 GitHub。
