# 通知实测记录（去敏发布版）

本目录保留 2026-10-15 在 Codex 桌面父聊天 + Pi CLI 子端之间做的三轮通信实测结论：

| 文件 | 内容 |
| --- | --- |
| `NOTIFICATION-TEST.md` | 后台任务完成通知、子端运行中主动汇报、反向决策回复三轮实测总览 |
| `PROGRESS-RESULT.md` | 第二轮：子端运行中通过 bridge 主动汇报的详细记录 |
| `REVERSE-RESULT.md` | 第三轮：子端 ask、父端 reply、等待方收到答复的详细记录 |

发布整理规则：

- 已隐去本机后台 task ID、父端一次性 reply_tag 等私有通信标识；
- 原始日志（`*.stdout` / `*.stderr` / `*.jsonl`）、SQLite 数据与一次性探测脚本不随仓库发布，仅保留在本机原实验目录 `experiments/notification-smoke/`；
- 结论、时间线、测试计数与限制保持原样，未改写实验结果。

这些记录描述的是当时的实验实现（`message_store.py`、`decision_probe.py` 等一次性脚本），不是正式通信 SDK；正式实现与约定见仓库根目录 [SPEC.md](../../../SPEC.md) 和 [USAGE.md](../../../USAGE.md)。
