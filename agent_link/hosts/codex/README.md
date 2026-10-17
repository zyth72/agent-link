# Codex Desktop 接入：能力与启动

入口 `get_adapters()` 提供父端，`child=None`。本适配不提供 Codex 子端启动、steer、queue 或 interrupt；不要把桌面父端通知能力理解为子端控制能力。

## 父端接入

1. 安装并配置 codex-background-tasks 的通知桥，取得本机实际 `notifier.py` 路径。
2. 使用当前 Codex 聊天的可信 session id 作 `parent.session_id`，`parent.framework=codex-desktop`；notifier 路径写进 binding。
3. 按 [公共准备](../README.md#启动责任与公共准备) 创建绑定、用后台运行器启动通信服务，选择 `--framework codex-desktop`。
4. 等服务 ready 后，按 [Pi](../pi/README.md)、[OMP](../omp/README.md) 或 [OpenCode](../opencode/README.md) 启动子端。

Codex 父会话就是用户正在开发的聊天，不额外启动一个 Codex RPC 进程。无需为通信层配置模型：父会话模型由 Codex 配置，子端模型由对应框架配置。

## 使用与连接已有绑定

从 REPO 执行，`PARENT_SESSION_ID` 为本聊天实际标识：

```bash
python3 -m agent_link.hosts.codex.cli --session-id "$PARENT_SESSION_ID" list
python3 -m agent_link.hosts.codex.cli --session-id "$PARENT_SESSION_ID" \
  --binding "$BINDING" child-status
```

list 从注册表发现并只读核对 SQLite 中的父会话归属；多条匹配必须指定 binding。child-status 当前针对 Pi RPC，不代表所有框架都实现了健康检查。回复、请求示例见 [Codex 指南](../../../docs/CODEX.md)。


## 连接登记、旧管道与离线恢复

Codex 应用重启会换掉私有桌面管道；长驻宿主进程继承的
`CODEX_APP_TOOLS_PIPE_PATH` 可能已失效。适配器不再使用进程全局环境作为唯一来源：

- 当前父会话的连接登记在 `~/.agent-link/codex-connections/<session_id>.json`
  （`AGENT_LINK_CODEX_CONNECTIONS` 可覆盖；目录 0700、文件 0600）。只保存
  protocol/session/generation/update time 和
  `CODEX_APP_TOOLS_PIPE_PATH`、`CODEX_APP_TOOLS_CALLER_HOST_ID` 两个白名单变量；
  不保存完整环境、模型密钥、凭据或消息，stdout 不显示管道地址。
- 每次通知重新读取最新描述符，复制当前环境后只覆盖传输字段并调用
  `bridge(payload, env=...)`；不修改 `os.environ`。同一 session 同一环境登记幂等，
  新管道原子递增 generation。另一个 session 不能重定向本 session。
- 无描述符时保持旧 `bridge(payload)` 行为。有描述符但 bridge 不接受 `env`、
  描述符损坏或身份不匹配时明确失败，不静默回退旧管道。env-aware bridge 在没有
  描述符且没有继承管道时也会在调用前明确判定为未提交。
- 登记只表示连接信息存在，**不表示**管道/进程存活或模型已处理。

### SessionStart hook 安装与信任

仓库根有最小插件清单 `.codex-plugin/plugin.json`，显式指向 `hooks/hooks.json`。
SessionStart hook 匹配 `startup|resume|clear|compact|fork`，命令为
`python3 "$PLUGIN_ROOT/hooks/session_start.py"`；它读取 JSON stdin 的
`hook_event_name/session_id/source`，使用当前继承的管道环境登记，不依赖 cwd、
不需要模型；缺少管道只写安全诊断且不覆盖旧描述符。

1. 在 Codex 插件设置中加入仓库根为本地插件源（或复制/链接整个仓库根，至少包含
   `.codex-plugin/`、`hooks/`、`agent_link/`、`skills/`）。
2. 显式信任/批准 Agent Link 的 hooks；未信任时 Codex 不会执行 hook。
3. 启动或 resume 会话后运行
   `python3 -m agent_link.hosts.codex.cli --session-id CURRENT connection-status`
   查看本地元数据；该命令不探测传输存活。
4. 若当前版本未把私有管道环境传给 hook，在父会话当前工具环境手动兜底：
   `python3 -m agent_link.hosts.codex.cli --session-id CURRENT register-connection`
   （不需要已有 binding）。

官方插件/hook 文档确认 hook 发现、显式路径和信任要求；私有管道环境是否被真实
SessionStart 生命周期继承**未由官方文档保证**，需用户在信任 hook 后用真实 app
resume 验证，当前保持 unknown。测试只调用 fake stdin/临时环境，不修改真实 Codex
配置、安装缓存或信任状态。

### 未 accepted 消息与安全恢复

`notifications` 在 helper 完成注册表 + SQLite 只读归属校验后列出本 binding 未
accepted 的 report/ask：原始报告文本、request id、问题、deadline、答复状态。
`recover-notifications` 只重放桥调用前可证明未提交的失败（结构化
`certainty=not_sent`），使用跨进程 SQLite compare-and-set 领取；保留消息 id 与
deadline，跳过过期/已回答 ask，不重放 host_accepted/dispatching/unknown，无
force-unknown。桥调用后的 ENOENT/超时/异常永远是 unknown，不按错误字符串匹配，
也不自动重试。恢复成功仍只是 `host_accepted`，模型处理必须由真实回复证明。

## 就绪、清理与验证范围

通信服务 ready 证明 HTTP 入口就绪，通知桥 `host_accepted` 只证明宿主接受投递，实际父端回复才证明模型处理；连接登记和 `connection-status` 也不是健康检查。结束时先停止子端，再取消通信服务任务。不同 Codex 版本的忙碌排队与唤醒行为未验证时保持 unknown；SessionStart hook 是否继承私有管道同样等待真实 app resume 验证。

通知桥与运行器：[codex-background-tasks](https://github.com/zyth72/codex-background-tasks)。不要在验证中改真实 Codex 配置。
