# AI 协作约定

你是一位会反复检查的专家，你持怀疑态度，而且你会做研究。我并不总是对的，你也不是，但我们都力求准确。

开始设计或修改前，先读 [术语约定](docs/TERMINOLOGY.md)，沟通和文档沿用其中名称。按任务需要读取 [核心规范](SPEC.md)、[宿主协议](docs/HOST-PROTOCOL.md) 和 [使用说明](USAGE.md)。历史文档位于 docs/history 与 docs/experiments，不把历史状态当成当前事实。

- 父/子是任务角色，Task、Run、Session、Binding、原生 task_id 不混用。
- Agent Link 是协作层；后台运行器、父端宿主适配器、子端适配器、通知桥各有职责。现有标识和 CLI 保持兼容，术语统一不意味着重命名 API。
- 宿主接受投递不等于模型已处理；报告明确区分模拟测试、真实接口测试和模型端到端测试。未验证能力保留 unknown。
- runtime、日志、凭据和真实会话数据不入库；不要在测试中改真实 Codex 配置或发送未授权消息。
- 核心/宿主改动按需运行 `python3 -W error::ResourceWarning -m unittest discover -s tests -v`；Pi 扩展改动另运行 `node tests/pi/extension_unit.test.mjs`（需要本机 Pi）。纯文档改动检查链接和语义即可。
