# REVERSE-RESULT.md — 反向决策回复实测结果

> 发布整理版：父端一次性 reply_tag 已隐去；原始 JSONL 与脚本仅保存在本机 `experiments/notification-smoke/`，不随仓库发布。

日期：2026-10-15（Asia/Shanghai 21:41）
子端：Pi CLI，deepseek/deepseek-flash。父端：Codex 桌面 WSL 聊天。
范围：仅当前目录 `notification-smoke`，未改配置、未安装依赖、未执行 git 操作。未修改 `decision_probe.py` 与 `reverse-probe/` 内任何文件，未自行调用 reply。

## 1. ask 探测（真实返回）

命令：`python3 decision_probe.py ask`

```json
{"request_id": "reverse-probe-001", "decision": "zeros", "reply_tag": "<已隐去>", "replied_at": "2026-10-15T13:41:07.663941+00:00"}
```

| 字段 | 值 |
| --- | --- |
| request_id | `reverse-probe-001` |
| decision | `zeros` |
| reply_tag | `<已隐去>` |

事件时间线（`reverse-probe/events.jsonl`，由探测脚本自身写入）：

| 时间（UTC） | 事件 |
| --- | --- |
| 13:41:01.982 | `request_created` |
| 13:41:02.446 | `bridge_returned`（bridge 调用返回，仅 0.46s） |
| 13:41:07.664 | `parent_reply_prepared`（父端准备回复） |
| 13:41:07.853 | `reply_received`（子端收到，decision=zeros，reply_tag 已隐去） |

子端在 bridge 返回后等待约 5.2 秒收到父端真实回复，未超时、未报错。

## 2. 依据 decision 的实现

- 新增 `summarize_percentages.py`：`summarize_percentages(store)` 返回 `pending` / `replied` / `timeout` 三种状态的百分比份额（0–100），分母为三者计数之和（迟到回复仍计入 `timeout`）。
- 父端 decision=**zeros**：空库返回 `{"pending": 0.0, "replied": 0.0, "timeout": 0.0}`，不抛 `ValueError`（error 分支未被选中，未实现）。
- 未修改 `decision_probe.py`、`reverse-probe/`、`summarize_store.py`、`message_store.py`。

## 3. 测试

新增 `test_summarize_percentages.py`（6 个用例），覆盖混合状态与父端选定空库行为：空库全零、键集合恰为三状态、3 pending/2 replied/1 timeout 混合比例（50/33.33/16.67，和为 100）、单一状态 100%、超时后迟到回复仍计 timeout、仅 replied+pending。

命令：`python3 -m unittest -v`

```
Ran 32 tests in 0.021s

OK
```

- `test_message_store.py`：24 个通过
- `test_summarize_store.py`：2 个通过
- `test_summarize_percentages.py`：6 个通过
- 合计 32 个全部通过，无失败、无错误。

## 4. 结论与限制

已验证：子端发起决策请求 → 父端在当前聊天轮次内接收并生成内容 → 通过本地 reply 通道返回 decision 与 reply_tag → 子端等待期间不实现、收到后按 decision 实现并测试。此通道为一次性本地文件探测，不是正式通用回复 API；仅验证单次请求、父端即时响应场景，未验证排队、重试、父轮次结束后唤醒等。
