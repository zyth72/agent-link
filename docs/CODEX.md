# Codex 父端指南（agent_link.hosts.codex.cli）

本文件说明 Codex 桌面父会话在当前版本中的推荐用法。核心语义见 [SPEC.md](../SPEC.md)，宿主协议与子端工具见 [HOST-PROTOCOL.md](HOST-PROTOCOL.md)，通用 CLI 见 [USAGE.md](../USAGE.md)。

能力范围（明确支持与不承诺）：

- Codex 桌面**父端**：主要支持。通知仍经绑定里的 `notifier_path`（本机为 background-tasks 桌面桥）投递；父端用本文件的 helper `python3 -m agent_link.hosts.codex.cli` 发现绑定、查询请求、回复子端。
- Codex 作为**子端**：尚无适配器，也没有自动 inbox 注入；不要假设 `ask-child` 能唤醒一个 Codex 子会话。
- Pi 子端：当前只有父端 watcher，没有子端 inbox watcher；`ask-child` 不会自动唤醒 Pi，需要子端进程用 CLI `answer` 答复。
- OMP / OpenCode 子端：有子端 inbox watcher，`ask-child` 可唤醒（见 HOST-PROTOCOL §6.2、§7）。

## 1. 为什么不能凭消息里的 root 直接 reply

旧 CLI 的 `reply` 需要调用方提供 `--root`；如果 root 来自消息、历史记录或另一个会话，答复可能写进错误的数据库。`agent_link.hosts.codex.cli` **不接受 `--root`**：runtime root 只能来自通过校验的注册表条目。这项检查面向可信本机用户的误路由保护，不声称能防恶意同用户进程。

## 2. 会话 id 来源

`--session-id` 显式传入，或使用 Codex 聊天环境提供的 `CODEX_THREAD_ID` / `CODEX_SESSION_ID`（前者优先）。CLI 不硬编码任何会话 id；取不到时明确报错（exit 5），不会猜测。

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
| `get-request --request-id R` | 查询子端请求与迟到答复 |
| `get-parent-request --request-id R` | 查询父端请求状态 |
| `list-parent-requests [--pending]` | 列出父端请求 |

`reply` 与 `answer` 不能混用：`reply` 答复子端发起的 `ask`（`pending -> replied | timed_out`）；`answer`（或宿主 `/v1/answer`）答复父端发起的 `ask-child`（`pending -> answered | timed_out`）。同 ID 重放幂等，答复不可覆盖。

## 5. ask-child 会阻塞

`ask-child` 阻塞到回答或超时。长等待必须放进 background-tasks 的 `run_cli` 后台任务，不要在父轮次里同步等数十分钟。exit 3 表示 `timed_out`；迟到答复仍可用 `get-parent-request` 读到（`late=true`，状态保持 `timed_out`）。

能否用 `ask-child` 唤醒子端取决于**子端适配器**：OMP/OpenCode 有 inbox watcher；Pi 和 Codex 子端目前没有。写入 inbox 成功只代表事件可读（`host_accepted`），不代表子端已处理；是否答复以持久化的 answer 为准。

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

2026-10-16 父端另行真实验收：Pi + DeepSeek 加载原生扩展后，report_parent / ask_parent 经 HTTP 宿主与桌面通知到达当前 Codex；父端使用新 helper 查询并 reply，子模型原样回传新生成的标记。父端再用 helper 后台执行 ask-child，Pi 显式轮询 list-parent-requests、读取问题后执行 answer，父端收到 answered、late=false；子任务正常退出，临时服务已取消。运行标识和原始日志只保存在本机 runtime，不提交。

此证据验证双向请求答复，不是 Pi 子端自动 inbox 注入，也不是 RPC steer。Pi 自身支持 RPC steer / follow_up，但当前运行方式没有接入该控制通道。

未验证：Codex 作为子端、Pi 子端 inbox 自动唤醒、忙碌期间的自动注入及父轮次结束后的唤醒。

## Pi RPC 的角色边界

RPC 是可选的 **Pi 子端执行控制方式**：交互式 Codex（或 Pi/OMP）父会话继续作为开发入口，由父端启动一个专用 RPC 控制器，控制器持有 `pi --mode rpc` 子进程的 stdin/stdout，负责 prompt、steer、follow_up、abort 和事件读取。父会话不改成 RPC 模式，也不把用户现有交互会话改为受管子进程。

子端 report/ask 仍经 Agent Link 宿主适配器通知父会话；RPC控制器只负责子端执行控制，不替代父端消息投递。当前尚未实现这个控制器，本轮通讯验收采用 `pi -p` 加显式轮询。
