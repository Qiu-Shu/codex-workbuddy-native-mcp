# 后台独立会话：2026-10-24 更新

## 已接通

Codex 可以自动新建 WorkBuddy 后台独立会话，完成任务后释放其进程，下一轮启动新进程并恢复同一会话。无需点击聊天输入框，也不占用原来的专用桌面聊天。WorkBuddy 必须运行并登录。

`workbuddy_background_start(prompt, cwd)` 返回桥接会话 ID 和任务 ID。`workbuddy_wait(task_id)` 读取结果；续聊时向后台启动工具传回桥接会话 ID 和相同 cwd。不传 ID 表示新会话，不会猜测要复用哪一段历史。

新工具共四项：后台启动、会话列表、历史读取、中断恢复。原有七项保留，整个 `catalog_mcp.py` 入口共 11 项。2026-10-24 已扩展独立模型/技能目录与后台模型核验：查询传 cwd，任务传 model_id；续聊默认沿用。共享会话没有活动端点不阻断后台入口。

## 生命周期与权限

启动使用当前 WorkBuddy Sidecar 的 `session.create(port=0)`，由 WorkBuddy 自己完成临时端口和登录材料初始化。随后走正常 loopback ACP connect、initialize、session/new 或 session/load。桥接不读取密钥存储、不复制登录凭据、不关闭认证、不读取终端中打印的密码。

不使用 `--no-session-persistence`，也不发送 `/clear`。独立 worker 的实际工作目录就是已授权的 cwd。启动为默认权限模式并禁用额外 MCP 配置，权限处理复用已有 ScopedClient：仅允许该目录下的 Read/Write/Edit 与 WebSearch/WebFetch，其余权限请求拒绝。这不是操作系统沙箱，不能约束 WorkBuddy 自身预批准的行为。

每轮 finally 停止自己创建的唯一 worker，已有 runtimeId 时同时验证它。后台 ID、运行状态和问答保存在当前用户 `.codex/workbuddy-background/`；跨安装副本共享此锁目录。同会话从排队开始即加锁，不允许两个 MCP 同时续聊。最多并行两个后台任务。

取消先使用既有 ACP 取消请求；8 秒仍没有完成回执时，只结束该任务所属的 worker。启动、回复和停止各有独立超时，因此总耗时可能大于回复参数 `timeout_seconds`。失败或超时不会自动再次提交同一提示词。

若 MCP 异常退出，记录可能保持 running；下次启动会拒绝直接续聊。先查看历史，再调用 `workbuddy_background_recover` 清理保存的 worker 身份；该工具不重发任务，不删除记录，不撤销先前文件等副作用。旧 Sidecar 已消失时，仅确认记录中的 worker PID 不存在才允许清除恢复状态。

## 历史在哪里

1. **Codex 可读的问答回执**：`workbuddy_background_history`，桥接重启后仍可用。中断时可能没有最后一段回答，原生 JSONL 可能包含更多部分内容。
2. **WorkBuddy 原生本地历史文件**：正常写入 `.workbuddy/projects/<cwd 对应目录>/<native-session-id>.jsonl`，可供后续 ACP load 恢复。
3. **WorkBuddy 桌面聊天列表**：本轮创建的测试会话没有 SQLite `sessions` 行，不会自动显示。`native_history_visible` 明确返回 false。

只读检查没有发现正常外部导入接口。`workbuddy://chat/<id>` 只导航；桌面列表只读已登记的 SQLite 会话。代码中的 JSONL 重建是数据库故障恢复机制，不应借用来登记正常会话。内部 `conversations.create` 存在，但尚未找到可从外部正常调用的入口。没有修改数据库或注入界面脚本。

## 2026-10-23 历史验收

- WorkBuddy 5.8.3、Windows、Python 3.14。
- 原始正常启动路径创建、连接、文字回复成功；释放 worker 后恢复，正确记住上一轮测试词。
- 实际 stdio MCP 发现 11 工具；新建 A 会话回复成功；新建 B 会话未携带 A 的测试词。
- 关闭整个测试 MCP 进程，再启动 MCP 恢复 A，会话 native ID 保持一致并正确回答测试词；历史返回两轮。
- 每个已完成测试任务返回 worker_released=true，requires_recovery=false。
- 异常关闭导致锁泄漏、原 Sidecar 消失后无法恢复，两项独立审查发现已补回归测试并修复；取消无回执的限定 worker 清理也有离线测试。
- 10 月 23 日验收时宿主尚未刷新；10 月 24 日已直接确认当前宿主 v5、11 工具及 Kimi start/wait 成功。其他聊天需核对各自实际状态。

未验证：第二台电脑、升级后的接口兼容性、长时间无人值守、整个 WorkBuddy 应用重启后的真实恢复，以及完整后台文件/联网任务。旧 Sidecar 与 worker 都退出的恢复分支已做 Windows 离线故障注入验证；不能把它称为完整应用重启验收。

## 后续边界

当前宿主已确认加载 v5、新模型参数和分路径状态，并通过真实问答；其他聊天升级时仍需核对参数与实际调用，不能只数工具名称。后续若要让同一后台会话出现在 WorkBuddy 桌面列表，需要正常的本地会话登记入口；在此之前保留真实可读的 Codex 历史。v6 合并版本已恢复主分支的共享聊天防护；后台路径继续使用独立 worker。GitHub 更新不代表本机宿主已加载 v6，需重载后核对。

## 2026-10-24 修复

状态增加 available、shared_session、background 和 recommended_tool。背景就绪检查只证明 Sidecar 可访问，不保证账号余额或模型推理成功。connected 保留共享聊天语义。独立元数据 worker 不发送提示词；查询结束后释放。显式模型由启动参数传入，再通过 session/load 获取 currentModelId 和 availableModels 核验。失败时不发送用户提示词，保留实际模型结果供诊断。不能用模型自述代替核验。
