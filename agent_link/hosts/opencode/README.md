# OpenCode 接入：能力与启动

入口 `get_adapters()` 提供父端和子端。插件针对 OpenCode **v2**；本机版本 2.0.29。旧版 `/session/:id/prompt_async` 示例不用于本版。

## 原生能力与当前适配

| 操作 | OpenCode v2 原生 | 当前 Agent Link 适配 |
| --- | --- | --- |
| steer | prompt 的 `delivery:steer`，默认安全边界投递 | 未提供直接 HTTP 控制入口 |
| queue | prompt 的 `delivery:queue` | 现有插件 watcher 用 queue 注入 |
| interrupt | session interrupt | 未接入统一控制接口 |
| 事件订阅 | 原生流式接口 | 未接入直接事件流传输 |
| 双向请求/答复 | 插件工具 | 已有工具 + 文件桥 watcher |

当前路径仍会轮询文件，不能因为框架有 HTTP/事件接口就声明适配已无轮询。原生规范见 [v2.0.29 Session](https://github.com/anomalyco/opencode/blob/v2.0.29/specs/v2/session.md)，API 见 [协议定义](https://github.com/anomalyco/opencode/blob/v2.0.29/packages/protocol/src/groups/session.ts)。原生 prompt 是 `POST /api/session/:sessionID/prompt`，接受不等于模型已处理。

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

端口由调用方选择，4096 仅为示例；端口被占用应明确失败。API 服务就绪不代表已有会话或模型运行：还需用 v2 客户端创建/选择目标会话，取得真实 session id，写入绑定后再发送任务。若父端是 OpenCode，服务选择 `--framework opencode`；若父端是 Codex 仍选择 codex-desktop。

`opencode serve` 本身没有模型选择参数，不把 Pi 的 `--provider/--model` 参数传给它。模型按 v2 会话的 model 配置选择；发任务调用框架 API，才会产生模型调用。

## 已有会话、就绪与限制

插件必须在服务启动时加载；仅启动一个独立 serve 不会自动接管用户已有 TUI 的服务。连接已有会话需使用持有该会话的服务及其真实 session id，不能另建服务后宣称已 attach。

现有插件以 location 最近活跃的顶层会话为跟踪目标，多会话归属存在限制；建议一个专用服务/location 仅运行一个目标会话。核对插件加载、工具注册及实际目标 session，再通过 answer_parent 的持久化答复确认模型处理。五个子端工具与 OMP 相同。新 worktree 的模型端到端、忙碌投递效果仍为 unknown。

## 清理

通过运行器停止本次专用 OpenCode 服务，再停止通信服务；不要停止用户自己的既有服务。当前适配没有直接远程 interrupt/stop-child 实现，不能套用 Pi 命令。后续接入原生 HTTP/事件传输时，应同步更新本文件与 capabilities，分别记录模拟、真实接口和模型验收证据。
