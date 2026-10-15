# Agent Link 薄层规范 v0.1

状态：2026-10-15 首个本机实现规范。目标组合：Codex 桌面父会话 + Pi CLI 子会话。

## 1. 目标和边界

统一框架之间的任务绑定、运行中汇报、决策请求与回复语义。父/子是一次任务绑定的角色，不是框架的固定属性；一个会话可以同时承担两种角色。

保留原框架后台运行、取消、超时和退出管理。v0.1 通过 background-tasks 启动 Pi，薄层不实现进程运行器，不要求 Intercom broker，不修改已安装插件缓存。后续允许替换传输，不承诺当前文件协议与 Intercom 协议兼容。

## 2. 分层

- 核心：绑定、消息/请求标识、幂等与冲突检查、持久化、状态转换。
- 适配器：父端通知、子端等待答复；框架 ID 与原生会话/任务 ID 保持独立。
- 可选宿主服务（附加实现，不改本节语义）：父端可在本机启动 binding 级 loopback HTTP 服务，子端只持 endpoint/token 访问 report/ask/get-request；宿主驱动以 `HostDriver` 协议替换。协议、错误、资源上限与生命周期见 `docs/HOST-PROTOCOL.md`。
- CLI：为 shell agent 提供通用入口。Python 标准库实现，无第三方运行依赖。
- 可选生命周期接口：spawn/cancel，v0.1 声明不支持；可以绑定已经启动的任务。

## 3. 身份与信任

每个 binding 固定 parent.framework/session_id、child.framework/session_id、run_id、native_task_id（允许未知为空）。绑定创建后不可重定向；同一配置重复绑定幂等，不同配置冲突。

本版以同一 OS 用户、可信子执行器为前提，目录权限 0700；子进程不能通过 report/ask 参数选择其他父会话。它不是针对恶意同用户进程的安全隔离。运行目录不可写入仓库源码或提交凭据。适配器只加载绑定时指定的本机 notifier.py，路径须存在；不得根据消息内容执行代码。

## 4. 消息与请求

report(binding, message_id, text) 是无需回复的进度事件。
ask(binding, request_id, message_id, question, timeout) 先持久化，再通知父端，然后等待 reply；返回结构化答复，超时退出非零。
reply(binding, request_id, message_id, answer) 必须关联已存在请求，不允许覆盖答复。
get_request(binding, request_id) 返回持久化状态和答复。

标识符只允许字母、数字、下划线、连字符，长度 1..128；禁止路径穿越。相同 message_id 和相同内容重复提交幂等；相同 ID 内容不同明确冲突。不同 message_id 对同一请求再次回复也不得覆盖，返回冲突。

请求状态：pending -> replied 或 timed_out；timed_out 的首个迟到答复仍保存，标记 late=true，状态保持 timed_out。截止时间到后，即使等待者尚未更新状态，也不能把迟到回复当作及时回复。使用持久化 UTC epoch 截止时间、进程内单调时钟控制等待；记录墙钟跳变限制。

复审澄清（v0.1 修复，仅明确语义，协议不变）：

- timeout 是请求语义的一部分：同一 request_id/message_id/question 重复 ask 时 timeout 必须相同才幂等，否则冲突；timeout 必须为正有限数，核心 API 与 CLI 都校验。
- 等待期限由持久化 deadline_epoch 映射到单调时钟，映射只做一次且不重复扣减通知耗时；重复 ask 只使用原截止时间的剩余量，不重新计时。
- 到达 deadline（当前墙钟 >= deadline，或单调等待期限已到）即按 timed_out 处理；已进入 timed_out 的请求，其首个答复一律 late=true，不因墙钟回拨而改变。
- reply 的消息身份包含目标 request_id：同一 message_id 不得用于回复不同请求，检查在写事务内完成，跨进程并发也只允许一个成功。

## 5. 投递状态与失败

必须区分 persisted、dispatching、host_accepted、failed_or_unknown。桌面桥成功仅代表宿主接口接受；模型是否处理必须由父端实际行为/答复证明，不能使用桥的 delivered 字段声称模型已处理。

在外部调用前持久化 dispatching；异常或中断状态不可盲目重发。v0.1 不自动重试通知；重复 CLI 调用也不得自动重复投递。未确定投递和请求超时分别记录，不能合并。出错时保留可查询请求，方便父端人工恢复。

## 6. 能力声明

使用 JSON 声明 notification_transport、receive_mode、reply_mode、busy_behavior、wake_after_turn、spawn、cancel。不确定值必须为 unknown，不以框架名称推断能力。

当前 Codex 适配：notification_transport=desktop_bridge；receive_mode=host_injection；busy_behavior=unknown；wake_after_turn=unknown。
当前 Pi 适配：reply_mode=blocking_tool_file_poll；spawn=false；cancel=false。
当前 OMP 适配：notification_transport=session_bridge_file；receive_mode=session_injection；reply_mode=blocking_tool_file_poll；spawn=false；cancel=false（busy_behavior/wake_after_turn=unknown）。
这些值描述本适配方式，不代表框架全部原生能力。

## 7. v0.1 CLI 契约

源码包 agent_link，仓库根执行 python3 -m agent_link。所有命令接受 --root ABS_DIR，stdout 是一条 JSON 结果；诊断写 stderr；错误非零。

bind --config FILE
capabilities
report --binding ID --message-id ID --text TEXT
ask --binding ID --request-id ID --message-id ID --question TEXT --timeout SECONDS
reply --binding ID --request-id ID --message-id ID --answer TEXT
get-request --binding ID --request-id ID

bind 配置 JSON：binding_id、run_id、native_task_id、parent={framework,session_id}、child={framework,session_id}、notifier_path。回复 answer 是文本。ask 的成功 JSON 必须包含 request_id、answer、late、status。CLI 不在命令行传任何密钥。

## 8. 存储与并发

推荐单个 SQLite 数据库、事务和唯一约束。不同 CLI 进程可能同时 ask/reply，禁止只用进程内锁。不得在数据库写事务内执行外部通知或等待回复。请求、回复和审计事件保留到显式人工清理，不实现自动删除。

## 9. 验证要求

测试至少覆盖绑定不可重定向、ID 校验、相同消息幂等/不同内容冲突、重复回复不可覆盖、未知请求、截止时间与迟到回复、重开数据库恢复、两个独立连接的冲突，以及模拟 notifier 失败后不重投。复审补充覆盖：慢通知不缩短真实截止时间、同一 message_id 跨请求回复冲突（含两个独立连接并发）、timeout 变更冲突与非有限/非正 timeout 拒绝、deadline 边界（>=）以及已超时请求在墙钟回拨下仍 late=true。模拟传输测试不发送真实桌面消息。

真实验收使用新实现的 CLI：Pi 完成实现和测试后，创建 live-001 绑定；report 主动通知；ask 请求父端给验收标记，Pi 等待；Codex 使用 reply CLI 返回答复；Pi 把原样答复写入 IMPLEMENTATION-RESULT.md，再正常退出。父端独立复验代码和测试，按事实更新已验证能力。

## 10. 已知限制

单机可信 OS 用户、桌面消息管道需存活、文件轮询有延迟；不承诺应用重启后通知可用、不实现 exactly-once 外部投递。MCP/Intercom/OpenCode/zcode 适配和恢复策略留待后续版本。
