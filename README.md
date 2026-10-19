# Codex WorkBuddy Native MCP

Windows 上通过本机 ACP 接口，让 Codex 委派任务给已登录的 WorkBuddy，并把对话保留在 WorkBuddy 的一个专用原生会话中。版本：0.1.3。

运行方式是 stdio MCP → 本机 WorkBuddy ACP；不模拟键盘、鼠标，不调用 Computer Use。

## 已验证范围

- 2026-10-13，Windows、Python 3.12、WorkBuddy 5.8.2：在已有原生会话中完成文字回复、Read / Write / Edit 文件操作和 WebSearch / WebFetch 联网任务。
- 2026-10-16，WorkBuddy 5.8.2：通过已注册的 stdio MCP 入口复测文件和联网操作；主动回收连接器专用运行进程后，下一次新任务自动恢复，回复保存在相同原生会话中且只提交一次。56 项自动测试及解压包 MCP 启动检查通过。
- 2026-10-19，WorkBuddy 5.8.3：修复同一会话存在两个进程时误选连接器后台进程的问题；通过全新 stdio MCP 完成纯文字测试，桌面会话日志收到更新。旧版本的“记录已保存”不能作为窗口可见的证明。
- 发布包自动测试覆盖本地逻辑和上游兼容性；还没有在第二台实体电脑上验收。
- 使用 WorkBuddy 当前登录状态。包内不含账号凭据、聊天记录、会话绑定、Python 环境或个人测试文件。
- 这是基于客户端内部接口的社区适配，不是官方稳定 API；客户端升级后可能需要适配。

## 新电脑安装

需要 Windows、Python 3.11 或更新版本、Codex，以及已经安装并登录的 WorkBuddy。首次安装依赖需要联网。

1. 将整个目录解压到一个长期保留、当前用户可写的位置。注册后不要移动目录，否则 MCP 的绝对路径会失效。
2. 在 WorkBuddy 中手动创建或选择一个专用会话，发送一句普通消息并等回复结束。每台电脑必须选择它自己的会话，不能沿用其他电脑的 UUID。后续使用时保持 WorkBuddy 已启动、登录，并打开该专用会话。
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

5. 重启 Codex 或重新加载 MCP，然后让 Codex 调用 `workbuddy_status`。`connected: true` 表示发现了可连接的本机端口，且原生记录显示空闲；`requires_desktop_open: true` 表示需要在 WorkBuddy 重新打开专用会话。状态检查不启动进程、不获取写入权，不能代替实际任务验收。注册成功本身也不能证明连接成功。

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

- 必须先登录 WorkBuddy、绑定并打开专用会话。0.1.3 只连接该会话的桌面运行进程，排除旧版连接器创建的 `codex-native-*` 后台进程；多个候选进程无法区分时停止，不猜测最新端口。
- 0.1.2 的后台恢复进程虽然使用相同会话 UUID 并保存记录，但不是窗口订阅的进程。0.1.3 停止这种自动恢复：桌面进程被回收后，请在 WorkBuddy 重新打开专用聊天，然后再提交新任务。连接器不操作界面，也不重发以前的任务。
- 0.1.3 不再使用 `takeWriter`：该接口会关闭原有桌面消息订阅。写入权被占用时直接报告错误，不接管、不重发。
- 回复落盘、桌面接收事件和窗口实际显示是三个不同的验证层次。状态检查不证明窗口可见；首次安装或升级后，请用一条纯文字消息核对窗口。此前已落盘但未显示的消息可能需要重新打开会话或重启 WorkBuddy 后重新加载；不要补发旧任务。
- 原生会话保留历史。委派期间不要在它里面手动发送消息；不同敏感项目应使用不同会话，重新绑定前等任务结束。
- 状态检查仅探测 TCP，不创建 ACP 连接。任务结束时检查连接释放响应；失败会返回 `connection_cleanup_error`，此时先确认原任务结果，不能盲目重发。客户端的独立只读快照 HTTP 接口可能要求额外认证并返回 401；本包不绕过认证，也不把它当成写入权检测。
- 桥接器对收到的权限请求只允许任务目录内的 Read / Write / Edit 和 WebSearch / WebFetch，其他请求拒绝。它不是操作系统沙箱，无法约束 WorkBuddy 已自行批准且不再向桥接器询问的操作；请勿使用跳过权限检查模式，并独立核对结果。
- 超时、连接断开或结果不明时，不要自动重试。先检查专用会话和目标文件，确认上一轮是否已经执行。`work/native-active.json` 用于阻止不明状态下再次提交；不要在任务仍运行时删除它。
- 模型默认 `auto`，可用安装参数 `-Model` 更改。限流、排队及积分消耗由 WorkBuddy 控制；桥接成功不代表拥有额外模型额度。
- 同名 MCP 已注册时，先检查原有配置。若确认迁移，应手动更新该条目的 Python 和脚本绝对路径；请勿重跑脚本强行覆盖其他配置。
- 卸载时，先停止任务，再从 Codex 配置中仅移除 `mcp_servers.workbuddy` 条目，重新加载 MCP 后移除本目录。不要用旧备份覆盖之后的其他配置变更。

## 验证

安装依赖后运行本地测试，不发送 WorkBuddy 任务：

```powershell
.\.venv\Scripts\python.exe -m unittest test_configure test_local_bridge test_compat test_writer test_runtime test_visibility
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
