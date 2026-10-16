# OpenCode 接入：能力与启动

入口 `get_adapters()` 提供父端和子端。插件针对 OpenCode **v2**；本机版本 2.0.29。旧版 `/session/:id/prompt_async` 示例不用于本版。

## 已决：去轮询方向

三方讨论（2026-10-16，依据本机 [v2.0.29 去轮询实测报告](../../../runtime/oc-nonpoll-20261016-082234/opencode-nonpoll-report.md)）的决议：

- 采用方案 **(a)**：宿主直连 OpenCode 原生 HTTP API 推送注入，去掉文件轮询。备选 (b')（插件自建 loopback 监听）冻结、不实现，仅作为未来 attach/无需 server password 场景的预留形态；(c) 维持文件轮询仅作迁移期过渡、不再作为终点。
- **拓扑**：一 serve 一目标会话，serve 由集成自管；密码由集成以 `OPENCODE_SERVER_PASSWORD` 每 serve 生成，绝不复用用户 `service.json` 密码，绝不跨 run 重用。attach 不声明支持，attach 路径禁止 password 入描述符。
- 描述符必须带 `managed: true`；无 managed 标记（attach 路径）的适配器拒用。
- 切片 3c 已完成模型端到端（GLM-5.3-Flash）：C1 空闲唤醒、C2 ask→父模型 reply、C3 ask-child→子模型 answer、C4 busy 排队、C5 会话/DB 恢复、C6 插件 fail-closed 均已实测；证据见 [impl-report §9](../../../runtime/oc-push-impl-20261016-091321/impl-report.md)。

实施门禁 A–E（摘要）：

- **A 拓扑与凭据**：A1 serve 由集成启动、密码每 serve 唯一、一 serve 一目标会话；A2 密码只存 serve 描述符一份（0600/目录 0700，写者仅 runner，字段完整），attach 不写；A3 password/Authorization 禁入日志、审计与异常，并加单测断言；A4 仅 127.0.0.1 HTTP origin、禁重定向与代理、固定 prompt 路由，缺失/身份不匹配/pid 死→`failed_or_unknown`，不跨 run 缓存。
- **B 接口语义（真实隔离 serve、无模型）**：B1 401/404/同 id 幂等/loopback/禁重定向/超时映射与重复/残留描述符显式失败；B2 `message_id` 派生确定性 prompt `id`（`msg_<message_id>`，上游要求 `msg_` 前缀），仅上游返回后记 `host_accepted`，不自动重发，`delivery`/`resume` 策略如实标注。
- **C 模型端到端**：C1 空闲通知能唤醒并产生可验证行为；C2 ask→父模型实际 reply；C3 ask-child→子模型实际 answer；C4 busy（工具执行中）投递；C5 serve 重启后的会话/绑定恢复（不可恢复就明确重建语义）；C6 每次启动校验插件确已加载，未加载 fail closed。
- **D 去轮询证据与文档**：D1 适配器不再引用 bridge-watcher/inbox/omp-bridge；D2 capabilities 改 push 语义；D3 README/HOST-PROTOCOL/TERMINOLOGY 同步；D4 模拟/真实接口/模型三层证据分别留存，历史文件桥验收不得转述为推送验收。
- **E 生命周期清理**：E1 运行器只停自己启动的 serve，残留描述符显式处理；E2 attach 不声明支持，未来若支持只走 (b')/token 且须先补 listener 真实接口验收。

当前状态（切片 1/2/3 已完成）：Python 父子适配器直连原生 prompt HTTP 推送；插件只保留五个工具；bridge-watcher/inbox 已删除；runner 编排（启动自管 serve、插件 active 就绪握手、预定会话、写描述符、优雅停止）见下节；`tests/test_opencode_push.py`（fake serve）、`tests/test_opencode_serve.py`（真实隔离 serve，无模型）与切片 3c 模型端到端分别留存证据。

## 原生能力与当前适配

| 操作 | OpenCode v2 原生 | 当前 Agent Link 适配 |
| --- | --- | --- |
| steer | prompt 的 `delivery:steer`，默认安全边界投递 | 未提供直接控制入口 |
| queue | prompt 的 `delivery:queue` | Python 适配器推送时使用 |
| interrupt | session interrupt | 未接入统一控制接口 |
| 事件订阅 | 原生流式接口 | 未接入直接事件流传输 |
| 双向请求/答复 | 插件工具 | 五个子端工具 + Python 原生 prompt 推送（描述符契约） |

Python 推送的失败映射：仅上游 2xx 且回显 `sessionID` 与目标一致（若含 `id` 也须等于派生 prompt id `msg_<message_id>`）才记 `host_accepted`；401 为配置错误不重试；404 与其他非 2xx、超时、网络失败、身份不匹配均为 `failed_or_unknown`，不自动重发、无文件回退。仅同 `message_id`（派生同一 prompt id）允许幂等重放，上游首条 admission 生效。accept 不等于模型已处理。

## 描述符契约（切片 1 冻结，实现跟随）

宿主按 `$AGENT_LINK_SESSIONS`（默认 `~/.agent-link/sessions`）查找描述符；目录 0700、文件 0600：

```text
opencode/<session_id>.json           # 会话描述符：目标会话、serve 引用，不存密码
opencode/servers/<serve_id>.json     # serve 描述符：集成生成密码的唯一一份
```

会话描述符：`protocol=agent-link-opencode-push/1`、`framework=opencode`、`session_id`、`endpoint`（仅 `http://127.0.0.1:<port>`）、`pid`、`serve_id`、`managed=true`、`credential_kind=server_descriptor`，且不得出现 `password` 字段（C2）。

serve 描述符：`protocol`/`framework` 同值、`serve_id`、`endpoint`、`pid`（三者与会话描述符一致且进程存活）、`managed=true`、`credential_kind=inline_password`、`password`（≥16 字符），以及可选 `supervisor_pid`（runner 监督进程，供 `stop` 优雅停止）。

完整字段表、校验顺序与 fail-closed 规则见 [HOST-PROTOCOL §6.3](../../../docs/HOST-PROTOCOL.md)。

## 自管 serve runner（切片 3a）

```bash
# 启动（前台运行；由父端运行器后台执行）并输出 ready JSON
python3 -m agent_link.hosts.opencode.serve --root ROOT --binding B serve \
  --cwd WORK --credentials ROOT/child-credentials.json \
  [--model PROVIDER/MODEL] [--config USER_OPENCODE_JSON] [--ready-timeout 60]

python3 -m agent_link.hosts.opencode.serve --root ROOT --binding B status
python3 -m agent_link.hosts.opencode.serve --root ROOT --binding B send --text '...' [--message-id ID]
python3 -m agent_link.hosts.opencode.serve --root ROOT --binding B stop
```

- 启动：`OPENCODE_SERVER_PASSWORD` 每 serve 随机生成（不复用用户 `service.json`）；会话库隔离在 `<root>/opencode-serve/<binding>/opencode.db`（稳定 per binding，重启可恢复同一会话）；`OPENCODE_CONFIG_DIR` 只生成指向本仓库插件的 wrapper，用户配置仅通过 `OPENCODE_CONFIG` 只读引用；端口冲突/插件未 active 均 fail closed。
- 就绪：轮询 `/api/plugin` 确认 `agent-link` 为 `active`（C6），随后 `POST /api/session` 以绑定 `session_id` 预定会话，再写两描述符（`O_EXCL`、0600）。
- 停止：`stop` 优先向 `supervisor_pid` 发 SIGTERM，由 supervisor 停子进程并只删自己的描述符；无 supervisor 时校验 serve pid 命令行后才停。
- C5 语义：进程被强杀后描述符保留且 pid 已死；`status` 报 `stale`、推送明确失败、runner 拒绝在残留描述符上启动；显式确认旧进程已退出并清理后，用同一 DB 重启会采用同一 session id 并保留历史；不自动重建进程。

## 加载插件与启动

先读 [公共准备](../README.md#启动责任与公共准备)。保持整个 extensions 目录完整；不要单独复制 agent-link.ts。可在本机 OpenCode v2 的自动发现目录 `~/.config/opencode/plugin/` 放一个入口，绝对路径替换为实际 REPO：

```ts
// agent-link.ts：本机配置文件，不提交真实路径到共享配置
export { default } from "/absolute/path/to/agent-link/extensions/opencode/agent-link.ts";
```

这是配置操作说明；本次开发不自动修改用户的真实 OpenCode 配置。已有插件配置应合并，不覆盖。模型与认证通过 OpenCode 自身配置选择，不写进 binding；Agent Link 不直接调用模型 API。用 runner `serve` 时无需把它装进用户目录：runner 在 `<root>/opencode-serve/<binding>/config/plugin/` 生成 wrapper 并设置 `OPENCODE_CONFIG_DIR`，用户自己的配置只被只读引用。

子端凭据须在启动进程时提供；父端服务 ready 后执行：

```bash
cd "$WORK"
AGENT_LINK_CREDENTIALS="$ROOT/child-credentials.json" \
  opencode serve --hostname 127.0.0.1 --port 4096
```

端口由调用方选择，4096 仅为示例；端口被占用应明确失败。API 服务就绪不代表已有会话或模型运行：还需用 v2 客户端创建/选择目标会话，取得真实 session id，写入绑定后再发送任务。若父端是 OpenCode，服务选择 `--framework opencode`；若父端是 Codex 仍选择 codex-desktop。上述手工启动方式仍需自行按上节写描述符；用 runner `serve` 则由编排自动完成（见上节）。

`opencode serve` 本身没有模型选择参数，不把 Pi 的 `--provider/--model` 参数传给它。模型按 v2 会话的 model 配置选择；发任务调用框架 API，才会产生模型调用。

## 已有会话、就绪与限制

插件必须在服务启动时加载；仅启动一个独立 serve 不会自动接管用户已有 TUI 的服务。连接已有会话需使用持有该会话的服务及其真实 session id，不能另建服务后宣称已 attach；受支持的路径是集成自管的专用 serve + 描述符，attach 不声明支持。

Python 推送按权威 Binding 的 `session_id` 直接寻址；插件不再选择或跟踪目标会话。一个专用服务/location 只运行一个目标会话。核对插件加载（runner 已做 active 握手）与工具注册，再通过父端持久化 reply/子端 answer 确认模型处理。五个子端工具与 OMP 相同。

2026-10-16 模型端到端实测（GLM-5.3-Flash，独立 runtime root）：空闲会话的推送能唤醒模型并调用 `report_parent`（C1，host_accepted 且工具调用可在 session message 中看到）；ask→父模型 reply（C2，父端答复被子模型回收并回报）；ask-child→子模型 `answer_parent`（C3）；工具执行中再次推送立即 2xx 但排队，当前轮结束后才处理，不中断运行中的工具（C4）；强杀后残留描述符显式失败、显式清理后同一 DB 重启会话与历史完整恢复（C5）；每次启动均验证插件 active，未加载 fail closed（C6）。无直接远程 interrupt/stop-child；interrupt 未接入统一控制接口。

## 清理

通过 runner `stop` 停止本次专用 OpenCode 服务（优先停 supervisor，再停通信服务）；不要停止用户自己的既有服务；只停自己启动的 serve（门禁 E1）。强杀后的描述符残留必须显式核对旧进程已退出后再按 serve_id/pid 清理，不自动扩散或重建。当前适配没有直接远程 interrupt/stop-child 实现，不能套用 Pi 命令。
