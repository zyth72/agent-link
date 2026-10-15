# OMP 接入：能力与启动

入口 `get_adapters()` 提供父端和子端。当前使用扩展 + 文件桥/收件箱 watcher；尚未提供统一 steer、queue、interrupt 控制接口，不能从 OMP 原生能力推断适配器已支持。

## 启动与模型配置

先读 [公共准备](../README.md#启动责任与公共准备)。扩展路径必须保留 `extensions/shared/` 相对结构。交互父端或子端均可这样加载：

```bash
cd "$WORK"
omp --extension "$REPO/extensions/omp/agent-link.ts" --model "$OMP_MODEL"
```

`OMP_MODEL` 使用本机 `omp models` 列出的模型标识，认证由 OMP 自行配置。子端额外设置凭据路径（父端服务 ready 后）：

```bash
export AGENT_LINK_CREDENTIALS="$ROOT/child-credentials.json"
omp --extension "$REPO/extensions/omp/agent-link.ts" --model "$OMP_MODEL"
```

父端采用 `parent.framework=omp` 和通信服务 `--framework omp`；子端采用 `child.framework=omp`。取框架实际 session id 填入对应绑定，勿假设 OMP 接受 Pi 的 `--session-id`。已有会话按 `omp --help` 的 `--resume` 方式恢复，并在启动时加载扩展；运行中的进程不承诺支持热挂扩展。需要保持消息接收的子端使用长驻会话，不把执行完即退出的 `-p` 当作常驻服务。

## 就绪与功能

父端服务 ready 后，扩展按 registry 匹配真实 session id，或父端显式设置 `AGENT_LINK_BRIDGE_FILE` 和 `AGENT_LINK_BINDING`。注册表可在会话启动后新增绑定，watcher 再发现。子端工具包括 report_parent、ask_parent、check_parent_request、poll_parent_requests、answer_parent。

父→子事件写 `<root>/inbox/<binding>.jsonl`，子→父事件写 `<root>/omp-bridge/<binding>.jsonl`；当前 watcher 轮询文件。文件落盘只表示传输接受，需 report/ask/reply 或 ask-child/answer 实际闭环判断接入成功。临时 HTTP 推送探针已在真实交互 OMP 18.11.1 中通过 sendUserMessage(aside) 唤醒空闲会话并取得 DeepSeek 答复；探针未整合进正式适配器。会话忙碌、会话切换和重连行为仍为 unknown。

## 清理

通过运行器停止本次专用 OMP 子进程，再停止通信服务；用户正在使用的交互父端不随子任务取消。没有已实现的 OMP 远程 stop 命令，不套用 Pi RPC stop。保留请求状态供审计，不将 runtime、凭据或真实会话数据入库。
