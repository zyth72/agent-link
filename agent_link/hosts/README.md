# 框架接入指南

## 工具范围

Agent Link 是跨 Agent 框架的小工具集合，提供通信工具，并管理通信所需的父子会话关联。

- 通信：汇报、请求、答复；按框架和适配器实际能力提供 steer、queue、interrupt。
- 会话关联：记录父子 session、框架及对应 Binding、Run、连接信息，定位消息接收方；这些标识不混用。
- 接入说明：每个框架的启动方式、调用方法、支持能力与限制写在该框架 README。

后台运行器负责启动、取消和执行超时，各框架负责模型调用。Pi RPC 控制器持有专用子进程属于适配工具的实现细节；本项目不扩展为通用任务调度框架。原生支持不等于适配已实现，连接成功不等于模型已处理。

调用方从各框架目录的 README 开始：能力、启动、模型配置、就绪检查和清理均在同一文件维护。

| 框架 | 父端 | 子端 | 使用说明 |
| --- | --- | --- | --- |
| Codex Desktop | 有 | 无，`child=None` | [Codex](codex/README.md) |
| Pi | 有 | 有，专用 RPC 控制器 | [Pi](pi/README.md) |
| OMP | 有 | 有，session HTTP 主动推送 | [OMP](omp/README.md) |
| OpenCode | 有 | 有，原生 prompt HTTP 推送（描述符契约） | [OpenCode](opencode/README.md) |

框架原生能力、适配器已实现能力、当前进程是否连接分别判断。入口 `get_adapters()` 返回对象不代表进程已经启动；操作成功也不代表模型已处理。

## 启动责任与公共准备

Agent Link 核心维护 Binding、通信和请求状态。后台运行器负责启动长驻命令、保留输出和原生任务句柄、取消与超时；Pi RPC 控制器则在适配器内部持有专用 Pi 子进程。模型请求由框架负责，模型凭据由框架自己的配置或环境提供，不写进 binding、仓库或日志。

以下命令中的变量由调用方提供，均不是复制历史会话 ID 的示例：

```bash
REPO=/absolute/path/to/agent-link
ROOT=/absolute/path/to/private-runtime
WORK=/absolute/path/to/authorized-workspace
BINDING=unique-binding-id
cd "$REPO"
python3 -m agent_link --root "$ROOT" bind --config /absolute/path/to/binding.json
```

绑定格式和校验见 [USAGE](../../USAGE.md#bind-配置)。`parent.session_id` 必须来自实际父会话；Pi 可预先用 UUID 指定子会话，其他框架须取得实际会话标识后再绑定。`native_task_id` 未知时为 null，不用 run_id 代替。当前 bind 仍要求一个已存在的 notifier 文件，即使非 Codex 父端不调用它。

父端再将以下命令交给后台运行器执行（独立任务、cwd 为 REPO）：

```bash
python3 -m agent_link.host --root "$ROOT" --binding "$BINDING" \
  --framework codex-desktop --credentials "$ROOT/child-credentials.json"
```

`--framework` 选择的是**父端框架**，可为 `codex-desktop`、`pi`、`omp`、`opencode`，与子端框架无关。必须确认 stdout 的 `ready:true` 和凭据文件创建成功后才启动子端；仅 PID 存在不代表就绪。凭据文件由子端读取，禁止打印内容。

服务与控制器分别托管，后台超时覆盖任务时长。任务结束先停止子端，再停止通信服务；正常退出只清理自己持有的凭据或 descriptor。强杀后先检查进程再处理残留文件，不盲目删除仍活跃服务的文件。

## 查询能力与验收

在仓库根读取已实现的静态能力，无需启动框架或调用模型：

```bash
python3 - <<'PYCODE'
import json
from agent_link.hosts import load_adapters
for framework in ('codex-desktop', 'pi', 'omp', 'opencode'):
    adapters = load_adapters(framework)
    print(json.dumps({'framework': framework,
        'parent': adapters.parent.capabilities() if adapters.parent else None,
        'child': adapters.child.capabilities() if adapters.child else None}, ensure_ascii=False))
PYCODE
```

静态能力不是健康检查，也不是已完成统一能力 schema 的承诺。先检查宿主 ready/控制器握手，再做 report/ask/reply 或 ask-child/answer 闭环；分别记录模拟测试、真实接口测试、模型端到端测试。未验证行为保留 unknown。单纯启动、查询能力不需要模型；发送任务并让 agent 执行会使用模型额度。

规范见 [适配器入口](../../docs/HOST-ADAPTERS.md)、[核心规范](../../SPEC.md)、[宿主协议](../../docs/HOST-PROTOCOL.md)。

Pi/OMP 无轮询通知的启动、认证与会话登记见 [SESSION-PUSH](../../docs/SESSION-PUSH.md)。
