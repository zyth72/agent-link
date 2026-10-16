# Pi / OMP 主动通知

Agent Link 是通信工具与会话关联管理的小工具集合。Pi/OMP 的正式扩展在交互会话启动时建立本机 HTTP 接口，发送方按框架与 session ID 直接推送；没有文件收件箱扫描、注册表定时发现或轮询降级。父端不要求 RPC。

## 启动和就绪

```bash
# REPO 为本仓库绝对路径；同一组发送/接收进程需使用相同目录。
export AGENT_LINK_SESSIONS=/absolute/private/agent-link-sessions
pi --extension "$REPO/extensions/pi/agent-link.ts"
# 或
omp --extension "$REPO/extensions/omp/agent-link.ts"
```

默认目录 `~/.agent-link/sessions`。保持 extensions/shared 完整。扩展在 session_start 启动监听，session_shutdown 关闭（包括切换/重载）。注册记录为 `<目录>/<framework>/<session_id>.json`，目录创建权限 0700，文件 0600；记录 protocol、framework、session_id、endpoint、独立随机 token 与 pid。token 不输出、不入库。仅描述连接，不存放消息。

会话先启动也可以：注册与 Binding 无关，之后创建绑定不需要发现扫描。使用记录中的真实 session_id 创建绑定；父端服务继承相同 AGENT_LINK_SESSIONS，按 parent.framework 选择 --framework pi/omp。OMP 子端同样注册，父→子 ask-child 走直接推送。Pi 的专用 RPC 子端优先由 PI-RPC 控制器负责；没有 RPC descriptor 时可连接交互 Pi 的 session HTTP 接口，两者都不写 inbox。

监听与注册完成才算可接收。重复/残留 descriptor 不自动覆盖：启动失败并关闭新建接口。崩溃后的残留需先核对旧进程已退出，再移除；重启不自动重放失败通知。会话关闭只删除自己 token 对应的记录，替换记录不误删。

## 协议与语义

`POST /v1/notify`，127.0.0.1 随机端口，Bearer token。JSON 字段：protocol=agent-link-push/1、framework、session_id、binding_id、run_id、role=parent|child、message_id、prompt。认证与目标 session/framework 不匹配拒绝；标识符沿用核心格式。接收端只将 prompt 作为消息数据注入，不执行携带的命令。

发送方从权威 binding 选择目的会话、渲染消息，仅按需读取 descriptor。禁用代理与 HTTP 重定向，只接受 loopback HTTP origin。10 秒超时、64 KiB 请求/响应限制；不在线/过时/拒绝/结果未知均失败，无文件回退、无自动重发。核心仍在外部调用前记录 dispatching，再记录 host_accepted 或 failed_or_unknown。

Pi：空闲调用 sendUserMessage，忙碌调用 followUp，不改变用户正在执行的任务。OMP：sendUserMessage(deliverAs=aside, attribution=agent)，在步骤边界加入，通常可唤醒空闲会话，计划模式或用户中断抑制等仍服从原生行为。HTTP 返回 accepted 证明框架调用被接受，不证明模型处理；需 report/reply/answer 等行为证据。

注册是同一 OS 用户可信场景的发现信息，不是隔离机制。接收端不对 Binding 重新建库或改写身份；拥有 token 的同用户进程可信。正常请求按到达处理队列串行提交，最多 64 个待提交项，过载拒绝。异常/断线后结果可能未知，不承诺 exactly-once、离线队列或崩溃重放。

## 能力和验证

Pi/OMP 父端 capabilities.notification_transport=session_push_http、polling=false；OMP 子端 delivery_transport=session_push_http。OpenCode 不使用本文件的 `agent-link-push/1` 监听接口：其 Python 父子适配器直连框架原生 prompt HTTP 端点（Basic 认证 + 独立的会话/serve 描述符契约，见 [HOST-PROTOCOL §6.3](HOST-PROTOCOL.md)），capabilities 已改为 push 语义，插件侧文件 watcher 已删除。请求等待状态查询和测试等待与消息传输轮询是不同操作。

模拟测试覆盖认证、身份、上限、并发、失败与生命周期；真实接口测试验证本地框架加载；模型端到端记录正式扩展的真实模型行为。忙碌、中断、重连等尚无端到端证据时保持 unknown，不以原生文档代替验收。

本轮正式扩展的真实模型验收：Pi 1.2.0、OMP 18.11.1、DeepSeek Flash。父端空闲通知返回指定标记；宿主服务 ask → 推送 → 父模型实际执行 CLI reply，均 replied/late=false；OMP 子端 ask-child → 推送 → 模型执行 CLI answer，answered/late=false。两边在执行 5 秒工具期间接受通知（本机接受耗时约 7ms），随后模型实际回复标记。后续模型处理耗时不由该 HTTP 接受耗时推断。测试 observer 仅记录事件，接收和发送均使用正式扩展/适配器。

真实测试数据在本地忽略的 runtime/push-live；不提交 session ID、token、模型思考或原始会话日志。正常退出的监听清理另作核对。默认自动测试不调用模型。
