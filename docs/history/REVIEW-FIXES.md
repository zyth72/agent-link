# 父端审查：必须修复的边界

现有 14 测试已独立复跑通过，新 CLI 的 report/ask/reply 真实闭环已成功。以下是父端在临时目录中实际复现的缺陷，请修复并添加回归测试，不发真实桌面通知，不重用 live-001 做发送验收，不改 experiments 旧实验。

1. cli.cmd_ask：start_mono 在通知前记录，remaining 在通知后计算，却使用 start_mono + remaining，通知耗时被重复扣除。模拟 notifier sleep(0.6)，请求 timeout=1.4，实际约 0.802 秒就超时。应正确保留从请求持久化截止时间映射出的单调时钟期限，包括 replay 剩余时间；加慢通知后仍能在真实截止时间前收到答复的测试。不要把等待期限延长到通知结束后重新开始，也不要重复减耗时。
2. Store.reply：对 r1 用 message_id=reply1/answer=yes 回复，再对 r2 用同一 reply1/yes，第二次错误成功。消息去重内容必须包含目标 request_id，跨请求复用消息 ID 应冲突；事务内检查，不能用仅进程内集合。加两个连接复用同一消息 ID 回复不同请求的回归。
3. prepare_ask：同一 request_id/message_id/question 但 timeout=60 改成 600，返回幂等且保留 60，没有报冲突。超时参数也是请求语义的一部分，应冲突；校验放在核心 API 内，拒绝非有限或非正 timeout。补测试。
4. late：请求已 timed_out 后，如果墙钟回拨至 deadline 之前，当前 late 计算可能 false；已超时请求的答复必须 late=true。边界到达 deadline 时按超时处理（>=），补时钟模拟测试。

请读取当前代码后实现。更新 USAGE.md、SPEC.md 的相关澄清，并更新 IMPLEMENTATION-RESULT.md，保留首次真实验收的原样标记，明确修复前后结果，不伪造真实第二次通知。stdout 的 JSON 协议和既有 CLI 保持兼容。

修复后执行 python3 -m unittest discover -s tests -v，最终报告测试数量和修复项。
