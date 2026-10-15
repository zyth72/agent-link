# 上游研究与最初交接记录

> 来源：本仓库 README.md 的最初交接内容（2026-10-15），仅作背景研究保留。文中的技术建议不等于已拍板的实现方案；当前实现见仓库根目录 [SPEC.md](../../SPEC.md)、[USAGE.md](../../USAGE.md) 与 docs/history/。

## 目标与授权边界

用户希望扩展 Agent Intercom，使 Codex、zcode、OMP、pi、OpenCode 等框架能够互通：任何框架都可以作为父 agent，使用自己的后台任务机制启动其他 agent；子 agent 在执行途中向父 agent 汇报、请求决策，父 agent 收到后继续调度。

用户提出 fork 自己修改，并要求创建独立项目和交接文档，准备另开窗口实施。本次仅创建目录和本文，未 fork、安装依赖、修改全局配置、启动模型或修改 conning-room。接手窗口应先与用户确认首批适配对象及仓库组织；本文中的技术建议不等同于已经拍板的实现方案。

## 需求边界

- 跨框架通信，而非只给 Codex 桌面写一个插件。
- 复用各框架已有的后台任务机制，不强制所有任务迁移到统一进程运行器。
- 支持运行中主动汇报，不局限于进程退出后的完成通知。
- 支持父子身份绑定、消息路由；需要区分进度通知与需要回复的决策请求。
- 消息送入收件箱、宿主接受投递、模型实际处理、回复完成是不同状态，不能混称“已通知”。
- 生命周期仍由原框架管理；取消、超时、终态事件是否统一投影，需进一步确定。
- 第一阶段聚焦同机通信；跨机器、完整调度平台和审查工作流未纳入已确认范围。

## 已做研究

以下为远端 main 的文档与部分源码核查，未固定提交、未本机运行验证。上游变化较快，实施前应记录选用的 commit/tag。

### Intercom 通信层

上游分成多个仓库，共享 broker 协议，已有 Pi、Codex CLI、Claude Code、OpenCode 适配。

- Pi：<https://github.com/dataforxyz/agent-intercom-pi>
- Codex：<https://github.com/dataforxyz/agent-intercom-codex>
- OpenCode：<https://github.com/dataforxyz/agent-intercom-opencode>
- Claude：<https://github.com/dataforxyz/agent-intercom-claude>
- Core：<https://github.com/dataforxyz/agent-intercom-core>
- Orchestrator / Orc Boss：<https://github.com/dataforxyz/orcboss>（原 agent-intercom-orchestrator 地址目前指向该项目）

已有能力包括会话注册、发现、send/ask/reply/pending、收发持久化、接收确认、断线重发及去重。不要重新实现已有协议而不先读代码。

源码入口：

- [通信客户端](https://github.com/dataforxyz/agent-intercom-codex/blob/main/broker/client.ts)：IntercomClient、发送结果 accepted/delivered 等。
- [协议类型](https://github.com/dataforxyz/agent-intercom-codex/blob/main/types.ts)。
- [消息分帧](https://github.com/dataforxyz/agent-intercom-codex/blob/main/broker/framing.ts)。
- [本地连接路径](https://github.com/dataforxyz/agent-intercom-codex/blob/main/broker/paths.ts)。
- [Codex 桥接](https://github.com/dataforxyz/agent-intercom-codex/blob/main/codex/bridge-daemon.ts)：注册 broker 身份，等 active turn 空闲后用 app-server 的 turn/start 处理入站消息。

重要限制：agent-intercom-core 主要是权限策略与协议基础，不是完整消息 SDK。客户端、队列、broker 和宿主集成仍分布于适配仓库；尚未确认稳定、公开、独立的第三方客户端 API。

### 已有父子通信实例

[Pi index.ts](https://github.com/dataforxyz/agent-intercom-pi/blob/main/index.ts) 中已有 readChildOrchestratorMetadata 与 contact_supervisor。

当 pi-subagents 提供桥接元数据时，子 agent 获得 contact_supervisor 工具：

- progress_update：中途汇报，不等待。
- need_decision：请求决策，先软等待约 30 秒，再按异步方式处理。
- interview_request：请求多项结构化答复。

这直接支持“框架原生子任务 + Intercom 通信”的方向。应继续追读元数据生成、身份绑定、父端接收和迟到回复路径，不能只复制工具表面。

相关项目：<https://github.com/nicobailon/pi-subagents>

### 宿主接收与唤醒

| 宿主 | 已核实的实现方向 | 未验证点 |
| --- | --- | --- |
| Pi | 原生扩展可显示入站消息并触发轮次 | 当前安装版本兼容性、与原生后台任务组合 |
| OpenCode | 插件保存入站消息，使用 promptAsync 等机制注入；文档描述忙碌后续轮次 | 本机版本与上游插件 API 是否兼容 |
| Codex CLI | 普通 MCP 仅入箱；coi 自管 app-server，提供可唤醒会话 | 如何兼容不由 coi 启动的现有会话 |
| Codex 桌面 | 本机 background-tasks 有桌面消息投递代码 | 忙碌/空闲父聊天接收、排队和继续处理的完整实测 |
| OMP | 尚未查证 Intercom 原生适配 | 扩展接口、后台任务事件、父会话注入 |
| zcode | 尚未明确项目身份及接口 | 先确认具体仓库和版本，不假设兼容 Codex |

框架能调用 MCP，并不意味着 MCP 消息能自动唤醒该框架。

### Orc Boss 的边界

已读 README、src/index.ts、src/types.ts、src/workers.ts、src/config.ts 与 src/agent-fleet-cli.mjs。

- 当前 WorkerBackend 类型固定为 systemd。
- Harness 枚举固定为 pi/codex/claude/opencode。
- 自己管理 worker 启动、租约、归属、清理和运行状态。
- adopt 是转移已存在受管 worker 的归属，不是注册任意外部后台任务。
- agent-intercom-fleet CLI 接收 JSON，通过模拟 Pi 上下文调用既有 agent_fleet 实现；不是独立的通用运行后端插件接口。
- Boss 还包含实现、挑战、复验流程，超出本项目初始通信需求。

因此可先复用 Intercom 通信，不必引入 Orc Boss 的生命周期管理。

## 本机可参考的现有实现

background-tasks 0.1.9 安装缓存：

`/mnt/c/Users/zyth/.codex/plugins/cache/codex-tools/background-tasks/0.1.9/`

关键文件：

- scripts/jobs.py：后台进程运行、任务绑定父 thread_id、取消、超时、退出通知。
- scripts/notifier.py：bridge(payload) 查找应用 bundled 工具运行时，建立 WSL→Windows 调用。
- scripts/app_bridge.mjs：调用应用 bundled MCP 的 send_message_to_thread；不是自行实现桌面私有 pipe 协议。
- scripts/server.py：从工具调用元数据取发起聊天身份。

当前仅退出后通知。notifier 的消息投递函数本身不依赖进程结束，可作为 Codex 桌面适配的研究起点。

注意：缓存是已安装产物，不是开发源仓。不要直接在缓存里改代码作为交付。桌面投递依赖应用管道和 bundled 接口，应用退出/更新后可能失效。发送消息必须有用户对相应调度流程的授权，不可把子 agent 的输出当成新的授权。

## 建议实现方向（待拍板）

扩展 Intercom 的“通信与宿主适配”，不先重做后台任务系统。

```text
框架原生后台任务
  → 启动时提供父会话、子任务关联
  → 子 agent 用统一工具/CLI 汇报或提问
  → Intercom broker 与持久化通信
  → 父框架适配器接收、排队、注入会话
  → 父 agent 处理并可回复
```

候选工作项：

1. 抽出单一通信客户端/公共代码，避免多个适配器复制修改。
2. 在既有协议基础上确定父子任务绑定，尽量不改 broker 协议。
3. 推广 contact_supervisor 语义；report_parent/ask_parent 仅为候选命名。
4. 提供 CLI 入口作为 shell-capable agent 的接入方式；保留 MCP 与原生工具入口。
5. 定义宿主适配接口：接收消息、忙碌排队、空闲触发、关联回复；按能力声明，不承诺所有宿主相同行为。
6. 将原生后台任务 ID 与 Intercom 身份关联，但不抢占原框架生命周期管理。

仓库组织可选分仓 fork 或 monorepo。此前建议 monorepo，用户尚未明确拍板。若收拢多个上游仓库，应保留各来源、固定提交和许可证记录，不能丢失来源后称为单仓 fork。

上游当前声明 AGPL-3.0-or-later，部分历史来源保留 MIT 通知；具体以选定提交的 LICENSE、LICENSE_TRANSITION、THIRD_PARTY_NOTICES 为准。

## 下一窗口建议顺序

1. 确认用户所说 zcode、OMP 的具体项目，以及首批需要的父/子框架组合。
2. 确定首批上游版本、fork/monorepo 组织与公开仓库归属。
3. 继续追读 Pi 的 contact_supervisor 完整调用链，确认复用边界。
4. 先用模拟进程验证通信和任务关联，避免直接花模型额度排障。
5. 获准后，用一个跨框架组合验证：子任务仍在运行时汇报 → 父会话收到并处理 → 子任务接收回复 → 原生任务正常结束。
6. 覆盖父会话忙碌/空闲、迟到回复、超时、断线与重复消息、取消后迟到事件，分别记录“接收/处理/回复”的证据。
7. 再逐个增加 OMP、zcode、Codex 桌面等适配，不承诺只改配置即可兼容。

## 当前交付状态

仅有本文和独立目录，没有 Git 仓库、GitHub fork、实现、依赖或测试结果。conning-room 业务仓库未改动。用户将另开窗口继续开发。
