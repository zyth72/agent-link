# Agent Link

跨框架 agent 任务通信薄层：父/子会话绑定、运行中进度汇报（`report`）、决策请求与阻塞等待答复（`ask` / `reply`）。任务的生命周期（启动、取消、超时、退出通知）仍由各框架自己的后台任务机制管理；本层不实现进程运行器，也不要求 Intercom broker。

- Python 标准库实现，无第三方运行依赖。
- SQLite 单文件持久化，支持多个 CLI 进程并发。
- 粘性绑定与消息幂等：绑定不可重定向，重复提交不重复投递，冲突显式报错。
- 可选父端宿主服务 + 子端 Pi 原生工具：父端用旧 CLI 数据库，子端只持 loopback endpoint/token。
- 当前已验证组合：Codex 桌面父端 + Pi CLI 子端（同机、同一 OS 用户，Linux/WSL）。

## 状态

v0.1，仓库内 46 个测试全部通过（`python3 -W error::ResourceWarning -m unittest discover -s tests -t . -v`）。

已验证：绑定/幂等/冲突、运行中 `report`、阻塞 `ask` 与父端 `reply` 反向回复、超时与迟到答复、数据库重开恢复、并发写入冲突、通知失败不重投；以及宿主服务 loopback 协议（超时上限、body/连接限制、统一 JSON 错误、并发/失败/跨 binding/未知字段、重启重放不重投）与 Pi 扩展（jiti 加载真实扩展、伪造 ExtensionAPI、真实 HTTP + 模拟父端闭环、`pi --help` 无模型加载验证）。

仍为未知：父端忙碌时排队、父轮次结束后的唤醒、应用重启后的可靠性、exactly-once 外部投递、新宿主服务的真实桌面通知与 LLM 端到端工具调用。未适配其他宿主；不要只修改 `framework` 字符串就声称新框架受支持。

## 项目结构

```text
agent_link/          # Python 包：CLI、SQLite 状态机、通知适配器、宿主服务与 HostDriver
  host.py            # 父端 binding 级 loopback 服务（python3 -m agent_link.host）
  hosts/codex.py     # Codex 桌面驱动；真实依赖 background-tasks 桌面桥
  adapter.py         # v0.1 兼容导入
extensions/pi/agent-link.ts              # Pi 子端原生工具（report_parent/ask_parent/check_parent_request）
tests/               # unittest 测试（模拟 notifier/driver，不发真实桌面消息）
  pi/                # Node/jiti 扩展测试与真实 HTTP 闭环驱动脚本
skills/agent-link/   # 宿主 skill 定义 + 可从任意 cwd 调用 CLI 的 link.py
docs/
  HOST-PROTOCOL.md   # 宿主服务 HTTP 协议、HostDriver 扩展方式、Pi --extension 用法
  BACKGROUND.md      # 上游研究与最初交接记录（背景资料）
  history/           # 实施、父端复审与边界修复历史
  experiments/       # 去敏后的通知/汇报/反向回复实测记录
SPEC.md              # Agent Link 薄层规范 v0.1
USAGE.md             # CLI 用法、状态模型、退出码、宿主服务启动
```

`experiments/`、`runtime/` 与根目录运行日志是本机数据，已由 `.gitignore` 排除：原始日志、SQLite 数据库和含聊天信息的运行产物不提交、不发布。

## Codex 推荐配套：background-tasks

Codex 桌面 + WSL 推荐先安装独立项目 [codex-background-tasks](https://github.com/zyth72/codex-background-tasks#安装)（MIT）。它提供后台启动、查询、取消、超时、完成通知和任务面板；Agent Link 提供运行中汇报、提问与回复。当前 Codex 宿主驱动还复用它的 `notifier.py` 桌面桥，Pi 子端不需要安装这个插件。

安装方式、Python 版本与桌面限制以配套仓库说明为准。安装后重新加载 Codex，确认 `run_cli` 可用，再继续下面的绑定与宿主设置。绑定的 `notifier_path` 使用当前安装中的实际路径，不复制其他电脑的用户名或缓存版本。

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
- [docs/HOST-PROTOCOL.md](docs/HOST-PROTOCOL.md)：宿主服务 HTTP 协议、HostDriver 扩展、Pi `--extension` 用法与未验证项。
- [docs/history/IMPLEMENTATION-RESULT.md](docs/history/IMPLEMENTATION-RESULT.md)：实现与真实反向回复验收记录（去敏版）。
- [docs/history/REVIEW-FIXES.md](docs/history/REVIEW-FIXES.md)：父端独立复现的四项边界缺陷与回归要求。
- [docs/experiments/notification-smoke/](docs/experiments/notification-smoke/)：三轮通信实测记录（去敏版）。
- [docs/BACKGROUND.md](docs/BACKGROUND.md)：Intercom 上游生态与本机既有实现的研究记录。

## 安全边界与限制

- 同一 OS 可信用户模型：runtime 目录 0700、数据库 0600、子端凭据 0600；不防范恶意同用户进程。
- 适配器只加载绑定时指定的本机 `notifier.py`；消息内容仅作数据传递，不作为代码执行。宿主服务只监听 127.0.0.1，单 binding/单 token；子端不能 `bind`/`reply`/指定父会话。
- 依赖父端宿主消息管道存活；宿主服务必须由父端环境启动（当前 Codex 驱动依赖 background-tasks 桌面桥）。投递成功仅表示宿主接口接受（`host_accepted`），不代表模型已处理；模型是否处理由父端实际答复证明。
- 不自动重试通知，不实现 exactly-once 外部投递；墙钟回拨只能记录 `wall_clock_jump_observed`，不能消除影响。
- v0.1 不支持 `spawn` / `cancel`，不能绑定未启动的任务，不实现自动清理。

## 许可证

本项目采用 [MIT License](LICENSE)。外部依赖及所引用上游项目遵循各自的许可证。
