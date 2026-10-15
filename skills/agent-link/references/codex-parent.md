# Codex 父端 helper 细节（agent_link.codex）

`LINK --codex ...` 等价于 `python3 -m agent_link.codex ...`。除本文件说明的发现规则外，子命令参数、JSON 字段与退出码复用核心 CLI，见 `ABS_REPO/USAGE.md` 与 `ABS_REPO/docs/CODEX.md`。

## 命令

| 命令 | 关键参数 | 退出码 |
| --- | --- | --- |
| `list` | 无（可选全局 `--binding` 过滤） | 0 |
| `reply` | `--request-id --message-id --answer` | 0 / 4 / 5 / 6 |
| `ask-child` | `--request-id --message-id --question --timeout` | 0 / 3 / 4 / 5 / 6 / 7 |
| `get-request` | `--request-id` | 0 / 5 / 6 |
| `get-parent-request` | `--request-id` | 0 / 5 / 6 |
| `list-parent-requests` | `[--pending]` | 0 / 5 / 6 |

`--session-id` 和 `--binding` 是全局参数，也可写在各子命令后。会话 id 缺省读 `CODEX_THREAD_ID` / `CODEX_SESSION_ID`；都取不到 exit 5。

## 发现与校验（list）

候选 = 用户级注册表（`~/.agent-link/registry/`，`AGENT_LINK_REGISTRY` 可覆盖）中 `parent.framework=codex-desktop` 且 `parent.session_id` 等于本会话的条目。每个候选：

1. root 必须为存在的绝对目录，且其中 `agent_link.sqlite3` 存在（**只读**打开，缺文件不会新建）。
2. 数据库 `bindings` 行必须存在；`parent_framework/parent_session_id` 与注册表和本会话一致；`run_id`、`native_task_id`、`config_digest`、`child.framework/session_id` 与注册表一致。
3. 用 `config_json` 重算 SHA-256，必须等于 `config_digest`。

输出 `{"session_id", "registry", "registry_present", "bindings": [{...,"status":"accepted"|"rejected","reason"?}], "accepted": [...], "rejected":[{"binding_id","root","reason"}]}`，exit 0。无法解析的注册表文件也作为 rejected 报告（binding_id 用文件名）。

## 选择与执行

- 恰好一个候选且 accepted：可省略 `--binding`。
- 多条候选（含「一条 accepted + 其他 rejected」）：exit 5，message 提示 `--binding`，JSON 带 `bindings` 明细。
- 唯一候选 rejected：exit 5，原因在 `bindings[0].reason`（含身份、摘要、child 或 DB 不一致的具体字段）。
- `--binding` 未注册于本会话：exit 6；未在本会话候选里：exit 6。
- 选定后创建 Store，并用写连接再校验 parent/child/run_id/config_digest；不一致拒绝执行。之后才调用核心 handler，输出与退出码同核心 CLI。

## 错误 JSON

错误 stdout 为 `{"error", "message", ...}`，发现类错误额外带 `bindings` 数组；诊断也写 stderr。`reply` 冲突（已答复/跨请求 message_id）exit 4，未知请求 exit 6。

## ask-child 与 background-tasks

`ask-child` 阻塞到 `answered` 或 `timed_out`（exit 3）。长等待必须用 `run_cli` 后台执行，不要在父轮次同步等待。`timed_out` 后首个答复保存为 `late=true`、状态保持 `timed_out`；不会自动恢复已退出的等待进程。

能否唤醒子端取决于子端适配器（见 [child-frameworks.md](child-frameworks.md)）：OMP/OpenCode 有 inbox watcher；Pi 子端需自己用 CLI `answer`；Codex 子端没有适配器。写入 inbox 成功只表示事件可读，答复以数据库记录为准。

## 宿主服务与 Pi 子端

```text
run_cli notify=false: python3 -m agent_link.host --root ABS_RUNTIME --binding BINDING \
  --credentials ABS_RUNTIME/child-credentials.json
```

等 ready JSON 与凭据文件生成后，用另一个 `run_cli`（`notify=true`）启动 Pi，命令中显式 `--extension ABS_REPO/extensions/pi/agent-link.ts` 并设置 `AGENT_LINK_CREDENTIALS`。Pi 退出不结束服务；所有任务结束后取消服务任务。服务失败/超时上限、凭据生命周期与协议见 `ABS_REPO/docs/HOST-PROTOCOL.md`。
