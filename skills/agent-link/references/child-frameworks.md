# 子端框架：启动、能力与唤醒

本文件只在启动/配置子端或判断 `ask-child` 能否唤醒时读。协议细节见 `ABS_REPO/docs/HOST-PROTOCOL.md`，能力声明以 `capabilities` 与实际测试为准，不要只改 framework 字符串就声称支持。

## 共同点

- 子端只持父端宿主服务生成的凭据文件（`AGENT_LINK_CREDENTIALS`，内容为 loopback endpoint + token）；不能 `bind`、不能 `reply`、不能指定父会话。
- 父端要先用 `bind` 创建绑定，再用 `python3 -m agent_link.host` 启动服务并写凭据；子端启动前确认凭据已就绪。
- 扩展在**会话启动时**加载，运行中无法补挂；必须保持整个 `extensions/` 目录完整（各适配器依赖 `extensions/shared/`）。
- 通知/事件投递是 at-least-once；`host_accepted` 不代表模型已处理。

## Pi

- 父端 watcher：`extensions/pi/agent-link.ts`，可显式设 `AGENT_LINK_BRIDGE_FILE`/`AGENT_LINK_BINDING`，否则按注册表自动认领（`parent.session_id` 匹配本会话）。
- 子端工具：`report_parent`、`ask_parent`、`check_parent_request`。
- 启动必须显式加载扩展，不要只写 `--no-extensions`：

  ```text
  AGENT_LINK_CREDENTIALS=ABS_RUNTIME/child-credentials.json \
  PI_BIN -p --session-id CHILD_SESSION_ID --extension ABS_REPO/extensions/pi/agent-link.ts '<任务>'
  ```

- Pi 原生支持 RPC 的 `steer` / `follow_up`，但本适配暂未持有 RPC 控制通道，不把框架能力等同于已接入能力。将来 RPC 仅用于专用 Pi 子进程，父端保留用户交互开发会话，不要求父端进入 RPC 模式。
- **没有子端 inbox watcher**：`ask-child` 写入 `<root>/inbox/<binding>.jsonl` 不会自动唤醒 Pi；子端需用 `python3 LINK --root ABS_RUNTIME answer ...` 主动答复（`answer` 是核心 CLI，不是 Pi 工具）。

## OMP

- 父端驱动：宿主服务 `--driver agent_link.hosts.omp:OmpHost`，通知写入 `<root>/omp-bridge/<binding>.jsonl`。
- 同一扩展 `extensions/omp/agent-link.ts` 同时提供父端 watcher（父桥）与子端 watcher（inbox），父子角色都能按注册表自动认领；子端工具五个：`report_parent`、`ask_parent`、`check_parent_request`、`poll_parent_requests`、`answer_parent`。
- 加载：`--extension ABS_REPO/extensions/omp/agent-link.ts`，或登记进 `~/.omp/agent/config.yml` 的 `extensions:`。**保持 `extensions/shared/` 路径完整**，扩展引用共享客户端与 watcher。
- `ask-child` 可唤醒 OMP 子端（无模型验收已覆盖）；忙碌会话注入与轮次结束唤醒仍为 unknown。

## OpenCode

- 插件 `extensions/opencode/agent-link.ts` 是 v2 插件（本机验证 2.0.29，v1 形态被拒绝）；由 OpenCode 插件配置加载；部署时须保持 `../shared/` 相对导入可用，不能只复制单个文件。
- 五个子端工具与 OMP 同名同语义；watcher 复用共享模块，支持注册表自动认领；父端驱动 `agent_link.hosts.opencode:OpenCodeHost`。
- LLM 端到端、忙碌/唤醒细节未验证；server 级多会话归属是近似。按 `ABS_REPO/docs/HOST-PROTOCOL.md` 与插件文件头注释说明，不编造。

## Codex 子端

尚无适配器与 inbox watcher。父端 `ask-child` 不会自动唤醒，不要承诺；Codex 只作为父端（见 [codex-parent.md](codex-parent.md)）。

## 唤醒矩阵

| 子端 | report/ask 工具 | inbox 自动唤醒（ask-child） |
| --- | --- | --- |
| Pi | 有（3 工具） | 否，需 CLI `answer` |
| OMP | 有（5 工具） | 是（watcher，无模型验收） |
| OpenCode | 有（5 工具） | 是（watcher，无模型验收） |
| Codex | 无 | 否（无适配器） |
