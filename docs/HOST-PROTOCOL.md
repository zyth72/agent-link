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

### 6.1 OMP 父端驱动（session_bridge_file）

`agent_link.hosts.omp.parent:OmpParentAdapter` 把每条通知追加为一行 JSON 事件到 `<root>/omp-bridge/<binding_id>.jsonl`（flock 独占 + fsync，0600）；父端 OMP 主会话加载 `extensions/omp/agent-link.ts` 轮询该文件并注入会话：

```bash
python3 -m agent_link.host --root /abs/runtime --binding live-001 \
  --credentials /abs/runtime/child-credentials.json \
  --driver agent_link.hosts.omp.parent:OmpParentAdapter

AGENT_LINK_BRIDGE_FILE=/abs/runtime/omp-bridge/live-001.jsonl \
AGENT_LINK_BINDING=live-001 \
  omp --no-extensions --extension /abs/repo/extensions/omp/agent-link.ts
```

- 桥文件旁有两个由扩展管理的派生文件：`<bridge>.lock`（单消费者锁，内容 `pid:uuid`；持有进程死亡后由下一个会话回收一次，存活则拒绝成为第二消费者）与 `<bridge>.cursor`（`{offset, dev, ino}`，逐条原子推进，重启后从断点继续并识别追加/替换/截断）。
- 事件字段：`v`(=1)、`ts`(UTC ISO8601 毫秒)、`source`(`service`|`notifier`)、`binding_id`、`thread_id`(父会话 id)、`parent_framework`、`child_framework`、`child_session_id`、`kind`、`message_id`、`prompt`；ask 事件另有 `request_id`。
- watcher 的目标选择：显式 `AGENT_LINK_BRIDGE_FILE` + `AGENT_LINK_BINDING` 优先；未设置时按注册表自动认领（见 §6.2）：parent 角色匹配 `parent.framework` 与本会话框架 + `parent.session_id`==本会话 id，child 角色匹配 `child.*`；自动认领模式下事件还必须 `thread_id`（父桥）/ `child_session_id`（子端 inbox）等于本会话 id。
- 扩展每秒轮询；只注入通过校验的事件（`v==1`、`binding_id` 匹配、`prompt` 非空），以 `deliverAs:"aside"`、`attribution:"agent"` 注入后推进 cursor。JSON 非法、结构非法、binding 不匹配或单行超过 1 MiB 的事件记日志跳过，不阻塞后续事件；**注入失败的事件会保序阻塞其后所有事件**，直到重试成功。watcher 本体（锁/cursor/重试/认领）在 `extensions/shared/bridge-watcher.ts` 共享，OMP 与 Pi 适配器只是宿主胶水。
- 写入失败（如桥目录位置被占用）由驱动抛 `DeliveryFailure`，服务记为 `failed_or_unknown`，不会产生半个事件。
- 投递为 at-least-once：cursor 落盘前崩溃、或桥文件被整体替换时，已注入事件可能重放；不承诺 exactly-once。

旧 `omp_notifier.py` 路径已删除，不提供兼容转发；OMP 接入使用本节通信服务与扩展。

### 6.2 绑定索引（registry）与父→子通道（inbox）

- `bind` 与宿主服务启动时把绑定写入用户级索引 `~/.agent-link/registry/<binding_id>.json`（0600，目录 0700；环境变量 `AGENT_LINK_REGISTRY` 可覆盖）。索引只是发现指针，权威记录仍是各 root 的 SQLite；条目丢失可由重放 bind 或启动宿主服务自愈。
- 适配器（OMP/Pi watcher）扫描索引，把条目里 `parent.*`/`child.*` 的 framework+session_id 与本会话匹配来自动认领 binding（取 `updated_at` 最新者；显式环境变量优先）。CLI `list-bindings` 提供同一索引的人类/shell 视图（`--parent-session-id` 等过滤）。
- 父→子通道：CLI `ask-child`/`answer`/`get-parent-request`/`list-parent-requests`，语义与子端 ask/reply 镜像（同内容重放幂等、不同内容冲突、超时/迟到答复 late=true、答复不可覆盖）。宿主服务另有 `/v1/answer`、`/v1/child-requests`、`/v1/get-parent-request`（见 §3.2）。
- 父端请求经 `<root>/inbox/<binding_id>.jsonl` 唤醒子端会话（同一共享 watcher，child 角色认领）；事件字段同 §6.1，`kind` 为 `ask_child`、`source` 为 `parent-cli`。投递的 `host_accepted` 只表示事件可读，不表示子端已处理；子端是否答复由持久化的 answer 证明。
- Codex 父端用 `python3 -m agent_link.hosts.codex.cli --session-id ... [--binding ...] list|reply|get-request|ask-child|get-parent-request|list-parent-requests` 发现和操作绑定：按 `parent.framework=codex-desktop` 与 `parent.session_id` 过滤注册表，SQLite 只读校验 root/归属/run_id/config_digest 后才接受，不接受消息里的 `--root`；多条匹配必须显式 `--binding`。详见 [CODEX.md](CODEX.md)。

## 7. 子端扩展工具（Pi / OMP）

`extensions/pi/agent-link.ts` 在 Pi 中注册三个模型可调用工具（`Type` 来自 `@earendil-works/pi-ai`，`defineTool`/`ExtensionAPI` 来自 `@earendil-works/pi-coding-agent`），并带有父端桥 watcher：

| 工具 | 调用 | 行为 |
| --- | --- | --- |
| `report_parent` | `{message_id, text}` | POST `/v1/report`；投递失败（exit 7）抛错；重放返回 `idempotent=true`，不重投 |
| `ask_parent` | `{request_id, message_id, question, timeout}` | POST `/v1/ask` 并阻塞等待；`status=replied` 时文本包含真实 `answer`；`timed_out` 不抛错、不伪造答案，提示用 `check_parent_request` 读迟到答复；timeout schema 1ms..3600s |
| `check_parent_request` | `{request_id}` | POST `/v1/get-request`；读取本子端持久化状态与迟到答复 |

- 凭据来自 `AGENT_LINK_CREDENTIALS` 指向的 JSON 文件，每次调用重新读取；非 loopback/未知协议/无效 token 直接报错，不发请求。
- 客户端超时：默认 30s；ask 为服务端 timeout + 15s 余量。abort/超时不会取消服务端等待，也不会删除请求；应以相同 ID 查询或重放。
- 父端发起的请求（ask-child）：OMP 子端另有两个工具——`poll_parent_requests`（POST `/v1/child-requests`，列出待答复的父端请求）与 `answer_parent`（POST `/v1/answer`，答复不可覆盖）；Pi 子端暂用 CLI `answer` 答复，工具化留待后续。
- 加载方式（启动步骤见 §2.2）：命令行 `--extension` 显式加载，或放入项目/用户扩展目录，或登记进 `~/.omp/agent/config.yml` 的 `extensions:`；不会自动读取任何父端配置或会话 ID（session id 由扩展从宿主运行时读取，见 §6.2 自动认领）。
- OMP：`extensions/omp/agent-link.ts` 注册全部五个工具（`pi.zod` schema，带 `loadMode:"essential"`），工具名、文案与语义与 Pi 版同名工具逐字一致。同一文件还充当桥 watcher（共享实现见 `extensions/shared/bridge-watcher.ts`）：显式环境变量优先，否则按注册表自动认领，可同时持有父桥与子端 inbox 两个 watcher。
- Pi：`extensions/pi/agent-link.ts` 三个子端工具（`Type`/`defineTool`）+ 父端 watcher（同一共享模块；注入按 Pi 语义：空闲 `sendUserMessage` 起一轮，忙碌 `deliverAs:"followUp"` 排队）。
- OpenCode：`extensions/opencode/agent-link.ts` 是 v2 插件（`{id, setup}`；`ctx.tool.transform` 注册五个子端工具，语义与 OMP 逐字一致；注入用 `ctx.session.prompt({delivery:"queue"})`）；watcher 复用同一共享模块（framework "opencode"，父子两角色；插件是 server 级，会话归属取最近活跃会话，多会话并发时为近似——见文件头注释）。父端驱动 `agent_link.hosts.opencode.parent:OpenCodeParentAdapter`（OmpHost 薄子类，同一文件桥）。本机 2.0.29 只接受 v2 插件形态（v1 工厂形态被拒绝）。
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
- `tests/pi/extension_unit.test.mjs`：工具注册、凭据校验、HTTP 错误、timeout 结果、abort、父端 watcher（env 注入空闲/忙碌、自动认领匹配/不匹配、迟到 binding 发现、注入失败保序）。
- `tests/test_omp_host.py`：OmpHost capabilities 与桥事件 schema、服务闭环（report 幂等不重发、ask 经 CLI `reply` 闭环）、`<root>/omp-bridge` 写失败记 `failed_or_unknown`、`omp_notifier.py` 旧 CLI 路径与缺环境变量失败。
- `tests/test_omp_extension.py`（驱动脚本 `tests/omp/tool_loop.mjs`）：Bun 加载真实扩展对真实 `agent_link.host` + OmpHost 闭环（含父→子 poll/answer）；父端桥验收（重启恢复、注入失败保序阻塞与同进程重试、第二消费者拒绝与陈旧锁回收、会话切换换锁、文件替换/截断从零重放、超长与非法事件跳过、按 session id 自动认领与迟到 binding 发现、子端 inbox 认领）；真实 `omp --mode rpc` 的 `dumpTools` 注册五个工具（无模型）。
- `tests/test_registry.py`：绑定注册表（写入权限/原子替换/重放保留 created_at/过滤/CLI bind 联动/宿主服务自愈重建）。
- `tests/test_parent_requests.py`：父→子通道 CLI 闭环（inbox 事件字段、answered/超时/迟到答复、幂等与冲突、跨请求答复冲突、pending 过滤）、服务 `/v1/answer`/`/v1/child-requests`/`/v1/get-parent-request`。
- `tests/test_codex.py`：Codex 父端 helper 的注册表发现与只读校验（多绑定不可选错、跨会话/错框架/陈旧摘要与 run_id 拒绝、丢 DB 不新建、坏条目带原因）、reply 与 ask-child/answer 临时数据库闭环（模拟子端，不发桌面消息）、`--root` 不被接受、session 环境变量回退、wrapper 从 `/tmp` 两种分发与 repo-location.json 定位、prompt 提示的框架隔离与 shell 引号。
- `tests/test_opencode_extension.py`（`tests/opencode/` harness + driver）：v2 插件 fake ctx 五工具闭环、watcher 全套（env 注入/恢复/保序/双消费者/会话切换/替换截断/超大非法/自动认领四组）、真实 `opencode serve` 隔离 HOME 注册（无模型）。

已验证：上述 loopback 闭环、Pi 扩展注册与调用、OMP 扩展在真实 OMP 18.11.1 下的加载与工具注册（`dumpTools` 列表）、OMP 父端桥六组验收（全部无模型）、真实 OMP 会话注入实测（`--mode rpc` 空闲会话收到桥事件后启动一轮，模型实际回答 `bridge-ok`；`.lock` 由真实 1s 轮询持有与释放，`.cursor.offset` 推进到文件大小）、OMP 子端真实模型工具调用（后台任务：`omp -p` + 真实扩展的模型实际调用 `report_parent`/`ask_parent` 并收到父端 CLI `reply` 的答复，桥文件与数据库为服务端证据）、绑定注册表与按 session id 自动认领（OMP/Pi watcher 无模型验收）、父→子通道（ask-child/answer CLI 与宿主服务闭环、OMP 子端 inbox 唤醒与工具，全部无模型）、Pi 父端 watcher（无模型验收，共享同一 watcher 模块）、OpenCode 适配器（v2 插件注册与五工具、共享 watcher 场景、真实 `opencode serve` 隔离环境注册，均无模型）、旧 CLI 全量测试。
未验证/不承诺：真实桌面新服务的通知投递（本轮避免与父任务冲突，未发真实桌面消息）、OMP 忙碌会话（轮次进行中）的注入行为与父轮次结束后的唤醒（空闲会话 `aside` 启动一轮已实测；其余语义依据上游 extensions.md（main））、Pi 侧的 LLM 端到端工具调用（OMP 侧已实测：模型实际调用 `report_parent`/`ask_parent`；Pi 侧只验证了扩展加载、工具执行与注入后的一轮回答）、OpenCode 的 LLM 端到端与忙碌/唤醒细节（v2 插件注册已实测；一次空闲注入唤醒来自意外探测，其余未验）、Codex 子端适配与 Pi 子端 inbox 自动唤醒（均未实现）、跨机器/MCP/Intercom、恶意同用户隔离、应用重启后的通知可靠性、exactly-once 投递。

如需复查真实会话注入（会调用一次当前配置的模型，仅手动执行，不属于默认测试）：

```bash
AGENT_LINK_BRIDGE_FILE=/abs/runtime/omp-bridge/live-001.jsonl AGENT_LINK_BINDING=live-001 \
  omp --mode rpc --no-ui --no-extensions -e /abs/repo/extensions/omp/agent-link.ts
# 再向桥文件追加一行 {"v":1,"binding_id":"live-001","prompt":"..."}：
# <bridge>.lock 出现、<bridge>.cursor.offset 推进到文件大小，rpc stdout 出现 turn_start 与模型回答。
```
