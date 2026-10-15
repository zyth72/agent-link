# agent_link 使用说明（SPEC v0.1）

`agent_link` 是跨框架任务通信薄层：父/子会话绑定、运行中进度汇报（report）、决策请求与反向回复（ask/reply）。生命周期仍由各框架自己的后台任务机制管理，本层不实现进程运行器、不要求 broker。

- Python 标准库实现，无第三方运行依赖。
- 存储为单个 SQLite 数据库，支持多个 CLI 进程并发。
- v0.1 只支持同一 OS 用户的同机可信场景，不承诺恶意同用户隔离。

## 框架能力与启动

具体框架的启动命令、模型配置、就绪检查、已有会话接入和清理见 [框架接入指南](agent_link/hosts/README.md)。各框架目录 README 区分原生能力与已实现适配能力。

## 快速开始

```bash
cd /path/to/agent-intercom-workspace

# 1. 绑定（--root 必须放在子命令之前）
python3 -m agent_link --root /abs/runtime bind --config /abs/live-config.json

# 2. 子端主动汇报，不等待
python3 -m agent_link --root /abs/runtime report \
  --binding live-001 --message-id progress-001 --text "实现完成 60%"

# 3. 子端请求决策并阻塞等待，超时退出非零（3）
python3 -m agent_link --root /abs/runtime ask \
  --binding live-001 --request-id decision-001 --message-id question-001 \
  --question "请选择方案 A 或 B" --timeout 240

# 4. 父端回复（由父 agent 执行，不能覆盖已有答复）
python3 -m agent_link --root /abs/runtime reply \
  --binding live-001 --request-id decision-001 \
  --message-id codex-reply-001 --answer "选择 A，验收标记 XYZ"

# 5. 查询请求持久化状态
python3 -m agent_link --root /abs/runtime get-request \
  --binding live-001 --request-id decision-001
```

## 命令

| 命令 | 说明 | 退出码 |
| --- | --- | --- |
| `bind --config FILE` | 创建绑定；同配置重复绑定幂等，不同配置冲突 | 0 / 4 / 5 |
| `capabilities` | 输出能力 JSON（七个声明字段 + 适配器明细） | 0 |
| `report ...` | 持久化并通知一条进度；重复提交不重投 | 0 / 4 / 5 / 7 |
| `ask ...` | 先持久化请求，再通知父端，然后阻塞等待答复 | 0 / 3 / 4 / 5 / 7 |
| `reply ...` | 关联已存在请求写入答复；不允许覆盖 | 0 / 4 / 5 / 6 |
| `get-request ...` | 返回请求状态与答复 | 0 / 5 / 6 |
| `ask-child ...` | 父端请求子端答复并阻塞等待（按框架使用 Pi RPC、session HTTP 或 OpenCode inbox） | 0 / 3 / 4 / 5 / 6 / 7 |
| `answer ...` | 子端答复父端请求；不允许覆盖 | 0 / 4 / 5 / 6 |
| `get-parent-request ...` | 返回父端请求状态与答复 | 0 / 5 / 6 |
| `list-parent-requests --binding ID [--pending]` | 列出父端请求 | 0 / 6 |
| `list-bindings [--parent-session-id ID] [--child-session-id ID] [--parent-framework F] [--child-framework F]` | 列出用户级绑定索引条目（注册表）；`--root` 接受但不使用 | 0 |

所有命令 `--root ABS_DIR` 放在子命令前；stdout 恰好一条 JSON 结果，诊断写 stderr；错误非零。

### 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功（ask 只有 `status=replied` 才是 0） |
| 1 | 未预期内部错误 |
| 2 | 命令行用法错误（argparse） |
| 3 | ask 请求超时（JSON 仍含 request_id/answer/late/status） |
| 4 | 冲突：绑定重定向、同 ID 不同内容、重复回复覆盖 |
| 5 | 参数/配置校验失败（ID、timeout、notifier 路径等） |
| 6 | 绑定或请求不存在 |
| 7 | 通知投递 `failed_or_unknown` 或未确定（不自动重试） |

## bind 配置

```json
{
  "binding_id": "live-001",
  "run_id": "implementation-live-001",
  "native_task_id": null,
  "parent": {"framework": "codex-desktop", "session_id": "PARENT_SESSION_ID"},
  "child": {"framework": "pi", "session_id": "implementation-pi-001"},
  "notifier_path": "/abs/path/to/notifier.py"
}
```

- `binding_id`、`run_id`、`native_task_id`、`parent.framework/session_id`、`child.framework/session_id` 只允许 `[A-Za-z0-9_-]{1,128}`；`native_task_id` 可为 `null`。
- `notifier_path` 必须是绑定时已存在的本机文件，且暴露 `bridge(payload)`；适配器只加载该文件，路径在 bind 时固定，之后不可重定向。
- 通知 payload 为 `{"thread_id": parent.session_id, "prompt": "<构造的文本>"}`；消息内容只作为数据处理，不作为代码执行。

## 标识符与幂等

- `binding_id`：同配置重复 bind 幂等；不同配置返回冲突（exit 4）。
- `message_id`：同 ID 同内容同类型重复提交幂等且**不会重复投递**；同 ID 不同内容/类型返回冲突。
- `request_id`：`ask` 先持久化再通知；同一请求再次 `ask` 只等待已有状态，不重投。重复 `ask` 的 `question` 与 `timeout` 必须与原请求一致，否则返回冲突（exit 4）；`timeout` 必须为正有限数，否则校验失败（exit 5）。
- `reply`：必须关联已存在请求；已答复后不同 `message_id` 再回复返回冲突；完全相同回复幂等。回复的消息 ID 绑定目标请求：同一 `message_id` 回复不同请求返回冲突（写事务内检查，两个独立进程并发也只允许一个成功）。
- 所有标识符禁止路径穿越；运行目录权限 0700，数据库 0600。

## 状态模型

投递状态（messages.delivery_state）：

`persisted -> dispatching -> host_accepted | failed_or_unknown`

- 外部调用前先持久化 `dispatching`；异常/中断记 `failed_or_unknown`。
- `host_accepted` 只表示桌面桥接受了宿主接口调用，**不代表模型已处理**；模型是否处理由父端实际答复证明。
- v0.1 不自动重试，重复 CLI 调用也不重投。

请求状态（requests.status）：

`pending -> replied | timed_out`

- 截止时间持久化为 UTC epoch；等待进程用单调时钟控制等待时长，并记录墙钟跳变事件。等待期限由持久化截止时间映射到单调时钟，通知耗时不会被再次扣减；重复 `ask` 只等待原截止时间的剩余量。
- 迟到判定：当前墙钟 `>= deadline` 即按超时处理；请求已 `timed_out` 时答复一律 `late=true`，包括墙钟回拨到 deadline 之前的情况；即使等待进程尚未更新状态，迟到回复也不会被当作及时回复。
- 截止时间后到达的首个答复仍保存（`late=true`），状态保持 `timed_out`。
- `timed_out` 之后不允许再写入第二个不同的迟到答复（冲突）。
- 请求、答复和审计事件（events 表）保留到显式人工清理，不自动删除。

父端请求（`parent_requests`，父端 `ask-child` 发起、子端 `answer` 答复）语义与子端请求完全镜像：`pending -> answered | timed_out`，幂等/冲突/迟到答复（`late=true`）规则相同；父端请求的投递状态写在请求行上——子端 inbox 事件写入成功即 `host_accepted`，不代表子端已处理，是否处理由持久化的答复证明。

## 能力声明

`capabilities` 输出本适配方式的声明，不推断框架全部原生能力：

```json
{
  "notification_transport": "desktop_bridge",
  "receive_mode": "host_injection",
  "reply_mode": "blocking_tool_file_poll",
  "busy_behavior": "unknown",
  "wake_after_turn": "unknown",
  "spawn": false,
  "cancel": false
}
```

## 宿主服务与 Pi 子端（可选）

旧 CLI 的父端直接 `reply` 已足够用；当子端是运行中的 Pi 会话、需要原生工具时，父端可在**父端环境**额外启动一个 binding 级 loopback 服务（前置：binding 已由 `bind` 创建）：

```bash
# 父端（必须）：绑定 127.0.0.1 临时端口，输出 ready JSON 与 0600 凭据文件
python3 -m agent_link.host --root /abs/runtime --binding live-001 \
  --credentials /abs/runtime/child-credentials.json

# 子端（Pi）：只需凭据文件，不需要 Codex/background-tasks/桌面依赖
AGENT_LINK_CREDENTIALS=/abs/runtime/child-credentials.json \
  pi -p --no-extensions --extension /abs/repo/extensions/pi/agent-link.ts '<任务>'
```

- 子端工具：`report_parent`、`ask_parent`、`check_parent_request`；只能访问本 binding 的 `report`/`ask`/`get-request`/`capabilities`，不能 `bind`、`reply`、指定父会话。
- 父端仍用旧 `reply` CLI 回答同一 SQLite 数据库；`ask` 工具带回真实答复，超时返回 `timed_out` 而不是伪造答案。
- HTTP 层限制：body 1..65536 字节、读取超时 10s、ask timeout 必须 `0 < t <= 3600`（核心 CLI 不受 3600 限制）。子端断开不会取消等待或删除请求；迟到答复保存为 `late=true`，仍可用 `get-request` 读取。
- 协议与错误码、HostDriver 扩展方式、`--driver MODULE:OBJECT` 用法见 [docs/HOST-PROTOCOL.md](docs/HOST-PROTOCOL.md)。Codex 驱动真实依赖 background-tasks 桌面桥，因此服务只能在父端环境启动；子端不得自行启动服务。

## OMP 父端/子端（可选）

OMP 可同时作为父端与子端；两侧均使用 session HTTP 主动推送，无需桥文件或定时扫描。完整命令与模型配置见 [OMP README](agent_link/hosts/omp/README.md)，协议见 [SESSION-PUSH](docs/SESSION-PUSH.md)。

```bash
export AGENT_LINK_SESSIONS=/abs/private/session-connections
omp --extension /abs/repo/extensions/omp/agent-link.ts
# 使用实际 session id 创建 binding，再由父端运行器启动：
python3 -m agent_link.host --root /abs/runtime --binding live-001 \
  --credentials /abs/runtime/child-credentials.json --framework omp
```

子端启动时额外设置 AGENT_LINK_CREDENTIALS；发送方与接收方使用同一连接目录。session_start 注册、session_shutdown 清理，已在线会话在创建 Binding 后立即可投递，无注册表重扫。认证/会话不匹配、缺连接或失效时明确失败，无文件回退或自动重投。ask-child 的答复使用 answer_parent；HTTP 接受不证明模型处理。

## OpenCode 父端/子端（可选）

OpenCode 用 v2 插件 `extensions/opencode/agent-link.ts`（本机验证 2.0.29；v1 插件形态会被拒绝加载）：把文件放入 `~/.config/opencode/plugin/` 后每个会话自动加载。子端五个工具与 OMP 同名同语义；watcher 复用共享模块，同样支持注册表自动认领。父端宿主服务用 `--driver agent_link.hosts.opencode.parent:OpenCodeParentAdapter`（文件桥与 OMP 相同）。已知限制（server 级多会话归属近似等）见 [docs/HOST-PROTOCOL.md](docs/HOST-PROTOCOL.md) §7 与插件文件头注释。

## Codex 父端助手（agent_link.hosts.codex.cli，推荐）

Codex 桌面父端不要把消息里的 `--root` 当作回复目标。新 helper 只用用户级注册表（`~/.agent-link/registry/`）发现绑定，并用 SQLite **只读模式**打开 root 下既有数据库，核对 parent 会话归属与 run_id/config_digest 等标识后才接受：

```bash
# 会话 id 显式传入，或使用聊天环境里的 CODEX_THREAD_ID / CODEX_SESSION_ID
python3 -m agent_link.hosts.codex.cli --session-id CURRENT_THREAD list

# 多个 binding 必须显式 --binding（不会自动选最新）
python3 -m agent_link.hosts.codex.cli --session-id CURRENT_THREAD --binding BINDING \
  reply --request-id R --message-id M --answer 'A'

python3 -m agent_link.hosts.codex.cli --session-id CURRENT_THREAD --binding BINDING \
  ask-child --request-id R --message-id M --question Q --timeout 240
python3 -m agent_link.hosts.codex.cli --session-id CURRENT_THREAD --binding BINDING \
  get-request --request-id R
python3 -m agent_link.hosts.codex.cli --session-id CURRENT_THREAD --binding BINDING \
  list-parent-requests --pending
```

- 支持 `list`、`reply`、`ask-child`、`get-request`、`get-parent-request`、`list-parent-requests`；参数与核心 CLI 同名，复用同一 handler，JSON 与退出码不变。
- **不接受 `--root`**；坏条目/缺 root/缺数据库在 `list` 中作为 `rejected` 带原因返回，不自动建库，也不会把消息里的 root 当成执行目标。
- 仅当恰好一个候选通过校验时才能省略 `--binding`；多条匹配必须显式选择。
- `ask-child` 阻塞；长等待放进 background-tasks 后台任务。能否唤醒子端取决于子端适配器（Pi 使用 RPC 或 session HTTP；OMP 使用 session HTTP；OpenCode 保留 inbox watcher；Codex 子端未实现）。
- wrapper：`python3 /abs/repo/skills/agent-link/scripts/link.py --codex --session-id CURRENT_THREAD list` 可从任意 cwd 调用；独立安装副本用脚本旁的 `repo-location.json` 定位仓库。

完整流程、校验规则与宿主服务启动步骤见 [docs/CODEX.md](docs/CODEX.md)。

## 测试

```bash
cd /path/to/agent-intercom-workspace
python3 -W error::ResourceWarning -m unittest discover -s tests -t . -v
```

自动测试不调用模型或改真实 Codex 配置。核心覆盖状态、幂等、冲突、超时与迟到答复；Pi/OMP 推送覆盖本地 HTTP 认证、身份校验、上限、错误、生命周期与注入；OpenCode 文件 watcher 另有覆盖。真实模型验收单独记录在 [SESSION-PUSH](docs/SESSION-PUSH.md) 与 [PI-RPC](docs/PI-RPC.md)，不把模拟上下文当作模型行为证据。

## 已知限制

- 单机、同一 OS 可信用户；不防范恶意同用户进程。
- 依赖父端桌面消息管道存活；WSL→Windows 通知需要 `CODEX_APP_TOOLS_PIPE_PATH` 等环境变量（由 Codex 桌面会话注入）。宿主服务也会加载同一 binding 的 notifier，因此必须由父端环境启动。
- loopback 宿主服务只承诺同机、单 binding/单 token、子端不能 bind/reply；不承诺跨机器、MCP、Intercom 兼容或恶意同用户隔离。
- 文件/数据库轮询有延迟（默认 0.1s），不承诺应用重启后通知可用，不实现 exactly-once 外部投递。
- 墙钟回拨可能影响迟到判定；已记录 `wall_clock_jump_observed` 审计事件，但无法消除该限制。
- 不实现 spawn/cancel；不绑定未启动的任务；不实现自动清理。
- 本轮未用新宿主服务发送真实桌面通知，也未做 LLM 端到端工具调用（只验证扩展真实加载与工具执行）；OMP 侧已实测真实会话注入（空闲 `--mode rpc` 会话收到桥事件后启动一轮，模型实际回答；锁与 cursor 由真实 1s 轮询推进/释放），忙碌会话注入与轮次结束唤醒仍未验证。

## Codex 后台运行配套

推荐先安装 [codex-background-tasks](https://github.com/zyth72/codex-background-tasks#安装)（MIT），重载后使用 `run_cli` 启动宿主和 Pi。父端发现、查询与回复推荐使用 `python3 -m agent_link.hosts.codex.cli`（见 [docs/CODEX.md](docs/CODEX.md)），不要凭消息里的 root 直接 `reply`。宿主服务单独运行、`notify=false`；Pi 任务 `notify=true`，必须显式 `--extension /abs/repo/extensions/pi/agent-link.ts`，继承父环境并设置子端凭据路径。等待服务凭据就绪再启动 Pi，结束后关闭服务。完整生命周期见 [宿主规范](docs/HOST-PROTOCOL.md#5-codex-驱动的真实依赖)。
