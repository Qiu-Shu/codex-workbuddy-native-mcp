# Codex WorkBuddy Native MCP

Windows 上通过本机 ACP 接口，让 Codex 委派任务给已登录的 WorkBuddy，并把对话保留在 WorkBuddy 的一个专用原生会话中。版本：0.1.2。

运行方式是 stdio MCP → 本机 WorkBuddy ACP；不模拟键盘、鼠标，不调用 Computer Use。

## 已验证范围

- 2026-10-13，Windows、Python 3.12、WorkBuddy 5.8.2：在已有原生会话中完成文字回复、Read / Write / Edit 文件操作和 WebSearch / WebFetch 联网任务。
- 2026-10-16，WorkBuddy 5.8.2：通过已注册的 stdio MCP 入口复测文件和联网操作；主动回收连接器专用运行进程后，下一次新任务自动恢复，回复保存在相同原生会话中且只提交一次。56 项自动测试及解压包 MCP 启动检查通过。
- 发布包自动测试覆盖本地逻辑和上游兼容性；还没有在第二台实体电脑上验收。
- 使用 WorkBuddy 当前登录状态。包内不含账号凭据、聊天记录、会话绑定、Python 环境或个人测试文件。
- 这是基于客户端内部接口的社区适配，不是官方稳定 API；客户端升级后可能需要适配。

## 新电脑安装

需要 Windows、Python 3.11 或更新版本、Codex，以及已经安装并登录的 WorkBuddy。首次安装依赖需要联网。

1. 将整个目录解压到一个长期保留、当前用户可写的位置。注册后不要移动目录，否则 MCP 的绝对路径会失效。
2. 在 WorkBuddy 中手动创建或选择一个专用会话，发送一句普通消息并等回复结束。每台电脑必须选择它自己的会话，不能沿用其他电脑的 UUID。后续使用时保持 WorkBuddy 已启动并登录。
3. 在本目录打开 PowerShell，运行以下命令安装依赖并列出最近 30 个会话的 ID、标题和工作目录（不列出消息正文）：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
   ```

   如果 `python` 不在 PATH，可在命令末尾加 `-Python '你的 Python.exe 绝对路径'`。

4. 从列表选中专用会话，把下面的占位符替换成真实 UUID：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -SessionId '专用会话的UUID' -Register
   ```

   脚本创建本目录 `.venv` 和 `desktop_session.json`，并向 `$CODEX_HOME/config.toml`（未设置时为用户目录下 `.codex/config.toml`）追加 `mcp_servers.workbuddy`。已有配置会先备份；若同名 MCP 已存在，脚本会停止，不覆盖它。只绑定会话时省略 `-Register`。

5. 重启 Codex 或重新加载 MCP，然后让 Codex 调用 `workbuddy_status`。`connected: true` 表示发现了可连接的本机端口，且原生记录显示空闲；`connected: false, can_attempt_recovery: true` 表示端点已消失，但下一次 `workbuddy_start` 可尝试恢复。状态检查不启动进程、不获取写入权，不能代替实际任务验收。注册成功本身也不能证明连接成功。

这些命令的 ExecutionPolicy Bypass 只作用于该 PowerShell 进程，不修改系统执行策略。安装不需要管理员权限。

## 使用

向 Codex 明确任务和可操作的绝对目录，例如：

> 请调用 WorkBuddy，读取我指定工作目录里的 input.txt，把摘要写到同目录 summary.txt。仅允许读写这个目录，不运行 shell。

工具包括：

| 工具 | 用途 |
| --- | --- |
| `workbuddy_status` | 探测指定原生会话的活动端点与空闲状态 |
| `workbuddy_start` | 指定 prompt、现存绝对 cwd，开始一个任务 |
| `workbuddy_wait` | 每次最多等待 45 秒，直到返回终态 |
| `workbuddy_cancel` | 请求取消指定任务；之后仍需 wait 确认结果 |

`start` 的任务超时范围为 10–600 秒，默认 240 秒。同一专用会话串行执行；任务 ID 仅在启动它的 MCP 进程中有效。

## 边界和故障处理

- 必须先登录 WorkBuddy 并绑定已有专用会话。0.1.2 在原工作进程被回收后，通过本机 sidecar 接口启动连接器专用运行进程，重新加载相同会话 UUID 和原工作目录；不操作界面、不创建新聊天、不自动重发旧任务。它使用动态端口，由 WorkBuddy 内部通道完成登录状态初始化，桥接器不提取登录凭据。WorkBuddy 主程序和 sidecar 必须仍在运行；原工作目录和会话也必须存在。
- 恢复后的运行进程按需复用，仅监听本机回环地址；它被回收后，可在下一个新任务开始时再次恢复。会话忙碌、状态未知或旧任务结果不明时，不启动替代进程。发现多个 sidecar 时先检查全部已有运行进程，避免重复创建。
- 回复保存在原生会话记录中；桌面窗口是否立即刷新取决于 WorkBuddy 客户端，桥接器不操作界面刷新。
- 原生会话保留历史。委派期间不要在它里面手动发送消息；不同敏感项目应使用不同会话，重新绑定前等任务结束。
- 0.1.1 修复 `Persistent Session already has an external writer`：仅当首次 `session/load` 明确返回这一占用错误、且再次读取专用聊天记录确认空闲时，使用客户端的 `takeWriter` 接口重新加载一次。此操作会断开旧写入连接，包括桌面端的活动连接；不结束 WorkBuddy 进程、不操作界面。忙碌、未知状态、超时或其他错误不会触发接管或自动重发任务。检查和接管不是客户端提供的原子操作，专用聊天在委派期间不得被其他人或软件同时使用。
- 状态检查仅探测 TCP，不创建 ACP 连接。任务结束时检查连接释放响应；失败会返回 `connection_cleanup_error`，此时先确认原任务结果，不能盲目重发。客户端的独立只读快照 HTTP 接口可能要求额外认证并返回 401；本包不绕过认证，也不把它当成写入权检测。
- 桥接器对收到的权限请求只允许任务目录内的 Read / Write / Edit 和 WebSearch / WebFetch，其他请求拒绝。它不是操作系统沙箱，无法约束 WorkBuddy 已自行批准且不再向桥接器询问的操作；请勿使用跳过权限检查模式，并独立核对结果。
- 超时、连接断开或结果不明时，不要自动重试。先检查专用会话和目标文件，确认上一轮是否已经执行。`work/native-active.json` 用于阻止不明状态下再次提交；不要在任务仍运行时删除它。
- 模型默认 `auto`，可用安装参数 `-Model` 更改。限流、排队及积分消耗由 WorkBuddy 控制；桥接成功不代表拥有额外模型额度。
- 同名 MCP 已注册时，先检查原有配置。若确认迁移，应手动更新该条目的 Python 和脚本绝对路径；请勿重跑脚本强行覆盖其他配置。
- 卸载时，先停止任务，再从 Codex 配置中仅移除 `mcp_servers.workbuddy` 条目，重新加载 MCP 后移除本目录。不要用旧备份覆盖之后的其他配置变更。

## 验证

安装依赖后运行本地测试，不发送 WorkBuddy 任务：

```powershell
.\.venv\Scripts\python.exe -m unittest test_configure test_local_bridge test_compat test_writer test_runtime
$env:PYTHONPATH = (Resolve-Path .\vendor\desktop-bridge).Path
.\.venv\Scripts\python.exe -m unittest discover -s vendor/desktop-bridge/workbuddy_bridge -p 'test_*.py'
```

绑定完成后，可主动运行完整联网实测（会在专用聊天里发送三轮任务、使用 WorkBuddy 额度，并在本目录 `work/` 下写入随机测试数据）：

```powershell
.\.venv\Scripts\python.exe .\integration_test.py
```

成功条件为读写结果与本地期望一致、Edit 保留其他字段，以及观察到 WebSearch / WebFetch 完成事件。测试数据和报告不会被 Git 跟踪。

## 来源

复用 [Kxiandaoyan/workbuddy-acp-bridge](https://github.com/Kxiandaoyan/workbuddy-acp-bridge) 的活动端点发现，以及 [gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge) 的 ACP 客户端。

固定版本、修改说明与许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。`vendor` 中上游 README 描述的是各自原项目；本包以这份 README 的安装步骤和能力范围为准。
