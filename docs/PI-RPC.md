# Pi RPC 子端控制器规范（PI-RPC v1）

本文件定义 Pi 作为**子端**时的 RPC 接入：父端通过本机 loopback 控制通道把消息送进一个专用 Pi 进程，不写文件收件箱、不轮询。父端交互式会话继续由 `extensions/pi/agent-link.ts` 注册的 session HTTP 主动推送注入（见 [SESSION-PUSH](SESSION-PUSH.md)），不改 RPC。

- 传输：控制器持有 `pi --mode rpc` 子进程的 stdin/stdout；父端 CLI（`ask-child`/`send-child`）或 Codex helper 通过 loopback HTTP + 独立随机 token 调用控制器。
- 只支持同一台机器、同一可信 OS 用户；不承诺跨机、MCP 或防恶意同用户进程。
- `host_accepted` 只表示 RPC 命令被 Pi 接受（`success: true` 的 response），不表示模型已处理、更不表示完成。

## 1. 组件与生命周期

```text
父端交互会话（session HTTP 推送，不改）
        │  report/ask 上行（现有 host 服务）
父端 CLI ── HTTP ──> Pi RPC 控制器 ── stdin/stdout JSONL ──> 专用 Pi 进程（--mode rpc）
   │                                                        └─ extensions/pi/agent-link.ts
   └── ask-child/send-child ── HTTP ──> 控制器（prompt/steer/follow-up）
```

1. 父端先用核心 CLI `bind` 创建 binding（`child.framework=pi`、`child.session_id` 即要使用的 Pi session id），再启动父端宿主服务写子端凭据。
2. 父端启动控制器：

   ```bash
   python3 -m agent_link.hosts.pi.rpc --root ABS_ROOT --binding BINDING_ID serve \
     --pi-bin ABS_PI --cwd ABS_WORK --credentials ABS_CREDENTIALS \
     --extension ABS_REPO/extensions/pi/agent-link.ts \
     [--provider PROVIDER] [--model MODEL] \
     [--rpc-timeout 30] [--stop-grace 10]
   ```

   控制器启动专用 Pi：`pi --mode rpc --session-id <child.session_id> --extension <扩展> --no-extensions --no-skills --no-context-files`（轻量禁用其他 extensions/skills/context；显式 `--extension` 仍加载）。子进程环境设置 `AGENT_LINK_CREDENTIALS=<credentials>`，并防御性移除遗留的 `AGENT_LINK_BRIDGE_FILE`/`AGENT_LINK_BINDING`（桥传输已删除，不再有消费者）。
3. `ready` 只在真实 `get_state` response 返回且 `data.sessionId == child.session_id` 后才输出；输出的 JSON 含 descriptor 路径与 endpoint，不含 token。
4. 控制器长驻：`agent_settled` 只表示 Pi 变空闲，不是进程退出；控制器不因 agent 空闲而退出。
5. `stop` 优雅关闭 stdin，等待进程退出；超时则对 Pi 进程组（`start_new_session`）SIGTERM，再超时 SIGKILL。控制器自身的 SIGTERM/SIGINT 走同一清理路径，不留孤儿进程。清理只删除仍属于本进程的 descriptor。

## 2. Descriptor（控制 endpoint，不是收件箱）

路径 `<root>/pi-rpc/<binding_id>.json`，目录 0700、文件 0600，`O_CREAT|O_EXCL` 创建（重复 `serve` 明确失败，不覆盖）。字段：

```json
{
  "protocol": "agent-link-pi-rpc/1",
  "state": "starting|ready",
  "binding_id": "B",
  "run_id": "R",
  "child_session_id": "S",
  "endpoint": "http://127.0.0.1:PORT",
  "token": "<随机 256-bit>",
  "pid": 1234,
  "pi_pid": 2345,
  "pi_session_id": "S",
  "created_at": "...",
  "updated_at": "..."
}
```

- `state=starting` 先占位；握手成功并验证 identity 后原子替换为 `ready`。客户端拒绝非 `ready`。
- token 不打印、不写日志；CLI 输出只给 descriptor 路径与 endpoint。
- descriptor 只描述控制通道；父→子不通过写文件唤醒（没有 RPC descriptor 时可连接交互 Pi 的 session HTTP 接口）。

## 3. 控制 HTTP

- 仅 `POST http://127.0.0.1:<port>/v1/<operation>`，Bearer token 认证；无 GET。
- 请求体必须是 JSON 对象且字段恰好匹配；body 1..65536 字节，读超时 10 秒；所有错误为 JSON。
- 响应统一 `{"result": {...}, "exit_code": N}`，退出码与核心 CLI 同义（0 成功、3 超时、4 冲突、5 校验、6 不存在、7 投递失败/未确定）。

| 操作 | 请求体 | 行为 |
| --- | --- | --- |
| `/v1/prompt` | `{"text"}` | RPC `prompt`，**始终**携带 `streamingBehavior:"steer"`：空闲直接开始，忙碌排队，避免先 `get_state` 判断空闲的 TOCTOU |
| `/v1/steer` | `{"text"}` | RPC `steer` 原样排队 |
| `/v1/follow-up` | `{"text"}` | RPC `follow_up`；空闲时不声称已启动，只表示已排队 |
| `/v1/status` | `{}` | 按需发一次 `get_state`，返回 `data` + 进程存活信息；不定时轮询 |
| `/v1/abort` | `{}` | 先 `clear_queue`（返回被清空的 `steering`/`followUp` 文本），再 `abort`（Pi 自身等到空闲后回应） |
| `/v1/stop` | `{}` | 返回后优雅停止控制器与 Pi |

`/v1/prompt` 的 `success: true` 只表示 Pi 接受/排队/处理了该命令。RPC 无回复不代表未接受：控制器与客户端都超时后必须报错，**不得自动重发**；重放同一 `message_id` 由核心状态机拦截，不会二次投递。

## 4. RPC 进程 I/O

- stdout 持续读取（防止背压），只按字节 LF 分帧，允许行尾 CR；**不**把 U+2028/U+2029 当分隔符。
- 单帧（去掉 LF 后）超过 8 MiB、非 UTF-8、非 JSON 对象：记 `rpc_protocol` 致命错误，唤醒全部等待者并终止 Pi；不允许静默跳过。
- `type == "response"` 且 `id` 命中 pending 才完成对应等待；异步事件（`agent_end`、`agent_settled`、`message_*` 等）只进有界事件历史，永远不当 response。乱序、并发 response 按 `id` 关联；未知/迟到 response 记录但不误配。
- 写 stdin 串行化；命令自带唯一 `id`，response 前写入完成。
- 子进程退出/启动失败/超时都会唤醒全部等待者；`status` 中 stderr 只保留有界尾部，不进入 CLI stdout。
- 命令成功但 `success: false` 明确抛 `rpc_command_failed`，不返回半真结果。

## 5. ask-child / send-child 分发

核心 CLI 同时提供 `ask-child`（阻塞等待答复）与 `send-child`（非阻塞：持久化并投递后立即返回 `request_id`/`delivery_state`，不等模型；见 [HOST-PROTOCOL §6.4](HOST-PROTOCOL.md)）。两者共用同一 `prepare_parent_ask` 持久化与下述适配器分发。核心 CLI 的父→子分发按 `child.framework` 选择子端适配器：

- `pi` 且 `<root>/pi-rpc/<binding>.json` 存在且可校验（协议、binding/run/child session 身份、`ready`、loopback endpoint）：用共享的 `build_child_prompt` 渲染问题，经 `/v1/prompt`（`streamingBehavior:"steer"`）送入 Pi；不写 inbox；记录 `host_accepted` 后立即进入核心等待状态机等 `answer_parent`。
- descriptor 存在但不可用（旧协议、身份不匹配、state 非 ready、网络/认证失败）：**明确失败**为 `failed_or_unknown`，不得静默降级到 inbox。
- descriptor 不存在：改用目标 Pi session 的 HTTP 主动推送接口（见 SESSION-PUSH.md）；接口缺失或失效明确失败，不写 inbox。
- OMP 子端使用 session HTTP，OpenCode 使用原生 prompt HTTP 推送（描述符契约）；未实现的子端框架明确报错。

Codex helper `agent_link.hosts.codex.cli` 在现有 registry/SQLite 归属校验之后提供：

- `send-child`：直接复用核心 `cli.cmd_send_child`（helper 内不再重复实现），非阻塞，JSON 与退出码与核心一致。
- `child-status`：读取 descriptor 并**按需**调用 `/v1/status`；无控制器时返回 `controller: "absent"`，不伪造在线。
- `stop-child`：经 `/v1/stop` 停止控制器；无 descriptor 明确 6。

## 6. Pi 扩展子端工具

`extensions/pi/agent-link.ts` 现有三个子端工具（`report_parent`/`ask_parent`/`check_parent_request`）之外新增：

| 工具 | 调用 | 行为 |
| --- | --- | --- |
| `answer_parent` | `{request_id, message_id, answer}` | POST `/v1/answer` 答复父端 `ask-child` 请求；答复不可覆盖；重放同 message_id 返回 `idempotent=true` |

Pi 不轮询父端请求：收到 RPC prompt（问题文本含 `request_id`）后直接调用 `answer_parent` 即可。

## 7. CLI 用法

```bash
# 父端（全局参数在子命令前）
python3 -m agent_link.hosts.pi.rpc --root ABS_ROOT --binding B serve \
  --pi-bin ABS_PI --cwd ABS_WORK --credentials ABS_CRED \
  --extension ABS_REPO/extensions/pi/agent-link.ts --provider P --model M

python3 -m agent_link.hosts.pi.rpc --root ABS_ROOT --binding B \
  send --mode prompt --text '...'          # 或 --text-file FILE；mode: prompt|steer|follow-up
python3 -m agent_link.hosts.pi.rpc --root ABS_ROOT --binding B status
python3 -m agent_link.hosts.pi.rpc --root ABS_ROOT --binding B abort
python3 -m agent_link.hosts.pi.rpc --root ABS_ROOT --binding B stop
```

skill wrapper 也支持分发：`link.py --pi-rpc --root ... --binding ... serve ...`。

## 8. 测试与不承诺

- `tests/test_pi_rpc.py`：fake Pi 脚本模拟 RPC（测试注入 argv，不暴露任意 shell）——handshake/身份、并发乱序 id 与异步事件、LF 分帧含 U+2028、`success:false`、超时不重发、子进程退出唤醒、坏帧/过长帧失败、认证与身份校验、重复 serve 不覆盖、只删自己的 descriptor、`ask-child` 直连 RPC 不产生 inbox+answer 反写+幂等、无 descriptor 的 session HTTP 接入、其他框架兼容；真实本机 `pi` 的 `get_state`/`stop`（无模型）。
- `tests/pi/extension_unit.test.mjs`：`answer_parent` 工具注册与 `/v1/answer` 调用。
- 不承诺 exactly-once、不承诺超时后自动重投、不承诺跨机；模型是否处理仍由父端收到 `answer_parent` 的答复证明。
