# agent_link 使用说明（SPEC v0.1）

`agent_link` 是跨框架任务通信薄层：父/子会话绑定、运行中进度汇报（report）、决策请求与反向回复（ask/reply）。生命周期仍由各框架自己的后台任务机制管理，本层不实现进程运行器、不要求 broker。

- Python 标准库实现，无第三方运行依赖。
- 存储为单个 SQLite 数据库，支持多个 CLI 进程并发。
- v0.1 只支持同一 OS 用户的同机可信场景，不承诺恶意同用户隔离。

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

## 测试

```bash
cd /path/to/agent-intercom-workspace
python3 -W error::ResourceWarning -m unittest discover -s tests -t . -v
```

测试使用模拟 notifier/driver（不发真实桌面消息）；Pi 扩展测试用 jiti 加载真实扩展、以真实 `agent_link.host` + notifier stub（走旧 CLI reply）做 loopback 闭环，并用 `pi --help` 探针扩展验证 Pi 真实加载（不调用模型）。覆盖：绑定不可重定向、ID 校验、幂等/冲突、重复回复保护、未知请求、超时与迟到回复、数据库重开恢复、两个独立进程并发冲突、模拟 notifier 失败后不重投；复审补充：慢通知不缩短真实截止时间、跨请求复用回复消息 ID 冲突（含并发）、timeout 变更冲突与非法 timeout 拒绝、deadline 边界（>=）与墙钟回拨下的 `late` 判定；宿主服务：认证/统一 JSON 错误/未知字段/方法/路径、body 与 timeout 上限、并发同消息单次投递、驱动失败不重投、跨 binding 隔离、服务重启重放不重投、请求体读超时 408、凭据不覆盖与只删自己的。

## 已知限制

- 单机、同一 OS 可信用户；不防范恶意同用户进程。
- 依赖父端桌面消息管道存活；WSL→Windows 通知需要 `CODEX_APP_TOOLS_PIPE_PATH` 等环境变量（由 Codex 桌面会话注入）。宿主服务也会加载同一 binding 的 notifier，因此必须由父端环境启动。
- loopback 宿主服务只承诺同机、单 binding/单 token、子端不能 bind/reply；不承诺跨机器、MCP、Intercom 兼容或恶意同用户隔离。
- 文件/数据库轮询有延迟（默认 0.1s），不承诺应用重启后通知可用，不实现 exactly-once 外部投递。
- 墙钟回拨可能影响迟到判定；已记录 `wall_clock_jump_observed` 审计事件，但无法消除该限制。
- 不实现 spawn/cancel；不绑定未启动的任务；不实现自动清理。
- 本轮未用新宿主服务发送真实桌面通知，也未做 LLM 端到端工具调用（只验证扩展真实加载与工具执行）。

## Codex 后台运行配套

推荐先安装 [codex-background-tasks](https://github.com/zyth72/codex-background-tasks#安装)（MIT），重载后使用 `run_cli` 启动宿主和 Pi。宿主服务单独运行、`notify=false`；Pi 任务 `notify=true`，继承父环境并设置子端凭据路径。等待服务凭据就绪再启动 Pi，结束后关闭服务。完整生命周期见 [宿主规范](docs/HOST-PROTOCOL.md#5-codex-驱动的真实依赖)。
