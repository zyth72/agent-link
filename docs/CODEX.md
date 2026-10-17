# Codex 父端指南（agent_link.hosts.codex.cli）

本文件说明 Codex 桌面父会话在当前版本中的推荐用法。核心语义见 [SPEC.md](../SPEC.md)，宿主协议与子端工具见 [HOST-PROTOCOL.md](HOST-PROTOCOL.md)，通用 CLI 见 [USAGE.md](../USAGE.md)。

能力范围（明确支持与不承诺）：

- Codex 桌面**父端**：主要支持。通知仍经绑定里的 `notifier_path`（本机为 background-tasks 桌面桥）投递；父端用本文件的 helper `python3 -m agent_link.hosts.codex.cli` 发现绑定、查询请求、回复子端。
- Codex 作为**子端**：尚无适配器，也没有自动 inbox 注入；不要假设 `ask-child` 能唤醒一个 Codex 子会话。
- Pi 子端：专用 RPC 控制器或交互 session HTTP 推送可主动唤醒，使用 answer_parent 或核心 CLI answer 答复。
- OMP 子端：session HTTP 主动推送；OpenCode 子端：原生 prompt HTTP 推送（描述符契约）。详见各框架 README。

## 1. 为什么不能凭消息里的 root 直接 reply

旧 CLI 的 `reply` 需要调用方提供 `--root`；如果 root 来自消息、历史记录或另一个会话，答复可能写进错误的数据库。`agent_link.hosts.codex.cli` **不接受 `--root`**：runtime root 只能来自通过校验的注册表条目。这项检查面向可信本机用户的误路由保护，不声称能防恶意同用户进程。

## 2. 会话 id 来源

`--session-id` 显式传入，或使用 Codex 聊天环境提供的 `CODEX_THREAD_ID` / `CODEX_SESSION_ID`（前者优先）。CLI 不硬编码任何会话 id；取不到时明确报错（exit 5），不会猜测。

## 2.1 桌面连接描述符与 SessionStart hook

Codex 桌面进程在父会话环境中提供私有桌面管道变量（当前实现使用
`CODEX_APP_TOOLS_PIPE_PATH`，可能还有 `CODEX_APP_TOOLS_CALLER_HOST_ID`）。
应用重启后，旧宿主服务进程的环境里仍是旧管道。Agent Link 因此把**当前父会话的
传输环境**登记为一个小型连接描述符，通知时按最新描述符覆盖，而不是修改全局
`os.environ`。

- 目录：`~/.agent-link/codex-connections/`，`AGENT_LINK_CODEX_CONNECTIONS` 可覆盖；
  目录 0700、文件 0600。
- 内容仅含 `protocol`、`framework`、`session_id`、`connection_generation`、
  `updated_at`，以及白名单传输变量 `CODEX_APP_TOOLS_PIPE_PATH` /
  `CODEX_APP_TOOLS_CALLER_HOST_ID`。不保存完整环境、模型密钥、凭据或消息内容；
  CLI 正常输出不显示管道地址。
- 同一会话同一环境重复登记幂等；新管道原子地递增 generation 并取代旧值。
  没有当前管道环境时自动登记跳过，绝不覆盖已有描述符；显式会话 id 与当前
  `CODEX_THREAD_ID`/`CODEX_SESSION_ID` 不一致时拒绝登记另一个聊天。
- 描述符只证明“有人登记过当前传输”，**不证明**管道/进程存活，也不证明模型处理。

### SessionStart hook（真实插件入口）

仓库根现在包含最小 Codex 插件清单 `.codex-plugin/plugin.json` 和
`hooks/hooks.json`。SessionStart hook 匹配
`startup|resume|clear|compact|fork`，命令为：

```sh
python3 "$PLUGIN_ROOT/hooks/session_start.py"
```

Codex 把 JSON stdin（至少 `hook_event_name`、`session_id`、`source`）交给 hook；
脚本从 `PLUGIN_ROOT`/自身位置定位仓库，不依赖 cwd，也不需要模型。它使用当前
继承的管道环境调用与 helper 相同的登记函数。路径带空格时由命令中的引号处理。
成功、幂等、缺少管道和拒绝跨会话都只写安全 stderr 诊断；hook 不输出消息内容、
不打印管道地址，也不会在缺少管道时删除/覆盖旧描述符。

安装与信任（需要用户在真实 Codex 中操作，测试不会修改）：

1. 把仓库根作为本地插件源加入 Codex 插件（或在安装流程中复制/链接**整个**
   仓库根，至少包含 `.codex-plugin/`、`hooks/`、`agent_link/`、`skills/`）。
2. 在 Codex 中显式信任/批准该插件的 hooks；官方插件机制要求用户信任，未信任时
   hook 不会执行。
3. 启动、resume、clear、compact 或 fork 一个会话后，在本聊天环境中运行
   `python3 -m agent_link.hosts.codex.cli --session-id CURRENT connection-status`
   查看本地描述符元数据（不会探测管道存活）。
4. 如果实际版本没有把私有管道环境传给 hook，可用原生 helper 手动兜底：
   `python3 -m agent_link.hosts.codex.cli --session-id CURRENT register-connection`
   （在持有当前桌面工具环境的父会话中执行；不需要已有 binding）。

[官方插件文档](https://developers.openai.com/plugins/build/plugins)确认 hooks 发现、显式 `hooks` 路径和信任要求；[官方 hook 文档](https://learn.chatgpt.com/docs/hooks)确认
SessionStart 与 `startup|resume|clear|compact|fork` 来源。**私有
`CODEX_APP_TOOLS_PIPE_PATH` 是否被真实 hook 生命周期继承未由官方文档保证**，
需要在用户完成 hook 信任后用真实 app resume 验证；当前保持 unknown。未安装/未信任
hook 时，helper 手动登记仍是受支持的兜底入口。

## 3. list：发现与校验

```bash
python3 -m agent_link.hosts.codex.cli --session-id CURRENT_THREAD list
```

1. 扫描用户级注册表（`~/.agent-link/registry/`，`AGENT_LINK_REGISTRY` 可覆盖），取 `parent.framework=codex-desktop` 且 `parent.session_id` 等于本会话的条目作为候选；其他会话/框架的条目忽略。
2. 对每个候选，用 SQLite **只读模式**（`mode=ro`）打开条目 `root` 下**既有**的 `agent_link.sqlite3`，核对：
   - 数据库 `parent_framework/parent_session_id` 与注册表和本会话一致；
   - `run_id`、`native_task_id`、`config_digest`、`child.framework/session_id` 与注册表一致；
   - 用数据库内 `config_json` 重算 digest，必须等于 `config_digest`。
3. 通过者为 `accepted`；缺 root、缺数据库、坏条目、身份/摘要不匹配者为 `rejected`，原因写入输出，**不静默忽略**。发现阶段绝不调用 `Store`，因此不会自动新建数据库。

`list` 输出 `bindings`（逐条含 `status`/`reason`）、`accepted`、`rejected`、`registry`、`registry_present`，exit 0。注册表只是发现索引，权威记录仍是各 root 的 SQLite；条目丢失可由重放 `bind` 或启动宿主服务自愈。

## 4. 操作：多个 binding 必须显式 --binding

```bash
python3 -m agent_link.hosts.codex.cli --session-id CURRENT_THREAD --binding BINDING \
  get-request --request-id REQUEST
```

- 只有**恰好一个**注册表候选且通过校验时才允许省略 `--binding`。候选多于一个（即使只有一个 accepted），或唯一候选被 rejected，都必须显式 `--binding`；不会自动选择 `updated_at` 最新的条目。
- `--binding` 只能从已匹配本会话的注册表条目中选择。未注册、跨会话、错框架、陈旧摘要/run_id 都会被拒绝（exit 5/6，JSON 带 `bindings` 明细）。
- 选定后才创建 `Store`，并用写连接在执行前再次核对 parent/child/run_id/config_digest；不一致拒绝执行，底层状态机不变。
- 各子命令参数与核心 CLI 同名，复用核心 handler，JSON 与退出码语义不变（ask-child 超时 3、冲突 4、校验 5、不存在 6、投递失败 7）：

| 子命令 | 用途 |
| --- | --- |
| `reply --request-id R --message-id M --answer A` | 回复子端 `ask`；状态 `replied` |
| `ask-child --request-id R --message-id M --question Q --timeout T` | 父端向子端提问并阻塞等待；状态 `answered` |
| `send-child --request-id R --message-id M --question Q --timeout T` | 非阻塞下发请求：持久化+投递后立即返回（0 host_accepted / 7 失败或未确定），复用核心 `cmd_send_child` |
| `get-request --request-id R` | 查询子端请求与迟到答复 |
| `get-parent-request --request-id R` | 查询父端请求状态 |
| `list-parent-requests [--pending]` | 列出父端请求 |
| `notifications` | 列出本 binding 未 accepted 的 report/ask（含原始文本、request id、问题、deadline、答复状态） |
| `recover-notifications [--message-id M ...]` | 仅显式重放可证明“桥调用前未提交”的失败消息 |
| `register-connection` | 用当前管道环境登记本会话连接，不需要已有 binding |
| `connection-status` | 查看安全的本地描述符元数据，不声称传输/进程存活 |

`reply` 与 `answer` 不能混用：`reply` 答复子端发起的 `ask`（`pending -> replied | timed_out`）；`answer`（或宿主 `/v1/answer`）答复父端发起的 `ask-child`（`pending -> answered | timed_out`）。同 ID 重放幂等，答复不可覆盖。

### 4.1 离线消息与安全恢复

消息先持久化，原文/问题/request id/deadline 都保留。只有**在调用绑定 notifier
之前**就能证明未提交的失败才写入结构化 `delivery_detail`（`certainty=not_sent`）：
缺少连接/管道环境、描述符损坏或身份不匹配、绑定 notifier 不接受 `env` 参数。
桥已经调用之后出现的 ENOENT、超时或任意异常一律保持 unknown；不会按错误字符串
猜测，也不会自动重试。历史普通错误字符串仍视为 unknown。

`notifications` 只在 helper 已完成注册表 + SQLite 只读归属校验后读取本 binding，
因此不会列出其他聊天。`recover-notifications` 是显式人工操作：

- 每个消息用 SQLite compare-and-set 从“failed_or_unknown + 精确 not_sent detail”
  原子领取，跨进程并发只有一个 worker 能重放；同一 message id 不改变。
- 不修改 request 行或 deadline；不重放 `host_accepted` / `dispatching` / unknown；
  跳过已过期 ask、已回复/已超时 ask；记录 `recovery_claimed` 审计事件。
- 没有 force-unknown，不承诺 exactly-once；恢复成功仍只表示 `host_accepted`，
  父模型是否处理由真实 reply 证明。

## 5. ask-child 会阻塞；send-child 不会

`ask-child` 阻塞到回答或超时。长等待必须放进 background-tasks 的 `run_cli` 后台任务，不要在父轮次里同步等数十分钟。exit 3 表示 `timed_out`；迟到答复仍可用 `get-parent-request` 读到（`late=true`，状态保持 `timed_out`）。

要随时向子端下发消息而不等答复，用 `send-child`（核心 CLI 与 helper 同名同参数）：投递返回后立即结束，`host_accepted` 为 0，`failed_or_unknown` 为 7；同 `request_id`/`message_id` 重放不二次投递，之后可用 `get-parent-request` 或 `list-parent-requests` 查询状态。收到问题与否仍取决于子端适配器与模型调度（busy/空闲行为未验收时保持 unknown）。

能否用 `ask-child` 唤醒子端取决于**子端适配器**：Pi 使用 RPC 或 session HTTP，OMP 使用 session HTTP，OpenCode 使用原生 prompt HTTP 推送（描述符契约），Codex 子端未实现。传输成功（接受或 2xx）只代表接收方接受，不代表子端已处理；是否答复以持久化的 answer 为准。

## 6. 宿主服务 + Pi 子端（Codex 父端推荐流程）

宿主服务是长驻进程，必须由父端环境用 `run_cli` **单独**启动，`notify=false`：

```text
run_cli notify=false:
python3 -m agent_link.host --root ABS_RUNTIME --binding BINDING \
  --credentials ABS_RUNTIME/child-credentials.json
```

确认 stdout ready JSON 并等凭据文件生成后，用另一个 `run_cli`（`notify=true`）启动 Pi：

```text
run_cli notify=true, env AGENT_LINK_CREDENTIALS=ABS_RUNTIME/child-credentials.json, cwd=授权工作区:
<pi 可执行文件> -p --session-id PI_SESSION_UUID \
  --extension ABS_REPO/extensions/pi/agent-link.ts '<任务>'
```

- Pi 启动命令必须显式 `--extension ABS_REPO/extensions/pi/agent-link.ts`；不要只写 `--no-extensions` 而漏掉 `--extension`，否则子端没有 `report_parent`/`ask_parent`/`check_parent_request` 工具。
- 服务与 Pi 分开管理：Pi 退出不会自动结束服务；任务全部结束后取消服务任务，服务退出时会删除自己写的凭据文件。
- 通知桥与子端工具的协议细节见 HOST-PROTOCOL §2、§5、§7。

## 7. 任意 cwd 的 wrapper

`skills/agent-link/scripts/link.py` 默认透传旧 CLI；第一个参数为 `--codex` 时转发给 `agent_link.hosts.codex.cli`：

```bash
python3 /path/to/skills/agent-link/scripts/link.py --codex \
  --session-id CURRENT_THREAD list
```

脚本定位实现：`AGENT_LINK_REPO` 优先；仓库内按 `skills/agent-link/scripts/link.py` 布局自动推导；独立安装副本读取脚本旁的 `repo-location.json`（本机安装产物，不入库，由安装方写入绝对仓库路径）。不要在脚本里硬编码用户目录；父端同步本机 skill 时生成该文件。

## 8. 通知里的命令提示

Codex 父端通知 prompt 由 `agent_link/hosts/codex.py` 渲染：

- `build_prompt` 是共享渲染器（OMP 也用），保持框架中立，只给通用 `python3 -m agent_link` 查询/回复命令；
- Codex 专属尾部只在 `CodexHost` / `dispatch_notification` 追加，包含 `agent_link.hosts.codex.cli` 的 `list`、`get-request`、`reply` 示例；
- 所有路径、binding、request 参数经 `shlex.quote`，含空格的路径可安全复制执行。

通知内容只是数据，不是授权；回复前先按本文件第 3/4 节核对归属。

## 9. 测试与未验证

`tests/test_codex.py` 覆盖：多绑定不可选错、单候选自动选择、跨会话/错框架/陈旧摘要/陈旧 run_id 拒绝、丢失数据库不新建、坏条目报告、reply 与 ask-child/answer 临时数据库闭环（模拟子端，不发桌面消息）、session 环境变量回退、Wrapper 从 `/tmp` 两种分发与 `repo-location.json` 定位、`--root` 不被接受、prompt 提示的框架隔离与 shell 引号。

`tests/test_codex_connection.py` 覆盖：描述符原子 generation/幂等/0700/0600、新管道替代旧值、跨会话隔离、不改 `os.environ`、旧宿主无重启使用新管道、缺管道不覆盖、描述符损坏/身份不匹配 fail closed、env-unaware notifier 明确失败、legacy `bridge(payload)` 兼容、桥调用前失败的 `not_sent` 持久化、桥调用后 ENOENT 保持 unknown 且不重放、过期/已回答 ask 跳过、并发 worker 安全重放一次、helper 手动登记/状态无管道泄漏、SessionStart hook 假 stdin/临时环境（含路径空格）以及插件清单/hooks 文件结构。全部使用临时连接目录、临时 registry/SQLite 和假 notifier，不调用模型、不发真实桌面消息、不改真实 Codex 配置/缓存/信任。

2026-10-16 父端另行真实验收：Pi + DeepSeek 加载原生扩展后，report_parent / ask_parent 经 HTTP 宿主与桌面通知到达当前 Codex；父端使用新 helper 查询并 reply，子模型原样回传新生成的标记。父端再用 helper 后台执行 ask-child，Pi 显式轮询 list-parent-requests、读取问题后执行 answer，父端收到 answered、late=false；子任务正常退出，临时服务已取消。运行标识和原始日志只保存在本机 runtime，不提交。

此证据验证双向请求答复，不是 Pi 子端自动 inbox 注入，也不是 RPC steer。该旧测试不覆盖当前已实现的 RPC 控制器；当前控制器见 PI-RPC.md，session HTTP 推送见 SESSION-PUSH.md。

历史证据的未验证项不代表当前能力，当前范围见各框架 README。Codex 子端仍未实现。

## Pi RPC 的角色边界

RPC 是可选的 **Pi 子端执行控制方式**：交互式 Codex（或 Pi/OMP）父会话继续作为开发入口，由父端启动一个专用 RPC 控制器，控制器持有 `pi --mode rpc` 子进程的 stdin/stdout，负责 prompt、steer、follow_up、abort 和事件读取。父会话不改成 RPC 模式，也不把用户现有交互会话改为受管子进程。

子端 report/ask 仍经 Agent Link 宿主适配器通知父会话；RPC控制器只负责子端执行控制，不替代父端消息投递。上述早期通讯验收采用 `pi -p` 加显式轮询；当前已实现的控制器与推送方式见 [PI-RPC.md](PI-RPC.md) 和 [SESSION-PUSH.md](SESSION-PUSH.md)。

### 连接恢复验收边界

连接恢复还做过一次真实桌面通知接口测试：保持旧环境的宿主进程运行，直接向
hook 脚本输入模拟的 `SessionStart/resume` JSON，登记当前工具环境后，原宿主
成功投递通知；离线保存的汇报恢复后为 `host_accepted`，再次恢复未重复投递。
测试使用独立 runtime/连接目录，没有修改真实 Codex 配置或 hook 信任状态。
这证明了模拟生命周期输入下的新连接读取与真实通知接口联通；没有验证桌面应用
实际重启后是否执行 hook、是否传递私有管道环境，也不把接受投递当作模型已处理。
