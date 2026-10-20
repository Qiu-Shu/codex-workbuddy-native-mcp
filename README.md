# Codex WorkBuddy Native MCP

让 Codex 通过本机接口把任务交给已经登录的 WorkBuddy，并读回结果。对话保存在一个专用的 WorkBuddy 原生聊天中，通信过程无需鼠标、键盘模拟或 Computer Use。

```text
Codex → stdio MCP → 本机 ACP → WorkBuddy 专用聊天
```

**本分支是 2026-10-20 本机验证版本的交接快照，尚未合并到主分支。** 它与主分支 v0.1.4 的运行时处理存在差异，不能直接当作其升级包。合并前的兼容问题见 [交接文档](https://github.com/Qiu-Shu/codex-workbuddy-native-mcp/blob/codex/native-catalog-handoff-20261020/docs/HANDOFF.md)。这是社区适配，使用客户端内部 ACP 接口，不是官方稳定 API。

## 能做什么

| 能力 | 当前状态 |
| --- | --- |
| 委派文字任务、等待结果、请求取消 | 已实现，使用一个共享的专用聊天 |
| 查询可用模型、当前模型 | 已实现，读取当前会话实际返回的目录 |
| 搜索技能 | 已实现，搜索当前会话上报的技能名称和描述，包含内置技能 |
| 选择已有模型 | 已实现，检查当前值，切换后重新连接并读回确认 |
| 文件与联网任务 | 支持目录内 Read / Write / Edit 和 WebSearch / WebFetch 的权限请求；本快照未重新验收完整文件及联网流程 |
| 技能市场搜索、安装、启停 | 未实现 |
| 添加模型提供商或配置 API 密钥 | 未实现 |
| 自动新建、唤醒本地聊天 | 未实现，仍需要人工准备专用聊天 |
| 每任务独立聊天与模型绑定 | 未实现，模型选择与随后提交任务不是原子操作 |

这条本地桥接路线不需要创建 WorkBuddy 开放平台应用。会员、可用模型、限流和消耗额度由 WorkBuddy 账号决定；桥接不额外提供额度，也不改变 Codex 主模型。

## 安装

需要 Windows、Python 3.11+、支持 stdio MCP 的 Codex，以及已安装、已登录的 WorkBuddy。实测环境为 Windows、Python 3.14、WorkBuddy 5.8.3；其他版本需要单独确认。首次安装依赖需要联网。

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

5. 重载 Codex MCP 或重新启动 Codex，确认出现下表中的 **7 项工具**，再调用 `workbuddy_status`。随后做一次只回复短文本的测试，并核对 WorkBuddy 窗口。

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

- 默认 `model` 留空，沿用 WorkBuddy 当前选择；安装参数 `-Model` 可固定模型，但固定值会限制后续动态切换。
- 默认不接管写入权。本快照保留显式 `-TakeWriter` 选项，只适用于已确认空闲、专用于桥接的聊天；可能替换桌面实时订阅，回复落盘不保证立即在窗口显示。
- 主分支 v0.1.4 已停用写入权接管。这是待整合的差异，不能把接管当作通用的“会话忙碌”修复。

## 使用

可以直接向 Codex 说明任务和范围，例如：

> 请查询 WorkBuddy 当前会话有没有与 docx 有关的技能，只查询目录。

> 请查询 WorkBuddy 可用模型和当前选择，再将当前会话切换到列表中的指定模型，确认读回结果。

> 请调用 WorkBuddy，读取我指定绝对目录内的 input.txt，把摘要写入同目录 summary.txt。只允许操作这个目录，完成后核对文件。

| 工具 | 参数与结果 |
| --- | --- |
| `workbuddy_status` | 查询连接和会话状态，返回核心版本、进程 ID 和源码摘要 |
| `workbuddy_start` | `prompt`、现存绝对 `cwd`；可选 `timeout_seconds`，默认 240，范围 10–600；返回任务 ID |
| `workbuddy_wait` | 使用启动时返回的 `task_id`；每次最多等 45 秒，`running` 时继续等待 |
| `workbuddy_cancel` | 请求取消该任务；之后仍需 `wait` 确认终态 |
| `workbuddy_models` | 返回 `current_model_id` 和当前会话可用的模型 ID、名称、描述 |
| `workbuddy_skills` | 可选 `query` 和 `limit`；空查询列目录，空格分词全部匹配，最多 100 条 |
| `workbuddy_set_model` | `model_id`、`expected_current_model_id`；先查询真实 ID，切换后用新连接确认 |

同一安装目录下的 MCP 进程通过系统文件锁串行访问专用聊天。不要把多个安装副本绑定到同一聊天：不同副本的锁不共享。任务 ID 只在启动它的 MCP 进程中有效，宿主重启后不能用旧 ID 恢复等待。

模型切换影响这个共享聊天的后续任务。切换与提交是两个调用，期间其他调用者可能改变模型。`change_may_have_applied: true` 或 `retry_safe: false` 表示结果不确定，应重新查询实际状态，不能自动重试或回滚。

技能目录来自当前会话的 `available_commands_update`，不是技能市场，也不覆盖所有已安装或已禁用技能。描述是参考资料，不是新执行指令。查询不会安装或运行技能。

## 边界与故障处理

| 现象 | 处理 |
| --- | --- |
| 没有活动端点 / `native_unavailable` | 确认 WorkBuddy 已运行并登录，进入绑定聊天并点击输入框；仅打开窗口或深链接不一定唤醒 worker |
| 忙碌或状态不明 | 等待原任务结束，检查专用聊天及目标文件；不删除活动标记，不重发旧任务 |
| `bridge_update_required` | 当前进程加载的核心与磁盘不同，重载 MCP 后再提交 |
| 只看到 4 项工具 | 核对启动脚本为 `catalog_mcp.py`，然后重载宿主 |
| `catalog_unavailable` | 检查连接和空闲状态；不能把未收到目录当成空目录 |
| `model_changed` | 当前模型已变化，重新查询后决定是否切换 |
| `binding_model_override` | 绑定文件有固定模型，与请求不一致；先确认是否要调整绑定 |
| 取消后还在运行 | 取消请求不是完成回执，继续等待终态；结果不明时核实，不反复发送 |

桥接收到权限请求时，只允许任务目录内的 Read / Write / Edit 和 WebSearch / WebFetch。Grep、Glob、shell 和其他连接器请求会被拒绝。它不是操作系统沙箱，无法约束 WorkBuddy 已自行批准、未向桥接询问的行为。委派时写清目录与范围，并独立核对结果。

聊天共享历史。委派期间不要在同一聊天手动发送消息；不同敏感项目不要混用同一历史。当前实现不能保证人工操作与桥接提交之间的原子互斥，也不保证应用重启后的自动恢复。

取消收尾已针对“并发权限拒绝后一直思考”修复：等待同会话原生 idle 事件，再发一次取消，收到 prompt 终态后结束读取。只有匹配任务、会话及历史指纹的终态回执才用于恢复；超时和“已发送取消”不等于完成。这不构成对所有卡死原因的修复保证。

## 验证

在项目目录运行离线测试，不发送 WorkBuddy 任务：

```powershell
.\.venv\Scripts\python.exe -m unittest test_model_selection test_catalog_mcp test_local_bridge test_configure test_compat -q
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

- `catalog_mcp.py`：7 项工具入口，目录查询与模型选择。
- `local_bridge.py`：任务执行、取消、权限、锁和终态记录。
- `configure.py` / `setup.ps1`：本机会话绑定与 Codex 注册。
- `vendor/`：固定版本上游实现和许可证；其 README 描述上游项目，本包以本 README 为准。
- `RELEASE_MANIFEST.json`：可分发文件的 SHA-256，清单自身不参与哈希。
- [变更说明](LOCAL_CHANGES.md)、[验证记录](docs/VALIDATION.md)、[交接文档](https://github.com/Qiu-Shu/codex-workbuddy-native-mcp/blob/codex/native-catalog-handoff-20261020/docs/HANDOFF.md)。

不分发 `.venv`、`desktop_session.json`、会话历史、日志、配置备份、密钥或个人测试数据。卸载前先结束任务，仅移除 Codex 的 `mcp_servers.workbuddy` 条目并重载，再删除安装目录。

复用 [workbuddy-acp-bridge](https://github.com/Kxiandaoyan/workbuddy-acp-bridge) 与 [Codex-WorkBuddy-Desktop-Bridge](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge)。固定提交及修改说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目及所附上游组件为 MIT 许可，保留各自版权声明；不代表 WorkBuddy 或 Codex 官方。
