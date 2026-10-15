# OMP 接入：能力与启动

入口 get_adapters() 提供父端和子端。两侧使用 session HTTP 主动推送，不再读桥接文件或 inbox，不依赖轮询。框架注入使用 sendUserMessage(deliverAs=aside, attribution=agent)。统一 steer/queue/interrupt 控制仍未接入，不由 aside 推断其他能力。

## 启动

先读 [公共准备](../README.md#启动责任与公共准备) 与 [主动通知协议](../../../docs/SESSION-PUSH.md)。

```bash
cd "$WORK"
export AGENT_LINK_SESSIONS=/absolute/private/session-connections
# 子端还需父端通信服务已就绪后设置此项；父端不需要子端凭据。
export AGENT_LINK_CREDENTIALS="$ROOT/child-credentials.json"
omp --extension "$REPO/extensions/omp/agent-link.ts" --model "$OMP_MODEL"
```

OMP_MODEL 从本机 omp models 取得，认证由 OMP 配置。保持 extensions/shared 完整。扩展在 session_start 按真实 session id 登记，随后可创建 Binding；不复制 Pi 的 --session-id 参数。父端通信服务 --framework omp，继承相同 AGENT_LINK_SESSIONS。子端应为常驻会话，运行结束即退出的 -p 不提供后续唤醒保证。

## 就绪、已有会话、清理

检查对应 session descriptor 已登记，宿主服务 ready，再用 report/ask/reply 或 ask-child/answer 闭环。HTTP accepted 仅代表框架注入成功；模型处理需实际答复证明。身份不匹配、会话已退出或接口失败明确报错，不写文件兜底。

已有进程须在启动时已加载扩展；恢复使用本机 --resume 参数。session 切换或 shutdown 会关闭旧监听并删除属于自己的记录。结束时先退出专用子会话，再停通信服务，保留用户交互父端。崩溃后先确认旧进程不存在再人工清理残留 descriptor；不覆盖活跃注册。

## 验证

正式扩展已在交互 OMP 18.11.1 + DeepSeek 下通过空闲通知、工具忙碌时通知、父端 ask/reply 与子端 ask-child/answer。计划模式、用户中断竞争与重连仍为 unknown。工具 report_parent、ask_parent、check_parent_request、poll_parent_requests、answer_parent 保留；主动通知不需要模型反复调用 poll_parent_requests。
