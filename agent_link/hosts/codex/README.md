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

## 就绪、清理与验证范围

通信服务 ready 证明 HTTP 入口就绪，通知桥 `host_accepted` 只证明宿主接受投递，实际父端回复才证明模型处理。结束时先停止子端，再取消通信服务任务。不同 Codex 版本的忙碌排队与唤醒行为未验证时保持 unknown。

通知桥与运行器：[codex-background-tasks](https://github.com/zyth72/codex-background-tasks)。不要在验证中改真实 Codex 配置。
