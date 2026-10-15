# 子端框架：启动、能力与唤醒

每个框架的权威说明位于仓库 `ABS_REPO/agent_link/hosts/<framework>/README.md`，公共准备读 `ABS_REPO/agent_link/hosts/README.md`。从真实框架取得会话 ID；扩展目录与共享模块必须完整。

| 框架 | 当前接入 | 父→子唤醒 |
| --- | --- | --- |
| Pi | 原生 report/ask/check/answer 工具 + 专用 RPC 控制器 | descriptor ready 时 RPC prompt，已通过真实模型空闲唤醒；无 descriptor 的 inbox 不唤醒 Pi |
| OMP | 扩展工具 + 文件桥/收件箱 watcher | 现有 watcher；无轮询 HTTP 推送仅有临时探针验证 |
| OpenCode v2 | 插件工具 + 文件桥/收件箱 watcher | 当前 watcher；原生 steer/queue/interrupt 尚未接入统一控制接口 |
| Codex | 仅父端适配 | child=None，不支持子端唤醒 |

通信服务的 --framework 选择父端框架，不是子端框架。模型调用由各框架负责，运行命令由后台运行器管理。host_accepted 不等于模型已处理；原生能力、适配已实现能力和当前连接状态分别判断，未验证行为保持 unknown。
