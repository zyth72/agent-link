# 后台通知实测：2026-10-15

> 发布整理版：本机 task ID 与父端一次性标记已隐去；原始日志、JSONL 与脚本仅保存在本机 `experiments/notification-smoke/`，不随仓库发布。

父端：Codex 桌面 WSL 聊天；子端：Pi CLI，deepseek/deepseek-flash。
通过 background-tasks 0.1.9 的 run_cli 启动，notify=true。

- OpenCode 的 opencode-go/glm-5.3-flash 返回 provider.no-route / Model unavailable；失败通知自动回到父聊天。
- 后台环境的 PATH 找不到 pi；该次退出码 127，失败通知也自动回到父聊天。
- 改用 /home/zyth/.npm-global/bin/pi，显式 --provider deepseek --model deepseek-flash，成功执行。
- 成功任务 ID：`cli-<已隐去>`。
- 退出码 0，后台记录耗时 77.985 秒；完成通知以后台任务结果消息自动进入父聊天，父 agent 随后读取实现并复跑测试。
- 父端复验：python3 -m unittest -v，24 个测试通过。

代码和 stdout/stderr 均在本目录。因命令输出重定向到文件，自动通知的 output_tail 为空；父端需另读结果文件。

已验证：后台执行、失败/成功终态通知、当前父聊天在等待期间收到消息并继续处理、父端验收。
未验证：Pi 运行中主动汇报、向运行中 Pi 返回决策、父端生成内容期间的排队行为、父端结束当前轮次后的唤醒、应用重启后的可靠性。

message_store.py 是子执行器编写的状态存储实验，不是正式 Intercom SDK。它允许多次迟到回复覆盖，去重不验证重复消息内容一致性，不能据此认定已具备生产通信可靠性。

## 第二轮：运行中主动汇报

- 任务 ID：`cli-<已隐去>`；仍为 Pi + DeepSeek Flash。
- 父端提供一次性 report_progress.py，固定发起聊天；Pi 读取消息模块后主动通过 bash 执行该脚本。使用原插件的 notifier.bridge，不修改插件缓存。
- 21:38:43.598（Asia/Shanghai）记录发送，21:38:44.213 桥返回；事件 ID progress-probe-001。
- 父端真实收到进度消息并继续处理，随即读取任务记录：status=running、exit_code=null、notification_status=pending；此时 summarize_store.py 尚不存在。
- Pi 汇报后继续等待观察窗口、编写 summarize_store.py 和测试，随后正常退出；总耗时 51.003 秒，退出码 0。完成通知另外送达父聊天。
- 父端独立复跑全部 26 个测试，通过。

结论：已验证子模型执行工具主动汇报 → 桌面桥投递 → 父模型在等待期间接收并处理 → 子任务继续工作 → 终态通知。此轮使用预设内容的一次性脚本，不是正式通用汇报工具；未验证父轮次结束后的唤醒、生成期间消息排队、反向决策回复及可靠重试。

## 第三轮：反向决策回复

- 任务 ID：`cli-<已隐去>`；Pi + DeepSeek Flash。
- 子模型执行 decision_probe.py ask，通过桌面桥向父端询问空库处理策略，随后在同一运行进程中等待本地文件回复。
- 父端实际收到 request_id=reverse-probe-001；回复前读取任务状态为 running、exit_code=null，目标实现和 reply.json 都不存在。
- 父模型选择 zeros，并当场生成 reply_tag=`<已隐去的一次性标记>`，通过 decision_probe.py reply 写入临时文件后原子替换为 reply.json。
- 21:41:01.982（Asia/Shanghai）创建请求，21:41:07.664 父端准备答复，21:41:07.853 子端读取答复，写入 reply_received 事件。
- 子模型后续报告准确返回上述 reply_tag，并按 zeros 实现空库返回零的比例统计函数；任务总耗时 44.953 秒，退出码 0，完成通知自动送达。
- 父端检查代码并独立运行全部 unittest：32 个通过。

结论：运行中子任务提问 → Codex 接收并作出决定 → 本地文件反向回复 → 等待中的工具返回答复给 Pi 模型 → Pi 按答复继续写代码，单次闭环验证成功。上行使用桌面消息桥，下行使用本地文件轮询（0.2 秒）；未使用 Intercom broker，未验证通用 MCP reply API、多请求并发、超时恢复或父轮次结束后的唤醒。测试脚本固定身份，仅适用于当前实验。
