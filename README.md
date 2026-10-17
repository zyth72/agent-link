# Agent Link

跨框架 agent 任务通信薄层：父/子会话绑定、运行中进度汇报（`report`）、决策请求与阻塞等待答复（`ask` / `reply`）。任务的生命周期（启动、取消、超时、退出通知）仍由各框架自己的后台任务机制管理；本层不实现进程运行器，也不要求 Intercom broker。

- Python 标准库实现，无第三方运行依赖。
- SQLite 单文件持久化，支持多个 CLI 进程并发。
- 粘性绑定与消息幂等：绑定不可重定向，重复提交不重复投递，冲突显式报错。
- 可选父端宿主服务 + 子端 Pi/OMP 原生工具：父端用旧 CLI 数据库，子端只持 loopback endpoint/token。
- 当前已验证组合：Codex 桌面父端 + Pi CLI 子端；OMP 父/子适配（含桥文件通知与轮询注入）已完成无模型验收（同机、同一 OS 用户，Linux/WSL）。

## 状态

Pi/OMP 的正式扩展使用会话 HTTP 主动推送，无文件轮询；OpenCode 使用框架原生 prompt HTTP 推送（描述符契约，见 [HOST-PROTOCOL §6.3](docs/HOST-PROTOCOL.md)）。Pi 专用子端还可使用 RPC。框架能力、启动、关联管理与清理见 [接入指南](agent_link/hosts/README.md) 和 [主动通知协议](docs/SESSION-PUSH.md)。

模型端到端：真实 DeepSeek 已验证 Pi/OMP 交互父端的空闲通知与 ask/reply，以及 OMP 子端的 ask-child/answer。模拟测试、真实接口和模型验收分别记录，忙碌/中断/重连行为未验证时保持 unknown，不承诺 exactly-once。

## 前置安装与接入

核心（CLI、SQLite 状态机、宿主服务）只依赖 Python ≥ 3.9 标准库，无需安装任何 Python 包；下列依赖只在启用相应框架适配时需要（括号为本机验证版本）。

| 适配 | 前置安装 | 接入方式 |
| --- | --- | --- |
| 子端（任意 shell-capable agent / 脚本） | Python ≥ 3.9 + 本仓库 | `python3 -m agent_link ...`；或把 `skills/agent-link/scripts/link.py` 的路径交给子端 |
| Codex 桌面父端（推荐配套） | [codex-background-tasks](https://github.com/zyth72/codex-background-tasks) 插件（提供 `run_cli` 与 `notifier.py` 桌面桥），桌面应用存活 | 绑定的 `notifier_path` 指向该插件的 `notifier.py`；由 `run_cli` 启动宿主服务与子任务 |
| Pi 父端 / 子端 | Pi（本机验证 1.2.0） | 用户级安装一次（见下），或临时 `pi -p --no-extensions --extension <repo>/extensions/pi/agent-link.ts`；子端另需 `AGENT_LINK_CREDENTIALS=<凭据文件>`；父端：同一扩展按 session 注册本地 HTTP 接口，主动推送，无需轮询 |
| OMP 父端 / 子端 | OMP（本机验证 18.11.1，Bun 运行时；更旧版本未验证） | 会话启动时加载 `extensions/omp/agent-link.ts`（见下）；父端与子端按实际 session 登记 HTTP 接口，子端另需 `AGENT_LINK_CREDENTIALS` |
| OpenCode 父端 / 子端 | OpenCode（本机验证 2.0.29；只接受 v2 插件形态，v1 会被拒绝） | 插件 `extensions/opencode/agent-link.ts` 放入 `~/.config/opencode/plugin/` 自动发现；子端另需 `AGENT_LINK_CREDENTIALS`；父/子推送另需集成自管 serve 及其会话/serve 描述符（`AGENT_LINK_SESSIONS`，切片 3 提供启动编排） |

### OMP 扩展安装

扩展模块只在**会话启动时**加载，把路径登记进配置后每个新会话自动带上（运行中的会话无法补挂）：

```yaml
# ~/.omp/agent/config.yml（或项目 <cwd>/.omp/config.yml）
extensions:
  - /abs/path/to/agent-link/extensions/omp/agent-link.ts
```

也可每次显式加载：`omp --no-extensions -e /abs/path/to/agent-link/extensions/omp/agent-link.ts`。扩展从仓库加载，须保持 `extensions/` 目录完整（`omp/agent-link.ts` 依赖 `shared/agent-link-client.ts`）。
- 父端与子端通知：扩展在 session_start 注册本地 HTTP 接口，服务或父端 CLI 按 binding 的真实 session 直接推送；session_shutdown 清理。连接目录由 AGENT_LINK_SESSIONS 指定，默认 ~/.agent-link/sessions；无需桥文件、inbox 或定时扫描。
- 子端工具（`report_parent` / `ask_parent` / `check_parent_request`，与 Pi 版同名同语义）：只需 `AGENT_LINK_CREDENTIALS=<凭据文件>`，由父端宿主服务在绑定创建后写出。

### Pi 扩展安装

同样只在会话启动时加载。Pi 自动发现 `~/.pi/agent/extensions/`（或项目 `.pi/extensions/`）下含 `index.ts` 的目录；因扩展用相对引用 `../shared/*`，用户级安装用一个不含逻辑的转发入口（仓库布局不动）：

```ts
// ~/.pi/agent/extensions/agent-link/index.ts
export { default } from "/abs/path/to/agent-link/extensions/pi/agent-link.ts";
```

临时使用则每次显式加载：`pi --no-extensions --extension /abs/path/to/agent-link/extensions/pi/agent-link.ts`。卸载：删掉 `agent-link/` 目录；更新：git pull 仓库即可。OMP/Pi 每个会话都会带上扩展，未认领绑定的会话只有约 2s 一次的注册表扫描，无其他副作用。

命令、限制与协议细节见 [USAGE.md](USAGE.md) 与 [docs/HOST-PROTOCOL.md](docs/HOST-PROTOCOL.md)。

### 父端 skill 安装

`skills/agent-link/` 是共享壳（通用规则与 `scripts/link.py`），父端流程按宿主分册：`skills/agent-link-codex/`（Codex 桌面）、`skills/agent-link-omp/`（OMP）。安装时共享壳与当前宿主的分册都要对父端可见；独立安装副本由安装方在 `link.py` 旁写入 `repo-location.json`（本机产物，不入库）。OMP 用户级技能目录为 `~/.agents/skills/`（可用软链接指向仓库内分册）；Codex 按既有同步方式复制 skill 目录。

### Codex 插件与 SessionStart hook（连接恢复）

仓库根包含最小 Codex 插件清单 `.codex-plugin/plugin.json`，显式指向 `hooks/hooks.json`。SessionStart hook 匹配 `startup|resume|clear|compact|fork`，读取 JSON stdin 的 `hook_event_name/session_id/source`，用当前继承的桌面管道环境登记父 session；不需要模型，也不依赖 cwd：

```sh
python3 "$PLUGIN_ROOT/hooks/session_start.py"
```

安装与信任步骤（需要用户在真实 Codex 中操作）：

1. 将仓库根作为本地插件源加入 Codex（或复制/链接整个仓库根，至少包含 `.codex-plugin/`、`hooks/`、`agent_link/`、`skills/`）。
2. 显式信任/批准 Agent Link 的 hooks。官方插件机制要求用户信任，未信任时 hook 不会运行。
3. 启动或 resume 会话后，可用 `python3 -m agent_link.hosts.codex.cli --session-id CURRENT connection-status` 查看安全的本地描述符元数据；它不显示管道地址，也不证明传输/进程存活。
4. 如果实际版本没有把私有 `CODEX_APP_TOOLS_PIPE_PATH` 传给 hook，在父会话当前工具环境中手动兜底：
   `python3 -m agent_link.hosts.codex.cli --session-id CURRENT register-connection`
   （不需要已有 binding）。

连接描述符保存在 `~/.agent-link/codex-connections/`（`AGENT_LINK_CODEX_CONNECTIONS` 可覆盖；目录 0700、文件 0600），只存白名单传输变量，不存完整环境/凭据/消息。通知每次读取最新描述符，复制环境后只覆盖传输字段，不修改 `os.environ`；旧宿主无需重启即可使用新登记管道。官方文档未保证私有管道 env 会被真实 SessionStart 生命周期继承，需用户信任 hook 后用真实 app resume 验证，当前保持 unknown。失败消息只在**桥调用前**可证明未提交时写结构化 `not_sent`；`recover-notifications` 显式、原子地只重放这些消息，跳过过期/已回答 ask，不重试 unknown，也不提供 force 或 exactly-once。

## 项目结构

```text
agent_link/          # Python 包：CLI、SQLite 状态机、通知适配器、宿主服务与 HostDriver
  host.py            # 父端 binding 级 loopback 服务（python3 -m agent_link.host）
  registry.py        # 用户级绑定索引（~/.agent-link/registry/）：bind/服务写入，适配器按 session id 认领
  hosts/base.py     # ParentAdapter、ChildAdapter 与统一入口
  hosts/codex/      # Codex 父端适配与 cli.py
  hosts/pi/         # Pi 父子适配与 RPC 控制器
  hosts/omp/        # OMP 父子适配
  hosts/opencode/   # OpenCode 父子适配（原生 prompt HTTP 推送；描述符契约）
  hosts/codex/connection.py # Codex 每 session 桌面连接描述符（白名单传输 env）
  shared.py         # 公共消息渲染与文件桥工具（OpenCode Python 适配已不引用）
extensions/
  shared/agent-link-client.ts            # Pi/OMP/OpenCode 共用的子端 HTTP 客户端
  shared/session-push.ts                 # Pi/OMP 会话 HTTP 接收接口
  pi/agent-link.ts                       # Pi 子端四工具 + 会话推送
  omp/agent-link.ts                      # OMP 五个工具 + 会话推送
  opencode/agent-link.ts                 # OpenCode v2 插件：五个子端工具（消息投递由 Python 适配器原生 prompt 推送）
hooks/               # Codex SessionStart hook（connection 登记；无模型）
.codex-plugin/       # 最小 Codex 插件清单（skills + hooks 路径）
tests/               # unittest 测试（模拟 notifier/driver，不发真实桌面消息）
  pi/                # Node/jiti 扩展测试与真实 HTTP 闭环驱动脚本
  omp/               # Bun 扩展测试 harness 与桥验收驱动脚本
skills/agent-link/        # 共享壳 skill（通用规则）+ 可从任意 cwd 调用 CLI 的 link.py
skills/agent-link-codex/  # Codex 桌面父端分册 skill
skills/agent-link-omp/    # OMP 父端分册 skill
docs/
  HOST-PROTOCOL.md   # 宿主服务 HTTP 协议、HostDriver 扩展方式、Pi/OMP --extension 用法
  CODEX.md           # Codex 父端 helper：注册表发现、只读校验、宿主服务启动顺序
  BACKGROUND.md      # 上游研究与最初交接记录（背景资料）
  history/           # 实施、父端复审与边界修复历史
  experiments/       # 去敏后的通知/汇报/反向回复实测记录
SPEC.md              # Agent Link 薄层规范 v0.1
USAGE.md             # CLI 用法、状态模型、退出码、宿主服务与 OMP 启动
```

`experiments/`、`runtime/` 与根目录运行日志是本机数据，已由 `.gitignore` 排除：原始日志、SQLite 数据库和含聊天信息的运行产物不提交、不发布。

## Codex 推荐配套：background-tasks

Codex 桌面 + WSL 推荐先安装独立项目 [codex-background-tasks](https://github.com/zyth72/codex-background-tasks#安装)（MIT）。它提供后台启动、查询、取消、超时、完成通知和任务面板；Agent Link 提供运行中汇报、提问与回复。当前 Codex 宿主驱动还复用它的 `notifier.py` 桌面桥，Pi 子端不需要安装这个插件。

安装方式、Python 版本与桌面限制以配套仓库说明为准。安装后重新加载 Codex，确认 `run_cli` 可用，再继续下面的绑定与宿主设置。绑定的 `notifier_path` 使用当前安装中的实际路径，不复制其他电脑的用户名或缓存版本。

父端发现、查询与回复推荐使用 `python3 -m agent_link.hosts.codex.cli`（按本会话 session id 从注册表发现绑定并只读校验，不接受消息里的 `--root`），完整流程见 [docs/CODEX.md](docs/CODEX.md)。

在 Codex 中可以说：“使用 agent-link，通过 background-tasks 启动 Pi，过程中汇报，需要决定时向父 agent 提问。”

## 快速开始

```bash
cd /path/to/agent-link

# 1. 绑定（--root 必须放在子命令之前；config 见 USAGE.md）
python3 -m agent_link --root /abs/runtime bind --config /abs/binding.json

# 2. 子端主动汇报，不等待
python3 -m agent_link --root /abs/runtime report \
  --binding live-001 --message-id progress-001 --text "实现完成 60%"

# 3. 子端请求决策并阻塞等待；成功 exit 0，超时 exit 3
python3 -m agent_link --root /abs/runtime ask \
  --binding live-001 --request-id decision-001 --message-id question-001 \
  --question "请选择方案 A 或 B" --timeout 240

# 4. 父端回复（不能覆盖已有答复）
python3 -m agent_link --root /abs/runtime reply \
  --binding live-001 --request-id decision-001 \
  --message-id parent-reply-001 --answer "选择 A"

# 5. 查询请求持久化状态
python3 -m agent_link --root /abs/runtime get-request \
  --binding live-001 --request-id decision-001

# 6. 父端反向请求子端答复（写入子端 inbox；OMP 子端自动认领后被唤醒）
python3 -m agent_link --root /abs/runtime ask-child \
  --binding live-001 --request-id pq-001 --message-id pq-msg-001 \
  --question "补充一下你改动文件的清单" --timeout 240

# 子端答复（OMP 子端也可用 answer_parent 工具）
python3 -m agent_link --root /abs/runtime answer \
  --binding live-001 --request-id pq-001 --message-id child-answer-001 --answer "改动了 a.py 和 b.py"
```

所有命令 stdout 恰好一条 JSON，诊断写 stderr。退出码：`0` 成功、`1` 内部错误、`2` 用法错误、`3` ask 超时、`4` 冲突、`5` 校验失败、`6` 不存在、`7` 投递失败或未确定。能力声明请运行 `capabilities`。

## 宿主服务 + Pi 子端（可选）

父端需要让运行中的 Pi 子端通过原生工具汇报/提问时，在**父端环境**启动 binding 级服务（前置：上面第 1 步的 binding 已创建）：

```bash
# 父端：启动 loopback 服务并把凭据文件交给子端（写入 0600；已存在不覆盖）
python3 -m agent_link.host --root /abs/runtime --binding live-001 \
  --credentials /abs/runtime/child-credentials.json

# 子端：只需凭据文件，不需要 Codex/桌面依赖
AGENT_LINK_CREDENTIALS=/abs/runtime/child-credentials.json \
  pi -p --no-extensions --extension /path/to/agent-link/extensions/pi/agent-link.ts '<任务>'
```

子端工具只有 `report_parent` / `ask_parent` / `check_parent_request`，无法指定父会话、`bind` 或 `reply`；父端仍用上面的旧 `reply` CLI 答复同一数据库。协议、错误码、资源上限、断开后请求保留、HostDriver 扩展方式见 [docs/HOST-PROTOCOL.md](docs/HOST-PROTOCOL.md)。

## 测试

```bash
cd /path/to/agent-link
python3 -W error::ResourceWarning -m unittest discover -s tests -t . -v
```

Python 测试使用临时目录中的 stub notifier/driver，不会发送真实桌面消息；Pi 扩展测试通过 jiti 加载真实扩展并用真实 `agent_link.host` + 模拟父端闭环，不调用模型。

## 打包与安装

`pyproject.toml` 仅声明打包元数据（版本动态取自 `agent_link.__version__`），无第三方运行依赖。需要命令行入口时可选用：

```bash
python3 -m pip install .          # 提供 agent-link 命令；不安装额外依赖
python3 -m pip install -e . --no-deps   # 开发模式
```

也可不安装，直接在仓库根运行 `python3 -m agent_link ...`；`skills/agent-link/scripts/link.py` 默认自动定位本仓库，或用 `AGENT_LINK_REPO` 指定仓库根目录。

## 文档

- [术语约定](docs/TERMINOLOGY.md)：与 AI 沟通时统一概念和命名；[AGENTS.md](AGENTS.md) 是 AI 项目入口。

- [SPEC.md](SPEC.md)：接口、能力声明、持久化与失败语义。
- [USAGE.md](USAGE.md)：CLI 用法、状态模型、幂等规则、宿主服务启动与已知限制。
- [docs/HOST-PROTOCOL.md](docs/HOST-PROTOCOL.md)：宿主服务 HTTP 协议、HostDriver 扩展、Pi/OMP `--extension` 用法与未验证项。
- [docs/history/IMPLEMENTATION-RESULT.md](docs/history/IMPLEMENTATION-RESULT.md)：实现与真实反向回复验收记录（去敏版）。
- [docs/history/REVIEW-FIXES.md](docs/history/REVIEW-FIXES.md)：父端独立复现的四项边界缺陷与回归要求。
- [docs/experiments/notification-smoke/](docs/experiments/notification-smoke/)：三轮通信实测记录（去敏版）。
- [docs/BACKGROUND.md](docs/BACKGROUND.md)：Intercom 上游生态与本机既有实现的研究记录。

## 安全边界与限制

- 同一 OS 可信用户模型：runtime 目录 0700、数据库 0600、子端凭据 0600；不防范恶意同用户进程。
- 适配器只加载绑定时指定的本机 `notifier.py`；消息内容仅作数据传递，不作为代码执行。宿主服务只监听 127.0.0.1，单 binding/单 token；子端不能 `bind`/`reply`/指定父会话。
- 依赖父端宿主消息管道存活；宿主服务必须由父端环境启动（当前 Codex 驱动依赖 background-tasks 桌面桥）。投递成功仅表示宿主接口接受（`host_accepted`），不代表模型已处理；模型是否处理由父端实际答复证明。连接登记/hook 成功也不是存活检查。
- 不自动重试通知，不实现 exactly-once 外部投递；仅对桥调用前可证明未提交的失败提供显式人工恢复，unknown 永不自动重放。墙钟回拨只能记录 `wall_clock_jump_observed`，不能消除影响。
- v0.1 不支持 `spawn` / `cancel`，不能绑定未启动的任务，不实现自动清理。

## 许可证

本项目采用 [MIT License](LICENSE)。外部依赖及所引用上游项目遵循各自的许可证。
