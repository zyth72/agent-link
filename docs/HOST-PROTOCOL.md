# Agent Link 宿主协议 v1（HOST-PROTOCOL）

本文件定义 agent_link 的 **父端宿主服务**、**子端 loopback HTTP 协议**，以及其他宿主实现 `HostDriver` 的注册方式。旧 CLI（`SPEC.md` / `USAGE.md`，v0.1）保持兼容，本文只描述新增的可选部署形态。

适用范围与不承诺：

- 同一台机器、同一可信 OS 用户；loopback 明文 HTTP。
- 每个服务进程只服务**一个 binding、一个 token**。
- **不**承诺跨机器、MCP、Intercom 协议兼容；**不**承诺防范恶意同用户进程（例如读取令牌或篡改本机数据库）。
- 桌面通知是否被模型处理，仍只能由父端实际回复证明；`host_accepted` 只代表宿主驱动/桥接受了调用。

## 1. 角色

| 角色 | 持有 | 能力 |
| --- | --- | --- |
| 父端环境 | binding、`notifier_path`/driver、服务进程、token | 启动服务、接收通知、用旧 CLI `reply` 答复；可绑定、回复、查询任意本 binding 请求 |
| 子端（如 Pi/OMP） | 凭据文件（endpoint + token） | 仅 `report` / `ask` / `get-request` / `capabilities`；不能指定父会话、不能 `bind`、不能 `reply` |

子端不需要 Codex 桌面、background-tasks 或任何桌面环境变量；它只需要凭据文件里的 loopback endpoint。父端默认 Codex 驱动依赖桌面桥，见 §5。

## 2. 启动与生命周期

### 2.1 父端启动

```bash
python3 -m agent_link.host \
  --root /abs/runtime \
  --binding BINDING_ID \
  --credentials /abs/runtime/child-credentials.json \
  [--driver MODULE:OBJECT]
```

前置条件是 binding 已由旧 CLI 创建：

```bash
python3 -m agent_link --root /abs/runtime bind --config /abs/binding.json
```

启动行为：

1. 校验 binding 存在；校验 driver 实现 `capabilities()` 与 `notify()`。
2. 绑定 `127.0.0.1` 的临时端口，生成 256-bit token。
3. 用 `O_CREAT|O_EXCL`（0600）创建凭据文件；**已存在则拒绝覆盖并关闭监听 socket、非零退出**。写失败时删除自己创建的部分文件。
4. stdout 输出一行 JSON（随后不再输出业务日志，消息内容不会进入访问日志）：

   ```json
   {"ready": true, "credentials": "/abs/runtime/child-credentials.json",
    "endpoint": "http://127.0.0.1:PORT"}
   ```

5. SIGTERM/SIGINT 优雅停止：关闭监听、退出前仅当凭据文件内容仍等于本服务写入的内容时才删除它（不会误删其他服务替换过的文件）。

### 2.2 子端启动（Pi 扩展）

```bash
AGENT_LINK_CREDENTIALS=/abs/runtime/child-credentials.json \
  pi -p --no-extensions --extension /abs/repo/extensions/pi/agent-link.ts ...
```

- 扩展也可放入项目/用户扩展目录随 Pi 自动加载；命令行显式 `--extension` 便于验证。
- 环境失败时的行为：凭据缺失、协议不是 `agent-link/1`、endpoint 不是 `http://127.0.0.1:PORT/`、token 过短 -> 工具报错，不做任何网络请求。
- 服务必须由**父端环境**启动。子端自己启动服务会拿到一个没有桌面父端的 Codex 驱动，投递只会得到 `failed_or_unknown`；这不是本协议的远程/跨机支持。

### 2.3 断开、取消与请求保留

- 子端断开连接**不会**取消等待或删除请求。ask 的处理线程最多等到持久化 deadline（受 §3.3 上限约束），随后把仍为 `pending` 的请求标记为 `timed_out`。
- 请求与答复持久化在旧 CLI 的同一个 SQLite 数据库；父端之后仍可用旧 CLI `reply` 写入答复（首个迟到答复保存为 `late=true`，状态保持 `timed_out`）。
- 子端可用相同 `request_id`/`message_id`/`question`/`timeout` 重发 ask 只等待**原截止时间的剩余量**，或用 `get-request` 读取持久化状态；读取到已过截止时间的 `pending` 请求会按 `timed_out` 处理。
- 服务重启后没有活动等待者：截止过期在下次 `get-request` / ask 重放 / `reply` 时按同一规则判定。进程在 `persisted` 与 `dispatching` 之间崩溃时保持未投递状态，**不会**盲目重投。

## 3. HTTP 协议

### 3.1 传输与认证

- `POST http://127.0.0.1:<port>/v1/<operation>`，仅 loopback，明文 HTTP/1.1，响应带 `Connection: close`。
- 每个请求必须携带 `Authorization: Bearer <token>`；缺失/错误返回 401 JSON。token 只对该服务进程的 binding 有效。
- 请求体必须是 JSON 对象，字段**恰好**如下；多一个、少一个都返回 400。
- 不支持 `Transfer-Encoding: chunked`；必须有 `Content-Length`，且 1..65536 字节。读取请求头/体的 socket 超时为 10 秒。

### 3.2 操作

成功响应统一为 `{"result": {...}, "exit_code": N}`，`exit_code` 与旧 CLI 退出码同义（0 成功、3 ask 超时、4 冲突、5 校验、6 不存在、7 投递失败/未确定）。

| 操作 | 请求体 | 成功 result 要点 | exit_code |
| --- | --- | --- | --- |
| `/v1/report` | `{"message_id","text"}` | `binding_id`,`message_id`,`kind`,`delivery_state`,`idempotent` | 0 / 7 |
| `/v1/ask` | `{"request_id","message_id","question","timeout"}` | `request_id`,`message_id`,`status`,`answer`,`late`,`delivery_state`,`idempotent`；阻塞到 replied/timed_out | 0 replied / 3 timed_out |
| `/v1/get-request` | `{"request_id"}` | `status`,`late`,`answer`,`reply_message_id`,`timeout_seconds`,`deadline_epoch`,`delivery_state` 等 | 0 |
| `/v1/answer` | `{"request_id","message_id","answer"}` | 子端答复父端发起的请求（ask-child）；`status`,`late`,`answer_message_id`,`idempotent`；语义与 CLI `answer` 一致 | 0 / 4 / 6 |
| `/v1/child-requests` | `{}` | `parent_requests`：本 binding 待答复的父端请求（pending）列表 | 0 |
| `/v1/get-parent-request` | `{"request_id"}` | 单条父端请求的持久化状态与答复 | 0 / 6 |
| `/v1/capabilities` | `{}` | 当前 driver 的能力对象 | 0 |

`timeout` 是数字（秒）；HTTP 层要求 `0 < timeout <= 3600`，超限在持久化**之前**返回 400。旧 CLI 的 ask 不受此 3600 上限影响（核心语义不变），该上限只是本服务的线程资源边界。

幂等与冲突语义与旧 CLI 完全一致：

- 同 `message_id` 同 kind/内容：`idempotent=true`，**不重复投递**；不同内容/kind：409。
- 同 `request_id` 再次 ask：question/timeout 必须一致，只等待原截止时间剩余量；改用其他 `message_id` 或改 timeout：409。
- 同一 `message_id` 不能答复两个请求；已答复/已存迟到答复后不能覆盖：409。
- 通知在写外部调用前持久化 `dispatching`；失败记 `failed_or_unknown`，任何重放都不自动重投。并发/重启后的重放由服务在 dispatch 锁内串行化，观察到的都是最终投递状态。

### 3.3 错误 JSON

所有错误（包括 HTTP 层解析错误）都是 JSON：

```json
{"error": "<code>", "message": "<text>", "exit_code": N}
```

| HTTP | error | 含义 |
| --- | --- | --- |
| 400 | `validation` | 字段/JSON/ID/timeout 校验失败（exit_code 5） |
| 401 | `unauthorized` | Bearer 缺失或错误 |
| 404 | `not_found` | 未知 binding 内请求，或未知 operation（exit_code 6） |
| 405 | `method_not_allowed` | 非 POST |
| 408 | `request_timeout` | 请求体读取超时 |
| 409 | `conflict` | 幂等/重定向/覆盖冲突（exit_code 4） |
| 4xx | `http_error` | `http.server` 自身解析错误，同样 JSON |
| 500 | `host_failure` | 服务内部未预期错误（不泄漏细节） |

## 4. 能力声明

`/v1/capabilities` 返回当前 driver 的声明，`python3 -m agent_link --root /abs/runtime capabilities` 仍是整体 CLI 能力声明。当前 Codex 驱动如实标注：

```json
{
  "host": "codex-desktop",
  "driver": "agent_link.hosts.codex.parent:CodexParentAdapter",
  "notification_transport": "desktop_bridge",
  "notification_bridge": "binding.notifier_path (background-tasks notifier.py)",
  "receive_mode": "host_injection",
  "busy_behavior": "unknown",
  "wake_after_turn": "unknown"
}
```

`busy_behavior`/`wake_after_turn` 未验证就保持 `unknown`；不要因为 binding 里的 `framework` 字符串改名而声称支持新框架。桌面桥成功只说明宿主接口接受，不代表模型已处理。

## 5. Codex 驱动的真实依赖

推荐使用独立的 [codex-background-tasks](https://github.com/zyth72/codex-background-tasks#安装)（MIT）作为 Codex 后台运行适配。先安装并重载工具，再由 `run_cli` 在父聊天环境中启动宿主服务和 Pi 子任务；明确传入绝对 cwd，保留返回的 task_id 用于查询、取消与面板查看。

宿主服务是长驻进程，与 Pi 子任务分开管理：服务设置覆盖任务时长的超时，使用 `notify=false`；确认凭据文件已生成后，再用另一个 `run_cli` 启动 Pi（`notify=true`，传入 `AGENT_LINK_CREDENTIALS`）。Pi 退出不自动结束服务，所有相关子任务结束后取消服务任务。不要把宿主服务也启用完成通知而混淆两种结果。

运行器不是通用宿主协议的强制依赖：其他框架可使用原生后台任务机制。当前 Codex 驱动对通知桥的依赖与上述运行器分工独立。

默认 `agent_link.hosts.codex.parent:CodexParentAdapter`：

- 只加载 binding 中记录的 `notifier_path`（bind 时固定、校验存在），调用其 `bridge(payload)`；消息内容只作为数据。
- 本机该路径是 background-tasks 插件的 `notifier.py`（WSL 桌面桥），需要父端桌面环境提供的 `CODEX_APP_TOOLS_PIPE_PATH` 等变量，并在内部调用应用 bundled codex-app-tools 的 send_message_to_thread。
- 因此：**服务只能在父端环境启动**；子端环境没有这些依赖。
- 桥返回只记录为 `host_accepted`；模型是否处理必须由父端实际 `reply` 证明。

父端的发现、查询与回复推荐使用 Codex 专用薄 CLI `python3 -m agent_link.hosts.codex.cli`：它按本会话 session id 从注册表发现条目，用 SQLite 只读模式校验 root 与归属后才复用核心指令，**不接受消息里的 `--root`**。用法、校验规则与 background-tasks 启动顺序见 [CODEX.md](CODEX.md)。

## 6. 其他宿主：HostDriver 注册方式

内置框架入口由 `agent_link/hosts/base.py` 定义，见 [适配器入口规范](HOST-ADAPTERS.md)。自定义 `--driver` 仍可按以下结构提供对象：

```python
class HostDriver(Protocol):
    def capabilities(self) -> dict: ...
    def notify(self, root, binding: dict, message: dict) -> str: ...
```

- `binding` 为 `Store.get_binding()` 的行（含 `binding_id`、`run_id`、`native_task_id`、parent/child framework/session、`notifier_path`）。
- `notify` 的 `message`：
  - report：`{"kind":"report","message_id","text"}`；
  - ask：`{"kind":"ask","message_id","request_id","question","timeout_seconds","deadline_epoch"}`。
- 返回值是审计用详情（截断到 2000 字符）；抛异常则投递记为 `failed_or_unknown`。
- 调用由服务在 dispatch 锁内串行执行；`notify` 不得执行消息内容、不得在数据库写事务内等待。

注册步骤（父端本地可信代码）：

1. 在可导入模块中实现类或零参工厂，例如 `myhost.driver:MyDriver`。
2. 自检：

   ```bash
   python3 -c "from agent_link.host import load_driver; \
     print(load_driver('myhost.driver:MyDriver').capabilities())"
   ```

3. 启动时指定：

   ```bash
   python3 -m agent_link.host --root ... --binding ... --credentials ... \
     --driver myhost.driver:MyDriver
   ```

4. 进程内嵌也可直接 `HostService(root, binding_id, token, driver=MyDriver())`。
5. 兼容说明：`bind` 配置仍要求存在 `notifier_path`（旧 CLI/适配器兼容）。不使用它的 driver 可以忽略该字段，但配置校验不会省略它。

### 6.1 Pi / OMP 父端与会话推送

Pi 和 OMP 使用 session-owned loopback HTTP 接口，按权威 Binding 的框架/session_id 直接推送。接收扩展启动时注册，关闭或切换时清理；无文件桥、收件箱扫描或注册表定时发现。缺连接、错身份、超时均明确失败，不自动重发或降级。

启动、认证、上限、语义与残留处理见 [SESSION-PUSH.md](SESSION-PUSH.md)。Pi 父端空闲启动/忙碌 followUp，OMP aside；OMP 子端 ask-child 同样推送，Pi 专用子端保持 [RPC](PI-RPC.md)。OpenCode 不使用此 loopback 监听协议：Python 适配器直连框架原生 prompt 端点，契约见 §6.3；插件侧文件 watcher 已删除。

### 6.2 两种索引的职责

Binding 注册表 `~/.agent-link/registry/` 记录任务会话关联，SQLite 仍是权威。Pi/OMP 会话连接目录 `~/.agent-link/sessions/`（AGENT_LINK_SESSIONS 可覆盖）只记录活跃扩展的 endpoint/token，不存消息；发送时按需查找，无需定时扫描。OpenCode 在该目录下使用独立的会话/serve 描述符（§6.3），同样按需读取；其旧 watcher 与自动认领已删除。

### 6.3 OpenCode 描述符契约（契约冻结，实现跟随）

OpenCode 走方案 (a)：宿主直连框架原生 `POST /api/session/<session_id>/prompt`（HTTP Basic `opencode:<password>`），不走 §6.1 的 `agent-link-push/1` 监听接口。拓扑决议：一 serve 一目标会话，serve 由集成自管，密码由集成以 `OPENCODE_SERVER_PASSWORD` 每 serve 生成，绝不复用用户 `service.json` 密码；attach 不声明支持，attach 路径禁止 password 入描述符。

目录（`$AGENT_LINK_SESSIONS`，默认 `~/.agent-link/sessions`；目录 0700，文件 0600）：

```text
opencode/<session_id>.json           # 会话描述符：定位一个目标会话，不存密码
opencode/servers/<serve_id>.json     # serve 描述符：密码唯一一份
```

会话描述符字段（JSON object）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `protocol` | string | 固定 `agent-link-opencode-push/1` |
| `framework` | string | 固定 `opencode` |
| `session_id` | string | 目标会话 id，与 Binding 一致 |
| `endpoint` | string | 仅 `http://127.0.0.1:<port>` origin |
| `pid` | int | serve 进程 pid，必须等于 serve 描述符的 pid 且进程存活 |
| `serve_id` | string | 引用 `servers/<serve_id>.json` 的标识 |
| `managed` | bool | 必须为 `true`；缺失/非 true（attach）适配器拒用 |
| `credential_kind` | string | 会话侧固定 `server_descriptor` |

serve 描述符字段：`protocol`/`framework`（同值）、`serve_id`（与会话引用的 id 一致）、`endpoint`（与会话描述符一致）、`pid`（与会话描述符一致且存活）、`managed=true`、`credential_kind="inline_password"`、`password`（集成生成，至少 16 字符），以及可选 `supervisor_pid`（runner 监督进程 pid，供 `stop` 优雅停止；读取方忽略未知字段）。

读取方校验（失败即 `failed_or_unknown`，不发起请求、不降级到文件）：

1. 两份描述符必须是私有普通文件（无 group/other 权限位）、JSON object，身份字段完全匹配。
2. `managed` 必须为 true；缺失或非 true 视为 attach 路径，拒绝。
3. `credential_kind` 只接受上表值；其他值（为后续 token 代理形态保留）fail closed。会话描述符出现 `password` 字段即拒绝（C2：密码只存 serve 描述符一份）。
4. endpoint 仅接受 loopback HTTP origin（无 path/query/fragment/userinfo），禁用代理与重定向；会话与 serve 描述符的 endpoint、pid 必须一致。
5. 每次投递重新读取两份描述符，不跨 run 或 delivery 缓存；密码轮换、serve 替换、pid 死亡立即生效。

请求与失败映射：

- 固定路由 `POST /api/session/<已校验 session_id>/prompt`，body `{"id": "msg_" + message_id, "text": 渲染后的 prompt, "delivery": "queue"}`；上游要求 prompt id 以 `msg_` 开头，故用确定性派生 `msg_<message_id>`（同 message_id 原样重放派生同一 prompt id）；不发送 `resume`，调度遵循框架默认。Agent Link 消息分发的 `delivery` 固定 `queue`；编排层（runner `send --delivery steer` / `post_prompt(delivery=...)`）可参数化为上游枚举 `"queue" | "steer"`（steer 注入当前活动运行，queue 排队到当前轮后），响应回显 `delivery` 不一致时不记 `host_accepted`。
- 会话级中断走固定路由 `POST /api/session/<已校验 session_id>/interrupt`（无请求体，同一描述符契约与认证），成功响应 `{"interrupted": bool}`：`false` 是"该 serve 无本进程持有的活动执行"的明确答复，不是投递失败。runner `interrupt` 子命令即此路由；401 配置错误不重试、404/超时/网络失败/无布尔标志按 `failed_or_unknown` 映射，均不自动重发。
- 只有上游 2xx 且响应 JSON 的 `sessionID` 回显与目标一致（若含 `id` 也必须等于 `msg_<message_id>`）才记录 `host_accepted`。
- 401：配置错误，不重试；404：目标会话不存在，`failed_or_unknown` 且不自动重发；其他非 2xx、超时、网络失败、响应身份不匹配：`failed_or_unknown`。仅同 `message_id`（派生同一 `msg_<message_id>` prompt id）允许幂等重放，上游首条 admission 生效。
- 密码与 Authorization 禁入日志、审计 detail 与异常文本（C3），并有模拟单测断言。

能力声明（2026-10-16 模型端到端切片 3c 后按实测更新）：父端 `notification_transport=opencode_prompt_http`、`delivery=queue`、`resume=framework_default_schedules`（不发送 `resume`，C1/C4 实测默认调度生效）、`busy_behavior=queue_after_turn`、`wake_after_turn=verified`、`polling=false`；子端 `delivery_transport=opencode_prompt_http`、`wake_child=native_injection`、`busy_behavior=queue_after_turn`、`polling=false`。C1（空闲唤醒 report_parent）、C2（ask→父模型实际 reply）、C3（ask-child→子模型 answer）、C4（工具执行中投递：2xx 接受、排队、当前轮结束后处理）、C6（每次启动插件 active 握手，未加载 fail closed）均已实测；详细证据见 `runtime/oc-push-impl-20261016-091321/impl-report.md` §9。

自管 serve runner（切片 3a）：`python3 -m agent_link.hosts.opencode.serve --root ROOT --binding B serve|status|send|interrupt|stop`。`serve` 以集成生成的 `OPENCODE_SERVER_PASSWORD`、隔离会话库（`<root>/opencode-serve/<binding>/opencode.db`，重启可恢复同一会话）和只含插件 wrapper 的 `OPENCODE_CONFIG_DIR` 启动真实 `opencode serve`；用户配置仅通过 `OPENCODE_CONFIG` 只读引用，不复制、不修改。就绪以 `/api/plugin` 中 `agent-link` 为 `active` 为准，随后用 `POST /api/session` 预定绑定会话并写描述符。`send --delivery queue|steer` 参数化上游投递模式；`interrupt` 调用上述原生中断路由。C5 实测语义：进程被强杀后描述符保留且 pid 已死——`status` 报 `stale`、推送明确失败、runner 拒绝在残留描述符上启动，需按 E1 显式确认旧进程已退出后清理；数据库保留时，重启 serve 会采用同一 session id 并保留全部历史（已验证），进程/描述符不会自动重建。优雅 `stop` 优先向 `supervisor_pid` 发 SIGTERM，只删自己的描述符。steer/interrupt 的验证分层：模拟与真实隔离 serve（无模型）已入库（`tests/test_opencode_push.py`、`tests/test_opencode_serve.py`）；运行中模型的 steer 采纳与 interrupt 生效以模型端到端记录为准，`host_accepted` 不代表模型已处理。

### 6.4 父→子消息：ask-child 与 send-child

两个命令都由核心 CLI 提供，按权威 Binding 经 `prepare_parent_ask` 持久化同一行 `parent_requests`，再由 `dispatch_parent_request` 选择 `binding.child_framework` 的适配器投递（Pi RPC 或 session push、OMP session push、OpenCode 原生 prompt push，见 §6.1/§6.3）：

| 命令 | 语义 | 退出码 |
| --- | --- | --- |
| `ask-child --binding B --request-id R --message-id M --question Q --timeout N` | 持久化+投递后阻塞等待 `answered`/`timed_out` | 0 answered / 3 timed_out / 4/5/6/7 |
| `send-child --binding B --request-id R --message-id M --question Q --timeout N` | 只持久化+投递，不等子模型；适合父端“随时可达”地下发消息 | 0 host_accepted / 7 failed_or_unknown / 4/5/6 |

- 两者共享幂等/冲突规则：同 `request_id` + 同 `message_id` + 同 question/timeout 为重放，不二次投递；同 `request_id` 不同 `message_id`/question/timeout，或同 `message_id` 已被其他 `request_id` 占用，返回冲突（exit 4）。先 `send-child` 后 `ask-child`（或反之）只投递一次，后者复用已有请求行等待。
- 投递成功只记 `host_accepted`，不代表子模型已处理；答复仍由子端 `answer`（或框架工具）写入并以持久化行为为准。
- 子端 busy/空闲时的可达性已对 OpenCode 做模型端到端实测（切片 3c）：空闲 prompt 可唤醒并产生行为（C1/C3）；`ask_parent` 工具运行期间再次投递会立即得到 2xx 但排队，在当前轮结束后才处理（C4），不中断正在运行的工具。`send-child` 只保证“已尝试投递且结果确定（host_accepted/failed_or_unknown）”，不保证忙碌子端立刻处理；其它框架的 busy 行为仍以各自实测为准。

## 7. 子端扩展工具（Pi / OMP）

`extensions/pi/agent-link.ts` 在 Pi 中注册四个模型可调用工具（`Type` 来自 `@earendil-works/pi-ai`，`defineTool`/`ExtensionAPI` 来自 `@earendil-works/pi-coding-agent`），并带有 session HTTP 主动通知接口：

| 工具 | 调用 | 行为 |
| --- | --- | --- |
| `report_parent` | `{message_id, text}` | POST `/v1/report`；投递失败（exit 7）抛错；重放返回 `idempotent=true`，不重投 |
| `ask_parent` | `{request_id, message_id, question, timeout}` | POST `/v1/ask` 并阻塞等待；`status=replied` 时文本包含真实 `answer`；`timed_out` 不抛错、不伪造答案，提示用 `check_parent_request` 读迟到答复；timeout schema 1ms..3600s |
| `check_parent_request` | `{request_id}` | POST `/v1/get-request`；读取本子端持久化状态与迟到答复 |

- 凭据来自 `AGENT_LINK_CREDENTIALS` 指向的 JSON 文件，每次调用重新读取；非 loopback/未知协议/无效 token 直接报错，不发请求。
- 客户端超时：默认 30s；ask 为服务端 timeout + 15s 余量。abort/超时不会取消服务端等待，也不会删除请求；应以相同 ID 查询或重放。
- 父端发起的请求（ask-child）：OMP 子端另有两个工具——`poll_parent_requests`（POST `/v1/child-requests`，列出待答复的父端请求）与 `answer_parent`（POST `/v1/answer`，答复不可覆盖）；Pi 子端提供原生 answer_parent，并从专用 RPC prompt 接收父端问题。
- 加载方式（启动步骤见 §2.2）：命令行 `--extension` 显式加载，或放入项目/用户扩展目录，或登记进 `~/.omp/agent/config.yml` 的 `extensions:`；不会自动读取任何父端配置或会话 ID（session id 由扩展从宿主运行时读取，见 §6.2 自动认领）。
- OMP：五个工具保持；session_start 登记当前会话推送接口，父端通知和子端问题均调用 aside 注入，session_shutdown 清理。
- Pi：四个工具保持；交互父端使用 session HTTP 推送，专用子端 RPC 使用 answer_parent 回答；都不扫描通知文件。
- OpenCode：`extensions/opencode/agent-link.ts` 是 v2 插件（`{id, setup}`；`ctx.tool.transform` 注册五个子端工具，语义与 OMP 逐字一致）。父端驱动 `agent_link.hosts.opencode.parent:OpenCodeParentAdapter`，Python 父子适配器均直连原生 prompt 端点（§6.3），不写文件桥/收件箱；插件不再含 watcher、锁/游标或“最近活跃会话”自动认领。本机 2.0.29 只接受 v2 插件形态（v1 工厂形态被拒绝）。
- Codex 子端：尚无适配器与 inbox watcher；`ask-child` 对它不会自动唤醒，不要承诺。Codex 作为父端的发现/回复见 [CODEX.md](CODEX.md)。

## 8. 测试与已验证 / 未验证

运行：

```bash
cd /home/zyth/agent-intercom-workspace
python3 -W error::ResourceWarning -m unittest discover -s tests -t .
# Node 单元（真实加载扩展 + fake HTTP，不调用模型）:
PI_CODING_AGENT_ROOT=<pi 包根> node tests/pi/extension_unit.test.mjs
```

本协议相关测试：

- `tests/test_host.py`：认证/**未知字段**/方法/路径 JSON 错误、body 限制、超时上限、**并发同消息单次投递**、驱动失败不重投、跨 binding 隔离、**服务重启重放不重投**、请求体读超时 408、凭据不覆盖/只删自己的。
- `tests/test_pi_extension.py`：jiti 加载真实扩展 + fake `ExtensionAPI`；真实 `agent_link.host` + notifier stub（走旧 CLI `reply`）闭环：report/ask/get-request、超时后迟到答复、重启重放；`pi --help` 用探针扩展验证 Pi 真实加载扩展（无模型、无会话）。
- `tests/pi/extension_unit.test.mjs`：原生工具与凭据校验，以及 HTTP 推送认证、身份、上限、失败、顺序和会话生命周期。
- `tests/test_omp_host.py`、`tests/test_push.py`：主动推送客户端与宿主状态闭环、错身份、缺连接、禁止重定向、不自动重投。
- `tests/test_opencode_push.py`：OpenCode 原生 prompt 推送（fake serve，无模型）：Basic 路由与 body、同 id 幂等重放、401 配置错误不重试、404/5xx/超时映射、描述符缺失/身份不匹配/未 managed/pid 死亡/非 loopback/重定向拒绝、描述符每次重读、密码不入异常/日志/审计。
- `tests/test_opencode_serve.py`：真实隔离 `opencode serve`（无模型，缺可执行文件则 skip）：runner ready/插件 active 握手、会话预定与描述符 0600、401/404/幂等/仅 loopback 监听、residue 显式失败、插件未加载 fail closed、超时/拒绝映射、优雅 stop+重启会话恢复。
- `tests/test_omp_extension.py`：真实 OMP 扩展的模拟上下文与本地 HTTP 注入，以及无模型注册；共享接收逻辑与 Pi 同源。
- `tests/test_parent_requests.py`：请求/答复状态、幂等、冲突、超时与迟到；OpenCode prompt 推送闭环仍有专门覆盖。
- `tests/test_codex.py`、`tests/test_registry.py`：父端发现、归属校验和注册表行为。
- `tests/test_opencode_extension.py`：OpenCode v2 工具、原生 prompt 推送宿主闭环与隔离环境插件注册（已删除 watcher 套件）。

当前测试矩阵和真实模型证据以 [SESSION-PUSH](SESSION-PUSH.md)、[PI-RPC](PI-RPC.md) 与各框架 README 为准。历史文件桥验收不等于主动推送验收。Pi/OMP 正式扩展已通过真实 DeepSeek 的空闲通知和 ask/reply 闭环；工具运行中的消息注入已由真实模型验证；计划模式、中断竞争、会话切换、重连仍需分别验证。OpenCode 已完成三层验收：模拟（`test_opencode_push.py`）、真实隔离 serve 无模型（`test_opencode_serve.py`，含 B 组）与模型端到端（切片 3c，C1–C6 见 §6.3）；插件 watcher 已删除；Codex 子端未实现。
