# Agent Link

跨框架 agent 任务通信薄层：父/子会话绑定、运行中进度汇报（`report`）、决策请求与阻塞等待答复（`ask` / `reply`）。任务的生命周期（启动、取消、超时、退出通知）仍由各框架自己的后台任务机制管理；本层不实现进程运行器，也不要求 Intercom broker。

- Python 标准库实现，无第三方运行依赖。
- SQLite 单文件持久化，支持多个 CLI 进程并发。
- 粘性绑定与消息幂等：绑定不可重定向，重复提交不重复投递，冲突显式报错。
- 当前已验证组合：Codex 桌面父端 + Pi CLI 子端（同机、同一 OS 用户，Linux/WSL）。

## 状态

v0.1，仓库内 23 个测试全部通过（`python3 -m unittest discover -s tests -t . -v`）。

已验证：绑定/幂等/冲突、运行中 `report`、阻塞 `ask` 与父端 `reply` 反向回复、超时与迟到答复、数据库重开恢复、并发写入冲突、通知失败不重投。

仍为未知：父端忙碌时排队、父轮次结束后的唤醒、应用重启后的可靠性、exactly-once 外部投递。未适配其他宿主；不要只修改 `framework` 字符串就声称新框架受支持。

## 项目结构

```text
agent_link/          # Python 包：CLI、SQLite 状态机、通知适配器
tests/               # unittest 测试（模拟 notifier，不发真实桌面消息）
skills/agent-link/   # 宿主 skill 定义 + 可从任意 cwd 调用 CLI 的 link.py
docs/
  BACKGROUND.md      # 上游研究与最初交接记录（背景资料）
  history/           # 实施、父端复审与边界修复历史
  experiments/       # 去敏后的通知/汇报/反向回复实测记录
SPEC.md              # Agent Link 薄层规范 v0.1
USAGE.md             # CLI 用法、状态模型、退出码
```

`experiments/`、`runtime/` 与根目录运行日志是本机数据，已由 `.gitignore` 排除：原始日志、SQLite 数据库和含聊天信息的运行产物不提交、不发布。

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

## 测试

```bash
cd /path/to/agent-link
python3 -m unittest discover -s tests -t . -v
```

测试使用临时目录中的 stub notifier，不会发送真实桌面消息。

## 打包与安装

`pyproject.toml` 仅声明打包元数据（版本动态取自 `agent_link.__version__`），无第三方运行依赖。需要命令行入口时可选用：

```bash
python3 -m pip install .          # 提供 agent-link 命令；不安装额外依赖
python3 -m pip install -e . --no-deps   # 开发模式
```

也可不安装，直接在仓库根运行 `python3 -m agent_link ...`；`skills/agent-link/scripts/link.py` 默认自动定位本仓库，或用 `AGENT_LINK_REPO` 指定仓库根目录。

## 文档

- [SPEC.md](SPEC.md)：接口、能力声明、持久化与失败语义。
- [USAGE.md](USAGE.md)：CLI 用法、状态模型、幂等规则、已知限制。
- [docs/history/IMPLEMENTATION-RESULT.md](docs/history/IMPLEMENTATION-RESULT.md)：实现与真实反向回复验收记录（去敏版）。
- [docs/history/REVIEW-FIXES.md](docs/history/REVIEW-FIXES.md)：父端独立复现的四项边界缺陷与回归要求。
- [docs/experiments/notification-smoke/](docs/experiments/notification-smoke/)：三轮通信实测记录（去敏版）。
- [docs/BACKGROUND.md](docs/BACKGROUND.md)：Intercom 上游生态与本机既有实现的研究记录。

## 安全边界与限制

- 同一 OS 可信用户模型：runtime 目录 0700、数据库 0600，不防范恶意同用户进程。
- 适配器只加载绑定时指定的本机 `notifier.py`；消息内容仅作数据传递，不作为代码执行。
- 依赖父端宿主消息管道存活；投递成功仅表示宿主接口接受（`host_accepted`），不代表模型已处理；模型是否处理由父端实际答复证明。
- 不自动重试通知，不实现 exactly-once 外部投递；墙钟回拨只能记录 `wall_clock_jump_observed`，不能消除影响。
- v0.1 不支持 `spawn` / `cancel`，不能绑定未启动的任务，不实现自动清理。

## 许可证

本仓库尚未声明开源许可证。如需公开分发或允许他人复用，请先与维护者确认许可证后再添加 `LICENSE`。
