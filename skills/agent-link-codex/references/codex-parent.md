# Codex 父端 helper 细节（agent_link.hosts.codex.cli）

`LINK --codex ...` 等价于 `python3 -m agent_link.hosts.codex.cli ...`。除本文件说明的发现规则外，子命令参数、JSON 字段与退出码复用核心 CLI，见 `ABS_REPO/USAGE.md` 与 `ABS_REPO/docs/CODEX.md`。

## 命令

| 命令 | 关键参数 | 退出码 |
| --- | --- | --- |
| `list` | 无（可选全局 `--binding` 过滤） | 0 |
| `reply` | `--request-id --message-id --answer` | 0 / 4 / 5 / 6 |
| `ask-child` | `--request-id --message-id --question --timeout` | 0 / 3 / 4 / 5 / 6 / 7 |
| `get-request` | `--request-id` | 0 / 5 / 6 |
| `get-parent-request` | `--request-id` | 0 / 5 / 6 |
| `list-parent-requests` | `[--pending]` | 0 / 5 / 6 |
| `notifications` | 无 | 0 / 5 / 6 |
| `recover-notifications` | `[--message-id ID ...]` | 0 / 5 / 6 / 7 |
| `register-connection` | 无（不需要 binding） | 0 / 5 |
| `connection-status` | 无（不需要 binding） | 0 / 5 |

`--session-id` 是全局参数；`--binding` 也是全局参数，也可写在各子命令后。会话 id 缺省读 `CODEX_THREAD_ID` / `CODEX_SESSION_ID`；都取不到 exit 5。

## 发现与校验（list）

候选 = 用户级注册表（`~/.agent-link/registry/`，`AGENT_LINK_REGISTRY` 可覆盖）中 `parent.framework=codex-desktop` 且 `parent.session_id` 等于本会话的条目。每个候选：

1. root 必须为存在的绝对目录，且其中 `agent_link.sqlite3` 存在（**只读**打开，缺文件不会新建）。
2. 数据库 `bindings` 行必须存在；`parent_framework/parent_session_id` 与注册表和本会话一致；`run_id`、`native_task_id`、`config_digest`、`child.framework/session_id` 与注册表一致。
3. 用 `config_json` 重算 SHA-256，必须等于 `config_digest`。

输出 `{"session_id", "parent_framework", "registry", "registry_present", "bindings": [{...,"status":"accepted"|"rejected","reason"?}], "accepted": [...], "rejected":[{"binding_id","root","reason"}]}`，exit 0。无法解析的注册表文件也作为 rejected 报告（binding_id 用文件名）。

## 选择与执行

- 恰好一个候选且 accepted：可省略 `--binding`。
- 多条候选（含「一条 accepted + 其他 rejected」）：exit 5，message 提示 `--binding`，JSON 带 `bindings` 明细。
- 唯一候选 rejected：exit 5，原因在 `bindings[0].reason`（含身份、摘要、child 或 DB 不一致的具体字段）。
- `--binding` 未出现在本会话候选中：exit 6；出现在候选中但被 rejected：exit 5，原因在 `bindings` 明细。
- 选定后创建 Store，并用写连接再校验 parent/child/run_id/config_digest；不一致拒绝执行。之后才调用核心 handler，输出与退出码同核心 CLI。

## 错误 JSON

错误 stdout 为 `{"error", "message", ...}`，发现类错误额外带 `bindings` 数组；诊断也写 stderr。`reply` 冲突（已答复/跨请求 message_id）exit 4，未知请求 exit 6。

## 连接登记、离线恢复与 SessionStart hook

连接描述符位于 `~/.agent-link/codex-connections/<session_id>.json`（`AGENT_LINK_CODEX_CONNECTIONS` 可覆盖；目录 0700、文件 0600），只保存 protocol/session/generation/update time 与 `CODEX_APP_TOOLS_PIPE_PATH`、`CODEX_APP_TOOLS_CALLER_HOST_ID`。通知每次读取最新描述符，复制环境后只覆盖传输字段；不改全局环境。新管道原子递增 generation，同环境重放幂等，缺管道不覆盖旧值，跨会话不能重定向。

`register-connection` 使用当前继承的管道环境手动登记，不需要已有 binding；显式 session 与当前 `CODEX_THREAD_ID`/`CODEX_SESSION_ID` 不一致会被拒绝。`connection-status` 仅报告本地元数据，不探测传输存活。

插件清单 `.codex-plugin/plugin.json` 指向 `hooks/hooks.json`；SessionStart hook（`startup|resume|clear|compact|fork`）读取 JSON stdin 的 `hook_event_name/session_id/source`，用当前管道环境执行同一登记函数。插件必须由用户显式信任 hook；若真实版本不把私有管道环境传给 hook，用手动 `register-connection` 兜底。官方文档未保证该私有 env 的 hook 继承，真实 app resume 验证前保持 unknown。

`notifications` 在已完成注册表 + 只读 SQLite 归属校验后列出本 binding 未 accepted 的 report/ask（原文、request id、问题、deadline、答复状态）。`recover-notifications` 只重放桥调用前可证明未提交的 `not_sent` 失败：SQLite compare-and-set 原子领取、保留消息 id 与 deadline、跳过过期/已回答 ask、不重放 host_accepted/dispatching/unknown。桥调用后的 ENOENT/超时/异常保持 unknown，不按错误字符串匹配，也不自动重试；没有 force-unknown 或 exactly-once 承诺。

## ask-child 与 background-tasks

`ask-child` 阻塞到 `answered` 或 `timed_out`（exit 3）。长等待必须用 `run_cli` 后台执行，不要在父轮次同步等待。`timed_out` 后首个答复保存为 `late=true`、状态保持 `timed_out`；不会自动恢复已退出的等待进程。

能否唤醒子端取决于子端适配器（见共享壳 [../../agent-link/references/child-frameworks.md](../../agent-link/references/child-frameworks.md)）：Pi 使用 RPC 或 session HTTP；OMP 使用 session HTTP；OpenCode 使用原生 prompt HTTP 推送（描述符契约）；Codex 子端未实现。传输成功只代表接收方接受，不代表子端已处理；是否答复以持久化的 answer 为准。

## 宿主服务与 Pi 子端

```text
run_cli notify=false: python3 -m agent_link.host --root ABS_RUNTIME --binding BINDING \
  --credentials ABS_RUNTIME/child-credentials.json
```

等 ready JSON 与凭据文件生成后，用另一个 `run_cli`（`notify=true`）启动 Pi，命令中显式 `--extension ABS_REPO/extensions/pi/agent-link.ts` 并设置 `AGENT_LINK_CREDENTIALS`。Pi 退出不结束服务；所有任务结束后取消服务任务。服务失败/超时上限、凭据生命周期与协议见 `ABS_REPO/docs/HOST-PROTOCOL.md`。
