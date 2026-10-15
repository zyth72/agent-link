# Agent Link v0.1 实施结果（IMPLEMENTATION-RESULT）

> 父端最终复验（2026-10-15，Asia/Shanghai）：已收到新 CLI 的真实 report 和 ask，并使用新 reply CLI 答复；子模型准确带回验收标记。独立复现后交回 DeepSeek 修复四项边界；修复任务正常退出后，再次独立执行 `python3 -m unittest discover -s tests -v`：23 个测试全部通过（10.236 秒）。另以 get-request 确认原真实请求仍为 replied、late=false，答复保持原文。修复后没有再次发送真实桌面消息，未验证父轮次结束后唤醒或生成期间排队。

日期：2026-10-15。执行者：Pi（child `pi/implementation-pi-001`），按 `SPEC.md` 与 `IMPLEMENTATION-BRIEF.md` 实施并完成真实验收。同日父端复审后完成四项边界修复与回归测试（见文末“复审修复”节）；首次 live-001 真实验收记录保留在本机，本文为发布整理版。

> 发布整理说明：真实聊天标识、验收标记与答复原文已隐去，原始记录保留在本机 `runtime/local-archive/`（不提交）。验收事实（replied、late=false、未超时、`host_accepted`）与时间线保持原样。

## 交付物

| 文件 | 说明 |
| --- | --- |
| `agent_link/__main__.py` | `python3 -m agent_link` 入口 |
| `agent_link/cli.py` | 六个子命令、参数校验、退出码、能力声明 |
| `agent_link/storage.py` | SQLite 持久化、状态机、跨进程事务、审计事件 |
| `agent_link/adapter.py` | 只加载绑定指定的 notifier.py，构造 payload，不执行消息内容 |
| `agent_link/errors.py` | 类型化错误与退出码 |
| `tests/test_agent_link.py` | 23 个测试：14 个原端到端测试 + 9 个复审回归/核心测试（模拟 notifier，不发真实桌面消息） |
| `USAGE.md` | CLI 用法、状态模型、限制 |
| `runtime/live-config.json`、`runtime/live/agent_link.sqlite3` | live-001 绑定与真实运行数据（本机运行产物，不提交） |

## 本地测试（首次实施，修复前计数）

命令与结果：

```
$ cd /home/zyth/agent-intercom-workspace
$ python3 -m unittest discover -s tests -t . -v
...
Ran 14 tests in 4.719s

OK
```

SPEC 第 9 节验证点覆盖对照：

| SPEC 验证点 | 测试 |
| --- | --- |
| 绑定不可重定向 | `test_bind_idempotent_conflict_and_no_redirect`、`test_concurrent_bind_conflict` |
| ID 校验（含路径穿越） | `test_identifier_validation`、`test_bind_rejects_missing_notifier` |
| 相同消息幂等 / 不同内容冲突 | `test_report_idempotent_same_conflict_different`、`test_reopen_database_and_idempotent_replay` |
| 重复回复不可覆盖 | `test_duplicate_reply_cannot_overwrite` |
| 未知请求 | `test_unknown_binding_and_request` |
| 截止时间与迟到回复 | `test_timeout_then_late_reply_is_stored`、`test_late_reply_without_waiter_timeout_update` |
| 重开数据库恢复 | `test_reopen_database_and_idempotent_replay` |
| 两个独立连接的冲突 | `test_concurrent_replies_one_wins`、`test_concurrent_bind_conflict` |
| 模拟 notifier 失败后不重投 | `test_simulated_notifier_failure_no_retry` |
| 能力声明七字段 | `test_capabilities_declares_all_fields` |
| ask/reply 正常往返 | `test_ask_waits_and_receives_reply` |

测试传输全部为临时目录内 stub notifier；没有发送真实桌面消息。

## 真实反向回复验收（live-001）

流程全部使用本次新实现的 CLI，未使用旧实验脚本，未自行调用 reply，未修改运行数据库模拟父端。

1. bind：

```
$ python3 -m agent_link --root .../runtime/live bind --config .../runtime/live-config.json
{"binding_id": "live-001", "created": true, "idempotent": false, "config_digest": "6e901239a80a712f526520730a00901a59ec9ecb7efcc53c6b37d3b5f93f9d51"}
```

2. report（主动汇报，真实投递）：

```
$ python3 -m agent_link --root .../runtime/live report --binding live-001 --message-id implementation-progress-001 --text '...'
{"binding_id": "live-001", "message_id": "implementation-progress-001", "kind": "report", "delivery_state": "host_accepted", "idempotent": false}
```

3. ask（阻塞等待，timeout=240s，13:51:34.307 UTC 持久化）：

```
$ python3 -m agent_link --root .../runtime/live ask --binding live-001 --request-id implementation-decision-001 --message-id implementation-question-001 --question '新薄层反向回复验收：请父端通过 agent_link reply 返回本次验收标记，我收到后原样写入 IMPLEMENTATION-RESULT.md。' --timeout 240
{"binding_id": "live-001", "request_id": "implementation-decision-001", "message_id": "implementation-question-001", "status": "replied", "answer": "<验收标记与答复原文已隐去，本机原始记录见 runtime/local-archive/>", "late": false, "delivery_state": "host_accepted", "idempotent": false}
ask-exit=0
```

4. 持久化核验（get-request，13:51:40.934 UTC 写入答复，reply_message_id=`parent-acceptance-001`）：

- status = `replied`
- late = `false`
- 用时约 6.6 秒，远小于 240 秒截止时间；**未超时**
- 投递状态 = `host_accepted`
- 审计事件链：`binding_created` → `report_persisted` → `dispatch_started` → `delivery_recorded` → `ask_persisted` → `dispatch_started` → `delivery_recorded` → `reply_recorded`

### 父端答复（发布版已隐去原文）

> 父端答复已通过新 CLI 返回并被 `ask` 原样接收；标记与文本属私有通信内容，未写入本发布版，本机原始记录保留在 `runtime/local-archive/`。

说明：`host_accepted` 仅证明宿主接口接受投递；本次父端实际调用 reply 并返回了答复，证明决策请求确实到达父端并被处理。report 是否被模型阅读无法由桥字段证明，已按 SPEC 不声称。

## 复审修复（2026-10-15，父端四项复现）

父端按 `REVIEW-FIXES.md` 在临时目录独立复现四项边界缺陷。本轮在原任务范围内修复源码、补回归测试并更新文档，全部使用临时目录中的模拟 notifier；未发送真实桌面通知，未重新使用 live-001 做发送验收，未修改运行数据库，stdout JSON 协议与既有 CLI 保持兼容。

### 修复项与回归测试

| # | 缺陷（修复前） | 修复 | 回归测试 |
| --- | --- | --- | --- |
| 1 | `cmd_ask` 用通知前的 `start_mono` 加通知后的 `remaining`，通知耗时被重复扣除；stub sleep(0.6)+timeout 1.4 约 0.802s 即超时 | 通知完成后取一次 (wall, mono) 参考对，`mono_deadline = mono + (deadline_epoch - wall)`；replay 只用原截止时间的剩余量 | `test_slow_notifier_does_not_shorten_real_deadline` |
| 2 | `Store.reply` 只比较 kind/text；`message_id=reply1/answer=yes` 先回 r1 再回 r2 会错误成功 | 写事务内检查 `requests.reply_message_id`，同一消息 ID 已属于其他请求即 conflict；消息身份包含目标 request_id | `test_reply_message_id_cannot_answer_two_requests`、`test_concurrent_same_reply_message_two_requests_only_one_wins` |
| 3 | 同一 request_id/message_id/question 把 timeout 60 改 600 仍幂等且保留 60 | timeout 纳入请求语义，replay 时 question/timeout 不同即 conflict；`validate_timeout_seconds` 在核心 API 拒绝非有限/非正 timeout | `test_ask_timeout_change_conflicts`、`test_invalid_timeout_rejected`、`test_core_ask_replay_requires_same_timeout`、`test_core_rejects_non_positive_and_non_finite_timeout` |
| 4 | 已 timed_out 后墙钟回拨至 deadline 之前时 `late` 可能为 false；deadline 相等时按 `>` 判为未超时 | 已 timed_out 一律 `late=true`；迟到判定改为 `time.time() >= deadline_epoch` | `test_core_timed_out_reply_stays_late_after_wall_clock_rollback`、`test_core_deadline_boundary_is_timed_out` |

附带稳定性修复（非四项之一）：`Store.__init__` 的 `PRAGMA journal_mode = WAL` 在并发首次打开同一新库时可能立刻返回 SQLITE_BUSY（不经过 busy handler），导致 `test_concurrent_bind_conflict` 偶发 exit 1。现改为在既有 30s 预算内重试；该并发测试单独重复 30 次 0 失败。

### 修复后测试（本次实际运行）

```
$ cd /home/zyth/agent-intercom-workspace
$ python3 -m unittest discover -s tests -v
...
Ran 23 tests in 8.9s

OK
```

- 修复前基线：14/14 通过（未覆盖上述四项边界）。修复后：23/23 通过（新增 9 个回归/核心测试）；两次完整运行均 OK。
- 修复 1：stub sleep 1.0s、timeout 3.0s，约 2.5s 时答复成功且 `late=false`；修复前同参数约 2.0s 即超时退出。
- 修复 2：跨请求复用回复消息 ID 返回 exit 4，目标请求保持 pending；并发场景返回码为 `[0, 4]`。
- 修复 3：timeout 变更返回 exit 4 且持久化值不变；`0/-1/nan/inf` 返回 exit 5，核心 API 同样拒绝。
- 修复 4：`time.time() == deadline` 时 `timed_out/late=true`；已超时请求在墙钟回拨 120s 后答复仍 `late=true`。

### 真实验收说明

上述 live-001 记录为首次且唯一一次真实桌面通知验收；验收标记与答复原文已隐去，本机原始记录保留在 `runtime/local-archive/`。本轮修复未重跑、未改写，也未发送第二次真实通知。

## CLI 用法（摘要）

```bash
python3 -m agent_link --root ABS_DIR bind --config FILE
python3 -m agent_link --root ABS_DIR capabilities
python3 -m agent_link --root ABS_DIR report --binding ID --message-id ID --text TEXT
python3 -m agent_link --root ABS_DIR ask --binding ID --request-id ID --message-id ID --question TEXT --timeout SECONDS
python3 -m agent_link --root ABS_DIR reply --binding ID --request-id ID --message-id ID --answer TEXT
python3 -m agent_link --root ABS_DIR get-request --binding ID --request-id ID
```

stdout 一条 JSON；退出码：0 成功、3 ask 超时、4 冲突、5 校验、6 不存在、7 投递未确定/失败；ask 成功 JSON 含 `request_id`、`answer`、`late`、`status`。详见 `USAGE.md`。

## 限制

- 单机、同一 OS 可信用户；目录 0700、数据库 0600，但不防范恶意同用户进程。
- 依赖 Codex 桌面消息管道与环境变量（`CODEX_APP_TOOLS_PIPE_PATH`）；WSL→Windows 桥不可用时投递记为 `failed_or_unknown`，不自动重试。
- 轮询有延迟（默认 0.1s）；不承诺应用重启后通知可用；不实现 exactly-once 外部投递。
- 墙钟回拨可能影响迟到判定，仅记录 `wall_clock_jump_observed`，无法消除。
- v0.1 不实现 spawn/cancel；不实现自动清理（请求、答复、审计事件需人工清理）。

## 结论

- 首次实现、测试、真实反向回复验收全部完成；首次实现时 14/14 测试通过。
- 父端复审四项缺陷已修复并补 9 个回归/核心测试，修复后 23/23 测试通过（两次完整运行均 OK）。
- live-001 验收回答已收到且未超时（late=false）；答复标记与原文已按发布要求隐去，本机原始记录保留在 `runtime/local-archive/`（不提交）。本轮修复仅使用模拟 notifier，未发送第二次真实桌面通知。
- 按 SPEC 第 9 节，父端独立复验代码与测试后按事实更新已验证能力；本文不代替父端复验结论。
