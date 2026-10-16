# OpenCode 接入：能力与启动

入口 `get_adapters()` 提供父端和子端。插件针对 OpenCode **v2**；本机版本 2.0.29。旧版 `/session/:id/prompt_async` 示例不用于本版。

## 已决：去轮询方向

三方讨论（2026-10-16，依据本机 [v2.0.29 去轮询实测报告](../../../runtime/oc-nonpoll-20261016-082234/opencode-nonpoll-report.md)）的决议：

- 采用方案 **(a)**：宿主直连 OpenCode 原生 HTTP API 推送注入，去掉文件轮询。备选 (b')（插件自建 loopback 监听）冻结、不实现，仅作为未来 attach/无需 server password 场景的预留形态；(c) 维持文件轮询仅作迁移期过渡、不再作为终点。
- **拓扑**：一 serve 一目标会话，serve 由集成自管；密码由集成以 `OPENCODE_SERVER_PASSWORD` 每 serve 生成，绝不复用用户 `service.json` 密码，绝不跨 run 重用。attach 不声明支持，attach 路径禁止 password 入描述符。
- 描述符必须带 `managed: true`；无 managed 标记（attach 路径）的适配器拒用。
- busy 投递、空闲唤醒、serve 重启恢复只有 [推断]，capabilities 与文档保持 `unknown`，不得宣称已验证。

实施门禁 A–E（摘要）：

- **A 拓扑与凭据**：A1 serve 由集成启动、密码每 serve 唯一、一 serve 一目标会话；A2 密码只存 serve 描述符一份（0600/目录 0700，写者仅 runner，字段完整），attach 不写；A3 password/Authorization 禁入日志、审计与异常，并加单测断言；A4 仅 127.0.0.1 HTTP origin、禁重定向与代理、固定 prompt 路由，缺失/身份不匹配/pid 死→`failed_or_unknown`，不跨 run 缓存。
- **B 接口语义（真实隔离 serve、无模型）**：B1 401/404/同 id 幂等/loopback/禁重定向/超时映射与重复/残留描述符显式失败；B2 `message_id` 原样作 prompt `id`，仅上游返回后记 `host_accepted`，不自动重发，`delivery`/`resume` 策略如实标注。
- **C 模型端到端**：C1 空闲通知能唤醒并产生可验证行为；C2 ask→父模型实际 reply；C3 ask-child→子模型实际 answer；C4 busy（工具执行中）投递；C5 serve 重启后的会话/绑定恢复（不可恢复就明确重建语义）；C6 每次启动校验插件确已加载，未加载 fail closed。
- **D 去轮询证据与文档**：D1 适配器不再引用 bridge-watcher/inbox/omp-bridge；D2 capabilities 改 push 语义；D3 README/HOST-PROTOCOL/TERMINOLOGY 同步；D4 模拟/真实接口/模型三层证据分别留存，历史文件桥验收不得转述为推送验收。
- **E 生命周期清理**：E1 运行器只停自己启动的 serve，残留描述符显式处理；E2 attach 不声明支持，未来若支持只走 (b')/token 且须先补 listener 真实接口验收。

当前状态（切片 1/2 已完成）：Python 父子适配器改为原生 prompt HTTP 推送（读取下节的冻结描述符契约）；插件删除 watcher/锁/游标/自动认领，只保留五个工具；`extensions/shared/bridge-watcher.ts` 与 `agent_link/shared.py` 的 bridge/inbox helper 已删除；`tests/test_opencode_push.py` 提供 fake serve 模拟测试。切片 3 实现 runner 启动自管 serve 与描述符写入编排，并做真实隔离 serve/模型端到端（C 组）；在此之前 C 组保持 unknown。

## 原生能力与当前适配

| 操作 | OpenCode v2 原生 | 当前 Agent Link 适配 |
| --- | --- | --- |
| steer | prompt 的 `delivery:steer`，默认安全边界投递 | 未提供直接控制入口 |
| queue | prompt 的 `delivery:queue` | Python 适配器推送时使用 |
| interrupt | session interrupt | 未接入统一控制接口 |
| 事件订阅 | 原生流式接口 | 未接入直接事件流传输 |
| 双向请求/答复 | 插件工具 | 五个子端工具 + Python 原生 prompt 推送（描述符契约） |

Python 推送的失败映射：仅上游 2xx 且回显 `sessionID` 与目标一致（若含 `id` 也须等于 `message_id`）才记 `host_accepted`；401 为配置错误不重试；404 与其他非 2xx、超时、网络失败、身份不匹配均为 `failed_or_unknown`，不自动重发、无文件回退。仅同 `message_id`（= prompt `id`）允许幂等重放，上游首条 admission 生效。accept 不等于模型已处理。

## 描述符契约（切片 1 冻结，实现跟随）

宿主按 `$AGENT_LINK_SESSIONS`（默认 `~/.agent-link/sessions`）查找描述符；目录 0700、文件 0600：

```text
opencode/<session_id>.json           # 会话描述符：目标会话、serve 引用，不存密码
opencode/servers/<serve_id>.json     # serve 描述符：集成生成密码的唯一一份
```

会话描述符：`protocol=agent-link-opencode-push/1`、`framework=opencode`、`session_id`、`endpoint`（仅 `http://127.0.0.1:<port>`）、`pid`、`serve_id`、`managed=true`、`credential_kind=server_descriptor`，且不得出现 `password` 字段（C2）。

serve 描述符：`protocol`/`framework` 同值、`serve_id`、`endpoint`、`pid`（三者与会话描述符一致且进程存活）、`managed=true`、`credential_kind=inline_password`、`password`（≥16 字符）。

完整字段表、校验顺序与 fail-closed 规则见 [HOST-PROTOCOL §6.3](../../../docs/HOST-PROTOCOL.md)。测试用 fake serve 与写入 helper 见 `tests/opencode_server.py`。runner 写描述符的编排属于切片 3；当前手工/测试环境须自行保证拓扑与权限。

## 加载插件与启动

先读 [公共准备](../README.md#启动责任与公共准备)。保持整个 extensions 目录完整；不要单独复制 agent-link.ts。可在本机 OpenCode v2 的自动发现目录 `~/.config/opencode/plugin/` 放一个入口，绝对路径替换为实际 REPO：

```ts
// agent-link.ts：本机配置文件，不提交真实路径到共享配置
export { default } from "/absolute/path/to/agent-link/extensions/opencode/agent-link.ts";
```

这是配置操作说明；本次开发不自动修改用户的真实 OpenCode 配置。已有插件配置应合并，不覆盖。模型与认证通过 OpenCode 自身配置选择，不写进 binding；Agent Link 不直接调用模型 API。

子端凭据须在启动进程时提供；父端服务 ready 后执行：

```bash
cd "$WORK"
AGENT_LINK_CREDENTIALS="$ROOT/child-credentials.json" \
  opencode serve --hostname 127.0.0.1 --port 4096
```

端口由调用方选择，4096 仅为示例；端口被占用应明确失败。API 服务就绪不代表已有会话或模型运行：还需用 v2 客户端创建/选择目标会话，取得真实 session id，写入绑定后再发送任务。若父端是 OpenCode，服务选择 `--framework opencode`；若父端是 Codex 仍选择 codex-desktop。Python 推送还要求该 serve 的会话/serve 描述符已按上节写入（runner 编排属切片 3）。

`opencode serve` 本身没有模型选择参数，不把 Pi 的 `--provider/--model` 参数传给它。模型按 v2 会话的 model 配置选择；发任务调用框架 API，才会产生模型调用。

## 已有会话、就绪与限制

插件必须在服务启动时加载；仅启动一个独立 serve 不会自动接管用户已有 TUI 的服务。连接已有会话需使用持有该会话的服务及其真实 session id，不能另建服务后宣称已 attach；受支持的路径是集成自管的专用 serve + 描述符，attach 不声明支持。

Python 推送按权威 Binding 的 `session_id` 直接寻址，不再依赖插件侧会话跟踪；插件已在切片 2 删除 watcher 与自动认领，也不再选择或跟踪目标会话。一个专用服务/location 建议仅运行一个目标会话。核对插件加载、工具注册及实际目标 session，再通过父端持久化 reply/子端 answer 确认模型处理。五个子端工具与 OMP 相同。新 worktree 的模型端到端、忙碌投递、空闲唤醒与重启恢复仍未验证（unknown）。

## 清理

通过运行器停止本次专用 OpenCode 服务，再停止通信服务；不要停止用户自己的既有服务；只停自己启动的 serve（门禁 E1）。描述符残留必须显式核对旧进程已退出后再处理，不自动扩散。当前适配没有直接远程 interrupt/stop-child 实现，不能套用 Pi 命令。runner 启动 serve/写描述符的编排在切片 3 处理。
