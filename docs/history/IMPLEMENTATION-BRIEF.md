# Pi / DeepSeek 实施任务

读取 SPEC.md 并实际实现，不仅提供建议。只修改 /home/zyth/agent-intercom-workspace 下源码、测试、文档；不要修改 experiments 内旧实验，不改全局配置、不安装依赖、不执行 git 操作。不加载额外 agent，不启动其他模型。

交付 agent_link 包、tests 测试、USAGE.md 和 IMPLEMENTATION-RESULT.md。Python 标准库即可。按 SPEC 的明确 CLI 形式实现，--root 参数放在子命令前。

完成本地测试后必须使用新 CLI 实测，不使用旧实验脚本冒充新实现。创建 runtime/live-config.json，内容：

```json
{"binding_id":"live-001","run_id":"implementation-live-001","native_task_id":null,"parent":{"framework":"codex-desktop","session_id":"PARENT_SESSION_ID"},"child":{"framework":"pi","session_id":"implementation-pi-001"},"notifier_path":"/mnt/c/Users/zyth/.codex/plugins/cache/codex-tools/background-tasks/0.1.9/scripts/notifier.py"}
```

调用 python3 -m agent_link --root /home/zyth/agent-intercom-workspace/runtime/live bind --config /home/zyth/agent-intercom-workspace/runtime/live-config.json。

然后用 report --binding live-001 --message-id implementation-progress-001 --text 汇报实现和测试的实际进展。随后调用 ask --binding live-001 --request-id implementation-decision-001 --message-id implementation-question-001 --question '新薄层反向回复验收：请父端通过 agent_link reply 返回本次验收标记，我收到后原样写入 IMPLEMENTATION-RESULT.md。' --timeout 240。

不得自己调用 reply，不得提前伪造答案，不得修改运行数据库来模拟父端。父端收到消息后会执行 reply。必须让 ask 的 JSON 返回给你的模型，再记录原样 answer。ask 失败如实记录并停止真实验收，不循环发消息。完成报告应包含真实测试计数、限制、CLI 用法、反向答复和是否超时。
