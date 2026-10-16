# Agent Link 术语约定

面向开发者、文档作者和 AI agent。讨论设计、编写任务 brief、接口和报告时采用本表；现有公开命令、字段和插件标识保持兼容，不因中文术语统一而改名。

## 基本概念

| 中文术语 | 英文 | 定义与边界 |
| --- | --- | --- |
| Agent 框架 | Agent Framework | 执行模型并提供工具、会话管理的软件，例如 Codex、Pi、OpenCode。 |
| 会话 | Session | 框架中的对话与执行上下文；Codex 的聊天在本项目映射为 session，实际接口中的 thread/thread_id 保留原名。 |
| 父 agent / 子 agent | Parent / Child Agent | 某次任务委派中的角色，不是框架的固定属性；一个会话可同时向上汇报、向下委派。 |
| 任务 | Task | 一项被委派的工作；不等于会话、进程或后台工具返回的句柄。 |
| 运行 | Run | 任务的一次执行尝试；重试通常产生新的 run_id，不等于新会话。 |
| 绑定 | Binding | 将一次运行与父子会话、原生任务句柄关联的不可重定向记录；当前 v1 一个 binding 对应一组父子关系。 |
| 原生任务 ID | Native Task ID | 框架或运行器返回的任务句柄；当前字段 native_task_id，可在尚未知晓时为空，不用 run_id 冒充。 |

当前实现尚无独立的逻辑 Task 实体/通用 task_id 字段；Task 是设计层概念，不代表数据库已经实现任务调度。`background-tasks` 的 `task_id` 是运行器原生句柄，在 Agent Link 中对应 `native_task_id`。

## 组件与职责

| 中文术语 | 英文 | 职责与当前映射 |
| --- | --- | --- |
| 跨框架协作层 | Cross-framework Collaboration Layer | Agent Link 项目的定位：统一绑定、汇报、决策请求与答复，不接管所有框架的生命周期。 |
| 后台运行器 | Task Runner | 启动、查询、取消后台执行，管理执行超时与退出；Codex 当前使用 background-tasks，其他框架可用原生机制。TaskRunner 是架构角色，当前 Agent Link 没有同名通用接口。 |
| 父端宿主适配器 | Host Adapter | 将通知投递到父会话并声明宿主能力；代码接口 ParentAdapter，Codex 实现 CodexParentAdapter。不把整个框架称为适配器。 |
| 子端适配器 | Child Adapter | 把通信操作暴露为子 agent 可调用的工具；当前为 Pi 扩展 extensions/pi/agent-link.ts、OMP 扩展 extensions/omp/agent-link.ts 与 OpenCode 插件 extensions/opencode/agent-link.ts（Pi/OMP 在 session_start 注册 HTTP 接收接口；OMP 父→子也使用主动推送；共享实现 extensions/shared/session-push.ts）。 |
| 通信服务 | Link Service | 为子端提供认证、绑定范围内的通信入口及请求状态；当前入口 python3 -m agent_link.host，类 HostService。 |
| 通知桥 | Notification Bridge | 连接具体宿主接口的投递实现；Codex 当前使用 background-tasks 中的 notifier.py / app_bridge.mjs；Pi/OMP 使用 session HTTP 主动推送；OpenCode 使用框架原生 prompt HTTP 推送（描述符契约，见 HOST-PROTOCOL §6.3）。 |
| 桥接文件 | Bridge File | 已废弃术语：OpenCode 迁移前的父端通知 JSONL 文件（`<root>/omp-bridge/<binding_id>.jsonl`）。去轮询切片 2 已删除插件 watcher 与该文件，仓库不再有实现；历史文档出现此词仅指旧实现。 |
| 绑定索引 / 注册表 | Binding Registry | 用户级发现索引（`~/.agent-link/registry/`，`AGENT_LINK_REGISTRY` 可覆盖）：bind 与宿主服务写入绑定条目，父/子端适配器按本会话 id 自动认领绑定；只是发现指针，权威记录仍是各 runtime root 的 SQLite。 |
| 收件箱文件 | Inbox File | 已废弃术语：OpenCode 迁移前的父→子事件 JSONL（`<root>/inbox/<binding_id>.jsonl`）。去轮询切片 2 已删除；Pi RPC、Pi/OMP session push 与 OpenCode 原生 prompt 推送均不使用。 |
| 会话注入 | Session Injection | receive_mode 的一种：把通过校验的桥事件作为用户消息注入父会话（OMP 当前用 `deliverAs:"aside"`、`attribution:"agent"`）；注入成功不等于模型已处理。 |
| 传输 | Transport | 消息请求的通信方式，如当前本机 loopback HTTP。MCP 是可选工具接入方式，当前 v1 子端协议不是 MCP。 |

推荐描述：子端适配器通过通信服务提交消息，通信服务调用父端宿主适配器，由通知桥投递到父会话；后台运行器独立负责进程生命周期。当前服务同时承载协议处理与状态管理，不代表所有模块已独立部署。

## 消息操作

| 中文术语 | 操作 / 英文 | 含义 |
| --- | --- | --- |
| 汇报 | report | 无需答复的进度消息；Pi 工具 report_parent。 |
| 决策请求 | ask | 需要父端答复的请求；Pi 工具 ask_parent，请求以 request_id 关联。 |
| 答复 | reply | 关联到某个决策请求的父端答复；不等于工具返回、接收确认或新的用户授权。 |
| 请求查询 | get-request | 查询持久化状态及答复；Pi 工具 check_parent_request。 |
| 父端请求 | ask_child / ask-child | 父端发起、需子端答复的请求；状态 `pending -> answered | timed_out`，语义与 ask 镜像。 |
| 子端答复 | answer | 答复某个父端请求，不可覆盖；OMP 子端工具 answer_parent，语义与 reply 镜像。 |
| 完成通知 | Completion Notification | 后台运行器报告执行完成、失败、取消或超时的终态；区别于运行中的汇报。 |

`message_id` 标识消息和幂等提交，`request_id` 关联请求与答复。重投消息与重新运行任务是不同动作，不可混用。

## 状态措辞

- 已持久化（persisted）：消息或请求已保存。
- 投递中（dispatching）：外部通知调用已开始或即将执行，结果可能未知。
- 宿主已接受（host_accepted）：宿主接口返回成功，不证明父模型已处理。
- 投递失败或结果未知（failed_or_unknown）：不能盲目自动重发。
- 请求等待中 / 已答复 / 已超时（pending / replied / timed_out）：决策请求的状态，不是后台进程状态。
- 迟到答复（late=true）：超时后保存的首个答复，请求仍为 timed_out；不意味着已退出的工具自动恢复。

“父模型已处理”“子模型已读到答复”“运行已完成”分别需要实际行为或状态证据，不统称“已通知”。后台任务超时与决策请求超时也应明确区分。

## 项目标识

| 标识 | 用途 |
| --- | --- |
| agent-link | GitHub 项目、CLI 命令、Codex skill 名称。 |
| agent_link | Python 包。 |
| codex-background-tasks | 独立 GitHub 项目，后台运行器与 Codex 通知桥；不依赖 Agent Link。 |
| background-tasks | 该项目的插件名与 skill 名。 |
| background-tasks@codex-tools | 当前插件安装标识。 |
| background_tasks | MCP 原生面板工具名，不是整个运行器。后台启动工具为 run_cli。 |

## 文档优先级

本表定义词义；[SPEC.md](../SPEC.md) 定义核心语义；[HOST-PROTOCOL.md](HOST-PROTOCOL.md) 定义宿主协议；[USAGE.md](../USAGE.md) 提供操作步骤。历史记录不代表当前实现，实际冲突应核对代码并同步文档，不以术语表推断不存在的功能。
