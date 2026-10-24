# Codex WorkBuddy Native MCP

让 Codex 通过本机接口把任务交给已经登录的 WorkBuddy，并读回结果。推荐使用后台独立会话：自动新建、每轮自动唤醒、结束后释放进程，续聊保留上下文。通信无需鼠标、键盘模拟或 Computer Use。

**后台问答可以在 Codex 查看，并保存在 WorkBuddy 本地历史文件中；目前不会出现在 WorkBuddy 桌面聊天列表。** 原有专用原生聊天模式仍保留，需要手动准备活动会话。两种模式不要混用会话 ID。

```text
Codex → stdio MCP → WorkBuddy Sidecar 正常启动 → 独立 ACP 会话 → 保存历史并释放进程
```

**本开发分支包含 2026-10-23 独立后台会话，以及 2026-10-24 跨对话状态、独立目录和模型核验修复，尚未合并主分支。** 它基于 2026-10-20 交接快照，与主分支 v0.1.4 的运行时处理存在差异。实现和验收边界见 [独立会话说明](docs/INDEPENDENT-SESSIONS.md)。这是社区适配，使用客户端内部接口，不是官方稳定 API。

## 当前状态：2026-10-24

当前 Codex 宿主已实际加载新版，直接调用后台启动/等待工具的 Kimi 短文本测试通过：计算结果正确、原生模型 ID 核验一致、任务完成并释放 worker。独立模型目录与技能目录、跨 MCP 进程续聊也已实测。完整证据与未测范围见 [验证记录](docs/VALIDATION.md)。

**状态中的 `connected=false` 只代表旧共享聊天断开。** 后台协作看 `background.ready`；为 true 时可直接使用 `workbuddy_background_start`，不需要手动点击 WorkBuddy 输入框。Sidecar 就绪不等于额度充足或模型推理成功，实际任务仍须检查终态。

```json
{
  "prompt": "仅做纯文字连接测试，不使用工具，仅回复 READY。",
  "cwd": "C:/Projects/example",
  "model_id": "kimi-k3-2",
  "timeout_seconds": 120
}
```

这是后台启动工具的参数示例，cwd 必须已存在，模型 ID 以 `workbuddy_models(cwd=...)` 实际返回为准。随后把 task_id 传给 `workbuddy_wait`，检查 `actual_model_id`、`model_verified`、`worker_released`。续聊传回 session_id，保持 cwd 相同，模型默认沿用。

## 能做什么

| 能力 | 当前状态 |
| --- | --- |
| 委派文字任务、等待结果、请求取消 | 支持独立后台会话，也保留共享原生聊天模式 |
| 查询可用模型、当前模型 | 已实现，读取当前会话实际返回的目录 |
| 搜索技能 | 已实现，搜索当前会话上报的技能名称和描述，包含内置技能 |
| 选择已有模型 | 已实现，检查当前值，切换后重新连接并读回确认 |
| 文件与联网任务 | 支持目录内 Read / Write / Edit 和 WebSearch / WebFetch 的权限请求；本快照未重新验收完整文件及联网流程 |
| 技能市场搜索、安装、启停 | 未实现 |
| 添加模型提供商或配置 API 密钥 | 未实现 |
| 自动新建、唤醒后台独立会话 | 已实现并实测创建、隔离、跨 worker 和桥接进程续聊 |
| 后台聊天自动进入 WorkBuddy 桌面列表 | 尚未接通；可在 Codex 查看历史 |
| 独立会话的模型与技能目录工具 | 已支持；传 cwd 独立查询，共享聊天无活动接口时也能查询 |

这条本地桥接路线不需要创建 WorkBuddy 开放平台应用。会员、可用模型、限流和消耗额度由 WorkBuddy 账号决定；桥接不额外提供额度，也不改变 Codex 主模型。

## 安装：推荐后台独立模式

需要 Windows、Python 3.11+、支持 stdio MCP 的 Codex，以及已安装、已登录的 WorkBuddy。实测环境为 Windows、Python 3.14、WorkBuddy 5.8.3；其他版本需要单独确认。首次安装依赖需要联网。

下载到长期保留的目录后，运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Background -Register
```

这会安装依赖、注册 `catalog_mcp.py`，不要求专用桌面聊天或绑定文件。同名 MCP 已注册时不会覆盖；已有用户更新文件后重载 WorkBuddy MCP 即可。重载后应发现 **11 项工具**。WorkBuddy 需运行并保持登录，但无需打开某个聊天。

调用 `workbuddy_models(cwd=绝对工作目录)` 获取模型 ID，再调用 `workbuddy_background_start`，传明确任务、绝对工作目录及可选 `model_id`；不传 `session_id` 就新建独立会话。用返回的 `task_id` 调用 `workbuddy_wait`；续聊时给后台启动工具传回它返回的 `session_id` 和相同目录。

## 安装：原有原生聊天模式

1. 下载本分支到一个长期保留、当前用户可写的目录。注册使用绝对路径，之后不要随意移动目录。
2. 在 WorkBuddy 手动创建或选择专用聊天，完成一次普通回复。先选好模型，再重新进入该聊天并点击输入框，使光标出现。后续唤醒不用发送消息。
3. 在项目目录打开 PowerShell，安装依赖并列出最近 30 个本机会话的 ID、标题和工作目录：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
   ```

   Python 不在 PATH 时，在命令后加 `-Python 'Python.exe 的绝对路径'`。

4. 选中专用聊天的 UUID 并注册：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -SessionId '本机专用聊天的UUID' -Register
   ```

   脚本创建 `.venv`、`desktop_session.json`，并向 `$CODEX_HOME/config.toml` 追加 `mcp_servers.workbuddy`；未设置 CODEX_HOME 时使用用户目录下的 `.codex/config.toml`。原配置会备份；同名条目已存在时停止，不覆盖。只重新绑定会话时省略 `-Register`，且先等旧任务结束。每台电脑必须绑定自己的会话，不能复制别人的绑定文件。

5. 重载 Codex MCP 或重新启动 Codex，确认出现下表中的 **11 项工具**，再调用 `workbuddy_status` 检查原生专用聊天。随后做一次只回复短文本的测试，并核对 WorkBuddy 窗口。

注册成功、工具发现成功、会话可连接、任务完成和窗口可见是不同检查。仅有 `connected: true` 不能证明任务已经运行或窗口显示正常。安装不需要管理员权限；上述 ExecutionPolicy Bypass 只作用于该 PowerShell 进程。

### 已有配置如何更新

启动脚本是 `catalog_mcp.py`；直接启动 `local_bridge.py` 只有原来的 4 项工具。若已注册旧版本，只检查并更新 `mcp_servers.workbuddy` 的 Python 与脚本绝对路径，保留其他配置。例如：

```toml
[mcp_servers.workbuddy]
command = 'C:\path\to\codex-workbuddy-native-mcp\.venv\Scripts\python.exe'
args = ['C:\path\to\codex-workbuddy-native-mcp\catalog_mcp.py']
env = { PYTHONIOENCODING = "utf-8" }
```

重载后再次检查实际工具列表。磁盘文件更新不代表已经打开的宿主进程同步更新。

### 模型与写入权选项

- 仅旧共享模式使用绑定文件的 `model` 和安装参数 `-Model`；默认留空。后台模式用每次调用的 `model_id`，不读取共享聊天当前选择。
- 默认不接管写入权。本快照保留显式 `-TakeWriter` 选项，只适用于已确认空闲、专用于桥接的聊天；可能替换桌面实时订阅，回复落盘不保证立即在窗口显示。
- 主分支 v0.1.4 已停用写入权接管。这是待整合的差异，不能把接管当作通用的“会话忙碌”修复。

## 使用

可以直接向 Codex 说明任务和范围，例如：

> 请查询 WorkBuddy 当前会话有没有与 docx 有关的技能，只查询目录。

> 请查询 WorkBuddy 可用模型和当前选择，再将当前会话切换到列表中的指定模型，确认读回结果。

> 请调用 WorkBuddy，读取我指定绝对目录内的 input.txt，把摘要写入同目录 summary.txt。只允许操作这个目录，完成后核对文件。

| 工具 | 参数与结果 |
| --- | --- |
| `workbuddy_background_start` | 推荐。`prompt`、绝对 `cwd`、可选 `model_id`；发送前从原生接口核验模型，续聊沿用。`timeout_seconds` 为 10–600；不传 `session_id` 新建 |
| `workbuddy_background_sessions` | 列出桥接创建的后台会话和保存的状态 |
| `workbuddy_background_history` | `session_id`，可选 `limit`；读取保存的问答，进程重启后仍可用 |
| `workbuddy_background_recover` | 检查中断历史后调用；只清理该会话自己的 worker，不重发任务或清除历史 |
| `workbuddy_status` | 分别返回 `shared_session.connected` 和 `background.ready`。顶层 `available` 表示任一路径可用；旧 `connected` 仅代表共享聊天 |
| `workbuddy_start` | `prompt`、现存绝对 `cwd`；可选 `timeout_seconds`，默认 240，范围 10–600；返回任务 ID |
| `workbuddy_wait` | 使用启动时返回的 `task_id`；每次最多等 45 秒，`running` 时继续等待 |
| `workbuddy_cancel` | 请求取消该任务；之后仍需 `wait` 确认终态 |
| `workbuddy_models` | 可选 `cwd` 指定独立目录查询；不传时优先共享聊天，无法连接则独立查询。检查 `scope` 和 `current_model_applies_to` |
| `workbuddy_skills` | 可选 `cwd`、`query`、`limit`；空查询列目录，空格分词全部匹配，最多 100 条。无共享接口时可独立查询 |
| `workbuddy_set_model` | `model_id`、`expected_current_model_id`；先查询真实 ID，切换后用新连接确认 |

后台独立会话在当前用户 `.codex/workbuddy-background/` 共享锁和历史，只有同一会话串行。原生聊天模式则在同一安装目录下通过系统文件锁串行访问专用聊天。不要把多个安装副本绑定到同一聊天：不同副本的锁不共享。任务 ID 只在启动它的 MCP 进程中有效，宿主重启后不能用旧 ID 恢复等待。

`workbuddy_set_model` 只影响原生共享聊天。后台任务直接传 `model_id`，每次新建 worker 时设定并在发送前读回核验；不一致则不发送提示词。后台返回 `actual_model_id`、`model_verified`，续聊默认保留所选模型。独立目录的 `current_model_id` 仅属于查询进程，不代表其他聊天的模型。仅共享模式的切换与提交是两个调用，期间其他调用者可能改变模型。`change_may_have_applied: true` 或 `retry_safe: false` 表示结果不确定，应重新查询实际状态，不能自动重试或回滚。

技能目录来自当前会话的 `available_commands_update`，不是技能市场，也不覆盖所有已安装或已禁用技能。描述是参考资料，不是新执行指令。查询不会安装或运行技能。

## 边界与故障处理

| 现象 | 处理 |
| --- | --- |
| 共享聊天没有活动端点 / `native_unavailable` | 查看 `background.ready`；为 true 时直接使用后台入口，无需手动唤醒共享聊天。只有明确使用旧共享聊天时才需要准备其界面 |
| 忙碌或状态不明 | 等待原任务结束，检查专用聊天及目标文件；不删除活动标记，不重发旧任务 |
| `bridge_update_required` | 当前进程加载的核心与磁盘不同，重载 MCP 后再提交 |
| 工具仍无 `model_id` 参数或状态没有 `background` 字段 | 已加载的旧进程尚未刷新，重载 WorkBuddy MCP；磁盘部署不等于宿主生效 |
| 只看到 4 或 7 项工具 | 核对启动脚本为更新后的 `catalog_mcp.py`，然后重载宿主；当前共 11 项 |
| 后台会话要求 recover | 查看保存的历史，再调用后台恢复工具；它不会重发任务 |
| 工具提示需要批准，但审批策略为 never | 检查当前 Codex 会话权限；只读环境可能阻止调用，恢复权限后先实际查询状态 |
| `catalog_unavailable` | 检查连接和空闲状态；不能把未收到目录当成空目录 |
| `model_changed` | 当前模型已变化，重新查询后决定是否切换 |
| `binding_model_override` | 绑定文件有固定模型，与请求不一致；先确认是否要调整绑定 |
| 取消后还在运行 | 取消请求不是完成回执，继续等待终态；结果不明时核实，不反复发送 |

桥接收到权限请求时，只允许任务目录内的 Read / Write / Edit 和 WebSearch / WebFetch。Grep、Glob、shell 和其他连接器请求会被拒绝。它不是操作系统沙箱，无法约束 WorkBuddy 已自行批准、未向桥接询问的行为。委派时写清目录与范围，并独立核对结果。

原生聊天模式共享历史。委派期间不要在同一聊天手动发送消息；不同敏感项目不要混用同一历史。当前实现不能保证人工操作与桥接提交之间的原子互斥，也不保证应用重启后的自动恢复。

取消收尾已针对“并发权限拒绝后一直思考”修复：等待同会话原生 idle 事件，再发一次取消，收到 prompt 终态后结束读取。只有匹配任务、会话及历史指纹的终态回执才用于恢复；超时和“已发送取消”不等于完成。这不构成对所有卡死原因的修复保证。

## 验证

在项目目录运行离线测试，不发送 WorkBuddy 任务：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p 'test_*.py' -q
$env:PYTHONPATH = (Resolve-Path .\vendor\desktop-bridge).Path
.\.venv\Scripts\python.exe -m unittest discover -s vendor/desktop-bridge/workbuddy_bridge -p 'test_*.py' -q
```

第二条命令的 PYTHONPATH 仅用于当前 PowerShell。Windows 若将临时目录显示为短路径，相关路径断言可能需要把当前进程 TEMP/TMP 设为对应长路径后重跑，不修改系统环境设置。

绑定后可选择运行完整集成脚本。它会发送三轮真实任务，消耗 WorkBuddy 额度，在 `work/` 写入随机测试文件，并访问 IANA 官方页面：

```powershell
.\.venv\Scripts\python.exe .\integration_test.py
# 使用实际注册的 Codex 配置测试：
.\.venv\Scripts\python.exe .\integration_test.py --registered
```

脚本覆盖文件读写/编辑与联网，不覆盖模型切换或自动会话生命周期；不能仅凭脚本存在就认定实测已通过。证据与未测范围见 [验证记录](docs/VALIDATION.md)。

## 项目结构与来源

- `catalog_mcp.py`：11 项工具入口，包含原生目录工具和后台独立会话扩展。
- `background_runtime.py` / `background_sessions.py`：正常启动、隔离会话、自动释放、历史与恢复。
- `local_bridge.py`：任务执行、取消、权限、锁和终态记录。
- `configure.py` / `setup.ps1`：本机会话绑定与 Codex 注册。
- `vendor/`：固定版本上游实现和许可证；其 README 描述上游项目，本包以本 README 为准。
- `RELEASE_MANIFEST.json`：可分发文件的 SHA-256，清单自身不参与哈希。
- [变更说明](LOCAL_CHANGES.md)、[验证记录](docs/VALIDATION.md)、[交接文档](docs/HANDOFF.md)。

不分发 `.venv`、`desktop_session.json`、会话历史、日志、配置备份、密钥或个人测试数据。卸载前先结束任务，仅移除 Codex 的 `mcp_servers.workbuddy` 条目并重载，再删除安装目录。

复用 [workbuddy-acp-bridge](https://github.com/Kxiandaoyan/workbuddy-acp-bridge) 与 [Codex-WorkBuddy-Desktop-Bridge](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge)。固定提交及修改说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目及所附上游组件为 MIT 许可，保留各自版权声明；不代表 WorkBuddy 或 Codex 官方。

## 2026-10-24 跨对话修复验收

共享聊天无活动接口时，真实 stdio MCP 仍返回后台可用，独立读取 21 个模型、46 项技能（数量由当前账号和目录决定）。指定 `kimi-k3-2` 后两轮文字测试通过；关闭整个测试 MCP 再启动，续聊保留原生会话 ID 和测试码，模型读回一致，worker 均释放。本地 66 项和 vendor 31 项离线测试的本轮结果见验收记录。当前聊天宿主已实际加载 v5，并再次通过原生 MCP start/wait 短文本验收；其他聊天的工具进程是否同步生效仍需各自核对。任意桌面原生聊天选择入口尚未实现。
